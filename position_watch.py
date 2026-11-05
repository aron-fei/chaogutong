# -*- coding: utf-8 -*-
"""M9 持仓实时跟踪 · 分层操作建议 + 异动弹窗（ADR-0034）

背景
----
原框架里卖出只有一个决策点：**10:40 批处理**（`SELL_STAGES = ("10:40",)`）。问题是
它一天只看一眼，与"次日实盘要盯盘"的需求错配：早盘冲高、盘中资金转向、尾盘跳水
这些信息 10:40 都看不到，用户拿不到任何提示。

本模块补上「实时建议层」，与 10:40 的「正式派单层」**分层协作**：

  · 实时层（本模块）：交易时段每 PW_INTERVAL 秒评估一次 50 万模拟盘持仓，
    综合①主动资金净流入②涨速异动③量比/换手④均价偏离⑤日内位置⑥日K 均线趋势，
    输出 HOLD / HOLD_TIGHT / SELL_HALF / SELL_ALL —— 随时可弹窗提醒。
  · 批处理层（market_scheduler 10:40）：仍是兜底扫一遍，防止实时层漏判。

两者写**同一份** `pending_sell_confirm_<日期>.json`（同 schema、同"保留用户已填
actual_*"合并语义），因此工作台「操作确认台 · 卖出区」随时可确认；全部确认即
立即写库（ADR-0032），未确认的收盘后由兜底流程处理。

铁律
----
**绝不自动平仓**。本模块只产出「建议 + 待确认单」，任何写库都必须经用户确认。

数据源（全部为腾讯 Level-1 快照可算出的真实值，无估算）
----------------------------------------------------
  · 快照：qt.gtimg.cn 批量（现价/昨收/开/外盘/内盘/高/低/额/换手/量比/均价）
  · 日K：新浪 getKLineData（复用 bottom_surge.daily_k，5 分钟缓存）

资金流口径
----------
qt [7]=外盘（主动买）[8]=内盘（主动卖），单位为「手」（科创板为「股」，需 ÷100），
且为**当日累计**。故：
  · 累计主动净买入(元) = (外盘 − 内盘)(手) × 100 × 现价
  · 窗口净流入 = 窗口内累计值之差 → 真实的"这 5 分钟主动资金往哪边走"
  · 归一化 `flow` = 窗口净流入 / 窗口成交额 ∈ [−1, 1]，跨价位可比，避免绝对金额失真
"""

import json
import os
import subprocess
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import config

BASE_DIR = Path(__file__).resolve().parent

# 与 bottom_surge 复用同一套已实测的取数与符号解析，避免第二套实现漂移
from bottom_surge import _num, _qt_symbol, _session, _vol_shou, daily_k

_S = _session()


# ---------------------------------------------------------------------------
# 常量（阈值集中在 config.PW_* ，便于运行时调）
# ---------------------------------------------------------------------------
def _cfg(name, default):
    v = getattr(config, name, None)
    return default if v is None else v


def _reports_dir():
    p = _cfg("PW_REPORTS_DIR", "/Users/williamli/Work/reports")
    return Path(p)


def _backtest_db_path():
    p = _cfg("PW_BACKTEST_DB", "")
    if p:
        return Path(p)
    return _reports_dir() / "backtest_database.json"


ACTION_META = {
    "HOLD":       {"label": "持有",     "level": 3, "cls": "pw-hold"},
    "HOLD_TIGHT": {"label": "持有观察", "level": 3, "cls": "pw-tight"},
    "SELL_HALF":  {"label": "减半",     "level": 2, "cls": "pw-half"},
    "SELL_ALL":   {"label": "全清",     "level": 1, "cls": "pw-all"},
}

EVENT_META = {
    "all_out":   {"icon": "🛑", "label": "全清信号", "level": 1},
    "half_out":  {"icon": "🟡", "label": "减半信号", "level": 2},
    "spike_up":  {"icon": "🚀", "label": "急拉异动", "level": 2},
    "spike_dn":  {"icon": "💧", "label": "跳水异动", "level": 2},
    "flow_in":   {"icon": "🟢", "label": "资金流入", "level": 3},
    "flow_out":  {"icon": "🔴", "label": "资金流出", "level": 2},
    "recover":   {"icon": "⚪", "label": "信号解除", "level": 3},
}

# 卖出建议才会写待确认单
_SELL_ACTIONS = ("SELL_ALL", "SELL_HALF")


def _lot_size(symbol):
    """最小交易单位(股/手)：科创板 688/689 = 200 股，其余 100 股。"""
    return 200 if str(symbol).startswith(("688", "689")) else 100


def _to_lots(shares, symbol):
    n = _lot_size(symbol)
    try:
        return int(shares) // n
    except (TypeError, ValueError):
        return 0


def _f(v, nd=2):
    if v is None:
        return None
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return None


def _parse_quote(code, p):
    """把 qt 单条记录解析成盯盘需要的字段。字段位见项目 MEMORY（qt 字段位表）。"""
    price = _num(p, 3)
    pre = _num(p, 4)
    if not price or not pre:
        return None
    outer_raw = _num(p, 7)
    inner_raw = _num(p, 8)
    outer = _vol_shou(code, outer_raw)      # 手
    inner = _vol_shou(code, inner_raw)
    amt_wan = _num(p, 37)                   # 成交额（万元）
    return {
        "code": code,
        "name": (p[1] or "").strip(),
        "price": price,
        "pre_close": pre,
        "open": _num(p, 5),
        "high": _num(p, 33),
        "low": _num(p, 34),
        "avg_price": _num(p, 51),
        "pct": _f((price - pre) / pre * 100.0, 3),
        "outer": outer,
        "inner": inner,
        "amount_yi": _f((amt_wan or 0) / 1e4, 4),
        "turnover": _num(p, 38),
        "vol_ratio": _num(p, 49),
        "float_cap_yi": _num(p, 44),
    }


def fetch_quotes(codes):
    """批量取持仓快照（通常 1~5 只，一次请求搞定）。返回 {code: fields}。"""
    syms, mapping = [], {}
    for c in codes:
        s = _qt_symbol(c)
        if s:
            syms.append(s)
            mapping[s] = c
    if not syms:
        return {}
    out = {}
    try:
        r = _S.get("https://qt.gtimg.cn/q=" + ",".join(syms), timeout=6)
        r.encoding = "gbk"
        text = r.text
    except Exception as e:
        print(f"[position_watch] 行情拉取失败: {type(e).__name__}: {e}")
        return {}
    for line in text.split(";"):
        line = line.strip()
        if not line.startswith("v_"):
            continue
        seg = line.split('="', 1)
        if len(seg) < 2:
            continue
        sym = seg[0][2:]
        p = seg[1].rstrip('"').split("~")
        if len(p) < 52:
            continue
        code = mapping.get(sym)
        if not code:
            continue
        q = _parse_quote(code, p)
        if q:
            out[code] = q
    return out


# ---------------------------------------------------------------------------
# 趋势（日K 均线 + 60 日位置）—— 低频，日K 自带 5 分钟缓存
# ---------------------------------------------------------------------------
class TrendCache:
    def __init__(self, ttl=300):
        self.ttl = ttl
        self._lock = threading.Lock()
        self._d = {}       # code -> (ts, trend_dict)

    def get(self, code):
        with self._lock:
            hit = self._d.get(code)
            if hit and time.time() - hit[0] < self.ttl:
                return hit[1]
        t = self._compute(code)
        with self._lock:
            if t is not None:
                self._d[code] = (time.time(), t)
        return t

    @staticmethod
    def _compute(code):
        """用「不含今日」的日K 算均线，再拿现价去比 → 盘中不会被当日 K 线自我污染。"""
        try:
            k = daily_k(code, n=60)
        except Exception as e:
            print(f"[position_watch] {code} 日K失败: {type(e).__name__}: {e}")
            return None
        if not k or len(k) < 21:
            return None
        closes = [float(x["close"]) for x in k]
        base = closes[:-1]                     # 剔除今日那根（盘中是未完成 K）
        if len(base) < 20:
            return None

        def ma(n):
            return sum(base[-n:]) / float(n)

        lo, hi = min(closes), max(closes)
        return {
            "ma5": _f(ma(5)), "ma10": _f(ma(10)), "ma20": _f(ma(20)),
            "range_lo": _f(lo), "range_hi": _f(hi),
            "k_n": len(closes),
        }


# ---------------------------------------------------------------------------
# 实时盯盘引擎
# ---------------------------------------------------------------------------
class PositionWatch:
    def __init__(self):
        self.lock = threading.RLock()
        self._hist = {}            # code -> deque[(ts, price, cum_net_outer(手), amount_yi)]
        self._rows = {}            # code -> 最近一次评估结果
        self._events = deque(maxlen=int(_cfg("PW_EVENTS_MAX", 300)))
        self._seq = 0
        self._cool = {}            # (kind, code) -> ts
        # 【2026-10-21】卖出建议连续确认计数：(code, action) -> 连续成立轮数
        # 需连续 PW_SELL_CONFIRM_ROUNDS 轮才升级为"已建议"（才写单、才弹窗）
        self._sell_rounds = {}
        self._trend = TrendCache(ttl=int(_cfg("PW_TREND_TTL", 300)))
        self._holdings_provider = None
        self._mkt_temp_provider = None
        self._day = ""
        self._thread = None
        self._stop = False
        self._closing_done = ""    # 收盘兜底写库的日期锁
        self._had_advice = False   # 上一轮是否写过卖出建议（用于「信号全部解除→撤回」）
        self._stats = {
            "ticks": 0, "last_tick": None, "last_error": None,
            "holdings": 0, "events_total": 0, "sell_files": 0,
            "last_quotes_at": None,
        }
        self.errors = []

    # ---------------- 注入 ----------------
    def set_holdings_provider(self, fn):
        """注入持仓来源（默认读 50 万模拟盘 backtest_database.json）。"""
        self._holdings_provider = fn

    def set_mkt_temp_provider(self, fn):
        """注入大盘温度来源（0~100，可返回 None）。"""
        self._mkt_temp_provider = fn

    # ---------------- 内部工具 ----------------
    def _err(self, tag, exc):
        msg = f"[{datetime.now():%H:%M:%S}] position_watch-{tag}: {type(exc).__name__} {exc}"
        print(msg)
        with self.lock:
            self._stats["last_error"] = msg
            self.errors.append(msg)
            if len(self.errors) > 20:
                self.errors.pop(0)

    def _load_holdings(self):
        """持仓列表。默认读回测库 active_positions（只读，绝不改写）。"""
        if self._holdings_provider:
            try:
                return list(self._holdings_provider() or [])
            except Exception as e:
                self._err("holdings", e)
                return []
        try:
            raw = json.loads(_backtest_db_path().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except Exception as e:
            self._err("db", e)
            return []
        out = []
        for p in raw.get("active_positions", []) or []:
            sym = str(p.get("symbol") or "").strip()
            shares = int(p.get("shares") or 0)
            if not sym or shares <= 0:
                continue
            out.append({
                "symbol": sym,
                "name": p.get("name") or sym,
                "shares": shares,
                "buy_price": float(p.get("buy_price") or 0),
                "buy_fee": float(p.get("buy_fee") or 0),
                "capital": float(p.get("capital") or 0),
                "buy_date": p.get("buy_date") or "",
                "buy_session": p.get("buy_session") or "",
            })
        return out

    def _mkt_temp(self):
        if not self._mkt_temp_provider:
            return None
        try:
            v = self._mkt_temp_provider()
            return float(v) if v is not None else None
        except Exception as e:
            self._err("mkt_temp", e)
            return None

    def _push_hist(self, code, ts, q):
        net_outer = (q.get("outer") or 0.0) - (q.get("inner") or 0.0)   # 手，当日累计
        dq = self._hist.get(code)
        if dq is None:
            dq = deque(maxlen=int(_cfg("PW_HIST_MAXLEN", 120)))
            self._hist[code] = dq
        dq.append((ts, q["price"], net_outer, q.get("amount_yi") or 0.0))
        # 裁掉超出保留窗口的旧帧
        keep = int(_cfg("PW_HIST_SECONDS", 900))
        while len(dq) > 1 and ts - dq[0][0] > keep:
            dq.popleft()
        return dq

    @staticmethod
    def _at_or_before(dq, target):
        """取时间戳 ≤ target 的最后一帧（时间戳回溯，不依赖帧间隔）。"""
        best = None
        for row in dq:
            if row[0] <= target:
                best = row
            else:
                break
        return best

    def _win(self, dq, ts, window):
        """回溯 window 秒的起点帧。太久远（>1.8×window）视为无效。"""
        ref = self._at_or_before(dq, ts - window)
        if ref is None:
            return None
        if ts - ref[0] > window * 1.8:
            return None
        return ref

    def _flows(self, dq, ts, price):
        """窗口资金流：返回 (净流入元, 成交额元, 归一化 flow ∈ [-1,1])。

        ⚠️ 单位（2026-09-30 修复，此前为 P0 bug）：hist 第 4 位存的是 amount_yi（**亿**），
        差分后须 ×1e8 折算为元。旧代码误用 ×1e4（把亿差当万元差），分母缩小 1 万倍 →
        flow 恒被钳到 ±1.0 → 弹窗永远显示「±100%」且随净流入符号来回翻转
        （全清→解除→又全清的弹窗风暴根因，实测 2026-09-30 13:56-14:16 事件流）。
        """
        w = int(_cfg("PW_FLOW_WINDOW", 300))
        ref = self._win(dq, ts, w)
        if ref is None:
            return None, None, None
        cur = dq[-1]
        d_net_outer = cur[2] - ref[2]                       # 手
        inflow = d_net_outer * 100.0 * price                # 元（主动买 − 主动卖）
        d_amt = (cur[3] - ref[3]) * 1e8                     # 亿 → 元
        if d_amt <= 0:
            return _f(inflow, 0), 0.0, None
        ratio = max(-1.0, min(1.0, inflow / d_amt))
        return _f(inflow, 0), _f(d_amt, 0), _f(ratio, 4)

    def _speeds(self, dq, ts):
        out = {}
        for w in _cfg("PW_SP_WINDOWS", [60, 180]):
            ref = self._win(dq, ts, w)
            if ref is None or not ref[1]:
                out[w] = None
                continue
            out[w] = _f((dq[-1][1] - ref[1]) / ref[1] * 100.0, 3)
        wins = _cfg("PW_SP_WINDOWS", [60, 180])
        return out.get(wins[0]), (out.get(wins[1]) if len(wins) > 1 else None)

    # ---------------- 决策矩阵 ----------------
    def decide_watch(self, ctx):
        """分层操作建议的唯一权威矩阵（纯函数，便于测试）。

        ctx 字段（缺省视为不可用，规则自动跳过）：
          pct 当日涨跌% / sp60 1分钟涨速% / sp180 3分钟涨速% / flow 5分钟主动净流入占比
          price 现价 / avg 均价 / day_pos 日内位置0~1 / pnl_pct 持仓浮盈%
          trend 'up'|'down'|'flat' / pos60 60日位置0~1 / mkt_temp 大盘温度
        返回 (action, reason)
        """
        g = ctx.get
        pct = g("pct")
        sp60 = g("sp60")
        flow = g("flow")
        price = g("price")
        avg = g("avg")
        day_pos = g("day_pos")
        pnl_pct = g("pnl_pct")
        trend = g("trend")
        pos60 = g("pos60")
        mkt_temp = g("mkt_temp")

        stop = float(_cfg("PW_STOP_PCT", -5.0))
        weak = float(_cfg("PW_WEAK_PCT", -2.0))
        f_in = float(_cfg("PW_FLOW_IN", 0.15))
        f_out = float(_cfg("PW_FLOW_OUT", -0.18))
        spike_dn = float(_cfg("PW_SPIKE_DOWN", -1.2))
        take = float(_cfg("PW_TAKE_PNL", 6.0))
        below = float(_cfg("PW_BELOW_AVG", 0.996))
        low_pos = float(_cfg("PW_LOW_POS", 0.15))
        t_low = float(_cfg("PW_MKT_TEMP_LOW", 50))

        below_avg = (price is not None and avg and avg > 0 and price < avg * below)

        # ---------- 一、封板：最强状态，优先锁仓 ----------
        if pct is not None and pct >= 9.5:
            return "HOLD", f"涨停封板（{pct:+.2f}%），锁仓不动"

        # ---------- 二、全清（硬性风控优先） ----------
        if pct is not None and pct <= stop:
            return "SELL_ALL", f"当日 {pct:+.2f}% 破位深跌，触发止损全清"

        if sp60 is not None and sp60 <= spike_dn and below_avg:
            return "SELL_ALL", f"1分钟急跌 {sp60:+.2f}% 且跌破均价，异动向下全清"

        if flow is not None and flow <= f_out and (
                (sp60 is not None and sp60 <= -0.3) or (pct is not None and pct <= -1.0)):
            return "SELL_ALL", f"5分钟主动资金净流出占成交额 {flow*100:.0f}%，资金转向全清"

        if (day_pos is not None and day_pos <= low_pos and pct is not None and pct < 0
                and below_avg):
            return "SELL_ALL", f"贴日内低点（位置 {day_pos*100:.0f}%）且价低于均价，主动卖压占优，全清"

        # ---------- 三、减半 ----------
        if (pnl_pct is not None and pnl_pct >= take and sp60 is not None and sp60 <= -0.6):
            return "SELL_HALF", f"浮盈 {pnl_pct:+.2f}% 后 1 分钟转跌 {sp60:+.2f}%，冲高回落先落袋一半"

        if pct is not None and pct <= weak and flow is not None and flow < 0:
            return "SELL_HALF", f"当日 {pct:+.2f}% 弱势且 5 分钟资金净流出，先减半防守"

        if below_avg and pct is not None and pct < 0:
            return "SELL_HALF", f"跌破均价 {avg:.2f} 且当日 {pct:+.2f}%，弱于当日成本线，减半"

        # ---------- 四、强势但需盯 ----------
        if (pct is not None and pct >= 4.0 and mkt_temp is not None and mkt_temp < t_low):
            return "HOLD_TIGHT", f"当日 {pct:+.2f}% 强势但大盘温度仅 {mkt_temp:.0f}（偏冷），可择机止盈"

        # ---------- 五、持有：资金持续流入 + 趋势向上 ----------
        if (flow is not None and flow >= f_in and (price is None or avg is None or price >= avg)
                and trend == "up"):
            return "HOLD", f"5分钟主动资金净流入占成交额 {flow*100:.0f}% 且均线多头，资金推动持股"

        if flow is not None and flow >= f_in:
            return "HOLD", f"5分钟主动资金净流入占成交额 {flow*100:.0f}%，资金偏多，持股观察"

        # ---------- 六、默认 ----------
        if trend == "down" and pct is not None and pct < 0:
            return "HOLD_TIGHT", f"当日 {pct:+.2f}% 且均线转空（现价低于 MA5/MA10），收紧观察"
        if pos60 is not None and pos60 >= 0.85 and pct is not None and pct < 0:
            return "HOLD_TIGHT", f"60日高位（{pos60*100:.0f}%）+ 当日 {pct:+.2f}% 走弱，收紧观察"
        return "HOLD", "无异常信号，正常持有"

    # ---------------- tick ----------------
    def tick(self, force=False, warm=False):
        """跑一轮评估。返回本轮新产生的事件数。force=True 跳过交易时段判断（测试用）。

        warm=True 为「冷启动预热」：只建立快照基线（填 self._rows），不产生事件、
        不写待确认卖出单。用于进程启动时先取一次最近状态，避免重启后（尤其非交易时段）
        打开 tab 显示"无持仓"，也避免把基线状态当成"建议变化"重复弹窗。
        """
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        st = self._market_status()
        trading = (st == "trading")
        if not force and not trading:
            if st == "closed":
                self._maybe_confirm_at_close(now)
            # 2026-09-30（ADR-0038 修订）：休市时段 DB 持仓仍可能变化（盘后在工作台
            # 确认卖出/补记成交）。原实现直接 return，快照停留在线 → 盯盘页出现
            # 「现金是新的、手数是旧的」自相矛盾。检测到手数/数量漂移时用 warm tick
            # 重建基线（force 跳过时段判断；warm 不产生事件、不写待确认单）。
            try:
                holds_now = {h["symbol"]: int(h["shares"] or 0)
                             for h in self._load_holdings()}
                with self.lock:
                    cur = self._rows or {}
                    drift = (len(cur) != len(holds_now)) or any(
                        int((cur.get(sym) or {}).get("shares") or 0) != sh
                        for sym, sh in holds_now.items())
                if drift:
                    print("[position_watch] 休市对账：持仓与快照不一致 → warm 重建")
                    self.tick(force=True, warm=True)
            except Exception as e:
                self._err("afterhours_sync", e)
            return 0

        if self._day != today:      # 跨日重置
            with self.lock:
                self._day = today
                self._hist = {}
                self._rows = {}
                self._cool = {}
                self._sell_rounds = {}
            print(f"[position_watch] 跨日重置 → {today}")

        holds = self._load_holdings()
        with self.lock:
            self._stats["holdings"] = len(holds)
        if not holds:
            with self.lock:
                self._rows = {}
            return 0

        quotes = fetch_quotes([h["symbol"] for h in holds])
        ts = time.time()
        with self.lock:
            self._stats["ticks"] += 1
            self._stats["last_tick"] = now.strftime("%Y-%m-%d %H:%M:%S")
            self._stats["last_quotes_at"] = self._stats["last_tick"]

        mkt_temp = self._mkt_temp()
        new_events = []
        rows = {}
        advice_sells = []
        evaluated_ok = 0          # 本轮成功取到行情并评估的持仓数（用于判断"无建议"是否为数据缺失所致）

        for h in holds:
            code = h["symbol"]
            q = quotes.get(code)
            if not q:
                rows[code] = {
                    "symbol": code, "name": h["name"], "shares": h["shares"],
                    "lots": _to_lots(h["shares"], code), "price": None,
                    "action": "HOLD", "action_label": "数据缺失", "level": 3,
                    "reason": "无法获取实时行情，本轮不评估", "stale": True,
                    "buy_price": _f(h["buy_price"], 4), "pnl_pct": None,
                }
                continue
            dq = self._push_hist(code, ts, q)
            sp60, sp180 = self._speeds(dq, ts)
            inflow, amt_win, flow = self._flows(dq, ts, q["price"])
            tr = self._trend.get(code) or {}

            price = q["price"]
            avg = q.get("avg_price")
            hi, lo = q.get("high"), q.get("low")
            day_pos = None
            if hi is not None and lo is not None and hi > lo:
                day_pos = _f((price - lo) / (hi - lo), 3)
            pos60 = None
            if tr.get("range_hi") and tr.get("range_lo") and tr["range_hi"] > tr["range_lo"]:
                pos60 = _f((price - tr["range_lo"]) / (tr["range_hi"] - tr["range_lo"]), 3)
            trend = "flat"
            if tr.get("ma5") and tr.get("ma10"):
                if price > tr["ma5"] > tr["ma10"]:
                    trend = "up"
                elif price < tr["ma5"] < tr["ma10"]:
                    trend = "down"

            pnl = (price - h["buy_price"]) * h["shares"] if h["buy_price"] else None
            pnl_pct = ((price - h["buy_price"]) / h["buy_price"] * 100.0) if h["buy_price"] else None

            locked = bool(h["buy_date"]) and h["buy_date"] >= today   # T+1 锁仓
            if locked:
                action, reason = "HOLD", f"T+1 锁仓中（{h['buy_date']} {h['buy_session']} 买入），今日不可卖"
            else:
                action, reason = self.decide_watch({
                    "pct": q["pct"], "sp60": sp60, "sp180": sp180,
                    "flow": flow, "price": price, "avg": avg, "day_pos": day_pos,
                    "pnl_pct": pnl_pct, "trend": trend, "pos60": pos60,
                    "mkt_temp": mkt_temp,
                })

            row = {
                "symbol": code, "name": h["name"],
                "shares": h["shares"], "lots": _to_lots(h["shares"], code),
                "buy_price": _f(h["buy_price"], 4), "buy_date": h["buy_date"],
                "buy_session": h["buy_session"],
                "price": _f(price, 3), "pct": _f(q["pct"], 2),
                "avg_price": _f(avg, 3),
                "vs_avg": _f((price - avg) / avg * 100.0, 3) if avg else None,
                "day_pos": day_pos, "pos60": pos60,
                "high": _f(hi, 3), "low": _f(lo, 3), "pre_close": _f(q["pre_close"], 3),
                "sp60": sp60, "sp180": sp180,
                "flow": flow, "inflow_5m": inflow, "amt_5m": amt_win,
                "inflow_total": _f(((q.get("outer") or 0) - (q.get("inner") or 0)) * 100.0 * price, 0),
                "vol_ratio": _f(q.get("vol_ratio"), 2),
                "turnover": _f(q.get("turnover"), 2),
                "amount_yi": _f(q.get("amount_yi"), 3),
                "ma5": tr.get("ma5"), "ma10": tr.get("ma10"), "ma20": tr.get("ma20"),
                "trend": trend,
                "capital": _f(h["capital"], 2),
                "mkt_value": _f(price * h["shares"], 2),
                "pnl": _f(pnl, 2), "pnl_pct": _f(pnl_pct, 2),
                "action": action,
                "action_label": ACTION_META.get(action, {}).get("label", action),
                "level": ACTION_META.get(action, {}).get("level", 3),
                "reason": reason, "locked": locked,
                "updated": now.strftime("%H:%M:%S"),
            }
            rows[code] = row
            evaluated_ok += 1

            # ---- 事件（异动 / 建议变化） ----
            prev = self._rows.get(code) or {}
            if not warm:
                new_events += self._emit_events(row, prev, ts, dq)

            if action in _SELL_ACTIONS and not locked:
                # 【2026-10-21】连续确认 + 开盘冷静期闸门。
                # 未连续确认 → 只记事件、不写确认单、不弹窗（降噪核心）。
                _conf, _rounds = self._sell_confirmed(code, action, row)
                _quiet = self._in_open_quiet(ts)
                self._log_sell_event(code, h["name"], action, reason,
                                     price, q["pct"], _conf, _rounds, _quiet)
                if _conf and not _quiet:
                    advice_sells.append({
                        "name": h["name"], "symbol": code, "action": action,
                        "held_shares": h["shares"],
                        "recommend_sell_shares": (h["shares"] if action == "SELL_ALL"
                                                  else max(_lot_size(code),
                                                           (_to_lots(h["shares"], code) // 2) * _lot_size(code))),
                        "price": price, "pct": q["pct"], "note": reason,
                    })

        with self.lock:
            self._rows = rows
            for ev in new_events:
                self._events.append(ev)
            self._stats["events_total"] += len(new_events)

        # 只在交易时段写待确认卖出单：收盘后（含手动 ?force=1 刷新）不再新增/撤回，
        # 避免生成"收盘后冒出来、次日早已失效"的卖出建议。
        if not warm and _cfg("PW_EMIT_SELL_CONFIRM", True) and trading:
            if advice_sells:
                self._write_sell_confirm(advice_sells)
                self._had_advice = True
            elif self._had_advice and evaluated_ok == len(holds):
                # 上一轮有卖出建议、本轮全部持仓均评估成功但已无卖出建议 → 撤回待确认单
                # （用户已确认的项仍保留）。对应用户要求：资金持续流入 / 趋势向好时提示持仓，
                # 而不是让 10:40 的卖出建议挂一整天。evaluated_ok 守卫避免"行情取不到→误撤回"。
                self._write_sell_confirm([])
                self._had_advice = False
        return len(new_events)

    # ---------------- 卖出建议连续确认（2026-10-21）----------------
    def _sell_confirmed(self, code, action, row):
        """卖出建议是否已「连续成立足够轮次」→ (confirmed, rounds)。

        背景（2026-10-21 实测）：09:17-09:18 连续 7 轮产生"减半"建议，全天
        "更新→撤回→更新"循环 20+ 次。根因：卖出建议**立即生效**，
        一根 1 分钟 K 线的瞬时波动就能翻转结论 —— 而集合竞价阶段的
        1 分钟数据本身极不准确（竞价撮合就是一次价格跳变）。

        改进：同一标的同一动作需**连续 PW_SELL_CONFIRM_ROUNDS 轮**成立，
        才升级为"已建议"（才写确认单、才弹窗）。中途变回 HOLD 则计数清零。
        """
        need = int(_cfg("PW_SELL_CONFIRM_ROUNDS", 2))
        if need <= 1:
            return True, 1
        key = (code, action)
        rounds = self._sell_rounds.get(key, 0) + 1
        self._sell_rounds[key] = rounds
        # 清掉同标的其他动作的计数（动作切换 → 重新计数）
        for k in list(self._sell_rounds.keys()):
            if k[0] == code and k[1] != action:
                del self._sell_rounds[k]
        return rounds >= need, rounds

    def _in_open_quiet(self, ts=None):
        """是否处于开盘冷静期（09:30 起 N 分钟内）→ bool。

        冷静期内**卖出建议只记录不弹窗**（也不写确认单）——
        竞价/早盘初期价格剧烈，"减半"信号噪音极高，弹出来只会误导操作。
        """
        quiet = int(_cfg("PW_OPEN_QUIET_MINUTES", 15))
        if quiet <= 0:
            return False
        now = datetime.fromtimestamp(ts) if ts else datetime.now()
        t = now.hour * 60 + now.minute
        open_t = 9 * 60 + 30
        return open_t <= t < open_t + quiet

    def _log_sell_event(self, code, name, action, reason, price, pct, confirmed, rounds, quiet):
        """卖出建议事件落盘（可追溯）—— 解决"M9 弹窗不落日志，历史无法回溯"。"""
        if not _cfg("PW_SELL_LOG_ENABLED", True):
            return
        try:
            path = _cfg("PW_SELL_LOG_FILE", "/Users/williamli/Work/reports/pw_events.jsonl")
            rec = {
                "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "code": code, "name": name, "action": action,
                "reason": reason, "price": price, "pct": pct,
                "rounds": rounds, "confirmed": confirmed, "quiet": quiet,
            }
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:
            self._err("sell_log", e)

    # ---------------- 事件 ----------------
    def _emit_events(self, row, prev, ts, dq):
        """异动 + 建议升级事件。同 (kind, code) 冷却内不重复。

        降频过滤（2026-09-30，用户反馈「弹窗乱跳干扰盯盘」）：
          1. 资金流事件用独立更长冷却 + 更严告警阈值 + 窗口成交额下限（流动性真空期不报）；
          2. 已处于卖出建议（全清/减半）状态时，不再重复弹 资金流出/跳水 噪音
             （操作指令已通过 all_out/half_out 传达，重复弹只助长追涨杀跌）；
          3. 资金流文案改为「净额(万) + 占比」，不再出现误导性的 ±100% 裸占比。
        """
        evs = []
        code, name = row["symbol"], row["name"]
        cd = int(_cfg("PW_COOLDOWN", 600))
        fcd = int(_cfg("PW_FLOW_COOLDOWN", 900))
        pct, sp60, flow = row.get("pct"), row.get("sp60"), row.get("flow")
        price = row.get("price")
        amt_5m = row.get("amt_5m") or 0.0

        def cooled(kind):
            key = (kind, code)
            last = self._cool.get(key)
            kind_cd = fcd if kind in ("flow_in", "flow_out") else cd
            if last is not None and ts - last < kind_cd:
                return True
            self._cool[key] = ts
            return False

        def push(kind, text, level=None):
            meta = EVENT_META.get(kind, {"icon": "•", "label": kind, "level": 3})
            self._seq += 1
            evs.append({
                "id": self._seq,
                "t": datetime.fromtimestamp(ts).strftime("%H:%M:%S"),
                "kind": kind, "icon": meta["icon"], "label": meta["label"],
                "level": level if level is not None else meta["level"],
                "code": code, "name": name, "price": price, "pct": pct,
                "action": row.get("action"), "action_label": row.get("action_label"),
                "text": text,
            })

        selling = row.get("action") in _SELL_ACTIONS   # 已在卖出建议状态 → 抑制重复噪音

        # 1) 建议升级：全清 / 减半（这是用户最关心的"操作提示"）
        # 【2026-10-21】开盘冷静期内不弹卖出提示 —— 竞价/早盘初期价格剧烈，
        # "1分钟转跌"判据噪音极高（2026-10-21 09:17-09:18 连续 7 轮误触发的根因）。
        # 冷静期内的判定仍会写入 pw_events.jsonl（可追溯），只是不打扰用户。
        _quiet = self._in_open_quiet(ts)
        prev_action = prev.get("action")
        if row["action"] == "SELL_ALL" and prev_action != "SELL_ALL" and not row.get("locked"):
            if not _quiet and not cooled("all_out"):
                push("all_out", f"【全清】{row['reason']}（现价 {price}，{pct:+.2f}%）")
        elif row["action"] == "SELL_HALF" and prev_action not in ("SELL_HALF", "SELL_ALL") \
                and not row.get("locked"):
            if not _quiet and not cooled("half_out"):
                push("half_out", f"【减半】{row['reason']}（现价 {price}，{pct:+.2f}%）")
        elif row["action"] in ("HOLD", "HOLD_TIGHT") and prev_action in _SELL_ACTIONS:
            if not cooled("recover"):
                push("recover", f"卖出信号解除 → {row['action_label']}：{row['reason']}")

        # 2) 价格异动（跳水在卖出建议状态下不重复报）
        up = float(_cfg("PW_SPIKE_UP", 1.2))
        dn = float(_cfg("PW_SPIKE_DOWN", -1.2))
        if sp60 is not None and sp60 >= up and pct is not None and pct < 9.5:
            if not cooled("spike_up"):
                push("spike_up", f"1分钟急拉 {sp60:+.2f}%（现价 {price}，当日 {pct:+.2f}%）")
        if sp60 is not None and sp60 <= dn and not selling:
            if not cooled("spike_dn"):
                push("spike_dn", f"1分钟跳水 {sp60:+.2f}%（现价 {price}，当日 {pct:+.2f}%）")

        # 3) 资金流异动：严阈值 + 窗口成交额下限 + 卖出状态下静默
        min_amt = float(_cfg("PW_FLOW_MIN_AMT", 5e6))
        a_in = float(_cfg("PW_FLOW_ALERT_IN", 0.35))
        a_out = float(_cfg("PW_FLOW_ALERT_OUT", -0.35))
        inflow = row.get("inflow_5m")
        if not selling and flow is not None and amt_5m >= min_amt:
            if flow >= a_in:
                if not cooled("flow_in"):
                    push("flow_in", f"5分钟主动净流入 {inflow/1e4:.0f}万（占窗口成交额 {flow*100:.0f}%）")
            elif flow <= a_out:
                if not cooled("flow_out"):
                    push("flow_out", f"5分钟主动净流出 {abs(inflow)/1e4:.0f}万（占窗口成交额 {flow*100:.0f}%）")

        return evs

    # ---------------- 待确认卖出单（与 market_scheduler 同 schema / 同合并语义） ----------------
    def _sell_path(self):
        return _reports_dir() / f"pending_sell_confirm_{datetime.now():%Y-%m-%d}.json"

    def _write_sell_confirm(self, recs):
        """写/更新当日待确认卖出单。

        语义与 market_scheduler.write_pending_sell_confirm 完全对齐：
          · 保留用户已填的 actual_sell / actual_lots / actual_shares / actual_sell_price
          · 建议升级时刷新 recommend_sell_* 与 note（用户能看到"建议从减半变全清"）
          · 本轮不再建议卖出的标的：若用户已填 actual_sell=true 则保留，否则移除
            （避免"信号已解除但待确认单还挂着"造成误操作）
        """
        path = self._sell_path()
        prev_items = {}
        if path.exists():
            try:
                d = json.loads(path.read_text(encoding="utf-8"))
                for it in d.get("items", []) or []:
                    prev_items[str(it.get("symbol"))] = it
            except Exception as e:
                self._err("sell_read", e)

        cur = {str(r["symbol"]): r for r in recs}
        items = []
        for sym, r in cur.items():
            prev = prev_items.get(sym, {})
            held_lots = _to_lots(r["held_shares"], sym)
            rec_lots = _to_lots(r["recommend_sell_shares"], sym)
            items.append({
                "name": r.get("name", ""), "symbol": sym,
                "action": r.get("action", "SELL_ALL"),
                "held_lots": held_lots, "held_shares": int(r["held_shares"]),
                "recommend_sell_lots": rec_lots,
                "recommend_sell_shares": int(r.get("recommend_sell_shares") or 0),
                "price": float(r.get("price") or 0), "pct": float(r.get("pct") or 0),
                "note": r.get("note", ""),
                "actual_sell": bool(prev.get("actual_sell", False)),
                "actual_lots": int(prev.get("actual_lots", 0)),
                "actual_shares": int(prev.get("actual_shares", 0)),
                "actual_sell_price": float(prev.get("actual_sell_price", 0.0)),
            })
        # 用户已确认卖出的历史项不能丢（否则写库时找不到它）
        for sym, it in prev_items.items():
            if sym in cur:
                continue
            if it.get("actual_sell"):
                items.append(it)

        if not items:
            # 本轮无任何卖出建议（用户已确认的历史项在合并时已保留，故为空即真的没有）
            # → 撤回待确认单，避免"信号已解除但旧建议还挂着"导致误卖。
            try:
                if path.exists():
                    path.unlink()
                    print(f"[position_watch] 待确认卖出单已撤回（本轮无卖出建议）: {path.name}")
            except Exception as e:
                self._err("sell_unlink", e)
            return None
        data = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "deadline": "15:00",
            "status": "pending",
            "instruction": (
                "请在 15:00 前编辑本文件：将每只你【实际已卖出】的标的 actual_sell 改为 true，"
                "并填写 actual_lots(实际卖出手数，1手=最小交易单位：主板100股/科创板200股) 与 "
                "actual_sell_price(实际卖出价，留0则用15:00现价)。填满 held_lots 视为全清(含零股)。"
                "15:00 程序自动按 confirmed 项平仓；超时未确认视为未卖出，不平仓。"
                "（本单由持仓实时跟踪 M9 与 10:40 批处理共同维护，建议会随行情升级/解除）"),
            "source": "position_watch+m10:40",
            "items": items,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            with self.lock:
                self._stats["sell_files"] += 1
            print(f"[position_watch] 待确认卖出单已更新: {path.name}（{len(items)} 项）")
        except Exception as e:
            self._err("sell_write", e)
            return None
        return path

    # ---------------- 收盘兜底写库 ----------------
    def _maybe_confirm_at_close(self, now):
        """收盘后补一次：若当日待确认卖出单里**用户已勾选确认**的项，交给权威脚本写库。

        只做「调用既有 CLI」，不重复实现平仓逻辑。触发条件刻意收紧为
        「存在 actual_sell=true 的项」——用户没确认就绝不唤醒写库流程，
        连子进程都不起（避免无意义 spawn；未确认的持仓本就不该被动）。
        """
        at = str(_cfg("PW_AUTO_CONFIRM_AT", "15:00"))
        try:
            hh, mm = (int(x) for x in at.split(":"))
        except Exception:
            return
        if (now.hour * 60 + now.minute) < hh * 60 + mm:
            return
        today = now.strftime("%Y-%m-%d")
        if self._closing_done == today:
            return
        path = self._sell_path()
        if not path.exists():
            return
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            confirmed = [i for i in (d.get("items") or []) if i.get("actual_sell")]
        except Exception as e:
            self._err("close_read", e)
            return
        if not confirmed:
            self._closing_done = today
            return
        script = str(_cfg("PW_ADVISOR_SCRIPT", ""))
        py = str(_cfg("PW_ADVISOR_PYTHON", ""))
        if not script or not os.path.exists(script) or not os.path.exists(py):
            return
        self._closing_done = today
        try:
            subprocess.Popen(
                [py, script, "confirm", "--now", "--sell"],
                cwd=os.path.dirname(script),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
            print(f"[position_watch] 收盘兜底：已触发卖出确认写库（{len(confirmed)} 项已确认）")
        except Exception as e:
            self._err("close_spawn", e)

    # ---------------- 状态 ----------------
    @staticmethod
    def _market_status():
        try:
            import data_fetcher
            st, _ = data_fetcher.market_status()
            return st
        except Exception:
            return "unknown"

    def snapshot(self, since=0, force=False):
        """返回可直接 jsonify 的 dict。since > 0 时只带增量事件（前端游标）。"""
        if force:
            try:
                self.tick(force=True)
            except Exception as e:
                self._err("force", e)
        with self.lock:
            rows = list(self._rows.values())
            evs = [e for e in self._events if e["id"] > since]
            st = self._market_status()
            acct = self._account(rows)
            return {
                "mk": "position_watch",
                "state": "ok" if rows else ("idle" if not self._stats["holdings"] else "init"),
                "error": self._stats.get("last_error"),
                "asof": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "day": self._day,
                "market": st,
                "trading": st == "trading",
                "seq": self._seq,
                "stats": dict(self._stats),
                "account": acct,
                "rows": rows,
                "events": evs,
            }

    @staticmethod
    def _account(rows):
        mv = sum((r.get("mkt_value") or 0) for r in rows)
        cap = sum((r.get("capital") or 0) for r in rows)
        pnl = sum((r.get("pnl") or 0) for r in rows)
        cash = None
        try:
            raw = json.loads(_backtest_db_path().read_text(encoding="utf-8"))
            cash = float(raw.get("available_cash") or 0)
        except Exception:
            pass
        return {
            "available_cash": _f(cash, 2),
            "holdings_value": _f(mv, 2),
            "cost_basis": _f(cap, 2),
            "unrealized": _f(pnl, 2),
            "total_equity": _f((cash or 0) + mv, 2) if cash is not None else None,
        }

    # ---------------- 线程 ----------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop = False
        self._thread = threading.Thread(target=self._loop, name="position-watch", daemon=True)
        self._thread.start()
        print("[position_watch] 实时持仓盯盘线程已启动")

    def _loop(self):
        interval = int(_cfg("PW_INTERVAL", 15))
        # 冷启动预热：先取一轮建立基线（不弹窗、不写待确认单）。
        # 这样即使进程在非交易时段启动，打开 tab 也能看到最近一次评估结果，
        # 而不是误报"无持仓"。
        try:
            self.tick(force=True, warm=True)
        except Exception as e:
            self._err("warmup", e)
        while not self._stop:
            try:
                self.tick()
            except Exception as e:
                self._err("loop", e)
            time.sleep(interval)

    def stop(self):
        self._stop = True


_INSTANCE = None
_INSTANCE_LOCK = threading.Lock()


def instance():
    global _INSTANCE
    if _INSTANCE is None:
        with _INSTANCE_LOCK:
            if _INSTANCE is None:
                _INSTANCE = PositionWatch()
    return _INSTANCE

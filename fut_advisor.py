# -*- coding: utf-8 -*-
"""期货超短顾问（M6，ADR-0026）：T+0 日内超短信号引擎

目标：给用户「一句人话」——品种/主力合约/方向/进场价/止损/目标/手数/当日进度，
像港股视图一样极简，数据全免费（新浪 hq 实时 + getMinLine 当日分时 + zhibo 7x24 快讯）。

铁律（对齐 vault 偏好）：
  - 数据可追溯：价/均价/涨跌全部来自新浪实时返回，不臆造数字
  - 风险先行：先算止损距离×乘数×手数 是否超出单笔风险预算；超预算 = 明确说「不可行」，
    绝不硬凑一手（生猪 1 手技术止损就可能 -2000，单笔风险 300 预算必须诚实拒绝）
  - 不留 except: pass；每类错误记录并打印
  - 只参谋不替人下单；用户手工执行，可手动记录已实现盈亏算当日进度

数据源（沙箱实测 2026-09-07 可达）：
  - 实时:  https://hq.sinajs.cn/list=nf_LH2611,...  （具体合约代码=大写，44 列商品结构）
  - 当日分时: stock2.finance.sina.com.cn ... getMinLine?symbol=LH2611 → [时间,价,均价,量,持仓]
  - 快讯:  https://zhibo.sina.com.cn/api/zhibo/feed?...&zhibo_id=152 （7x24 财经直播）
"""
import json
import re
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime
from pathlib import Path

import config

_DATA_DIR = Path(__file__).parent / "data"
PROFILE_FILE = _DATA_DIR / "fut_profile.json"
WATCH_FILE = _DATA_DIR / "fut_watchlist.json"
_HDR = {"Referer": "https://finance.sina.com.cn/", "User-Agent": "Mozilla/5.0"}
_ZHIBO_URL = ("https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size=40"
              "&zhibo_id=152&tag_id=0&dire=f&dpc=1")
_NEWS_KW = ["生猪", "猪", "LH", "螺纹", "钢", "RB", "铁矿石", "铁矿", "I2", "豆粕", "粕", "M2",
            "期货", "商品", "收储", "储备肉", "库存", "限产", "环保", "政策", "央行", "降准",
            "降息", "LPR", "原油", "OPEC", "USDA", "关税", "港口", "进口", "出口", "天气",
            "能繁母猪", "屠宰", "开工率", "贴水", "升水"]
# 排除误命中（"M2" 货币供应/股票"钢"等过泛噪音控制：只保留明确商品语境命中足够）
_MINUTE_SECS_BEFORE_LOCK = 16   # 开盘区间锁定时需要的 1 分钟根数（09:00-09:15）

# 常见品种合约乘数（吨/手；贵金属按克/千克计）；set_watch add 新 kind 时校准
_KNOWN_MULT = {
    "LH": 16, "RB": 10, "I": 100, "M": 10, "CU": 5, "AL": 5, "ZN": 5, "NI": 1,
    "SN": 1, "AU": 1000, "AG": 15, "SC": 1000, "FU": 10, "P": 10, "V": 5,
    "PP": 5, "L": 5, "TA": 5, "MA": 10, "EG": 10, "CF": 5, "SR": 10, "OI": 10,
    "AP": 10, "CJ": 5, "SP": 10, "JD": 10, "C": 10, "CS": 10, "A": 10,
    "Y": 10, "P": 10, "RM": 10, "FG": 20, "SA": 20, "UR": 20, "SS": 5,
    "J": 100, "JM": 60, "SF": 5, "SM": 5, "ZC": 100, "HC": 10, "BU": 10,
    "RU": 10, "NR": 10, "IF": 300, "IH": 300, "IC": 200, "IM": 200, "AO": 20,
    "BR": 5, "EC": 50, "LC": 1, "SI": 5, "PX": 5, "SH": 5, "AO": 20,
}


def _now():
    return datetime.now()


def _is_weekday(dt=None):
    dt = dt or _now()
    return dt.weekday() < 5


def trading_window(dt=None):
    """日盘主力窗口（顾问活跃期）。周末/午休/夜间不积极。"""
    dt = dt or _now()
    if not _is_weekday(dt):
        return False
    hm = dt.hour * 100 + dt.minute
    return (900 <= hm <= 1015) or (1030 <= hm <= 1130) or (1330 <= hm <= 1500)


def _month_list(base_ym, rule, horizon=8):
    """未来 horizon 个月（含下月）候选 YYMM 列表；rule=odd 只留 1/3/5/7/9/11 月。"""
    out = []
    y, m = base_ym // 100, base_ym % 100
    while len(out) < horizon:
        m += 1
        if m == 13:
            m, y = 1, y + 1
        if rule == "odd" and m % 2 == 0:
            continue
        out.append(y * 100 + m)
    return out


def _http_bytes(url, timeout=8):
    req = urllib.request.Request(url, headers=_HDR)
    return urllib.request.urlopen(req, timeout=timeout).read()


# ---------------------------------------------------------------------------
# 解析层
# ---------------------------------------------------------------------------
def _parse_hq(text):
    """解析 hq.sinajs.cn 多合约返回 → {CODE: dict}
    商品合约结构（44 列，ADR-0018 实测）：name=0 open=2 high=3 low=4
    price=8 prev_close=10 hold=13 vol=14 date=17。防御：字段够/数值合法才收。"""
    out = {}
    for line in text.strip().splitlines():
        if '="' not in line:
            continue
        var, val = line.split('="', 1)
        code = var.rsplit("_", 1)[-1].strip()
        val = val.rstrip('";')
        if not val:
            continue
        f = val.split(",")
        if len(f) < 18:
            continue
        try:
            price, prev = float(f[8]), float(f[10])
            if price <= 0 or prev <= 0:
                continue
            out[code] = {
                "name": f[0], "open": float(f[2]), "high": float(f[3]),
                "low": float(f[4]), "price": price, "prev": prev,
                "hold": int(float(f[13])), "vol": int(float(f[14])),
                "date": f[17],
            }
        except (ValueError, IndexError):
            continue
    return out


def _parse_minline(raw, code):
    """getMinLine JSONP → [(time, price, avg)]（升序）；首行含日期也同结构。"""
    txt = raw.decode("utf-8", "ignore")
    i = txt.find("([")
    if i < 0:
        return []
    j = txt.rfind("])")
    if j <= i:
        return []
    try:
        arr = json.loads(txt[i + 1:j + 1])
    except ValueError:
        return []
    rows = []
    for r in arr:
        try:
            if len(r) < 3:
                continue
            rows.append((str(r[0]), float(r[1]), float(r[2])))
        except (ValueError, TypeError, IndexError):
            continue
    return rows


def _fetch_minline(code):
    url = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/"
           f"var%20_=/InnerFuturesNewService.getMinLine?symbol={code}")
    return _parse_minline(_http_bytes(url), code)


# ---------------------------------------------------------------------------
# 顾问引擎
# ---------------------------------------------------------------------------
class FutAdvisor:
    def __init__(self, fs=None):
        self.fs = fs                      # futures_strategy 实例（主连日K → ATR）
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self.pool = self._load_json(WATCH_FILE, config.FUT_ADV_DEFAULT_POOL,
                                    must_list=True) or list(config.FUT_ADV_DEFAULT_POOL)
        self.profile = self._load_json(PROFILE_FILE, config.FUT_ADV_PROFILE_DEFAULT,
                                       must_list=False) or dict(config.FUT_ADV_PROFILE_DEFAULT)
        self.mains = {}                   # kind -> 主力合约代码
        self.quotes = {}                  # code -> quote dict（最新行情）
        self.mint = {}                    # code -> [(time, price, avg)] 当日分时
        self._am_range = {}               # code -> (hi, lo) 早盘区间
        self.news = deque(maxlen=config.FUT_ADV_NEWS_MAX)
        self._news_ids = set()
        self._last_cycle = {"quote": 0, "mint": 0, "probe": 0, "news": 0}
        self.errors = []
        self.trading = False
        self.started_at = None
        self._thread = None
        self._main_lock_held = None       # main 探测进行中标志
        self._ensure_files()

    # ---------- 配置读写 ----------
    def _load_json(self, path, default, must_list=False):
        try:
            if path.exists():
                d = json.loads(path.read_text(encoding="utf-8"))
                if (must_list and isinstance(d, list)) or (not must_list and isinstance(d, dict)):
                    return d
        except Exception as e:            # 兜底返回默认，但打印（不留静默）
            print(f"[fut_advisor] 读 {path.name} 失败: {e}")
        return default

    def _ensure_files(self):
        try:
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            if not WATCH_FILE.exists():
                WATCH_FILE.write_text(json.dumps(self.pool, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
            if not PROFILE_FILE.exists():
                PROFILE_FILE.write_text(json.dumps(self.profile, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
        except Exception as e:
            print(f"[fut_advisor] 配置文件落盘失败: {e}")

    def _save_profile(self):
        try:
            PROFILE_FILE.write_text(json.dumps(self.profile, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
        except Exception as e:
            print(f"[fut_advisor] profile 写盘失败: {e}")

    def _save_watch(self):
        try:
            WATCH_FILE.write_text(json.dumps(self.pool, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
        except Exception as e:
            print(f"[fut_advisor] watch 写盘失败: {e}")

    def _err(self, tag, e):
        line = f"{datetime.now().strftime('%H:%M:%S')} {tag}: {type(e).__name__} {e}"
        print("[fut_advisor]", line)
        with self._lock:
            self.errors.append(line)
            self.errors = self.errors[-5:]

    # ---------- 池 & 资金参数（API 调用） ----------
    def set_profile(self, capital=None, daily_goal=None, per_trade_risk=None):
        """运行时改资金参数；非法值抛 ValueError。"""
        with self._lock:
            if capital is not None:
                capital = float(capital)
                if not (10000 <= capital <= 10000000):
                    raise ValueError("本金须在 1 万 ~ 1000 万之间")
                self.profile["capital"] = capital
            if daily_goal is not None:
                daily_goal = float(daily_goal)
                if not (100 <= daily_goal <= 100000):
                    raise ValueError("日目标须在 100 ~ 100000 元之间")
                self.profile["daily_goal"] = daily_goal
            if per_trade_risk is not None:
                per_trade_risk = float(per_trade_risk)
                if not (50 <= per_trade_risk <= 50000):
                    raise ValueError("单笔风险须在 50 ~ 50000 元之间")
                self.profile["per_trade_risk"] = per_trade_risk
            self.profile["date"] = _now().strftime("%Y-%m-%d")
            self._save_profile()
        return dict(self.profile)

    def record_pnl(self, amount):
        """手动记录一笔已实现盈亏（当日累计，跨日自动清零）。"""
        with self._lock:
            today = _now().strftime("%Y-%m-%d")
            if self.profile.get("pnl_date") != today:
                self.profile["pnl_date"] = today
                self.profile["realized"] = 0.0
            self.profile["realized"] = round(float(self.profile.get("realized", 0)) + float(amount), 2)
            self._save_profile()
        return {"realized": self.profile["realized"],
                "goal": self.profile["daily_goal"], "date": today}

    def set_watch(self, action, kind):
        """add/remove 盯盘品种（kind 如 LH/RB）。"""
        with self._lock:
            if action == "add":
                if not kind or len(kind) > 6 or not kind.isalnum():
                    raise ValueError("kind 非法")
                if any(p["kind"] == kind for p in self.pool):
                    return len(self.pool)
                # 新 kind：默认乘数按已知表校准；无表则 mult=10 + 名称提醒（UI 展示）
                m = _KNOWN_MULT.get(kind)
                item = {"kind": kind,
                        "name": kind + ("(乘数待核)" if not m else ""),
                        "months": "all", "mult": m or 10, "unit": "元/吨"}
                self.pool.append(item)
                self._save_watch()
                # 立即探测并写回主力（否则要等主循环 600s 后才可见）
                main = self._probe_main_for(item)
                if main:
                    self.mains[item["kind"]] = main
                    self.mint.pop(main, None)
                    self._am_range.pop(main, None)
                return len(self.pool)
            elif action == "remove":
                self.pool = [p for p in self.pool if p["kind"] != kind]
                self.mains.pop(kind, None)
                self._save_watch()
                return len(self.pool)
            raise ValueError("action 须为 add/remove")

    # ---------- 主力探测（换月跟踪） ----------
    def _candidate_codes(self):
        base_ym = _now().year * 100 + _now().month
        codes = []
        for p in self.pool:
            for ym in _month_list(base_ym, p.get("months", "all"), horizon=8):
                codes.append(p["kind"] + str(ym)[2:])
        return codes

    def _probe_main_for(self, p):
        """对单个品种探测主力：候选批量请求，取持仓量最大。返回合约码或 None。"""
        base_ym = _now().year * 100 + _now().month
        cands = [p["kind"] + str(ym)[2:] for ym in _month_list(base_ym, p.get("months", "all"), 8)]
        try:
            url = "https://hq.sinajs.cn/list=" + ",".join("nf_" + c for c in cands)
            text = _http_bytes(url).decode("gb18030", "ignore")
            got = _parse_hq(text)
        except Exception as e:
            self._err(f"主力探测 {p['kind']}", e)
            return None
        best = None
        for c, q in got.items():
            if q["hold"] <= 0:
                continue
            if best is None or q["hold"] > best[1]["hold"]:
                best = (c, q)
        if best:
            # 用新浪返回名刷新中文品种名（"铜2610"→"铜"），落盘持久化
            cn = re.sub(r"\d+$", "", (best[1].get("name") or "").strip())
            if cn and cn != p.get("name"):
                p["name"] = cn
                self._save_watch()
            return best[0]
        return None

    def _probe_mains(self):
        """刷新全部品种主力（600s 一次 + 启动）。"""
        with self._lock:
            for p in self.pool:
                main = self._probe_main_for(p)
                if main:
                    old = self.mains.get(p["kind"])
                    if old != main:
                        print(f"[fut_advisor] {p['name']} 主力 → {main}")
                        self.mains[p["kind"]] = main
                        self.mint.pop(main, None)   # 换月清旧分时
                        self._am_range.pop(main, None)

    # ---------- 行情刷新 ----------
    def _refresh_quotes(self):
        if not self.mains:
            return
        codes = list(self.mains.values())
        try:
            url = "https://hq.sinajs.cn/list=" + ",".join("nf_" + c for c in codes)
            text = _http_bytes(url).decode("gb18030", "ignore")
            got = _parse_hq(text)
            with self._lock:
                self.quotes.update(got)
        except Exception as e:
            self._err("实时行情", e)

    def _refresh_mintime(self):
        for main in list(self.mains.values()):
            try:
                rows = _fetch_minline(main)
                if not rows:
                    continue
                with self._lock:
                    self.mint[main] = rows
                    # 早盘区间：前 15 根高低；根数未足 16 前动态更新
                    head = rows[:15]
                    if rows and len(rows) < _MINUTE_SECS_BEFORE_LOCK:
                        hi = max(r[1] for r in head)
                        lo = min(r[1] for r in head)
                        self._am_range[main] = (hi, lo)
                    elif len(rows) >= _MINUTE_SECS_BEFORE_LOCK and main not in self._am_range:
                        hi = max(r[1] for r in rows[:15])
                        lo = min(r[1] for r in rows[:15])
                        self._am_range[main] = (hi, lo)
            except Exception as e:
                self._err(f"分时 {main}", e)

    def _refresh_news(self):
        try:
            d = json.loads(_http_bytes(_ZHIBO_URL).decode("utf-8", "ignore"))
            items = (d.get("result", {}).get("data", {}).get("feed", {}) or {}).get("list", [])
            fresh = []
            for it in items:
                text = (it.get("rich_text") or "").strip()
                if not text or len(text) < 12:
                    continue
                if not any(kw.lower() in text.lower() for kw in _NEWS_KW):
                    continue
                nid = it.get("id")
                if nid in self._news_ids:
                    continue
                tags = [t.get("name", "") for t in (it.get("tag") or []) if t.get("name")]
                fresh.append({
                    "id": nid, "time": (it.get("create_time") or "")[11:16],
                    "text": text[:130], "tags": tags,
                    "url": it.get("url") or "",
                })
                self._news_ids.add(nid)
                if len(self._news_ids) > 200:
                    self._news_ids = set(list(self._news_ids)[-150:])
            fresh.reverse()
            for f in fresh:
                self.news.appendleft(f)
        except Exception as e:
            self._err("快讯", e)

    # ---------- ATR ----------
    def _atr_of(self, kind):
        """主连日K ATR14（fs 缓存）；无缓存 → 用当日振幅近似。返回 (atr, 数据日期或 None)。"""
        if self.fs is not None:
            try:
                arr = self.fs.kline.get(kind + "0") or []
                if len(arr) >= 15:
                    trs = []
                    for i in range(len(arr) - 14, len(arr)):
                        b = arr[i]
                        pc = arr[i - 1]["c"]
                        trs.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
                    return sum(trs) / len(trs), arr[-1].get("d")
            except Exception as e:
                self._err("ATR", e)
        return None, None

    # ---------- 单品种信号 ----------
    def _signal(self, p):
        """给一句人话建议。p = pool 元素。返回 dict 或 None（无行情）。"""
        kind = p["kind"]
        main = self.mains.get(kind)
        if not main:
            return None
        q = self.quotes.get(main)
        if not q:
            return None
        price, prev = q["price"], q["prev"]
        chg, pct = price - prev, (price / prev - 1) * 100 if prev else 0
        mult = float(p.get("mult", 10))
        rows = self.mint.get(main) or []
        profile = self.profile
        per_risk = float(profile["per_trade_risk"])

        # --- ATR / 技术止损距离 ---
        atr, kd = self._atr_of(kind)
        if not atr or atr <= 0:
            day_rng = max(q["high"] - q["low"], abs(q["high"] - prev), abs(q["low"] - prev))
            atr = day_rng if day_rng > 0 else prev * 0.008
        stop_dist = atr * 0.6          # 技术止损距离（元/吨）
        tgt_dist = max(atr * 0.9, stop_dist * 1.2)

        # --- 均价 / 5分钟动量 / 早盘区间 ---
        avg = rows[-1][2] if rows else (q["open"] or prev)
        vs_avg = price - avg
        slope = 0.0
        if len(rows) >= 7:
            slope = (rows[-1][1] - rows[-7][1])
        am = self._am_range.get(main)
        am_state = 0
        if am:
            if price > am[1]:
                am_state = 1
            elif price < am[0]:
                am_state = -1

        # --- 方向打分（多因子共振，拒绝单条一票否决）---
        s = 0
        s += 1 if vs_avg > 0 else (-1 if vs_avg < 0 else 0)
        s += 1 if slope > 0 else (-1 if slope < 0 else 0)
        s += 1 if price > q["open"] else (-1 if price < q["open"] else 0)
        s += 1 if chg > 0 else (-1 if chg < 0 else 0)
        s += am_state
        opened = len(rows) >= _MINUTE_SECS_BEFORE_LOCK   # 早盘区间已锁定

        if s >= 3:
            bias, conf = "多", "强"
        elif s == 2:
            bias, conf = "多", "中"
        elif s <= -3:
            bias, conf = "空", "强"
        elif s == -2:
            bias, conf = "空", "中"
        else:
            bias, conf = "观望", "低"

        # --- 手数 / 风险可行性 ---
        risk_1lot = stop_dist * mult
        # 预算倍数分级：≤1.0 绿 / ≤1.5 黄 / >1.5 红（不硬凑）
        ratio = risk_1lot / per_risk if per_risk > 0 else 99
        if ratio <= 1.0:
            risk_lv = "ok"
        elif ratio <= 1.5:
            risk_lv = "warn"
        else:
            risk_lv = "no"
        max_by_risk = max(1, int(per_risk // max(risk_1lot, 1e-9))) if risk_lv != "no" else 1
        margin_lots = int((float(profile["capital"]) * 0.6) /
                          max(price * mult * float(profile["margin_ratio"]), 1e-9))
        lots = max(1, min(max_by_risk, margin_lots)) if risk_lv != "no" else 0

        # --- 进场/止损/目标价（按方向） ---
        entry = stop = target = None
        if bias == "多" and risk_lv != "no":
            if vs_avg > stop_dist * 1.2:
                entry = round(avg, 1); state = "等回踩"
            elif vs_avg > -stop_dist * 0.3:
                entry = price; state = "可试多"
            else:
                entry = round(avg, 1); state = "等收复"
            stop = round(entry - stop_dist, 1)
            target = round(entry + tgt_dist, 1)
        elif bias == "空" and risk_lv != "no":
            if vs_avg < -stop_dist * 1.2:
                entry = round(avg, 1); state = "等反抽"
            elif vs_avg < stop_dist * 0.3:
                entry = price; state = "可试空"
            else:
                entry = round(avg, 1); state = "等跌破"
            stop = round(entry + stop_dist, 1)
            target = round(entry - tgt_dist, 1)
        else:
            state = "观望" if risk_lv == "no" else "不动"

        # --- 消息组装（一句话人话） ---
        d_arrow = "多" if bias == "多" else ("空" if bias == "空" else "—")
        # 中文显示名：生猪2611（名称来自新浪探测刷新；纯英代码时兜底合约码）
        _nm = (p.get("name") or "").replace("(乘数待核)", "").strip()
        disp = f"{_nm}{main[-4:]}" if _nm and not re.match(r"^[A-Z0-9]+$", _nm) else main
        risk_txt = {"ok": f"1手止损≈¥{risk_1lot:,.0f}(预算内)",
                    "warn": f"1手止损≈¥{risk_1lot:,.0f}(略超预算)",
                    "no": f"❌1手止损≈¥{risk_1lot:,.0f} ≫ 预算¥{per_risk:,.0f}"}[risk_lv]
        if bias == "观望" or risk_lv == "no" or entry is None:
            if risk_lv == "no":
                msg = (f"{disp} 现价{price:.0f} {d_arrow}信号但{risk_txt}——"
                       f"该品种最小止损即超单笔风险，建议调高单笔风险预算或跳过")
            elif not opened:
                msg = f"{disp} 开盘初期（{len(rows)}根），等 09:15 定区间再给信号"
            else:
                msg = f"{disp} 现价{price:.0f} 多空拉锯，不操作（宁可错过不乱做）"
            est_gain = 0
        else:
            rng_txt = f"均价{avg:.0f}({'上' if vs_avg >= 0 else '下'}{abs(vs_avg):.0f})"
            est_gain = (target - entry) * mult * lots if bias == "多" \
                else (entry - target) * mult * lots
            if state in ("等回踩", "等反抽", "等收复", "等跌破"):
                msg = (f"{disp} 现价{price:.0f} {rng_txt} {conf}偏{d_arrow}——"
                       f"{state} {entry:.0f} 附近再动手；止损{stop:.0f} 目标{target:.0f}")
            else:
                msg = (f"{disp} 现价{price:.0f} {rng_txt} {conf}偏{d_arrow}——"
                       f"{entry:.0f} 可试{d_arrow} {lots}手；止损{stop:.0f}(≈-¥{min(risk_1lot*lots, 9e9):,.0f})"
                       f" 目标{target:.0f}(吃满≈¥{est_gain:,.0f})")
        return {
            "kind": kind, "name": p.get("name", kind), "main": main,
            "price": round(price, 1), "chg": round(chg, 1), "pct": round(pct, 2),
            "avg": round(avg, 1), "vs_avg": round(vs_avg, 1),
            "bias": d_arrow, "conf": conf, "state": state,
            "entry": entry, "stop": stop, "target": target, "lots": lots,
            "risk_lv": risk_lv, "risk_1lot": round(risk_1lot, 0),
            "est_gain": round(est_gain, 0), "stop_dist": round(stop_dist, 1),
            "mult": mult, "msg": msg, "atr": round(atr, 1), "atr_date": kd,
            "updated": q.get("date", ""),
        }

    # ---------- 快照（API） ----------
    def snapshot(self):
        with self._lock:
            self.trading = trading_window()
            watch = []
            for p in self.pool:
                sig = self._signal(p)
                if sig:
                    watch.append(sig)
            today = _now().strftime("%Y-%m-%d")
            realized = 0.0
            if self.profile.get("pnl_date") == today:
                realized = float(self.profile.get("realized", 0))
            goal = float(self.profile["daily_goal"])
            pool = []
            for p in self.pool:
                main = self.mains.get(p["kind"])
                pool.append({"kind": p["kind"], "name": p.get("name", p["kind"]),
                             "main": main or "", "ready": bool(main and self.quotes.get(main))})
            return {
                "watch": watch,
                "pool": pool,
                "news": list(self.news),
                "goal": {"capital": float(self.profile["capital"]),
                         "daily_goal": goal, "per_trade_risk": float(self.profile["per_trade_risk"]),
                         "realized": realized, "remain": max(0.0, goal - realized),
                         "done": realized >= goal and goal > 0},
                "mains": dict(self.mains),
                "trading": self.trading,
                "asof": _now().strftime("%H:%M:%S"),
                "errors": list(self.errors),
            }

    # ---------- 主循环 ----------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self.started_at = _now()
        self._thread = threading.Thread(target=self._loop, name="fut-advisor", daemon=True)
        self._thread.start()
        print("[fut_advisor] 已启动")

    def stop(self):
        self._stop.set()

    def _loop(self):
        self._probe_mains()
        self._refresh_news()
        while not self._stop.is_set():
            try:
                t = time.time()
                self.trading = trading_window()
                if self.mains:
                    self._refresh_quotes()
                now = time.time()
                if self.trading:
                    if now - self._last_cycle["mint"] >= config.FUT_ADV_MINTIME_SEC:
                        self._refresh_mintime()
                        self._last_cycle["mint"] = now
                    if now - self._last_cycle["probe"] >= config.FUT_ADV_MAIN_PROBE_SEC:
                        self._probe_mains()
                        self._last_cycle["probe"] = now
                if now - self._last_cycle["news"] >= config.FUT_ADV_NEWS_SEC:
                    self._refresh_news()
                    self._last_cycle["news"] = now
                # 盘中 6s 快轮询；非盘中 60s 低频保活快照
                time.sleep(config.FUT_ADV_INTERVAL if self.trading else 60)
            except Exception as e:
                self._err("主循环", e)
                time.sleep(10)


_inst = None


def instance(fs=None):
    global _inst
    if _inst is None:
        _inst = FutAdvisor(fs=fs)
    return _inst

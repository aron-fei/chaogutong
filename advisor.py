# -*- coding: utf-8 -*-
"""时段操作建议引擎。

三个窗口（对应 market-advisor 五时段中的三个关键决策点）：
  - 早盘·强者恒强  09:35-10:10：昨日涨停池中今日继续走强的标的，回踩低吸 / 突破新高追
  - 盘中·卖点检测  10:30-11:00：持仓 + 当日已建议标的的炸板 / 冲高回落 / 破止损检测
  - 尾盘·补涨龙    14:20-14:50：当日主线板块中尚未大涨的低位股，尾盘低吸博次日惯性

数据源：
  - 昨日涨停池  ak.stock_zt_pool_em（东财 push2ex）
  - 个股快照    腾讯批量接口 qt.gtimg.cn（换手38/流通市值44/涨停价47/量比49）
  - 板块热度    新浪行业映射 newSinaHy.php + 成分股 stock_sector_detail
  - 情绪系数    复用 MarketData.dragon_radar() 的 mood

只参谋不替人下单：所有输出为建议，仓位提示按情绪系数分档。
卖出/止损阈值由策略内置（专家口径），不给用户暴露配置。
"""
import json
import log_util
import re
import threading
import time as _time
import traceback
from datetime import datetime, date, timedelta, time as dtime

import requests

import akshare as ak

import portfolio
import review

TIMEOUT = 8
CACHE_TTL = 60  # API 层缓存，避免高频重算

# 窗口顺序即时间顺序
WINDOW_ORDER = ["strong", "exit", "lagging"]
WINDOWS = {
    "strong": {
        "name": "早盘·强者恒强",
        "start": dtime(9, 35), "end": dtime(10, 10),
        "hint": "昨日涨停今日继续走强：回踩不破均价线低吸，或放量突破今日新高追入",
    },
    "exit": {
        "name": "盘中·卖点检测",
        "start": dtime(10, 30), "end": dtime(11, 0),
        "hint": "持仓与早盘买入标的：炸板先走、冲高回落减半、破止损纪律离场",
    },
    "lagging": {
        "name": "尾盘·补涨龙",
        "start": dtime(14, 20), "end": dtime(14, 50),
        "hint": "主线板块未大涨的低位股：尾盘现价低吸，博次日板块惯性冲高",
    },
}


def _qt_symbol(code: str) -> str:
    # 2026-09-29：剥离 .SZ/.SH 等前后缀（同 bottom_surge 修复口径）
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if not code:
        return ""
    if code.startswith(("6", "9", "5")):
        return "sh" + code
    if code.startswith(("0", "3", "2")):
        return "sz" + code
    return ""  # 北交所等腾讯标准接口不覆盖，跳过


def quote_stocks(codes):
    """腾讯批量个股行情（已迁移到 data_fetcher.quote_stocks，本处保留为别名）。"""
    from data_fetcher import quote_stocks as _qs
    return _qs(codes)


class Advisor:
    def __init__(self, market_data):
        self.md = market_data
        self._lock = threading.Lock()
        self._cache = None
        self._cache_at = 0.0
        self._yday = None            # {"date": "20260819", "rows": [...]}
        self._sina_map = None        # [{"code","name","count","pct"}]
        self._sina_map_date = None
        self._last_exit = []         # 盘中最后一批卖点信号（收盘归档用）
        # 窗口期落盘节流：同窗口内 60s 写一次，避免高频轮询反复写盘
        self._logged_at = {"strong": 0.0, "lagging": 0.0}
        # ADR-0020 方案B：后台自动线程（盘中定时 advise，不依赖用户打开工作台触发）
        self._auto_stop = None
        self._auto_th = None

    # ------------------------------------------------------------------ 时段
    def session(self):
        now = datetime.now()
        t, wd = now.time(), now.weekday()
        # 今日已结束的窗口进度（保留早上提示的关键）
        today_progress = [
            {
                "key": k,
                "name": WINDOWS[k]["name"],
                "start": WINDOWS[k]["start"].strftime("%H:%M"),
                "end": WINDOWS[k]["end"].strftime("%H:%M"),
                "passed": t > WINDOWS[k]["end"],
                "active": WINDOWS[k]["start"] <= t <= WINDOWS[k]["end"],
            }
            for k in WINDOW_ORDER
        ]
        if wd >= 5:
            return {"phase": "weekend", "name": "周末休市", "in_window": False,
                    "next": "下个交易日 09:35 强者恒强", "today_progress": today_progress}
        for key in WINDOW_ORDER:
            w = WINDOWS[key]
            if w["start"] <= t <= w["end"]:
                return {"phase": key, "name": w["name"], "in_window": True,
                        "hint": w["hint"],
                        "end": w["end"].strftime("%H:%M"),
                        "key": key,
                        "today_progress": today_progress}
        if t < dtime(9, 15):
            nxt, phase, name = "今日 09:35 · 强者恒强", "pre", "盘前"
        elif t < WINDOWS["strong"]["start"]:
            nxt, phase, name = "今日 09:35 · 强者恒强", "pre", "集合竞价"
        else:
            # 找下一个开始时间在当前之后的窗口
            nxt_w = [WINDOWS[k] for k in WINDOW_ORDER
                     if WINDOWS[k]["start"] > t]
            if nxt_w:
                w = nxt_w[0]
                nxt = f"今日 {w['start'].strftime('%H:%M')} · {w['name']}"
                phase, name = "idle", "盘中待机"
            else:
                nxt, phase, name = "明日 09:35 · 强者恒强", "after", "已收盘"
        return {"phase": phase, "name": name, "in_window": False, "next": nxt,
                "today_progress": today_progress}

    # ---------------------------------------------------------- 昨日涨停池
    def _yesterday_pool(self):
        today = date.today()
        for i in range(1, 7):
            d = today - timedelta(days=i)
            if d.weekday() >= 5:
                continue
            if self._yday and self._yday["date"] == d.strftime("%Y%m%d"):
                return self._yday
            try:
                df = ak.stock_zt_pool_em(date=d.strftime("%Y%m%d"))
                if df is None or df.empty:
                    continue
                rows = [{
                    "code": str(r["代码"]),
                    "name": str(r["名称"]),
                    "lianban": int(r["连板数"]),
                    "industry": str(r["所属行业"]),
                    "breaks": int(r["炸板次数"]),
                    "turnover": round(float(r["换手率"]), 2),
                    "float_mv": round(float(r["流通市值"]) / 1e8, 1),
                } for _, r in df.iterrows()]
                self._yday = {"date": d.strftime("%Y%m%d"), "rows": rows}
                return self._yday
            except Exception as e:
                print(f"[advisor] 昨日涨停池 {d} 获取失败: {e}")
        return self._yday or {"date": "", "rows": []}

    # ------------------------------------------------------------ 新浪行业
    def _sina_industry_map(self):
        today = date.today()
        if self._sina_map and self._sina_map_date == today:
            return self._sina_map
        try:
            r = requests.get("http://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php",
                             timeout=TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
            t = r.text
            i, j = t.find("{"), t.rfind("}")
            d = json.loads(t[i:j + 1])
            rows = []
            for code, v in d.items():
                f = v.split(",")
                if len(f) < 6:
                    continue
                rows.append({"code": code, "name": f[1],
                             "count": int(f[2]), "pct": float(f[5])})
            self._sina_map = rows
            self._sina_map_date = today
            return rows
        except Exception as e:
            print(f"[advisor] 新浪行业映射失败: {e}")
            return self._sina_map or []

    # -------------------------------------------------------- 策略一：强者恒强
    @staticmethod
    def _market_closed():
        """收盘后（15:00 之后 / 周末）返回 True——时段建议归档清空，不再展示当日播报"""
        from data_fetcher import market_status
        status, _ = market_status()
        return status in ("closed", "closed_all_day")

    def _strategy_strong(self, mood):
        t = datetime.now().time()
        in_window = (WINDOWS["strong"]["start"] <= t <= WINDOWS["strong"]["end"])
        if not in_window:
            if self._market_closed():
                # 收盘后：清空时段建议，明日重新获取（复盘已归档到「当日复盘」tab）
                return [], "已收盘 · 时段建议已归档，明日 09:35 重新获取"
            # 盘中非窗口：从 review 读取早上已落盘的建议，标记为历史播报
            recs = review.today_records().get("strong", [])
            if recs:
                note = f"📌 早盘窗口已结束 · 历史播报 {len(recs)} 条（{WINDOWS['strong']['start'].strftime('%H:%M')}-{WINDOWS['strong']['end'].strftime('%H:%M')} 快照）"
                # 把 review 记录转成 UI 期望的格式
                rows = []
                for it in recs:
                    rows.append({
                        "code": it.get("code"),
                        "name": it.get("name") or it.get("code"),
                        "price": it.get("entry_price"),
                        "pct": it.get("pct"),
                        "lianban": it.get("lianban", 1),
                        "score": it.get("score"),
                        "action": it.get("action", ""),
                        "stop": it.get("stop", ""),
                        "position": it.get("position", ""),
                        "why": it.get("why", ""),
                        "recorded_at": it.get("recorded_at", ""),
                    })
                return rows, note
            # 早盘窗口未到：说"等待中"；窗口已过但盘中且无落盘：说"今日未播报"（避免误以为窗口中）
            if t < WINDOWS["strong"]["start"]:
                return [], f"⏰ 早盘窗口 {WINDOWS['strong']['start'].strftime('%H:%M')}-{WINDOWS['strong']['end'].strftime('%H:%M')} 等待中（启动后实时统计）"
            return [], f"📭 今日早盘窗口 {WINDOWS['strong']['start'].strftime('%H:%M')}-{WINDOWS['strong']['end'].strftime('%H:%M')} 未播报（窗口内需打开工作台触发 /api/advisor）。下一窗口 10:30 · 卖点检测"
        pool = self._yesterday_pool()
        if not pool["rows"]:
            return [], "昨日涨停池不可用"
        # 今日涨停池行业热力（判断昨热点今延续）
        ind_zt = {}
        for s in self.md.zt_pool:
            ind_zt[s["industry"]] = ind_zt.get(s["industry"], 0) + 1
        quotes = quote_stocks([r["code"] for r in pool["rows"]])
        cands = []
        for s in pool["rows"]:
            q = quotes.get(s["code"])
            if not q or q["prev_close"] <= 0:
                continue
            gap = round((q["open"] / q["prev_close"] - 1) * 100, 2)
            hot = ind_zt.get(s["industry"], 0)
            # 一字涨停买不进；已涨停(>9.7%且接近涨停价)只能排队
            sealed = q["price"] >= q["zt_price"] * 0.998 and q["zt_price"] > 0
            # 评分：昨日高度 + 今日强度 + 高开姿态 + 量能 + 板块延续
            score = (s["lianban"] * 12
                     + max(q["pct"], -5) * 2.2
                     + max(gap, 0) * 1.6
                     + min(q["vol_ratio"], 6) * 2
                     + (8 if hot >= 3 else 0))
            score = round(score * mood, 1)
            if q["pct"] >= 2 and gap >= 0 and not sealed:
                tier, why = "core", "昨日涨停，今日高开继续走强"
            elif q["pct"] >= 2 and sealed:
                tier, why = "queue", "已涨停封板，只能排队/次日竞价"
            elif q["pct"] < 0:
                tier, why = "weak", "高开低走转弱，剔除"
            else:
                tier, why = "watch", "红盘但强度一般，观察"
            cands.append({
                "code": s["code"], "name": s["name"],
                "price": q["price"], "pct": q["pct"], "gap": gap,
                "lianban": s["lianban"], "vol_ratio": q["vol_ratio"],
                "high": q["high"], "zt_price": q["zt_price"],
                "score": score, "tier": tier, "why": why,
                "industry": s["industry"], "hot": hot,
            })
        cands.sort(key=lambda x: -x["score"])
        rows = []
        for c in cands:
            if len(rows) >= 6:
                break
            if c["tier"] == "weak":
                continue
            if c["tier"] == "queue":
                action, stop = f"排队打板（涨停价 {c['zt_price']:.2f}）", "炸板即撤"
                pos = "打板仓 ≤1成，次日不封直接走"
            else:
                buy = max(c["high"] * 1.005, c["price"] * 1.002)
                stop = round(min(c["price"] * 0.95, c["price"] - 0.03), 2)
                action = f"回踩 {c['price']:.2f} 不破可低吸；放量过 {buy:.2f} 可追"
                stop = f"跌破 {stop:.2f} 止损"
                pos = ("2-3成" if mood >= 1.0 else "1-2成" if mood >= 0.9 else "轻仓试错 ≤1成")
            rows.append({
                "code": c["code"], "name": c["name"], "price": c["price"],
                "pct": c["pct"], "gap": c["gap"], "lianban": c["lianban"],
                "vol_ratio": c["vol_ratio"], "score": c["score"],
                "industry": c["industry"], "action": action, "stop": stop,
                "position": pos, "why": c["why"], "tier": c["tier"],
                "stop_price": (round(c["zt_price"] * 0.99, 2)
                               if c["tier"] == "queue" and c["zt_price"] > 0
                               else round(min(c["price"] * 0.95,
                                              c["price"] - 0.03), 2)),
                "target_price": round(c["price"] * 1.07, 2),
            })
        note = f"样本={pool['date']}涨停池{len(pool['rows'])}只"
        # 窗口期落盘：让非窗口期也能读到「历史播报」快照（节流 60s）
        if rows and _time.time() - self._logged_at["strong"] > 60:
            try:
                review.record("strong", rows)
                self._logged_at["strong"] = _time.time()
            except Exception as e:
                print(f"[advisor] 落盘 strong 失败: {e}")
        return rows, note

    # -------------------------------------------------------- 策略二：补涨龙
    def _strategy_lagging(self, mood):
        t = datetime.now().time()
        in_window = (WINDOWS["lagging"]["start"] <= t <= WINDOWS["lagging"]["end"])
        if not in_window:
            if self._market_closed():
                # 收盘后：清空时段建议，明日重新获取
                return [], "已收盘 · 时段建议已归档，明日 14:20 重新获取"
            # 盘中非窗口：从 review 读取尾盘已落盘的建议，标记为历史播报
            recs = review.today_records().get("lagging", [])
            if recs:
                note = f"📌 尾盘窗口已结束 · 历史播报 {len(recs)} 条（{WINDOWS['lagging']['start'].strftime('%H:%M')}-{WINDOWS['lagging']['end'].strftime('%H:%M')} 快照）"
                rows = []
                for it in recs:
                    rows.append({
                        "code": it.get("code"),
                        "name": it.get("name") or it.get("code"),
                        "price": it.get("entry_price"),
                        "pct": it.get("pct"),
                        "sector": it.get("sector", ""),
                        "sector_pct": it.get("sector_pct"),
                        "score": it.get("score"),
                        "action": it.get("action", ""),
                        "stop": it.get("stop", ""),
                        "position": it.get("position", ""),
                        "why": it.get("why", ""),
                        "recorded_at": it.get("recorded_at", ""),
                    })
                return rows, note
            # 尾盘窗口未到：说"等待中"；窗口已过但盘中且无落盘：说"今日未播报"（避免误以为窗口中）
            if t < WINDOWS["lagging"]["start"]:
                return [], f"⏰ 尾盘窗口 {WINDOWS['lagging']['start'].strftime('%H:%M')}-{WINDOWS['lagging']['end'].strftime('%H:%M')} 等待中（启动后实时统计）"
            return [], f"📭 今日尾盘窗口 {WINDOWS['lagging']['start'].strftime('%H:%M')}-{WINDOWS['lagging']['end'].strftime('%H:%M')} 未播报（窗口内需打开工作台触发 /api/advisor）"
        smap = self._sina_industry_map()
        if not smap:
            return [], "新浪行业映射不可用"
        hot = sorted([s for s in smap if s["count"] >= 20], key=lambda x: -x["pct"])[:3]
        if not hot:
            return [], "今日无强势主线板块"
        # 主线一致性：与涨停池行业呼应加分
        ind_zt = {}
        for s in self.md.zt_pool:
            ind_zt[s["industry"]] = ind_zt.get(s["industry"], 0) + 1
        cands = []
        for ind in hot:
            try:
                df = ak.stock_sector_detail(sector=ind["code"])
            except Exception as e:
                print(f"[advisor] 成分获取失败 {ind['name']}: {e}")
                continue
            if df is None or df.empty:
                continue
            for _, r in df.iterrows():
                try:
                    name = str(r["name"])
                    if "ST" in name or "退" in name:
                        continue
                    pct = float(r["changepercent"])
                    mv = float(r["nmc"]) / 1e4          # 万 -> 亿
                    to = float(r["turnoverratio"])
                    amt = float(r["amount"])            # 元
                    price = float(r["trade"])
                except (ValueError, TypeError, KeyError):
                    continue
                # 补涨筛选：红盘 2~7%（没涨停、也没趴着）、市值 30~300亿、换手 2~15、成交额>1.5亿
                if not (2 <= pct <= 7 and 30 <= mv <= 300 and 2 <= to <= 15 and amt > 1.5e8):
                    continue
                # 评分：板块强度 + 涨幅适中度(4.5%最优) + 换手 + 市值适中(80亿最优)
                score = (ind["pct"] * 4
                         + (4.5 - abs(pct - 4.5)) * 3
                         + min(to, 10) * 1.2
                         + (10 - abs(mv - 80) / 22))
                score = round(score * mood, 1)
                cands.append({
                    "code": str(r["code"]), "name": name, "price": price,
                    "pct": round(pct, 2), "turnover": round(to, 2),
                    "float_mv": round(mv, 1), "sector": ind["name"],
                    "sector_pct": round(ind["pct"], 2), "score": score,
                })
        cands.sort(key=lambda x: -x["score"])
        rows = []
        for c in cands[:6]:
            stop = round(c["price"] * 0.955, 2)
            target = round(c["price"] * 1.06, 2)
            rows.append({
                "code": c["code"], "name": c["name"], "price": c["price"],
                "pct": c["pct"], "turnover": c["turnover"], "float_mv": c["float_mv"],
                "sector": c["sector"], "sector_pct": c["sector_pct"],
                "score": c["score"],
                "action": f"尾盘 {c['price']:.2f} 现价低吸",
                "stop": f"破 {stop:.2f} 止损（-4.5%）",
                "target": f"次日冲高 {target:.2f} 附近兑现",
                "position": ("2成" if mood >= 1.0 else "1成" if mood >= 0.9 else "≤1成"),
                "why": f"{c['sector']}({c['sector_pct']:+.1f}%)主线内低位，涨幅{c['pct']:+.1f}%尚未补涨",
                "stop_price": stop, "target_price": target,
            })
        note = "主线板块：" + "、".join(f"{h['name']}{h['pct']:+.1f}%" for h in hot)
        # 窗口期落盘：让非窗口期也能读到「历史播报」快照（节流 60s）
        if rows and _time.time() - self._logged_at["lagging"] > 60:
            try:
                review.record("lagging", rows)
                self._logged_at["lagging"] = _time.time()
            except Exception as e:
                print(f"[advisor] 落盘 lagging 失败: {e}")
        return rows, note

    # -------------------------------------------------------- 策略三：卖点检测
    def _strategy_exit(self, mood):
        """持仓 + 当日已建议标的的盘中卖点检测。
        ⚠️ A股 T+1 制度：今天新加仓的标的当天不可卖/减，必须排除
        ⚠️ 早盘建议标的如果用户没买入（不在持仓里），也不应被监控

        监控对象（按优先级去重）：
          1. 手动录入的持仓（排除今日新加仓——T+1 不可卖）
          2. 当日早盘·强者恒强建议（仅在已持仓时纳入；同 T+1 排除）
          3. 当日尾盘·补涨龙建议（同上）

        专家内置信号（优先级从高到低）：
          炸板      摸过涨停价但现未封板      → 卖出（落袋为安）
          冲高回落  距当日高点回撤 ≥3% 且未封板 → 减仓一半
          破止损    现价 ≤ 止损位             → 卖出（纪律离场）
          盈利回吐  持仓浮盈 ≥8% 且今日转弱    → 兑现一半
          涨停封板  现价封死涨停              → 持有（炸板再走）
          其余      走势正常                  → 持有观察
        """
        if self._market_closed():
            # 收盘后：卖点检测归档清空（复盘已归档到「当日复盘」tab）
            return [], "已收盘 · 卖点检测已归档，明日盘中继续监控"
        today_str = datetime.now().strftime("%Y-%m-%d")
        watch = {}  # code -> {"name","scope","stop_price","cost", "t1_locked"}
        t1_count = 0   # 今日新仓被排除数量
        for p in portfolio.list_positions():
            added = p.get("added_at", "")[:10]  # "2026-08-20 17:00" → "2026-08-20"
            if added == today_str:
                # T+1：今天新加的持仓当天不能卖/减
                t1_count += 1
                continue
            stop_px = None
            m = re.search(r"止损[^\d]*(\d+(?:\.\d+)?)", p.get("note") or "")
            if m:
                stop_px = float(m.group(1))
            elif p["cost"] > 0:
                stop_px = round(p["cost"] * 0.95, 2)
            watch[p["code"]] = {"name": p.get("name", p["code"]),
                                "scope": "我的持仓", "stop_price": stop_px,
                                "cost": p["cost"], "shares": p.get("shares")}
        recs = review.today_records()
        for strat, scope in (("strong", "早盘建议"), ("lagging", "尾盘建议")):
            for it in recs.get(strat, []):
                code = it.get("code")
                if not code or code in watch:
                    continue
                # 早盘/尾盘建议标的必须已实际持仓才纳入监控（用户没买就不监控）
                if not any(p["code"] == code for p in portfolio.list_positions()):
                    continue
                # 同样 T+1 排除
                pos = next((p for p in portfolio.list_positions() if p["code"] == code), None)
                if pos and pos.get("added_at", "")[:10] == today_str:
                    t1_count += 1
                    continue
                watch[code] = {"name": it.get("name", code), "scope": scope,
                               "stop_price": it.get("stop_price")
                               or (round(it["entry_price"] * 0.95, 2)
                                   if it.get("entry_price") else None),
                               "cost": it.get("entry_price"), "shares": None}
        if not watch:
            return [], "暂无监控对象：录入持仓或等早盘窗口产生建议后自动纳入"
        quotes = quote_stocks(list(watch.keys()))
        # 用行情返回的真实名字覆盖（持仓录入时可能没填名字被 fallback 成 code）
        for code, w in watch.items():
            q = quotes.get(code)
            if q and q.get("name"):
                w["name"] = q["name"]
        zt_codes = {s["code"] for s in self.md.zt_pool}
        zb_codes = {s["code"] for s in self.md.zb_pool}
        rows = []
        for code, w in watch.items():
            q = quotes.get(code)
            if not q or q["prev_close"] <= 0:
                continue
            price, high, prev = q["price"], q["high"], q["prev_close"]
            pct = q["pct"]
            high_pct = round((high / prev - 1) * 100, 2)
            drawdown = round((high - price) / prev * 100, 2)
            sealed = q["zt_price"] > 0 and price >= q["zt_price"] * 0.998
            touched_zt = q["zt_price"] > 0 and high >= q["zt_price"] * 0.998
            stop_px = w.get("stop_price")
            cost = w.get("cost") or 0
            pnl_pct = round((price / cost - 1) * 100, 2) if cost > 0 else None
            # ---- 信号判定（优先级） ----
            if (touched_zt and not sealed) or code in zb_codes:
                signal, advice = "sell", "涨停打开未回封（炸板），落袋为安，不恋战"
                if sealed:  # 炸板池数据滞后、现已回封
                    signal, advice = "hold", "炸板后已回封，继续持有，再炸再走"
            elif drawdown >= 3 and not sealed:
                signal, advice = "reduce", f"距高点回落 {drawdown:.1f}%，冲高乏力，减仓一半锁利润"
            elif stop_px and price <= stop_px:
                if pnl_pct is not None and pnl_pct <= -10:
                    signal = "sell"
                    advice = (f"深度破位（浮亏 {pnl_pct:.0f}%），止损已过点："
                              f"反弹减仓降风险，或认错换股，不摊平")
                else:
                    signal, advice = "sell", f"已破止损位 {stop_px:.2f}，纪律离场，不抱幻想"
            elif pnl_pct is not None and pnl_pct >= 8 and pct <= 0:
                signal, advice = "reduce", f"浮盈 {pnl_pct:+.1f}% 今日转弱，兑现一半利润"
            elif sealed or code in zt_codes:
                signal, advice = "hold", "涨停封板持有，盯炸板：开板即卖一半"
            else:
                signal, advice = "hold", "走势正常，持有观察，破位再执行纪律"
            rank = {"sell": 0, "reduce": 1, "hold": 2}[signal]
            rows.append({
                "code": code, "name": w["name"], "scope": w["scope"],
                "price": price, "pct": pct, "high_pct": high_pct,
                "drawdown": drawdown, "signal": signal, "advice": advice,
                "stop_price": stop_px, "pnl_pct": pnl_pct,
                "sealed": sealed, "rank": rank,
            })
        rows.sort(key=lambda x: (x["rank"], -(x["pct"] or 0)))
        # 只保留需要行动的票（卖出/减仓），"持有观察"不进面板，避免噪音
        action_rows = [r for r in rows if r["signal"] in ("sell", "reduce")]
        watched = len(rows)
        acted = len(action_rows)
        skip_tip = f" · 排除 {t1_count} 只今日新仓（T+1 不可卖）" if t1_count else ""
        if action_rows:
            note = (f"预警 {acted} 只 / 监控 {watched} 只"
                    f"（持仓 {sum(1 for r in action_rows if r['scope']=='我的持仓')}"
                    f" + 当日建议 {sum(1 for r in action_rows if r['scope']!='我的持仓')}）"
                    f"{skip_tip}")
        else:
            note = f"监控 {watched} 只，暂无卖出/减仓信号{skip_tip}"
        return action_rows, note

    # ------------------------------------------------------------- 主入口
    def advise(self):
        with self._lock:
            if self._cache and _time.time() - self._cache_at < CACHE_TTL:
                return self._cache
            errors = []
            try:
                # 涨停池为空时 dragon_radar 返回 mood=None（开盘初期/数据源延迟），回退中性 1.0
                mood = self.md.dragon_radar().get("mood")
                if mood is None:
                    mood = 1.0
            except Exception as e:
                errors.append(f"情绪系数回退默认: {e}")
                mood = 1.0
            try:
                strong, s_note = self._strategy_strong(mood)
            except Exception as e:
                errors.append(f"强者恒强失败: {e}")
                traceback.print_exc()
                strong, s_note = [], "计算异常"
            try:
                lagging, l_note = self._strategy_lagging(mood)
            except Exception as e:
                errors.append(f"补涨龙失败: {e}")
                traceback.print_exc()
                lagging, l_note = [], "计算异常"
            try:
                exit_rows, e_note = self._strategy_exit(mood)
                if exit_rows:
                    # 缓存盘中最后一批信号（收盘后 exit 会清空，归档需要盘中数据）
                    self._last_exit = [dict(r) for r in exit_rows]
            except Exception as e:
                errors.append(f"卖点检测失败: {e}")
                traceback.print_exc()
                exit_rows, e_note = [], "计算异常"
            result = {
                "session": self.session(),
                "mood": round(mood, 3),
                "strong": {"rows": strong, "note": s_note},
                "lagging": {"rows": lagging, "note": l_note},
                "exit": {"rows": exit_rows, "note": e_note},
                "windows": {k: {"name": w["name"],
                                "start": w["start"].strftime("%H:%M"),
                                "end": w["end"].strftime("%H:%M")}
                            for k, w in WINDOWS.items()},
                "generated_at": datetime.now().strftime("%H:%M:%S"),
                "errors": errors,
            }
            # 建议落盘（只在对应窗口内记录首条，供次日复盘与盘中卖点检测）
            phase = result["session"].get("phase")
            try:
                if phase == "strong" and strong:
                    review.record("strong", strong)
                elif phase == "lagging" and lagging:
                    review.record("lagging", lagging)
            except Exception as e:
                errors.append(f"建议落盘失败: {e}")
            # 收盘归档：首次进入收盘态时，把当日最后一次卖出/减仓信号落盘，
            # 供「当日复盘」账户部分读取（时段建议 tab 本身已清空）
            try:
                self._archive_on_close(result, exit_rows)
            except Exception as e:
                errors.append(f"收盘归档失败: {e}")
            self._cache = result
            self._cache_at = _time.time()
            return result

    def _archive_on_close(self, result, exit_rows):
        """收盘（或周末）首次调用时，把当日信号归档到 data/reviews/YYYY-MM-DD.json"""
        from data_fetcher import market_status
        status, _ = market_status()
        if status not in ("closed", "closed_all_day"):
            return
        from pathlib import Path
        import json as _json
        today_str = datetime.now().strftime("%Y-%m-%d")
        d = Path(__file__).parent / "data" / "reviews"
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{today_str}.json"
        if f.exists():
            try:
                old = _json.load(open(f, encoding="utf-8"))
                if old.get("date") == today_str:
                    return  # 今天已归档过，避免覆盖

            except Exception as _e:
                log_util.quiet_warn("advisor.py:622", _e)

        _json.dump({
            "date": today_str,
            "archived_at": datetime.now().strftime("%H:%M:%S"),
            "mood": result.get("mood"),
            "strong": [dict(r) for r in result.get("strong", {}).get("rows", [])],
            "lagging": [dict(r) for r in result.get("lagging", {}).get("rows", [])],
            "exit": [dict(r) for r in self._last_exit],
        }, open(f, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    # ------------------------------------------------------- ADR-0020 自动线程
    def start(self, interval=20):
        """ADR-0020 方案B：自起后台守护线程，盘中每 interval 秒自动 advise()。
        - 目的：窗口（strong 09:35 / lagging 14:20 / exit 盘中）不依赖用户打开工作台
          也自动跑策略 + 落盘 + 收盘归档（9-4 早盘无人触发导致 0 数据问题根治）。
        - 零 launchd 依赖（符合用户 9-3「不引入 9:xx launchd 自动化」偏好）；
          随 Flask 进程存活（KeepAlive plist 已保活），进程退出线程即灭。
        - 频率：advise() 内部 CACHE_TTL=60s 限频 → 实际每 ~60s 一次全量重算
          （dragon_radar 纯内存 / quote 批量 1-3 个 HTTP / 落盘幂等首条去重）。
        """
        if self._auto_th and self._auto_th.is_alive():
            return
        self._auto_stop = threading.Event()
        self._auto_th = threading.Thread(target=self._auto_loop, daemon=True,
                                         name="advisor-auto",
                                         args=(interval,))
        self._auto_th.start()
        print(f"[advisor] 自动线程已启动 (interval={interval}s, "
              f"thread={self._auto_th.name})")

    def _auto_loop(self, interval):
        while not self._auto_stop.is_set():
            try:
                now = datetime.now()
                # 仅交易日 + A股开市时段自动跑（收盘后由 advise() 内部归档一次即可）
                if now.weekday() < 5 and dtime(9, 15) <= now.time() <= dtime(15, 5):
                    self.advise()
            except Exception:
                # 后台线程永不因单次异常退出
                try:
                    traceback.print_exc()

                except Exception as _e:
                    log_util.quiet_warn("advisor.py:664", _e)

            self._auto_stop.wait(interval)

    def stop(self):
        """停掉自动线程（测试/退出用）。"""
        if self._auto_stop:
            self._auto_stop.set()
        if self._auto_th:
            self._auto_th.join(timeout=3)
            self._auto_th = None

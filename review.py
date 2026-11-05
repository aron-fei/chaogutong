# -*- coding: utf-8 -*-
"""建议持久化 + 复盘准确率引擎。

  - record()      盘中窗口内把当日建议落盘 data/suggestions/YYYY-MM-DD.json
                  （同策略同代码只记首条，保留建议时刻的入场参考价）
  - review()      复盘：东财日K（ak.stock_zh_a_hist，腾讯 fqkline 端点已失效）
                  计算每条建议的次日收盘收益 / 次日最高冲高 / 是否达标 / 是否触止损，
                  汇总胜率、平均次日收益、达标率、止损率
  - today_records() 供卖点检测读取当日已建议标的（早盘强者恒强 / 尾盘补涨龙）

评价口径（专家内置，不给用户暴露阈值）：
  - 次日收益  = 次日收盘 / 建议价 - 1
  - 达标      = 次日最高 >= 目标价（strong +7% / lagging +6%）
  - 触止损    = 次日最低 <= 止损价（strong -5% / lagging -4.5%）
  - 胜        = 次日收益 > 0
"""
import json
import log_util
import re
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

import portfolio

SUG_DIR = Path(__file__).parent / "data" / "suggestions"
LOCK = threading.Lock()
TIMEOUT = 8

STRATEGY_LABEL = {"strong": "强者恒强", "lagging": "补涨龙"}


def _qt_symbol(code: str) -> str:
    # 2026-09-29：剥离 .SZ/.SH 等前后缀（同 bottom_surge 修复口径）
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if not code:
        return ""
    if code.startswith(("6", "9", "5")):
        return "sh" + code
    if code.startswith(("0", "3", "2")):
        return "sz" + code
    return ""


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------
def _day_file(d=None):
    SUG_DIR.mkdir(parents=True, exist_ok=True)
    return SUG_DIR / f"{d or date.today().isoformat()}.json"


def record(strategy, rows):
    """记录当日建议（同策略同代码只记首条，保留首次出现时刻的价格）。"""
    if not rows:
        return
    with LOCK:
        f = _day_file()
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        data["date"] = date.today().isoformat()
        items = data.setdefault(strategy, [])
        seen = {x.get("code") for x in items}
        for r in rows:
            code = r.get("code")
            if not code or code in seen:
                continue
            seen.add(code)
            items.append({
                "code": code,
                "name": r.get("name", ""),
                "entry_price": r.get("price"),
                "pct": r.get("pct"),
                "score": r.get("score"),
                "action": r.get("action", ""),
                "stop": r.get("stop", ""),
                "target": r.get("target", ""),
                "stop_price": r.get("stop_price"),
                "target_price": r.get("target_price"),
                "position": r.get("position", ""),
                "why": r.get("why", ""),
                "recorded_at": datetime.now().strftime("%H:%M:%S"),
            })
        f.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                     encoding="utf-8")


def today_records():
    """当日已落盘的建议，供卖点检测引用。"""
    f = _day_file()
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# 日K（体检 P1-1 根因修复：腾讯 web.ifzq.gtimg.cn/fqkline 已返回非 JSON，
# 原实现每只票都失败并打一行日志，复盘接口因此稳定慢 11~13s 且刷屏）
# ---------------------------------------------------------------------------
_KLINE_CACHE = {}       # code -> (ts, [bars])  进程内 TTL，同轮复盘不重复拉
_KLINE_LOCK = threading.Lock()
_KLINE_TTL = 600


def kline(code, n=15):
    """日K线，返回 [{date, open, close, high, low}] 按日期升序。

    主源东财 ak.stock_zh_a_hist（实测 0.06s），备源新浪 ak.stock_zh_a_daily；
    日期统一为 'YYYY-MM-DD' 字符串，保证与建议日 isoformat 的字典序比较正确。
    """
    now = time.time()
    with _KLINE_LOCK:
        hit = _KLINE_CACHE.get(code)
        if hit and now - hit[0] < _KLINE_TTL:
            return hit[1][-n:]
    rows = []
    try:
        import akshare as ak
        end = date.today()
        start = end - timedelta(days=150)
        df = ak.stock_zh_a_hist(symbol=str(code).zfill(6), period="daily",
                                start_date=start.strftime("%Y%m%d"),
                                end_date=end.strftime("%Y%m%d"), adjust="qfq")
        for _, r in df.iterrows():
            rows.append({"date": str(r["日期"])[:10], "open": float(r["开盘"]),
                         "close": float(r["收盘"]), "high": float(r["最高"]),
                         "low": float(r["最低"])})
    except Exception as _e:
        log_util.quiet_warn(f"review.kline.em:{code}", _e)
    if not rows:
        sym = _qt_symbol(code)
        if sym:
            try:
                import akshare as ak
                df = ak.stock_zh_a_daily(symbol=sym, adjust="qfq").tail(60)
                for idx, r in df.iterrows():
                    d = str(r.get("date", idx))[:10]
                    rows.append({"date": d, "open": float(r["open"]),
                                 "close": float(r["close"]), "high": float(r["high"]),
                                 "low": float(r["low"])})
            except Exception as _e:
                log_util.quiet_warn(f"review.kline.sina:{code}", _e)
    rows.sort(key=lambda x: x["date"])
    with _KLINE_LOCK:
        _KLINE_CACHE[code] = (now, rows)
    return rows[-n:]


def live_quote(codes):
    """腾讯实时行情兜底（今日建议的跟踪价）。"""
    syms = [_qt_symbol(c) for c in codes]
    syms = [s for s in syms if s]
    if not syms:
        return {}
    url = "http://qt.gtimg.cn/q=" + ",".join(syms)
    r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
    r.encoding = "gbk"
    out = {}
    for line in r.text.split(";"):
        if "~" not in line:
            continue
        p = line.split("~")
        if len(p) < 40 or not p[3]:
            continue
        try:
            out[p[2]] = {"price": float(p[3]), "pct": float(p[32])}
        except (ValueError, IndexError):
            continue
    return out


# ---------------------------------------------------------------------------
# 复盘
# ---------------------------------------------------------------------------
def _prefetch_klines(files):
    """并发预热日K缓存：10 天建议约 120 只不同标的，串行 0.1s/只 ≈ 12s。
    并发 10 路后约 1~2s，且失败由各源的降级逻辑兜住，不影响复盘主流程。"""
    codes = set()
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as _e:
            log_util.quiet_warn(f"review.prefetch.read:{f.name}", _e)
            continue
        for strat in ("strong", "lagging"):
            for it in data.get(strat, []):
                c = str(it.get("code") or "").zfill(6)
                if c and c.isdigit():
                    hit = _KLINE_CACHE.get(c)
                    if not hit or time.time() - hit[0] >= _KLINE_TTL:
                        codes.add(c)
    if not codes:
        return
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=10) as ex:
        list(ex.map(lambda c: kline(c, 15), codes))


def review(max_days=10):
    """汇总最近 max_days 个交易日的建议准确率。"""
    files = sorted(SUG_DIR.glob("*.json"), reverse=True)[:max_days] \
        if SUG_DIR.exists() else []
    _prefetch_klines(files)
    today = date.today().isoformat()
    kcache = {}
    days, stats = [], {"strong": [], "lagging": []}
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        sug_day = data.get("date", f.stem)
        items = []
        for strat in ("strong", "lagging"):
            for it in data.get(strat, []):
                row = _eval_item(it, strat, sug_day, today, kcache)
                items.append(row)
                if row.get("next_ret") is not None:
                    stats[strat].append(row)
        if items:
            days.append({"date": sug_day, "items": items})
    summary = {}
    for strat, rows in stats.items():
        if not rows:
            continue
        n = len(rows)
        wins = sum(1 for r in rows if r["next_ret"] > 0)
        summary[strat] = {
            "label": STRATEGY_LABEL.get(strat, strat),
            "count": n,
            "win_rate": round(wins / n * 100, 1),
            "avg_next_ret": round(sum(r["next_ret"] for r in rows) / n, 2),
            "target_rate": round(sum(1 for r in rows if r["target_hit"]) / n * 100, 1),
            "stop_rate": round(sum(1 for r in rows if r["stop_hit"]) / n * 100, 1),
            "avg_latest_ret": round(sum(r["latest_ret"] for r in rows
                                        if r["latest_ret"] is not None) / n, 2),
        }
    return {"days": days, "summary": summary,
            "evaluated_at": datetime.now().strftime("%m-%d %H:%M")}


# ---------------------------------------------------------------------------
# 当日综合复盘：市场复盘 + 账户操作复盘 + 建议准确率
# ---------------------------------------------------------------------------
def daily_review(md, adv):
    """聚合「当日复盘」tab 所需数据：
       - market:   市场复盘（指数/涨停/板块轮动/资金/情绪/展望）
       - account:  账户操作复盘（持仓盈亏/今日信号/操作建议）
       - history:  建议准确率（复用 review()）
    """
    snap = md.snapshot()
    zt_n = snap.get("zt_count") or 0
    dt_n = snap.get("dt_count") or 0
    zb_n = snap.get("zb_count") or 0
    zha_rate = round(zb_n / (zt_n + zb_n) * 100, 1) if (zt_n + zb_n) else 0.0
    max_lb = max((int(k) for k in (snap.get("ladder") or {})), default=0)

    sectors = snap.get("sectors") or []
    by_pct = sorted(sectors, key=lambda x: -x["pct"])
    by_inflow = sorted(sectors, key=lambda x: -(x.get("net_inflow") or 0))
    top_sectors = [{"name": s["name"], "pct": s["pct"],
                    "net_inflow": s.get("net_inflow"),
                    "leader": s.get("leader"),
                    "leader_pct": s.get("leader_pct")}
                   for s in by_pct[:6]]
    bot_sectors = [{"name": s["name"], "pct": s["pct"],
                    "net_inflow": s.get("net_inflow")}
                   for s in by_pct[-6:]]
    fund_in = [{"name": s["name"], "net_inflow": s.get("net_inflow"),
                "pct": s["pct"]}
               for s in by_inflow[:5] if (s.get("net_inflow") or 0) > 0]
    fund_out = [{"name": s["name"], "net_inflow": s.get("net_inflow"),
                 "pct": s["pct"]}
                for s in by_inflow[-5:] if (s.get("net_inflow") or 0) < 0]

    # 涨停行业分布 → 支线方向
    ind_zt = {}
    for s in (snap.get("zt_pool") or []):
        ind_zt[s.get("industry", "")] = ind_zt.get(s.get("industry", ""), 0) + 1
    ind_rank = sorted(ind_zt.items(), key=lambda x: -x[1])[:6]

    # 情绪判断
    b = snap.get("breadth") or {}
    breadth = {"up": b.get("up"), "down": b.get("down"), "flat": b.get("flat"),
               "total": b.get("total")}
    if zt_n >= 50 and zha_rate < 15:
        sentiment = "🟢 活跃（涨停多 + 炸板低）"
    elif zt_n >= 50:
        sentiment = "🟡 中性偏分歧（涨停多但炸板率偏高）"
    elif zt_n >= 20:
        sentiment = "🟡 中性"
    else:
        sentiment = "🔴 低迷（涨停 <20）"

    return {
        "time": snap.get("time"),
        "market_status": snap.get("market_status"),
        "status_text": snap.get("status_text"),
        "market": {
            "indices": snap.get("indices"),
            "zt_count": zt_n, "dt_count": dt_n, "zb_count": zb_n,
            "zha_rate": zha_rate, "max_lb": max_lb,
            "ladder": snap.get("ladder"),
            "breadth": breadth,
            "sentiment": sentiment,
            "top_sectors": top_sectors,
            "bot_sectors": bot_sectors,
            "fund_in": fund_in,
            "fund_out": fund_out,
            "ind_zt": ind_rank,
        },
        "account": _account_review(adv),
        "history": review(),
    }


def _account_review(adv):
    """账户操作复盘：持仓汇总 + 当日卖出/减仓信号 + 建议。
    信号来源：收盘后读 data/reviews/ 归档（时段建议 tab 已清空），盘中用 adv 实时信号。"""
    pv = portfolio.valuation()
    s = pv.get("summary") or {}
    pos = pv.get("positions") or []
    sell_rows, reduce_rows = [], []
    # 收盘后：读归档文件（最准的当日信号）
    arch = Path(__file__).parent / "data" / "reviews" / f"{date.today().isoformat()}.json"
    if arch.exists():
        try:
            adata = json.loads(arch.read_text(encoding="utf-8"))
            for r in adata.get("exit", []):
                if r.get("signal") == "sell":
                    sell_rows.append({"name": r.get("name"), "code": r.get("code"),
                                      "pct": r.get("pct"), "advice": r.get("advice"),
                                      "scope": r.get("scope")})
                elif r.get("signal") == "reduce":
                    reduce_rows.append({"name": r.get("name"), "code": r.get("code"),
                                        "pct": r.get("pct"), "advice": r.get("advice"),
                                        "scope": r.get("scope")})

        except Exception as _e:
            log_util.quiet_warn("review.py:291", _e)

    else:
        # 盘中：用 advisor 实时信号
        try:
            advd = adv.advise()
            for r in (advd.get("exit") or {}).get("rows", []):
                if r.get("signal") == "sell":
                    sell_rows.append({"name": r.get("name"), "code": r.get("code"),
                                      "pct": r.get("pct"), "advice": r.get("advice"),
                                      "scope": r.get("scope")})
                elif r.get("signal") == "reduce":
                    reduce_rows.append({"name": r.get("name"), "code": r.get("code"),
                                        "pct": r.get("pct"), "advice": r.get("advice"),
                                        "scope": r.get("scope")})

        except Exception as _e:
            log_util.quiet_warn("review.py:306", _e)

    return {
        "positions": len(pos),
        "market_value": s.get("market_value"),
        "cost": s.get("cost"),
        "pnl": s.get("pnl"),
        "pnl_pct": s.get("pnl_pct"),
        "day_pnl": s.get("day_pnl"),
        "quoted_at": pv.get("quoted_at"),
        "sell_rows": sell_rows,
        "reduce_rows": reduce_rows,
        "sell_count": len(sell_rows),
        "reduce_count": len(reduce_rows),
    }


def _eval_item(it, strat, sug_day, today, kcache):
    code = it.get("code", "")
    entry = it.get("entry_price") or 0
    row = {**{k: it.get(k) for k in
              ("code", "name", "entry_price", "pct", "score", "action",
               "stop", "target", "position", "recorded_at")},
           "strategy": strat, "strategy_label": STRATEGY_LABEL.get(strat, strat),
           "next_date": None, "next_close": None, "next_ret": None,
           "next_high_ret": None, "next_low_ret": None,
           "target_price": it.get("target_price") or round(entry * 1.06, 2),
           "stop_price": it.get("stop_price") or round(entry * 0.95, 2),
           "target_hit": False, "stop_hit": False,
           "latest_close": None, "latest_ret": None, "label": "跟踪中"}
    if not code or entry <= 0:
        row["label"] = "数据缺失"
        return row
    if code not in kcache:
        try:
            kcache[code] = kline(code, 15)
        except Exception as e:
            print(f"[review] K线失败 {code}: {e}")
            kcache[code] = []
    bars = kcache[code]
    if not bars:
        row["label"] = "K线缺失"
        return row
    after = [b for b in bars if b["date"] > sug_day]
    if not after:
        # 当日建议：用最新价跟踪
        last = bars[-1]
        if last["close"] > 0:
            row["latest_close"] = last["close"]
            row["latest_ret"] = round((last["close"] / entry - 1) * 100, 2)
        return row
    nxt = after[0]
    row["next_date"] = nxt["date"]
    row["next_close"] = nxt["close"]
    row["next_ret"] = round((nxt["close"] / entry - 1) * 100, 2)
    row["next_high_ret"] = round((nxt["high"] / entry - 1) * 100, 2)
    row["next_low_ret"] = round((nxt["low"] / entry - 1) * 100, 2)
    row["target_hit"] = nxt["high"] >= row["target_price"]
    row["stop_hit"] = nxt["low"] <= row["stop_price"]
    last = bars[-1]
    row["latest_close"] = last["close"]
    row["latest_ret"] = round((last["close"] / entry - 1) * 100, 2)
    if row["stop_hit"] and not row["target_hit"]:
        row["label"] = "止损"
    elif row["target_hit"]:
        row["label"] = "达标"
    elif row["next_ret"] > 0:
        row["label"] = "胜"
    else:
        row["label"] = "负"
    return row

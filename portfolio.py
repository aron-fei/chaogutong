# -*- coding: utf-8 -*-
"""持仓模块：本地 JSON 存储 + 腾讯批量接口实时估值 + 只读操作建议参考层

设计原则（对齐 market-advisor 铁律）：
  - 纯手动录入，不碰任何券商账号，只参谋不替人下单
  - 持仓数据只存本地 portfolio.json，永不上传
  - 【ADR-0052】真实持仓给只读参考建议，但绝不写回测库/确认单/派单链路：
    「给建议」与「进策略」是两件事，前者是输出层，后者被 ADR-0011/0032/0044 硬隔离
"""
import json
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

import requests

PORTFOLIO_FILE = Path(__file__).parent / "portfolio.json"
LOCK = threading.Lock()
TIMEOUT = 5

# 板前缀：6=沪、0/3=深、4/8=北交所、5/1=沪市基金/债，默认按沪深处理
def _qt_symbol(code: str) -> str:
    # 2026-09-29 修复：原实现不剥离后缀，"600519.SH" 会拼成 "sh600519.SH"（无效符号，行情静默失败）。
    # 现统一只取数字，兼容 600519 / 600519.SH / sh600519。空输入返回空串交由调用方跳过。
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if not code:
        return ""
    if code.startswith(("6", "5", "9")):
        return "sh" + code
    if code.startswith(("0", "3", "1")):
        return "sz" + code
    if code.startswith(("4", "8")):
        return "bj" + code
    return "sh" + code


def _load():
    if not PORTFOLIO_FILE.exists():
        return []
    try:
        return json.loads(PORTFOLIO_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(positions):
    PORTFOLIO_FILE.write_text(
        json.dumps(positions, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
def list_positions():
    with LOCK:
        return _load()


def add_position(code, name, cost, shares, note=""):
    code = str(code).strip()
    cost, shares = float(cost), float(shares)
    if cost <= 0 or shares <= 0:
        raise ValueError("成本价和数量必须大于 0")
    if not code.isdigit() or len(code) != 6:
        raise ValueError("股票代码须为 6 位数字")
    with LOCK:
        positions = _load()
        if any(p["code"] == code for p in positions):
            raise ValueError(f"{code} 已在持仓中，请先删除或直接编辑")
        positions.append({
            "code": code,
            "name": str(name).strip() or code,
            "cost": cost,
            "shares": shares,
            "note": str(note).strip(),
            "added_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        })
        _save(positions)
        return positions


def update_position(code, cost, shares, name=None, note=None):
    with LOCK:
        positions = _load()
        for p in positions:
            if p["code"] == code:
                p["cost"] = float(cost)
                p["shares"] = float(shares)
                if name is not None:
                    p["name"] = str(name).strip()
                if note is not None:
                    p["note"] = str(note).strip()
                _save(positions)
                return positions
    raise ValueError(f"未找到持仓 {code}")


def remove_position(code):
    with LOCK:
        positions = _load()
        positions = [p for p in positions if p["code"] != code]
        _save(positions)
        return positions


# ---------------------------------------------------------------------------
# 实时估值：腾讯批量接口（~0.1s，与指数同源）
# ---------------------------------------------------------------------------
# 返回字段索引: 1名称 2代码 3最新价 4昨收 31涨跌 32涨跌幅 36成交量 37成交额(万)
def quote(codes):
    """批量取实时行情：{code: {...}}"""
    codes = [c for c in codes if c]
    if not codes:
        return {}
    url = "http://qt.gtimg.cn/q=" + ",".join(_qt_symbol(c) for c in codes)
    r = requests.get(url, timeout=TIMEOUT,
                     headers={"User-Agent": "Mozilla/5.0"})
    r.encoding = "gbk"
    out = {}
    for line in r.text.split(";"):
        if "~" not in line:
            continue
        p = line.split("~")
        if len(p) < 38 or not p[3]:
            continue
        try:
            out[p[2]] = {
                "name": p[1],
                "price": float(p[3]),
                "prev_close": float(p[4]),
                "chg": float(p[31]),
                "pct": float(p[32]),
                "amount_yi": round(float(p[37]) / 1e4, 2) if p[37] else 0,
            }
        except (ValueError, IndexError):
            continue
    return out


def valuation():
    """持仓总览：逐只盈亏 + 汇总。无持仓时返回空结构。"""
    with LOCK:
        positions = _load()
    if not positions:
        return {"positions": [], "summary": {"count": 0}, "quoted_at": None}
    q = quote([p["code"] for p in positions])
    now = datetime.now().strftime("%H:%M:%S")
    rows, total_mv, total_cost, day_pnl = [], 0.0, 0.0, 0.0
    for p in positions:
        k = q.get(p["code"])
        if not k:
            rows.append({**p, "price": None, "live_name": None,
                         "market_value": None, "pnl": None,
                         "pnl_pct": None, "day_pct": None})
            continue
        mv = k["price"] * p["shares"]
        cost_sum = p["cost"] * p["shares"]
        pnl = mv - cost_sum
        rows.append({
            **p,
            "live_name": k["name"],
            "price": k["price"],
            "market_value": round(mv, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl / cost_sum * 100, 2) if cost_sum else None,
            "day_pct": k["pct"],
        })
        total_mv += mv
        total_cost += cost_sum
        day_pnl += (k["price"] - k["prev_close"]) * p["shares"]
    summary = {
        "count": len(positions),
        "market_value": round(total_mv, 2),
        "cost": round(total_cost, 2),
        "pnl": round(total_mv - total_cost, 2),
        "pnl_pct": round((total_mv - total_cost) / total_cost * 100, 2)
                  if total_cost else None,
        "day_pnl": round(day_pnl, 2),
    }
    return {"positions": rows, "summary": summary, "quoted_at": now}


# ---------------------------------------------------------------------------
# 【ADR-0052】真实持仓 · 只读操作建议参考层
# ---------------------------------------------------------------------------
# 历史脉络：ADR-0009 曾按用户诉求做过真实持仓实时建议，ADR-0011 因「真实持仓乱入
# 模拟盘账户/弹窗」把它整体回退。本层取中间路线：
#   ✅ 给建议：复用 position_watch.decide_watch() 同一套已验证矩阵，真实持仓逐只评估
#   ❌ 不进策略：不写 pending_*_confirm / backtest_database / order_plan，不触发弹窗
# 这样既回应「昨天加的持仓今天没建议」的诉求，又不重蹈 ADR-0011 的污染事故。
_ADVICE_TTL = 45           # 秒；与 10s 估值轮询解耦，避免每次都拉日K
_ADVICE_CACHE = {"key": None, "ts": 0.0, "data": None}
_ADVICE_LOCK = threading.Lock()


def _mkt_temp_proxy():
    """大盘温度代理：涨家数/(涨+跌)×100，取 market-monitor 已缓存快照，零额外请求。"""
    try:
        import data_fetcher
        md = data_fetcher.MarketData()
        b = md.snapshot().get("breadth") or {}
        up, down = b.get("up"), b.get("down")
        if not up and not down:
            return None
        return round(float(up) / float(up + down) * 100.0, 2)
    except Exception as e:
        print(f"[portfolio.advice] 大盘温度取数失败: {type(e).__name__}: {e}")
        traceback.print_exc()
        return None


def _advice_for_one(code, name, cost, shares, pw, quotes, trend_cache, mkt_temp, lite):
    """对单只真实持仓评估。lite=True 时只做快速口径（无日K/无大盘温度）。"""
    q = quotes.get(code)
    if not q:
        return {"symbol": code, "name": name, "action": "HOLD",
                "action_label": "数据缺失", "level": 3,
                "reason": "无法获取实时行情，本轮不评估", "stale": True}
    price = q["price"]
    avg = q.get("avg_price")
    hi, lo = q.get("high"), q.get("low")
    day_pos = (price - lo) / (hi - lo) if hi is not None and lo is not None and hi > lo else None
    tr = {} if lite else (trend_cache.get(code) or {})
    pos60 = None
    if tr.get("range_hi") and tr.get("range_lo") and tr["range_hi"] > tr["range_lo"]:
        pos60 = (price - tr["range_lo"]) / (tr["range_hi"] - tr["range_lo"])
    trend = "flat"
    if tr.get("ma5") and tr.get("ma10"):
        if price > tr["ma5"] > tr["ma10"]:
            trend = "up"
        elif price < tr["ma5"] < tr["ma10"]:
            trend = "down"
    pnl_pct = (price - cost) / cost * 100.0 if cost else None
    action, reason = pw.instance().decide_watch({
        "pct": q.get("pct"), "sp60": None, "sp180": None, "flow": None,
        "price": price, "avg": avg, "day_pos": day_pos,
        "pnl_pct": pnl_pct, "trend": trend, "pos60": pos60,
        "mkt_temp": mkt_temp,
    })
    meta = pw.ACTION_META.get(action, {})
    return {
        "symbol": code, "name": name, "action": action,
        "action_label": meta.get("label", action), "level": meta.get("level", 3),
        "cls": meta.get("cls", ""), "reason": reason, "stale": False,
        "price": price, "pct": q.get("pct"), "pnl_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
        "day_pos": round(day_pos, 3) if day_pos is not None else None,
        "pos60": round(pos60, 3) if pos60 is not None else None,
        "trend": trend, "ma5": tr.get("ma5"), "ma10": tr.get("ma10"), "ma20": tr.get("ma20"),
        "vol_ratio": q.get("vol_ratio"), "turnover": q.get("turnover"),
        "note": "只读参考建议 · 不自动执行 · 不进模拟盘账户",
    }


def advise(positions=None, lite=False, force=False):
    """真实持仓逐只操作建议（只读参考，绝不产生任何副作用）。

    Args:
      positions: 可选，已加载的持仓列表；缺省从 portfolio.json 读
      lite: True 跳过日K/大盘温度（快速口径，适合非交易时段 / 弱网降级）
      force: True 忽略 TTL 缓存

    Returns:
      {code: advice_row}；空持仓返回 {}
    """
    positions = list_positions() if positions is None else list(positions or [])
    if not positions:
        return {}
    key = tuple((str(p.get("code")), p.get("cost"), p.get("shares")) for p in positions)
    now = time.time()
    if not force:
        with _ADVICE_LOCK:
            hit = _ADVICE_CACHE
            if hit["key"] == key and hit["data"] is not None and now - hit["ts"] < _ADVICE_TTL:
                return hit["data"]
    try:
        import position_watch as pw
        codes = [str(p.get("code")) for p in positions if p.get("code")]
        quotes = pw.fetch_quotes(codes)
        trend_cache = pw.TrendCache(ttl=int(_cfg_pw("PW_TREND_TTL", 300))) if not lite else None
        mkt_temp = None if lite else _mkt_temp_proxy()
        out = {}
        for p in positions:
            code = str(p.get("code") or "")
            if not code:
                continue
            out[code] = _advice_for_one(
                code, p.get("name") or code, float(p.get("cost") or 0),
                float(p.get("shares") or 0), pw, quotes, trend_cache, mkt_temp, lite)
        with _ADVICE_LOCK:
            _ADVICE_CACHE.update({"key": key, "ts": now, "data": out})
        return out
    except Exception as e:
        print(f"[portfolio.advice] 评估失败: {type(e).__name__}: {e}")
        traceback.print_exc()
        return {}


def _cfg_pw(name, default):
    try:
        import config
        v = getattr(config, name, None)
        return default if v is None else v
    except Exception:
        return default


if __name__ == "__main__":
    print(json.dumps(advise(force=True), ensure_ascii=False, indent=2))

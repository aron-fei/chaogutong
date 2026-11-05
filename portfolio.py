# -*- coding: utf-8 -*-
"""持仓模块：本地 JSON 存储 + 腾讯批量接口实时估值

设计原则（对齐 market-advisor 铁律）：
  - 纯手动录入，不碰任何券商账号，只参谋不替人下单
  - 持仓数据只存本地 portfolio.json，永不上传
"""
import json
import threading
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

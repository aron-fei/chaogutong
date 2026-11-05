#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""昨日（或指定日）操作确认单 —— 假设成交的多情景盈亏复盘核算。

用途：回答「昨天尾盘/午盘确认框推荐了哪几只、今天走势如何、假设买入是否赚钱」。

数据源：
  实时行情  qt.gtimg.cn（沙箱需 curl --noproxy，本脚本用 requests 直连）
  分时数据  web.ifzq.gtimg.cn/appstock/app/minute/query

用法：
  /opt/homebrew/anaconda3/bin/python3 recap_confirm.py                     # 默认取最近一份确认单
  python3 recap_confirm.py --session 1440 --date 2026-09-16
  python3 recap_confirm.py --file /path/to/xxx.confirmed.json --exit 1040

退出方式 (--exit)：
  1040  次日 10:40 全部出清（按确认单 note 的原始纪律）
  close 持有到当日收盘
  sw    按强弱：10:40 封涨停的持有到收盘，其余 10:40 出清
  all   同时输出以上三种（默认）

成本基准：建议价(suggest) 与 前收盘(prevclose) 两种都算。
费用：买入 佣金 0.025%(min 5 元)+过户 0.001%；卖出 佣金 0.025%(min 5)+印花 0.05%+过户 0.001%。
"""
import config
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.request import Request, urlopen

REPORTS_DIR = config.REPORTS_DIR
EXTRACT_1040 = "1040"


def qt_symbol(symbol: str) -> str:
    """000823.SZ -> sz000823 ; 600088.SS -> sh600088 ; 688xxx.SS -> sh688xxx"""
    code, _, mkt = symbol.partition(".")
    mkt = (mkt or "").upper()
    if mkt in ("SZ",):
        return "sz" + code
    if mkt in ("SS", "SH"):
        return "sh" + code
    # 兜底：6/9 开头沪市，其余深市
    return ("sh" if code[:1] in ("6", "9") else "sz") + code


def _get(url: str, referer: str | None = None) -> bytes:
    headers = {"User-Agent": "Mozilla/5.0"}
    if referer:
        headers["Referer"] = referer
    req = Request(url, headers=headers)
    with urlopen(req, timeout=15) as r:
        return r.read()


def fetch_quote(symbol: str) -> dict:
    """返回 {name, price, prev_close, open, high, low, pct}"""
    q = qt_symbol(symbol)
    raw = _get(f"https://qt.gtimg.cn/q={q}", referer="https://finance.qq.com/")
    txt = raw.decode("gb18030", "ignore")
    m = re.search(r'="([^"]*)"', txt)
    if not m:
        return {}
    p = m.group(1).split("~")
    def f(i):
        try:
            return float(p[i])
        except Exception:
            return None
    return {
        "name": p[1],
        "price": f(3),
        "prev_close": f(4),
        "open": f(5),
        "high": f(33),
        "low": f(34),
        "pct": f(32),
    }


def fetch_minute(symbol: str) -> list[tuple[str, float]]:
    """返回 [(HHMM, price), ...]"""
    q = qt_symbol(symbol)
    raw = _get(f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={q}")
    d = json.loads(raw.decode("utf-8", "ignore"))
    key = list(d["data"].keys())[0]
    rows = d["data"][key]["data"]["data"]
    out = []
    for r in rows:
        parts = r.split()
        if len(parts) >= 2:
            try:
                out.append((parts[0], float(parts[1])))
            except ValueError:
                continue
    return out


def price_at(series: list[tuple[str, float]], hhmm: str) -> float | None:
    prev = None
    for t, p in series:
        if t <= hhmm:
            prev = p
        else:
            break
    return prev


def fee_buy(amount: float) -> float:
    return max(amount * 0.00025, 5.0) + amount * 0.00001


def fee_sell(amount: float) -> float:
    return max(amount * 0.00025, 5.0) + amount * 0.0005 + amount * 0.00001


def load_confirm(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def latest_confirm() -> Path | None:
    files = sorted(REPORTS_DIR.glob("pending_buy_confirm_*.confirmed.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def is_limit_up(price: float | None, prev: float | None) -> bool:
    if not price or not prev:
        return False
    # 主板 10%（20cm 票需另判，这里以 9.8% 近似）
    return price >= prev * 1.098


EXIT_LABELS = {"1040": "10:40 全清", "close": "持有到收盘", "sw": "按强弱(封板持有)"}


def build_rows(items: list[dict]) -> list[dict]:
    rows = []
    for it in items:
        sym = it.get("symbol", "")
        q = fetch_quote(sym)
        try:
            series = fetch_minute(sym)
        except Exception as e:
            series = []
            print(f"  ! 分时获取失败 {sym}: {e}", file=sys.stderr)
        prev = q.get("prev_close") or it.get("prev_close")
        p1040 = price_at(series, EXTRACT_1040)
        close = q.get("price")
        rows.append({
            "name": it.get("name") or q.get("name") or sym,
            "symbol": sym,
            "shares": it.get("suggest_shares") or 0,
            "base_suggest": it.get("suggest_price"),
            "base_prev": prev,
            "p1040": p1040,
            "close": close,
            "limit_1040": is_limit_up(p1040, prev),
            "quote": q,
            "series": series,
        })
    return rows


def scenario(rows: list[dict], base_key: str, exit_key: str) -> dict:
    total_cost = total_rev = 0.0
    detail = []
    for r in rows:
        sh = r["shares"]
        bp = r["base_suggest"] if base_key == "suggest" else r["base_prev"]
        if not sh or not bp:
            continue
        if exit_key == "1040":
            sp = r["p1040"]
        elif exit_key == "close":
            sp = r["close"]
        else:
            sp = r["close"] if r["limit_1040"] else r["p1040"]
        if not sp:
            continue
        cost = bp * sh
        rev = sp * sh
        pnl = (rev - fee_sell(rev)) - (cost + fee_buy(cost))
        total_cost += cost + fee_buy(cost)
        total_rev += rev - fee_sell(rev)
        detail.append({"name": r["name"], "buy": bp, "sell": sp, "shares": sh,
                       "pnl": pnl, "pct": pnl / cost * 100})
    net = total_rev - total_cost
    return {"cost": total_cost, "net": net,
            "pct": (net / total_cost * 100) if total_cost else 0.0,
            "detail": detail}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=None, help="确认单 json 路径")
    ap.add_argument("--session", default=None, help="时段，如 1440 / 1340")
    ap.add_argument("--date", default=None, help="日期 YYYY-MM-DD")
    ap.add_argument("--exit", default="all", choices=["1040", "close", "sw", "all"])
    args = ap.parse_args()

    if args.file:
        path = Path(args.file)
    elif args.session and args.date:
        path = REPORTS_DIR / f"pending_buy_confirm_{args.session}_{args.date}.confirmed.json"
    else:
        path = latest_confirm()
    if not path or not path.exists():
        print("找不到确认单文件", file=sys.stderr)
        return 1

    conf = load_confirm(path)
    items = conf.get("items", [])
    print(f"确认单: {path.name}")
    print(f"日期: {conf.get('date')}  时段: {conf.get('session')}  "
          f"确认状态: {conf.get('status')}")
    actual = sum(1 for it in items if (it.get('actual_lots') or 0) > 0)
    print(f"实际成交: {actual} 只 / 建议 {len(items)} 只\n")

    rows = build_rows(items)

    print("【今日(次日)行情】")
    for r in rows:
        q = r["quote"]
        print(f"  {r['name']:<8} {r['symbol']:<11} 前收 {r['base_prev']}  "
              f"开 {q.get('open')}  高 {q.get('high')}  低 {q.get('low')}  "
              f"收 {r['close']} ({q.get('pct')}%)  10:40 {r['p1040']}"
              f"{'  [封板]' if r['limit_1040'] else ''}")
    print()

    bases = [("suggest", "成本=建议价"), ("prev", "成本=前收盘")]
    exits = ["1040", "close", "sw"] if args.exit == "all" else [args.exit]
    for bk, bl in bases:
        for ek in exits:
            s = scenario(rows, bk, ek)
            print(f"【{bl} × {EXIT_LABELS[ek]}】  净盈亏 {s['net']:+,.0f} 元 "
                  f"({s['pct']:+.2f}%)  成本 {s['cost']:,.0f}")
            for d in s["detail"]:
                print(f"    {d['name']:<8} 买 {d['buy']:.2f} × {d['shares']} → "
                      f"卖 {d['sell']:.2f}   盈亏 {d['pnl']:+,.0f} ({d['pct']:+.2f}%)")
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

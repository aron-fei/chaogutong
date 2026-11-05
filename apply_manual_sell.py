#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手工平账：把「实际已成交但系统未记录」的卖出写回回测库（2026-10-21 润建全清）。

背景：用户 2026-10-21 早盘看到 M9 持仓盯盘的「【减半】」弹窗后手动全清润建股份
（002929.SZ）5 手，但未在操作确认台勾选 actual_sell → 系统账上仍是 held 5 手，
账实不符。本脚本按用户提供的**实际成交价**写回。

安全设计：
  · 写入前强制备份 backtest_database.json
  · 幂等：若该笔卖出已在 completed_trades 中则跳过
  · 校验：成交价 > 0、股数为 100 整数倍、持仓存在且股数足够
  · 摩擦成本按既有口径（见下）估算，可 --no-fee 关闭
用法：
  /opt/homebrew/anaconda3/bin/python3 apply_manual_sell.py --symbol 002929.SZ --shares 500 --price 53.50
"""
import argparse
import json
import os
import shutil
from datetime import datetime

DB = "/Users/williamli/Work/reports/backtest_database.json"


def _fee(shares, price, side):
    """摩擦成本（与既有回测口径一致：佣金万2.5双向 + 卖出印花税千1 + 过户费十万分之2）。
    佣金不足 5 元按 5 元计（券商最低收费）。"""
    amount = shares * price
    commission = max(amount * 0.00025, 5.0)
    if side == "sell":
        commission += amount * 0.0001      # 印花税（卖出单边）
    commission += amount * 0.00002          # 过户费
    return round(commission, 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--shares", type=int, required=True)
    ap.add_argument("--price", type=float, required=True)
    ap.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--session", default="09:30")
    ap.add_argument("--no-fee", action="store_true", help="不计摩擦成本")
    ap.add_argument("--dry-run", action="store_true", help="只打印不写盘")
    args = ap.parse_args()

    with open(DB, encoding="utf-8") as f:
        db = json.load(f)

    # --- 幂等检查 ---
    for t in db.get("completed_trades", []):
        if (t.get("symbol") == args.symbol and t.get("sell_date") == args.date
                and int(t.get("shares") or 0) == args.shares):
            print("ℹ️ 该笔卖出已存在，跳过：%s %s @%s" % (args.symbol, args.shares, args.price))
            return 0

    # --- 找持仓 ---
    pos = None
    for p in db.get("active_positions", []):
        if args.symbol in str(p.get("symbol", "")):
            pos = p
            break
    if not pos:
        print("❌ 未找到持仓：%s（当前持仓：%s）"
              % (args.symbol, [p.get("symbol") for p in db.get("active_positions", [])]))
        return 1
    held = int(pos.get("shares") or 0)
    if args.shares > held:
        print("❌ 卖出股数 %d > 持仓 %d" % (args.shares, held))
        return 1
    if args.shares % 100 != 0:
        print("❌ 卖出股数必须是 100 的整数倍（1 手 = 100 股），当前 %d" % args.shares)
        return 1
    if args.price <= 0:
        print("❌ 成交价必须 > 0")
        return 1

    cost = float(pos.get("buy_price") or 0)
    gross = (args.price - cost) * args.shares
    fee = 0.0 if args.no_fee else _fee(args.shares, args.price, "sell")
    net = round(gross - fee, 2)

    print("=== 手工平账预览 ===")
    print("  标的: %s %s" % (pos.get("name"), pos.get("symbol")))
    print("  买入: %s %s @%.2f (%s股)" % (pos.get("buy_date"), pos.get("buy_session"), cost, held))
    print("  卖出: %s %s @%.2f (%s股)" % (args.date, args.session, args.price, args.shares))
    print("  毛盈亏: %+.2f 元 (%.2f%%)" % (gross, gross / (cost * args.shares) * 100 if cost else 0))
    print("  摩擦: -%.2f 元%s" % (fee, "（未计）" if args.no_fee else ""))
    print("  净盈亏: %+.2f 元" % net)
    print("  剩余持仓: %d → %d 股" % (held, held - args.shares))
    print("  可用现金: %.2f → %.2f" % (db.get("available_cash", 0),
                                        db.get("available_cash", 0) + net))
    if args.dry_run:
        print("\n(dry-run，未写盘)")
        return 0

    # --- 备份 ---
    bak = DB + ".bak_20261021_manual_sell"
    shutil.copy2(DB, bak)
    print("\n✅ 已备份：%s" % os.path.basename(bak))

    # --- 写回 ---
    pos["shares"] = held - args.shares
    if pos["shares"] <= 0:
        db["active_positions"] = [p for p in db["active_positions"]
                                  if p is not pos]
        print("  持仓清零 → 从 active_positions 移除")
    db["available_cash"] = round(db.get("available_cash", 0) + net, 2)
    db.setdefault("completed_trades", []).append({
        "name": pos.get("name"), "symbol": pos.get("symbol"),
        "shares": args.shares, "buy_price": cost, "sell_price": args.price,
        "buy_date": pos.get("buy_date"), "buy_session": pos.get("buy_session"),
        "sell_date": args.date, "sell_session": args.session,
        "pnl": net, "gross_pnl": round(gross, 2), "fee": fee,
        "source": "manual_confirm",
        "note": "手工平账：用户看到 M9 减半弹窗后手动全清，未在确认台勾选",
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    # 总权益 = 现金 + 持仓成本市值（与既有口径一致）
    held_mv = sum(float(p.get("shares") or 0) * float(p.get("buy_price") or 0)
                  for p in db.get("active_positions", []))
    db["total_equity"] = round(db.get("available_cash", 0) + held_mv, 2)

    with open(DB, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    print("✅ 已写回 %s" % os.path.basename(DB))
    print("  新可用现金: %.2f | 新总权益: %.2f | 已完成交易: %d 笔"
          % (db["available_cash"], db["total_equity"], len(db["completed_trades"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

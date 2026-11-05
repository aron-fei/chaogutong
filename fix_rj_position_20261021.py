#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""润建股份（002929.SZ）完整生命周期修正 —— 2026-10-21。

背景（用户确认的真实成交）：
  · 10-20 13:40 买入：**系统建议 5 手(500股)，实际挂单只成交 4 手(400股)**
    → 回测库误记 500 股（capital 24740 = 49.48×500，buy_fee 也按 500 算）
  · 10-21 早盘 09:3x 手动全清，两笔：
      2 手(200股) @ 54.90
      2 手(200股) @ 54.70
    → 合计 4 手(400股)，与实际持仓一致
    → 但系统侧未在确认台勾选，回测库仍是 held 500股，账实不符

本脚本一次性修正：
  1) 买入股数 500 → 400（capital / buy_fee 按 400 股重算）
  2) 记入两笔卖出成交（completed_trades）
  3) 从 active_positions 移除润建（已全部清仓）
  4) 重算 available_cash 与 total_equity

安全：写盘前自动备份；--dry-run 只预览。
"""
import argparse
import json
import os
import shutil
from datetime import datetime

DB = "/Users/williamli/Work/reports/backtest_database.json"
SYMBOL = "002929.SZ"
NAME = "润建股份"


def fee(shares, price, side):
    """摩擦成本：佣金万2.5(最低5元) + 卖出印花税千1 + 过户费十万分之2"""
    amount = shares * price
    c = max(amount * 0.00025, 5.0)
    if side == "sell":
        c += amount * 0.0001
    c += amount * 0.00002
    return round(c, 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(DB, encoding="utf-8") as f:
        db = json.load(f)

    pos = None
    for p in db.get("active_positions", []):
        if SYMBOL in str(p.get("symbol", "")):
            pos = p
            break
    if not pos:
        print("ℹ️ 回测库中已无 %s 持仓（可能已平账），无需处理。" % NAME)
        return 0

    cost = float(pos.get("buy_price") or 0)
    recorded_shares = int(pos.get("shares") or 0)
    REAL_BUY = 400                       # 实际成交 4 手
    sells = [(200, 54.90, "09:32"), (200, 54.70, "09:34")]

    print("=== 润建股份 账实修正 ===")
    print("  【买入修正】")
    print("    系统记录: %d 股 (%.2f 元, buy_fee %.3f)" % (
        recorded_shares, pos.get("capital", 0), pos.get("buy_fee", 0)))
    print("    实际成交: %d 股 (%.2f 元)" % (REAL_BUY, cost * REAL_BUY))
    diff = recorded_shares - REAL_BUY
    if diff:
        print("    → 多记 %d 股，capital 需减 %.2f，buy_fee 需减 %.3f" % (
            diff, cost * diff, fee(recorded_shares, cost, "buy") - fee(REAL_BUY, cost, "buy")))

    old_capital = float(pos.get("capital") or 0)
    old_fee = float(pos.get("buy_fee") or 0)
    new_capital = round(cost * REAL_BUY, 2)
    new_buy_fee = fee(REAL_BUY, cost, "buy")

    print("\n  【卖出记录】")
    total_gross = 0.0
    total_sell_fee = 0.0
    for sh, px, sess in sells:
        gross = round((px - cost) * sh, 2)
        sf = fee(sh, px, "sell")
        total_gross += gross
        total_sell_fee += sf
        print("    %s %d股 @%.2f → 毛 %+.2f, 摩擦 -%.2f" % (sess, sh, px, gross, sf))
    net = round(total_gross - total_sell_fee, 2)
    print("    合计毛盈亏 %+.2f | 卖出摩擦 -%.2f | 卖出净额 %+.2f" % (
        total_gross, total_sell_fee, net))

    # 完整净盈亏 = 卖出净额 - 实际买入摩擦（因为 capital 口径不含买入摩擦）
    full_net = round(net - new_buy_fee, 2)
    print("    完整净盈亏（含买入摩擦 -%.2f）= %+.2f 元" % (new_buy_fee, full_net))

    # 现金影响 = 卖出回款 - 卖出摩擦 - （多记部分的成本回退）
    cash_delta = round(sum(px * sh for sh, px, _ in sells) - total_sell_fee, 2)
    print("\n  【现金影响】")
    print("    卖出回款 %.2f - 卖出摩擦 %.2f = %+.2f" % (
        sum(px * sh for sh, px, _ in sells), total_sell_fee, cash_delta))
    print("    买入多记 %d 股 → 现金需退 %.2f" % (diff, cost * diff) if diff else "    买入无多记")
    print("    可用现金: %.2f → %.2f" % (
        db.get("available_cash", 0), db.get("available_cash", 0) + cash_delta - (cost * diff if diff else 0)))

    if args.dry_run:
        print("\n(dry-run，未写盘)")
        return 0

    # ---- 备份 ----
    bak = DB + ".bak_20261021_rj_fix"
    shutil.copy2(DB, bak)
    print("\n✅ 已备份：%s" % os.path.basename(bak))

    # ---- 1) 修正买入股数（仅当有持仓记录时；已清仓则从 completed_trades 补正）----
    for t in db.get("completed_trades", []):
        if SYMBOL in str(t.get("symbol", "")) and str(t.get("buy_date")) == "2026-10-20":
            if int(t.get("shares") or 0) == recorded_shares:
                t["shares"] = REAL_BUY
                t["note"] = (t.get("note", "") + " | 2026-10-21 修正：实际成交 400 股（建议500股未全成）").strip()
                print("  ✅ 已修正 completed_trades 中的买入股数 → %d" % REAL_BUY)

    # ---- 2) 记入两笔卖出 ----
    for sh, px, sess in sells:
        gross = round((px - cost) * sh, 2)
        sf = fee(sh, px, "sell")
        db.setdefault("completed_trades", []).append({
            "name": NAME, "symbol": SYMBOL,
            "shares": sh, "buy_price": cost, "sell_price": px,
            "buy_date": "2026-10-20", "buy_session": "13:40",
            "sell_date": "2026-10-21", "sell_session": sess,
            "pnl": round(gross - sf, 2), "gross_pnl": gross, "fee": sf,
            "source": "manual_confirm",
            "note": "手工平账：M9 减半弹窗后手动分两笔卖出（未在确认台勾选）",
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
    print("  ✅ 已记入 2 笔卖出成交")

    # ---- 3) 移除持仓 ----
    db["active_positions"] = [p for p in db["active_positions"] if p is not pos]
    print("  ✅ 已从 active_positions 移除（4 手全部清空）")

    # ---- 4) 重算现金与权益 ----
    db["available_cash"] = round(db.get("available_cash", 0) + cash_delta - (cost * diff if diff else 0), 2)
    held_mv = sum(float(p.get("shares") or 0) * float(p.get("buy_price") or 0)
                  for p in db.get("active_positions", []))
    db["total_equity"] = round(db.get("available_cash", 0) + held_mv, 2)

    with open(DB, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

    print("\n✅ 已写回 %s" % os.path.basename(DB))
    print("  可用现金: %.2f | 总权益: %.2f | 持仓 %d 只 | 已完成交易 %d 笔" % (
        db["available_cash"], db["total_equity"],
        len(db["active_positions"]), len(db["completed_trades"])))
    for p in db["active_positions"]:
        print("    持仓: %s %s | %d股 @%.2f" % (
            p.get("name"), p.get("symbol"), p.get("shares"), p.get("buy_price")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

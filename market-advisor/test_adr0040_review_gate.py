#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ADR-0040 复盘闸门 闭环端到端测试。

回答的问题：**「操作台弹出的推票逻辑」与「复盘」是否真的形成数据闭环？**

逐跳验证（用 10-08 真实 trade_gate.json 跑完整链路）：
  第1跳 复盘闸门 → 买入候选      (_apply_review_gate)
  第2跳 候选 → 待确认买入单      (get_buy_intents → write_pending_buy_confirm)
  第3跳 候选 → 弹窗闪卡          (build_ops_flashcard)
  第4跳 候选 → 订单计划/回测账户  (build_order_plan)
  第5跳 执行留痕 → 次日复盘效能   (trade_gate_applied.json → review_gate.effectiveness)
  反向  闸门缺失 → 整链退化到接入前行为（fail-open）

双情景（关键：账户状态会改变闸门结果）
  A 真实账户（持仓 47.8% > 分歧段上限 40%）→ **全部剔除、禁止加仓**并给出缺口
  B 已减仓账户（持仓 ≤40%）              → 高度/只数闸门剔除 3 只、放行 2 只

隔离：全部读写都在临时目录，不触碰真实 reports/。
仅 monkeypatch 四处：闸门新鲜度校验（asof 判定已由 test_review_gate 单独覆盖）、行情取数、
真实持仓参考、停牌判定。
"""

import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, "/Users/williamli/Work/market-monitor")   # review_gate 在监控台仓库
import market_scheduler as ms  # noqa: E402

FAILS = []


def ck(cond, msg):
    print(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond:
        FAILS.append(msg)


def setup():
    """建临时工作目录 + 拷回测库副本 + 读真实闸门。绝不触碰真实 reports/。"""
    tmp = tempfile.mkdtemp(prefix="gate_loop_")
    real = ms.REPORTS_DIR
    shutil.copy(os.path.join(real, "backtest_database.json"),
                os.path.join(tmp, "backtest_database.json"))
    db = json.load(open(os.path.join(tmp, "backtest_database.json"), encoding="utf-8"))
    gp = os.path.join(real, "trade_gate.json")
    gate = json.load(open(gp, encoding="utf-8")) if os.path.exists(gp) else None
    return tmp, db, gate


def patch(tmp, gate):
    # 注意：BACKTEST_DB_FILE / REPORTS_DIR 都是**导入期算好的模块常量**，
    # 必须显式改写，不能只改 REPORTS_DIR 就以为生效（本次踩过）。
    ms.REPORTS_DIR = tmp
    ms.BACKTEST_DB_FILE = os.path.join(tmp, "backtest_database.json")
    ms.REVIEW_GATE_APPLIED = os.path.join(tmp, "trade_gate_applied.json")
    ms.load_review_gate = lambda *a, **k: gate
    ms.real_holding_reference = lambda *a, **k: ""
    ms.is_suspended = lambda *a, **k: False
    q = PRICES
    # 均价必须略低于现价，否则会被 ADR-0036 可成交性闸门整批拦掉（本次踩过）
    ms.get_stock_detailed_sina = lambda cn: {
        "current_price": q.get(cn, 10.0), "yesterday_close": q.get(cn, 10.0) * 0.99,
        "open": q.get(cn, 10.0) * 0.995, "high": q.get(cn, 10.0) * 1.01,
        "low": q.get(cn, 10.0) * 0.98,
        "volume": 5.0e7, "amount": q.get(cn, 10.0) * 5.0e7 * 0.995}
    ms.get_stock_limits = lambda *a, **k: (q.get(a[1], 10.0) * 1.1, q.get(a[1], 10.0) * 0.9)


def set_cash(tmp, db, cash):
    d = dict(db)
    d["available_cash"] = float(cash)
    with open(os.path.join(tmp, "backtest_database.json"), "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    return float(db.get("total_equity") or 500000.0), float(cash)


PRICES = {"蓝丰生化": 7.13, "泰诺麦博-U": 25.60, "东山精密": 18.20,
          "新华传媒": 12.34, "襄阳轴承": 15.00}


def build_cands(gate):
    """候选标的**从真实闸门重点池里取代码**，避免手写代码与闸门对不上
    （本次踩过：泰诺麦博-U 实际是 688806，手写成 688288 → 重点池匹配失败）。"""
    code = {f.get("name"): f.get("code") for f in (gate or {}).get("focus") or []}

    def cs(name, fallback):
        c = code.get(name) or fallback
        return c + (".SH" if c.startswith(("6", "9")) else ".SZ")

    return [
        {"name": "新华传媒",   "symbol": cs("新华传媒", "600825"),   "board_days": 7, "score": 95.0},  # 超高度→剔除
        {"name": "襄阳轴承",   "symbol": cs("襄阳轴承", "000678"),   "board_days": 4, "score": 88.0},  # 超高度→剔除
        {"name": "蓝丰生化",   "symbol": cs("蓝丰生化", "002513"),   "board_days": 1, "score": 80.0},  # 放行+命中重点池
        {"name": "泰诺麦博-U", "symbol": cs("泰诺麦博-U", "688806"), "board_days": 1, "score": 78.0},  # 放行+命中重点池
        {"name": "东山精密",   "symbol": "002384.SZ",                "board_days": 1, "score": 70.0},  # 不在重点池→只数上限剔除
    ]


XGB = {"temperature": 50.0}


def reset_globals():
    ms._ACTIVE_REVIEW_GATE = None
    ms._REVIEW_GATE_META = {}
    ms._REVIEW_GATE_DROPS = []


def main():
    tmp, db, gate = setup()
    patch(tmp, gate)
    CANDS = build_cands(gate)
    if not gate:
        print("⚠️ 未找到 trade_gate.json —— 请先在交易日盘后运行复盘模式生成闸门")
        print("   （本测试需要真实闸门文件；缺失时仅验证 fail-open 分支）")

    print("闸门文件: %s段 cap=%s%% 不碰%d板 max_picks=%d 重点池=%d 未执行卖出=%d"
          % ((gate or {}).get("stage"), (gate or {}).get("position_cap_pct"),
             (gate or {}).get("max_board_days"), (gate or {}).get("max_picks"),
             len((gate or {}).get("focus") or []), len((gate or {}).get("pending_sell") or [])))

    # ---------------- 情景 A：真实账户状态（持仓 47.8% > 40%）----------------
    print("\n【情景A · 真实账户】持仓 47.8%% > 分歧段上限 40%% → 应全拦")
    set_cash(tmp, db, db.get("available_cash", 257672.88))
    reset_globals()
    a_kept = ms._apply_review_gate("13:40", CANDS, XGB, ms.load_backtest_db())
    meta = ms._REVIEW_GATE_META
    print("  仓位 %.1f%% / 额度 %s / 放行 %d / 剔除 %d"
          % (meta.get("position_now_pct") or 0, meta.get("allowance"),
             len(a_kept), len(ms._REVIEW_GATE_DROPS)))
    ck(a_kept == [], "持仓超上限 → 买入候选全部剔除（禁止加仓）")
    ck(any("需先减仓" in d["reason"] for d in ms._REVIEW_GATE_DROPS),
       "剔除原因明示「需先减仓」与额度缺口")
    fc_a = ms.build_ops_flashcard("13:40", a_kept, 50.0, None)
    ck("复盘闸门" in fc_a, "闪卡在无推票时也说明闸门（不是空泛的「无标的」）")
    ck("需先减仓" in fc_a, "闪卡直接给出「需先减仓」的动作指引")

    # ---------------- 情景 B：已减仓账户（持仓 ≤40%）----------------
    print("\n【情景B · 已减仓】持仓降到 ≤40%% → 高度/只数闸门独立生效")
    eq, _ = set_cash(tmp, db, float(db.get("total_equity") or 500000.0) * 0.65)  # 持仓 35%
    reset_globals()
    kept = ms._apply_review_gate("13:40", CANDS, XGB, ms.load_backtest_db())
    drops = list(ms._REVIEW_GATE_DROPS)
    names = [c["name"] for c in kept]
    print("  账户: 权益 %.0f / 可用 %.0f → 持仓 %.1f%%"
          % (eq, eq * 0.65, (eq - eq * 0.65) / eq * 100))
    print("  放行:", names)
    for d in drops:
        print("     ✕ %s ← %s" % (d["name"], d["reason"][:62]))
    ck(names == ["蓝丰生化", "泰诺麦博-U"],
       "高度闸门剔除 7板/4板 + 只数上限剔除第 3 只（实得 %s）" % names)
    ck(len(drops) == 3, "剔除 3 只并逐条留痕（实得 %d）" % len(drops))
    ck(all(c.get("_review_focus") for c in kept), "放行的 2 只均命中复盘重点池（已打标）")

    print("\n【第2跳】候选 → 待确认买入单（操作台数据源）")
    intents = ms.get_buy_intents("13:40", kept, 50.0, None)
    print("  intents:", [(i["name"], i.get("buy_lots")) for i in intents])
    ck(len(intents) == 2, "确认单只含闸门放行的 2 只")
    p = ms.write_pending_buy_confirm("13:40", intents) if intents else None
    pj = json.load(open(p, encoding="utf-8")) if p else {"items": []}
    print("  确认单 items:", [x["name"] for x in pj["items"]])
    ck(len(pj["items"]) == 2, "确认单条目数 == 闸门放行数")
    ck("新华传媒" not in json.dumps(pj, ensure_ascii=False)
       and "东山精密" not in json.dumps(pj, ensure_ascii=False),
       "被闸门剔除的票结构上不可能出现在确认单（无法被确认买入）")

    print("\n【第3跳】候选 → 弹窗闪卡")
    fc = ms.build_ops_flashcard("13:40", kept, 50.0, None)
    print("  " + fc.replace("\n", "\n  ").rstrip())
    ck("🔒 复盘闸门" in fc, "闪卡含闸门摘要行")
    ck("已剔除 3 只" in fc, "闪卡明示剔除数量与名单")
    ck("昨日卖出未执行" in fc, "闪卡含未执行卖出提醒")
    print("\n【第4跳】候选 → 订单计划")
    op = ms.build_order_plan("13:40", kept, "40%-50%", 50.0, None)
    rg = op["review_gate"]
    print("  review_gate: active=%s stage=%s asof=%s kept=%s drops=%d"
          % (rg["active"], rg["stage"], rg["asof"], rg["kept"], len(rg["drops"])))
    ck(rg["active"] and rg["stage"] == "分歧" and len(rg["drops"]) == 3, "订单计划带完整闸门留痕")
    ck(any("复盘闸门" in (a.get("note") or "") for a in op["actions"] if a["type"] == "INFO"),
       "actions 含闸门 INFO（报告/闪卡口径一致）")
    ck(len(op["buy_candidates"]) == 2, "order_plan.buy_candidates 只剩放行的 2 只")

    print("\n【第5跳】执行留痕 → 次日复盘效能度量")
    ap = json.load(open(ms.REVIEW_GATE_APPLIED, encoding="utf-8"))
    print("  留痕 %s asof=%s stages=%s" % (ap["date"], ap["asof"], [s["stage"] for s in ap["stages"]]))
    ck(bool(ap["stages"]) and len(ap["stages"][0]["drops"]) == 3, "调度器留痕已落盘")
    import review_gate as rgmod
    from pathlib import Path
    rgmod.APPLIED_FILE = Path(ms.REVIEW_GATE_APPLIED)
    rgmod.EFFECT_FILE = Path(tmp) / "gate_effectiveness.json"   # 别写到真实 reports/
    eff = rgmod.effectiveness(quotes={
        "600825": {"name": "新华传媒", "pct": -3.2}, "000678": {"name": "襄阳轴承", "pct": -1.8},
        "002384": {"name": "东山精密", "pct": -2.4},
        "002513": {"name": "蓝丰生化", "pct": 0.6}, "688806": {"name": "泰诺麦博-U", "pct": 1.1}})
    print("  效能: %s" % eff["verdict"])
    print("  剔除组 %s%% / 放行组 %s%% / 价差 %s" % (eff["dropped_avg_pct"], eff["kept_avg_pct"], eff["edge"]))
    ck(eff["dropped_n"] == 3 and eff["kept_n"] == 2, "效能度量正确分离剔除组/放行组")
    ck("有效" in eff["verdict"], "剔除组为负 → 判「闸门有效」")

    # ---------------- 反向：fail-open ----------------
    print("\n【反向验证】闸门缺失 → 整链退化到接入前行为")
    ms.load_review_gate = lambda *a, **k: None
    reset_globals()
    k2 = ms._apply_review_gate("13:40", CANDS, XGB, ms.load_backtest_db())
    fc2 = ms.build_ops_flashcard("13:40", k2, 50.0, None)
    ck(len(k2) == len(CANDS), "无闸门 → %d 只全部放行" % len(CANDS))
    ck("复盘闸门" not in fc2, "无闸门 → 闪卡不出现闸门行（不产生误导性提示）")

    print("\n结果: " + ("闭环全绿 ✅" if not FAILS else "断点 %d 处 ❌ %s" % (len(FAILS), FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())

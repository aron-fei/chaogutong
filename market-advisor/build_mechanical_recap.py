#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
机械版盘后复盘 recap_<date>.md 生成器（2026-11-27 新增，ADR：无 AI 兜底）
====================================================================
背景：recap_*.md 原本由 WorkBuddy 应用内 15:30 automation（agent 分析）撰写。
      应用离线 = recap 断档（11-20/23/24 三天缺失即此因）。本脚本挂进
      run_slot.sh「盘后复盘」槽尾部（闸门钩子之后），纯文件拼装、零网络、
      零 AI，launchd 自跑，保证每天都有一份 recap 骨架。

口径铁律（SKILL.md Pitfalls 2026-11-26）：
  涨跌停家数一律用 akshare 股池实数口径 —— 本脚本直接取闸门 stage_signals
  （zt_count/dt_count 口径），不引用 breadth 估算，不自算。

优先级：recap_<date>.md 已存在（AI 版）→ 跳过不覆盖；--force 强制覆盖。
用法：
  build_mechanical_recap.py                     # 生成今日
  build_mechanical_recap.py --date 2026-11-27   # 指定日期
  build_mechanical_recap.py --force             # 覆盖已有
  build_mechanical_recap.py --selftest          # 零副作用自检（不写盘）
退出码：0 成功或正常跳过 / 1 数据缺失硬失败
"""
import datetime as dt
import glob
import json
import os
import sys

REPORTS_DIR = os.environ.get("REPORTS_DIR", os.path.expanduser("~/Work/reports"))
GATE = os.path.join(REPORTS_DIR, "trade_gate.json")
APPLIED = os.path.join(REPORTS_DIR, "trade_gate_applied.json")
BACKTEST = os.path.join(REPORTS_DIR, "backtest_database.json")


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), None
    except FileNotFoundError:
        return None, "文件不存在"
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def _fmt(v, suffix=""):
    if v is None:
        return "—"
    try:
        f = float(v)
        return ("%g" % f) + suffix
    except (TypeError, ValueError):
        return str(v) + suffix


def sec_market(gate, date_str):
    sig = gate.get("stage_signals") or {}
    lines = [
        "## ① 市场速览（口径：akshare 股池实数，与闸门 stage_signals 同源）",
        "",
        "| 涨停 | 跌停 | 最高连板 | 涨跌比 | 炸板率 | 梯队断层 |",
        "|---|---|---|---|---|---|",
        "| %s | %s | %s | %s | %s | %s |" % (
            _fmt(sig.get("zt"), " 家"), _fmt(sig.get("dt"), " 家"),
            _fmt(sig.get("max_board"), " 板"), _fmt(sig.get("ratio")),
            _fmt(sig.get("zb_rate"), "%"), _fmt(sig.get("gap"))),
        "",
        "**复盘段位判定**：%s%s ｜ 总仓上限 %s ｜ 单票 ≤%s ｜ 最多 %s 只 ｜ 不碰 %s 板以上 ｜ %s" % (
            gate.get("stage_icon", ""), gate.get("stage", "—"),
            _fmt(gate.get("position_cap_pct"), "%"), _fmt(gate.get("single_pos_cap_pct"), "%"),
            _fmt(gate.get("max_picks")), _fmt(gate.get("max_board_days")),
            "硬阻断" if gate.get("hard_block") else "警告档"),
        "",
        (gate.get("reason") or "").strip(),
    ]
    if gate.get("escape_downgraded"):
        lines.append("")
        lines.append("> ⚠️ 逃生阀已降级为试探档（详见闸门 notes）")
    return lines


def sec_applied(applied, date_str):
    if not applied or applied.get("date") != date_str:
        return ["## ② 今日闸门执行留痕", "",
                "今日无留痕（%s）。" % (applied or {}).get("date", "文件缺失"), ""]
    lines = ["## ② 今日闸门执行留痕（trade_gate_applied.json）", ""]
    for st in applied.get("stages") or []:
        kept = [c.get("name") for c in st.get("kept") or []]
        drops = [d.get("name") for d in st.get("drops") or []]
        meta = st.get("meta") or {}
        notes = meta.get("notes") or []
        lines.append("- **%s**（%s）：放行 %d 只 %s %s" % (
            st.get("stage"), st.get("at", "—"), len(kept),
            "／".join(kept) if kept else "（空）",
            "；剔除 %s" % "、".join(drops) if drops else ""))
        for n in (notes if isinstance(notes, list) else [notes]):
            if n:
                lines.append("  - %s" % n)
    lines.append("")
    return lines


def sec_account(bt, date_str):
    if not bt:
        return ["## ③ 账户快照", "", "backtest_database.json 缺失。", ""]
    ini = bt.get("initial_capital") or 0.0
    eq = bt.get("total_equity") or 0.0
    cash = bt.get("available_cash") or 0.0
    pnl = eq - ini
    lines = [
        "## ③ 账户快照（backtest_database.json，50w 模拟盘）", "",
        "- 总权益 **%s**（初始 %s，累计 %s）｜ 可用现金 %s" % (
            _fmt(eq, " 元"), _fmt(ini, " 元"),
            ("%+.0f 元（%+.2f%%）" % (pnl, pnl / ini * 100)) if ini else "—",
            _fmt(cash, " 元")),
    ]
    pos = bt.get("active_positions") or []
    if pos:
        lines += ["", "| 标的 | 代码 | 成本 | 股数 | 现价 | 盈亏 |", "|---|---|---|---|---|---|"]
        for p in pos:
            cur = p.get("current_price", p.get("last_price"))
            pnlv = p.get("profit_loss_amount", p.get("pnl"))
            lines.append("| %s | %s | %s | %s | %s | %s |" % (
                p.get("name", "—"), p.get("symbol", "—"), _fmt(p.get("buy_price")),
                _fmt(p.get("shares")), _fmt(cur), _fmt(pnlv)))
    else:
        lines.append("- 当前持仓：空仓")
    today_trades = [t for t in bt.get("completed_trades") or []
                    if t.get("sell_date") == date_str]
    if today_trades:
        lines += ["", "**今日平仓 %d 笔**：" % len(today_trades)]
        for t in today_trades:
            lines.append("- %s（%s）：%s → %s，盈亏 **%s 元（%s）**，持股 %s 天" % (
                t.get("name"), t.get("symbol"), _fmt(t.get("buy_price")),
                _fmt(t.get("sell_price")), _fmt(t.get("profit_loss_amount")),
                _fmt(t.get("profit_loss_pct"), "%"), _fmt(t.get("hold_days"))))
    lines.append("")
    return lines


def sec_tomorrow(gate):
    lines = [
        "## ④ 明日闸门与焦点池（trade_gate.json，asof %s）" % gate.get("asof", "—"), "",
        "- deny_styles：%s ｜ hard_block：%s" % (
            "、".join(gate.get("deny_styles") or []) or "无",
            "是" if gate.get("hard_block") else "否"),
    ]
    for a in gate.get("avoid") or []:
        lines.append("- ⛔ %s" % a)
    focus = gate.get("focus") or []
    if focus:
        lines += ["", "| 标的 | 类型 | 连板 | 涨幅 | 入选理由 |", "|---|---|---|---|---|"]
        for f in focus:
            lines.append("| %s | %s | %s板 | %s | %s |" % (
                f.get("name"), f.get("kind_label"), _fmt(f.get("board_days")),
                _fmt(f.get("pct"), "%"), (f.get("why") or "—").replace("|", "｜")))
    for e in gate.get("focus_excluded") or []:
        lines.append("")
        lines.append("> 剔除 %s：%s" % (e.get("name"), e.get("excluded_reason")))
    if gate.get("checklist"):
        lines += ["", "**明日开盘清单**："]
        for c in gate.get("checklist"):
            lines.append("- [ ] %s" % c)
    lines.append("")
    return lines


def sec_files(date_str):
    code = date_str.replace("-", "")
    lines = ["## ⑤ 今日文件索引", ""]
    pats = [
        ("时段报告", "pro_report_%s_*.txt" % code),
        ("操作确认", "pending_*_confirm_%s.json" % date_str),
        ("订单计划", "order_plan_*.json"),
        ("闸门状态", "gate_status.json"),
    ]
    for label, pat in pats:
        hits = sorted(glob.glob(os.path.join(REPORTS_DIR, pat)))
        if pat.startswith("order_plan"):  # 只列今天改过的
            hits = [h for h in hits
                    if os.path.getmtime(h) > dt.datetime.now().replace(
                        hour=0, minute=0).timestamp()]
        lines.append("- %s：%s" % (label, "、".join(os.path.basename(h) for h in hits)
                                   if hits else "无"))
    return lines


def build(date_str):
    gate, err = _load(GATE)
    if gate:
        if gate.get("source_review_date") != date_str:
            print("[WARN] 闸门 source_review_date=%s ≠ %s（明日闸门可能未生成，段落用现值）"
                  % (gate.get("source_review_date"), date_str))
    else:
        print("[WARN] trade_gate.json 读取失败：%s（段落④将缺失）" % err)
    applied, _ = _load(APPLIED)
    bt, _ = _load(BACKTEST)
    out = ["# 盘后复盘（机械版）· %s" % date_str, ""]
    out.append("> ⚙️ **本文件由 launchd 15:30 盘后复盘槽自动拼装**（build_mechanical_recap.py），"
               "零 AI 分析，全部数据来自落盘文件、口径与闸门同源。"
               "若当日存在 agent 撰写的 AI 版 recap，本文件不会生成/会被 AI 版覆盖优先。")
    out.append("")
    if gate:
        out += sec_market(gate, date_str)
    out += sec_applied(applied, date_str)
    out += sec_account(bt, date_str)
    if gate:
        out += sec_tomorrow(gate)
    out += sec_files(date_str)
    out.append("")
    out.append("— 生成时间 %s ｜ 数据源：trade_gate.json / trade_gate_applied.json / "
               "backtest_database.json" % dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    return "\n".join(out) + "\n"


def selftest(date_str):
    print("== build_mechanical_recap 自检 ==")
    ok = True
    for p in (GATE, APPLIED, BACKTEST):
        state = "存在" if os.path.exists(p) else "缺失（有降级处理）"
        print("  [INFO] %s：%s" % (os.path.basename(p), state))
    for m in ("requests",):
        try:
            __import__(m)
        except ImportError:
            pass  # 本脚本不联网，无需外部依赖
    txt = build(date_str)
    print("  [PASS] 拼装成功：%d 行（预览前 3 行）" % len(txt.splitlines()))
    for ln in txt.splitlines()[:3]:
        print("    | " + ln)
    print("  [INFO] selftest 不写盘（正式生成由 run_slot.sh 盘后复盘槽调用）")
    return 0 if ok else 1


def main():
    args = sys.argv[1:]
    date_str = dt.date.today().strftime("%Y-%m-%d")
    if "--date" in args:
        date_str = args[args.index("--date") + 1]
    if "--selftest" in args:
        sys.exit(selftest(date_str))
    force = "--force" in args
    target = os.path.join(REPORTS_DIR, "recap_%s.md" % date_str)
    if os.path.exists(target) and not force:
        print("[SKIP] %s 已存在（AI 版优先），不加 --force 不覆盖" % target)
        sys.exit(0)
    if not os.path.exists(GATE):
        print("[ERROR] 缺 trade_gate.json，先跑闸门再生成复盘")
        sys.exit(1)
    os.makedirs(REPORTS_DIR, exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        f.write(build(date_str))
    print("[OK] 机械版复盘已生成：%s" % target)


if __name__ == "__main__":
    main()

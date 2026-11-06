#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0-5 修复回归测试（隔离沙箱，不写真实 reports/）。

验证 4 件事：
  T1  dict 快照 → _SnapshotAdapter 后，六引擎不再 AttributeError（errors 为空）
  T2  auto_build 走对象路径 → 产出**完整**闸门（signals 有值 / focus 非空 / stage 非默认）
  T3  降级守卫：引擎全挂时 auto_build 拒绝写盘（宁可不写）
  T4  真实 reports/ 三个文件 mtime 全程不变（无污染）
"""
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
REPORTS_DIR = Path(__file__).resolve().parents[2] / "reports"
sys.path.insert(0, str(BASE_DIR))
import review_gate as rg

REAL_GATE = REPORTS_DIR / "trade_gate.json"
REAL_EFFECT = REPORTS_DIR / "gate_effectiveness.json"
REAL_APPLIED = REPORTS_DIR / "trade_gate_applied.json"
WATCH = [REAL_GATE, REAL_EFFECT, REAL_APPLIED]


def mtimes():
    return {str(p): (p.stat().st_mtime if p.exists() else None) for p in WATCH}


def main():
    ok = True
    before = mtimes()

    # ---------- 沙箱：把落盘目标全部改写到临时目录 ----------
    tmpd = Path(tempfile.mkdtemp(prefix="gate_p05_"))
    bak = (rg.GATE_FILE, rg.APPLIED_FILE, rg.EFFECT_FILE, rg.HISTORY_DIR)
    rg.GATE_FILE = tmpd / "trade_gate.json"
    rg.APPLIED_FILE = tmpd / "trade_gate_applied.json"
    rg.EFFECT_FILE = tmpd / "gate_effectiveness.json"
    rg.HISTORY_DIR = tmpd / "history"

    try:
        # ===== T1 适配器：dict → 六引擎不再报错 =====
        print("[T1] dict 快照适配为对象")
        import urllib.request
        import review_deep
        raw = urllib.request.urlopen("http://127.0.0.1:8765/api/dashboard", timeout=25).read()
        d = json.loads(raw)
        snap = rg._fetch_market_snapshot(as_object=True)
        out = review_deep.review_deep(snap, None, do_archive=False)
        errs = out.get("errors") or []
        cyc = out.get("cycle") or {}
        sig = cyc.get("signals") or {}
        print("     errors =", json.dumps(errs, ensure_ascii=False))
        print("     stage =", cyc.get("stage"), "| signals =", json.dumps(sig, ensure_ascii=False))
        if errs:
            print("     ❌ 引擎仍报错"); ok = False
        elif not sig.get("zt"):
            print("     ❌ signals 仍为空"); ok = False
        else:
            print("     ✅ 引擎无报错且信号完整")

        # ===== T2 auto_build 产出完整闸门 =====
        print("\n[T2] auto_build 产出完整闸门（沙箱落盘）")
        r = rg.auto_build(log=lambda m: print("     " + str(m)))
        g = json.loads((tmpd / "trade_gate.json").read_text(encoding="utf-8"))
        gs = g.get("stage_signals") or {}
        print("     ok=%s built=%s asof=%s" % (r.get("ok"), r.get("built"), r.get("asof")))
        print("     stage=%s cap=%s single=%s picks=%s focus=%d" % (
            g.get("stage"), g.get("position_cap_pct"), g.get("single_pos_cap_pct"),
            g.get("max_picks"), len(g.get("focus") or [])))
        print("     signals =", json.dumps(gs, ensure_ascii=False))
        if not r.get("ok") or not gs.get("zt") or not (g.get("focus") or []):
            print("     ❌ 闸门不完整"); ok = False
        else:
            print("     ✅ 闸门完整（signals 有值 + focus 非空）")

        # ===== T3 降级守卫：引擎全挂 → 拒绝写盘 =====
        # 注意：必须清掉沙箱里的目标闸门，否则幂等分支会先 return，
        # 根本走不到降级守卫（这是测试设计问题，不是守卫失效）。
        print("\n[T3] 降级守卫（模拟引擎全挂）")
        (tmpd / "trade_gate.json").unlink(missing_ok=True)
        orig_rd = review_deep.review_deep

        def broken_review_deep(md, adv=None, do_archive=True):
            # 模拟旧缺陷：dict 输入导致四引擎 AttributeError 被吞
            o = orig_rd(md, adv, do_archive=do_archive)
            o["errors"] = ["emotion_cycle: AttributeError: 'dict' object has no attribute 'zt_pool'"]
            o["cycle"] = {"stage": None, "signals": {}}
            o["wind"] = []
            return o

        review_deep.review_deep = broken_review_deep
        try:
            r2 = rg.auto_build(log=lambda m: print("     " + str(m)))
        finally:
            review_deep.review_deep = orig_rd
        wrote = (tmpd / "trade_gate.json").exists()
        print("     ok=%s reason=%s" % (r2.get("ok"), r2.get("reason")))
        print("     是否写盘 =", wrote)
        if r2.get("ok") is not False or wrote:
            print("     ❌ 守卫未生效（不应写盘）"); ok = False
        else:
            print("     ✅ 守卫生效：拒绝写盘")

    finally:
        rg.GATE_FILE, rg.APPLIED_FILE, rg.EFFECT_FILE, rg.HISTORY_DIR = bak
        shutil.rmtree(tmpd, ignore_errors=True)

    # ---------- T4 真实 reports 无污染 ----------
    print("\n[T4] 真实 reports/ 无污染")
    after = mtimes()
    for p in WATCH:
        s = "不变" if before.get(str(p)) == after.get(str(p)) else "❌ 被改动"
        print("     %s : %s" % (Path(p).name, s))
        if before.get(str(p)) != after.get(str(p)):
            ok = False

    print("\n结果: %s" % ("全部通过 ✅" if ok else "存在失败 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""2026-11-26 三项遗留修复的回归测试（隔离沙箱，不写真实 reports/）。

背景（均为 11-26 盘后复盘闸门任务实测暴露）：
  F1  zt_pool[:60] 展示截断版被当成统计口径 → 闸门 zt 少算 5 家、zb_rate 虚高
      （60/32.6 vs 真值 65/30.85）；闸门路径（HTTP 快照）与工作台路径（进程内
      MarketData）**同源同刻可判反**。
  F2  focus 重点池未按 max_board_days 预过滤 → 5 板空间龙头被标成「重点」，
      占掉调度器 Top-N 名额后再被风格闸门剔除（11-05/11-19/11-20/11-26 四次实证）。
  F3  review_modes 归档断链（11-18 起 6 个交易日）→ 只有工作台 /api/review_deep
      写归档，09:0x 竞价时 zt_pool 为空 → archive() 直接返回 → 当日不落档。
  F4  （连带发现）快照不完整时 ratio=None 会**静默翻段**：mood_engine 分歧规则带
      `ratio is not None` 前置条件 → 分歧(40%) 翻成 反包(30%)。

验证 8 件事：
  T1  _SnapshotAdapter 提升全量池：zt_pool 取 zt_pool_all（65），展示版另存
  T2  _snapshot_complete：breadth 缺失 → False；有 up/down → True
  T3  build_gate 残缺信号（ratio=None）→ 拒绝写盘（沿用既有闸门）
  T4  build_gate 写明日闸门前，自动保住「今日闸门」到 trade_gate_current.json（P0-①）
  T5  build_gate 写今日闸门时不动 current 副本
  T6  focus 按 max_board_days 预过滤：超限项进 focus_excluded 且带原因
  T7  auto_build 快照不完整 → 拒绝写盘（不产出错段闸门）
  T8  真实 reports/ 三个文件 mtime 全程不变（无污染）
"""
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
REPORTS_DIR = Path(__file__).resolve().parents[2] / "reports"
sys.path.insert(0, str(BASE_DIR))
import review_gate as rg

REAL_GATE = REPORTS_DIR / "trade_gate.json"
REAL_EFFECT = REPORTS_DIR / "gate_effectiveness.json"
REAL_APPLIED = REPORTS_DIR / "trade_gate_applied.json"
REAL_CURRENT = REPORTS_DIR / "trade_gate_current.json"
WATCH = [REAL_GATE, REAL_EFFECT, REAL_APPLIED, REAL_CURRENT]

OK = True


def chk(cond, msg):
    global OK
    print(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond:
        OK = False


def mtimes():
    return {str(p): (p.stat().st_mtime if p.exists() else None) for p in WATCH}


def fake_review(stage="分歧", zt=65, dt=5, ratio=0.5, zb_rate=30.9,
                max_board=5, wind=None, loss_level=2):
    """构造一份 review_deep 输出（字段对齐 build_gate 的读取路径）。"""
    if wind is None:
        wind = [
            {"code": "603960", "name": "克来机电", "kind": "space",
             "kind_label": "空间龙头", "lianban": 5, "pct": 10.0, "why": "5 板空间高度"},
            {"code": "002565", "name": "顺灏股份", "kind": "pioneer",
             "kind_label": "率先封板先锋", "lianban": 1, "one_word": True, "pct": 10.05},
            {"code": "600467", "name": "好当家", "kind": "relay",
             "kind_label": "换手接力", "lianban": 2, "pct": 10.0},
            {"code": "600722", "name": "金牛化工", "kind": "relay",
             "kind_label": "换手接力", "lianban": 3, "pct": 10.04},
        ]
    return {
        "time": "2026-11-26 15:00:00",
        "cycle": {"stage": stage, "law": "测试心法",
                  "signals": {"zt": zt, "dt": dt, "ratio": ratio, "zb_rate": zb_rate,
                              "max_board": max_board, "gap": 0}},
        "loss": {"level": loss_level, "verdict": "测试"},
        "discipline": {"violations": []},
        "wind": wind,
        "main": {"industries": []},
        "plan": {"avoid": [], "checklist": []},
    }


def main():
    before = mtimes()
    tmpd = Path(tempfile.mkdtemp(prefix="gate_fullpool_"))
    bak = (rg.GATE_FILE, rg.APPLIED_FILE, rg.EFFECT_FILE, rg.HISTORY_DIR, rg.REPORTS)
    rg.GATE_FILE = tmpd / "trade_gate.json"
    rg.APPLIED_FILE = tmpd / "trade_gate_applied.json"
    rg.EFFECT_FILE = tmpd / "gate_effectiveness.json"
    rg.HISTORY_DIR = tmpd / "history"
    rg.REPORTS = tmpd
    cur_file = tmpd / "trade_gate_current.json"

    try:
        # ===== T1 适配器：全量池提升 =====
        print("[T1] _SnapshotAdapter 提升全量涨停/炸板池")
        d = {"zt_pool": list(range(60)), "zt_pool_all": list(range(65)),
             "zb_pool": list(range(29)), "zb_pool_all": list(range(45)),
             "dt_pool": [1, 2, 3, 4, 5], "breadth": {"up": 1683, "down": 3375}}
        a = rg._SnapshotAdapter(d)
        chk(len(a.zt_pool) == 65, "zt_pool 取全量 65（实得 %d）" % len(a.zt_pool))
        chk(len(a.zb_pool) == 45, "zb_pool 取全量 45（实得 %d）" % len(a.zb_pool))
        chk(len(getattr(a, "zt_pool_display", [])) == 60, "展示版另存 zt_pool_display=60")
        # 无全量字段时保持原样（向后兼容）
        a2 = rg._SnapshotAdapter({"zt_pool": [1, 2, 3], "breadth": {}})
        chk(len(a2.zt_pool) == 3, "无 zt_pool_all 时原样保留（实得 %d）" % len(a2.zt_pool))

        # ===== T2 快照完整性 =====
        print("[T2] _snapshot_complete")
        chk(rg._snapshot_complete(a) is True, "breadth 齐全 → 完整")
        a3 = rg._SnapshotAdapter({"zt_pool": [1], "breadth": {}})
        chk(rg._snapshot_complete(a3) is False, "breadth 为空 → 不完整（ratio 会是 None）")
        a4 = rg._SnapshotAdapter({"zt_pool": [], "breadth": {"up": 1, "down": 2}})
        chk(rg._snapshot_complete(a4) is False, "涨停池为空 → 不完整")

        # ===== T3 残缺信号拒绝写盘 =====
        print("[T3] build_gate 残缺信号拒绝写盘")
        g_ok = rg.build_gate(fake_review(), mode="enforce",
                             asof=(date.today() + timedelta(days=1)).isoformat(), write=True)
        chk(rg.GATE_FILE.exists(), "信号完整 → 正常写盘")
        good = json.loads(rg.GATE_FILE.read_text(encoding="utf-8"))
        chk(good.get("asof") == (date.today() + timedelta(days=1)).isoformat(), "asof 写入正确")
        mtime_before = rg.GATE_FILE.stat().st_mtime
        time.sleep(1.1)
        rg.build_gate(fake_review(ratio=None), mode="enforce",
                      asof=(date.today() + timedelta(days=2)).isoformat(), write=True)
        chk(rg.GATE_FILE.stat().st_mtime == mtime_before,
            "ratio=None → 拒绝写盘（沿用既有闸门，未被覆盖）")

        # ===== T4 写明日闸门前保住今日闸门（P0-①） =====
        print("[T4] build_gate 自动保住今日闸门")
        today_iso = date.today().isoformat()
        tomorrow_iso = (date.today() + timedelta(days=1)).isoformat()
        today_gate = fake_review()
        today_gate["cycle"]["stage"] = "退潮"
        rg.build_gate(today_gate, mode="enforce", asof=today_iso, write=True)
        chk(json.loads(rg.GATE_FILE.read_text(encoding="utf-8"))["asof"] == today_iso,
            "先写今日闸门到主文件")
        if cur_file.exists():
            cur_file.unlink()
        rg.build_gate(fake_review(), mode="enforce", asof=tomorrow_iso, write=True)
        chk(cur_file.exists(), "写明日闸门后 current 副本存在")
        if cur_file.exists():
            cur = json.loads(cur_file.read_text(encoding="utf-8"))
            chk(cur["asof"] == today_iso,
                "current 副本保住的是**今日**闸门 asof=%s（实得 %s）" % (today_iso, cur["asof"]))
        chk(json.loads(rg.GATE_FILE.read_text(encoding="utf-8"))["asof"] == tomorrow_iso,
            "主文件承载明日闸门")

        # ===== T5 写今日闸门时不动 current =====
        print("[T5] 写今日闸门时不误改 current")
        if cur_file.exists():
            cur_mtime = cur_file.stat().st_mtime
            time.sleep(1.1)
            rg.build_gate(fake_review(stage="冰点"), mode="enforce", asof=today_iso, write=True)
            chk(cur_file.stat().st_mtime == cur_mtime, "current 副本未被今日闸门覆盖")

        # ===== T6 focus 按 max_board_days 预过滤 =====
        print("[T6] focus 与 max_board_days 对齐")
        # 分歧段 max_board_days=3 → 5 板空间龙头必须被移出 focus
        g = rg.build_gate(fake_review(stage="分歧"), mode="enforce",
                          asof=tomorrow_iso, write=False)
        names = [f["name"] for f in g["focus"]]
        chk("克来机电" not in names, "5 板空间龙头不在 focus（实得 %s）" % names)
        exc = g.get("focus_excluded") or []
        chk(any(e["name"] == "克来机电" for e in exc), "5 板空间龙头进 focus_excluded")
        chk(all("板" in (e.get("excluded_reason") or "") for e in exc),
            "focus_excluded 带排除原因")
        chk(len(g["focus"]) == 3, "板数达标项保留在 focus（实得 %d）" % len(g["focus"]))
        # 退潮段 max_board_days=2 → 3 板金牛化工移出；2 板好当家**恰好达标**应保留
        g2 = rg.build_gate(fake_review(stage="退潮"), mode="enforce",
                           asof=tomorrow_iso, write=False)
        n2 = [f["name"] for f in g2["focus"]]
        chk("金牛化工" not in n2, "退潮段（≤2 板）3 板金牛化工被移出 focus（实得 %s）" % n2)
        chk("好当家" in n2, "退潮段 2 板好当家恰好达标 → 保留（实得 %s）" % n2)
        # 发酵段 max_board_days=5 → 5 板空间龙头应当保留
        g3 = rg.build_gate(fake_review(stage="发酵"), mode="enforce",
                           asof=tomorrow_iso, write=False)
        n3 = [f["name"] for f in g3["focus"]]
        chk("克来机电" in n3, "发酵段（≤5 板）5 板空间龙头保留（实得 %s）" % n3)

        # ===== T7 auto_build 快照不完整 → 拒绝写盘 =====
        print("[T7] auto_build 快照不完整 → 拒绝写盘")
        orig_fetch = rg._fetch_market_snapshot
        orig_complete = rg._fetch_complete_snapshot
        rg._fetch_complete_snapshot = lambda timeout=20, retries=4, wait=8, log=print: None
        try:
            res = rg.auto_build(force=True, log=lambda *a, **k: None)
            chk(res.get("ok") is False, "快照不完整 → ok=False")
            chk("快照" in (res.get("reason") or ""), "拒绝原因写明快照不完整")
        finally:
            rg._fetch_complete_snapshot = orig_complete
            rg._fetch_market_snapshot = orig_fetch

        # ===== T9 引擎降级时不得写归档 =====
        print("[T9] 引擎降级 → 不写归档（不得覆盖当天好归档）")
        import review_deep as rd
        arch_dir = rd.ARCH_DIR
        rd.ARCH_DIR = tmpd / "arch"
        arch_file = rd.ARCH_DIR / ("%s.json" % date.today().isoformat())
        rd.ARCH_DIR.mkdir(parents=True, exist_ok=True)
        arch_file.write_text(json.dumps({"date": date.today().isoformat(),
                                         "cycle": {"zt": 999}, "stage": "好归档"},
                                        ensure_ascii=False), encoding="utf-8")
        arch_mtime = arch_file.stat().st_mtime
        time.sleep(1.1)
        orig_rd = rd.review_deep

        def broken_rd(md, adv=None, do_archive=True):
            o = orig_rd(md, adv, do_archive=do_archive)
            o["errors"] = ["emotion_cycle: AttributeError: 'dict' object has no attribute 'zt_pool'"]
            o["cycle"] = {"stage": None, "signals": {}}
            return o

        rd.review_deep = broken_rd
        try:
            res = rg.auto_build(force=True, log=lambda *a, **k: None)
            chk(res.get("ok") is False, "引擎降级 → ok=False")
        finally:
            rd.review_deep = orig_rd
            rd.ARCH_DIR = arch_dir
        chk(arch_file.exists() and arch_file.stat().st_mtime == arch_mtime,
            "引擎降级 → 归档未被覆盖（好归档仍在）")
        chk(json.loads(arch_file.read_text(encoding="utf-8")).get("stage") == "好归档",
            "归档内容仍是降级前的好归档")

        # ===== T10 --health 口径校验（假阳性修复） =====
        print("[T10] gate_health 口径校验")
        fake_snap = {"zt_count": 65, "dt_count": 5, "zb_count": 29,
                     "breadth": {"up": 1683, "down": 3375}}
        orig_snap = rg._fetch_market_snapshot
        rg._fetch_market_snapshot = lambda timeout=20, as_object=False, **kw: dict(fake_snap)
        try:
            good_gate = {"asof": "2099-01-02", "stage": "分歧", "mode": "enforce",
                         "source_review_date": date.today().isoformat(),
                         "stage_signals": {"zt": 65, "dt": 5, "ratio": 0.5,
                                           "zb_rate": 30.9, "max_board": 5, "gap": 0}}
            mm = rg._caliber_mismatch(good_gate)
            chk(mm is not None and mm.get("ok") is True,
                "口径一致 → ok（实得 %s）" % (mm or None))
            bad_gate = dict(good_gate)
            bad_gate["stage_signals"] = dict(good_gate["stage_signals"],
                                             zt=0, zb_rate=0.0)   # 竞价口径废闸门形状
            mm2 = rg._caliber_mismatch(bad_gate)
            chk(mm2 is not None and mm2.get("ok") is False,
                "竞价口径废闸门（zt=0）→ 报不符（实得 %s）" % (mm2 or None))
            # 闸门源日期不是今天 → 不对表（盘中闸门拿今日盘中快照比必然不符）
            old_gate = dict(good_gate, source_review_date="2020-01-01")
            mm3 = rg._caliber_mismatch(old_gate)
            chk(mm3 is None or mm3.get("ok") is True,
                "非今日复盘的闸门不做口径判定（不误报）")
        finally:
            rg._fetch_market_snapshot = orig_snap

        # ===== T8 真实 reports/ 未被污染 =====
        print("[T8] 真实 reports/ 无污染")
        after = mtimes()
        for p in WATCH:
            if before.get(str(p)) != after.get(str(p)):
                chk(False, "被改写：%s" % p)
        chk(before == after, "真实 reports/ 四个文件 mtime 全程不变")
    finally:
        (rg.GATE_FILE, rg.APPLIED_FILE, rg.EFFECT_FILE, rg.HISTORY_DIR, rg.REPORTS) = bak
        shutil.rmtree(tmpd, ignore_errors=True)

    print("\n结果: %s" % ("全部通过 ✅" if OK else "存在失败 ❌"))
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())

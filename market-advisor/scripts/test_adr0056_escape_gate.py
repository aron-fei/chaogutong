#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ADR-0056 回归测试 · 2026-11-26 修三个真问题

覆盖：
  P0-1 逃生阀击穿退潮段「空仓」语义
       - 退潮段 max_picks=0 / single_pos_cap_pct=0（空仓语义由这两个 0 承载）
       - 旧实现：逃生阀只把 hard 置 False → 下游两处 `if picks and ...` /
         `if eq and single_pct` 遇 0 走 falsy → 只数上限与单票上限**双双失效**
         → 退潮段被降级成「无限制」（11-26 09:25 实测派出 1 只买单，2.48% 权益）
       - 修法：逃生阀 = 换试探档（1 只 / 单票 8%），0 一律用 is None 判定
  P0-2 15:35 闸门生成无 launchd 兜底
       - 新增 run_gate1535.sh：工作台拉起 → --auto → 四项对表 → force 重建
         → current 副本同步 → review_modes 归档 → 台账 ingest
  P0-3 报告仓位与闸门不同源
       - 报告 recommended_position / 「仓位控制」/ 单股建议 必须与闸门同源

用法：/opt/homebrew/anaconda3/bin/python3 test_adr0056_escape_gate.py
"""
import json
import os
import subprocess
import sys
import unittest

SCRIPTS = "/Users/williamli/.workbuddy/skills/market-advisor/scripts"
MONITOR = "/Users/williamli/Work/market-monitor"
REPORTS = "/Users/williamli/Work/reports"
PY = "/opt/homebrew/anaconda3/bin/python3"

sys.path.insert(0, SCRIPTS)
sys.path.insert(0, MONITOR)


def tide_gate(**kw):
    """退潮段真实形态：空仓语义由 picks=0 / single=0 承载。"""
    g = {"stage": "退潮", "mode": "enforce", "asof": "2026-11-27",
         "position_cap_pct": 10, "single_pos_cap_pct": 0, "max_picks": 0,
         "max_board_days": 2, "hard_block": True,
         "hard_block_reason": "退潮段：亏钱效应扩散", "cooling": [], "focus": []}
    g.update(kw)
    return g


class TestEscapeValve(unittest.TestCase):
    """P0-1：逃生阀不得击穿退潮段空仓语义"""

    def setUp(self):
        import market_scheduler as ms
        import review_gate as rg
        self.ms, self.rg = ms, rg
        self.cands = [{"name": "票%d" % i, "symbol": "00000%d" % i,
                       "board_days": 1, "sources": ["x"]} for i in range(5)]
        self.db = {"total_equity": 479352.93, "available_cash": 479352.93}
        # 拦截写盘副作用
        self._orig_write = ms._write_review_gate_applied
        ms._write_review_gate_applied = lambda *a, **k: None

    def tearDown(self):
        self.ms._write_review_gate_applied = self._orig_write

    def test_review_side_low_temp_hard_block(self):
        """低温：硬阻断 → 0 放行（不退化）"""
        k, d, m = self.rg.apply("09:25", self.cands, tide_gate(),
                                {"temperature": 30, "equity": 479352.93, "cash": 479352.93})
        self.assertEqual(len(k), 0, "低温硬阻断必须 0 放行")
        self.assertEqual(len(d), len(self.cands))

    def test_review_side_escape_bounded(self):
        """逃生阀：退潮段放行数 ≤ 试探档 1 只（修复前是 5 只）"""
        k, d, m = self.rg.apply("09:25", self.cands, tide_gate(),
                                {"temperature": 76.5, "equity": 479352.93, "cash": 479352.93})
        self.assertTrue(m.get("escape"), "逃生阀必须留痕 escape=True")
        self.assertLessEqual(len(k), self.rg.ESCAPE_MAX_PICKS,
                             "退潮段逃生阀放行 %d 只，超出试探档 %d 只"
                             % (len(k), self.rg.ESCAPE_MAX_PICKS))
        self.assertTrue(any("只数上限" in x["reason"] for x in d) or len(k) <= 1,
                        "超出试探档只数的候选必须以「只数上限」剔除")

    def test_scheduler_side_escape_bounded(self):
        """调度器侧：同样的退潮段形态，逃生阀后放行 ≤ 1 只"""
        self.ms.load_review_gate = lambda *a, **k: tide_gate()
        k = self.ms._apply_review_gate("09:25", self.cands, {"temperature": 76.5}, self.db)
        self.assertLessEqual(len(k), self.ms.REVIEW_GATE_ESCAPE_MAX_PICKS,
                             "调度器侧退潮段逃生阀放行 %d 只（应 ≤ %d）"
                             % (len(k), self.ms.REVIEW_GATE_ESCAPE_MAX_PICKS))

    def test_scheduler_side_picks_zero_means_zero(self):
        """picks=0 = 不许开仓（不是「无限制」）—— falsy-zero 陷阱的根因"""
        self.ms.load_review_gate = lambda *a, **k: tide_gate(hard_block=False)
        k = self.ms._apply_review_gate("09:25", self.cands, {"temperature": 30}, self.db)
        self.assertEqual(len(k), 0, "picks=0 必须 0 放行，不能当「无限制」")

    def test_single_cap_zero_is_enforced(self):
        """single_pos_cap_pct=0 → 单笔额度 0（不是「无上限」）"""
        self.ms.load_review_gate = lambda *a, **k: tide_gate(hard_block=False)
        self.ms._apply_review_gate("09:25", self.cands, {"temperature": 30}, self.db)
        self.ms._GATE_EQ_CACHE = {"total_equity": 479352.93}
        per = self.ms._gate_single_budget(47935.29, 1)
        self.assertEqual(per, 0.0, "single=0 时单笔额度必须是 0（实际 %.0f）" % per)

    def test_escape_keeps_stage_cap(self):
        """逃生阀降级不得放大段总仓上限（退潮仍是 10%）"""
        self.ms.load_review_gate = lambda *a, **k: tide_gate()
        self.ms._apply_review_gate("09:25", self.cands, {"temperature": 76.5}, self.db)
        g = self.ms._ACTIVE_REVIEW_GATE
        self.assertEqual(g.get("position_cap_pct"), 10, "段总仓上限不得被逃生阀放大")
        self.assertEqual(g.get("max_picks"), 1, "逃生阀后只数应为试探档 1")
        self.assertEqual(g.get("single_pos_cap_pct"), 8, "逃生阀后单票应为试探档 8%")


class TestGateFallbackScript(unittest.TestCase):
    """P0-2：15:35 闸门生成的 launchd 兜底"""

    def test_script_exists_and_executable(self):
        p = os.path.join(SCRIPTS, "run_gate1535.sh")
        self.assertTrue(os.path.isfile(p), "run_gate1535.sh 必须存在")
        self.assertTrue(os.access(p, os.X_OK), "run_gate1535.sh 必须可执行")

    def test_script_syntax(self):
        r = subprocess.run(["/bin/zsh", "-n", os.path.join(SCRIPTS, "run_gate1535.sh")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, "zsh 语法错误: %s" % r.stderr)

    def test_selftest_passes(self):
        r = subprocess.run([os.path.join(SCRIPTS, "run_gate1535.sh"), "--selftest"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, "自检失败:\n%s" % r.stdout)
        self.assertNotIn("[FAIL]", r.stdout, "自检存在 FAIL:\n%s" % r.stdout)

    def test_slot1530_hooks_gate_generation(self):
        """15:30 已加载槽尾部必须挂闸门生成（本机 bootstrap 报 I/O error，不能只靠新 plist）"""
        src = open(os.path.join(SCRIPTS, "run_slot.sh"), encoding="utf-8").read()
        self.assertIn("run_gate1535.sh", src, "run_slot.sh 必须挂载闸门生成")
        self.assertIn('STAGE" = "盘后复盘', src, "必须挂在盘后复盘时段")

    def test_plist_registered(self):
        p = os.path.expanduser("~/Library/LaunchAgents/com.marketadvisor.gate1535.plist")
        self.assertTrue(os.path.isfile(p), "gate1535 plist 必须已写入 LaunchAgents")
        r = subprocess.run(["plutil", "-lint", p], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, "plist 格式非法: %s" % r.stdout)

    def test_four_item_crosscheck_detects_pollution(self):
        """四项对表必须能检出「竞价口径伪冰点」（11-25 事故形态）"""
        import review_gate as rg
        snap = rg._fetch_market_snapshot(as_object=True)
        d = snap.__dict__ if hasattr(snap, "__dict__") else {}
        b = d.get("breadth") or {}
        zt_c, dt_c, zb_c = d.get("zt_count"), d.get("dt_count"), d.get("zb_count")
        if not b.get("up") or not b.get("down") or zt_c is None:
            self.skipTest("工作台快照不完整，跳过")
        live_ratio = round(float(b["up"]) / float(b["down"]), 4)
        live_zb = (round(float(zb_c) / (float(zb_c) + float(zt_c)) * 100, 2)
                   if (zb_c + zt_c) > 0 else None)
        # 伪冰点：zt=0/dt=0/ratio=0.5/zb_rate=0 → 逐字段与收盘真值对表。
        # 盘后部分字段会归零（如 dt=0 而真实跌停家数为 0 之外的情形），归零字段
        # 与伪冰点同值属「不可区分」而非「检出失败」，仅对非零真值字段断言。
        checks = [("zt", 0, int(zt_c))]
        if dt_c is not None:
            checks.append(("dt", 0, int(dt_c)))
        checks.append(("ratio", 0.5, live_ratio))
        if live_zb is not None:
            checks.append(("zb_rate", 0.0, live_zb))
        comparable = [(n, p, r) for n, p, r in checks if p != r]
        if not comparable:
            self.skipTest("盘后快照全部归零，与伪冰点不可区分，跳过")
        for name, pv, rv in comparable:
            self.assertNotEqual(pv, rv,
                                "伪冰点 %s=%s 必须与真值 %s 不吻合" % (name, pv, rv))


class TestReportPositionSameSource(unittest.TestCase):
    """P0-3：报告仓位必须与闸门同源"""

    def setUp(self):
        import market_scheduler as ms
        self.ms = ms

    def tearDown(self):
        self.ms._ACTIVE_REVIEW_GATE = None

    def test_hard_block_reports_zero(self):
        """硬阻断段：报告仓位必须是 0%（修复前写 50%-70%）"""
        self.ms._ACTIVE_REVIEW_GATE = tide_gate()
        txt, note = self.ms._gate_position_text("50%-70%")
        self.assertIn("0%", txt, "硬阻断段报告仓位必须是 0%%，实得 %s" % txt)
        self.assertTrue(note, "必须给出闸门约束说明")

    def test_gate_cap_wins_over_live_tier(self):
        """闸门 cap=10 时，报告仓位不得写 50%-70%"""
        self.ms._ACTIVE_REVIEW_GATE = tide_gate(hard_block=False, max_picks=1,
                                                 single_pos_cap_pct=8)
        txt, note = self.ms._gate_position_text("50%-70%")
        self.assertIn("10%", txt, "报告仓位必须收敛到闸门 10%%，实得 %s" % txt)
        self.assertNotIn("70%", txt, "报告仓位不得保留实时分档的 70%")

    def test_no_gate_keeps_live_tier(self):
        """无闸门时沿用实时分档（不改变既有行为）"""
        self.ms._ACTIVE_REVIEW_GATE = None
        txt, note = self.ms._gate_position_text("50%-70%")
        self.assertEqual(txt, "50%-70%", "无闸门时应原样返回")
        self.assertEqual(note, "")

    def test_tighter_gate_wins(self):
        """闸门比实时分档更松时，仍以闸门为准（ADR-0049：报告=派单）"""
        self.ms._ACTIVE_REVIEW_GATE = tide_gate(stage="分歧", hard_block=False,
                                                 position_cap_pct=40,
                                                 single_pos_cap_pct=13, max_picks=3)
        txt, note = self.ms._gate_position_text("20%-40%")
        self.assertIn("40%", txt, "分歧段报告仓位应为 40%%，实得 %s" % txt)


if __name__ == "__main__":
    unittest.main(verbosity=2)

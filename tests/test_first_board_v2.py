# -*- coding: utf-8 -*-
"""首板雷达 v2 回归测试（2026-11-05）

用户反馈原话：「现在这个框给我推过四次数据都是涨了三四个点我买了就下跌了」。
本测试把这条痛点固化为不可回退的断言：
  * +3~4% 早段异动绝不推送（v1 的误报根因）
  * 只有「当天第一次涨停 + 板进度≥0.85 + 封板意愿极强 + 连续确认」才推
  * 今日已封板 / 昨日涨停(连板接力) / ST / 巨量出货 一律排除
  * 单帧假突破不推；板进度归一化自动适配不同涨停制度

运行：/opt/homebrew/anaconda3/bin/python3 -m unittest discover -s tests -v
"""
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import sprite


def make_row(pct, code="300001", name="测试航天", vr=1.5):
    """构造一帧全A快照。主板 10% 涨停：prev=10.00 / zt=11.00。"""
    prev = 10.0
    price = round(prev * (1 + pct / 100.0), 4)
    return {
        "code": code, "name": name, "pct": float(pct), "price": price,
        "prev_close": prev, "open": 9.85, "zt_price": 11.0, "limit_down": 9.0,
        "high": price, "low": 9.80, "avg_price": round(price - 0.08, 4),
        "amount_yi": 3.5, "vol_ratio": vr, "turnover": 5.0, "mv_yi": 80.0,
        "speed_1m": None,
        # 真实五档：卖一被吃光、买一堆量、委差大幅为正、外盘占优
        "bid1": price, "bid1_vol": 300000.0,
        "ask1": 0.0, "ask1_vol": 0.0,
        "bid5_vol": 500000.0, "ask5_vol": 100000.0,
        "weicha": 500000.0, "outer": 150000.0, "inner": 50000.0,
    }


def feed_ramp(eng, base, pcts):
    """按 20s 一帧喂一串涨幅，返回事件列表。"""
    for i, p in enumerate(pcts):
        eng.feed_full([make_row(p)], ts=base + i * 20, in_trading=True)
    return [e for e in eng.events if e["kind"] == "first_board"]


class TestBoardProgress(unittest.TestCase):
    def test_progress_normalized(self):
        f = sprite.SpriteEngine._board_progress
        self.assertAlmostEqual(f(make_row(3.0)), 0.30, places=2)
        self.assertAlmostEqual(f(make_row(4.0)), 0.40, places=2)
        self.assertAlmostEqual(f(make_row(9.0)), 0.90, places=2)
        r = make_row(17.0)
        r["zt_price"] = 12.0          # 创业板 20% 涨停
        self.assertAlmostEqual(f(r), 0.85, places=2)
        self.assertIsNone(f({"price": 10, "prev_close": 10, "zt_price": 0}))


class TestFirstBoardV2(unittest.TestCase):
    def setUp(self):
        self.eng = sprite.SpriteEngine()
        self.eng._gate = {"allow": True, "reason": "test", "temp": 60, "stage": "发酵"}
        # 固定「今日」，避免 _rollover 在测试中途清空状态
        self.eng._stats["day"] = datetime.now().strftime("%Y-%m-%d")
        self.base = time.time() - 1000

    def test_early_34pct_never_emits(self):
        """核心回归：+3~4% 早段异动一次都不许推。"""
        evs = feed_ramp(self.eng, self.base, [3.0, 3.4, 3.7, 4.0, 3.8, 4.2, 3.9, 4.1])
        self.assertEqual(len(evs), 0, "+3~4% 早段不应产生任何首板事件")
        self.assertEqual(self.eng._fb_stats["emitted"], 0)

    def test_near_limit_strong_intent_emits_after_confirm(self):
        """临板 + 强意愿 + 连续 2 帧 → 推，且事件带真实盘口因子。"""
        # 倒数第二帧 8.4% (板进度 0.84) 仍在临板线之下，确保只有 1 帧进入判定
        ramp = [4.0, 4.6, 5.2, 5.9, 6.6, 7.3, 8.0, 8.4, 9.0]
        feed_ramp(self.eng, self.base, ramp)       # 第 1 帧临板：streak=1
        self.assertEqual(len([e for e in self.eng.events
                              if e["kind"] == "first_board"]), 0, "单帧不应推")
        feed_ramp(self.eng, self.base + len(ramp) * 20, [9.1])  # 第 2 帧临板：streak=2
        evs = [e for e in self.eng.events if e["kind"] == "first_board"]
        self.assertEqual(len(evs), 1, "连续确认后应推 1 次")
        e = evs[0]
        self.assertGreaterEqual(e["intent"], config.FB_INTENT_MIN)
        self.assertGreaterEqual(e["quality"], config.FB_QUALITY_MIN)
        self.assertGreaterEqual(e["board_progress"], config.FB_NEAR_PROGRESS)
        self.assertIn("卖压清空", e["highlights"])
        self.assertTrue(e["voice"].startswith("首板临门"))
        self.assertLess(e["dist_pct"], 1.5)

    def test_already_sealed_today_excluded(self):
        self.eng._zt_ever.add("300001")            # 今日曾封板
        evs = feed_ramp(self.eng, self.base,
                        [4.0, 4.6, 5.2, 5.9, 6.6, 7.3, 8.1, 8.6, 9.0, 9.1])
        self.assertEqual(len(evs), 0, "当天已封过板不算第一次涨停")
        self.assertGreater(self.eng._fb_stats["filtered"].get("今日已封过板", 0), 0)

    def test_prev_day_limit_up_excluded(self):
        self.eng._prev_zt = {"300001"}             # 昨日涨停 = 连板接力
        self.eng._prev_zt_day = datetime.now().strftime("%Y-%m-%d")
        evs = feed_ramp(self.eng, self.base,
                        [4.0, 4.6, 5.2, 5.9, 6.6, 7.3, 8.1, 8.6, 9.0, 9.1])
        self.assertEqual(len(evs), 0, "昨日涨停属于连板接力，不是首板")

    def test_st_and_giant_volume_excluded(self):
        evs = feed_ramp(self.eng, self.base, [4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 9.1])
        self.assertEqual(len(evs), 1)              # 基准先确认能推
        eng2 = sprite.SpriteEngine()
        eng2._gate = {"allow": True, "reason": "test", "temp": 60, "stage": "发酵"}
        eng2._stats["day"] = datetime.now().strftime("%Y-%m-%d")
        rows = [make_row(p) for p in [4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 9.1]]
        for i, r in enumerate(rows):
            r["name"] = "*ST测试"
            eng2.feed_full([r], ts=self.base + i * 20, in_trading=True)
        self.assertEqual(len([e for e in eng2.events if e["kind"] == "first_board"]), 0,
                         "ST 不应推送")

    def test_weak_intent_not_emitted(self):
        """板进度够了但盘口是卖压重 / 委差为负 → 封板意愿不足，不推。"""
        rows = [make_row(p) for p in [4.0, 4.8, 5.6, 6.4, 7.2, 8.0, 8.7, 9.0, 9.1]]
        for r in rows:
            r["ask1_vol"] = 400000.0
            r["bid1_vol"] = 100.0
            r["bid5_vol"] = 1000.0
            r["ask5_vol"] = 600000.0
            r["weicha"] = -500000.0
            r["outer"] = 10000.0
            r["inner"] = 190000.0
            r["avg_price"] = r["price"] + 0.5
        for i, r in enumerate(rows):
            self.eng.feed_full([r], ts=self.base + i * 20, in_trading=True)
        self.assertEqual(len([e for e in self.eng.events
                              if e["kind"] == "first_board"]), 0, "意愿不足不应推")

    def test_gate_blocks(self):
        self.eng._gate = {"allow": False, "reason": "情绪退潮", "temp": 30, "stage": "退潮"}
        evs = feed_ramp(self.eng, self.base,
                        [4.0, 4.6, 5.2, 5.9, 6.6, 7.3, 8.1, 8.6, 9.0, 9.1])
        self.assertEqual(len(evs), 0, "大盘/情绪闸门仍应生效")


if __name__ == "__main__":
    unittest.main(verbosity=2)

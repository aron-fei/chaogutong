# -*- coding: utf-8 -*-
"""GJD 宽基 ETF 资金面引擎测试（2026-11-06，ADR-0049）。

覆盖三类真会出事的地方：
  ① 日内进度必须「向下取档」，取到未来档 = 基准虚高 = 倍数虚低 = 把买入看成缩量
  ② 「放量但净卖出」必须判出货，不能因为 ratio 高就给满分（这是本模块相对第三方
     页面的核心增量）
  ③ 缺内外盘时必须返回 None 而不是 0 —— 0 会被下游当成「净买入持平」，属于伪造数据
"""

import unittest
from datetime import datetime

import gjd_etf as g


def _base_items():
    """夹具自洽：成交额 = 手数×100×均价，且 外盘+内盘 = 总手数。"""
    return [
        {"code": "510300", "tcode": "sh510300", "name": "沪深300ETF", "index": "沪深300",
         "group": "core", "huijin_yi": 735.0, "avg20_yi": 80.0, "mktcap_yi": 1800.0,
         "shape": [["10:45", 0.40], ["15:00", 1.0]]},
        {"code": "159915", "tcode": "sz159915", "name": "创业板ETF", "index": "创业板指",
         "group": "core", "huijin_yi": 170.0, "avg20_yi": 20.0, "mktcap_yi": 400.0,
         "shape": [["10:45", 0.50], ["15:00", 1.0]]},
        {"code": "588000", "tcode": "sh588000", "name": "科创50ETF", "index": "科创50",
         "group": "watch", "huijin_yi": 0.0, "avg20_yi": 100.0, "mktcap_yi": 1000.0,
         "shape": [["10:45", 0.30], ["15:00", 1.0]]},
    ]


def _live():
    return {
        # 基准 32 亿，成交 64 亿 → 2.00×；净买入 +8.00 亿
        "510300": {"price": 4.0, "prev_close": 4.0, "pct": 0.0, "amount_yi": 64.0,
                   "vol_lots": 16000000.0, "outer": 9000000.0, "inner": 7000000.0,
                   "avg_px": 4.0},
        # 基准 10 亿，成交 15 亿 → 1.50×；净买入 -5.00 亿
        "159915": {"price": 2.5, "prev_close": 2.5, "pct": 0.0, "amount_yi": 15.0,
                   "vol_lots": 6000000.0, "outer": 2000000.0, "inner": 4000000.0,
                   "avg_px": 2.5},
        "588000": {"price": 1.3, "prev_close": 1.3, "pct": 0.0, "amount_yi": 30.0,
                   "vol_lots": 23076923.0, "outer": 11538461.0, "inner": 11538462.0,
                   "avg_px": 1.3},
    }


AT = datetime(2026, 11, 6, 10, 47)


class TestProgress(unittest.TestCase):
    def test_floor_not_future(self):
        """10:47 只能取 10:45 档；取 10:50 就是把未来成交额算进基准。"""
        shape = [["09:35", 0.07], ["10:45", 0.40], ["10:50", 0.44], ["15:00", 1.0]]
        self.assertEqual(g._progress(shape, "10:47"), 0.40)
        self.assertEqual(g._progress(shape, "10:50"), 0.44)
        self.assertEqual(g._progress(shape, "15:00"), 1.0)

    def test_before_first_bucket(self):
        """开盘瞬间没有任何已过档 → None（交给调用方降级），不能返回 0。"""
        self.assertIsNone(g._progress([["09:35", 0.07]], "09:30"))

    def test_empty_shape(self):
        self.assertIsNone(g._progress([], "10:00"))

    def test_linear_fallback(self):
        self.assertEqual(g._linear_progress("09:30"), 0.0)
        self.assertEqual(g._linear_progress("11:30"), 0.5)
        self.assertEqual(g._linear_progress("15:00"), 1.0)


class TestGrade(unittest.TestCase):
    def test_distribution_beats_noise(self):
        """放量 + 净卖出 = 出货，分数必须低于常态，不能因为 ratio 高就高分。"""
        d = g.grade(2.5, -8.0)
        self.assertEqual(d["code"], "distribution")
        self.assertLess(d["score"], g.grade(1.0, 0.0)["score"])

    def test_strong_buy(self):
        d = g.grade(2.5, 8.0)
        self.assertEqual((d["code"], d["score"]), ("strong_buy", 100))

    def test_buy(self):
        d = g.grade(1.6, 3.0)
        self.assertEqual((d["code"], d["score"]), ("buy", 80))

    def test_shrink(self):
        self.assertEqual(g.grade(0.5, -20.0)["code"], "shrink")

    def test_unknown(self):
        self.assertEqual(g.grade(None, None)["code"], "unknown")

    def test_missing_net_cannot_reach_top(self):
        """缺内外盘时不能给满分 —— 分不清是增持还是出货。"""
        d = g.grade(2.5, None)
        self.assertLess(d["score"], 80)
        self.assertEqual(d["code"], "mild_buy")


class TestCompute(unittest.TestCase):
    def test_totals(self):
        r = g.compute(_base_items(), _live(), now=AT)
        c = r["core"]
        self.assertEqual(c["amount_yi"], 79.0)     # 64 + 15
        self.assertEqual(c["base_yi"], 42.0)       # 32 + 10
        self.assertEqual(c["ratio"], 1.88)         # 79 / 42
        self.assertEqual(c["net_buy_yi"], 3.0)     # +8.00 - 5.00
        self.assertEqual(c["net_cover"], 2)

    def test_per_etf_shape_used(self):
        """每只用自己的曲线，不能笼统按同一进度摊。"""
        r = g.compute(_base_items(), _live(), now=AT)
        by = {x["code"]: x for x in r["core"]["items"]}
        self.assertEqual(by["510300"]["ratio"], 2.0)
        self.assertEqual(by["159915"]["ratio"], 1.5)

    def test_watch_separated(self):
        r = g.compute(_base_items(), _live(), now=AT)
        self.assertEqual(r["core"]["count"], 2)
        self.assertEqual(r["watch"]["count"], 1)

    def test_missing_orderbook_not_zero(self):
        """内外盘全 0 → 净买入 None，绝不返回 0（0 会被读成「中性偏多」）。"""
        live = _live()
        for v in live.values():
            v["outer"] = v["inner"] = 0.0
        c = g.compute(_base_items(), live, now=AT)["core"]
        self.assertIsNone(c["net_buy_yi"])
        self.assertEqual(c["net_cover"], 0)
        self.assertEqual(c["ratio"], 1.88)          # 倍数仍可算

    def test_preopen_no_ratio(self):
        r = g.compute(_base_items(), _live(), now=datetime(2026, 11, 6, 9, 10))
        self.assertEqual(r["session"], "preopen")
        self.assertIsNone(r["core"]["ratio"])


class TestGateEffect(unittest.TestCase):
    def test_mapping_direction(self):
        self.assertEqual(g.gate_effect("distribution")["dir"], "tighten")
        self.assertLess(g.gate_effect("distribution")["pos_delta"], 0)
        self.assertEqual(g.gate_effect("strong_buy")["dir"], "loosen")
        self.assertGreater(g.gate_effect("strong_buy")["pos_delta"], 0)

    def test_unknown_is_hold(self):
        e = g.gate_effect("whatever")
        self.assertEqual(e["dir"], "hold")
        self.assertEqual((e["pos_delta"], e["pick_delta"]), (0.0, 0))

    def test_all_levels_covered(self):
        for code, _label, _lo, _need, _score in g.LEVELS:
            self.assertIn(code, g.GATE_EFFECT)
        for extra in ("distribution", "unknown"):
            self.assertIn(extra, g.GATE_EFFECT)

    def test_distribution_worse_than_shrink(self):
        """出货比国家队缺席更危险 —— 罚必须更重。"""
        self.assertLess(g.GATE_EFFECT["distribution"]["pos_delta"],
                        g.GATE_EFFECT["shrink"]["pos_delta"])
        self.assertEqual(g.GATE_EFFECT["normal"]["pos_delta"], 0.0)

    def test_compute_exposes_effect(self):
        r = g.compute(_base_items(), _live(), now=AT)
        self.assertIn("effect", r)
        self.assertEqual(r["effect"]["dir"], "loosen")   # 1.88× + 净买入 → buy


class TestQuickSnapshot(unittest.TestCase):
    def test_fail_open_without_base(self):
        """无基准 → ok=False + hold（fail-open），绝不因取数问题收紧仓位。"""
        old = g.BASE_FILE
        g.BASE_FILE = g.BASE_DIR / "data" / "__nonexistent_gjd_base__.json"
        try:
            s = g.quick_snapshot(timeout=1)
        finally:
            g.BASE_FILE = old
        self.assertFalse(s["ok"])
        self.assertEqual(s["effect"]["dir"], "hold")
        self.assertEqual(s["effect"]["pos_delta"], 0)


if __name__ == "__main__":
    unittest.main()

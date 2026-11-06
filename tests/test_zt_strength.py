# -*- coding: utf-8 -*-
"""板块强度榜 / 涨停晋级率回归（2026-11-06 开盘啦借鉴）

锁死四件事：
  ① 强度是绝对刻度（满刻度 10000），不是当日 min-max —— 否则每天都有一堆满分，
     「持续火爆」就失去意义；
  ② 缺失维度剔除后重归一，绝不补 0 —— 补 0 会冤枉「资金字段没匹配上」的板块，
     与 2026-10-21「炸板率恒 100%」是同一类坑（缺失 ≠ 最差）；
  ③ 无历史不编造名次变化（rank_delta 必须 None）；
  ④ 晋级率 / 分层晋级的分母必须去重，且没有分层数据时如实标 known=False。
"""
import unittest

import zt_board_stat as zbs


class TestStrength(unittest.TestCase):
    def test_full_scale_is_10000(self):
        g = {"count": 8, "max_lianban": 5, "zdf": 9.0, "speed": 3.0, "netflow_yi": 25.0}
        s, used = zbs.strength_of(g)
        self.assertEqual(s, zbs.STRENGTH_MAX)
        self.assertEqual(used, 5)

    def test_missing_dim_is_renormalised_not_zeroed(self):
        full = {"count": 8, "max_lianban": 5, "zdf": 9.0, "speed": 3.0, "netflow_yi": 25.0}
        nofund = {"count": 8, "max_lianban": 5, "zdf": 9.0, "speed": 3.0}
        s_full, u_full = zbs.strength_of(full)
        s_nofund, u_nofund = zbs.strength_of(nofund)
        self.assertEqual(u_nofund, 4)
        # 其它维度都是满刻度 → 剔除资金后重归一仍是满刻度，而不是被砍到 84%
        self.assertEqual(s_nofund, s_full)

    def test_nan_not_polluting(self):
        g = {"count": 3, "max_lianban": 2, "zdf": 1.0, "speed": None,
             "netflow_yi": float("nan")}
        s, used = zbs.strength_of(g)
        self.assertEqual(used, 3)
        self.assertGreater(s, 0)
        self.assertLess(s, zbs.STRENGTH_MAX)

    def test_all_missing_is_zero_not_crash(self):
        s, used = zbs.strength_of({"count": 2})
        self.assertGreater(used, 0)     # count 仍在
        s0, used0 = zbs.strength_of({})
        self.assertEqual((s0, used0), (0, 0))


class TestAnnotate(unittest.TestCase):
    def test_sorted_desc_and_rank(self):
        gs = [{"name": "弱", "count": 1, "max_lianban": 1, "zdf": 0.0, "speed": 0.0,
               "netflow_yi": 0.0},
              {"name": "强", "count": 8, "max_lianban": 5, "zdf": 9.0, "speed": 3.0,
               "netflow_yi": 25.0}]
        out = zbs.annotate(gs, "unittest_nohist", record=False)
        self.assertEqual([g["name"] for g in out["groups"]], ["强", "弱"])
        self.assertEqual(out["groups"][0]["rank"], 1)
        self.assertFalse(out["has_history"])

    def test_no_history_never_invents_delta(self):
        gs = [{"name": "A", "count": 5, "max_lianban": 2}]
        out = zbs.annotate(gs, "unittest_nohist", record=False)
        g = out["groups"][0]
        self.assertIsNone(g["rank_delta"])
        self.assertIsNone(g["strength_prev"])

    def test_hot_streak_by_ladder(self):
        g = {"count": 3, "max_lianban": 3, "zdf": -5.0, "speed": -2.0, "netflow_yi": -30.0}
        out = zbs.annotate([dict(g, name="X")], "unittest_nohist", record=False)
        self.assertTrue(out["groups"][0]["hot_streak"])


class TestPromotion(unittest.TestCase):
    ZT = [{"code": "000001", "lianban": 2}, {"code": "000002", "lianban": 1}]
    PREV = {"000001", "000003", "000004"}
    PREV_ITEMS = [{"code": "000001", "lianban": 1},
                  {"code": "000003", "lianban": 1},
                  {"code": "000004", "lianban": 2}]
    FULL = [{"code": "000003", "pct": -3.5}, {"code": "000001", "pct": 10.0}]

    def test_promote_rate(self):
        ps = zbs.promotion_stats(self.ZT, self.PREV, self.PREV_ITEMS, self.FULL)
        self.assertEqual(ps["prev_count"], 3)
        self.assertEqual(ps["promoted"], 1)
        self.assertEqual(ps["promote_rate"], 33.3)

    def test_ladder_by_level(self):
        ps = zbs.promotion_stats(self.ZT, self.PREV, self.PREV_ITEMS, self.FULL)
        self.assertTrue(ps["prev_ladder_known"])
        lv = {x["from"]: x for x in ps["prev_ladder"]}
        self.assertEqual(lv[1]["base"], 2)
        self.assertEqual(lv[1]["hit"], 1)
        self.assertEqual(lv[1]["rate"], 50.0)
        self.assertEqual(lv[2]["rate"], 0.0)

    def test_losing_effect(self):
        ps = zbs.promotion_stats(self.ZT, self.PREV, self.PREV_ITEMS, self.FULL)
        self.assertEqual(ps["prev_avg_pct"], 3.25)
        self.assertEqual(ps["prev_down_ratio"], 50.0)

    def test_old_cache_without_items_declares_unknown(self):
        ps = zbs.promotion_stats(self.ZT, self.PREV, None, None)
        self.assertFalse(ps["prev_ladder_known"])
        self.assertIsNone(ps["prev_avg_pct"])     # 没有全A帧就不编造均涨
        self.assertEqual(ps["promote_rate"], 33.3)

    def test_empty_prev_is_none_not_zero(self):
        ps = zbs.promotion_stats(self.ZT, set(), None, None)
        self.assertIsNone(ps["promote_rate"])     # 0 会让人误以为「一只都没晋级」

    def test_duplicate_prev_items_deduped(self):
        items = [{"code": "000001", "lianban": 1}, {"code": "000001", "lianban": 2}]
        ps = zbs.promotion_stats(self.ZT, {"000001"}, items, None)
        base = sum(x["base"] for x in ps["prev_ladder"])
        self.assertEqual(base, 1)


class TestHistoryFile(unittest.TestCase):
    """2026-11-06 实测踩到的两个 bug：历史只留最后一条、行业维度键名写成空。"""

    def setUp(self):
        import tempfile
        from pathlib import Path as _P
        self.tmpdir = tempfile.TemporaryDirectory()
        self._old_file = zbs.HIST_FILE
        self._old_bucket = dict(zbs._last_bucket)
        zbs.HIST_FILE = _P(self.tmpdir.name) / "hist.jsonl"
        zbs._last_bucket.clear()

    def tearDown(self):
        zbs.HIST_FILE = self._old_file
        zbs._last_bucket.clear()
        zbs._last_bucket.update(self._old_bucket)
        self.tmpdir.cleanup()

    def test_append_does_not_overwrite_previous_lines(self):
        zbs._append_hist({"b": "b1", "dim": "theme", "items": {"A": 100}})
        zbs._append_hist({"b": "b2", "dim": "theme", "items": {"A": 200}})
        hist = zbs._read_hist()
        self.assertEqual(len(hist), 2, "第二档必须追加，不能把第一档冲掉")
        self.assertEqual(zbs.prev_strength_map("theme"), {"A": 200})

    def test_prev_map_excludes_current_bucket(self):
        zbs._append_hist({"b": "b1", "dim": "theme", "items": {"A": 100}})
        zbs._append_hist({"b": "b2", "dim": "theme", "items": {"A": 200}})
        self.assertEqual(zbs.prev_strength_map("theme", excludes_bucket="b2"), {"A": 100})

    def test_industry_dim_records_names(self):
        """行业分组只有 industry 键，历史 items 不能写成空对象。"""
        gs = [{"industry": "光伏设备", "count": 4, "max_lianban": 4,
               "zdf": 1.0, "speed": 0.5, "netflow_yi": 3.0}]
        zbs.annotate(gs, "industry", record=True)
        hist = zbs._read_hist()
        self.assertEqual(len(hist), 1)
        self.assertIn("光伏设备", hist[0]["items"])
        self.assertGreater(hist[0]["items"]["光伏设备"], 0)

    def test_second_bucket_yields_rank_delta(self):
        # 先写一条「上一档」历史（B 当时更强），再拿当前档去比
        zbs._append_hist({"b": "prev_bucket", "dim": "theme",
                          "items": {"A": 5000, "B": 9000}})
        gs = [{"name": "A", "count": 8, "max_lianban": 5, "zdf": 9.0, "speed": 3.0,
               "netflow_yi": 25.0},
              {"name": "B", "count": 1, "max_lianban": 1, "zdf": 0.0, "speed": 0.0,
               "netflow_yi": 0.0}]
        out = zbs.annotate(gs, "theme", record=True)
        self.assertTrue(out["has_history"])
        top = out["groups"][0]
        self.assertEqual(top["name"], "A")
        # A 从第 2 名升到第 1 名 → rank_delta = +1（正数=名次上升）
        self.assertEqual(top["rank_delta"], 1)
        self.assertTrue(top["hot_streak"])
        self.assertEqual(out["groups"][1]["rank_delta"], -1)


class TestZhaRate(unittest.TestCase):
    def test_zero_denominator(self):
        self.assertIsNone(zbs.zha_rate(0, 0))

    def test_normal(self):
        self.assertEqual(zbs.zha_rate(10, 5), 33.3)


class TestDashboardPayload(unittest.TestCase):
    def test_fields_present(self):
        import app as app_module
        with app_module.app.test_client() as c:
            r = c.get("/api/dashboard")
            self.assertEqual(r.status_code, 200)
            d = r.get_json()
        self.assertIn("zt_strength_meta", d)
        self.assertIn("zt_promotion", d)
        self.assertIn("zt_zha_rate", d)
        meta = d["zt_strength_meta"]
        self.assertEqual(meta.get("scale"), "local")
        self.assertEqual(meta.get("max"), 10000)
        # 每个板块都必须带强度与名次，前端才排得出来
        for dim in ("zt_theme_groups", "zt_board_groups"):
            for g in (d.get(dim) or [])[:20]:
                self.assertIsInstance(g.get("strength"), int)
                self.assertIsInstance(g.get("rank"), int)
                self.assertIn("rank_delta", g)

    def test_industry_groups_declare_match_mode(self):
        """行业行的行情可能是近似映射，必须带 board_match 供前端标 ~，
        不能把「光伏设备」与「电力」共用同一腾讯板块的数据伪装成精确值。"""
        import app as app_module
        with app_module.app.test_client() as c:
            d = c.get("/api/dashboard").get_json()
        for g in (d.get("zt_board_groups") or [])[:20]:
            if g.get("netflow_yi") is None:
                continue
            self.assertIn(g.get("board_match"), ("exact", "fuzzy", "alias"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

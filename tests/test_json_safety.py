# -*- coding: utf-8 -*-
"""回归：任何 jsonify 输出必须是严格合法 JSON，不得含 NaN / Infinity 字面量。

2026-11-06 实盘发现：东财概念榜的 pandas NaN 经 float(...) 后仍是 nan，
theme_board 元数据把它带到 /api/theme_pick 的 lead_pct，Python json.dumps
默认输出 NaN，浏览器 Response.json() 抛 "Unexpected token 'N'"，
导致 L0 趋势题材 / L1 低位挖掘整块显示「取数失败」。
"""
import json
import unittest

import app as app_module
import theme_board


class TestJsonSafety(unittest.TestCase):
    def test_provider_downgrades_non_finite(self):
        s = app_module.app.json.dumps({
            "nan": float("nan"),
            "inf": float("inf"),
            "neg_inf": float("-inf"),
            "nested": {"v": float("nan")},
            "lst": [1.0, float("nan")],
            "ok": 3.5,
            "txt": "普通文字",
        })
        self.assertNotIn("NaN", s)
        self.assertNotIn("Infinity", s)
        d = json.loads(s)
        self.assertIsNone(d["nan"])
        self.assertIsNone(d["inf"])
        self.assertIsNone(d["neg_inf"])
        self.assertIsNone(d["nested"]["v"])
        self.assertIsNone(d["lst"][1])
        self.assertEqual(d["ok"], 3.5)
        self.assertEqual(d["txt"], "普通文字")

    def test_theme_board_finite_helper(self):
        self.assertIsNone(theme_board._finite(float("nan")))
        self.assertIsNone(theme_board._finite(float("inf")))
        self.assertEqual(theme_board._finite("3.5"), 3.5)
        self.assertEqual(theme_board._finite(None, 0.0), 0.0)
        self.assertEqual(theme_board._finite("bad", 7), 7)

    def test_theme_pick_api_strict_json(self):
        with app_module.app.test_client() as c:
            r = c.get("/api/theme_pick")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertNotIn(":NaN", body)
        self.assertNotIn(":Infinity", body)
        self.assertNotIn(":-Infinity", body)
        d = json.loads(body)
        self.assertTrue(d.get("ok"))
        for t in d.get("trends") or []:
            v = t.get("lead_pct")
            self.assertTrue(v is None or isinstance(v, (int, float)),
                            f"lead_pct 必须是有限数或 null，实际 {v!r}")


if __name__ == "__main__":
    unittest.main()

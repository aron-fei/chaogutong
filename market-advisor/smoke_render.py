#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
渲染路径冒烟测试 (smoke_render.py)
==================================
目的：挡住「改 _fmt_lots 等渲染辅助函数后，漏改调用方变量名导致整段报告/弹窗崩溃」这类回归。

背景：8/12 把 _fmt_lots 改成接收 symbol 参数(科创板 200股/手)时，有一处调用方变量名漏改
      (用了未定义的 `symbol` 而非作用域内已定义的 `sym`)，导致 8/13 09:25 启动即 NameError，
      全时段崩溃、整天无报告无弹窗。这种「改函数签名 → 漏改调用方」的回归，光测函数本身测不出，
      必须跑渲染路径才能暴露。本测试覆盖所有 _fmt_lots 渲染调用点。

覆盖点（全部 mock 掉网络/IO，秒级稳定）：
  A. build_account_review 在 active_positions 非空(含688票) + completed_trades 时渲染不崩
     —— 直接命中隐藏崩溃点 2677 行 `_fmt_lots(shares, symbol)`（symbol 未定义，持仓非空才触发）
  B. get_opening_auction_signals 09:25 含 688 候选 → 覆盖 1984 行 `_fmt_lots(shares, sym)`
  C. build_ops_flashcard 各时段含 688 候选 → 覆盖 _fmt_action 内 2559-2568 `_fmt_lots(it.get('symbol'))`
  D. run_backtest 持仓非空(含688票, 一只T+1 + 一只大跌) → 覆盖 1539/1593 行 `_fmt_lots(shares, symbol)`

用法(必须用 anaconda，因 import market_scheduler 需要 akshare)：
  /opt/homebrew/anaconda3/bin/python3 smoke_render.py
"""
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import market_scheduler as ms


def fake_xgb():
    return {
        "limit_up_pool": [
            {"stock_chi_name": "测试科创", "symbol": "688001.SH", "limit_up_days": 2,
             "change_percent": 0.05, "buy_lock_volume_ratio": 0.5,
             "non_restricted_capital": 5_000_000_000, "turnover_ratio": 0.03, "plates": ["半导体"]},
            {"stock_chi_name": "主板票", "symbol": "600519.SH", "limit_up_days": 3,
             "change_percent": 0.10, "buy_lock_volume_ratio": 0.6,
             "non_restricted_capital": 10_000_000_000, "turnover_ratio": 0.02, "plates": ["白酒"]},
        ],
        "super_stock_pool": [],
        "temperature": 50, "limit_up": 80, "limit_down": 5, "broken_ratio": 10,
        "premium": 1.0, "rise_count": 100, "fall_count": 50,
    }


def fake_stock_info(name=None):
    return {"current_price": 10.0, "pct": -8.0, "yesterday_close": 11.0, "high": 10.5, "low": 9.5}


# 在所有渲染测试中统一屏蔽的网络/IO 入口
GLOBAL_NET = {
    "get_akshare_macro": lambda: (0.5, 0.3, 0.2, 1_000_000.0),
    "fetch_xuangutong_data": fake_xgb,
    "get_stock_detailed_sina": fake_stock_info,
    "get_stock_limits": lambda s, n, yc: (999.0, 0.01),
    "get_stock_price_sina": lambda s: 1.0,
    "get_concept_pct": lambda xs: {},
    "load_state": lambda: None,
    "get_intel_candidates": lambda *a, **k: [],
    "get_intel_brief_lines": lambda *a, **k: [],
    "write_pending_sell_confirm": lambda *a, **k: None,  # 避免测试污染真实待确认单
    "write_pending_buy_confirm": lambda *a, **k: None,
    "save_backtest_db": lambda *a, **k: None,             # 避免测试把 fake_db 写回真实回测库
}


class SmokeRenderTest(unittest.TestCase):

    def setUp(self):
        self._patchers = []
        for name, fn in GLOBAL_NET.items():
            p = mock.patch.object(ms, name, fn)
            p.start()
            self._patchers.append(p)

    def tearDown(self):
        for p in self._patchers:
            p.stop()

    # ---- A. build_account_review：持仓非空渲染（命中隐藏崩溃点 2677）----
    def test_build_account_review_with_positions(self):
        fake_db = {
            "available_cash": 100000.0,
            "completed_trades": [
                {"name": "测试科创", "symbol": "688001.SH", "shares": 200,
                 "sell_price": 12.0, "profit_loss_amount": 400.0, "profit_loss_pct": 4.0},
            ],
            "active_positions": [
                {"name": "测试科创", "symbol": "688001.SH", "shares": 200,
                 "buy_price": 10.0, "buy_fee": 0.0, "buy_date": ""},
                {"name": "主板票", "symbol": "600519.SH", "shares": 100,
                 "buy_price": 100.0, "buy_fee": 0.0, "buy_date": ""},
            ],
        }
        with mock.patch.object(ms, "load_backtest_db", lambda: fake_db):
            out = ms.build_account_review()
        self.assertIn("测试科创", out)
        self.assertIn("主板票", out)
        # 科创板 200 股应渲染为 1手（200股/手），主板 100 股为 1手
        self.assertIn("1手", out)

    # ---- B. 09:25 集合竞价渲染含 688 候选（覆盖 1984）----
    def test_opening_auction_09_25_kc_lot(self):
        cands = [{"name": "测试科创", "symbol": "688001.SH", "price": 50.0, "buy_shares": 400}]
        out = ms.get_opening_auction_signals(fake_xgb(), "09:25", decision_buy_candidates=cands)
        out_str = "\n".join(out) if isinstance(out, list) else out
        self.assertIn("688001", out_str)
        self.assertIn("2手", out_str)  # 科创板 400 股 = 2手

    # ---- C. _fmt_action 四种操作类型含 688 候选（覆盖 2559-2568 全部 _fmt_lots 分支）----
    def test_fmt_action_kc_lots(self):
        board = {"max_lb": 5, "broken_ratio": 10, "limit_up": 80, "dragon_health": "中性", "premium": 1.0}
        for typ, sh in [("SELL_ALL", 200), ("SELL_HALF", 400), ("BUY", 400), ("BUY_LIMIT", 400)]:
            item = {"type": typ, "name": "科创", "symbol": "688001.SH",
                    "sell_shares": sh, "buy_shares": sh, "price": 50.0}
            s = ms._fmt_action(item)
            self.assertIn("688001", s, "_fmt_action(%s) 未渲染688候选" % typ)
        # 弹窗 14:40 买入路径同样应渲染 688 候选
        cands = [{"type": "BUY", "name": "科创", "symbol": "688001.SH", "price": 50.0,
                  "buy_shares": 400, "buy_lots": 2}]
        out = ms.build_ops_flashcard("14:40", cands, 50, board)
        self.assertIn("688001", out)

    # ---- D. run_backtest 持仓非空渲染（覆盖 1539/1593）----
    def test_run_backtest_with_positions(self):
        today = __import__("datetime").datetime.now().strftime("%Y-%m-%d")
        fake_db = {
            "available_cash": 100000.0,
            "initial_capital": 500000.0,
            "total_equity": 110000.0,
            "completed_trades": [],
            "active_positions": [
                # T+1 锁仓：覆盖 1539 行 _fmt_lots(shares, symbol)
                {"name": "科创T1", "symbol": "688001.SH", "shares": 200,
                 "buy_price": 10.0, "buy_fee": 0.0, "buy_date": today, "buy_session": "09:25",
                 "capital": 2000.0},
                # 大跌持仓：decide_exit 大概率 SELL，覆盖 1593 行 _fmt_lots(shares, symbol)
                {"name": "科创跌", "symbol": "688002.SH", "shares": 400,
                 "buy_price": 20.0, "buy_fee": 0.0, "buy_date": "2000-01-01", "buy_session": "14:40",
                 "capital": 8000.0},
            ],
        }
        with mock.patch.object(ms, "load_backtest_db", lambda: fake_db):
            out = ms.run_backtest(fake_xgb(), emit_sell=True)
        out_str = "\n".join(out) if isinstance(out, list) else out
        self.assertIn("688001", out_str)
        self.assertIn("688002", out_str)

    # ---- E. _mark_stale_board_pending_buys： stale 排单检测（防尾盘误显示早上委托价）----
    def test_mark_stale_board_pending_buys(self):
        import json
        import tempfile
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, f"pending_buy_confirm_0925_{today}.json")
            data = {
                "date": today, "session": "09:25", "deadline": "15:00", "status": "pending",
                "instruction": "test",
                "items": [{
                    "name": "测试票", "symbol": "600000.SS", "suggest_price": 100.0,
                    "suggest_lots": 1, "suggest_shares": 100, "actual_lots": 0, "actual_shares": 0,
                    "is_limit_up": False, "note": "09:25 排单"
                }]
            }
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            # 当前价跌至 90（跌幅 10%），且为跌停价 → 应触发失效标记
            with mock.patch.object(ms, "REPORTS_DIR", tmpdir):
                with mock.patch.object(ms, "get_stock_detailed_sina",
                                       lambda n: {"current_price": 90.0, "pct": -10.0,
                                                  "yesterday_close": 100.0, "high": 100.0, "low": 90.0}):
                    with mock.patch.object(ms, "get_stock_limits",
                                           lambda s, n, yc: (110.0, 90.0)):
                        ms._mark_stale_board_pending_buys()
            with open(path, 'r', encoding='utf-8') as f:
                new_data = json.load(f)
            it = new_data["items"][0]
            self.assertTrue(it.get("is_stale"), "跌幅超过阈值应标记 is_stale")
            self.assertIn("委托价 ¥100.00 已失效", it.get("note", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)

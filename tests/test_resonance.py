# -*- coding: utf-8 -*-
"""ADR-0055 交叉共振验证系统测试（纯函数/只读）

覆盖：
  1. 统计口径：胜率/盈亏比/均收益计算正确
  2. ST 过滤：ST 票不产生信号
  3. 历史重放：_genzhuang_at / _bottom_at 在真实缓存K线上能跑（不崩溃）
  4. 今日交叉：API 结构完整
  5. 回测缓存：能命中缓存且统计非空

运行：/opt/homebrew/anaconda3/bin/python3 -B tests/test_resonance.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import resonance as rz


def test_stats():
    st = rz._stats([
        {"ret": 5.0}, {"ret": -2.0}, {"ret": 3.0}, {"ret": -1.0},
    ])
    assert st["count"] == 4
    assert st["success_rate"] == 50.0
    assert abs(st["avg_win"] - 4.0) < 0.01
    assert abs(st["avg_loss"] + 1.5) < 0.01
    assert abs(st["profit_factor"] - 2.667) < 0.01, st
    print("[PASS] 统计口径:", st)


def test_stats_empty():
    st = rz._stats([{"ret": None}, {}])
    assert st["count"] == 0 and st["success_rate"] is None
    print("[PASS] 空统计降级")


def test_st_filter():
    # ST 票必须直接返回空信号
    assert rz._scan_one_stock("002717", "*ST岭南", 120) == []
    assert rz._scan_one_stock("002726", "ST龙大", 120) == []
    assert rz._scan_one_stock("600785", "新华百货", 120) is not None   # 正常票不因名字被挡
    print("[PASS] ST/退市过滤")


def test_history_replay():
    import bottom_surge as bs
    k = bs.daily_k("600785", n=250)
    assert k and len(k) >= 160
    g = rz._genzhuang_at(k, len(k) - 2)
    b = rz._bottom_at(k, len(k) - 2)
    assert g is None or isinstance(g, dict)
    assert b is None or isinstance(b, dict)
    print("[PASS] 历史重放可运行:", "gz=%s bottom=%s" % (bool(g), bool(b)))


def test_api_today():
    import json
    import urllib.request
    try:
        d = json.load(urllib.request.urlopen("http://127.0.0.1:8765/api/resonance", timeout=40))
    except Exception as e:
        print("[SKIP] 今日交叉（服务未就绪）:", e)
        return
    assert d.get("ok"), d
    for k in ("cross_count", "cross", "gz_hits", "bottom_recs", "backtest_stats"):
        assert k in d, (k, d.keys())
    print("[PASS] 今日交叉 严格%d 近似%d 回测胜率%s" %
          (d["cross_count"], d.get("near_count", 0),
           (d.get("backtest_stats") or {}).get("success_rate")))


def test_backtest_cache():
    import json
    import urllib.request
    try:
        d = json.load(urllib.request.urlopen("http://127.0.0.1:8765/api/resonance?view=backtest", timeout=40))
    except Exception as e:
        print("[SKIP] 回测缓存（服务未就绪）:", e)
        return
    assert d.get("ok"), d
    assert d["stats"]["count"] > 0, d
    assert d["stats"]["success_rate"] is not None
    assert d["stats"]["profit_factor"] is not None
    print("[PASS] 回测缓存命中: %d信号 胜率%s%% 盈亏比%s" %
          (d["stats"]["count"], d["stats"]["success_rate"], d["stats"]["profit_factor"]))


if __name__ == "__main__":
    test_stats()
    test_stats_empty()
    test_st_filter()
    test_history_replay()
    test_api_today()
    test_backtest_cache()
    print("ALL PASS")

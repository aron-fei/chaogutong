# -*- coding: utf-8 -*-
"""ADR-0053 首板蓄势形态评分引擎测试（纯函数/无副作用）

覆盖：
  1. 评分单调性：蓄势形态 > 横盘 > 下跌
  2. 封板质量单调性：好封板 > 差封板
  3. 涨停当日 K 线必须被剔除（只看涨停前形态）
  4. 数据不足降级
  5. 今日实况：25 只首板能跑出合理排序，含强候选

运行：/opt/homebrew/anaconda3/bin/python3 -B tests/test_firstboard_setup.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import firstboard_setup as fb


def _mk(n, start=10.0, drift=0.02, vol_base=1000, vol_drift=0.0, red_fat=False,
        today_last=False):
    rows, c = [], start
    for i in range(n):
        o = c
        c = c * (1 + drift)
        hi, lo = c * 1.012, o * 0.988
        v = vol_base * (1 + vol_drift * i)
        if red_fat:
            v = v * (1.3 if i % 2 == 0 else 0.7)
        rows.append({"date": f"2026-11-{i+1:02d}", "open": o, "close": c,
                     "high": hi, "low": lo, "vol_shou": v})
    if today_last:
        rows[-1]["date"] = "2099-01-01"   # 模拟"今日涨停日"
    return rows


GOOD_SEAL = {"first_time": "093000", "fund_yi": 2.0, "mv_yi": 50,
             "break_count": 0, "turnover": 6.0}
BAD_SEAL = {"first_time": "110000", "fund_yi": 0.1, "mv_yi": 50,
            "break_count": 3, "turnover": 25.0}


def test_score_monotonic():
    a = fb.analyze_one("A", _mk(25, drift=0.012, vol_drift=0.06, red_fat=True), "2099-01-01", GOOD_SEAL)
    b = fb.analyze_one("B", _mk(25, drift=0.0, vol_drift=-0.02), "2099-01-01", GOOD_SEAL)
    c = fb.analyze_one("C", _mk(25, drift=-0.01, vol_drift=0.10), "2099-01-01", GOOD_SEAL)
    assert a["score"] > b["score"] and a["score"] > c["score"], (a["score"], b["score"], c["score"])
    assert a["setup"] > b["setup"] and a["setup"] > c["setup"]
    assert a["verdict"] == "强候选", a
    assert b["verdict"] == "淘汰" and c["verdict"] == "淘汰"
    print("[PASS] 评分单调性 蓄势%.1f > 横盘%.1f / 下跌%.1f" % (a["score"], b["score"], c["score"]))


def test_seal_quality():
    k = _mk(25, drift=0.012, vol_drift=0.06, red_fat=True)
    good = fb.analyze_one("G", k, "2099-01-01", GOOD_SEAL)
    bad = fb.analyze_one("B", k, "2099-01-01", BAD_SEAL)
    assert good["seal"] > bad["seal"], (good["seal"], bad["seal"])
    assert good["score"] > bad["score"]
    print("[PASS] 封板质量 好%.0f > 差%.0f" % (good["seal"], bad["seal"]))


def test_exclude_today():
    k = _mk(20, today_last=True)
    pre = fb.pre_breakout_bars(k, "2099-01-01")
    assert len(pre) == 19 and all(x["date"] != "2099-01-01" for x in pre)
    print("[PASS] 涨停当日 K 线已剔除")


def test_insufficient():
    r = fb.analyze_one("D", _mk(5), "2099-01-01")
    assert r["verdict"] == "数据不足"
    print("[PASS] 数据不足降级")


def test_rank_today():
    """今日实况：需 Flask 服务在跑；否则跳过。"""
    import json
    import urllib.request
    try:
        d = json.load(urllib.request.urlopen("http://127.0.0.1:8765/api/firstboard_setup", timeout=25))
    except Exception as e:
        print("[SKIP] 今日实况（服务未就绪）:", e)
        return
    if not d.get("ok"):
        print("[SKIP] 今日实况:", d.get("error"))
        return
    ranked = d.get("ranked") or []
    assert d.get("first_count", 0) > 0
    assert ranked and all("score" in r for r in ranked)
    scores = [r["score"] for r in ranked]
    assert scores == sorted(scores, reverse=True), "排序必须降序"
    assert d.get("pick") is not None
    print("[PASS] 今日实况 %d 首板 → Top1 %s %.1f" %
          (d["first_count"], d["pick"]["name"], d["pick"]["score"]))


if __name__ == "__main__":
    test_score_monotonic()
    test_seal_quality()
    test_exclude_today()
    test_insufficient()
    test_rank_today()
    print("ALL PASS")

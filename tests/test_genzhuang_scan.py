# -*- coding: utf-8 -*-
"""ADR-0054 跟庄建仓公式全市场扫描器测试（纯函数/无副作用）

覆盖：
  1. 粗筛：排除 ST / 涨停 / 阴跌 / 低成交额
  2. 公式语义：底部横盘微抬头命中；高位/加速赶底不命中
  3. 今日实时帧追加：DD/EE 算的是今天而非缓存日K
  4. 今日实况：API 能跑出命中结果

运行：/opt/homebrew/anaconda3/bin/python3 -B tests/test_genzhuang_scan.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import genzhuang_scan as gs


def _mk_bottom_turn():
    """底部横盘45天后微微抬头的教科书形态。"""
    c, h, l, v = [], [], [], []
    px = 30.0
    for i in range(45):
        if i < 30:
            px -= 0.5
        elif i < 42:
            px = 10 + (i % 3) * 0.05
        else:
            px = 10.3 + (i - 41) * 0.15
        c.append(px); h.append(px + 0.3); l.append(px - 0.3)
        v.append(1000000 if i < 40 else (1300000 if i < 43 else 1800000))
    return [{"date": f"d{i}", "close": c[i], "high": h[i], "low": l[i], "vol_shou": v[i]}
            for i in range(45)]


def test_coarse_filter():
    rows = [
        {"code": "000001", "name": "正常票", "price": 10, "pct": 3, "vol_ratio": 2.0,
         "amount_wan": 5000, "zt_price": 11},
        {"code": "000002", "name": "ST票", "price": 10, "pct": 3, "vol_ratio": 2.0,
         "amount_wan": 5000, "zt_price": 11},
        {"code": "000003", "name": "涨停票", "price": 11, "pct": 10, "vol_ratio": 3.0,
         "amount_wan": 8000, "zt_price": 11},
        {"code": "000004", "name": "阴跌票", "price": 10, "pct": -2, "vol_ratio": 2.0,
         "amount_wan": 5000, "zt_price": 11},
        {"code": "000005", "name": "死水票", "price": 10, "pct": 3, "vol_ratio": 2.0,
         "amount_wan": 100, "zt_price": 11},
    ]
    out, stat = _coarse_rows(rows)
    codes = {r["code"] for r in out}
    assert codes == {"000001"}, (codes, stat)
    print("[PASS] 粗筛排除 ST/涨停/阴跌/死水")


def _coarse_rows(rows):
    return gs._coarse(rows)


def test_formula_semantics():
    hit = gs._eval_one({"code": "999999", "name": "测试"}, _mk_bottom_turn())
    assert hit and hit["aa"] < 13 and hit["cc"] > 13, hit
    assert hit["close_up"] and hit["ma5_gt_ma10"]
    print("[PASS] 底部横盘微抬头命中 AA=%.1f CC=%.1f" % (hit["aa"], hit["cc"]))

    # 高位票不命中
    k2 = [{"date": f"d{i}", "close": 20 + i * 0.5, "high": 21 + i * 0.5,
           "low": 19 + i * 0.5, "vol_shou": 1000000} for i in range(45)]
    assert gs._eval_one({"code": "000000", "name": "高位"}, k2) is None
    print("[PASS] 高位票正确排除")


def test_append_today():
    """今日实时帧必须替换缓存里的旧 bar，DD/EE 才算今天。"""
    k = _mk_bottom_turn()
    rec = {"price": 10.9, "open": 10.5, "high": 11.0, "low": 10.4,
           "vol_shou": 2000000}
    out = gs._append_today(k, rec)
    assert out[-1]["close"] == 10.9 and out[-1]["vol_shou"] == 2000000
    assert len(out) == len(k) + 1
    # 同日重复调用应替换而非追加
    out2 = gs._append_today(out, rec)
    assert len(out2) == len(out)
    print("[PASS] 今日实时帧追加/替换 OK")


def test_api_today():
    import json
    import urllib.request
    try:
        d = json.load(urllib.request.urlopen("http://127.0.0.1:8765/api/genzhuang_scan", timeout=40))
    except Exception as e:
        print("[SKIP] 今日实况（服务未就绪）:", e)
        return
    if not d.get("ok"):
        print("[SKIP] 今日实况:", d.get("error"))
        return
    assert d["universe"] > 4000 and d["probed"] > 0
    for h in d.get("hits", []):
        assert h["aa"] < 13 and h["cc"] > 13, h
    print("[PASS] 今日实况 全A %d → 命中 %d 只，均满足 AA<13/CC>13" %
          (d["universe"], d["hit_count"]))


if __name__ == "__main__":
    test_coarse_filter()
    test_formula_semantics()
    test_append_today()
    test_api_today()
    print("ALL PASS")

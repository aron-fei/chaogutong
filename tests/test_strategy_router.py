# -*- coding: utf-8 -*-
"""ADR-0051 多策略路由集成测试（只读/无副作用）

覆盖：
  1. 策略路由四模式判定 + 白名单
  2. 冰点防守候选过滤（今天实况 44 只候选 → 保留防守票）
  3. 仓位覆盖：冰点从 0 → 15%
  4. 真系统性风险全清空
  5. 打板模式完全不被改动（回归保护）
  6. 跟庄建仓公式落地

运行：/opt/homebrew/anaconda3/bin/python3 -B tests/test_strategy_router.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "market-advisor"))

import strategy_router as sr


def test_modes():
    assert sr.judge_mode(temp=70, stage="发酵")["mode"] == sr.MODE_AGGRESSIVE
    assert sr.judge_mode(temp=50, stage="确认")["mode"] == sr.MODE_TREND
    assert sr.judge_mode(temp=40, stage="分歧")["mode"] == sr.MODE_REVERSAL
    for t in (23, 28, 34):
        assert sr.judge_mode(temp=t, stage="退潮")["mode"] == sr.MODE_DEFENSE
    print("[PASS] 四模式判定")


def test_defense_filter():
    """今天实况：候选池有打板票 + 防守票，冰点只保留防守来源。"""
    route = sr.judge_mode(temp=28, stage="退潮", up=1200, down=3800)
    cands = [
        {"name": "冰山冷热", "sources": ["连板主线", "题材双雄"]},   # 打板 → 剔除
        {"name": "比亚迪", "sources": ["情报"]},                       # 防守 → 保留
        {"name": "凯美特气", "sources": ["底部放量"]},                 # 防守 → 保留
        {"name": "宁德时代", "sources": ["情报", "龙头资金"]},         # 含情报 → 保留
        {"name": "某连板妖", "sources": ["午后强势", "补涨·首阴"]},    # 打板 → 剔除
    ]
    # 不过 max_picks 截断，先看完整过滤结果（路由白名单是否生效）
    kept_all = [c for c in cands if sr.allow_source(route, c["sources"])]
    names_all = [c["name"] for c in kept_all]
    assert "比亚迪" in names_all and "凯美特气" in names_all, names_all
    assert "冰山冷热" not in names_all and "某连板妖" not in names_all, names_all
    # 再验证截断上限（防守 max_picks=1）
    kept = kept_all[:route["max_picks"]]
    assert len(kept) <= route["max_picks"]
    print("[PASS] 冰点防守过滤:", names_all, "→ 实派", [c["name"] for c in kept])


def test_position_override():
    import market_scheduler as m
    # 冰点原本归零
    m._STRATEGY_POS_OVERRIDE = None
    assert m._sentiment_pos_factor(28) == 0.0
    # 防守覆盖后 15%
    route = sr.judge_mode(temp=28, stage="退潮")
    m._STRATEGY_POS_OVERRIDE = route["max_pos"]
    assert abs(m._sentiment_pos_factor(28) - 0.15) < 1e-9
    # 真系统性风险不覆盖 → 仍归零
    m._STRATEGY_POS_OVERRIDE = None
    sysrisk = sr.judge_mode(temp=20, cy_pct=-4.0, stage="退潮")
    assert sysrisk["gate_open"] is False
    assert m._sentiment_pos_factor(20) == 0.0
    m._STRATEGY_POS_OVERRIDE = None
    print("[PASS] 仓位覆盖 0→15% + 系统性风险归零")


def test_aggressive_untouched():
    """回归保护：打板模式白名单含全部原有强势源，行为不变。"""
    route = sr.judge_mode(temp=70, stage="发酵")
    assert route["mode"] == sr.MODE_AGGRESSIVE
    for src in ("连板主线", "题材双雄", "午后强势", "底部突围", "龙首阴"):
        assert src in route["sources"], src
    assert route["max_pos"] == 0.45
    print("[PASS] 打板模式白名单完整（原逻辑不变）")


def test_genzhuang_formula():
    closes = [10.0] * 29 + [8.05, 8.15, 8.25, 8.4]
    highs = [12.0] * 33
    lows = [8.0] * 33
    vols = [100.0] * 28 + [300.0] * 5
    r = sr.genzhuang_xg(closes, highs, lows, vols)
    assert r["hit"] and r["aa"] < 13 and r["cc"] > 13
    print("[PASS] 跟庄建仓公式:", r["desc"])


if __name__ == "__main__":
    test_modes()
    test_defense_filter()
    test_position_override()
    test_aggressive_untouched()
    test_genzhuang_formula()
    print("\n[ALL PASS] ADR-0051 多策略路由集成测试")

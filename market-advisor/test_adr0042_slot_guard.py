#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ADR-0042 时段窗口守卫 + 行情就绪守卫 + save_state 空数据集护栏 —— 集成测试。

背景：2026-10-08（节后首日）桌面端 automation 在 app 唤醒后补跑错过的日程，
09:10/09:12/09:16 连跑 11:40/14:40 三档，抓盘前空快照；其中 14:40 档把
strategy_state.json（跨日基准）覆盖成"今日空状态"，导致当日正点运行的
【跨日策略连续性追踪】整段丢失。

本测试拦截一切副作用：STATE_FILE 重定向到临时目录，不触碰真实 reports 目录。
运行：/opt/homebrew/anaconda3/bin/python3 test_adr0042_slot_guard.py
"""
import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "market_scheduler.py")

# 以模块方式加载（不执行 __main__，无任何弹窗/写库副作用）
_spec = importlib.util.spec_from_file_location("ms_adr0042", SCRIPT)
ms = importlib.util.module_from_spec(_spec)
sys.modules["ms_adr0042"] = ms
_spec.loader.exec_module(ms)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + ((" | " + str(detail)) if detail else ""))


def hm(s):
    h, m = s.split(":")
    return datetime(2026, 10, 8, int(h), int(m))


print("=" * 70)
print("ADR-0042 守卫集成测试")
print("=" * 70)

# ---------- 1. 时段窗口守卫 ----------
print("\n[1] slot_window_ok —— 补跑必须被拦，正点必须放行")
cases = [
    ("13:40", "09:14", False, "13:40 档在 09:14 补跑（本次事故场景）"),
    ("13:40", "13:40", True, "13:40 正点"),
    ("11:40", "09:10", False, "11:40 档在 09:10 补跑（事故场景）"),
    ("11:40", "11:40", True, "11:40 正点"),
    ("11:40", "11:48", True, "11:40 窗口内延迟 8 分钟"),
    ("09:25", "09:25", True, "09:25 正点（集合竞价）"),
    ("09:25", "08:50", False, "09:25 档在 08:50 补跑"),
    ("10:40", "10:40", True, "10:40 唯一卖出决策点，正点"),
    ("14:40", "14:40", True, "14:40 正点"),
    ("14:40", "15:08", True, "14:40 窗口内延迟补跑（休眠唤醒）"),
    ("14:40", "15:30", False, "14:40 档在 15:30 补跑（超窗口，属盘后复盘的地盘）"),
    ("盘后复盘", "15:30", True, "盘后复盘 15:30"),
]
for stage, now_s, expect, desc in cases:
    ok, why = ms.slot_window_ok(stage, now=hm(now_s))
    check(f"{desc} → {'放行' if expect else '拦截'}", ok is expect, why)

ok, why = ms.slot_window_ok("auto未识别时段", now=hm("03:00"))
check("未定义时段 fail-open（异常放行，不误杀）", ok is True, why)

# ---------- 2. 行情就绪守卫 ----------
print("\n[2] market_data_ready —— 盘前零成交必须判定未就绪")
check("09:25 豁免（用竞价快照）", ms.market_data_ready("09:25")[0] is True)
check("盘后复盘豁免（用收盘快照）", ms.market_data_ready("盘后复盘")[0] is True)

_orig_macro = ms.get_akshare_macro
try:
    ms.get_akshare_macro = lambda: (0.0, 0.0, 0.0, 0.0)
    ok, why = ms.market_data_ready("13:40", tries=2, delay=0)
    check("成交额 0 → 判定未就绪（拦截）", ok is False, why)

    ms.get_akshare_macro = lambda: (0.5, -0.2, 0.1, 12345.0)
    ok, why = ms.market_data_ready("13:40", tries=2, delay=0)
    check("成交额>0 → 就绪（放行）", ok is True, why)

    _calls = {"n": 0}

    def flaky():
        _calls["n"] += 1
        if _calls["n"] == 1:
            raise RuntimeError("模拟首次网络抖动")
        return (0.3, 0.1, 0.0, 9999.0)

    ms.get_akshare_macro = flaky
    ok, why = ms.market_data_ready("13:40", tries=3, delay=0)
    check("首次异常+重试成功 → 放行（不误杀）", ok is True, why)
finally:
    ms.get_akshare_macro = _orig_macro

# ---------- 3. save_state 空数据集护栏 ----------
print("\n[3] save_state 空数据集护栏 —— 禁止抹掉跨日基准")
tmpdir = tempfile.mkdtemp(prefix="adr0042_")
_orig_state_file = ms.STATE_FILE
try:
    ms.STATE_FILE = os.path.join(tmpdir, "strategy_state.json")
    base = {"date": "2026-09-30", "dragons": ["新华传媒"],
            "top_leaders": ["新华传媒"], "sectors": ["医药", "机器人", "大消费"],
            "first_board_stars": [{"name": "昭衍新药"}]}
    with open(ms.STATE_FILE, "w") as f:
        json.dump(base, f, ensure_ascii=False)

    # 3.1 事故场景复现：盘前空跑（全空）调用 save_state
    ms.save_state([], [], [], "2026-10-08", [])
    after = json.load(open(ms.STATE_FILE))
    check("全空数据集 → 拒绝覆盖（09-30 基准完好）",
          after.get("date") == "2026-09-30" and after.get("dragons") == ["新华传媒"],
          f"date={after.get('date')}")

    # 3.2 有数据时正常落盘
    ms.save_state(["新华传媒"], ["新华传媒"], ["医药"], "2026-10-08",
                  [{"name": "某某新星"}])
    after2 = json.load(open(ms.STATE_FILE))
    check("有数据 → 正常覆盖写入",
          after2.get("date") == "2026-10-08" and after2.get("sectors") == ["医药"])

    # 3.3 force=True 可显式覆盖（逃生阀）
    ms.save_state([], [], [], "2026-10-08", [], force=True)
    after3 = json.load(open(ms.STATE_FILE))
    check("force=True → 允许写入空状态（逃生阀）",
          after3.get("date") == "2026-10-08" and after3.get("dragons") == [])
finally:
    ms.STATE_FILE = _orig_state_file

# ---------- 4. 代码结构守卫（防止未来重构把闸门挪到副作用之后） ----------
print("\n[4] 代码结构守卫 —— 闸门必须早于报告生成")
src = open(SCRIPT, encoding="utf-8").read()
i_win = src.find("闸门 1：时段窗口守卫")
i_rd = src.find("闸门 2：行情就绪守卫")
i_gen = src.find("generate_report(current_stage)", src.find('if __name__ == "__main__"'))
check("闸门 1 存在且早于 generate_report()", 0 < i_win < i_gen)
check("闸门 2 存在且早于 generate_report()", 0 < i_rd < i_gen)
check("闸门 2 晚于闸门 1（先看时钟，再看行情）", i_win < i_rd)
check("--force 逃生阀已注册", 'parser.add_argument("--force"' in src)
check("两个闸门均有 sys.exit(0) 零副作用退出",
      src.count("跳过报告生成（零副作用）") == 2)

# ---------- 5. 跨日基准可用性（重建后必须能恢复跨日追踪段） ----------
print("\n[5] 跨日基准可用性 —— 空跑污染 vs 重建基准")
_today = datetime.now().strftime("%Y-%m-%d")
_orig_px = ms.get_stock_price_sina
_orig_det = ms.get_stock_detailed_sina
try:
    ms.get_stock_price_sina = lambda name: ("000000", 2.5)
    ms.get_stock_detailed_sina = lambda name: {"yesterday_close": 10.0, "current_price": 10.25}

    # 5.1 污染态（事故后状态：date=今日 且全空）→ 跨日分支与追踪段双失
    polluted = {"date": _today, "dragons": [], "top_leaders": [], "sectors": [],
                "first_board_stars": []}
    cross_day_branch = bool(polluted) and polluted.get("date") != _today
    check("污染态：跨日分支被跳过（prev_state.date == 今日）", cross_day_branch is False)
    check("污染态：首板新星追踪段为空", ms.build_first_board_followup(polluted, "13:40") == [],
          "整段丢失 = 事故的实际影响")

    # 5.2 重建态（09-30 基准）→ 两段都恢复
    rebuilt_path = "/Users/williamli/Work/reports/strategy_state.json"
    if os.path.exists(rebuilt_path):
        rebuilt = json.load(open(rebuilt_path, encoding="utf-8"))
        check("重建态：date 为上一交易日 2026-09-30（≠ 今日）",
              rebuilt.get("date") == "2026-09-30", f"date={rebuilt.get('date')}")
        check("重建态：旧龙基准存在（新华传媒）", rebuilt.get("dragons") == ["新华传媒"])
        check("重建态：旧主线基准存在（医药/机器人/大消费）",
              rebuilt.get("sectors") == ["医药", "机器人", "大消费"])
        cross_ok = bool(rebuilt) and rebuilt.get("date") != _today
        check("重建态：跨日分支将执行", cross_ok is True)
        seg = ms.build_first_board_followup(rebuilt, "13:40")
        check("重建态：首板新星追踪段恢复（含 09-30 标签 + 5 只判定）",
              len(seg) > 5 and any("2026-09-30" in s for s in seg)
              and sum(1 for s in seg if s.strip().startswith("•  ") or s.strip().startswith("• ")) >= 5,
              f"{len(seg)} 行")
    else:
        check("重建态：state 文件存在", False, rebuilt_path)
finally:
    ms.get_stock_price_sina = _orig_px
    ms.get_stock_detailed_sina = _orig_det

print("\n" + "=" * 70)
print(f"结果：PASS {len(PASS)} / FAIL {len(FAIL)}")
if FAIL:
    for f in FAIL:
        print(f"  ❌ {f}")
    print("=" * 70)
    sys.exit(1)
print("全部通过 ✅")
print("=" * 70)

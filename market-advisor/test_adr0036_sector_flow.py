#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ADR-0036 定向集成测试（副作用全拦截：不发网络请求、不写报告/订单/回测库）。

覆盖三处改造：
  L1 可成交性闸门 _fill_gate()      —— 均价下方 / 流动性不足 → 挡；取数异常 → 放行
  L2 板块资金流    sector_flow_signal() + rank_operation_pool 放行与加分
  L3 连板动量降权  prog 20→12、board_days>3 归零

背景实证（2026-08-24~09-28 全量回溯 66 条建议）：
  操作池 159 只中 91 只(59.5%)涨停；66 条建议实际成交仅 5 条(7.6%)；
  09:25 打板 5 条 0 成交、80% 当日涨停、60% 一字板。
"""
import sys
from datetime import datetime

sys.path.insert(0, "/Users/williamli/.workbuddy/skills/market-advisor/scripts")
import market_scheduler as ms

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


TODAY = datetime.now().strftime("%Y-%m-%d")

# ---------------------------------------------------------------------------
print("== 1) _fill_gate 可成交性闸门 ==")
# 现价 10.0，成交额 5 亿 / 成交量 4000 万股 → 均价 12.5 > 现价 → 应挡
ok, why = ms._fill_gate({"current_price": 10.0, "volume": 40_000_000, "amount": 5e8})
check("现价低于均价 → 挡", ok is False, f"ok={ok} why={why}")
check("理由含'低于当日均价'", "低于当日均价" in why, f"why={why}")

# 现价 13.0 > 均价 12.5，成交额 5 亿 → 放行
ok, why = ms._fill_gate({"current_price": 13.0, "volume": 40_000_000, "amount": 5e8})
check("现价站上均价 → 放行", ok is True, f"ok={ok} why={why}")

# 现价 13.0 > 均价，但成交额 0.4 亿 < 1 亿 → 挡
ok, why = ms._fill_gate({"current_price": 2.0, "volume": 20_000_000, "amount": 4e7})
check("成交额不足 → 挡", ok is False, f"ok={ok} why={why}")
check("理由含'流动性不足'", "流动性不足" in why, f"why={why}")

# 边界：成交额恰为 1 亿 → 放行（用 >= 门槛）
ok, _ = ms._fill_gate({"current_price": 2.0, "volume": 50_000_000, "amount": 1e8})
check("成交额=1亿边界 → 放行", ok is True, f"ok={ok}")

# 安全设计：取数异常 → 必须放行（宁放过不错杀）
for bad in ({"current_price": 10.0, "volume": 0, "amount": 5e8},
            {"current_price": 10.0, "volume": 1e6, "amount": 0},
            {"current_price": 0, "volume": 1e6, "amount": 1e8},
            {"volume": 1e6, "amount": 1e8}):
    ok, _ = ms._fill_gate(bad)
    check(f"取数异常放行 {bad}", ok is True, f"ok={ok}")

# 总开关
ms.FILL_GATE_ENABLED = False
ok, _ = ms._fill_gate({"current_price": 1.0, "volume": 1e6, "amount": 1e9})
check("FILL_GATE_ENABLED=False → 恒放行", ok is True, "")
ms.FILL_GATE_ENABLED = True

# ---------------------------------------------------------------------------
print("== 2) 板块资金流：分级与匹配 ==")
check("净流入 6 亿 → 最高档 14", ms._sf_tier(6.0) == 14.0, f"got {ms._sf_tier(6.0)}")
check("净流入 2.5 亿 → 9", ms._sf_tier(2.5) == 9.0, f"got {ms._sf_tier(2.5)}")
check("净流入 1.0 亿 → 5", ms._sf_tier(1.0) == 5.0, f"got {ms._sf_tier(1.0)}")
check("净流入 0.5 亿 → 不入选 0", ms._sf_tier(0.5) == 0.0, f"got {ms._sf_tier(0.5)}")
check("净流出 -3 亿 → 0", ms._sf_tier(-3.0) == 0.0, f"got {ms._sf_tier(-3.0)}")

FAKE_FLOW = {
    "by_name": {"公用事业": 6.21, "汽车": 3.74, "商贸零售": 1.47, "有色金属": -64.22},
    "by_lead": {"600032": ("公用事业", 6.21), "000678": ("汽车", 3.74)},
    "top": [("公用事业", 6.21, "浙江新能", "600032"), ("汽车", 3.74, "襄阳轴承", "000678")],
    "ts": 0.0, "err": "",
}
b, why = ms.sector_flow_signal({"symbol": "600032.SS", "name": "浙江新能", "plates": []}, FAKE_FLOW)
check("板块龙头命中 → 满分 14", b == 14.0, f"b={b} why={why}")
check("命中说明标注'板块龙头'", "板块龙头" in why, f"why={why}")

# 非龙头但 plates 命中净流入板块 → 打 7 折（14*0.7=9.8）
b, why = ms.sector_flow_signal({"symbol": "300999.SZ", "name": "某成分股",
                                "plates": ["公用事业"]}, FAKE_FLOW)
check("成分股命中(板块名) → 打折 9.8", abs(b - 9.8) < 1e-6, f"b={b} why={why}")
check("命中说明标注'所属板块'", "所属板块" in why, f"why={why}")

# 概念名双向包含匹配（板块名"汽车" ⊂ plate"新能源汽车"）
b, why = ms.sector_flow_signal({"symbol": "300999.SZ", "name": "车股",
                                "plates": ["新能源汽车"]}, FAKE_FLOW)
check("行业名被 plate 包含 → 命中", b > 0, f"b={b} why={why}")

# 净流出板块不得加分
b, why = ms.sector_flow_signal({"symbol": "300999.SZ", "name": "有色股",
                                "plates": ["有色金属"]}, FAKE_FLOW)
check("净流出板块 → 不加分", b == 0.0 and why == "", f"b={b} why={why}")

# 完全无关 → 0
b, why = ms.sector_flow_signal({"symbol": "300999.SZ", "name": "无关股",
                                "plates": ["白酒概念"]}, FAKE_FLOW)
check("无关联 → 0", b == 0.0, f"b={b} why={why}")

# 取较大者：同一只票既命中龙头(汽车 3.74亿 → tier 9.0) 又命中成分板块(公用事业 6.21亿 → 14*0.7=9.8)
# → 应取 9.8（成分匹配更大）；逻辑是 max 而非"龙头无条件优先"
b, why = ms.sector_flow_signal({"symbol": "000678.SZ", "name": "襄阳轴承",
                                "plates": ["公用事业"]}, FAKE_FLOW)
check("双命中取较大值(成分9.8 > 龙头9.0)", abs(b - 9.8) < 1e-6, f"b={b} why={why}")
# 反向：龙头档位更高时应取龙头 14
b, why = ms.sector_flow_signal({"symbol": "600032", "name": "浙江新能",
                                "plates": ["汽车"]}, FAKE_FLOW)
check("双命中取较大值(龙头14 > 成分6.3)", abs(b - 14.0) < 1e-6, f"b={b} why={why}")

# ---------------------------------------------------------------------------
print("== 3) rank_operation_pool：资金流加分 + 白名单放行 ==")
ms.fetch_sector_flow = lambda force=False: FAKE_FLOW
ms.is_suspended = lambda name: False
ms.SECTOR_FLOW_STAGES = ("13:40", "14:40")

DB_EMPTY = {"active_positions": [], "available_cash": 400000.0}


def entry(sym, name, sources, signal=80.0, board_days=1, last_pct=3.0, plates=None):
    return {"symbol": sym, "name": name, "sources": list(sources), "signal": signal,
            "board_days": board_days, "first_seen": TODAY, "last_seen": TODAY,
            "last_pct": last_pct, "plates": plates or [], "thesis": "", "status": "tracking"}


# A. 白名单内 + 命中净流入板块 → 应带 sector_flow_bonus
ent = {"000111.SZ": entry("000111.SZ", "安全票", ["底部放量"], plates=["公用事业"])}
out = ms.rank_operation_pool(ent, {}, DB_EMPTY, "14:40", [], 60.0, None)
check("白名单内命中板块 → 加分 9.8", out and abs(out[0]["sector_flow_bonus"] - 9.8) < 1e-6,
      f"out={out}")
check("输出带命中文案", out and "所属板块" in out[0]["sector_flow_why"], f"out={out}")

# B. 白名单【外】但命中净流入板块 → 应被放行
ent = {"600032.SS": entry("600032.SS", "浙江新能", ["龙头资金"], plates=[])}
out = ms.rank_operation_pool(ent, {}, DB_EMPTY, "14:40", [], 60.0, None)
check("白名单外+龙头命中 → 放行", len(out) == 1 and out[0]["name"] == "浙江新能", f"out={out}")

# C. 白名单【外】且无资金流 → 仍必须被挡
ent = {"600099.SS": entry("600099.SS", "被挡票", ["龙头资金"], plates=["有色金属"])}
out = ms.rank_operation_pool(ent, {}, DB_EMPTY, "14:40", [], 60.0, None)
check("白名单外+无资金流 → 被挡", len(out) == 0, f"out={out}")

# D. 放行数量受 SECTOR_FLOW_MAX_ADMIT 限制
ent = {}
for i in range(5):
    s = "6000%02d" % (10 + i)
    ent[s] = entry(s, "放行票%d" % i, ["龙头资金"], signal=90 - i, plates=["公用事业"])
out = ms.rank_operation_pool(ent, {}, DB_EMPTY, "14:40", [], 60.0, None)
check("放行上限 2 只", len(out) == 2, f"len={len(out)} out={[o['name'] for o in out]}")

# E. 涨停票在 14:40 仍被硬过滤（资金流不能突破涨停闸门）
ent = {"600032.SS": entry("600032.SS", "浙江新能", ["龙头资金"], last_pct=10.0, plates=[])}
out = ms.rank_operation_pool(ent, {}, DB_EMPTY, "14:40", [], 60.0, None)
check("涨停票即使命中资金流也被挡", len(out) == 0, f"out={out}")

# F. 09:25 不接入资金流（SECTOR_FLOW_STAGES 不含 09:25）
ent = {"600099.SS": entry("600099.SS", "被挡票", ["龙头资金"], plates=["公用事业"])}
out = ms.rank_operation_pool(ent, {}, DB_EMPTY, "09:25", [], 60.0, None)
check("09:25 不因资金流放行", len(out) == 1, f"out={out}")   # 09:25 白名单=None → 全放行
# 但 09:25 不应带资金流加分（_flow 为空）
check("09:25 无资金流加分", out and out[0]["sector_flow_bonus"] == 0.0, f"out={out}")

# ---------------------------------------------------------------------------
print("== 4) 连板动量降权（仅影响 09:25 打板池） ==")
# board_days=2 且涨停 → 12 分；board_days=5 → 0 分
ent2 = {"600201.SS": entry("600201.SS", "二板票", ["连板主线"], board_days=2, last_pct=10.0)}
o2 = ms.rank_operation_pool(ent2, {}, DB_EMPTY, "09:25", [], 60.0, None)
ent5 = {"600205.SS": entry("600205.SS", "五板票", ["连板主线"], board_days=5, last_pct=10.0)}
o5 = ms.rank_operation_pool(ent5, {}, DB_EMPTY, "09:25", [], 60.0, None)
# 同 consensus/signal 下，分差应体现 prog 差 = 12
check("二板 prog=12 生效", o2 and abs(o2[0]["score"] - (15.0 + 28.0 + 12.0)) < 0.05,
      f"score={o2[0]['score'] if o2 else None}")
check("五板 prog 归零", o5 and abs(o5[0]["score"] - (15.0 + 28.0 + 0.0)) < 0.05,
      f"score={o5[0]['score'] if o5 else None}")
# 首板仍为 5
ent1 = {"600206.SS": entry("600206.SS", "首板票", ["连板主线"], board_days=1, last_pct=10.0)}
o1 = ms.rank_operation_pool(ent1, {}, DB_EMPTY, "09:25", [], 60.0, None)
check("首板 prog=5 不变", o1 and abs(o1[0]["score"] - (15.0 + 28.0 + 5.0)) < 0.05,
      f"score={o1[0]['score'] if o1 else None}")

# ---------------------------------------------------------------------------
print("== 5) 降级安全：资金流不可用时行为完全回到改造前 ==")
ms.fetch_sector_flow = lambda force=False: {"by_name": {}, "by_lead": {}, "top": [],
                                            "ts": 0.0, "err": "mock fail"}
ent = {"600099.SS": entry("600099.SS", "被挡票", ["龙头资金"], plates=["公用事业"])}
out = ms.rank_operation_pool(ent, {}, DB_EMPTY, "14:40", [], 60.0, None)
check("资金流为空 → 白名单外仍被挡(与改造前一致)", len(out) == 0, f"out={out}")
ent = {"000111.SZ": entry("000111.SZ", "安全票", ["底部放量"], plates=["公用事业"])}
out = ms.rank_operation_pool(ent, {}, DB_EMPTY, "14:40", [], 60.0, None)
check("资金流为空 → 白名单内不加分", out and out[0]["sector_flow_bonus"] == 0.0, f"out={out}")
# 旧口径 = consensus1*15 + signal80*0.35 + prog5 + bs_bonus40(14:40 底部放量) = 88.0
check("资金流为空 → 分数与旧口径(15+28+5+40)一致",
      out and abs(out[0]["score"] - 88.0) < 0.05, f"score={out[0]['score'] if out else None}")

print(f"\n===== RESULT: PASS={PASS} FAIL={FAIL} =====")
sys.exit(1 if FAIL else 0)

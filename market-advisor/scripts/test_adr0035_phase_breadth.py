#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ADR-0035 定向集成测试（副作用全拦截，不发任何网络请求、不写报告/订单文件）。

验证两处修复：
  1) classify_market_phase 广度权重：
     - 2026-09-28 实盘数据（温度47 / 1955涨2327跌 / 涨停5 / 溢价0.38 / 高度5板）
       必须从「修复/主升期」降级为「混沌震荡期」（对齐跨日追踪「管住手」）。
     - 2026-09-24 数据（47.9 / 1141:3305）同样降级。
     - 2026-09-17 数据（44.2 / 946:3711 极端失衡）必须判「强防守期」（对齐跨日「空仓防守」）。
     - 2026-09-21 数据（70.5 / 4029:1052 广度健康）必须保持「修复/主升期」不误伤。
     - 高潮期（09-21 型 82 家涨停）保持「高潮逼空期」。
  2) _board_buy_items 一字板流动性过滤：
     - 一字开盘涨停候选 → 只出 INFO 跳过派单，不出 BUY_LIMIT。
     - 盘中封板（非一字开盘）候选 → 正常 BUY_LIMIT 排队打板。
"""
import sys
import types

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


def phase(xgb, cy=-0.6, sh=-0.2, sz=-0.3, max_lb=5):
    emo, pos, atk, df, br = ms.classify_market_phase(xgb, cy, sh, sz, max_lb)
    return emo, pos, br


print("== 1) 盘口定性：广度权重 ==")

# 09-28 实盘：溢价>0 高度5板，但广度 0.84、温度 47 → 修复/主升 必须 NOT 触发
emo, pos, br = phase({"temperature": 47.0, "rise_count": 1955, "fall_count": 2327,
                      "limit_up": 5, "limit_down": 1, "broken_ratio": 0.0, "premium": 0.38}, max_lb=5)
check("09-28 不再误判修复/主升", "修复/主升" not in emo, f"got {emo}")
check("09-28 落入混沌震荡期", "混沌震荡" in emo, f"got {emo}")
check("09-28 仓位降为 30-50%", pos == "30%-50%", f"got {pos}")
check("09-28 广度比 0.84", abs(br - 1955 / 2327) < 1e-9, f"got {br}")

# 09-24 实盘：广度 0.345 极端失衡但温度 47.9(≥45) → 不强防守，落混沌
emo, pos, br = phase({"temperature": 47.9, "rise_count": 1141, "fall_count": 3305,
                      "limit_up": 5, "limit_down": 0, "broken_ratio": 0.0, "premium": 1.07}, max_lb=4)
check("09-24 不误判修复/主升", "修复/主升" not in emo, f"got {emo}")
check("09-24 落混沌(温度47.9未触发强防守)", "混沌震荡" in emo, f"got {emo}")

# 09-17 实盘：946:3711=0.255 极端失衡 + 温度 44.2<45 → 必须强防守（对齐跨日「空仓防守」）
emo, pos, br = phase({"temperature": 44.2, "rise_count": 946, "fall_count": 3711,
                      "limit_up": 5, "limit_down": 0, "broken_ratio": 0.0, "premium": 0.40}, max_lb=3)
check("09-17 极端失衡+偏冷 → 强防守", "强防守" in emo, f"got {emo}")

# 09-18 实盘：广度 4.10 健康 + 温度 71.3 + 溢价 1.79 + 4板龙 → 修复/主升 不误伤
emo, pos, br = phase({"temperature": 71.3, "rise_count": 3667, "fall_count": 895,
                      "limit_up": 7, "limit_down": 1, "broken_ratio": 0.0, "premium": 1.79}, max_lb=4)
check("09-18 广度健康保持修复/主升", "修复/主升" in emo, f"got {emo}")

# 高潮逼空：82 家涨停 + 溢价 2.5 → 高潮优先级仍在（limit_up>80 先命中）
emo, pos, br = phase({"temperature": 71.0, "rise_count": 4029, "fall_count": 1052,
                      "limit_up": 82, "limit_down": 2, "broken_ratio": 18.0, "premium": 2.5}, max_lb=5)
check("高潮逼空期不回退", "高潮逼空" in emo, f"got {emo}")

# 广度健康但温度 52、无极端 → 修复/主升 仍可触发（原行为保留）
emo, pos, br = phase({"temperature": 52.3, "rise_count": 2692, "fall_count": 2324,
                      "limit_up": 39, "limit_down": 2, "broken_ratio": 25.0, "premium": 2.38}, max_lb=6)
check("09-22 型(广度>1)仍判修复/主升", "修复/主升" in emo, f"got {emo}")

print("== 2) 一字板流动性过滤（_board_buy_items，副作用拦截） ==")

# 拦截网络/停牌检查
FAKE_OPEN = {}


def fake_quote(name):
    return {"name": name, "symbol": "sz000000", "open_price": FAKE_OPEN.get(name, 10.5),
            "yesterday_close": 10.0, "current_price": 11.0, "high": 11.0, "low": 10.5,
            "volume": 100, "amount": 1e6, "bid1": 11.0, "ask1": 11.0, "pct": 10.0,
            "date": "2026-09-28", "time": "09:25:00"}


ms.get_stock_detailed_sina = fake_quote
ms.get_stock_limits = lambda sym, name, yc: (11.0, 9.0)   # 昨收10 → 涨停11
ms.is_suspended = lambda name: False

board = {"max_lb": 5, "broken_ratio": 0.0, "limit_up": 50, "dragon_health": "极强",
         "premium": 3.5}  # 打板综合分应远超 55

# 候选1 一字开盘(open=11=涨停价)；候选2 盘中封板(open=10.5)
candidates = [{"name": "一字票", "symbol": "sz000001"},
              {"name": "换手票", "symbol": "sz000002"}]
FAKE_OPEN = {"一字票": 11.0, "换手票": 10.5}

items = ms._board_buy_items("09:25", candidates, avail=400000.0, mkt_temp=65.0, board=board)
buys = [it for it in items if it["type"] == "BUY_LIMIT"]
infos = [it for it in items if it["type"] == "INFO" and "一字" in it.get("note", "")]

check("一字票未派 BUY_LIMIT", all(it["name"] != "一字票" for it in buys),
      f"buys={[(b['name']) for b in buys]}")
check("一字票产出 INFO 跳过记录", any(it["name"] == "一字票" for it in infos), f"infos={infos}")
check("非一字票正常排队打板", any(it["name"] == "换手票" and it["type"] == "BUY_LIMIT" for it in items),
      f"items={[(i['type'], i.get('name')) for i in items]}")
check("BUY_LIMIT 挂单价=涨停价附近现价", all(10.9 <= it["price"] <= 11.01 for it in buys), "")

# 极端情形：全部候选都是一字 → 不应有任何 BUY_LIMIT
FAKE_OPEN = {"一字票A": 11.0}
items2 = ms._board_buy_items("09:25", [{"name": "一字票A", "symbol": "sz000003"}],
                             avail=400000.0, mkt_temp=65.0, board=board)
check("全一字 → 零 BUY_LIMIT", not [i for i in items2 if i["type"] == "BUY_LIMIT"],
      f"items2={items2}")

print(f"\n===== RESULT: PASS={PASS} FAIL={FAIL} =====")
sys.exit(1 if FAIL else 0)

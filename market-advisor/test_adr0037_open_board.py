# -*- coding: utf-8 -*-
"""ADR-0037 触板后开板闸门 —— 集成测试（全副作用拦截，零网络、零落盘）。

覆盖：
  1) _open_board_guard 单元：触板/未触板/仍封板/容差边界/取数缺失放行/开关关闭
  2) _plain_buy_items 集成：开板票被跳成 INFO、不占用手数、未触板票正常派单
  3) 与 ADR-0036 闸门共存：开板优先于均价闸门；两者互不干扰
  4) 降级安全：闸门取数不足时行为回到改造前
运行：/opt/homebrew/anaconda3/bin/python3 test_adr0037_open_board.py
"""
import sys, types, json, tempfile, os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import market_scheduler as ms   # noqa: E402

P = F = 0
FAILS = []


def chk(label, cond, extra=""):
    global P, F
    if cond:
        P += 1
        print("  [PASS] %s" % label)
    else:
        F += 1
        FAILS.append(label)
        print("  [FAIL] %s %s" % (label, extra))


# ---------------------------------------------------------------- 1) 单元
print("== 1) _open_board_guard 单元判据 ==")
# 触板后开板：最高摸到涨停价，现价回落
b, r = ms._open_board_guard({"current_price": 15.87, "high": 16.32}, 16.29)
chk("触板后开板 → 拦截", b is True)
chk("拦截文案含触板价与涨停价", "16.32" in r and "16.29" in r, r)

# 容差内（高 16.31 vs 涨停 16.29×(1-0.002)=16.257）→ 仍算触板
b, _ = ms._open_board_guard({"current_price": 15.90, "high": 16.30}, 16.29)
chk("容差内高处 → 仍拦截", b is True)

# 明确未触板：最高离涨停还远
b, row = ms._open_board_guard({"current_price": 14.80, "high": 14.95}, 16.29)
chk("全天未触板 → 放行", b is False and row == "")

# 容差恰好不足：高 = 涨停×(1-0.002) - 0.01 → 放行
lim = 10.00
b, _ = ms._open_board_guard({"current_price": 9.20, "high": lim * (1 - 0.002) - 0.01}, lim)
chk("低于容差阈值 → 放行", b is False)
b, _ = ms._open_board_guard({"current_price": 9.20, "high": lim * (1 - 0.002)}, lim)
chk("等于容差阈值 → 拦截", b is True)

# 仍封在板上 → 不属本闸门（幂等安全）
b, _ = ms._open_board_guard({"current_price": 16.29, "high": 16.29}, 16.29)
chk("仍封在板上 → 放行(幂等)", b is False)

# 取数缺失 → 放行 + WARN
for lbl, info, lu in (
    ("缺 high", {"current_price": 15.87}, 16.29),
    ("缺现价", {"high": 16.32}, 16.29),
    ("涨停价<=0", {"current_price": 15.87, "high": 16.32}, 0),
    ("负数", {"current_price": -1, "high": 16.32}, 16.29),
):
    b, _ = ms._open_board_guard(info, lu)
    chk("取数异常(%s) → 放行" % lbl, b is False)

# 开关关闭
_old = ms.OPEN_BOARD_GUARD_ENABLED
ms.OPEN_BOARD_GUARD_ENABLED = False
b, _ = ms._open_board_guard({"current_price": 15.87, "high": 16.32}, 16.29)
chk("开关关闭 → 全部放行", b is False)
ms.OPEN_BOARD_GUARD_ENABLED = _old


# ---------------------------------------------------- 2) 集成：_plain_buy_items
print("\n== 2) _plain_buy_items 集成 ==")

# 打桩：不触网
ms.is_suspended = lambda cn: False
ms.get_stock_limits = lambda cs, cn, pc: (round(pc * 1.10, 2), round(pc * 0.90, 2))

# 夹具自检（夹具算术错是本项目历史高频坑）：
#   均价 = amount/volume，必须与 current_price 一致，否则会被 ADR-0036 均价闸门误拦。
#   「未触板低吸票」amount=11.5e8 / vol=8.0e7 → 均价 14.375 < 现价 14.80 ✅ 放行
INFO = {
    "触板开板票": {"current_price": 15.87, "high": 16.32, "yesterday_close": 14.81,
                   "volume": 8.0e7, "amount": 12.5e8},   # 均价 15.625 < 15.87，只能被 0037 拦
    "未触板低吸票": {"current_price": 14.80, "high": 14.95, "yesterday_close": 14.60,
                     "volume": 8.0e7, "amount": 11.5e8},  # 均价 14.375，两道闸门均放行
    "低于均价票": {"current_price": 14.10, "high": 14.30, "yesterday_close": 14.60,
                   "volume": 8.0e7, "amount": 13.0e8},   # 均价 16.25 > 14.10 → 0036 拦
    "小额票": {"current_price": 14.60, "high": 14.70, "yesterday_close": 14.58,
               "volume": 5.0e6, "amount": 0.70e8},        # 均价 14.0 放行，额 0.7亿 → 0036 拦
}
def inst(cn):
    """当前生效的行情桩（每次 run 都重置，防上一个用例的 lambda 泄漏）。"""
    return _MAP.get(cn)


_MAP = dict(INFO)
ms.get_stock_detailed_sina = inst


def run(names, override=None, **kw):
    """每次调用都重设行情桩，避免用例间互相污染（本文件曾因此误报 2 个 FAIL）。"""
    global _MAP
    _MAP = dict(override) if override is not None else dict(INFO)
    cands = [{"name": n, "symbol": "60000%d.SH" % i} for i, n in enumerate(names)]
    return ms._plain_buy_items("14:40", cands, kw.get("avail", 500000.0),
                               kw.get("pos_pct", 0.30), kw.get("max_picks", 3),
                               "尾盘低吸", "无标的")


its = run(["触板开板票", "未触板低吸票"])
chk("开板票不产生 BUY", all(i["type"] != "BUY" or i["name"] != "触板开板票" for i in its))
ob = [i for i in its if i["type"] == "INFO" and i.get("name") == "触板开板票"]
chk("开板票产生 INFO 记录", len(ob) == 1)
chk("INFO 文案标明闸门", "触板后开板闸门跳过" in ob[0]["note"], ob[0]["note"] if ob else "")
chk("INFO 手数归零", ob[0]["buy_lots"] == 0 and ob[0]["buy_shares"] == 0)
buys = [i for i in its if i["type"] == "BUY"]
chk("未触板票正常派单", len(buys) == 1 and buys[0]["name"] == "未触板低吸票")
chk("正常派单手数>0", buys[0]["buy_lots"] > 0)

its = run(["未触板低吸票"])
chk("全放行时无 INFO 噪声", len([i for i in its if i["type"] == "INFO"]) == 0)

its = run(["触板开板票"])
# 闸门只做减法、不做补位（沿用 ADR-0036 既有约定）：全被拦时输出的是"为什么没派单"，
# 而不是笼统的 empty_note —— 对复盘更有价值，故此处断言 INFO 文案可解释。
chk("全被拦 → 输出可解释的 INFO(非笼统空提示)",
    len(its) == 1 and its[0]["type"] == "INFO" and "触板后开板闸门跳过" in its[0]["note"],
    str(its[:1]))

# 3) 与 ADR-0036 共存
print("\n== 3) 与 ADR-0036 可成交性闸门共存 ==")
its = run(["低于均价票"])
gd = [i for i in its if i["type"] == "INFO"]
chk("低于均价票被 ADR-0036 拦截", len(gd) == 1 and "可成交性闸门跳过" in gd[0]["note"],
    gd[0]["note"] if gd else "")

# 同时满足两条闸门 → 报 ADR-0037（顺序在前，证据更强）
DUAL = {"双触发票": {"current_price": 14.10, "high": 16.32, "yesterday_close": 14.81,
                     "volume": 8.0e7, "amount": 11.6e8}}   # 均价 14.5 > 14.10、且触板
its = run(["双触发票"], override=DUAL)
d = [i for i in its if i["type"] == "INFO"]
chk("双触发时报 ADR-0037(优先级更高)", len(d) == 1 and "触板后开板闸门跳过" in d[0]["note"],
    d[0]["note"] if d else "")

its = run(["小额票"])
d = [i for i in its if i["type"] == "INFO"]
chk("小额票被 ADR-0036 流动性拦截", len(d) == 1 and "流动性不足" in d[0]["note"],
    d[0]["note"] if d else "")

# 4) 降级安全
print("\n== 4) 降级安全：开板闸门取数不足 → 行为回到改造前 ==")
NOHIGH = {"触板开板票": {"current_price": 14.90, "yesterday_close": 14.60,
                         "volume": 8.0e7, "amount": 11.6e8}}   # 无 high；均价 14.5 < 14.90
its = run(["触板开板票"], override=NOHIGH)
chk("无 high → 放行并正常派单", len([i for i in its if i["type"] == "BUY"]) == 1,
    str(its[:1]))

# 5) 涨停票仍被上游 is_lu 硬挡（ADR-0037 不接管）
print("\n== 5) 已封板票仍由 is_lu 硬挡（与本闸门解耦）==")
LU = {"已封板票": {"current_price": 16.29, "high": 16.29, "yesterday_close": 14.81,
                   "volume": 8.0e7, "amount": 11.6e8}}
its = run(["已封板票"], override=LU)
chk("已封板票零 BUY", len([i for i in its if i["type"] == "BUY"]) == 0)
chk("已封板票不产生本闸门 INFO", len([i for i in its if i["type"] == "INFO"
                                      and "触板后开板闸门" in (i.get("note") or "")]) == 0)

print("\n===== RESULT: PASS=%d FAIL=%d =====" % (P, F))
if FAILS:
    print("FAILED:", " | ".join(FAILS))
sys.exit(1 if F else 0)

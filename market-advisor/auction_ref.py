#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auction_ref.py · 集合竞价参考因子（2026-10-21 新增）

【为什么建这个】
用户的观察：10:40 卖出对短线来说偏晚，且买入/卖出规则都没有集合竞价的参考。
查证后的结论（重要，别误读）：
  · **10:40 这个时点本身不晚** —— 它刻意避开 09:30-10:00 的剧烈波动与假突破
    （"买进的很大程度是开板了" 这个真实痛点），在开盘 70 分钟后才决策是对的。
  · **真正缺的是"卖出依据"**：decide_exit(pct, mkt_temp) 只有两个入参，
    只看 10:40 那一瞬的涨幅。09:25 到 10:40 之间这只票发生了什么，卖出决策完全看不见。
      · 高开6%后一路阴跌到 10:40 只剩 +0.5%  → 该跑
      · 平开后稳步走强到 10:40 涨 +0.5%      → 该留
    这两个票在 decide_exit 眼里**完全一样**（pct 都是 +0.5），但结论应该相反。
  · 而 09:25 的集合竞价数据（高开幅度、竞价量能）**算出来了却没人消费** ——
    get_opening_auction_signals() 的结果只写进报告文本，没有落盘给下游时段读。

【设计原则（铁律）】
  1. **只做参考因子，不改卖出时点**。10:40 仍是唯一卖出决策点，当日只派一次卖单，
     T+1 锁仓规则不变。绝不开第二个卖出口径（否则两处卖单打架）。
  2. **全程 fail-open**。竞价数据缺失 / 文件不存在 / 字段异常 → 全部降级为
     "无参考"，调用方行为与改造前**完全一致**，绝不因缺数据误杀持仓。
  3. **决策权仍在人**。本模块只提供参考分与文字说明，不自动下单、不改持仓。

【数据流】
  09:25  → build_auction_ref()  → 落盘 reports/auction_ref.json
  10:40  → load_auction_ref()   → decide_exit(..., auction_ref=...)  → 卖出判定微调
  13:40/14:40 → load_auction_ref() → rank 阶段打分加权（买入侧）

【强度分级】
  strong  强承接：高开 ≥3% 且竞价成交额 ≥1000万 → 早盘有真金白银进场
  weak    虚高/走弱：高开 ≥3% 但竞价量能不足，或高开后竞价转弱
  normal  中性：其余
  评分口径见 _score_one。
"""

import json
import os
from datetime import datetime

REPORTS_DIR = "/Users/williamli/Work/reports"
REF_FILE = os.path.join(REPORTS_DIR, "auction_ref.json")

# 竞价强度门槛（与 get_opening_auction_signals 的判定口径对齐：
# 那里用「高开≥3% 且竞价成交额>1500万」筛抢筹股，这里取更宽的 1000万作 strong 下限）
STRONG_GAP_PCT = 3.0          # 高开幅度下限（%）
STRONG_AMOUNT_YI = 0.1       # 竞价成交额下限（亿）
WEAK_AMOUNT_YI = 0.03        # 低于此值视为量能不足 → 虚高

STRONG = "strong"
NORMAL = "normal"
WEAK = "weak"

LABEL = {
    STRONG: "竞价强承接",
    NORMAL: "竞价中性",
    WEAK: "竞价虚高/量能不足",
}


def _norm_code(s):
    """6 位纯数字代码（与 market_scheduler._rg_norm 同口径）。"""
    return "".join(ch for ch in str(s or "") if ch.isdigit())


def _score_one(pct, turnover_yi, cap_yi=None):
    """单只票的竞价强度判定 → (level, reason)。

    pct         竞价涨跌幅（%），正=高开
    turnover_yi 竞价成交额（亿）
    cap_yi      流通市值（亿），用于判断量能占比

    口径说明：
      · 高开 ≥3% 且竞价额 ≥1000万 → strong（真金白银抢筹）
      · 高开 ≥3% 但竞价额 <300万    → weak（虚高，无量）
      · 高开 0~3% 且有量            → normal（温和）
      · 低开                          → weak（承接差）
    """
    try:
        pct = float(pct or 0)
        turnover_yi = float(turnover_yi or 0)
    except (TypeError, ValueError):
        return NORMAL, "竞价数据解析异常"

    if pct >= STRONG_GAP_PCT:
        if turnover_yi >= STRONG_AMOUNT_YI:
            return STRONG, "高开%.1f%%且竞价额%.2f亿（真金白银进场）" % (pct, turnover_yi)
        if turnover_yi < WEAK_AMOUNT_YI:
            return WEAK, "高开%.1f%%但竞价额仅%.3f亿（虚高无量）" % (pct, turnover_yi)
        return NORMAL, "高开%.1f%%，竞价额%.2f亿（承接中性）" % (pct, turnover_yi)
    if pct < 0:
        return WEAK, "低开%.1f%%（承接弱）" % pct
    if turnover_yi >= STRONG_AMOUNT_YI:
        return NORMAL, "小幅高开%.1f%%但竞价额%.2f亿（温和放量）" % (pct, turnover_yi)
    return NORMAL, "竞价平淡（高开%.1f%%，额%.3f亿）" % (pct, turnover_yi)


def build_auction_ref(stocks, date_str=None, log=print):
    """09:25 侧：把集合竞价画像落盘。

    stocks: [{name, symbol, pct, turnover_亿, cap_亿}, ...]
             （对应 get_opening_auction_signals 里 scored_stocks 的字段）
    返回落盘条数。异常 → 返回 0 且不写盘（fail-open，绝不产出半份坏数据）。
    """
    if not stocks:
        log("[auction] 无竞价样本，跳过落盘")
        return 0
    date_str = date_str or datetime.now().strftime("%Y%m%d")
    out = {}
    try:
        for s in stocks:
            sym = s.get("symbol")
            code = _norm_code(sym)
            if not code:
                continue
            pct = s.get("pct", 0)
            amt = s.get("turnover_亿", 0)
            level, reason = _score_one(pct, amt, s.get("cap_亿"))
            out[code] = {
                "name": s.get("name", ""),
                "symbol": sym,
                "pct": round(float(pct or 0), 2),
                "turnover_yi": round(float(amt or 0), 4),
                "level": level,
                "level_label": LABEL.get(level, level),
                "reason": reason,
                "plates": s.get("plates") or [],
            }
        if not out:
            log("[auction] 无有效样本，跳过落盘")
            return 0
        data = {
            "date": date_str,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "count": len(out),
            "items": out,
        }
        os.makedirs(REPORTS_DIR, exist_ok=True)
        tmp = REF_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, REF_FILE)   # 原子替换，避免下游读到半份文件
        log("[auction] 竞价画像已落盘 %s（%d 只，%s）"
            % (os.path.basename(REF_FILE), len(out), date_str))
        return len(out)
    except Exception as e:
        log("[auction] 落盘失败(不影响主流程): %s" % e)
        return 0


def load_auction_ref(date_str=None, log=print):
    """读取竞价参考 → {code: item}。任何异常 → 返回空 dict（调用方按"无参考"处理）。"""
    try:
        if not os.path.exists(REF_FILE):
            return {}
        with open(REF_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        want = date_str or datetime.now().strftime("%Y%m%d")
        got = str(data.get("date") or "")
        if got and got != want:
            # 不是当天的竞价画像 → 视为无参考（绝不用隔日数据误导今日决策）
            log("[auction] 竞价画像日期 %s ≠ 今日 %s → 视为无参考" % (got, want))
            return {}
        items = data.get("items")
        return items if isinstance(items, dict) else {}
    except Exception as e:
        log("[auction] 读取失败(视为无参考): %s" % e)
        return {}


def get_ref(symbol_or_name, ref_map=None, date_str=None):
    """取单只票的竞价参考 → dict 或 None（None = 无参考）。"""
    code = _norm_code(symbol_or_name)
    if not code:
        return None
    if ref_map is None:
        ref_map = load_auction_ref(date_str=date_str)
    if not ref_map:
        return None
    return ref_map.get(code)


def auction_hint(symbol_or_name, ref_map=None, date_str=None):
    """给人看的一句话参考（报告/弹窗用）。无参考 → 返回空串。"""
    it = get_ref(symbol_or_name, ref_map=ref_map, date_str=date_str)
    if not it:
        return ""
    return "%s（%s）" % (it.get("level_label") or "", it.get("reason") or "")


def adjust_exit_action(action, pct, ref, mkt_temp=50):
    """【核心】用竞价参考微调 10:40 的卖出动作。

    仅在 **中间档**（-2% ≤ pct < 4%）生效 —— 这正是 decide_exit 里
    「HOLD(温度≥55) / SELL_ALL(温度<55)」二选一的那一档，也是最需要上下文的区间。
    两端（pct≥9.5 锁仓晋级 / pct≤-5 止损）**不改动**，避免用竞价噪声干扰强信号。

    规则：
      · 竞价 strong（真金白银进场）+ 当前还站在均价之上 → HOLD 倾向，升级为 HOLD
      · 竞价 weak（虚高/低开/无量）→ 降级为 SELL_ALL（早盘已走坏，别留）
      · 竞价 normal / 无参考 → 保持原判定不变

    返回 (new_action, note)。note 为空表示未调整。
    """
    if not ref:
        return action, ""
    try:
        p = float(pct)
    except (TypeError, ValueError):
        return action, ""
    # 只在中间档生效
    if not (-2.0 <= p < 4.0):
        return action, ""
    level = ref.get("level")
    if level == STRONG:
        if action in ("SELL_ALL", "SELL_HALF"):
            return "HOLD", ("竞价强承接（%s）→ 判定由「%s」上调为锁仓观察"
                           % (ref.get("reason") or "", action))
    elif level == WEAK:
        # 竞价虚高/无量 → 一律下调为全清。
        # 【2026-10-21 修正】原先只降 HOLD，漏了 SELL_HALF ——
        # 而 SELL_HALF 恰好是"减仓一半"这一档，正是用户最在意的动作，
        # 漏掉它等于这个判据在关键档位不起作用。已实测补上。
        if action in ("HOLD", "SELL_HALF"):
            return "SELL_ALL", ("竞价虚高/承接弱（%s）→ 判定由「%s」下调为全清"
                                % (ref.get("reason") or "", action))
    return action, ""


def auction_score_bonus(symbol_or_name, ref_map=None, date_str=None):
    """买入侧打分加权：竞价强 → 加分；竞价虚高 → 减分。

    返回 (bonus, reason)。无参考 → (0, "")，调用方按无参考处理（不改变原有排序）。
    幅度刻意保守（±6 分，相对 signal 量级很小），避免单因子主导排序。
    """
    it = get_ref(symbol_or_name, ref_map=ref_map, date_str=date_str)
    if not it:
        return 0, ""
    level = it.get("level")
    if level == STRONG:
        return 6, "竞价强承接 +6"
    if level == WEAK:
        return -6, "竞价虚高/承接弱 -6"
    return 0, ""


def supplement_with_quotes(positions, quote_fn, date_str=None, log=print):
    """【2026-10-21 补覆盖缺口】用行情快照补齐「竞价时没涨停」的持仓股。

    背景：build_auction_ref 的样本来自涨停池 —— 只有竞价就封板的票在里面。
    持仓股若竞价只是小幅高开/平开（没涨停），就**拿不到任何竞价参考**，
    而这类票恰恰最需要参考（它们不会涨停，10:40 判定全靠 pct）。
    补法：09:25 时逐只取行情快照，此时
        open   = 集合竞价成交价（= 竞价开盘价）
        volume = 竞价成交量（开盘那一笔）
      → 竞价涨幅 = (open - 昨收) / 昨收，竞价额 = open × volume / 1e8。

    quote_fn(name) → dict（需含 open_price / yesterday_close / volume / current_price），
                   取不到返回 None → 该股跳过（fail-open）。
    返回补齐条数。
    """
    if not positions:
        return 0
    date_str = date_str or datetime.now().strftime("%Y%m%d")
    try:
        data = {}
        if os.path.exists(REF_FILE):
            with open(REF_FILE, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and str(d.get("date") or "") == date_str:
                data = dict(d.get("items") or {})
        added = 0
        for p in (positions or []):
            name = p.get("name") or ""
            code = _norm_code(p.get("symbol") or p.get("code") or "")
            if not name or not code:
                continue
            if code in data:
                continue          # 涨停池里已有（更权威），不覆盖
            try:
                q = quote_fn(name)
            except Exception:
                q = None
            if not q:
                continue
            try:
                op = float(q.get("open_price") or 0)
                pc = float(q.get("yesterday_close") or 0)
                vol = float(q.get("volume") or 0)
                cur = float(q.get("current_price") or 0)
                if op <= 0 or pc <= 0:
                    continue
                gap_pct = (op - pc) / pc * 100.0
                amt_yi = op * vol / 1e8
                # 竞价价就是开盘价时（09:25 竞价撮合完成），量能才有意义
                if abs(cur - op) > 1e-9 and vol <= 0:
                    continue
                level, reason = _score_one(gap_pct, amt_yi)
                data[code] = {
                    "name": name,
                    "symbol": p.get("symbol") or p.get("code") or "",
                    "pct": round(gap_pct, 2),
                    "turnover_yi": round(amt_yi, 4),
                    "level": level,
                    "level_label": LABEL.get(level, level),
                    "reason": reason + "（竞价开盘价 %.2f，昨收 %.2f）" % (op, pc),
                    "plates": [],
                    "source": "quote_supplement",
                }
                added += 1
            except Exception:
                continue
        if added:
            out = {
                "date": date_str,
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "count": len(data),
                "items": data,
            }
            os.makedirs(REPORTS_DIR, exist_ok=True)
            tmp = REF_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=2)
            os.replace(tmp, REF_FILE)
            log("[auction] 持仓股竞价参考补齐 %d 只（合计 %d）" % (added, len(data)))
        return added
    except Exception as e:
        log("[auction] 持仓补充失败(不影响主流程): %s" % e)
        return 0


def _selftest():
    ok = True

    def chk(cond, desc):
        nonlocal ok
        print(("  ✅ " if cond else "  ❌ ") + desc)
        if not cond:
            ok = False

    print("[selftest] 竞价强度判定")
    chk(_score_one(6.0, 0.5)[0] == STRONG, "高开6%+竞价0.5亿 → strong")
    chk(_score_one(6.0, 0.01)[0] == WEAK, "高开6%+竞价0.01亿 → weak（虚高无量）")
    chk(_score_one(-2.0, 0.5)[0] == WEAK, "低开-2% → weak")
    chk(_score_one(1.0, 0.05)[0] == NORMAL, "小幅高开+小量 → normal")

    print("\n[selftest] 卖出动作微调（只在 -2%~4% 生效）")
    strong_ref = {"level": STRONG, "reason": "高开6%且竞价0.5亿"}
    weak_ref = {"level": WEAK, "reason": "高开6%但竞价0.01亿"}
    a, n = adjust_exit_action("HOLD", 1.0, strong_ref)
    chk(a == "HOLD" and n == "", "strong + 本就 HOLD → 不变（已是最优）")
    a, n = adjust_exit_action("SELL_ALL", 1.0, strong_ref)
    chk(a == "HOLD" and n != "", "strong + 原判 SELL_ALL → 上调 HOLD")
    a, n = adjust_exit_action("HOLD", 1.0, weak_ref)
    chk(a == "SELL_ALL" and n != "", "weak + 原判 HOLD → 下调 SELL_ALL")
    a, n = adjust_exit_action("SELL_HALF", 1.0, weak_ref)
    chk(a == "SELL_ALL" and n != "",
        "weak + 原判 SELL_HALF → 下调 SELL_ALL（2026-10-21 修正：原只降 HOLD，漏了减半这一档）")
    a, n = adjust_exit_action("HOLD", 1.0, None)
    chk(a == "HOLD" and n == "", "无参考 → 完全不变（fail-open）")
    a, n = adjust_exit_action("HOLD", 8.0, weak_ref)
    chk(a == "HOLD" and n == "", "pct≥4%（强信号区）→ 不受竞价影响")
    a, n = adjust_exit_action("SELL_ALL", -6.0, strong_ref)
    chk(a == "SELL_ALL" and n == "", "pct≤-2%（止损区）→ 不受竞价影响")

    print("\n[selftest] 买入侧加权")
    chk(auction_score_bonus.__doc__ is not None, "函数可用")
    b, r = auction_score_bonus("000001", ref_map={"000001": strong_ref})
    chk(b == 6, "strong → +6")
    b, r = auction_score_bonus("000001", ref_map={"000001": weak_ref})
    chk(b == -6, "weak → -6")
    b, r = auction_score_bonus("000001", ref_map={})
    chk(b == 0 and r == "", "无参考 → 0 加权")

    print("\n[selftest] 落盘与读取往返")
    import tempfile
    global REF_FILE
    old = REF_FILE
    tmpd = tempfile.mkdtemp()
    REF_FILE = os.path.join(tmpd, "auction_ref.json")
    try:
        n = build_auction_ref([
            {"name": "测试股", "symbol": "sz000001", "pct": 6.0, "turnover_亿": 0.5, "cap_亿": 100},
            {"name": "弱票", "symbol": "sz000002", "pct": 6.0, "turnover_亿": 0.01, "cap_亿": 100},
        ], date_str="20261021", log=lambda *a, **k: None)
        chk(n == 2, "落盘 2 条")
        m = load_auction_ref("20261021", log=lambda *a, **k: None)
        chk(len(m) == 2, "读回 2 条")
        chk(m.get("000001", {}).get("level") == STRONG, "000001 → strong")
        chk(m.get("000002", {}).get("level") == WEAK, "000002 → weak")
        chk(load_auction_ref("20261020", log=lambda *a, **k: None) == {},
            "非当日数据 → 视为无参考（不用隔日数据）")
        chk(get_ref("000001", ref_map=m) is not None, "按代码取到")
        chk(get_ref("sz000001", ref_map=m) is not None, "带市场前缀也能取到")
        chk(get_ref("999999", ref_map=m) is None, "不存在的代码 → None")
    finally:
        REF_FILE = old

    print("\n[selftest] 持仓股补齐（覆盖缺口）")
    import tempfile, shutil
    old = REF_FILE
    tmpd = tempfile.mkdtemp()
    REF_FILE = os.path.join(tmpd, "auction_ref.json")
    try:
        build_auction_ref([{"name": "池内票", "symbol": "sh600825", "pct": 10.0,
                            "turnover_亿": 3.0, "cap_亿": 100}],
                          date_str="20261021", log=lambda *a, **k: None)
        def _fake_quote(name):
            return {"open_price": 56.0, "yesterday_close": 51.04,
                    "volume": 1_000_000, "current_price": 56.0}
        n = supplement_with_quotes([{"name": "持仓股", "symbol": "002929.SZ"}],
                                   _fake_quote, date_str="20261021",
                                   log=lambda *a, **k: None)
        chk(n == 1, "池外持仓股被补齐（%d 条）" % n)
        m = load_auction_ref("20261021", log=lambda *a, **k: None)
        chk(len(m) == 2, "合计 2 条")
        chk(m.get("002929", {}).get("source") == "quote_supplement",
            "补齐项标记 source=quote_supplement")
        chk(m.get("600825", {}).get("source") is None,
            "池内票未被覆盖（仍以涨停池数据为准）")
        n2 = supplement_with_quotes([{"name": "持仓股", "symbol": "002929.SZ"}],
                                    _fake_quote, date_str="20261021",
                                    log=lambda *a, **k: None)
        chk(n2 == 0, "重复补齐幂等（不重复添加）")
        chk(supplement_with_quotes([], _fake_quote, date_str="20261021",
                                   log=lambda *a, **k: None) == 0,
            "空持仓 → 0 条（不报错）")
    finally:
        REF_FILE = old
        shutil.rmtree(tmpd, ignore_errors=True)

    print("\n结果: " + ("全部通过 ✅" if ok else "存在失败 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    m = load_auction_ref()
    print("竞价参考 %d 条" % len(m))
    for code, it in list(m.items())[:10]:
        print("  %s %s | %s" % (code, it.get("name"), it.get("reason")))

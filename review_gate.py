#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""复盘闸门（ADR-0040）：把 T 日复盘结论翻译成 T+1 可执行的**交易约束表**。

【为什么需要它】
ADR-0039 交付的复盘模式（review_deep.py 六引擎）解决了「看清盘面」，
但它是一块**死数据** —— 结论只渲染在「复盘模式」tab，对第二天的买入通道零约束。
用户原话：「复盘决定了第二天啥可以买……而不是还和以前一样简单的功能堆砌」。
本模块补上缺失的那一环：**复盘 → 约束 → 次日操作 → 再复盘度量**。

【闭环四环】
  T 日盘后     复盘六引擎 → build_gate() → reports/trade_gate.json（写明 asof=次日）
  T+1 买入时段  调度器 _apply_review_gate() 读闸门 → 硬阻断/冷却/风格/高度/仓位上限
                → reports/trade_gate_applied.json（留痕：放行谁、剔除谁、为什么）
  T+1 确认台    工作台显示闸门横幅（本时段放行 N 只 / 剔除 M 只及原因）
  T+1 盘后      effectiveness() 度量昨日闸门剔除票的今日涨跌
                → reports/gate_effectiveness.json（闸门有效 or 误杀 → 自我校准）

【设计铁律】
  1. **fail-open**：闸门文件缺失 / 过期 / 格式错 / 任何异常 → 一律「不干预」，
     行为与接入前**完全一致**（最坏情况等于没有这个功能）。
  2. **单一写入者**：trade_gate.json 只由复盘侧写；trade_gate_applied.json 只由调度器写。
     两个进程不共享内存，只通过这两个 JSON 握手 —— 无耦合、可分别测试。
  3. **新鲜度硬校验**：只有 asof == 今天才生效。跨日 / 跨长假 / 跨周末的旧闸门一律作废。
  4. **逃生阀**：复盘判「退潮/冰点 / 亏钱效应扩散」，次日市场却明显转强
     （实时情绪温度 ≥ ESCAPE_TEMP）→ 硬阻断自动降级为警告，防踏空整段反弹。
     降级事实与原因写入留痕，绝不静默。
  5. 本模块**只读**回测库 / 持仓文件，绝不写（唯一写入对象是 reports/ 下自己的三个 JSON）。

【与调度器的关系】
调度器侧有一份**算法镜像**（market_scheduler._apply_review_gate）—— 原因：调度器
在盘中热路径上，不能 subprocess 起 python 再 import 本模块（秒级延迟 + akshare 导入开销）。
两边共用同一份 **数据契约**（本文件顶部的 _POLICY / 字段名），规则改动必须**同时**改两处。
`review_gate.py --selftest` 会对比两边对同一组输入的判定是否一致。
"""

import json
import os
import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = Path(__file__).parent
REPORTS = Path("/Users/williamli/Work/reports")
GATE_FILE = REPORTS / "trade_gate.json"            # 复盘写 → 调度器读
APPLIED_FILE = REPORTS / "trade_gate_applied.json"  # 调度器写 → 工作台读
EFFECT_FILE = REPORTS / "gate_effectiveness.json"   # 复盘写 → 工作台读
HISTORY_DIR = BASE / "data" / "gate_history"

GATE_VERSION = 2   # v2（ADR-0041）：focus[] 增补 board_days/pct —— 供调度器把「复盘重点」当供票源用
COOLING_DAYS = 5          # 追高/超配违规 → 冷却 N 个交易日
ESCAPE_TEMP = 60          # 逃生阀：次日实时温度 ≥ 此值 → 硬阻断降级为警告
STALE_WARN_TRADE_DAYS = 2  # 闸门 asof 落后今日超过 N 个交易日 → 工作台红条告警

# ============================================================================
# 核心映射表：情绪阶段 → 次日交易约束（这是本模块唯一「有观点」的地方）
# ----------------------------------------------------------------------------
# 取值依据：炒股养家六阶段 + 七步复盘法（见 ADR-0039 研究结论），
# 与 mood_engine.STAGE_RULES 的阶段判定同源，与 STAGE_POSITION 的仓位上限同值。
#
#   cap        总仓位上限(%)     —— 买入可用额度 = cap%×总权益 − 当前持仓市值
#   single     单票上限(%)       —— 传给 validate_order_plan_cash 的单票硬顶
#   picks      本时段最多只数     —— 冰点只允许试探 1 只
#   max_board  允许参与的最高连板 —— 「不接力高位连板」的量化落点（追高亏损的主因）
#   hard       硬阻断（清空买入候选）
# ============================================================================
_POLICY = {
    # 阶段:   (cap, single, picks, max_board, hard)
    "退潮": (10, 0, 0, 2, True),     # 亏钱效应扩散 → 空仓，不接力不低吸不抄底
    "冰点": (20, 8, 1, 3, False),    # 只做试探性低吸，1 只，单票 ≤8%
    "启动": (30, 15, 2, 2, False),   # 首板/1进2，不碰 3 板以上
    "确认": (50, 20, 3, 3, False),
    "发酵": (70, 25, 3, 5, False),   # 主升：可打梯队，但仍不碰 ≥6 板
    "加速": (50, 20, 3, 8, False),   # 高潮期：只卖不买为主，允许做最强但严控单票
    # 【2026-10-21 修 P0-3】分歧段 max_picks 2→3，单票 15→13（总仓上限 40% 保持不变）
    # 依据：10-20 首次 gate_effectiveness 显示 kept +4.70% vs dropped +3.88%（edge 仅 +0.82pp），
    # 且 5 次剔除理由**全部**是「本时段最多 2 只」，其中 3 只当天涨停（海洋王/科森/泰晶）——
    # 说明"名额不足"在扣分，而非"方向判错"。放宽到 3 只，同时把单票从 15% 降到 13%
    # 以维持 40% 段总仓上限不变（3×13=39 ≤ 40），不放大总风险敞口。
    "分歧": (40, 13, 3, 3, False),   # 高标独舞 → 弱转强/低吸反包，3 只，不碰 4 板以上
    "反包": (30, 15, 2, 3, False),
    "观察": (30, 10, 1, 3, False),   # 结构不明 → 少动
}
_DEFAULT_STAGE = "观察"

STAGE_ICON = {"退潮": "🌊", "冰点": "🧊", "启动": "🌱", "确认": "✅", "发酵": "🔥",
              "加速": "🚀", "分歧": "💥", "反包": "↩️", "观察": "⚪"}

# 违规类型 → 冷却天数（0 = 不冷却，只提醒）
_VIOLATION_COOLING = {
    "追高度": COOLING_DAYS,     # 买在当日最高点 → 该股 N 个交易日内不再派单
    "超配": COOLING_DAYS,       # 单一敞口 ≥25% → 冷却期内禁止加仓
    "低吸过早": 3,
}
# 违规类型 → 是否为「卖出未执行」提醒
_VIOLATION_PENDING_SELL = ("漏执行",)


# ---------------------------------------------------------------- 基础工具

def _norm_code(s):
    """6 位纯数字代码（兼容 000066 / 000066.SZ / sz000066）。跨文件匹配必须走这里 ——
    pending_sell 用 6 位、操作池候选用 000066.SZ，不归一必然匹配失败。"""
    return "".join(ch for ch in str(s or "") if ch.isdigit())


def _to_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _to_float(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def is_trading_day(d=None):
    """交易日判定（复用 review_deep，akshare 官方日历 + 周末降级）。"""
    try:
        import review_deep
        return bool(review_deep.is_trading_day(d or date.today()))
    except Exception:
        d = d or date.today()
        return d.weekday() < 5


def next_trade_date(from_d=None, include_today=False):
    """下一个交易日（from_d 之后；include_today=True 时含当天）。"""
    d = from_d or date.today()
    if not include_today:
        d = d + timedelta(days=1)
    for _ in range(30):
        if is_trading_day(d):
            return d
        d = d + timedelta(days=1)
    return d


def add_trade_days(from_d, n):
    """从 from_d 起往后数 n 个交易日（n=5 → 第 5 个交易日截止）。"""
    d = from_d
    left = max(0, _to_int(n))
    while left > 0:
        d = d + timedelta(days=1)
        if is_trading_day(d):
            left -= 1
    return d


# ---------------------------------------------------------------- 闸门生成

def _cooling_from_violations(violations, asof_d):
    """纪律违规 → 冷却名单（带截止交易日）。同一标的取最长冷却。"""
    cool = {}
    for v in (violations or []):
        kind = v.get("kind") or ""
        days = _VIOLATION_COOLING.get(kind)
        if not days:
            continue
        code = _norm_code(v.get("symbol") or v.get("name"))
        if not code:
            continue
        until = add_trade_days(asof_d, days).isoformat()
        prev = cool.get(code)
        if prev and prev.get("until", "") >= until:
            continue
        cool[code] = {
            "name": v.get("name") or "",
            "symbol": v.get("symbol") or code,
            "reason": "%s（%s）" % (kind, (v.get("text") or "")[:60]),
            "until": until,
            "days": days,
        }
    return sorted(cool.values(), key=lambda x: x.get("until", ""), reverse=True)


def _pending_sell_from_violations(violations):
    out = []
    for v in (violations or []):
        if (v.get("kind") or "") not in _VIOLATION_PENDING_SELL:
            continue
        out.append({
            "name": v.get("name") or "",
            "symbol": v.get("symbol") or "",
            "action": v.get("action") or "",
            "lots": v.get("lots") or 0,
            "text": v.get("text") or "",
        })
    return out


def build_gate(review, mode=None, asof=None, write=True):
    """复盘六引擎输出 → 次日交易约束表（dict）。

    review  = review_deep.review_deep() 的返回值
    mode    = "enforce"（默认，真拦截）| "shadow"（只记录不拦截）
    asof    = 生效交易日（默认 = 下一个交易日）
    write   = 是否落盘 trade_gate.json
    """
    review = review or {}
    cycle = review.get("cycle") or {}
    loss = review.get("loss") or {}
    disc = review.get("discipline") or {}
    wind = review.get("wind") or []
    plan = review.get("plan") or {}

    stage = cycle.get("stage") or _DEFAULT_STAGE
    if stage not in _POLICY:
        stage = _DEFAULT_STAGE
    cap, single, picks, max_bd, hard = _POLICY[stage]
    sig = cycle.get("signals") or {}

    asof_d = None
    if asof:
        try:
            asof_d = date.fromisoformat(str(asof)[:10])
        except ValueError:
            asof_d = None
    if asof_d is None:
        asof_d = next_trade_date()

    # ---- 硬阻断升格：亏钱效应等级 ≥4（方法论：扩散期一律不做）----
    hard_reasons = []
    if hard:
        hard_reasons.append("%s段（%s）" % (stage, (cycle.get("law") or "")[:80]))
    try:
        if _to_int(loss.get("level")) >= 4:
            hard = True
            hard_reasons.append("亏钱效应等级 %s/5：%s" % (
                loss.get("level"), (loss.get("verdict") or "")[:60]))
    except Exception:
        pass
    # 退潮/冰点 且 跌停 ≥50 → 一并硬阻断
    try:
        if stage in ("退潮", "冰点") and _to_int(sig.get("dt")) >= 50 and not hard:
            hard = True
            hard_reasons.append("跌停 %s 家，亏钱效应全面扩散" % sig.get("dt"))
    except Exception:
        pass

    # ---- 冷却名单 / 未执行卖出提醒 ----
    violations = disc.get("violations") or []
    cooling = _cooling_from_violations(violations, asof_d)
    pending_sell = _pending_sell_from_violations(violations)

    # ---- 风向标重点池（复盘选出的 8 只，次日命中即置顶）----
    focus = []
    for w in wind[:8]:
        code = _norm_code(w.get("code") or w.get("symbol"))
        if not code:
            continue
        focus.append({
            "name": w.get("name") or "",
            "symbol": w.get("symbol") or code,
            "code": code,
            "kind": w.get("kind") or "",
            "kind_label": w.get("kind_label") or "",
            "one_word": bool(w.get("one_word")),
            # ADR-0041：v2 起带上连板数与涨幅 —— 调度器要把「非一字板且板数达标」的
            # 风向标反过来当【供票源】用（修复"复盘只能拦不能推"），必须知道板数。
            "board_days": _to_int(w.get("lianban"), 1) or 1,
            "pct": w.get("pct"),
            "why": (w.get("why") or "")[:100],
        })

    gate = {
        "version": GATE_VERSION,
        "mode": mode or "enforce",
        "asof": asof_d.isoformat(),
        "source_review_date": (review.get("time") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"))[:10],
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        # —— 阶段与仓位 ——
        "stage": stage,
        "stage_icon": STAGE_ICON.get(stage, "⚪"),
        "reason": (cycle.get("law") or "")[:200],
        "position_cap_pct": cap,
        "single_pos_cap_pct": single,
        "max_picks": picks,
        # —— 风格闸门 ——
        "max_board_days": max_bd,
        "deny_styles": (["高位连板"] if max_bd < 5 else []),
        # —— 硬阻断 ——
        "hard_block": bool(hard),
        "hard_block_reason": " ；".join(hard_reasons) if hard else "",
        # —— 名单 ——
        "cooling": cooling,
        "pending_sell": pending_sell,
        "focus": focus,
        # —— 复盘原始信号（供避难阀与展示）——
        "stage_signals": {"zt": sig.get("zt"), "dt": sig.get("dt"),
                          "max_board": sig.get("max_board"), "ratio": sig.get("ratio"),
                          "zb_rate": sig.get("zb_rate"), "gap": sig.get("gap")},
        "loss_level": loss.get("level"),
        "avoid": (plan.get("avoid") or [])[:4],
        "checklist": (plan.get("checklist") or [])[:5],
    }

    if write:
        GH_ok = _write_json(GATE_FILE, gate)
        if GH_ok:
            try:
                HISTORY_DIR.mkdir(parents=True, exist_ok=True)
                _write_json(HISTORY_DIR / ("%s.json" % asof_d.isoformat()),
                            {"gate": gate, "applied": None})
            except Exception as e:
                print("[gate] 历史归档失败(不影响主流程): %s" % e)
    return gate


def _write_json(path, obj):
    """原子写（临时文件 + replace），避免调度器读到半截 JSON。"""
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception as e:
        print("[gate] 写入失败 %s: %s" % (path, e))
        return False


# ---------------------------------------------------------------- 闸门读取

def load_gate(today=None, strict=True):
    """读取当日生效闸门。返回 dict 或 None（None ⇒ 调用方必须 fail-open）。

    strict=True：asof 必须等于 today（默认今天），否则视为过期作废。
    """
    try:
        d = json.loads(GATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(d, dict) or not d.get("asof"):
        return None
    t = (today or date.today())
    t = t.isoformat() if hasattr(t, "isoformat") else str(t)[:10]
    if strict and str(d["asof"])[:10] != t:
        return None
    return d


def stale_reason(today=None):
    """闸门存在但不可用时的原因（供日志/UI 说明，绝不静默）。"""
    if not GATE_FILE.exists():
        return "无闸门文件（尚未做过复盘）"
    try:
        d = json.loads(GATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        return "闸门文件损坏: %s" % e
    t = (today or date.today())
    t = t.isoformat() if hasattr(t, "isoformat") else str(t)[:10]
    if str(d.get("asof"))[:10] != t:
        return "闸门 asof=%s ≠ 今日 %s（已作废）" % (d.get("asof"), t)
    return ""


# ---------------------------------------------------------------- 闸门应用

def _drop(c, reason):
    return {
        "name": c.get("name") or c.get("stock_chi_name") or "",
        "symbol": c.get("symbol") or c.get("code") or "",
        "reason": reason,
    }


def apply(stage, candidates, gate=None, ctx=None):
    """把闸门套到候选列表上 → (kept, drops, meta)。

    **纯函数**：不读文件、不写文件、不联网，只做判定。调度器侧是同算法的独立实现。
    ctx: {"temperature": 实时情绪温度, "equity": 总权益, "cash": 可用现金}
    """
    cands = [dict(c) for c in (candidates or [])]
    ctx = dict(ctx or {})
    meta = {"applied": False, "stage": stage, "avail_cap": None,
            "kept": 0, "dropped": 0, "notes": [], "gate_asof": None}

    if not gate or not isinstance(gate, dict):
        meta["notes"].append("无闸门（fail-open，行为与接入前一致）")
        return cands, [], meta

    meta["gate_asof"] = gate.get("asof")
    meta["gate_stage"] = gate.get("stage")
    meta["hard_block"] = bool(gate.get("hard_block"))
    meta["position_cap_pct"] = gate.get("position_cap_pct")

    if str(gate.get("mode") or "enforce") != "enforce":
        meta["notes"].append("闸门为 shadow 模式：仅记录判定，不拦截")
        meta["shadow_would_drop"] = [d["name"] for d in _judge(cands, gate, ctx)[1]]
        return cands, [], meta

    # ---- 逃生阀：复盘判防守，次日却明显转强 → 降级为警告 ----
    hard = bool(gate.get("hard_block"))
    temp = _to_float(ctx.get("temperature"))
    if hard and temp is not None and temp >= ESCAPE_TEMP:
        hard = False
        note = ("逃生阀：今日实时情绪温度 %.1f ≥ %d，复盘判「%s」与实时结构背离 → "
                "硬阻断降级为警告（仍受冷却/高度/仓位上限约束）"
                % (temp, ESCAPE_TEMP, gate.get("stage")))
        meta["notes"].append(note)
        meta["escape"] = True

    if hard:
        why = gate.get("hard_block_reason") or gate.get("reason") or "复盘判定禁止开新仓"
        drops = [_drop(c, "复盘硬阻断：%s" % why) for c in cands]
        meta.update({"applied": True, "kept": 0, "dropped": len(drops)})
        return [], drops, meta

    kept, drops = _judge(cands, gate, ctx)
    meta["applied"] = True

    # ---- 总仓位上限：可用额度 = cap% × 总权益 − 持仓市值 ----
    cap = _to_float(gate.get("position_cap_pct"))
    eq = _to_float(ctx.get("equity"))
    if cap is not None and eq:
        held_mv = _to_float(ctx.get("positions_mv"))
        if held_mv is None:
            cash = _to_float(ctx.get("cash")) or 0.0
            held_mv = max(eq - cash, 0.0)          # 无逐日盯市持仓市值时用成本近似
        allowance = cap / 100.0 * eq - held_mv
        meta["position_now_pct"] = round(held_mv / eq * 100, 2) if eq else None
        meta["allowance"] = round(allowance, 2)
        if allowance <= 0:
            for c in kept:
                drops.append(_drop(c, (
                    "复盘仓位上限：当前仓位 %.1f%% 已达/超 %s 段上限 %g%%，"
                    "买入额度缺口 ¥%.0f，需先减仓再加仓"
                    % (meta.get("position_now_pct") or 0.0, gate.get("stage"),
                       cap, abs(allowance)))))
            kept = []
        else:
            meta["avail_cap"] = round(min(allowance, _to_float(ctx.get("cash")) or allowance), 2)

    meta["kept"] = len(kept)
    meta["dropped"] = len(drops)
    return kept, drops, meta


def _judge(cands, gate, ctx=None):
    """候选级判定（冷却 → 高度上限 → 只数上限）。不涉及金额。"""
    drops = []
    cooling = {_norm_code(x.get("symbol") or x.get("code") or x.get("name")): x
               for x in (gate.get("cooling") or [])}
    focus = {_norm_code(x.get("symbol") or x.get("code") or x.get("name")): x
             for x in (gate.get("focus") or [])}
    max_bd = _to_int(gate.get("max_board_days"), 0)
    kept = []
    for c in cands:
        key = _norm_code(c.get("symbol") or c.get("code") or c.get("name"))
        cd = cooling.get(key)
        if cd:
            drops.append(_drop(c, "复盘冷却名单（至 %s）：%s" % (cd.get("until"), cd.get("reason"))))
            continue
        bd = _to_int(c.get("board_days") or c.get("lianban"), 0)
        if max_bd and bd > max_bd:
            drops.append(_drop(c, ("复盘风格闸门：%d 连板 > 「%s」段允许的最高 %d 板"
                                   "（不接力高位连板）" % (bd, gate.get("stage"), max_bd))))
            continue
        f = focus.get(key)
        if f:
            c["_review_focus"] = True
            c["_review_focus_label"] = "复盘重点·%s" % (f.get("kind_label") or "风向标")
        kept.append(c)
    picks = _to_int(gate.get("max_picks"), 0)
    if picks and len(kept) > picks:
        for c in kept[picks:]:
            drops.append(_drop(c, "复盘只数上限：「%s」段本时段最多 %d 只" % (gate.get("stage"), picks)))
        kept = kept[:picks]
    return kept, drops


# ---------------------------------------------------------------- 效能度量

def effectiveness(prev_gate=None, today=None, quotes=None):
    """度量「上一次生效的闸门」到底帮了还是害了 —— 这是闭环的最后一环。

    算法：取 trade_gate_applied.json 记录的剔除票 / 放行票，分别算今日涨跌幅均值。
      剔除组显著为负 → 闸门有效（避开了亏损）
      剔除组为正     → 闸门误杀（放走了盈利）→ 需要校准阈值
    """
    today = today or date.today()
    ts = today.isoformat() if hasattr(today, "isoformat") else str(today)[:10]
    if prev_gate is None:
        try:
            # Path(...) 兜一层：调用方可能把这三个路径常量改成 str（测试/外部脚本），
            # 直接 .read_text() 会 AttributeError 并被下面 except 静默吞成 None。
            prev_gate = json.loads(Path(APPLIED_FILE).read_text(encoding="utf-8"))
        except Exception:
            return None
    if not isinstance(prev_gate, dict):
        return None

    stages = prev_gate.get("stages") or []
    if not stages:
        return None
    codes = set()
    for s in stages:
        for x in (s.get("drops") or []) + (s.get("kept") or []):
            c = _norm_code(x.get("symbol") or x.get("name"))
            if c:
                codes.add(c)
    if not codes:
        return None

    if quotes is None:
        try:
            import review_deep
            quotes = review_deep.qt_batch(sorted(codes))
        except Exception as e:
            return {"date": ts, "error": "行情获取失败: %s" % e}

    def _pct(x):
        c = _norm_code(x.get("symbol") or x.get("name"))
        q = (quotes or {}).get(c) or {}
        v = q.get("pct")
        return _to_float(v), q.get("name") or x.get("name")

    drop_rows, keep_rows = [], []
    for s in stages:
        for x in (s.get("drops") or []):
            p, nm = _pct(x)
            if p is not None:
                drop_rows.append({"name": x.get("name") or nm, "pct": p, "reason": x.get("reason")})
        for x in (s.get("kept") or []):
            p, nm = _pct(x)
            if p is not None:
                keep_rows.append({"name": x.get("name") or nm, "pct": p})

    if not drop_rows and not keep_rows:
        return None
    d_avg = round(sum(r["pct"] for r in drop_rows) / len(drop_rows), 2) if drop_rows else None
    k_avg = round(sum(r["pct"] for r in keep_rows) / len(keep_rows), 2) if keep_rows else None
    miss = [r for r in drop_rows if r["pct"] >= 3.0]
    if d_avg is None:
        verdict = "本轮无剔除项，闸门未起作用"
    elif d_avg <= -1.0:
        verdict = "闸门有效：剔除组均值 %.2f%%，避开亏损" % d_avg
    elif d_avg >= 1.0:
        verdict = "⚠️ 闸门可能误杀：剔除组均值 %+.2f%%（放走了盈利）" % d_avg
    else:
        verdict = "闸门中性：剔除组均值 %+.2f%%，无显著影响" % d_avg

    # ===== 【2026-10-21 修 P0-3】按剔除原因分组统计 =====
    # 痛点：10-20 首次 effectiveness 显示 edge 仅 +0.82pp，且 5 次剔除理由
    # **全部**是「本时段最多 2 只」，其中 3 只当天涨停 —— 说明问题不是"方向判错"，
    # 而是"名额不足"。但原先只有总体均值，看不出剔除究竟因何而起，
    # 于是"名额不足"被误读为"闸门判错方向"，导致误改策略。
    # 现在按 drop_reason 归类分组，各组独立算均值与命中数，一眼看出是哪条规则在起作用。
    _REASON_RULES = [
        ("只数上限", ("只数上限", "最多")),
        ("风格闸门", ("风格闸门", "连板", "高度")),
        ("冷却名单", ("冷却",)),
        ("仓位上限", ("仓位上限", "额度")),
        ("硬阻断", ("硬阻断", "禁止开新仓")),
        ("伪强势距高", ("伪强势", "距高", "距当日最高")),
        ("风险闸门", ("系统性风险", "风险闸门")),
    ]

    def _reason_group(reason):
        r = str(reason or "")
        for label, keys in _REASON_RULES:
            if any(k in r for k in keys):
                return label
        return "其他"

    by_reason = {}
    for r in drop_rows:
        g = _reason_group(r.get("reason"))
        grp = by_reason.setdefault(g, {"n": 0, "sum_pct": 0.0, "items": []})
        grp["n"] += 1
        grp["sum_pct"] += float(r["pct"])
        grp["items"].append({"name": r.get("name"), "pct": r["pct"],
                             "reason": r.get("reason")})
    for g, grp in by_reason.items():
        grp["avg_pct"] = round(grp["sum_pct"] / grp["n"], 2) if grp["n"] else None
        # 组内命中数 = 涨超 3%（说明这一组规则可能偏严/误杀）
        grp["hit_n"] = sum(1 for it in grp["items"] if float(it["pct"]) >= 3.0)
        grp["miss_n"] = sum(1 for it in grp["items"] if float(it["pct"]) <= -3.0)
        del grp["sum_pct"]

    out = {
        "date": ts,
        "gate_asof": prev_gate.get("asof"),
        "gate_stage": prev_gate.get("stage"),
        "dropped_n": len(drop_rows), "kept_n": len(keep_rows),
        "dropped_avg_pct": d_avg, "kept_avg_pct": k_avg,
        "edge": (round(k_avg - d_avg, 2) if (k_avg is not None and d_avg is not None) else None),
        "miss": miss,
        "dropped": drop_rows, "kept": keep_rows,
        # 【2026-10-21 修 P0-3】按剔除原因分组（区分「方向判错」与「名额不足」）
        "by_reason": by_reason,
        "verdict": verdict,
    }
    # verdict 追加分组提示：只数上限占比高 → 提示是名额问题而非方向问题
    _grp_top = ""
    try:
        if by_reason:
            _top = max(by_reason.items(), key=lambda kv: kv[1]["n"])
            if _top[0] == "只数上限" and _top[1]["n"] == len(drop_rows) and drop_rows:
                _grp_top = ("｜注意：全部剔除均因「只数上限」（非方向判错），"
                            "该组平均 %+.2f%%、命中涨停 %d 只 → 应优先调整名额而非阈值"
                            % (_top[1].get("avg_pct") or 0.0, _top[1].get("hit_n") or 0))
    except Exception:
        _grp_top = ""
    if _grp_top:
        out["verdict"] = out["verdict"] + _grp_top
    _write_json(EFFECT_FILE, out)
    return out


# ---------------------------------------------------------------- 展示

def summary_text(gate):
    if not gate:
        return "复盘闸门：无（未做过复盘或已过期）"
    icon = gate.get("stage_icon") or "⚪"
    parts = ["%s 复盘闸门 %s段 · 仓位上限 %g%% · 单票 ≤%g%% · 最多 %g 只 · 不碰 %d 板以上"
             % (icon, gate.get("stage"), gate.get("position_cap_pct") or 0,
                gate.get("single_pos_cap_pct") or 0, gate.get("max_picks") or 0,
                _to_int(gate.get("max_board_days")))]
    if gate.get("hard_block"):
        parts.append("⛔ 硬阻断：%s" % (gate.get("hard_block_reason") or "")[:70])
    if gate.get("cooling"):
        parts.append("冷却 %d 只：%s" % (len(gate["cooling"]),
                                      "、".join(x.get("name") or "" for x in gate["cooling"][:4])))
    if gate.get("pending_sell"):
        parts.append("⚠ 昨日卖出未执行 %d 笔：%s" % (
            len(gate["pending_sell"]),
            "、".join("%s %s" % (x.get("name"), x.get("action")) for x in gate["pending_sell"][:3])))
    return " ｜ ".join(parts)


def load_applied():
    try:
        return json.loads(Path(APPLIED_FILE).read_text(encoding="utf-8"))
    except Exception:
        return None


def status(today=None):
    """给工作台用的一站式状态：gate + applied + effectiveness + 可用性说明。"""
    t = today or date.today()
    g = load_gate(t)
    return {
        "today": (t.isoformat() if hasattr(t, "isoformat") else str(t)[:10]),
        "gate": g,
        "gate_active": bool(g and g.get("stage")),
        "inactive_reason": "" if g else stale_reason(t),
        "summary": summary_text(g),
        "applied": load_applied(),
        "effectiveness": _read_effect(),
    }


def _read_effect():
    try:
        return json.loads(Path(EFFECT_FILE).read_text(encoding="utf-8"))
    except Exception:
        return None


# ---------------------------------------------------------------- CLI

def _selftest():
    """契约自检：同一组输入下，本模块 apply() 与调度器内联实现是否同判定。

    调度器不可导入（依赖 akshare），故只做「契约字段 + 本模块行为」自检，
    并打印调度器侧需要保持一致的规则清单。

    【隔离铁律 2026-10-06】selftest 会调用 apply()/effectiveness()，这两个函数会
    **写盘**（APPLIED_FILE / EFFECT_FILE）。此前直接跑会把 reason="x" 的合成数据
    写进真实 reports/gate_effectiveness.json，工作台就会展示假的闸门效能。
    故先把全部落盘目标重定向到临时目录，结束后还原。
    """
    import tempfile
    global GATE_FILE, APPLIED_FILE, EFFECT_FILE, HISTORY_DIR
    _bak = (GATE_FILE, APPLIED_FILE, EFFECT_FILE, HISTORY_DIR)
    _tmpd = Path(tempfile.mkdtemp(prefix="rg_selftest_"))
    GATE_FILE = _tmpd / "trade_gate.json"
    APPLIED_FILE = _tmpd / "trade_gate_applied.json"
    EFFECT_FILE = _tmpd / "gate_effectiveness.json"
    HISTORY_DIR = _tmpd / "gate_history"
    ok = True
    g = {
        "version": GATE_VERSION, "mode": "enforce", "asof": "2099-01-01", "stage": "分歧",
        "position_cap_pct": 40, "single_pos_cap_pct": 15, "max_picks": 2, "max_board_days": 3,
        "hard_block": False, "hard_block_reason": "",
        "cooling": [{"name": "中国长城", "symbol": "000066.SZ", "until": "2099-01-02",
                     "reason": "追高度（买在当日最高点）"}],
        "pending_sell": [], "focus": [{"name": "新华传媒", "code": "600825",
                                       "kind_label": "空间龙头"}],
        "stage_signals": {}, "loss_level": 1, "avoid": [], "checklist": [],
    }
    cands = [
        {"name": "中国长城", "symbol": "000066.SZ", "board_days": 1},   # 冷却 → 剔除
        {"name": "新华传媒", "symbol": "600825.SH", "board_days": 7},   # 超高度 → 剔除
        {"name": "襄阳轴承", "symbol": "000678.SZ", "board_days": 4},   # 超高度 → 剔除
        {"name": "泰诺麦博-U", "symbol": "688288.SH", "board_days": 1},  # 保留 + 超只数上限
        {"name": "蓝丰生化", "symbol": "002513.SZ", "board_days": 1},    # 保留
    ]
    cands_focus = [
        {"name": "新华传媒", "symbol": "600825.SH", "board_days": 2},   # 命中风向标
        {"name": "泰诺麦博-U", "symbol": "688288.SH", "board_days": 1},  # 未命中
    ]
    kept, drops, meta = apply("13:40", cands, g, {"temperature": 30, "equity": 500000, "cash": 450000})
    exp_drop = {"中国长城", "新华传媒", "襄阳轴承"}

    def _chk(cond, msg):
        nonlocal ok
        print(("  ✅ " if cond else "  ❌ ") + msg)
        if not cond:
            ok = False

    print("[selftest] 冷却/风格/只数三闸门")
    _chk({d["name"] for d in drops} == exp_drop,
         "剔除集合 = %s（实得 %s）" % (sorted(exp_drop), sorted(d["name"] for d in drops)))
    _chk(len(kept) == 2, "只数上限 2 生效（实得 %d）" % len(kept))
    _chk(meta.get("avail_cap") == 150000.0,
         "仓位额度 = cap40%%×50万 − 持仓5万 = 150000（实得 %s）" % meta.get("avail_cap"))

    print("[selftest] 风向标打标")
    kf, df, mf = apply("13:40", cands_focus, g, {"temperature": 30, "equity": 500000, "cash": 450000})
    _chk(any(c.get("_review_focus") for c in kf), "命中风向标的候选被打标 _review_focus")
    _chk(all(not c.get("_review_focus") for c in kf if c["name"] == "泰诺麦博-U"),
         "未命中风向标的候选不打标")

    print("[selftest] 总仓位上限（当前仓位已超上限 → 禁止加仓）")
    kc, dc, mc = apply("13:40", cands, g, {"temperature": 30, "equity": 500000, "cash": 260000})
    _chk(len(kc) == 0 and len(dc) == 5, "持仓 48%% > 分歧段上限 40%% → 全部剔除并给出缺口")
    _chk(any("需先减仓" in d["reason"] for d in dc), "剔除原因含「需先减仓」")

    print("[selftest] 硬阻断 + 逃生阀")
    g2 = dict(g, hard_block=True, hard_block_reason="退潮段：亏钱效应扩散", stage="退潮")
    k2, d2, m2 = apply("09:25", cands, g2, {"temperature": 30, "equity": 500000, "cash": 450000})
    _chk(len(k2) == 0 and len(d2) == len(cands), "硬阻断清空全部候选")
    k3, d3, m3 = apply("09:25", cands, g2, {"temperature": 68, "equity": 500000, "cash": 450000})
    _chk(len(k3) > 0 and m3.get("escape"), "逃生阀：温度 68 ≥ 60 → 降级放行")

    print("[selftest] fail-open")
    k4, d4, m4 = apply("13:40", cands, None, {})
    _chk(k4 == cands and not d4 and not m4["applied"], "无闸门 → 原样放行")
    k5, d5, m5 = apply("13:40", cands, dict(g, mode="shadow"), {})
    _chk(k5 == cands and not d5, "shadow 模式 → 不拦截")

    print("[selftest] 符号归一")
    _chk(_norm_code("000066") == _norm_code("000066.SZ") == _norm_code("sz000066"),
         "6 位代码归一一致")

    print("\n调度器侧必须与此保持一致的规则（改动需同步两处）：")
    for ln in ("1) mode != enforce → 不拦截", "2) hard_block → 清空（除非 temperature ≥ %d）" % ESCAPE_TEMP,
               "3) cooling 命中 → 剔除", "4) board_days > max_board_days → 剔除",
               "5) 只数 > max_picks → 截断", "6) 额度 ≤0 → 清空；否则 avail = min(额度, 现金)",
               "7) 任何异常 → 放行"):
        print("   " + ln)
    print("\n结果: " + ("全部通过 ✅" if ok else "存在失败 ❌"))
    # 还原真实路径并清理临时目录（防止全局常量被本次自检永久改写）
    (GATE_FILE, APPLIED_FILE, EFFECT_FILE, HISTORY_DIR) = _bak
    try:
        shutil.rmtree(_tmpd, ignore_errors=True)
    except Exception:
        pass
    print("[selftest] 落盘目标已还原（本次自检写的是临时目录，未污染真实 reports/）")
    return 0 if ok else 1


def gate_health(today=None):
    """闸门健康度自检（供工作台红条告警 + 盘前巡检使用）。

    【2026-10-19 新增】修复「闸门陈旧 7 个交易日却无人知晓」——断裂期间
    scheduler 每天打 fail-open 日志，但用户界面完全无感，于是复盘报告里
    反复出现「闸门链断裂 N 日」的文字结论，却没有任何主动提示。

    返回 dict:
      level      "ok" | "warn" | "error"
      asof       闸门声明的生效日
      stale_days 闸门声明生效日距今的交易日数（陈旧度）
      message    人话结论，可直接显示
    """
    today = today or date.today()
    out = {"level": "ok", "asof": None, "stale_days": 0, "message": "", "gate": None}

    # 【语义澄清·重要】闸门 asof = 「它约束的那一天」，由 T 日**盘后**生成、供 T+1 使用。
    # 因此两种状态都是健康的：
    #   a) asof == 今日            → 盘中，正在约束今天的买入
    #   b) asof == 下一个交易日     → 盘后，明天 Already备好（最理想）
    # 只有「两者都不是」（陈旧 / 缺失 / 损坏）才是异常。
    g_today = load_gate(today, strict=True)
    # 【2026-10-21 修】今日副本（trade_gate_current.json）也算"今日闸门已就绪"
    if not g_today:
        try:
            cur = REPORTS / "trade_gate_current.json"
            if cur.exists():
                d = json.loads(cur.read_text(encoding="utf-8"))
                if isinstance(d, dict) and str(d.get("asof"))[:10] == today.isoformat():
                    g_today = d
        except Exception:
            pass
    g_next = None
    try:
        nxt = next_trade_date(today, include_today=False)
        g_next = load_gate(nxt, strict=True)
    except Exception as e:
        out["_next_err"] = str(e)

    if g_today:
        out["gate"] = g_today
        out["asof"] = g_today.get("asof")
        out["message"] = "复盘闸门生效中（asof=%s · %s段 · %s）" % (
            out["asof"], g_today.get("stage"),
            "约束" if g_today.get("mode") == "enforce" else "仅观察")
        return out

    if g_next:
        out["gate"] = g_next
        out["asof"] = g_next.get("asof")
        out["message"] = "复盘闸门已备妥明日（asof=%s · %s段 · %s）" % (
            out["asof"], g_next.get("stage"),
            "约束" if g_next.get("mode") == "enforce" else "仅观察")
        return out

    # 都不可用 → 陈旧 / 缺失 / 损坏，报错并说明影响
    reason = stale_reason(today)
    out["level"] = "error"
    out["asof"] = None
    try:
        raw = json.loads(GATE_FILE.read_text(encoding="utf-8"))
        out["asof"] = raw.get("asof")
        a = date.fromisoformat(str(out["asof"])[:10])
        stale = 0
        cur = today
        while cur > a and stale < 60:
            cur = cur - timedelta(days=1)
            if is_trading_day(cur):
                stale += 1
        out["stale_days"] = stale
    except Exception:
        pass
    out["message"] = ("复盘闸门不可用：%s（买入闸门已 fail-open 放行，"
                      "复盘结论未约束选股）" % reason)
    return out


class _SnapshotAdapter:
    """【2026-10-21 修 P0-5】把 /api/dashboard 的 **dict** 快照包装成带属性的对象。

    根因：`mood_engine.cycle_judge(md)` / `review_deep` 六引擎用的是**属性访问**
    （`md.zt_pool` / `md.dt_pool` / `md.breadth` …），而本模块的
    `_fetch_market_snapshot()` 从 /api/dashboard 取回的是 **纯 dict**。
    → 四个引擎（emotion_cycle / loss_effect / mainline / wind_vane）全部抛
      `AttributeError: 'dict' object has no attribute 'zt_pool'`，
      被 review_deep 的 `safe()` 兜底吞成空字典 →
      **cycle.stage=None、signals 全 null、wind 空** →
      build_gate 落到 `_DEFAULT_STAGE='观察'` 并写出**降级空闸门**
      （cap 30% / single 10% / picks 1 / focus 0），且**不报错、退出码仍为 0**。
    这正是 app.py 的 `/api/review_deep` 传 `md`（MarketData 对象）而本模块传 dict 的差别。

    修复方式：这里做一层鸭子类型适配，让 dict 快照也能被属性访问命中，
    属性名与 `data_fetcher.MarketData.snapshot()` 返回的键保持一致。
    """

    def __init__(self, d):
        self._d = d or {}
        for k, v in self._d.items():
            # 键名大多与属性名一致（zt_pool / zb_pool / dt_pool / breadth /
            # indices / sectors / concepts / screen / ladder …）
            setattr(self, k, v)
        # 引擎依赖的属性兜底（快照里没有就给空容器，避免 AttributeError）
        for k in ("zt_pool", "zb_pool", "dt_pool", "breadth", "indices",
                  "sectors", "concepts", "screen", "ladder"):
            if getattr(self, k, None) is None:
                setattr(self, k, {} if k == "breadth" else ([] if k.endswith("_pool")
                                                             else {}))
        if not isinstance(self.breadth, dict):
            self.breadth = {}

    def snapshot(self):
        return self._d

    def __getitem__(self, k):
        return self._d[k]

    def get(self, k, default=None):
        return self._d.get(k, default)

    def keys(self):
        return self._d.keys()


def _fetch_market_snapshot(timeout=20, as_object=False):
    """从工作台 /api/dashboard 取当日快照（复用已有聚合，避免重复抓行情）。

    返回 dict；as_object=True 时返回 `_SnapshotAdapter`（供 review_deep 六引擎使用）。
    工作台不可用时抛异常由调用方处理。
    """
    import urllib.request
    url = "http://127.0.0.1:8765/api/dashboard"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        d = json.loads(resp.read())
    return _SnapshotAdapter(d) if as_object else d


def auto_build(force=False, mode="enforce", log=print):
    """【2026-10-19 修复·闸门链断裂】盘后自动生成次日闸门 + 度量上一次闸门效能。

    断裂根因：`review_deep` / `build_gate` / `effectiveness` 三个函数早已实现且可用
    （实测 `--build` 正常产出 asof=次日 的闸门），但**没有任何自动任务调用它们** ——
    trade_gate.json 的 asof 停留在 2026-10-08，连续 7 个交易日 fail-open，
    ADR-0040「复盘结论 → 次日买入闸门」闭环形同虚设。
    本函数是那个缺失的调用方，供 launchd / automation 在盘后调用。

    幂等设计（可安全重复调用）：
      · 当日闸门已存在且 asof 正确 → 默认不覆盖（force=True 才重建）
      · 非交易日 → 直接跳过，不写盘
      · 任一环节异常 → 返回非 0 并打印原因，绝不静默失败

    返回 dict: {"ok": bool, "reason": str, ...}
    """
    result = {"ok": False, "reason": "", "built": False, "asof": None, "effect": None}

    today = date.today()
    if not is_trading_day(today):
        result["reason"] = "非交易日（%s），跳过闸门生成" % today
        log("[gate_auto] %s" % result["reason"])
        return result
    log("[gate_auto] 交易日 %s，开始盘后闸门生成" % today)

    # ---- 1) 幂等：目标生效日的闸门已就绪则不覆盖（避免覆盖人工调过的 mode）----
    # 【关键】闸门的 asof 语义是「**下一个**交易日」（T 日盘后生成，供 T+1 约束），
    # 所以幂等基准是 next_trade_date(today) 而不是 today —— 否则永远判定为缺失而重复生成。
    try:
        target = next_trade_date(today, include_today=False)
    except Exception:
        target = today
    existing = load_gate(target)
    if existing and not force:
        result["ok"] = True
        result["asof"] = existing.get("asof")
        result["reason"] = ("目标生效日闸门已就绪（asof=%s, mode=%s），未覆盖"
                            % (existing.get("asof"), existing.get("mode")))
        log("[gate_auto] %s" % result["reason"])
        return result

    # ---- 1.5) 【2026-10-21 修】保护：不得用"明日闸门"覆盖"今日生效闸门" ----
    # 场景：10-21 盘前 09:09 跑了本任务 → 生成 asof=10-22 的闸门，覆盖了
    # 10-20 盘后生成的 asof=10-21 闸门 → **当天盘中闸门直接失效（fail-open）**。
    # 根因：闸门文件只有一份，而"约束今日"与"约束明日"两份需要并存。
    # 修法：若磁盘上已存在约束**今日**的闸门，且本次生成的是**明日**闸门，
    #      则把今日闸门另存为 trade_gate_current.json（供调度器读取），
    #      再让 trade_gate.json 承载明日闸门 —— 两者不再互相覆盖。
    _today_gate = load_gate(today, strict=True)
    if _today_gate and not force:
        try:
            cur_path = REPORTS / "trade_gate_current.json"
            cur_path.write_text(json.dumps(_today_gate, ensure_ascii=False, indent=2),
                                encoding="utf-8")
            result["ok"] = True
            result["asof"] = _today_gate.get("asof")
            result["reason"] = ("已保留今日生效闸门（asof=%s）到 %s；"
                                "本次生成的明日闸门将写入 %s，不影响今日盘中约束"
                                % (_today_gate.get("asof"), cur_path.name, GATE_FILE.name))
            log("[gate_auto] %s" % result["reason"])
            # 继续往下生成明日闸门（不 return）
        except Exception as e:
            log("[gate_auto] WARN 今日闸门另存失败（继续生成）: %s" % e)

    # ---- 2) 六引擎复盘 → build_gate ----
    try:
        import review_deep
        # 【2026-10-21 修 P0-5】必须用**对象**快照：六引擎是属性访问（md.zt_pool 等），
        # 传 dict 会让四个引擎 AttributeError 被 safe() 吞掉 → 写出降级空闸门。
        snapshot = _fetch_market_snapshot(as_object=True)
        review = review_deep.review_deep(snapshot, None, do_archive=False)
    except Exception as e:
        result["reason"] = "复盘引擎失败: %s" % e
        log("[gate_auto] ERROR %s" % result["reason"])
        return result

    # ---- 2.5) 【2026-10-21 修 P0-5】降级守卫：引擎全挂时禁止写空闸门 ----
    # 宁可保留昨日闸门，也不要用一个 signals 全 null 的"观察段 30%/1 只"废闸门
    # 去约束明日 —— 那是"有约束但无信息"，比 fail-open 更危险。
    _errs = review.get("errors") or []
    _cyc = review.get("cycle") or {}
    _sig = _cyc.get("signals") or {}
    if _errs and not _sig.get("zt"):
        result["reason"] = ("复盘引擎全部降级（%s），已中止写盘："
                            "避免用空闸门约束下一交易日" % "; ".join(_errs[:3]))
        log("[gate_auto] ERROR %s" % result["reason"])
        return result
    if _errs:
        log("[gate_auto] WARN 部分引擎降级（%d 项），其余引擎结论照常使用"
            % len(_errs))

    try:
        gate = build_gate(review, mode=mode)
    except Exception as e:
        result["reason"] = "build_gate 失败: %s" % e
        log("[gate_auto] ERROR %s" % result["reason"])
        return result

    if not gate or not gate.get("asof"):
        result["reason"] = "build_gate 返回空闸门"
        log("[gate_auto] ERROR %s" % result["reason"])
        return result

    # ---- 2.6) 【2026-10-21 修 P0-5】空信号闸门守卫 ----
    # stage_signals 全 null ⇒ 六引擎没拿到市场数据（属性访问失配 / 数据源异常），
    # 此时落 _DEFAULT_STAGE='观察' 会产出"有约束但无信息"的废闸门（cap30/picks1/focus0），
    # 比 fail-open 更危险。宁可不写。
    _gs = gate.get("stage_signals") or {}
    if not _gs.get("zt"):
        result["reason"] = ("闸门信号为空（zt/dt/ratio 全 None，六引擎降级），"
                            "已中止写盘：宁可用既有闸门也不用空闸门")
        log("[gate_auto] ERROR %s" % result["reason"])
        return result

    result["built"] = True
    result["asof"] = gate.get("asof")
    result["reason"] = "已生成 asof=%s（source=%s, stage=%s, mode=%s）" % (
        gate.get("asof"), gate.get("source_review_date"),
        gate.get("stage"), gate.get("mode"))
    log("[gate_auto] %s" % result["reason"])
    log("[gate_auto] %s" % summary_text(gate))

    # ---- 3) 闭环最后一环：度量上一个闸门的效果（失败不阻断主流程）----
    try:
        eff = effectiveness()
        if eff and not eff.get("error"):
            _write_json(EFFECT_FILE, eff)
            result["effect"] = eff.get("verdict")
            log("[gate_auto] 闸门效能: %s（已写 %s）" % (eff.get("verdict"), EFFECT_FILE.name))
        elif eff and eff.get("error"):
            log("[gate_auto] WARN 效能度量跳过: %s" % eff["error"])
        else:
            log("[gate_auto] INFO 无应用留痕可度量（调度器当日未跑买入时段）")
    except Exception as e:
        log("[gate_auto] WARN 效能度量异常（不影响闸门生成）: %s" % e)

    result["ok"] = True
    return result


def main(argv=None):
    argv = argv or sys.argv[1:]
    cmd = argv[0] if argv else "--show"

    if cmd == "--auto":
        force = "--force" in argv
        mode = "shadow" if "--shadow" in argv else "enforce"
        r = auto_build(force=force, mode=mode)
        return 0 if r.get("ok") else 1

    if cmd == "--health":
        h = gate_health()
        print("[%s] %s" % (h.get("level"), h.get("message")))
        return 0 if h.get("level") != "error" else 1

    if cmd == "--show":
        g = load_gate(date.today())
        print(summary_text(g))
        if not g:
            print("  不可用原因: %s" % stale_reason(date.today()))
        else:
            print(json.dumps(g, ensure_ascii=False, indent=2))
        ap = load_applied()
        if ap:
            print("\n最近一次应用留痕（%s）:" % ap.get("applied_at"))
            for s in (ap.get("stages") or []):
                print("  %s: 放行 %d / 剔除 %d" % (s.get("stage"), len(s.get("kept") or []),
                                                  len(s.get("drops") or [])))
        ef = _read_effect()
        if ef:
            print("\n闸门效能: %s" % ef.get("verdict"))
        return 0

    if cmd == "--build":
        # 独立生成（不依赖 Flask）：自己抓 dashboard
        try:
            import review_deep
            md = _fetch_market_snapshot(as_object=True)
        except Exception as e:
            print("无法取得市场数据（需工作台在跑）: %s" % e)
            return 2
        rev = review_deep.review_deep(md, None, do_archive=False)
        mode = "enforce"
        if "--shadow" in argv:
            mode = "shadow"
        # 【2026-10-21 修 P0-5】与 auto_build 同款守卫：空信号闸门不写盘
        _errs = rev.get("errors") or []
        _sig = (rev.get("cycle") or {}).get("signals") or {}
        if _errs and not _sig.get("zt"):
            print("复盘引擎全部降级（%s），中止写盘" % "; ".join(_errs[:3]))
            return 2
        g = build_gate(rev, mode=mode)
        if not (g.get("stage_signals") or {}).get("zt"):
            print("闸门信号为空，已中止写盘（宁可用既有闸门也不用空闸门）")
            return 2
        print(summary_text(g))
        print("已写入 %s（asof=%s）" % (GATE_FILE, g["asof"]))
        return 0

    if cmd == "--mode":
        g = load_gate(date.today(), strict=False)
        if not g:
            print("无闸门文件可切换")
            return 2
        want = argv[1] if len(argv) > 1 else "toggle"
        if want == "toggle":
            want = "shadow" if g.get("mode") == "enforce" else "enforce"
        if want not in ("enforce", "shadow"):
            print("模式须为 enforce / shadow / toggle")
            return 2
        g["mode"] = want
        _write_json(GATE_FILE, g)
        print("闸门模式 → %s" % g["mode"])
        return 0

    if cmd == "--verify":
        ef = effectiveness()
        if not ef:
            print("无应用留痕可度量（需调度器在交易日跑过至少一个买入时段）")
            return 3
        print(json.dumps(ef, ensure_ascii=False, indent=2))
        return 0

    if cmd == "--selftest":
        return _selftest()

    print(__doc__.split("【")[0])
    print("用法: review_gate.py [--show|--health|--auto [--force] [--shadow]|--build [--shadow]|"
          "--mode enforce|shadow|toggle|--verify|--selftest]")
    return 1


if __name__ == "__main__":
    sys.exit(main())

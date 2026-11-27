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
import config

import json
import log_util
import os
import shutil
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = Path(__file__).parent
REPORTS = config.REPORTS_DIR
GATE_FILE = REPORTS / "trade_gate.json"            # 复盘写 → 调度器读
APPLIED_FILE = REPORTS / "trade_gate_applied.json"  # 调度器写 → 工作台读
EFFECT_FILE = REPORTS / "gate_effectiveness.json"   # 复盘写 → 工作台读
HISTORY_DIR = BASE / "data" / "gate_history"

GATE_VERSION = 2   # v2（ADR-0041）：focus[] 增补 board_days/pct —— 供调度器把「复盘重点」当供票源用
COOLING_DAYS = 5          # 追高/超配违规 → 冷却 N 个交易日
ESCAPE_TEMP = 60          # 逃生阀：次日实时温度 ≥ 此值 → 硬阻断降级为试探档
# 【2026-11-26 修 P0-逃生阀】逃生阀降级后的「试探档」硬上限（= 「冰点」档的试探约束）。
# 退潮段 _POLICY = (cap 10, single 0, picks 0, max_board 2, hard True)：
# 「空仓」语义由 picks=0 / single=0 承载；旧实现降级 hard 时让这两个 0 被 falsy 判空跳过
# → 上限整体失效 → 退潮段变成无限制（11-26 09:25 实测派出买单，击穿空仓防守）。
# 降级时 0 → 显式试探档（1 只 / 单票 8%），段总仓上限 cap 不变。
# ★ 与 market_scheduler.py 的 REVIEW_GATE_ESCAPE_MAX_PICKS / _SINGLE_PCT 必须一致。
ESCAPE_MAX_PICKS  = 1     # 逃生阀降级后最多派 1 只
ESCAPE_SINGLE_PCT = 8     # 逃生阀降级后单票上限 8%
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

    except Exception as _e:
        log_util.quiet_warn("review_gate.py:237", _e)

    # 退潮/冰点 且 跌停 ≥50 → 一并硬阻断
    try:
        if stage in ("退潮", "冰点") and _to_int(sig.get("dt")) >= 50 and not hard:
            hard = True
            hard_reasons.append("跌停 %s 家，亏钱效应全面扩散" % sig.get("dt"))

    except Exception as _e:
        log_util.quiet_warn("review_gate.py:244", _e)


    # ---- 冷却名单 / 未执行卖出提醒 ----
    violations = disc.get("violations") or []
    cooling = _cooling_from_violations(violations, asof_d)
    pending_sell = _pending_sell_from_violations(violations)

    # ---- 风向标重点池（复盘选出的 8 只，次日命中即置顶）----
    # 【2026-11-26 修 P0-口径】重点池必须与本段 max_board_days 对齐：
    # 闸门自己不允许买超过 max_board 板的高位票，可重点池却把它标成「重点」→
    # 调度器给它打上「复盘重点」标签占掉 Top-N 名额，随后又被风格闸门剔除 →
    # 白占名额 + 污染闸门效能统计（dropped 组里混进自己推荐过的票）。
    # 该偏差已在 11-05 / 11-19 / 11-20 / 11-26 四次实证。
    # 超限项不丢弃：移到 focus_excluded 并写明原因（工作台可见，信息不丢）。
    focus = []
    focus_excluded = []
    for w in wind[:8]:
        code = _norm_code(w.get("code") or w.get("symbol"))
        if not code:
            continue
        bd = _to_int(w.get("lianban"), 1) or 1
        item = {
            "name": w.get("name") or "",
            "symbol": w.get("symbol") or code,
            "code": code,
            "kind": w.get("kind") or "",
            "kind_label": w.get("kind_label") or "",
            "one_word": bool(w.get("one_word")),
            # ADR-0041：v2 起带上连板数与涨幅 —— 调度器要把「非一字板且板数达标」的
            # 风向标反过来当【供票源】用（修复"复盘只能拦不能推"），必须知道板数。
            "board_days": bd,
            "pct": w.get("pct"),
            "why": (w.get("why") or "")[:100],
        }
        if bd > max_bd:
            item["excluded_reason"] = ("%d 板 > 「%s」段允许的最高 %d 板（不接力高位连板）"
                                       % (bd, stage, max_bd))
            focus_excluded.append(item)
            continue
        focus.append(item)

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
        "focus_excluded": focus_excluded,
        # —— 复盘原始信号（供避难阀与展示）——
        "stage_signals": {"zt": sig.get("zt"), "dt": sig.get("dt"),
                          "max_board": sig.get("max_board"), "ratio": sig.get("ratio"),
                          "zb_rate": sig.get("zb_rate"), "gap": sig.get("gap")},
        "loss_level": loss.get("level"),
        "avoid": (plan.get("avoid") or [])[:4],
        "checklist": (plan.get("checklist") or [])[:5],
    }

    if write:
        # ---- 【2026-11-26 修 P0-口径】残缺信号不得写盘 ----
        # zt=0 或 ratio=None 都意味着「判段依据不全」：ratio 缺失时 mood_engine 的
        # 分歧规则因 `ratio is not None` 前置条件失配 → 阶段静默翻掉
        # （11-26 实测：ratio=None → 分歧 变 反包，仓位上限 40%→30%）。
        # 宁可不写（沿用既有闸门），也不能用错段约束明日。
        if not sig.get("zt") or sig.get("ratio") is None:
            print("[gate] 信号不完整（zt=%s ratio=%s）→ 拒绝写盘（沿用既有闸门）"
                  % (sig.get("zt"), sig.get("ratio")))
            return gate
        # 【2026-11-26 修 P0-①】副本逻辑下沉进 build_gate（唯一写盘点）。
        # 在把「明日闸门」写进主文件之前，先保住磁盘上的**今日闸门**。
        _preserve_today_gate(asof_d)
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


def _preserve_today_gate(asof_d):
    """写盘前保住「今日生效闸门」到 trade_gate_current.json（P0-① 防护下沉点）。

    【为什么必须在这里】此前保护只在 `auto_build` 的 step1.5 里，而
    `review_deep.review_deep` → `build_gate(write=True)` 这条路径**没有**保护：
    工作台「复盘模式」tab 自动调 /api/review_deep（用户零操作）→ 盘中 09:0x
    竞价时段就会把**下一交易日**的失真闸门写进 trade_gate.json，
    覆盖掉约束今日的那份 → 当天盘中闸门直接 fail-open。
    （ADR-0040 必查坑 #1「写闸门必须先另存今日副本」，历史上已复发 4 次。）

    规则：
      · 本次要写的闸门 asof == 今日 → 不动（写的就是今日闸门本身）。
      · 本次要写的闸门 asof != 今日 → 若磁盘上存在 asof == 今日的闸门
        （主文件或 current 副本任一），先把它写进 trade_gate_current.json，
        再让主文件承载新闸门。调度器 `load_review_gate` 优先读 current 副本。

    任何异常都不得阻断主流程（闸门路径必须 fail-open）。
    """
    try:
        today_d = date.today()
        if asof_d == today_d:
            return False
        cur = REPORTS / "trade_gate_current.json"
        for src in (cur, GATE_FILE):
            try:
                d = json.loads(src.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(d, dict) or not d.get("asof"):
                continue
            if str(d.get("asof"))[:10] != today_d.isoformat():
                continue
            if src == cur:
                return False        # current 已是今日闸门，无需重写
            if _write_json(cur, d):
                print("[gate] 已保留今日生效闸门（asof=%s）到 %s"
                      % (today_d.isoformat(), cur.name))
                return True
    except Exception as e:
        print("[gate] 保留今日闸门失败(不影响主流程): %s" % e)
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

    # ---- 逃生阀：复盘判防守，次日却明显转强 → 降级为「试探档」 ----
    # 【2026-11-26 修 P0-逃生阀】降级 ≠ 解除全部约束。
    # 退潮段 _POLICY = (cap 10, single 0, picks 0, max_board 2, hard True)：
    # 「空仓」语义由 picks=0 / single=0 承载（hard 只是最后一道保险）。
    # 旧实现只把 hard 置 False 就走 else 分支，而下游两处判据遇 0 走 falsy：
    #   · `if picks and len(kept) > picks` → picks=0 → 只数上限整体失效
    #   · `if eq and single_pct`           → single=0 → 单票上限整体失效
    # → 退潮段被降级成「无限制」，11-26 09:25 实测仍派出 1 只买单（2.48% 权益），
    #    完全击穿「空仓防守」。同 10-21「炸板率恒 100%」的 `or` 陷阱，一并治。
    #
    # 降级规则（保守，与调度器侧一致）：
    #   · 段自带 picks/single 为正数 → 保留（量化上限仍生效）
    #   · 段自带 picks/single 为 0（空仓编码）→ 换显式试探档（1 只 / 单票 8%）
    #   · 段总仓上限 cap 始终不变（退潮仍是 10%）
    hard = bool(gate.get("hard_block"))
    temp = _to_float(ctx.get("temperature"))
    if hard and temp is not None and temp >= ESCAPE_TEMP:
        hard = False
        note = ("逃生阀：今日实时情绪温度 %.1f ≥ %d，复盘判「%s」与实时结构背离 → "
                "硬阻断降级为试探档（最多 %d 只 / 单票 ≤%d%% / 段总仓上限 %g%% 不变）"
                % (temp, ESCAPE_TEMP, gate.get("stage"),
                   ESCAPE_MAX_PICKS, ESCAPE_SINGLE_PCT,
                   _to_float(gate.get("position_cap_pct")) or 0.0))
        meta["notes"].append(note)
        meta["escape"] = True
        # 生效上限写回闸门副本 → 下游额度计算与展示看到同一套数
        gate = dict(gate)
        _p_raw = _to_int(gate.get("max_picks"), 0)
        _s_raw = _to_float(gate.get("single_pos_cap_pct"))
        gate["max_picks"] = (_p_raw if _p_raw > 0 else ESCAPE_MAX_PICKS)
        gate["single_pos_cap_pct"] = (_s_raw if (_s_raw is not None and _s_raw > 0)
                                      else ESCAPE_SINGLE_PCT)
        gate["escape_downgraded"] = True
        gate["hard_block"] = False

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
    # 【2026-11-26 修 P0-逃生阀】只数上限：`picks` 为 0 是「本段不许开仓」（退潮段空仓
    # 语义），不是「无限制」。旧写法 `if picks and ...` 遇 0 走 falsy → 上限整体失效。
    # 同 10-21「炸板率恒 100%」的 `or` 陷阱，一并治（0 必须用 is None 判定）。
    picks = _to_int(gate.get("max_picks"), None)
    if picks is not None and len(kept) > picks:
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
    global GATE_FILE, APPLIED_FILE, EFFECT_FILE, HISTORY_DIR, REPORTS
    _bak = (GATE_FILE, APPLIED_FILE, EFFECT_FILE, HISTORY_DIR, REPORTS)
    _tmpd = Path(tempfile.mkdtemp(prefix="rg_selftest_"))
    REPORTS = _tmpd  # 【2026-11-17 修】_sync_current_copy 写 REPORTS/trade_gate_current.json
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

    # 【2026-11-26 修 P0-逃生阀】退潮段真实形态：picks=0 / single=0（空仓语义由这两个 0 承载）。
    # 旧实现遇 0 走 falsy → 只数与单票上限双双失效 → 退潮段被降级成「无限制」
    # （11-26 09:25 实测在 hard_block=True 的退潮段仍派出 1 只买单，击穿空仓防守）。
    print("[selftest] 逃生阀 · 退潮段空仓语义（picks=0 / single=0）")
    g_tide = dict(g, stage="退潮", hard_block=True, hard_block_reason="退潮段：亏钱效应扩散",
                  position_cap_pct=10, single_pos_cap_pct=0, max_picks=0, max_board_days=2)
    kt1, dt1, mt1 = apply("09:25", cands, g_tide, {"temperature": 30, "equity": 500000, "cash": 450000})
    _chk(len(kt1) == 0 and len(dt1) == len(cands), "退潮段 + 低温：硬阻断 → 0 放行")
    kt2, dt2, mt2 = apply("09:25", cands, g_tide, {"temperature": 68, "equity": 500000, "cash": 450000})
    _chk(len(kt2) <= ESCAPE_MAX_PICKS,
         "退潮段 + 逃生阀：放行 %d 只 ≤ 试探档 %d 只（空仓语义未被击穿）"
         % (len(kt2), ESCAPE_MAX_PICKS))
    _chk(mt2.get("escape"), "退潮段 + 逃生阀：留痕标记 escape=True")
    _chk(any("只数上限" in d["reason"] for d in dt2) or len(kt2) <= ESCAPE_MAX_PICKS,
         "退潮段 + 逃生阀：超出试探档只数的候选被「只数上限」剔除")

    print("[selftest] fail-open")
    k4, d4, m4 = apply("13:40", cands, None, {})
    _chk(k4 == cands and not d4 and not m4["applied"], "无闸门 → 原样放行")
    k5, d5, m5 = apply("13:40", cands, dict(g, mode="shadow"), {})
    _chk(k5 == cands and not d5, "shadow 模式 → 不拦截")

    print("[selftest] 符号归一")
    _chk(_norm_code("000066") == _norm_code("000066.SZ") == _norm_code("sz000066"),
         "6 位代码归一一致")

    print("[selftest] current 副本内置维护（2026-11-17 修）")
    _cur = REPORTS / "trade_gate_current.json"
    _sync_current_copy(dict(g, asof="2099-01-02"), log=lambda *_: None)
    _chk(_cur.exists() and json.loads(_cur.read_text(encoding="utf-8"))["asof"] == "2099-01-02",
         "首次同步：current 副本写入 asof=2099-01-02")
    _sync_current_copy(dict(g, asof="2099-01-03"), log=lambda *_: None)
    _chk(json.loads(_cur.read_text(encoding="utf-8"))["asof"] == "2099-01-03",
         "asof 更新：current 副本被刷新为 2099-01-03")
    _sync_current_copy(dict(g, asof="2099-01-03"), log=lambda *_: None)
    _chk(json.loads(_cur.read_text(encoding="utf-8"))["asof"] == "2099-01-03",
         "幂等：重复同步不报错、内容不变")
    _sync_current_copy({}, log=lambda *_: None)
    _chk(json.loads(_cur.read_text(encoding="utf-8"))["asof"] == "2099-01-03",
         "空闸门：不覆盖已有副本")

    print("\n调度器侧必须与此保持一致的规则（改动需同步两处）：")
    for ln in ("1) mode != enforce → 不拦截", "2) hard_block → 清空（除非 temperature ≥ %d）" % ESCAPE_TEMP,
               "3) 逃生阀降级 = 换试探档：picks=0/single=0 → %d 只 / 单票 ≤%d%%（段 cap 不变）"
               % (ESCAPE_MAX_PICKS, ESCAPE_SINGLE_PCT),
               "4) cooling 命中 → 剔除", "5) board_days > max_board_days → 剔除",
               "6) 只数 > max_picks → 截断（max_picks=0 是「0 只」，不是「无限制」）",
               "7) 额度 ≤0 → 清空；否则 avail = min(额度, 现金)",
               "8) single_pos_cap_pct=0 是「单票 0%」，不是「无上限」",
               "9) 任何异常 → 放行"):
        print("   " + ln)
    print("\n结果: " + ("全部通过 ✅" if ok else "存在失败 ❌"))
    # 还原真实路径并清理临时目录（防止全局常量被本次自检永久改写）
    (GATE_FILE, APPLIED_FILE, EFFECT_FILE, HISTORY_DIR, REPORTS) = _bak
    try:
        shutil.rmtree(_tmpd, ignore_errors=True)

    except Exception as _e:
        log_util.quiet_warn("review_gate.py:769", _e)

    print("[selftest] 落盘目标已还原（本次自检写的是临时目录，未污染真实 reports/）")
    return 0 if ok else 1


def _caliber_mismatch(gate):
    """闸门**口径**校验：把闸门的 stage_signals 与工作台实时快照四项对表。

    【2026-11-26 新增】--health 此前只验 asof（闸门链断裂会报警），
    但**不验口径** → 盘中/竞价口径写出的失真闸门（例如工作台 09:0x 触发的
    review_deep 写出的 zt=0/dt=0「冰点段废闸门」）照样报 [ok]，
    台账已催办 3 次（6e0379523d「--health 假阳性第 3 次」、
    31bbe9d5fb「asof 正确但口径错误仍报 [ok]」）。

    对表口径：zt=zt_count、dt=dt_count、ratio=up/down、zb_rate=zb/(zt+zb)*100。
    工作台不可用 → 返回 None（跳过校验，绝不误报）。
    """
    try:
        snap = _fetch_market_snapshot(timeout=10, as_object=False)
    except Exception:
        return None
    try:
        sig = (gate or {}).get("stage_signals") or {}
        b = snap.get("breadth") or {}
        up, down = b.get("up"), b.get("down")
        zt_t = snap.get("zt_count")
        dt_t = snap.get("dt_count")
        zb_t = snap.get("zb_count")
        if not (zt_t and dt_t is not None and up and down and zb_t is not None):
            return None
        ratio_t = round(float(up) / max(float(down), 1), 2)
        zbr_t = round(float(zb_t) / max(float(zt_t) + float(zb_t), 1) * 100, 1)
        pairs = [("zt", sig.get("zt"), zt_t), ("dt", sig.get("dt"), dt_t),
                 ("ratio", sig.get("ratio"), ratio_t), ("zb_rate", sig.get("zb_rate"), zbr_t)]
        bad = []
        for name, gv, tv in pairs:
            if gv is None:
                bad.append("%s 缺失(快照 %s)" % (name, tv))
                continue
            try:
                if abs(float(gv) - float(tv)) >= 0.011:
                    bad.append("%s 闸门 %s ≠ 快照 %s" % (name, gv, tv))
            except (TypeError, ValueError):
                bad.append("%s 不可比(闸门 %s / 快照 %s)" % (name, gv, tv))
        if bad:
            return {"ok": False, "detail": "；".join(bad)}
        return {"ok": True, "detail": "四项口径与收盘快照一致"}
    except Exception as e:
        log_util.quiet_warn("review_gate.py:_caliber_mismatch", e)
        return None


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

        except Exception as _e:
            log_util.quiet_warn("review_gate.py:805", _e)

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
        # ---- 【2026-11-26】口径校验（--health 假阳性的正解）----
        # 只在「该闸门声称由**今日**复盘生成」时对表：它必须与今日快照一致。
        # （盘中约束今日的闸门是昨日盘后生成的，拿今天的盘中快照比必然不符 → 跳过）
        try:
            src = str(out["gate"].get("source_review_date") or "")[:10]
            if src == today.isoformat():
                mm = _caliber_mismatch(out["gate"])
                if mm is not None:
                    out["caliber"] = mm
                    if not mm.get("ok"):
                        out["level"] = "warn"
                        out["message"] += "｜⚠️ 口径不符：%s" % mm.get("detail")
        except Exception as _e:
            log_util.quiet_warn("review_gate.py:gate_health", _e)
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

    except Exception as _e:
        log_util.quiet_warn("review_gate.py:845", _e)

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

        # ---- 【2026-11-26 修 P0-口径】展示截断版不能当统计口径 ----
        # /api/dashboard 的 zt_pool 是 [:60] 截断版、zb_pool 是 [:40] 截断版，
        # 而 zt_count / zb_count 是全量计数（data_fetcher.snapshot）。
        # 本模块走 HTTP 快照，进程内 MarketData 走的是 self.zt_pool（全量）——
        # 于是**闸门路径与工作台路径同源同刻可判反**（ADR-0040 必查坑 #2）。
        # 11-26 实测：闸门 zt=60 / zb_rate=32.6，真值 zt=65 / zb_rate=30.85。
        # 这里把 *_pool_all 提升为引擎口径的 zt_pool / zb_pool，与进程内路径对齐；
        # 截断版另存 *_pool_display，仅供需要"前 N 只"的展示场景使用。
        for _short, _full in (("zt_pool", "zt_pool_all"), ("zb_pool", "zb_pool_all")):
            _fv = self._d.get(_full)
            _sv = self._d.get(_short)
            if isinstance(_fv, list) and (not isinstance(_sv, list) or len(_fv) > len(_sv)):
                setattr(self, _short + "_display", _sv)
                setattr(self, _short, _fv)

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


def _snapshot_complete(snap):
    """快照完整性判定：闸门判段所需的 4 个信号是否齐备。

    【2026-11-26 修 P0-口径】闸门判段依赖 zt / dt / ratio / zb_rate 四个信号，
    其中 **ratio 来自 breadth（涨跌比）**。应用重启后全A扫描尚未完成时，
    breadth 为空 → ratio = None。而 mood_engine 的「分歧」规则带
    `s["ratio"] is not None` 前置条件 → **ratio 缺失会静默把阶段翻掉**
    （11-26 实测：zt=65/dt=5/ratio=None → 阶段从「分歧」翻成「反包」，
     仓位上限从 40% 掉到 30%）。这不是"数据缺失显示为 --"，是判错段。

    完整条件：breadth 有 up/down（ratio 可算）+ 涨停池非空（zt 可算）。
    """
    try:
        b = getattr(snap, "breadth", None) or {}
        if not isinstance(b, dict):
            return False
        if not (b.get("up") and b.get("down")):
            return False
        zt = getattr(snap, "zt_pool", None) or []
        return bool(zt)
    except Exception:
        return False


def _fetch_complete_snapshot(timeout=20, retries=4, wait=8, log=print):
    """取快照并等它「完整」—— 应用重启后全A扫描要 ~20 秒才回填 breadth。

    拿不到完整快照就返回 None（调用方必须拒绝写盘，不得用残缺信号判段）。
    """
    for i in range(max(1, retries)):
        try:
            snap = _fetch_market_snapshot(timeout=timeout, as_object=True)
        except Exception as e:
            log("[gate_auto] 快照抓取失败(%s/%s): %s" % (i + 1, retries, e))
            snap = None
        if snap is not None and _snapshot_complete(snap):
            return snap
        if i < retries - 1:
            log("[gate_auto] 快照不完整（涨跌比缺失 = 全A扫描未回填）… %d 秒后重试" % wait)
            time.sleep(wait)
    return None


def _sync_current_copy(gate, log=print):
    """把闸门快照同步到 trade_gate_current.json（调度器盘前优先读的「生效副本」）。

    【2026-11-17 修 · 闸门副本维护内置化】
    历史缺陷：auto_build 自身从不写 trade_gate_current.json，完全依赖 automation
    prompt 里的一句 `cp trade_gate.json trade_gate_current.json`（步骤 3.5）。
    结果：任何人直接跑 `python review_gate.py --auto`（诊断、手工补跑、别的调度器）
    都只更新 main，current 停在旧 asof；次日 10:59 工作台 review_deep 覆写 main 后，
    调度器两个文件都读不到「约束当日」的闸门 → 全天 fail-open（ADR-0040 P0-① 复发）。

    本函数把这一步收进代码，使 --auto 命令自身幂等地维护副本：
      · 只写「闸门内容」，不判断是否该写 —— 判断在调用方（幂等出口 / 新建出口 / 今日保护）。
      · 用 _write_json 原子写，避免调度器读到半截 JSON。
      · 失败只告警不抛异常（副本维护失败不应阻断主闸门生成）。
    """
    if not isinstance(gate, dict) or not gate.get("asof"):
        log("[gate_auto] WARN current 副本同步跳过：源闸门无 asof")
        return False
    cur = REPORTS / "trade_gate_current.json"
    old_asof = None
    try:
        if cur.exists():
            old_asof = str((json.loads(cur.read_text(encoding="utf-8")) or {}).get("asof"))[:10]
    except Exception:
        old_asof = None
    new_asof = str(gate.get("asof"))[:10]
    if old_asof == new_asof:
        log("[gate_auto] current 副本已就绪（asof=%s，无需重写）" % new_asof)
        return True
    ok = _write_json(cur, gate)
    if ok:
        log("[gate_auto] current 副本已同步: %s（asof %s → %s）" % (cur.name, old_asof, new_asof))
    return ok


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
        # 【2026-11-17 修】幂等跳过也要维护 current 副本：
        # 盘后 review_deep 常已把明日闸门写进 main，导致这里 early-return；
        # 过去直接 return 不碰 current → 靠 automation prompt 的 cp 兜底，
        # 一旦有人直接跑 --auto，current 就会陈旧 → 次日盘中 fail-open。
        _sync_current_copy(existing, log)
        result["ok"] = True
        result["asof"] = existing.get("asof")
        result["reason"] = ("目标生效日闸门已就绪（asof=%s, mode=%s），未覆盖；current 副本已同步"
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
    _preserved_today_gate = False  # 【2026-11-17 修】标记 step1.5 是否已把「今日闸门」存入 current
    _today_gate = load_gate(today, strict=True)
    if _today_gate and not force:
        try:
            cur_path = REPORTS / "trade_gate_current.json"
            ok = _write_json(cur_path, _today_gate)
            _preserved_today_gate = bool(ok)
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
        # 【2026-11-26 修 P0-口径】还必须等快照「完整」：应用重启后全A扫描未回填时
        # breadth 为空 → ratio=None → 阶段被静默翻掉（分歧→反包，实测）。
        snapshot = _fetch_complete_snapshot(log=log)
        if snapshot is None:
            result["reason"] = ("市场快照不完整（涨跌比缺失）→ 已中止写盘："
                                "残缺信号会把阶段判错（宁可不写，也不能用错段约束明日）")
            log("[gate_auto] ERROR %s" % result["reason"])
            return result
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

    # ---- 归档补写（【2026-11-26 修】修 review_modes 归档断链）----
    # 此前 data/review_modes/<date>.json 只由工作台 /api/review_deep 写，而该接口
    # 在 09:0x 竞价时段 zt_pool 为空 → archive() 直接 return None → 当日不落档。
    # 结果 11-18 起连续 6 个交易日归档缺失 → verify_yesterday 引擎长期降级为
    # 「归档读取失败」，全量溢价验证（连板溢价兑现率）形同虚设。
    # 现在由盘后自动任务用**收盘全量快照**补写归档；这里单独调 archive() 而不用
    # do_archive=True —— 后者会连带触发 build_gate(write=True)，与本函数重复写盘。
    # ⚠ 必须排在两道降级守卫**之后**：引擎降级时写出来的归档是残缺的
    # （cycle={} / stage=None），会覆盖当天已有的好归档（11-26 实测踩到）。
    try:
        _arch = review_deep.archive(snapshot, review)
        log("[gate_auto] 复盘归档: %s" % (_arch or "未写入（休市日或无涨停数据）"))
    except Exception as e:
        log("[gate_auto] WARN 复盘归档失败(不影响闸门): %s" % e)

    # 【2026-11-17 修】新建路径也要维护 current 副本（否则直接跑 --auto 时 current 陈旧）。
    # 但若 step1.5 刚把「今日闸门」存进 current（盘前误跑场景），绝不能用明日闸门覆盖，
    # 否则当天盘中本该约束今日的闸门会被顶掉 —— 这正是 10-21 fail-open 的根因。
    if not _preserved_today_gate:
        _sync_current_copy(gate, log)
    else:
        log("[gate_auto] current 保留今日闸门（step1.5），不覆盖为明日闸门")

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
            md = _fetch_complete_snapshot(log=print)
            if md is None:
                print("市场快照不完整（涨跌比缺失）→ 中止写盘（残缺信号会把阶段判错）")
                return 2
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

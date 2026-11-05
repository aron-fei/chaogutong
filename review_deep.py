# -*- coding: utf-8 -*-
"""复盘模式引擎（ADR-0039）。

设计依据：开盘啦「盘后教程：如何让复盘更有条理？」+ 多源交叉验证的「短线七步复盘法」
+ 炒股养家六阶段情绪周期。三工具串联（当日强弱 → 关键个股 → 阶段方向）落地为六引擎：

  ① emotion_cycle   情绪周期定位（择时·复用 mood_engine.cycle_judge）
  ② verify_yesterday 昨日验证环（打脸/验证：昨日预案 → 今日兑现率）
  ③ loss_effect     亏钱效应雷达（跌停共性 / 炸板 / 大面股 / 扩散度）
  ④ mainline        主线识别器（≥3 家涨停 + 梯队完整 + 新老题材）
  ⑤ wind_vane       风向标观察池（五类标记 → 5-8 只）
  ⑥ discipline      纪律检查器（追高 / 违纪 / 漏执行 / 超配）

产出：next_day_plan() 次日交易预案（每只 × 强涨/分歧/走弱三情形）+ 仓位闸门建议。
归档：archive() 落盘 data/review_modes/<date>.json，供次日 verify_yesterday 消费。

只读模块：不改动任何买卖通道，不写回测库。
"""
import json
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

import mood_engine

BASE = Path(__file__).parent
ARCH_DIR = BASE / "data" / "review_modes"
REVIEWS_DIR = BASE / "data" / "reviews"
GENE_FILE = BASE / "data" / "gene_board.json"
TIMEOUT = 8
_KCACHE = {}
_KCACHE_TTL = 300
_LOCK = threading.Lock()
_TRADE_DAY_CACHE = {}

# 阶段 → 仓位上限（%）。加速段刻意低于发酵段：拒绝追高。
STAGE_POSITION = {
    "启动": 30, "确认": 50, "发酵": 70, "加速": 50,
    "分歧": 40, "反包": 30, "龙回头": 30,
    "冰点": 20, "退潮": 10, "观察": 30,
}


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _qt_symbol(code):
    c = "".join(ch for ch in str(code or "") if ch.isdigit())
    if not c:
        return ""
    if c.startswith(("6", "9", "5")):
        return "sh" + c
    if c.startswith(("0", "3", "2")):
        return "sz" + c
    return ""


def qt_batch(codes):
    """腾讯批量快照（一次请求拿全）。返回 {6位代码: {price, pct, prev_close, high, low, open}}。"""
    syms, back = [], {}
    for c in codes:
        s = _qt_symbol(c)
        if s:
            syms.append(s)
            back[s] = "".join(ch for ch in str(c) if ch.isdigit())
    if not syms:
        return {}
    out = {}
    for i in range(0, len(syms), 120):
        chunk = syms[i:i + 120]
        try:
            r = requests.get("http://qt.gtimg.cn/q=" + ",".join(chunk),
                             timeout=TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
            r.encoding = "gbk"
        except Exception:
            continue
        for line in r.text.split(";"):
            if "~" not in line:
                continue
            p = line.split("~")
            if len(p) < 35 or not p[3]:
                continue
            try:
                out[p[2]] = {
                    "name": p[1], "price": float(p[3]), "prev_close": float(p[4]),
                    "open": float(p[5]), "pct": float(p[32]),
                    "high": float(p[33]) if p[33] else None,
                    "low": float(p[34]) if p[34] else None,
                }
            except (ValueError, IndexError):
                continue
    return {back.get(s, s): v for s, v in out.items()}


def _kline(code, n=5):
    """日K（腾讯，qfq）。带 5 分钟缓存。"""
    sym = _qt_symbol(code)
    if not sym:
        return []
    key = (sym, n)
    now = time.time()
    with _LOCK:
        hit = _KCACHE.get(key)
        if hit and now - hit[0] < _KCACHE_TTL:
            return hit[1]
    try:
        r = requests.get(
            f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,{n},qfq",
            timeout=TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        d = r.json().get("data", {}).get(sym, {})
        bars = d.get("qfqday") or d.get("day") or []
        out = []
        for b in bars:
            try:
                out.append({"date": b[0], "open": float(b[1]), "close": float(b[2]),
                            "high": float(b[3]), "low": float(b[4])})
            except (ValueError, IndexError):
                continue
    except Exception:
        out = []
    with _LOCK:
        _KCACHE[key] = (now, out) if out else (0, out)  # 空结果不缓存，避免瞬时失败粘住 5 分钟
    return out


def is_trading_day(d=None):
    """交易日判定（akshare 官方日历，带进程内缓存 + 周末降级）。

    复用 market_scheduler.is_trading_day() 的口径 —— 修复 M9 在休市日刷幽灵单
    的同类根因（data_fetcher.market_status() 不含节假日）。"""
    d = d or date.today()
    key = d.isoformat()
    with _LOCK:
        if key in _TRADE_DAY_CACHE:
            return _TRADE_DAY_CACHE[key]
    ok = d.weekday() < 5
    try:
        import akshare as ak
        df = ak.tool_trade_date_hist_sina()
        ok = key in {str(x)[:10] for x in df["trade_date"].tolist()}
    except Exception:
        pass
    with _LOCK:
        _TRADE_DAY_CACHE[key] = ok
    return ok


def _prev_trade_date():
    """上一交易日：优先取归档目录里 < 今天的最近一天（确保归档可被消费）。"""
    try:
        if ARCH_DIR.exists():
            ds = sorted((f.stem for f in ARCH_DIR.glob("*.json")), reverse=True)
            today = date.today().isoformat()
            for x in ds:
                if x < today:
                    return x
    except Exception:
        pass
    return None


def _load_gene():
    try:
        g = json.loads(GENE_FILE.read_text(encoding="utf-8"))
        return g if isinstance(g, dict) else {}
    except Exception:
        return {}


def _prev_gene_date(gene):
    """基因归档里 < 今天的最近一天（基因文件自带日期，不依赖复盘归档）。"""
    today = date.today().isoformat()
    ds = [k for k, v in gene.items() if isinstance(v, dict) and k < today]
    return max(ds) if ds else None


def _is_one_word(p):
    """一字/竞价秒板判定：首封时间 ≤09:26 且换手极低 → 挂单难成交。

    历史教训（09-22 华瓷股份）：一字板在回测里会产出"幽灵盈亏"，
    成本=建议价 的假设不成立，必须显式标注不可成交。"""
    ft = str(p.get("first_time") or "999999")
    try:
        to = float(p.get("turnover") or 0)
    except (TypeError, ValueError):
        to = 0.0
    return ft <= "092600" and to < 5.0


# ---------------------------------------------------------------------------
# ① 情绪周期定位
# ---------------------------------------------------------------------------
def emotion_cycle(md):
    """情绪周期阶段 + 仓位闸门 + 关键信号表 + 昨日对比。"""
    c = mood_engine.cycle_judge(md)
    stage = c.get("stage") or "观察"
    sig = {
        "zt": c.get("zt"), "dt": c.get("dt"), "zb": c.get("zb"),
        "zb_rate": c.get("zb_rate"), "max_board": c.get("max_board"),
        "gap": c.get("gap"), "ratio": c.get("ratio"),
        "up": c.get("up"), "down": c.get("down"),
    }
    # 昨日同信号对比（从归档读）
    prev = None
    pv = _prev_trade_date()
    if pv:
        try:
            a = json.loads((ARCH_DIR / f"{pv}.json").read_text(encoding="utf-8"))
            prev = {"date": pv, **{k: (a.get("cycle") or {}).get(k) for k in ("zt", "dt", "zb_rate", "max_board", "gap")}}
        except Exception:
            prev = None
    return {
        "stage": stage,
        "law": c.get("law", ""),
        "position_cap": STAGE_POSITION.get(stage, 30),
        "signals": sig,
        "prev": prev,
        "points": mood_engine.operation_points(stage),
        "alert": mood_engine.risk_alert(stage, c.get("max_board") or 0),
        "progress": mood_engine.stage_progress(stage),
        "dist": c.get("dist") or {},
    }


# ---------------------------------------------------------------------------
# ② 昨日验证环
# ---------------------------------------------------------------------------
def verify_yesterday(md):
    """昨日复盘 → 今日兑现率。无归档时降级为「连板结构代理」并显式标注。"""
    pv = _prev_trade_date()
    if not pv:
        zt = md.zt_pool or []
        lb = [p for p in zt if (p.get("lianban") or 1) >= 2]
        return {
            "available": False,
            "note": "归档从本次运行开始积累，全量溢价验证需 1 个交易日后启用",
            "proxy": {
                "lianban_count": len(lb),
                "first_board_count": len(zt) - len(lb),
                "lianban_ratio": round(len(lb) / max(len(zt), 1) * 100, 1),
                "verdict": ("接力力度强（连板占比 ≥25%）" if len(zt) and len(lb) / max(len(zt), 1) >= 0.25
                            else "接力力度偏弱（连板占比 <25%），首板炮灰概率高"),
            },
            "rows": [], "metrics": None,
        }
    try:
        a = json.loads((ARCH_DIR / f"{pv}.json").read_text(encoding="utf-8"))
    except Exception:
        return {"available": False, "note": f"{pv} 归档读取失败", "rows": [], "metrics": None}

    watch = (a.get("zt_codes") or []) + (a.get("wind_codes") or [])
    watch = list(dict.fromkeys(watch))
    if not watch:
        return {"available": False, "note": f"{pv} 归档无标的", "rows": [], "metrics": None}
    q = qt_batch(watch)
    zt_set = set(a.get("zt_codes") or [])
    rows, adv, big_loss, sealed = [], 0, 0, 0
    for code in watch:
        v = q.get(code)
        if not v:
            continue
        pct = v.get("pct")
        if pct is None:
            continue
        rows.append({"code": code, "name": v.get("name"), "pct": pct,
                     "price": v.get("price"), "is_zt": code in zt_set})
        if code in zt_set:
            adv += pct
            if pct >= 9.5:
                sealed += 1
            if pct <= -5:
                big_loss += 1
    rows.sort(key=lambda x: -(x["pct"] or 0))
    n = len(rows)
    metrics = None
    if n:
        zt_rows = [r for r in rows if r["is_zt"]]
        zu = len(zt_rows)
        metrics = {
            "sample": n,
            "avg_pct": round(adv / zu, 2) if zu else None,
            "idx": f"{pv} 涨停样本 {zu} 只",
            "continue_rate": round(sealed / zu * 100, 1) if zu else None,
            "big_loss_rate": round(big_loss / zu * 100, 1) if zu else None,
            "win_rate": round(sum(1 for r in rows if (r["pct"] or 0) > 0) / n * 100, 1),
        }
    return {"available": True, "note": "", "date": pv, "rows": rows[:30], "metrics": metrics}


# ---------------------------------------------------------------------------
# ③ 亏钱效应雷达
# ---------------------------------------------------------------------------
def loss_effect(md):
    """跌停共性 / 炸板强度 / 大面股 / 扩散度 1-5 级。"""
    dt_pool = md.dt_pool or []
    zb_pool = md.zb_pool or []
    zt_pool = md.zt_pool or []
    b = md.breadth or {}

    ind = {}
    for p in dt_pool:
        k = p.get("industry") or "其他"
        ind[k] = ind.get(k, 0) + 1
    ind_rank = sorted(ind.items(), key=lambda x: -x[1])[:6]
    cluster = ind_rank[0][1] if ind_rank else 0

    reb = 0
    for p in zb_pool:
        try:
            reb += int(p.get("break_count") or 1)
        except (TypeError, ValueError):
            reb += 1

    # 大面股：跌停池 + 跌幅前列（amount 较大的更值得警惕）
    faces = sorted(dt_pool, key=lambda x: -(x.get("amount_yi") or 0))[:8]

    # 扩散度：跌停数 / 炸板 / 跌停行业聚集度 / 涨跌比 四因子加总 1-5
    score = 0
    if len(dt_pool) >= 50:
        score += 2
    elif len(dt_pool) >= 20:
        score += 1
    zt, zb = len(zt_pool), len(zb_pool)
    zbr = zb / max(zt + zb, 1) * 100
    if zbr >= 40:
        score += 2
    elif zbr >= 25:
        score += 1
    if cluster >= 3:
        score += 1
    up, down = b.get("up"), b.get("down")
    if up and down and up / max(down, 1) < 0.5:
        score += 1
    level = max(1, min(5, score))

    verdict = {
        1: "🟢 亏钱效应收敛：跌停零散，可适度试错",
        2: "🟡 局部风险：个别板块退潮，避开高位补跌股",
        3: "🟠 亏钱效应显现：大面股增多，控仓、只做低位",
        4: "🔴 风险扩散：跌停聚集，高位批量杀跌，减仓防守",
        5: "🟥 系统性杀跌：热门股批量杀跌，空仓等待",
    }[level]

    return {
        "level": level, "verdict": verdict,
        "dt_count": len(dt_pool), "zb_count": zb, "zb_rate": round(zbr, 1),
        "rebound_total": reb, "cluster": cluster, "ind_rank": ind_rank,
        "faces": faces,
    }


# ---------------------------------------------------------------------------
# ④ 主线识别器
# ---------------------------------------------------------------------------
def mainline(md):
    """板块/概念主线：≥3 家涨停 + 梯队完整度 + 新老题材 + 龙头。"""
    zt_pool = md.zt_pool or []
    gene = _load_gene()
    pv = _prev_gene_date(gene)
    prev_gene = gene[pv] if pv else {}

    by_ind = {}
    for p in zt_pool:
        k = p.get("industry") or "其他"
        by_ind.setdefault(k, []).append(p)

    out = []
    for name, arr in by_ind.items():
        n = len(arr)
        if n < 3:
            continue
        boards = sorted((x.get("lianban") or 1) for x in arr)
        mx = max(boards)
        lv = sorted({x for x in boards if x >= 2})
        gap = sum(1 for l in range(mx - 1, 1, -1) if l not in set(lv))
        leader = max(arr, key=lambda x: (x.get("lianban") or 1))
        prev_n = prev_gene.get(name)
        if prev_n is None:
            tag = "新题材"
        elif n > prev_n:
            tag = "延续发酵"
        elif n == prev_n:
            tag = "持平"
        else:
            tag = "降温"
        amt = sum((x.get("amount_yi") or 0) for x in arr)
        fnd = sum((x.get("fund_yi") or 0) for x in arr)
        out.append({
            "name": name, "zt_count": n, "max_board": mx, "gap": gap,
            "ladder_ok": gap == 0 and mx >= 2,
            "leader": {"code": leader.get("code"), "name": leader.get("name"),
                       "lianban": leader.get("lianban"), "turnover": leader.get("turnover")},
            "amount_yi": round(amt, 1), "fund_yi": round(fnd, 1),
            "prev_count": prev_n, "tag": tag,
            "members": [{"code": x.get("code"), "name": x.get("name"),
                         "lianban": x.get("lianban"), "first_time": x.get("first_time"),
                         "turnover": x.get("turnover")} for x in
                        sorted(arr, key=lambda y: -(y.get("lianban") or 1))[:6]],
        })
    # 排序：涨停家数 → 无断层 → 最高板
    out.sort(key=lambda x: (-x["zt_count"], x["gap"], -x["max_board"]))

    # 概念层（题材粒度，更细）。≥3 家才算主线；全部未达标时给 Top5 作参考（标 weak）
    concepts = []
    for c in (md.concepts or []):
        z = c.get("zt_count") or 0
        if z >= 3:
            concepts.append({"name": c.get("name"), "zt_count": z,
                             "pct": c.get("pct"), "net_inflow": c.get("net_inflow"),
                             "leader": c.get("leader"), "leader_pct": c.get("leader_pct"),
                             "weak": False})
    if not concepts:
        for c in sorted((md.concepts or []), key=lambda x: -(x.get("zt_count") or 0))[:5]:
            z = c.get("zt_count") or 0
            if z < 1:
                break
            concepts.append({"name": c.get("name"), "zt_count": z,
                             "pct": c.get("pct"), "net_inflow": c.get("net_inflow"),
                             "leader": c.get("leader"), "leader_pct": c.get("leader_pct"),
                             "weak": True})
    concepts.sort(key=lambda x: (-x["zt_count"], -(x["net_inflow"] or 0)))

    return {"industries": out[:6], "concepts": concepts[:8]}


# ---------------------------------------------------------------------------
# ⑤ 风向标观察池
# ---------------------------------------------------------------------------
def _mmss(t):
    s = str(t or "").zfill(6)
    return f"{s[:2]}:{s[2:4]}" if len(s) == 6 else "--"


def wind_vane(md):
    """五类风向标 → 每类 1-3 只，合计收敛到 5-8 只。"""
    zt = md.zt_pool or []
    zb = md.zb_pool or []
    pool, seen = [], set()

    def add(kind, kind_label, p, why, plan, src="zt"):
        code = str(p.get("code") or "")
        if not code or code in seen:
            return
        one_word = src == "zt" and _is_one_word(p)
        if one_word:
            why = (f"{why} · 一字板（换手 {p.get('turnover')}%）不可成交，仅作情绪风向标")
            plan = {
                "strong": "一字板无法买入 —— 只观察封板是否维持，作为情绪强度的标尺",
                "diverge": "若开板 → 情绪转弱信号，立即下调全市场仓位预期",
                "weak": "低开/炸板 → 空间高度终结，主线大概率同步退潮",
            }
        seen.add(code)
        pool.append({
            "kind": kind, "kind_label": kind_label, "src": src,
            "one_word": one_word,
            "code": code, "name": p.get("name"), "industry": p.get("industry"),
            "lianban": p.get("lianban"), "pct": p.get("pct"),
            "first_time": _mmss(p.get("first_time")) if src == "zt" else None,
            "break_count": p.get("break_count"),
            "turnover": p.get("turnover"), "amount_yi": p.get("amount_yi"),
            "fund_yi": p.get("fund_yi"), "mv_yi": p.get("mv_yi"),
            "why": why, "plan": plan,
        })

    # 类别 1：空间龙头（最高板）
    if zt:
        mx = max((p.get("lianban") or 1) for p in zt)
        for p in [x for x in zt if (x.get("lianban") or 1) == mx][:1]:
            add("space", "空间龙头", p,
                f"{mx} 板空间高度，市场情绪最强载体；封单 {p.get('fund_yi')} 亿",
                {"strong": "高开抢筹并快速封板 → 持有，只减不加",
                 "diverge": "平开震荡/反复开板 → 减半兑现，不加仓",
                 "weak": "低开或破前日均价 → 清仓，龙头破位即板块退潮"})

    # 类别 2：率先封板先锋（首封最早，剔除缩量一字）
    early = sorted([p for p in zt if (p.get("amount_yi") or 0) >= 0.5],
                   key=lambda x: str(x.get("first_time") or "999999"))[:3]
    for p in early:
        if (p.get("lianban") or 1) == 1:
            add("pioneer", "率先封板先锋", p,
                f"首封 {_mmss(p.get('first_time'))}，早盘主动做多意愿最强",
                {"strong": "竞价高开 >2% 且 15 分钟内再封 → 打板或持有",
                 "diverge": "竞价平开、首封时间明显后移 → 只观察，不追",
                 "weak": "竞价低开 >3% → 放弃，先锋转弱即情绪转弱"})

    # 类别 3：换手接力候选（2-4 板 + 有量 + 换手适中）
    for p in sorted([x for x in zt if 2 <= (x.get("lianban") or 1) <= 4],
                    key=lambda x: -(x.get("amount_yi") or 0))[:3]:
        to = p.get("turnover") or 0
        if 4 <= to <= 30:
            add("relay", "换手接力", p,
                f"{p.get('lianban')} 板 · 换手 {to}% · 成交 {p.get('amount_yi')} 亿，"
                f"有量能承接（非一字板可参与）",
                {"strong": "放量高开直接接力 → 打板，止损设当日均价",
                 "diverge": "平开缩量 → 低吸均价线附近，破均价减半",
                 "weak": "低开破前收 → 不参与，接力失败通常连板结束"})

    # 类别 4：弱转强 / 反包候选（炸板但仍在高位）
    for p in sorted([x for x in zb if (x.get("pct") or 0) >= 5],
                    key=lambda x: -(x.get("pct") or 0))[:3]:
        add("reversal", "弱转强/反包", p,
            f"炸板 {p.get('break_count')} 次仍收 {p.get('pct')}%，次日有反包预期",
            {"strong": "竞价高开 + 快速回封 → 确认反包，小仓参与",
             "diverge": "竞价平开震荡 → 观望，等分时站稳均价",
             "weak": "持续走弱破均价 → 坚决不参与，反包失败即杀跌"},
            src="zb")

    # 类别 5：低位首板（1 板 + 中小市值 + 换手充分）
    for p in sorted([x for x in zt if (x.get("lianban") or 1) == 1 and
                     (x.get("mv_yi") or 0) <= 200],
                    key=lambda x: -(x.get("turnover") or 0))[:2]:
        add("lowstart", "低位首板", p,
            f"首板 · 流通市值 {p.get('mv_yi')} 亿 · 换手 {p.get('turnover')}%，"
            f"低位启动空间大",
            {"strong": "高开 >3% 且量能同步放大 → 小仓打板",
             "diverge": "平开缩量 → 观察题材纵深，不追",
             "weak": "低开破前收 → 放弃（首板失败多为炮灰）"})

    return pool[:8]


# ---------------------------------------------------------------------------
# ⑥ 纪律检查器
# ---------------------------------------------------------------------------
def discipline(md, adv=None):
    """对账户做纪律体检：追高度 / 漏执行 / 超配 / 跨时段重复。"""
    violations, notes = [], []
    pv = None
    try:
        import portfolio
        pv = portfolio.valuation()
    except Exception:
        pv = None

    pos = (pv or {}).get("positions") or []
    s = (pv or {}).get("summary") or {}

    # a) 超配：单一标的占比 > 25%
    mv = s.get("market_value") or 0
    if mv and pos:
        for p in pos:
            v = p.get("market_value") or p.get("value") or 0
            try:
                share = float(v) / float(mv) * 100
            except (TypeError, ValueError, ZeroDivisionError):
                continue
            if share >= 25:
                violations.append({
                    "kind": "超配", "level": 3,
                    "name": p.get("name") or "",
                    "symbol": p.get("symbol") or p.get("code") or "",
                    "text": f"{p.get('name')} 单一敞口 {share:.1f}%（>25%），"
                            f"单票黑天鹅可直接击穿账户",
                })

    # b) 执行缺口：卖出信号未执行
    try:
        d = json.loads((BASE.parent / "reports" / f"pending_sell_confirm_{date.today().isoformat()}.json")
                       .read_text(encoding="utf-8"))
        pend = [i for i in (d.get("items") or []) if not i.get("actual_sell")]
        for i in pend:
            violations.append({
                "kind": "漏执行", "level": 4,
                "name": i.get("name") or "",
                "symbol": i.get("symbol") or "",
                "action": i.get("action") or "",
                "lots": i.get("recommend_sell_lots") or i.get("held_lots") or 0,
                "text": f"{i.get('name')} {i.get('action')} 卖出建议未执行"
                        f"（现价 {i.get('price')}），卖出通道不通",
            })
    except Exception:
        pass

    # c) 追高度：近期买入价 vs 买入当日最高价
    bt = None
    for cand in (BASE.parent / "reports" / "backtest_database.json",):
        if cand.exists():
            try:
                bt = json.loads(cand.read_text(encoding="utf-8"))
                break
            except Exception:
                bt = None
    if bt:
        cutoff = (date.today() - timedelta(days=20)).isoformat()
        kline_miss = 0
        for p in (bt.get("active_positions") or []):
            code, bp, bd = p.get("symbol"), p.get("buy_price"), p.get("buy_date")
            if not code or not bp or not bd:
                continue
            if str(bd) < cutoff:
                continue  # 只体检近 20 个自然日内的建仓，避免陈旧条目长期刷屏
            bars = _kline(code, 10)
            day = next((b for b in bars if b["date"] == bd), None)
            if not day or not day.get("high"):
                kline_miss += 1
                continue
            try:
                gap = (float(bp) / float(day["high"]) - 1) * 100
            except (TypeError, ValueError, ZeroDivisionError):
                continue
            if gap >= -0.5:
                violations.append({
                    "kind": "追高度", "level": 4,
                    "name": p.get("name") or "",
                    "symbol": p.get("symbol") or p.get("code") or "",
                    "text": f"{p.get('name')}（{bd} 建仓）买入价 {round(float(bp), 3)} ≈ "
                            f"当日最高 {day['high']}（{gap:+.2f}%），买在当日最高点的典型追高",
                })
            elif gap <= -8:
                violations.append({
                    "kind": "低吸过早", "level": 2,
                    "name": p.get("name") or "",
                    "symbol": p.get("symbol") or p.get("code") or "",
                    "text": f"{p.get('name')}（{bd} 建仓）买入价 {round(float(bp), 3)} 低于"
                            f"当日最高 {day['high']}（{gap:+.2f}%），买在日内深跌区，"
                            f"需确认是否左侧接刀",
                })
        if kline_miss:
            notes.append(f"{kline_miss} 只持仓的日K数据暂不可用（接口超时），"
                         f"本轮未做追高体检，稍后刷新会自动补检")

    # d) 今日卖出/减仓信号（供人工核对）
    sig_n = 0
    if REVIEWS_DIR.exists():
        f = REVIEWS_DIR / f"{date.today().isoformat()}.json"
        if f.exists():
            try:
                a = json.loads(f.read_text(encoding="utf-8"))
                sig_n = len(a.get("exit") or [])
            except Exception:
                pass
    if sig_n:
        notes.append(f"今日归档含 {sig_n} 条卖出/减仓信号，请逐条对照执行情况")

    # e) 跨时段重复入选（今日无 order_plan 时静默）
    try:
        import glob
        seen = {}
        for fp in glob.glob(str(BASE.parent / "reports" / f"order_plan_*_{date.today().isoformat().replace('-', '')}.json")):
            try:
                o = json.loads(open(fp, encoding="utf-8").read())
            except Exception:
                continue
            for c in (o.get("buy_candidates") or []):
                code = c.get("symbol") or c.get("code")
                seen.setdefault(code, []).append(o.get("stage") or o.get("session"))
        dup = {k: v for k, v in seen.items() if len(v) > 1}
        for code, stages in dup.items():
            violations.append({
                "kind": "重复入选", "level": 3,
                "text": f"{code} 同时被 {', '.join(str(x) for x in stages)} 派单，"
                        f"跨时段未去重 → 敞口被重复放大",
            })
    except Exception:
        pass

    violations.sort(key=lambda x: -x["level"])
    return {"violations": violations, "notes": notes,
            "count": len(violations),
            "high_count": sum(1 for x in violations if x["level"] >= 4)}


# ---------------------------------------------------------------------------
# 次日交易预案
# ---------------------------------------------------------------------------
def next_day_plan(cycle, wind, mains, loss):
    """总仓位建议 + 逐只三情形预案 + 必须回避项。"""
    cap = cycle.get("position_cap") or 30
    stage = cycle.get("stage")
    avoid = []
    if loss.get("level", 1) >= 4:
        avoid.append("亏钱效应扩散中：不打板、不低吸、不接力高位连板")
    if stage in ("退潮", "冰点"):
        avoid.append(f"当前处于「{stage}」阶段：仓位上限 {cap}%，只做试错单，不做主攻")
    if stage == "加速":
        avoid.append("加速段风报比≈1:1：4 板以上一律不接力")
    if cycle.get("signals", {}).get("gap", 0) >= 1:
        avoid.append(f"连板中位断层 {cycle['signals']['gap']} 层：高标独舞，接力资金断档")
    avoid.append("不做竞价一字板（无法成交，属账面收益）")

    focus = []
    seen_ind = {}
    for w in wind:
        ind = w.get("industry") or ""
        seen_ind[ind] = seen_ind.get(ind, 0) + 1
    for name, n in sorted(seen_ind.items(), key=lambda x: -x[1])[:3]:
        if n >= 2:
            focus.append(f"{name}（{n} 只风向标，关注板块内联动）")
    for m in mains[:2]:
        focus.append(f"{m['name']}（{m['zt_count']} 家涨停 · {m['tag']}）")

    return {
        "position_cap": cap, "stage": stage,
        "avoid": avoid, "focus": focus[:5],
        "watch_count": len(wind),
        "checklist": [
            "开盘 15 分钟：看风向标竞价强弱，决定今日是进攻还是防守",
            "空间龙头若低开破均价 → 全市场情绪转弱，立即降仓",
            "主线板块若出现「涨停家数减半 + 龙头炸板」→ 主线结束",
            "只在预设信号出现时出手，禁止盘中临时起意追涨",
        ],
    }


# ---------------------------------------------------------------------------
# 归档
# ---------------------------------------------------------------------------
def archive(md, data, force=False):
    """落盘当日复盘，供次日验证环消费。仅交易日/有数据时写。"""
    if not force and not is_trading_day():
        return None
    zt = md.zt_pool or []
    if not zt:
        return None
    ARCH_DIR.mkdir(parents=True, exist_ok=True)
    f = ARCH_DIR / f"{date.today().isoformat()}.json"
    payload = {
        "date": date.today().isoformat(),
        "archived_at": datetime.now().strftime("%H:%M:%S"),
        "cycle": data.get("cycle", {}).get("signals", {}),
        "stage": data.get("cycle", {}).get("stage"),
        "zt_codes": [str(p.get("code")) for p in zt if p.get("code")],
        "zb_codes": [str(p.get("code")) for p in (md.zb_pool or []) if p.get("code")],
        "dt_codes": [str(p.get("code")) for p in (md.dt_pool or []) if p.get("code")],
        "wind_codes": [w["code"] for w in (data.get("wind") or [])],
        "mainline": [m["name"] for m in (data.get("main", {}).get("industries") or [])],
        "position_cap": data.get("plan", {}).get("position_cap"),
    }
    try:
        f.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        return None
    return str(f)


# ---------------------------------------------------------------------------
# 总装
# ---------------------------------------------------------------------------
def review_deep(md, adv=None, do_archive=True):
    """六引擎总装。任何单引擎异常都不拖垮整体。"""
    errs = []

    def safe(fn, default, *a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:
            errs.append(f"{getattr(fn, '__name__', fn)}: {type(e).__name__}: {e}")
            return default

    cycle = safe(emotion_cycle, {}, md)
    verify = safe(verify_yesterday, {"available": False, "note": "引擎异常"}, md)
    loss = safe(loss_effect, {}, md)
    main = safe(mainline, {"industries": [], "concepts": []}, md)
    wind = safe(wind_vane, [], md)
    disc = safe(discipline, {"violations": [], "notes": [], "count": 0, "high_count": 0}, md, adv)
    plan = safe(next_day_plan, {}, cycle, wind, main.get("industries") or [], loss)

    snap = md.snapshot() if hasattr(md, "snapshot") else {}
    out = {
        "time": snap.get("time"),
        "market_status": snap.get("market_status"),
        "status_text": snap.get("status_text"),
        "cycle": cycle,
        "verify": verify,
        "loss": loss,
        "main": main,
        "wind": wind,
        "discipline": disc,
        "plan": plan,
        "errors": errs,
    }
    if do_archive:
        out["archived"] = archive(md, out)

    # ===== ADR-0040 闭环第 1 环：复盘结论 → 次日交易约束表 =====
    # 这是「复盘有结论但用不上」的正面修正 —— 复盘不再只是看板，
    # 它写出的 trade_gate.json 会被次日调度器的买入通道真实执行。
    # 休市日 / 诊断模式(?noarch=1)不生成，避免用陈旧结论约束未来的交易日。
    if do_archive and is_trading_day():
        out["gate_generated"] = True
        try:
            import review_gate
            out["gate"] = review_gate.build_gate(out)
            # 上一次闸门的**实际执行留痕**（哪个时段放行了谁、剔除了谁）—— 供工作台展示
            out["gate_applied"] = review_gate.load_applied()
            # 闭环最后一环：度量**上一轮**闸门剔除票的今日表现（闸门有效 or 误杀）→ 自我校准
            out["gate_effect"] = review_gate.effectiveness()
        except Exception as e:
            errs.append(f"review_gate: {type(e).__name__}: {e}")
            out["gate"] = out["gate_applied"] = out["gate_effect"] = None
    else:
        # 休市 / 诊断模式：**不生成**新闸门（避免用陈旧结论约束未来交易日），
        # 但**仍要展示**已存在的闸门 —— 「展示」与「生成」是两件事，
        # 用户在长假里打开复盘模式，需要看到 10-08 将会执行什么约束。
        out["gate_generated"] = False
        out["gate_skipped"] = ("非交易日：本次不重新生成（避免用陈旧结论约束未来交易日），"
                               "以下为该闸门文件的当前内容"
                               if not is_trading_day() else
                               "诊断模式(?noarch=1)：本次不重新生成，以下为当前内容")
        try:
            import review_gate
            out["gate"] = review_gate.load_gate(strict=False)
            out["gate_applied"] = review_gate.load_applied()
            out["gate_effect"] = review_gate._read_effect()
        except Exception as e:
            errs.append(f"review_gate(display): {type(e).__name__}: {e}")
            out["gate"] = out["gate_applied"] = out["gate_effect"] = None
    return out

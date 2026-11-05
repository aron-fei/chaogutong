# -*- coding: utf-8 -*-
"""操盘心法引擎：基于《恶魔心法》上/下篇梳理的量化规则。

核心逻辑：
1. 情绪周期阶段判定（7 阶段：启动/确认/发酵/加速/分歧/反包/龙回头 + 冰点/退潮）
   数据输入：涨停家数 / 最高板 / 连板分布 / 炸板率 / 昨涨停溢价（可算的用，算不到的降级）
2. 各阶段自动输出操作心法（禁止项 + 机会项）
3. 防守三维：技术图形（指数 vs MA30/MA20）· 量能（今日 vs 近5日均量）· 周期位置
4. 心法 8 律（常驻纪律）
5. 涨停基因榜（板块近 N 日累计涨停，跨日落盘）
"""
import json
import os
from datetime import datetime, date
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
GENE_FILE = DATA_DIR / "gene_board.json"   # 板块涨停基因落盘：{date: {industry: count}}
GENE_DAYS = 8                              # 基因榜统计窗口（近8日）

# ---------------- 情绪周期阶段定义 ----------------
# 阶段判定规则（信号：zt 涨停 / dt 跌停 / zb_rate 炸板率 / max_board 最高板 /
#              gap 连板断层层数 / ratio 涨跌比）
#
# 2026-10-06 修复（ADR-0039）。旧实现三处结构性缺陷，实测 09-21~09-30 六个交易日
# 中 5 天判定与真实市况不符：
#   ① 顺序错误：把"加速"放第一条，退潮日容易被更高优先级的宽松规则吃掉；
#   ② max_board 用精确等值（== 2 / in (3,4)）：一旦出现 5/6/7 板即"高标独舞"结构，
#      7 条规则全部落空 → 回退"观察"，择时指引直接空白（09-30 实证）；
#   ③ 只读 涨停数/最高板/炸板率，完全不读跌停数与涨跌比：09-28 跌停 113 家、
#      涨跌比 0.20:1 的系统性杀跌日，旧规则判"分歧"并输出"低吸做反包"→ 方向相反。
#
# 修复原则：防守优先排序 + 区间判断取代等值 + 纳入亏钱效应（跌停/涨跌比）+ 断层检测。
STAGE_RULES = [
    # ---- 防守优先：亏钱效应主导，必须先判 ----
    ("退潮", lambda s: s["dt"] >= 50 or s["zb_rate"] >= 40
                       or (s["ratio"] is not None and s["ratio"] < 0.4),
     "🌊 退潮段：跌停 {dt} 家 / 炸板率 {zb_rate}% / 涨跌比 {ratio}，亏钱效应全面扩散。"
     "空仓防守，不接力不低吸不抄底，等待下一个冰点信号。"),
    ("冰点", lambda s: s["zt"] < 20 and s["max_board"] <= 3,
     "🧊 冰点段：涨停仅 {zt} 家、高度压至 {max_board} 板。极度悲观，"
     "仅 1-2 成仓试错前期人气股/超跌龙头，不打板不重仓，等拐点信号。"),
    # ---- 高位结构：高标独舞 / 中位断层 = 分歧 ----
    ("分歧", lambda s: s["max_board"] >= 4 and s["gap"] >= 1,
     "💥 分歧段：{max_board} 板高标独舞 + 中位断层 {gap} 层，接力资金断档。"
     "不碰高位连板，只做弱转强/低吸反包，控仓试探。"),
    ("分歧", lambda s: s["zb_rate"] >= 32
                       or (s["max_board"] >= 5 and s["zt"] < 60)
                       or (s["ratio"] is not None and s["ratio"] < 0.9 and s["zt"] >= 45),
     "💥 分歧段：炸板率 {zb_rate}%、涨跌比 {ratio}，高位票承压、赚钱效应收缩。"
     "规避高位连板，控仓做低位弱转强。"),
    # ---- 上升阶段 ----
    ("加速", lambda s: s["zt"] >= 90 and s["max_board"] >= 6 and s["zb_rate"] < 25,
     "🚀 加速段：情绪过热（涨停 {zt} 家 · {max_board} 板）。拒绝追高！"
     "4 板以上不接力，只持有并随时减仓兑现，风报比≈1:1。"),
    ("发酵", lambda s: s["zt"] >= 45 and s["max_board"] >= 3 and s["gap"] == 0
                       and s["zb_rate"] < 30 and s["dt"] < 30,
     "🔥 发酵段：涨停 {zt} 家、高度 {max_board} 板、梯队完整无断层。"
     "主线扩散，2 板低吸 / 打 3 板，坐主升顺风车。"),
    ("确认", lambda s: s["zt"] >= 25 and 1 <= s["max_board"] <= 2 and s["zb_rate"] < 35,
     "✅ 确认段（二板认龙头）：换手 2 板确定性最高！打放量 2 板或低吸辨识度龙头。"),
    ("启动", lambda s: s["zt"] < 45 and s["max_board"] <= 2,
     "🌱 启动段：混沌期首板试盘，炮灰概率高。仅小仓打首板，肉=次日溢价/波段，失败率高。"),
    ("反包", lambda s: s["max_board"] >= 3 and s["zb_rate"] >= 30,
     "↩️ 反包段：龙头惯性反弹。忌打板！仅低吸辨识度龙头，买在前日尾盘或今日急杀。"),
]
# 兜底阶段（数据结构不明确时才落这里，且必须给出仓位约束，不留空白）
DEFAULT_STAGE = ("观察", "⚪ 结构不明确（涨停 {zt} 家 · {max_board} 板 · 涨跌比 {ratio}）："
                 "保持观察，动作少一点，仓位控制在 30% 以内。")


def _fmt_law(tpl, s):
    """心法模板填充。缺失信号显示 '--'，格式化异常绝不吞掉心法本身。"""
    safe = {k: ("--" if v is None else v) for k, v in s.items() if k != "dist"}
    try:
        return tpl.format(**safe)
    except Exception:
        return tpl


def _calc_stage_signals(md):
    """从 MarketData 计算周期判定信号。

    2026-10-06（ADR-0039）新增 dt（跌停数）、ratio（涨跌比）、gap（连板断层数）。
    旧实现只有 zt/zb/zb_rate/max_board/dist 四个信号，无法识别"高位补跌"与
    "高标独舞+中位断层"两类退潮前兆。
    """
    zt_pool = md.zt_pool or []
    zb_pool = md.zb_pool or []
    dt_pool = getattr(md, "dt_pool", None) or []
    zt = len(zt_pool)
    zb = len(zb_pool)
    dt = len(dt_pool)
    zb_rate = round(zb / max(zt + zb, 1) * 100, 1)

    b = getattr(md, "breadth", None) or {}
    up, down = b.get("up"), b.get("down")
    ratio = None
    try:
        if up and down:
            ratio = round(float(up) / max(float(down), 1), 2)
    except (TypeError, ValueError):
        ratio = None

    # 连板分布 + 最高板
    boards = []
    for p in zt_pool:
        try:
            boards.append(int(p.get("lianban") or 1))
        except (TypeError, ValueError):
            boards.append(1)
    max_board = max(boards) if boards else 0
    dist = {}
    for x in boards:
        dist[x] = dist.get(x, 0) + 1

    # 断层：最高板向下直到 2 板之间缺失的层数（"高标独舞 / 中位断层"的量化）
    levels = {x for x in boards if x >= 2}
    gap = sum(1 for lvl in range(max_board - 1, 1, -1) if lvl not in levels)

    return {
        "zt": zt, "zb": zb, "dt": dt, "zb_rate": zb_rate,
        "max_board": max_board, "dist": dist,
        "gap": gap, "ratio": ratio, "up": up, "down": down,
    }


def cycle_judge(md):
    """情绪周期阶段判定 + 心法输出。『防守优先』顺序，首个命中即返回。"""
    s = _calc_stage_signals(md)
    for stage, cond, law in STAGE_RULES:
        try:
            if cond(s):
                return {"stage": stage, "law": _fmt_law(law, s), **s}
        except Exception:
            continue
    return {"stage": DEFAULT_STAGE[0], "law": _fmt_law(DEFAULT_STAGE[1], s), **s}


# ---------------- 防守三维：技术图形 / 量能 / 周期 ----------------
def defense_dimensions(indices, full_market):
    """防守维度检测。
    indices: {name: {pct, price, ma20, ma30}} 由 data_fetcher 提供指数均线
    full_market: {total, up, down, amount_yi} 全A量能
    """
    lights = []
    # 维度1：技术图形 —— 上证指数 vs MA30
    sh = (indices or {}).get("上证指数") or {}
    if sh.get("ma30"):
        if sh["price"] < sh["ma30"]:
            lights.append({"dim": "技术图形", "level": "red",
                           "msg": f"沪指 {sh['price']:.0f} < MA30 {sh['ma30']:.0f}：破 30 日线，进入防守状态，"
                                  "按《恶魔心法》应降低进攻仓位"})
        elif sh["price"] < sh.get("ma20", 1e18):
            lights.append({"dim": "技术图形", "level": "yellow",
                           "msg": f"沪指 {sh['price']:.0f} 跌破 MA20，半防守：控制仓位试探，不加仓"})
        else:
            lights.append({"dim": "技术图形", "level": "green",
                           "msg": f"沪指 {sh['price']:.0f} 站稳 MA30({sh['ma30']:.0f}) 上方，图形健康"})
    # 维度2：量能 —— 今日 vs 近5日均量
    if full_market and full_market.get("amount_yi") and full_market.get("avg5_yi"):
        a, a5 = full_market["amount_yi"], full_market["avg5_yi"]
        if a < a5 * 0.8:
            lights.append({"dim": "量能", "level": "yellow",
                           "msg": f"今日 {a:.0f} 亿 < 5日均量 {a5:.0f} 亿（缩量），追涨风险增大"})
        elif a > a5 * 1.3:
            lights.append({"dim": "量能", "level": "red",
                           "msg": f"今日 {a:.0f} 亿 放量 {a5:.0f} 亿 的 {a/a5:.1f} 倍，警惕高潮见顶"})
        else:
            lights.append({"dim": "量能", "level": "green",
                           "msg": f"今日 {a:.0f} 亿 vs 5日均量 {a5:.0f} 亿，量能正常"})
    return lights


# ---------------- 心法 8 律（静态纪律卡） ----------------
MIND_LAWS = [
    ("买在分歧、买在恐慌", "所有买入都考虑是否恐慌/分歧，绝不在加速阶段上车。"),
    ("卖在一致、卖在不涨停", "一致性狂热或不再涨停就是离场信号。"),
    ("买入只看 3 天", "买入后第 2 天早盘是极限，第 3 天早盘不涨停（趋势票 5%+）就离场。"),
    ("每天最高七成仓", "分 3 只，非满仓涨停不加，控制回撤优先。"),
    ("周期第 5 天后只减不加", "周期第 5 天（含）起除非龙头，不再加仓，只有持有和减仓。"),
    ("连续 2 日亏损空仓 1 日", "连亏 2 天强制空仓 1 天，控制持续回撤；看不懂/心情差一键清仓。"),
    ("不追加速段缩量板", "加速段风报比≈1:1，追缩量=给里面的人抬轿。"),
    ("动作少一点，出手果断一点", "少犯错比多出手重要，一击必中。"),
]


def mind_laws():
    return MIND_LAWS


# ---------------- 涨停基因榜（板块近 N 日累计） ----------------
def _load_gene():
    if GENE_FILE.exists():
        try:
            return json.loads(GENE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_gene(g):
    GENE_FILE.parent.mkdir(parents=True, exist_ok=True)
    GENE_FILE.write_text(json.dumps(g, ensure_ascii=False, indent=1), encoding="utf-8")


def record_gene(md, today=None):
    """把当日板块涨停分布落盘（每天一次）。"""
    today = today or date.today().isoformat()
    g = _load_gene()
    if g.get("_last") == today:
        return g
    # 统计当日板块涨停数（涨停池 industry）
    dist = {}
    for p in md.zt_pool or []:
        ind = p.get("industry") or p.get("板块") or "其他"
        dist[ind] = dist.get(ind, 0) + 1
    # 只保留近 GENE_DAYS 天
    keys = sorted([k for k in g.keys() if k != "_last"])
    g = {k: v for k, v in g.items() if k in keys[-GENE_DAYS + 1:] or k == "_last"}
    g[today] = dist
    g["_last"] = today
    _save_gene(g)
    return g


def sector_gene(md):
    """涨停基因榜：板块近8日累计涨停数排序 + 今日该板块涨停成员（按连板数倒序）。"""
    g = record_gene(md)
    agg = {}
    for d, dist in g.items():
        if d == "_last":
            continue
        for ind, cnt in dist.items():
            agg[ind] = agg.get(ind, 0) + cnt
    # 今日 zt_pool 按 industry 聚合成员（用于补「今日龙头」chips）
    today_map = {}  # industry -> [stock]
    for p in (md.zt_pool or []):
        ind = p.get("industry") or p.get("板块") or "其他"
        today_map.setdefault(ind, []).append({
            "code": p.get("code", ""),
            "name": p.get("name", ""),
            "lianban": p.get("lianban", 1),
            "pct": p.get("pct", 0),
        })
    rows = sorted(agg.items(), key=lambda kv: -kv[1])
    out = []
    for ind, cnt in rows[:12]:
        # 今日成员按连板数倒序、同连板按代码字典序稳定
        members = sorted(
            today_map.get(ind, []),
            key=lambda s: (-(s.get("lianban") or 1), s.get("code", ""))
        )
        out.append({
            "industry": ind,
            "count": cnt,
            "stocks": members[:4],   # 最多展示 4 个今日龙头
        })
    return out, list(g.keys())


# ---------------- 7 阶段进度条（图3 组件）----------------
STAGES_7 = [
    ("启动", "🌱", "打首板"),
    ("确认", "✅", "打首板2板"),
    ("发酵", "🔥", "2板或回分歧"),
    ("加速", "🚀", "补涨未必"),
    ("分歧", "💥", "跟随买怕"),
    ("反包", "↩️", "竞价低吸"),
    ("龙回头", "🐉", "龙头低吸"),
]


def stage_progress(current_stage):
    """7 阶段进度条：返回 [{name, icon, hint, current}, ...]，含 idx 用于高亮。
    若 current_stage 不在 7 阶段（如'退潮/观察'），高亮 None → 全部灰显。"""
    cur = current_stage if current_stage in [s[0] for s in STAGES_7] else None
    return {
        "stages": [
            {"name": k, "icon": ic, "hint": lab, "current": (k == cur)}
            for k, ic, lab in STAGES_7
        ],
        "current_idx": ([i for i, s in enumerate(STAGES_7) if s[0] == cur] or [None])[0],
        "current_key": cur,
    }


# ---------------- 当前阶段操作要点（图3 下半部分）----------------
OPERATION_POINTS = {
    "启动": [
        "首板试盘期,炮灰概率高",
        "操作建议:仅小仓打首板,肉=次日溢价/波段",
        "观察题材纵深,等待确认",
    ],
    "确认": [
        "二板认龙头,换手2板确定性最高",
        "操作建议:打放量2板或低吸辨识度龙头",
        "不追缩量,逢分歧建仓",
    ],
    "发酵": [
        "题材纵深,主升浪",
        "操作建议:4板以下可接力,5板以上观望",
        "低吸分歧中继,补涨龙有量",
        "买点后置,卖点前置",
    ],
    "加速": [
        "题材全线爆发,小票占优",
        "操作建议:持有不低仓,4板以上不接力",
        "风报比≈1:1,随时准备减仓",
    ],
    "分歧": [
        "炸板潮/冲高回落频发",
        "操作建议:不碰高位连板,低吸做反包",
        "控制仓位试探,亏钱效应显现",
    ],
    "反包": [
        "龙头惯性反弹窗口",
        "操作建议:忌打板!仅低吸辨识度龙头",
        "买在前日尾盘或今日急杀",
    ],
    "龙回头": [
        "周期尾段龙头低吸窗口",
        "操作建议:龙头恐慌杀跌时低吸",
        "仓位控制 30% 以内,一击必中",
    ],
    "冰点": [
        "涨停枯竭,高度压至 3 板以内",
        "操作建议:仅 1-2 成仓试错前期人气股/超跌龙头",
        "不打板不重仓,等拐点信号出现",
    ],
    "观察": [
        "结构不明确,方向未定",
        "操作建议:仓位控制在 30% 以内,只做已确认主线",
        "不追高,不接力高位连板",
    ],
    "退潮": [
        "涨停稀少,高度崩塌",
        "操作建议:空仓防守或仅试盘",
        "不接力不低吸,等待新周期",
    ],
}


def operation_points(stage):
    """返回当前阶段的操作要点列表（图3 下半部分）。"""
    return OPERATION_POINTS.get(stage, OPERATION_POINTS["观察"])


# ---------------- 拒绝追高风险提示（3 张图共有）----------------
RISK_ALERTS = {
    "加速": ("⛔ 拒绝追高",
             "4 板以上不接力,只持有不板,随时准备分歧兑现。"),
    "发酵": ("⛔ 拒绝追高",
             "5 板以上不接力,4 板以下择优接力,随时准备分歧兑现。"),
    "分歧": ("⛔ 拒绝追高",
             "高位连板全部不接力,只做低位弱转强/反包,控仓试探。"),
    "冰点": ("⚠️ 极致防守",
             "仅 1-2 成仓试错,不打板不重仓,等拐点信号。"),
    "退潮": ("⛔ 拒绝追高",
             "全部不接力,空仓等待新周期信号。"),
    "观察": ("⚠️ 结构不明",
             "仓位控制在 30% 以内,不追高不接力高位连板。"),
}


def risk_alert(stage, max_board):
    """返回 (title, msg)，若不在告警阶段返回 None（前端不渲染）。"""
    if stage in RISK_ALERTS:
        return {"title": RISK_ALERTS[stage][0], "msg": RISK_ALERTS[stage][1]}
    # 启发式：高板数也提示
    if max_board >= 6:
        return {"title": "⛔ 拒绝追高",
                "msg": f"{max_board} 板以上不接力,只持有不板,随时准备分歧兑现。"}
    return None


# ---------------- 涨停 CM 结构：10cm / 20cm-创业板 / 20cm-科创板 / 30cm-北交 ----------------
def _code_to_band(code: str) -> str:
    """根据股票代码判断所属板（决定涨停振幅上限）。"""
    s = str(code).lstrip("0")
    if not s:
        return "main_10cm"
    # 北交所：4xxxxx, 8xxxxx, 920xxx（新北证）, 830xxx, 836xxx
    if code.startswith(("4", "8", "920", "920xxx")) or code.startswith("830") or code.startswith("836"):
        return "bj_30cm"
    # 科创板 20cm
    if code.startswith(("688", "689")):
        return "kc_20cm"
    # 创业板 20cm（002xxx / 003xxx / 30xxxx 部分；30xxxx 中 301xxx 也属创业板）
    if code.startswith(("002", "003", "301")):
        return "cy_20cm"
    # 其他 30xxxx 也归 10cm（深市主板）
    return "main_10cm"


def cm_structure(zt_pool):
    """涨停池按 4 个板分类：主板10cm / 创业板20cm / 科创板20cm / 北交30cm。
    每类 {n: 涨停数, max_board: 最高连板, pct: 占总涨停比}。"""
    bands = {
        "main_10cm": {"key": "main_10cm", "label": "主板 10cm", "n": 0, "max_board": 0},
        "cy_20cm":   {"key": "cy_20cm",   "label": "创业板 20cm", "n": 0, "max_board": 0},
        "kc_20cm":   {"key": "kc_20cm",   "label": "科创板 20cm", "n": 0, "max_board": 0},
        "bj_30cm":   {"key": "bj_30cm",   "label": "北交 30cm", "n": 0, "max_board": 0},
    }
    total = 0
    for p in (zt_pool or []):
        b = _code_to_band(p.get("code", ""))
        bn = p.get("lianban", 1)
        bands[b]["n"] += 1
        if bn > bands[b]["max_board"]:
            bands[b]["max_board"] = bn
        total += 1
    for v in bands.values():
        v["pct"] = round(v["n"] / total * 100, 1) if total else 0.0
    # 按占比降序
    arr = sorted(bands.values(), key=lambda x: -x["pct"])
    return arr, total


# ---------------- 沪深300 vs 国证2000 权重对比（图2）----------------
def breadth_indices(indices):
    """沪深300（权重）vs 国证2000（小票）涨幅对比 + 小票占优判断。
    返回 {hs300: {name, pct}, gz2000: {name, pct}, diff: pct_diff, dominance: 'gz'|'hs'|'tie'}。
    若任一指数缺失 → 返回 None。"""
    if not indices:
        return None
    hs300 = gz2000 = None
    for it in indices:
        nm = it.get("name", "")
        if "沪深300" in nm:
            hs300 = it
        elif "国证2000" in nm or "国证 2000" in nm:
            gz2000 = it
    if not hs300 or not gz2000:
        return None
    diff = round(gz2000["pct"] - hs300["pct"], 2)
    if abs(diff) < 0.05:
        dominance = "tie"
    elif diff > 0:
        dominance = "gz"   # 小票强
    else:
        dominance = "hs"   # 权重强
    return {
        "hs300": {"name": hs300.get("name"), "pct": hs300.get("pct", 0)},
        "gz2000": {"name": gz2000.get("name"), "pct": gz2000.get("pct", 0)},
        "diff": diff,
        "dominance": dominance,
    }


# ---------------- 板块反包（图2 中段卡片）----------------
def sector_rebound(md):
    """板块反包：昨日跌、今日涨的板块数。
    数据来源：md.sectors 已有 'pct' / 'change_pct' / 'prev_pct' 字段。
    返回 {rebound_count, sample: [{name, today_pct, prev_pct}, ...]}。"""
    sectors = getattr(md, "sectors", None) or []
    # 数据结构可能差异：兜底用 pct vs prev_pct 比较
    reb = []
    for s in sectors:
        today = s.get("pct")
        prev = s.get("prev_pct")
        if today is None or prev is None:
            continue
        if prev < 0 and today > 0:
            reb.append({
                "name": s.get("name"),
                "today_pct": today,
                "prev_pct": prev,
            })
    reb.sort(key=lambda x: -x["today_pct"])
    return {
        "rebound_count": len(reb),
        "sample": reb[:5],
    }


# ---------------- 赚钱效应扩散与高度（图1 右下 5 维仪表）----------------
def money_effect(zt_pool, full_market):
    """赚钱效应 5 维：
    1. 市场中枢数 = 涨停数
    2. 扩散判断 = 当日最高板 >= 4 即'强扩散'
    3. 高度空间 = 最高板 / 90 (90 视为天花板板数；>= 90% 即'未压制')
    4. 红盘板块 = full_market.up / (up+down) 比例（数据可用时）
    5. 强度 = zt >= 80 强 / >= 40 中 / 弱
    """
    zt = len(zt_pool or [])
    max_board = max([p.get("lianban", 1) for p in (zt_pool or [])], default=0)
    up = (full_market or {}).get("up", 0)
    down = (full_market or {}).get("down", 0)
    total = up + down
    red_pct = round(up / total * 100, 1) if total else 0.0
    height_ratio = round(max_board / 90 * 100, 1) if max_board else 0.0
    if zt >= 80:
        strength = "强"
    elif zt >= 40:
        strength = "中"
    else:
        strength = "弱"
    return {
        "zt": zt,
        "max_board": max_board,
        "扩散判断": "强扩散" if max_board >= 4 else ("扩散" if max_board >= 2 else "弱扩散"),
        "height_ratio": height_ratio,
        "高度压制": "未压制" if height_ratio < 70 else ("压制临界" if height_ratio < 90 else "压制"),
        "red_pct": red_pct,
        "strength": strength,
        "小票占优": (full_market or {}).get("gz_dominate", False),
    }

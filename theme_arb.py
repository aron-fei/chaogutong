# -*- coding: utf-8 -*-
"""题材联动·短线套利引擎 (M1)

机制（对应用户场景：内蒙一机顶到 2 板 → 同题材竞价抢筹自动列队）：
  题材库(theme_library.json) 中某题材的龙头/成员在【涨停池】内 ≥ 触发板数(默认2板)
  → 判定题材触发 → 自动实时扫描该题材全部成分股行情，
  按「竞价高开幅度 / 量比 / 距涨停 / 成交额」代理指标排出套利候选池。

竞价倍数列（M1 降级口径，前端显示 -- / 待M2）：
  真实倍数 = 今日竞价成交额 ÷ 昨日竞价成交额，依赖竞价金额归档
  （M2：9:30 首分钟量落盘 daily_auction.json 后接入）。
  M1 阶段该列恒为 null，以 open_pct(竞价高开%) + vol_ratio(量比) + amount_yi 作盘中抢筹代理。

数据源：腾讯 qt.gtimg.cn（与全A同一数据源，规避东财 WAF）。
"""
import json
import time
from pathlib import Path
from datetime import datetime

from data_fetcher import quote_stocks, market_status

LIB_FILE = Path(__file__).parent / "data" / "theme_library.json"


def load_library():
    try:
        return json.loads(LIB_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"meta": {}, "themes": []}


def _zt_snapshot(md):
    """取涨停池只读快照（线程安全，同 dragon_radar 手法）。"""
    with md.lock:
        return [dict(s) for s in getattr(md, "zt_pool", None) or []]


def _leader_of(theme, zt_hits, trigger_board):
    """题材内涨停命中里选『最高连板 + 最早封板』作触发龙头。"""
    if not zt_hits:
        return None
    best = max(zt_hits, key=lambda s: (s.get("lianban", 0), -int(str(s.get("first_time", "999999"))[:6] or 999999)))
    if best.get("lianban", 0) < trigger_board:
        return None
    return best


def _row_state(q):
    """根据实时行情给成员打状态标签。"""
    if not q:
        return "无行情", "mute", None
    price = q.get("price") or 0
    zt_price = q.get("zt_price") or 0
    pct = q.get("pct") or 0
    sealed = zt_price > 0 and price >= zt_price * 0.998
    gap = None
    if zt_price > 0 and price > 0:
        if sealed:
            gap = 0.0
        else:
            gap = round((zt_price - price) / zt_price * 100, 2)  # 距涨停还差 %
    if sealed:
        return "封板中", "zt", gap
    if zt_price > 0 and gap is not None and gap <= 1.2:
        return "抢筹逼近", "near", gap
    if pct >= 5:
        return "跟涨强", "strong", gap
    if pct >= 0:
        return "跟涨", "weak", gap
    return "走弱", "down", gap


def scan(md):
    t0 = time.time()
    lib = load_library()
    meta = lib.get("meta", {})
    themes = lib.get("themes", [])
    trigger_board = int(meta.get("trigger_board", 2) or 2)
    status, status_text = market_status()
    errors = []
    zt = _zt_snapshot(md)
    zt_by_code = {s["code"]: s for s in zt}

    # ---- 1) 题材热力（全题材库 × 当日涨停池缓存） ----
    heat = []
    triggers = []
    pool = []
    quoted = {}          # code -> theme 触发集合
    leader_by_theme = {}  # theme_id -> zt股票(龙头)
    for th in themes:
        members = th.get("stocks", [])
        th_trigger = int(th.get("trigger_board", 0) or trigger_board)
        hits = [zt_by_code[m["code"]] for m in members if m["code"] in zt_by_code]
        # 热力
        lb_max = max((s.get("lianban", 0) for s in hits), default=0)
        heat.append({
            "id": th.get("id"), "name": th.get("name"), "group": th.get("group"),
            "zt_count": len(hits), "max_lb": lb_max,
            "zt_names": [s.get("name", "") for s in hits][:4],
        })
        # 触发判定：题材内最高板 ≥ 该题材触发板数
        leader = _leader_of(th, hits, th_trigger)
        if leader:
            triggers.append({
                "id": th.get("id"), "name": th.get("name"),
                "group": th.get("group"), "trigger_board": th_trigger,
                "leader": {
                    "code": leader["code"], "name": leader["name"],
                    "lianban": leader.get("lianban", 0),
                    "industry": leader.get("industry", ""),
                    "first_time": leader.get("first_time", ""),
                    "pct": leader.get("pct"),
                },
            })
            leader_by_theme[th.get("id")] = leader
            for m in members:
                if m["code"] in zt_by_code:
                    quoted.setdefault(m["code"], set()).add(th.get("id"))

    # ---- 1.5) 当日涨停行业动态聚合（题材库之外的实时补位，永不空） ----
    # 固定题材库是「建库快照」，覆盖不到当日轮动热点时会整块显示 0。
    # 这里直接用涨停池自带的 industry 字段（东财行业口径）做当日动态聚合，
    # 保证「今天什么在涨停」永远有答案；is_hot = 该行业内最高板 ≥ 触发线。
    ind_map = {}
    for s in zt:
        key = (s.get("industry") or "").strip() or "其他"
        d = ind_map.get(key)
        if d is None:
            d = ind_map[key] = {"name": key, "count": 0, "max_lb": 0,
                                "names": [], "amount_yi": 0.0}
        d["count"] += 1
        d["max_lb"] = max(d["max_lb"], int(s.get("lianban", 0) or 0))
        d["names"].append(s.get("name", ""))
        try:
            d["amount_yi"] = round(d["amount_yi"] + float(s.get("amount_yi", 0) or 0), 1)
        except (TypeError, ValueError):
            pass
    industry_heat = sorted(ind_map.values(),
                           key=lambda x: (-x["count"], -x["max_lb"], x["name"]))
    for x in industry_heat:
        x["is_hot"] = x["max_lb"] >= trigger_board
        x["top_names"] = x["names"][:4]
    industry_triggers = [x for x in industry_heat if x["is_hot"]]
    lib_hits_total = sum(h["zt_count"] for h in heat)
    lib_pool_total = sum(len(th.get("stocks", [])) for th in themes)
    if zt and lib_hits_total == 0:
        coverage_note = f"题材库 0 命中 / 当日涨停 {len(zt)} 只，已用当日涨停行业补位"
    elif zt:
        coverage_note = f"题材库命中 {lib_hits_total} 只 / 当日涨停 {len(zt)} 只"
    else:
        coverage_note = "涨停池为空（未开盘 / 数据源延迟）"

    # ---- 2) 触发题材成分股实时扫描（腾讯批量） ----
    scan_codes = []
    for th in themes:
        if th.get("id") in leader_by_theme:
            scan_codes += [m["code"] for m in th.get("stocks", [])]
    scan_codes = list(dict.fromkeys(scan_codes))
    quotes = {}
    if scan_codes:
        try:
            quotes = quote_stocks(scan_codes)
        except Exception as e:  # 网络异常不致命，保留空行情按名称降级展示
            errors.append(f"成分股行情拉取失败: {type(e).__name__} {e}")

    for th in themes:
        th_id = th.get("id")
        if th_id not in leader_by_theme:
            continue
        lb = leader_by_theme[th_id]
        leader_label = f"{lb['name']}·{lb['lianban']}板"
        for m in th.get("stocks", []):
            q = quotes.get(m["code"])
            state, state_cls, gap = _row_state(q)
            if q:
                prev_close = q.get("prev_close") or 0
                op = q.get("open") or 0
                open_pct = round((op / prev_close - 1) * 100, 2) if prev_close > 0 else None
                row = {
                    "theme_id": th_id, "theme_name": th.get("name"),
                    "theme_group": th.get("group"),
                    "leader_label": leader_label, "leader_lb": lb["lianban"],
                    "code": m["code"], "name": q.get("name") or m.get("name"),
                    "role": m.get("role", ""),
                    "price": q.get("price"), "pct": q.get("pct"),
                    "open_pct": open_pct, "vol_ratio": q.get("vol_ratio"),
                    "turnover": q.get("turnover"), "amount_yi": q.get("amount_yi"),
                    "gap_zt": gap, "state": state, "state_cls": state_cls,
                    "auction_multiple": None,   # M2 竞价归档后填充
                }
            else:
                # 无实时行情（退市/停牌/代码失效）：降级展示但不参与排序
                row = {
                    "theme_id": th_id, "theme_name": th.get("name"),
                    "theme_group": th.get("group"),
                    "leader_label": leader_label, "leader_lb": lb["lianban"],
                    "code": m["code"], "name": m.get("name"),
                    "role": m.get("role", ""),
                    "price": None, "pct": None, "open_pct": None,
                    "vol_ratio": None, "turnover": None, "amount_yi": None,
                    "gap_zt": None, "state": "无行情", "state_cls": "mute",
                    "auction_multiple": None,
                }
            pool.append(row)

    # 排序：触发高度高者前 → 封板中 → 抢筹逼近 → 高开幅度大 → 量比大
    def _sort_key(r):
        s_order = {"zt": 0, "near": 1, "strong": 2, "weak": 3, "down": 4, "mute": 5}
        return (-r["leader_lb"], s_order.get(r["state_cls"], 9),
                -(r.get("open_pct") if r.get("open_pct") is not None else -99),
                -(r.get("vol_ratio") if r.get("vol_ratio") is not None else -99))
    pool.sort(key=_sort_key)
    heat.sort(key=lambda h: (-h["zt_count"], -h["max_lb"], h["name"]))
    triggers.sort(key=lambda t: -t["leader"]["lianban"])

    return {
        "generated_at": datetime.now().strftime("%H:%M:%S"),
        "elapsed_ms": int((time.time() - t0) * 1000),
        "market_status": status,
        "status_text": status_text,
        "trigger_board": trigger_board,
        "degraded": True,
        "degrade_note": meta.get("degrade_note", ""),
        "themes_total": len(themes),
        "lib_pool_total": lib_pool_total,
        "zt_total": len(zt),
        "lib_hits_total": lib_hits_total,
        "coverage_note": coverage_note,
        "heat": heat,
        "industry_heat": industry_heat,
        "industry_triggers": industry_triggers,
        "triggers": triggers,
        "pool": pool,
        "errors": errors,
    }

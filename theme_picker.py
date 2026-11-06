# -*- coding: utf-8 -*-
"""首板推票 · L0 题材趋势层 + L1 低位挖掘层（2026-11-05 v2）

用户要求整理后的策略：
  「侧重首板、侧重趋势板块里推票、板块成趋势时找哪些票还没涨停但即将爆发、
    侧重做题材趋势、侧重挖掘 / 低位挖掘 / 即将冒头、市场也有新闻传闻」

本模块只做**方向 + 挖掘**，产出观察池；真正会语音播报的「临板首板」在
sprite.py 的 L2（因为要逐帧历史 + 五档盘口 + 连续确认）。

数据流（全部复用已缓存数据，零额外网络请求）：
    app._build_zt_theme_groups()  → 题材涨停家数 / 连板梯队 / 板块行情
    theme_board.codes_for()       → 题材 → 成分股（正向索引）
    md.get_full_rows()            → 全A最新帧（价/量比/五档/委差/均价/涨停价）
    news_catalyst.heat()          → 近 N 小时新闻传闻催化
                    ↓
    L0 板块强度分 → 趋势题材榜
                    ↓
    L1 启动分     → 低位观察池（未涨停 + 题材内相对低位 + 量能刚抬头）

设计原则
--------
* 多因子共振打分，不用一条死规则一票否决（用户既定策略观）。
* 「低位」= **题材内相对低位**（涨幅落后、距涨停远），不是跌得多的冷门票。
* 数据缺失的因子记 0 分并标注，绝不编造；题材级资金/涨速明确标「主行业近似」。
* 纯函数 + 只读输入，方便离线构造数据做单元测试。
* 自检：/opt/homebrew/anaconda3/bin/python3 theme_picker.py --selftest
"""
import time
from datetime import datetime

import config


def _clamp(v, lo=0.0, hi=1.0):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return lo


def _f(v, default=None):
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _norm(code):
    return str(code or "").strip().zfill(6)


def _is_excluded(name):
    """排除 ST / 退市 / 上市初期新股（N、C 前缀）。"""
    nm = str(name or "").upper().replace(" ", "")
    if not nm:
        return True
    if getattr(config, "FB_EXCLUDE_ST", True) and ("ST" in nm or "退" in nm):
        return True
    if getattr(config, "FB_EXCLUDE_NEW", True) and (nm.startswith("N") or nm.startswith("C")):
        return True
    return False


def board_progress(rec):
    """板进度 = (现价-昨收)/(涨停价-昨收)，0~1，自动适配 10/20/30/5% 涨停制度。

    主板 10% 票 +3~4% 只有 0.3~0.4（用户抱怨的早段误报）；
    +8.5% 是 0.85；创业板 20% 票 +17% 也是 0.85。这是 v2 的核心归一化。
    """
    price = _f(rec.get("price"))
    prev = _f(rec.get("prev_close"))
    zt = _f(rec.get("zt_price"))
    if price is None or prev is None or zt is None or zt <= prev or price <= 0:
        return None
    return _clamp((price - prev) / (zt - prev), 0.0, 1.2)


def news_heat_for(heat, theme):
    """题材催化条数：精确名优先，再做子串兜底（合并别名如「光模块·CPO」）。"""
    if not heat:
        return 0, []
    h = heat.get(theme)
    if h:
        return int(h.get("count") or 0), list(h.get("titles") or [])
    total, titles = 0, []
    for k, v in heat.items():
        if theme and (theme in k or k in theme):
            total += int(v.get("count") or 0)
            titles.extend(list(v.get("titles") or [])[:2])
    return total, titles[:3]


# ---------------------------------------------------------------- L0 题材趋势
def rank_trends(theme_groups, heat=None, top_n=None):
    """L0：按板块强度分挑出「成趋势」的题材。

    强度分 = 涨停家数 38 + 连板梯队 18 + 题材涨幅 14 + 板块涨速 10
           + 主力净流入 10 + 新闻催化 10（各因子归一化到 0~1 后加权）。
    """
    w = getattr(config, "FB_TREND_W", {})
    top_n = int(top_n or getattr(config, "FB_TREND_TOP_N", 12))
    out = []
    for g in theme_groups or []:
        name = str(g.get("name") or "").strip()
        count = int(g.get("count") or 0)
        ladder = int(g.get("max_lianban") or 0)
        if not name or _is_pseudo_theme(name):
            continue
        min_zt = int(getattr(config, "FB_TREND_MIN_ZT", 3))
        relaxed = (ladder >= int(getattr(config, "FB_TREND_LADDER_MIN", 3))
                   and count >= int(getattr(config, "FB_TREND_MIN_ZT_LADDER", 2)))
        if count < min_zt and not relaxed:
            continue
        news_n, news_titles = news_heat_for(heat, name)
        parts = {
            "zt_count": _clamp(count / 8.0),
            "ladder": _clamp(ladder / 4.0),
            "zdf": _clamp((_f(g.get("zdf"), 0) or 0) / 5.0),
            "speed": _clamp((_f(g.get("speed"), 0) or 0) / 1.5),
            "netflow": _clamp((_f(g.get("netflow_yi"), 0) or 0) / 10.0),
            "news": _clamp(news_n / 3.0),
        }
        score = round(sum(float(w.get(k, 0)) * v for k, v in parts.items()), 1)
        out.append({
            "name": name,
            "kind": g.get("kind"),
            "score": score,
            "zt_count": count,
            "max_lianban": ladder,
            "zdf": g.get("zdf"),
            "speed": g.get("speed"),
            "netflow_yi": g.get("netflow_yi"),
            "proxy_industry": g.get("proxy_industry"),
            "lead": g.get("lead"),
            "lead_pct": g.get("lead_pct"),
            "news_count": news_n,
            "news_titles": news_titles,
            "parts": {k: round(v, 2) for k, v in parts.items()},
            "leaders": [{"name": s.get("name"), "code": s.get("code"),
                         "lianban": s.get("lianban")}
                        for s in (g.get("stocks") or [])[:4]],
        })
    out.sort(key=lambda x: (-x["score"], -x["zt_count"], -x["max_lianban"]))
    return out[:top_n]


def _is_pseudo_theme(name):
    """二次防御：涨停池分组里若混入统计型伪概念，L0 不采纳。"""
    try:
        import theme_board
        return theme_board._is_pseudo(name)
    except Exception:
        return False


# ------------------------------------------------------------------ 会话态
def _session_state(frame_live):
    """按交易时钟给会话定性，避免把「盘前」误写成「收盘定版」造成误导。

    返回 (session, label, hint)。周末一律 closed；交易时段但无实时帧为 paused，
    此时同样不标「即将冒头」（数据不全时保守优先）。
    """
    now = datetime.now()
    hm = now.hour * 60 + now.minute
    if now.weekday() >= 5:
        return "closed", "休市", "周末休市，仅展示最近一个交易日的定版数据"
    if hm < 9 * 60 + 15:
        return "preopen", "盘前", "尚未开盘，当日涨停池为空；不产生实时低位候选"
    if hm < 9 * 60 + 30:
        return "auction", "集合竞价", "集合竞价中，尚未连续竞价；不产生实时低位候选"
    if 11 * 60 + 30 <= hm < 13 * 60:
        return "lunch", "午间", "午间休市，行情为上午收盘定版"
    if hm >= 15 * 60:
        return "closed", "收盘", "已收盘，低位池仅作次日观察名单"
    if not frame_live:
        return "paused", "待更新", "交易时段但未取到实时涨速帧，保守不标「即将冒头」"
    return "trading", "盘中", "实时帧可用，低位池要求 60s 涨速拐头"


# ------------------------------------------------- 盘前隔夜题材热度（兜底）
def overnight_trends(prev_zt_codes, tboard, heat=None, top_n=8):
    """当日涨停池为空时（盘前 / 盘后），用「昨日涨停成分股所属题材 + 真实快讯」
    给一份隔夜方向榜。明确标注隔夜口径，绝不冒充当日 L0 趋势，也不编造数据。

    只在 app 层判定需要兜底时调用；返回结构与 rank_trends 对齐，前端可直接渲染。
    """
    counts, news_map = {}, {}
    for c in prev_zt_codes or []:
        try:
            names = tboard.themes_for(_norm(c))
        except Exception:
            names = []
        for nm in names:
            nm = str(nm or "").strip()
            if not nm or _is_pseudo_theme(nm):
                continue
            counts[nm] = counts.get(nm, 0) + 1
    for nm in set(counts) | set(heat or {}):
        n, titles = news_heat_for(heat, nm)
        if n:
            news_map[str(nm)] = (n, titles[:2])
    out = []
    for nm in set(counts) | set(news_map):
        cnt = counts.get(nm, 0)
        n, titles = news_map.get(nm, (0, []))
        if cnt < 2 and n < 2:
            continue
        # 分母放宽到 10，避免多个题材同时饱和成 100 分造成「强度很确定」的错觉。
        parts = {"prev_zt": _clamp(cnt / 10.0), "news": _clamp(n / 10.0)}
        out.append({
            "name": nm, "kind": "concept",
            "score": round(60.0 * parts["prev_zt"] + 40.0 * parts["news"], 1),
            "zt_count": cnt, "max_lianban": 0,
            "zdf": None, "speed": None, "netflow_yi": None,
            "proxy_industry": None, "lead": None, "lead_pct": None,
            "news_count": n, "news_titles": titles,
            "parts": parts, "leaders": [],
            "source": "overnight",
        })
    out.sort(key=lambda x: (-x["score"], -x["zt_count"], x["name"]))
    return out[:int(top_n or 8)]


# ---------------------------------------------------------------- L1 低位挖掘
def _vol_shape(vr):
    """温和放量三角形：1.3~1.8 最理想（用户原标准），越靠近两端衰减。"""
    lo = float(getattr(config, "FB_DIG_VOL_MIN", 1.05))
    hi = float(getattr(config, "FB_DIG_VOL_MAX", 3.50))
    g_lo = float(getattr(config, "FB_VOL_GENTLE_LO", 1.30))
    g_hi = float(getattr(config, "FB_VOL_GENTLE_HI", 1.80))
    if vr is None or vr < lo or vr > hi:
        return 0.0
    if g_lo <= vr <= g_hi:
        return 1.0
    if vr < g_lo:
        return _clamp((vr - lo) / max(g_lo - lo, 0.01))
    return _clamp((hi - vr) / max(hi - g_hi, 0.01))


def discover(trends, full_rows, tboard, heat=None, avoid_codes=None,
             top_n=None, zt_codes=None, live=True, session_label="收盘"):
    """L1：在趋势题材成分股里找「还没涨停、位置低、即将冒头」的票。

    avoid_codes：今日已涨停 / 炸板 / 昨日已涨停 的合集（首板导向，不挖连板接力）。
    """
    top_n = int(top_n or getattr(config, "FB_DIG_TOP_N", 20))
    w = getattr(config, "FB_DIG_W", {})
    avoid = {_norm(c) for c in (avoid_codes or set())}
    ztset = {_norm(c) for c in (zt_codes or set())}
    by_code = {}
    for r in full_rows or []:
        c = _norm(r.get("code"))
        if c:
            by_code[c] = r

    min_pct = float(getattr(config, "FB_DIG_MIN_PCT", -1.0))
    max_pct = float(getattr(config, "FB_DIG_MAX_PCT", 5.5))
    min_amt = float(getattr(config, "FB_DIG_MIN_AMOUNT", 0.5))
    max_prog = float(getattr(config, "FB_DIG_MAX_PROGRESS", 0.62))
    vol_lo = float(getattr(config, "FB_DIG_VOL_MIN", 1.05))
    vol_hi = float(getattr(config, "FB_DIG_VOL_MAX", 3.50))
    min_score = float(getattr(config, "FB_DIG_MIN_SCORE", 52))

    picked, seen, stat = [], set(), {"theme_codes": 0, "no_quote": 0,
                                     "excluded": 0, "not_low": 0, "low_score": 0}
    for t in trends or []:
        try:
            codes = tboard.codes_for(t["name"])
        except Exception as e:
            print(f"[theme_picker] 取成分股失败 {t['name']}: {type(e).__name__}: {e}")
            continue
        stat["theme_codes"] += len(codes)
        for c in codes:
            c = _norm(c)
            if c in seen or c in avoid or c in ztset:
                stat["excluded"] += 1
                continue
            rec = by_code.get(c)
            if not rec:
                stat["no_quote"] += 1
                continue
            name = rec.get("name") or ""
            if _is_excluded(name):
                stat["excluded"] += 1
                continue
            pct = _f(rec.get("pct"))
            price = _f(rec.get("price"))
            amount = _f(rec.get("amount_yi"), 0) or 0
            vr = _f(rec.get("vol_ratio"))
            prog = board_progress(rec)
            if pct is None or price is None or prog is None:
                stat["no_quote"] += 1
                continue
            if not (min_pct <= pct <= max_pct) or prog > max_prog or amount < min_amt:
                stat["not_low"] += 1
                continue
            if vr is not None and not (vol_lo <= vr <= vol_hi):
                stat["not_low"] += 1
                continue
            sp1 = _f(rec.get("speed_1m"))
            # 盘中「即将冒头」必须有涨速拐头；收盘定版没有实时涨速，保留为
            # 次日观察池但绝不标成「即将冒头」（用户最在意误导性提示）。
            if live and (sp1 is None or sp1 < float(getattr(config, "FB_DIG_MIN_SP1", 0.10))):
                stat["not_low"] += 1
                continue

            # ---- 多因子启动分（缺失因子记 0，并在 reasons 里标注）----
            theme_s = _clamp(t["score"] / 100.0)
            pct_norm = _clamp((pct - min_pct) / max(max_pct - min_pct, 0.01))
            prog_norm = _clamp(prog / max(max_prog, 0.01))
            low_s = 0.6 * (1.0 - pct_norm) + 0.4 * (1.0 - prog_norm)
            vol_s = _vol_shape(vr)
            avg_p, open_p, prev_p = (_f(rec.get("avg_price")), _f(rec.get("open")),
                                     _f(rec.get("prev_close")))
            checks = [avg_p is not None and price > avg_p,
                      open_p is not None and price > open_p,
                      prev_p is not None and price > prev_p]
            trend_s = sum(1 for x in checks if x) / 3.0
            speed_s = _clamp((sp1 or 0) / 1.0)
            outer, inner = _f(rec.get("outer"), 0) or 0, _f(rec.get("inner"), 0) or 0
            weicha = _f(rec.get("weicha"))
            order_s = 0.0
            if outer + inner > 0:
                order_s = max(order_s, _clamp(outer / (outer + inner) * 1.4 - 0.2))
            if weicha is not None:
                b5, a5 = _f(rec.get("bid5_vol"), 0) or 0, _f(rec.get("ask5_vol"), 0) or 0
                if b5 + a5 > 0:
                    order_s = max(order_s, _clamp(weicha / (b5 + a5) * 2.0))
            parts = {"theme": theme_s, "low": low_s, "vol": vol_s,
                     "trend": trend_s, "speed": speed_s, "order": order_s}
            score = round(sum(float(w.get(k, 0)) * v for k, v in parts.items()), 1)
            if score < min_score:
                stat["low_score"] += 1
                continue

            reasons = [f"属趋势题材「{t['name']}」强度{t['score']:.0f}分"]
            if low_s >= 0.6:
                reasons.append(f"题材内相对低位（涨{pct:+.1f}% / 板进度{prog:.2f}）")
            if vol_s >= 0.7:
                reasons.append(f"温和放量 量比{vr:.2f}")
            if trend_s >= 0.66:
                reasons.append("价站上均价/开盘/昨收")
            if sp1 is not None and sp1 >= 0.1:
                reasons.append(f"60s 涨速{sp1:+.2f}% 拐头")
            else:
                reasons.append(f"{session_label}：无实时涨速，开盘后确认量能")
            if order_s >= 0.5:
                reasons.append("委买/外盘占优")
            _, n_titles = news_heat_for(heat, t["name"])
            if n_titles:
                reasons.append("有消息面催化")
            picked.append({
                "code": c, "name": name, "price": price, "pct": round(pct, 2),
                "amount_yi": round(amount, 2), "vol_ratio": vr,
                "board_progress": round(prog, 3), "score": score,
                "sp1": sp1, "weicha": weicha,
                "theme": t["name"], "theme_score": t["score"],
                "theme_zt_count": t["zt_count"], "theme_max_lianban": t["max_lianban"],
                "news_count": t.get("news_count") or 0,
                "news_titles": n_titles[:2],
                "parts": {k: round(v, 2) for k, v in parts.items()},
                "reasons": reasons,
                "tag": (f"{session_label}低位" if not live else
                        "量能抬头" if (sp1 or 0) >= 0.5 and (vr or 0) >= 1.2 else
                        "即将冒头" if score >= 70 else "低位观察"),
            })
            seen.add(c)
    picked.sort(key=lambda x: (-x["score"], -(x["sp1"] or 0), x["board_progress"]))
    return picked[:top_n], stat


def pick(theme_groups, full_rows, tboard, heat=None, avoid_codes=None,
         zt_codes=None, news_state=None, theme_state=None, rows_at=None):
    """对外主入口：一次算完 L0 + L1，返回可直接 jsonify 的 dict。"""
    t0 = time.time()
    try:
        # 会话态按交易时钟定性：盘前 ≠ 收盘，午休 ≠ 数据异常。
        # 只有「盘中 + 实时帧可用」才允许标「即将冒头」，避免误导。
        sample = (full_rows or [])[:300]
        frame_live = sum(1 for r in sample if r.get("speed_1m") is not None) > 20
        session, label, hint = _session_state(frame_live)
        live = session == "trading" and frame_live
        trends = rank_trends(theme_groups, heat=heat)
        rows, stat = discover(trends, full_rows, tboard, heat=heat,
                              avoid_codes=avoid_codes, zt_codes=zt_codes,
                              live=live, session_label=label)
        return {
            "ok": True,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "rows_at": datetime.fromtimestamp(rows_at).strftime("%H:%M:%S") if rows_at else None,
            "elapsed_ms": round((time.time() - t0) * 1000, 1),
            "trends": trends,
            "discoveries": rows,
            "meta": {
                "trend_count": len(trends),
                "candidate_count": len(rows),
                "session": session,
                "session_label": label,
                "session_hint": hint,
                "frame_live": frame_live,
                "trend_source": "live",
                "stat": stat,
                "news_state": news_state or {},
                "theme_state": theme_state or {},
                "note": ("L0 板块强度分 = 涨停家数/连板梯队/题材涨幅/板块涨速/资金/新闻催化加权；"
                         "L1 只在趋势题材成分股里找未涨停 + 题材内相对低位 + 量能刚抬头的票。"
                         "题材级资金/涨速按成分股主行业近似（无权威题材口径）。"
                         "本层为观察池，不产生语音播报；唯一强播报是 L2 临板首板。"),
                "config": {
                    "trend_min_zt": getattr(config, "FB_TREND_MIN_ZT", 3),
                    "dig_max_pct": getattr(config, "FB_DIG_MAX_PCT", 5.5),
                    "dig_max_progress": getattr(config, "FB_DIG_MAX_PROGRESS", 0.62),
                    "dig_min_score": getattr(config, "FB_DIG_MIN_SCORE", 52),
                },
            },
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "trends": [], "discoveries": [], "meta": {}}


# ------------------------------------------------------------------ 自检
if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        print("[1] 板进度归一化")
        assert abs(board_progress({"price": 10.3, "prev_close": 10.0,
                                   "zt_price": 11.0}) - 0.30) < 0.01
        assert abs(board_progress({"price": 10.85, "prev_close": 10.0,
                                   "zt_price": 11.0}) - 0.85) < 0.01
        assert abs(board_progress({"price": 11.7, "prev_close": 10.0,
                                   "zt_price": 12.0}) - 0.85) < 0.01
        assert board_progress({"price": 10.0, "prev_close": 10.0,
                               "zt_price": 0}) is None
        print("    OK：主板/创业板/异常值")

        print("[2] L0 题材趋势（构造 3 个题材，家数不足的应被过滤）")
        groups = [
            {"name": "商业航天", "kind": "concept", "count": 6, "max_lianban": 3,
             "zdf": 4.2, "speed": 1.1, "netflow_yi": 12.0,
             "stocks": [{"name": "航天A", "code": "600001", "lianban": 3}]},
            {"name": "光模块·CPO", "kind": "concept", "count": 5, "max_lianban": 2,
             "zdf": 3.0, "speed": 0.6, "netflow_yi": 5.0, "stocks": []},
            {"name": "冷门题材", "kind": "concept", "count": 1, "max_lianban": 1,
             "zdf": 0.4, "speed": 0.1, "netflow_yi": 0.2, "stocks": []},
        ]
        heat = {"商业航天": {"count": 3, "titles": ["卫星星座组网"]},
                "光模块·CPO": {"count": 1, "titles": ["800G 需求超预期"]}}
        tr = rank_trends(groups, heat=heat)
        print("    trends:", [(x["name"], x["score"]) for x in tr])
        assert [x["name"] for x in tr] == ["商业航天", "光模块·CPO"]
        assert tr[0]["score"] > tr[1]["score"]

        print("[3] L1 低位挖掘（趋势题材内低位票应入选，涨停/高位/ST 应被排除）")
        class FakeBoard:
            def codes_for(self, name):
                return ["300001", "300002", "300003", "300004", "300005"]

        def row(code, name, pct, price, vr=1.5, amt=2.0, sp1=0.4, zt=11.0):
            return {"code": code, "name": name, "pct": pct, "price": price,
                    "prev_close": 10.0, "open": 9.9, "zt_price": zt,
                    "avg_price": price - 0.05, "vol_ratio": vr, "amount_yi": amt,
                    "speed_1m": sp1, "outer": 600, "inner": 400,
                    "weicha": 300, "bid5_vol": 900, "ask5_vol": 400}
        rows = [
            row("300001", "低位航天", 2.1, 10.21),
            row("300002", "已冲高", 8.9, 10.89),      # 板进度 0.89 > 0.62
            row("300003", "*ST航天", 1.5, 10.15),     # ST
            row("300004", "冷票", 0.2, 10.02, vr=0.6),  # 量能不足
            row("300005", "巨量票", 3.0, 10.30, vr=8.0),
        ]
        got, st = discover(tr, rows, FakeBoard(), heat=heat,
                           avoid_codes={"300006"}, zt_codes=set())
        print("    picks:", [(x["name"], x["score"], x["tag"]) for x in got])
        print("    stat:", st)
        assert any(x["code"] == "300001" for x in got), "低位票应入选"
        assert all(x["code"] != "300002" for x in got), "高位票不应入选"
        assert all("ST" not in x["name"] for x in got), "ST 不应入选"

        print("[4] 新闻催化子串兜底")
        assert news_heat_for(heat, "商业航天")[0] == 3
        assert news_heat_for({}, "商业航天")[0] == 0

        print("[5] 会话态 + 盘前隔夜题材热度（不得冒充当日趋势）")
        valid = ("preopen", "auction", "trading", "lunch", "closed", "paused")
        assert _session_state(False)[0] in valid
        assert _session_state(True)[0] in valid

        class OvernightBoard:
            def themes_for(self, code):
                return ["商业航天", "光模块·CPO"] if code in ("600001", "600002", "600003") else []

        ov = overnight_trends(["600001", "600002", "600003", "600009"],
                              OvernightBoard(), heat=heat)
        print("    overnight:", [(x["name"], x["score"], x["zt_count"], x["news_count"]) for x in ov])
        assert ov and all(x["source"] == "overnight" for x in ov), "隔夜榜必须带口径标记"
        assert ov[0]["zt_count"] == 3, "昨日涨停家数应真实累计"
        assert ov[0]["news_count"] >= 1, "真实快讯热度应叠加"
        print("[selftest] PASS")
    else:
        print(__doc__)

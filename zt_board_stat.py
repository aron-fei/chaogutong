"""板块强度榜 · 涨停晋级率（2026-11-06 开盘啦借鉴）

设计前提（必须诚实）：
  开盘啦的「板块强度值」是私有算法，市面上没有任何公开文档给出公式，
  外部无法复刻。本模块用**公开可得字段的加权绝对刻度**给出「本地口径」强度，
  UI 上固定标注「本地口径」，绝不冒充开盘啦官方数值。

强度 = 10000 × Σ(w_i × x_i) / Σ(w_i)

  维度              权重   刻度 x（绝对值，非当日 min-max，故可跨时点比较）
  count   涨停家数  0.34   min(count / 8, 1)
  max_lianban 最高连板 0.20  min(lb / 5, 1)
  zdf     板块涨幅  0.18   clamp((zdf + 2) / 11, 0, 1)       # -2% → 0，+9% → 1
  speed   板块涨速  0.12   clamp((speed + 1) / 4, 0, 1)
  netflow 净流入亿  0.16   clamp((nf + 8) / 33, 0, 1)        # -8亿 → 0，+25亿 → 1

  缺失维度**剔除并重新归一**，不补 0。补 0 会冤枉「资金字段没匹配上」的板块，
  这是 2026-10-21 那次「炸板率恒 100%」同类坑（缺失 ≠ 最差）。

名次变化 rank_delta 依赖历史落盘 data/zt_strength_history.jsonl（5 分钟一档，
只记当日、只在 bucket 变化时追加）。没有历史 → 返回 None，前端不显示箭头，
不编造「上升 3 名」。
"""

from __future__ import annotations

import json
import math
from datetime import datetime

import config

HIST_FILE = config.BASE_DIR / "data" / "zt_strength_history.jsonl"
HIST_MAX_LINES = 600          # 超过则保留最近 400 行，避免长跑无限膨胀
STRENGTH_MAX = 10000

WEIGHTS = {
    "count": 0.34,
    "max_lianban": 0.20,
    "zdf": 0.18,
    "speed": 0.12,
    "netflow": 0.16,
}

_last_bucket: dict[str, str] = {}


def _finite(v):
    """东财 / pandas 的 NaN 是 truthy，float(nan) or 0 仍是 nan（2026-11-06 踩过）。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _clamp01(v):
    return 0.0 if v <= 0 else (1.0 if v >= 1 else v)


def _bucket(now=None):
    now = now or datetime.now()
    return now.strftime("%Y%m%d%H") + "%02d" % ((now.minute // 5) * 5)


def _axis(g, key):
    """把一个维度映射到 [0,1]；返回 None 表示该板块没有这个字段。"""
    if key == "count":
        c = _finite(g.get("count"))
        return None if c is None else _clamp01(c / 8.0)
    if key == "max_lianban":
        lb = _finite(g.get("max_lianban"))
        return None if lb is None else _clamp01(lb / 5.0)
    if key == "zdf":
        v = _finite(g.get("zdf"))
        return None if v is None else _clamp01((v + 2.0) / 11.0)
    if key == "speed":
        v = _finite(g.get("speed"))
        return None if v is None else _clamp01((v + 1.0) / 4.0)
    if key == "netflow":
        v = _finite(g.get("netflow_yi"))
        return None if v is None else _clamp01((v + 8.0) / 33.0)
    return None


def strength_of(g):
    """返回 (strength 0~10000, 参与计算的维度数)。缺字段越多置信度越低。"""
    num = 0.0
    den = 0.0
    used = 0
    for key, w in WEIGHTS.items():
        x = _axis(g, key)
        if x is None:
            continue
        num += w * x
        den += w
        used += 1
    if den <= 0:
        return 0, 0
    return int(round(STRENGTH_MAX * num / den)), used


# ---------------------------------------------------------------- 历史落盘


def _read_hist():
    if not HIST_FILE.exists():
        return []
    out = []
    try:
        with HIST_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue          # 半行 / 断电残行跳过，不因一行坏掉整份历史
    except OSError:
        return []
    return out


def _gname(g):
    """题材维度用 name，行业维度用 industry —— 统一取一个名字键。

    2026-11-06 踩坑：行业分组没有 name 字段，导致历史 items 写成 {}，
    名次变化永远是 None（不是「没变化」，是「没记上」）。
    """
    return (g.get("name") or g.get("industry") or "").strip()


def _append_hist(rec):
    try:
        HIST_FILE.parent.mkdir(parents=True, exist_ok=True)
        # 必须「读旧 + 追加」，不能只在超阈值时才读 —— 否则文件小时每次都整份覆盖，
        # 永远只剩最后一条，第二档历史永远等不到（2026-11-06 实际踩到）。
        keep = _read_hist()
        # 同一 (bucket, dim) 只留最新一条：进程重启会清空内存里的 _last_bucket，
        # 否则同一个 5 分钟档会被反复追加，历史文件白白膨胀。
        key = (rec.get("b"), rec.get("dim"))
        keep = [x for x in keep if (x.get("b"), x.get("dim")) != key]
        if len(keep) >= HIST_MAX_LINES:
            keep = keep[-400:]
        keep.append(rec)
        HIST_FILE.write_text(
            "\n".join(json.dumps(x, ensure_ascii=False) for x in keep) + "\n",
            encoding="utf-8")
    except OSError as e:
        print(f"[zt_board_stat] 历史落盘失败: {type(e).__name__}: {e}")


def prev_strength_map(dim, excludes_bucket=None):
    """取该维度**上一次**快照的 name→strength（不含当前 bucket）。"""
    prev = {}
    for rec in _read_hist():
        if rec.get("dim") != dim:
            continue
        if excludes_bucket and rec.get("b") == excludes_bucket:
            continue
        items = rec.get("items") or {}
        if isinstance(items, dict):
            prev = items          # 顺序覆盖 → 最后一条（最新）胜出
    return prev


# ---------------------------------------------------------------- 主入口


def annotate(groups, dim, record=True):
    """就地给每个板块补 strength / rank / rank_delta / strength_prev / hot_streak。

    hot_streak: 连续 ≥2 个快照 strength ≥ 6000 → True，用于「🔥 持续火爆」标记。
    只靠当下一个点很难说「持续」，所以要求前后两档都过线，取不到前值就不标。
    """
    groups = groups or []
    b = _bucket()
    prev = prev_strength_map(dim, excludes_bucket=b)
    used_hist = bool(prev)

    scored = []
    for g in groups:
        s, used = strength_of(g)
        g["strength"] = s
        g["strength_dims"] = used
        scored.append(g)
    scored.sort(key=lambda x: (-x.get("strength", 0), -x.get("count", 0)))
    for i, g in enumerate(scored):
        g["rank"] = i + 1
        p = prev.get(_gname(g))
        if p is None:
            g["strength_prev"] = None
            g["rank_delta"] = None
            g["hot_streak"] = False
        else:
            g["strength_prev"] = p
            g["hot_streak"] = bool(p >= 6000 and g["strength"] >= 6000)
            # 名次变化需要上一档的名次，靠 strength 反推（同类排序一致）
            rank_prev = 1 + sum(1 for v in prev.values() if v > p)
            g["rank_delta"] = rank_prev - g["rank"]
        # 「持续火爆」也可由涨停梯队支撑：≥3 家且最高 ≥3 板
        if not g["hot_streak"] and g.get("count", 0) >= 3 and g.get("max_lianban", 0) >= 3:
            g["hot_streak"] = True

    if record:
        try:
            _record(dim, b, scored)
        except Exception as e:                     # 落盘失败不影响主流程
            print(f"[zt_board_stat] 落盘异常: {type(e).__name__}: {e}")
    return {"groups": scored, "has_history": used_hist, "bucket": b}


def _record(dim, bucket, scored):
    global _last_bucket
    if _last_bucket.get(dim) == bucket:
        return
    top = sorted(scored, key=lambda x: -x.get("strength", 0))[:80]
    _append_hist({
        "b": bucket,
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "dim": dim,
        "items": {_gname(g): g.get("strength", 0) for g in top if _gname(g)},
    })
    _last_bucket[dim] = bucket


# ---------------------------------------------------------------- 晋级率 / 亏钱效应


def promotion_stats(zt_pool, prev_codes, prev_items=None, full_rows=None):
    """涨停晋级率 + 昨日涨停今日表现（开盘啦「涨停表现 / 亏钱效应」）。

    全部字段都能由「今日涨停池 + 昨日涨停基线 + 全A帧」现算，不引入新数据源。
    昨日分层（1板→2板 晋级率）需要 prev_items 里带 lianban；老缓存只有 codes，
    此时 prev_ladder 返回空列表并标 known=False，前端不显示，绝不编造。
    """
    prev_codes = {str(c).zfill(6) for c in (prev_codes or []) if str(c).strip()}
    today = {}
    for s in zt_pool or []:
        c = str(s.get("code") or "").strip().zfill(6)
        if c:
            today[c] = int(_finite(s.get("lianban")) or 1)

    promoted = [c for c in today if c in prev_codes]
    n_prev = len(prev_codes)
    out = {
        "prev_count": n_prev,
        "promoted": len(promoted),
        "promote_rate": round(100.0 * len(promoted) / n_prev, 1) if n_prev else None,
        "prev_avg_pct": None,
        "prev_down_ratio": None,
        "prev_ladder": [],
        "prev_ladder_known": False,
    }

    # 昨日分层晋级：昨 N 板 → 今日 ≥N+1 板
    prev_lb = {}
    for it in prev_items or []:
        c = str(it.get("code") or "").strip().zfill(6)
        lb = _finite(it.get("lianban"))
        if c and lb:
            prev_lb[c] = int(lb)
    if prev_lb:
        base = {}
        hit = {}
        for c, lb in prev_lb.items():
            base[lb] = base.get(lb, 0) + 1
            t = today.get(c)
            if t is not None and t > lb:
                hit[lb] = hit.get(lb, 0) + 1
        out["prev_ladder"] = [
            {"from": lb, "to": lb + 1, "base": base[lb], "hit": hit.get(lb, 0),
             "rate": round(100.0 * hit.get(lb, 0) / base[lb], 1)}
            for lb in sorted(base) if base[lb] > 0
        ]
        out["prev_ladder_known"] = True
    else:
        # 没有分层信息时，退化成「昨日全部 → 今日涨停」一个总口径，并标明不分组
        if n_prev:
            out["prev_ladder"] = [{
                "from": None, "to": None, "base": n_prev,
                "hit": len(promoted),
                "rate": round(100.0 * len(promoted) / n_prev, 1),
            }]

    # 亏钱效应：昨日涨停股今日涨跌幅（全A帧可得时）
    if full_rows and prev_codes:
        pcts = []
        for r in full_rows:
            c = str(r.get("code") or "").strip().zfill(6)
            if c in prev_codes:
                p = _finite(r.get("pct"))
                if p is not None:
                    pcts.append(p)
        if pcts:
            out["prev_avg_pct"] = round(sum(pcts) / len(pcts), 2)
            out["prev_down_ratio"] = round(100.0 * sum(1 for p in pcts if p < 0) / len(pcts), 1)
    return out


def zha_rate(zt_count, zb_count):
    """炸板率 = 炸板 / (涨停 + 炸板)。两者都为 0 时返回 None，不返回 0 假装「无炸板」。"""
    z = _finite(zt_count) or 0
    b = _finite(zb_count) or 0
    if z + b <= 0:
        return None
    return round(100.0 * b / (z + b), 1)


# ---------------------------------------------------------------- selftest


def _selftest():
    ok = True

    def chk(name, cond, extra=""):
        nonlocal ok
        print(("  PASS " if cond else "  FAIL ") + name + (f" {extra}" if extra else ""))
        if not cond:
            ok = False

    print("[1] 缺维度剔除并重归一，不补 0")
    a = {"name": "全字段", "count": 8, "max_lianban": 5, "zdf": 9.0, "speed": 3.0, "netflow_yi": 25.0}
    b = {"name": "无资金字段", "count": 8, "max_lianban": 5, "zdf": 9.0, "speed": 3.0}
    sa, ua = strength_of(a)
    sb, ub = strength_of(b)
    chk("满刻度 = 10000", sa == STRENGTH_MAX, f"got={sa}")
    chk("缺字段仍有强度且不为 0", sb > 0 and sb <= STRENGTH_MAX, f"got={sb}")
    chk("参与维度数正确", ua == 5 and ub == 4, f"{ua}/{ub}")

    print("[2] NaN 不得污染强度")
    c = {"name": "NaN资金", "count": 3, "max_lianban": 2, "zdf": 1.0, "speed": None,
         "netflow_yi": float("nan")}
    sc, uc = strength_of(c)
    chk("NaN 被剔除", uc == 3 and sc > 0, f"got={sc} dims={uc}")

    print("[3] 名次变化：无历史不编造")
    res = annotate([dict(a), dict(b)], "selftest_nohist", record=False)
    r = res["groups"][0]
    chk("无历史 rank_delta=None", r["rank_delta"] is None, f"got={r['rank_delta']}")
    chk("强度降序排名", res["groups"][0]["rank"] == 1 and res["groups"][1]["rank"] == 2)

    print("[4] 晋级率 / 亏钱效应")
    zt = [{"code": "000001", "lianban": 2}, {"code": "000002", "lianban": 1}]
    ps = promotion_stats(zt, {"000001", "000003", "000004"},
                         prev_items=[{"code": "000001", "lianban": 1},
                                     {"code": "000003", "lianban": 1},
                                     {"code": "000004", "lianban": 2}],
                         full_rows=[{"code": "000003", "pct": -3.5},
                                    {"code": "000001", "pct": 10.0}])
    chk("昨 3 只今晋级 1 只 = 33.3%", ps["promote_rate"] == 33.3, f"got={ps['promote_rate']}")
    chk("分层 1→2 晋级 1/2", ps["prev_ladder"][0]["from"] == 1
        and ps["prev_ladder"][0]["rate"] == 50.0, f"got={ps['prev_ladder'][:1]}")
    chk("分层 2→3 晋级 0/1", ps["prev_ladder"][1]["from"] == 2
        and ps["prev_ladder"][1]["rate"] == 0.0, f"got={ps['prev_ladder'][1:2]}")
    chk("昨涨停今日均涨 = 3.25", ps["prev_avg_pct"] == 3.25, f"got={ps['prev_avg_pct']}")
    chk("亏钱占比 50%", ps["prev_down_ratio"] == 50.0, f"got={ps['prev_down_ratio']}")

    print("[5] 炸板率 0/0 返回 None")
    chk("0/0 → None", zha_rate(0, 0) is None)
    chk("10/5 → 33.3", zha_rate(10, 5) == 33.3, f"got={zha_rate(10, 5)}")

    print("[6] 历史落盘：追加不覆盖 + 行业键名不写空 + 第二档有名次变化")
    import tempfile
    from pathlib import Path as _P
    global HIST_FILE, _last_bucket
    old_file, old_bucket = HIST_FILE, dict(_last_bucket)
    with tempfile.TemporaryDirectory() as td:
        HIST_FILE = _P(td) / "h.jsonl"
        _last_bucket.clear()
        gs = [{"industry": "光伏设备", "count": 4, "max_lianban": 4,
               "zdf": 1.0, "speed": 0.5, "netflow_yi": 3.0}]
        annotate(gs, "industry", record=True)
        hist = _read_hist()
        chk("行业维度写入真实名字", bool(hist) and "光伏设备" in hist[0]["items"],
            f"got={hist[0]['items'] if hist else None}")
        _append_hist({"b": "prev_bucket", "dim": "theme", "items": {"A": 5000, "B": 9000}})
        chk("追加不覆盖上一档", len(_read_hist()) == 2, f"lines={len(_read_hist())}")
        gs2 = [{"name": "A", "count": 8, "max_lianban": 5, "zdf": 9.0, "speed": 3.0,
                "netflow_yi": 25.0},
               {"name": "B", "count": 1, "max_lianban": 1}]
        out = annotate(gs2, "theme", record=True)
        chk("第二档能算出名次变化", out["has_history"] and out["groups"][0]["rank_delta"] == 1,
            f"delta={out['groups'][0]['rank_delta']}")
    HIST_FILE, _last_bucket = old_file, dict(old_bucket)

    print("SELFTEST", "ALL PASS" if ok else "HAS FAIL")
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if _selftest() else 1)

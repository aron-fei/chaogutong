# -*- coding: utf-8 -*-
"""交叉共振 · 跟庄建仓 × 底部放量 双信号验证系统（ADR-0055）

用户诉求（2026-11-23 13:16）：
  「同时出现在跟庄建仓命中和底部放量推荐的股票，做推荐 + 记录 + 验证次日走势/成功率/盈亏比」

核心原则：不编数字。所有统计来自真实历史日K重放，口径透明可复算。

## 三个功能

1. **今日交叉信号**：实盘两个扫描器结果的交集（genzhuang_scan 命中 ∩ bottom_surge 推荐）
2. **历史回测**：对全A逐只拉历史日K，在每个交易日 T 重放两个形态条件，取交集信号，
   验证 T+1 实际走势 → 成功率 / 平均收益 / 盈亏比 / 最大回撤
3. **信号台账**：信号落盘 JSONL，支持次日回填实际走势，长期累积真实样本

## 回测口径（严格，避免未来函数）

- 信号日 T：截至 T 收盘的日K，两条件同时成立
- 买入：T+1 开盘价（不在 T 日收盘买，那是未来函数）
- 卖出：T+1 收盘价
- 收益 = (T+1收 - T+1开)/T+1开 - 成本(0.1% 单边×2 = 0.2%)
- 成功率 = 收益>0 占比；盈亏比 = 平均盈利/abs(平均亏损)
- 只统计有完整 T+1 数据的信号（末日信号无次日数据 → 不计入，进台账待回填）

## 历史重放的两个条件（与实盘口径一致）

跟庄建仓：genzhuang_xg() 公式四条件（AA<13 / CC>13 / MA5>MA10 / 收阳）
底部放量：pos120≤0.40 / dd≥0.25 / price≤MA60×1.10 / 量比(当日量÷前5日均量)≥2.0 /
          AMT_MULT≥1.6 / score≥70（用收盘日K算等价量，progress=1 收盘态）

只读行情；台账写入独立 data/ 文件（这是本模块唯一写操作，且只写信号记录不碰任何账户）。
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import bottom_surge as bs
import strategy_router as sr
import genzhuang_scan as gs

DATA_DIR = Path(__file__).parent / "data"
LEDGER_FILE = DATA_DIR / "resonance_ledger.jsonl"
BACKTEST_FILE = DATA_DIR / "resonance_backtest.json"

COST_RATE = 0.002          # 双边成本 0.2%（佣金+印花税+滑点近似）
DEFAULT_LOOKBACK = 120     # 回测最近 N 个交易日
MAX_WORKERS = 8
SAMPLE_LIMIT = 15          # 报告里最多展示的信号样例数


# ---------------------------------------------------------------------------
# 历史重放：在截至 idx 的日K上判断两个条件
# ---------------------------------------------------------------------------
def _genzhuang_at(k: list[dict], idx: int) -> dict | None:
    """截至 idx（含）跑跟庄建仓公式。返回 dict 或 None。"""
    if idx < 32:
        return None
    win = k[idx - 39:idx + 1] if idx >= 39 else k[:idx + 1]
    if len(win) < 33:
        return None
    c = [float(x["close"]) for x in win]
    h = [float(x["high"]) for x in win]
    l = [float(x["low"]) for x in win]
    v = [float(x.get("vol_shou", 0)) for x in win]
    g = sr.genzhuang_xg(c, h, l, v)
    return g if g.get("hit") else None


def _bottom_at(k: list[dict], idx: int) -> dict | None:
    """截至 idx（含）判断底部放量推荐条件。收盘态重放，返回打分 dict 或 None。"""
    if idx < 120:
        return None
    # 忠实重放实盘 _kstats：h120/l120/ma20/ma60 用「全部可用历史」（daily_k 默认250根），
    # 而非字面120根——实盘变量名虽叫 h120，实际取的是 max(highs of all bars)。
    hist = k[:idx]                      # 截至 T 日之前的历史（不含 T）
    if len(hist) < 60:
        return None
    price = float(k[idx]["close"])
    today_vol = float(k[idx].get("vol_shou", 0))
    pct = (price / float(k[idx - 1]["close"]) - 1) * 100 if idx >= 1 else 0.0
    highs = [float(x["high"]) for x in hist]
    lows = [float(x["low"]) for x in hist]
    closes = [float(x["close"]) for x in hist]
    h120, l120 = max(highs), min(lows)
    ma20 = sum(closes[-20:]) / 20.0
    ma60 = sum(closes[-60:]) / 60.0
    avg_vol5 = sum(float(x["vol_shou"]) for x in hist[-5:]) / 5.0
    avg_vol20 = sum(float(x["vol_shou"]) for x in hist[-20:]) / 20.0
    if h120 <= l120 or avg_vol5 <= 0 or ma60 <= 0:
        return None
    pos = (price - l120) / (h120 - l120)
    dd = (h120 - price) / h120
    vr = today_vol / avg_vol5            # 收盘态量比 = 当日量 / 前5日均量
    amt_yi = today_vol * price / 1e6     # 手×100股×价 / 1e8 = vol*price/1e6
    avg_amt20_yi = avg_vol20 * price / 1e6
    amt_mult = amt_yi / avg_amt20_yi if avg_amt20_yi > 0 else 0
    # 底部放量实盘过滤（收盘态）
    if pos > bs.POS_MAX or dd < bs.DD_MIN:
        return None
    if price / ma60 > bs.MA60_MAX:
        return None
    if vr < bs.VR_STRONG or avg_vol5 < bs.MIN_AVG_VOL_SHOU:
        return None
    if amt_mult < bs.AMT_MULT_MIN:
        return None
    # 打分（与 _score 同口径）
    clamp = bs._clamp
    s_pos = bs.W_POS * clamp((bs.POS_MAX - pos) / bs.POS_MAX)
    s_dd = bs.W_DD * clamp((dd - bs.DD_MIN) / (0.60 - bs.DD_MIN))
    s_vol = bs.W_VOL * clamp((vr - bs.VR_STRONG) / (bs.VR_TOP - bs.VR_STRONG))
    s_pct = bs.W_PCT * pow(2.718281828, -0.5 * ((pct - bs.PCT_BEST) / bs.PCT_SIGMA) ** 2)
    s_liq = bs.W_LIQ * clamp(amt_yi / bs.AMT_FULL)
    score = round(s_pos + s_dd + s_vol + s_pct + s_liq, 1)
    if score < bs.RECOMMEND_MIN_SCORE:
        return None
    return {"score": score, "pos": round(pos, 3), "dd": round(dd, 3),
            "vr": round(vr, 2), "amt_mult": round(amt_mult, 2),
            "pct": round(pct, 2)}


def _scan_one_stock(code: str, name: str, lookback: int) -> list[dict]:
    """对单只票重放最近 lookback 个交易日，返回交集信号列表。

    优先读 bottom_surge 的日K缓存文件（已有 5000+ 只 × 250 根），零网络请求；
    缓存缺失时才走 daily_k 网络拉取。
    """
    # 与实盘一致：排除 ST / 退市（实盘两个扫描器都不碰这些票）
    nm = str(name or "").upper()
    if "ST" in nm or "退" in nm:
        return []
    k = None
    try:
        sym = bs._qt_symbol(str(code))
        cf = bs.KLINE_CACHE_DIR / f"{sym}.json"
        if cf.exists():
            raw = json.loads(cf.read_text(encoding="utf-8"))
            k = raw.get("rows") or []
    except Exception:
        k = None
    if not k or len(k) < 160:
        k = bs.daily_k(code, n=250)
    if not k or len(k) < 160:
        return []
    out = []
    start = max(120, len(k) - lookback - 1)
    for idx in range(start, len(k) - 1):     # 末根留作可能的次日，不判信号
        g = _genzhuang_at(k, idx)
        if not g:
            continue
        b = _bottom_at(k, idx)
        if not b:
            continue
        # T+1 必须存在
        nxt = k[idx + 1]
        buy, sell = float(nxt["open"]), float(nxt["close"])
        if buy <= 0:
            continue
        ret = (sell - buy) / buy - COST_RATE
        out.append({
            "code": code, "name": name,
            "signal_date": k[idx]["date"],
            "next_date": nxt["date"],
            "buy": round(buy, 2), "sell": round(sell, 2),
            "next_open": round(buy, 2), "next_close": round(sell, 2),
            "ret": round(ret * 100, 2),
            "next_pct": round((sell / float(k[idx]["close"]) - 1) * 100, 2),
            "aa": g["aa"], "cc": g["cc"],
            "bottom_score": b["score"], "bottom_vr": b["vr"],
            "signal_close": float(k[idx]["close"]),
        })
    return out


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------
def _stats(signals: list[dict]) -> dict:
    """从信号列表算成功率/盈亏比/收益分布。只统计有次日结果的。"""
    done = [s for s in signals if s.get("ret") is not None]
    if not done:
        return {"count": 0, "success_rate": None, "avg_ret": None,
                "profit_factor": None, "avg_win": None, "avg_loss": None,
                "max_ret": None, "min_ret": None}
    rets = [s["ret"] for s in done]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    pf = (avg_win / abs(avg_loss)) if avg_loss < 0 else (None if not losses else float("inf"))
    return {
        "count": len(done),
        "success_rate": round(len(wins) / len(done) * 100, 1),
        "avg_ret": round(sum(rets) / len(rets), 2),
        "profit_factor": round(pf, 2) if pf is not None and pf != float("inf") else (999.0 if pf else None),
        "avg_win": round(avg_win, 2), "avg_loss": round(avg_loss, 2),
        "max_ret": round(max(rets), 2), "min_ret": round(min(rets), 2),
        "wins": len(wins), "losses": len(losses),
    }


def backtest(lookback: int = DEFAULT_LOOKBACK, universe: list[dict] | None = None,
             verbose: bool = True, refresh: bool = False) -> dict:
    """历史回测。universe 为 [{code,name}]，缺省用缓存票池。结果落盘缓存（默认24h）。"""
    # 缓存命中（同一 lookback，24小时内直接回吐）
    if universe is None and not refresh and BACKTEST_FILE.exists():
        try:
            cached = json.loads(BACKTEST_FILE.read_text(encoding="utf-8"))
            if cached.get("lookback") == lookback and time.time() - cached.get("cached_ts", 0) < 86400:
                if verbose:
                    print(f"[resonance] 回测缓存命中 ({lookback}日, {cached.get('signal_count')}信号)")
                return cached
        except Exception as e:
            print(f"[resonance] 缓存读取失败: {type(e).__name__}: {e}")
    t0 = time.time()
    if universe is None:
        # 优先复用 bottom_surge 的日K缓存文件枚举票池（5000+只，零网络请求）。
        # 缓存缺失时退回 load_codes + 全A帧名字映射。
        cached = []
        try:
            for cf in bs.KLINE_CACHE_DIR.glob("*.json"):
                code = "".join(ch for ch in cf.stem if ch.isdigit())
                if len(code) == 6:
                    cached.append({"code": code, "name": code})
        except Exception as e:
            print(f"[resonance] 缓存枚举失败: {type(e).__name__}: {e}")
        if cached:
            # 用全A帧补名字（一次网络请求，失败则用代码兜底，不影响回测）
            try:
                nm = {}
                for rr in (bs.fetch_full_market(list(bs.load_codes())) or []):
                    if rr.get("code"):
                        nm[str(rr["code"])] = rr.get("name") or str(rr["code"])
                for u in cached:
                    u["name"] = nm.get(u["code"], u["code"])
            except Exception as e:
                print(f"[resonance] 名称映射降级: {type(e).__name__}: {e}")
            universe = cached
        else:
            codes = bs.load_codes()
            names = {}
            try:
                for r in (bs.fetch_full_market(codes) or []):
                    if r.get("code"):
                        names[str(r["code"])] = r.get("name") or str(r["code"])
            except Exception as e:
                print(f"[resonance] 名称映射降级: {type(e).__name__}: {e}")
            universe = [{"code": str(c), "name": names.get(str(c), str(c))} for c in codes]
    all_signals = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        results = list(ex.map(
            lambda u: _scan_one_stock(str(u["code"]), u.get("name", ""), lookback),
            universe))
    for rs in results:
        all_signals.extend(rs)
    all_signals.sort(key=lambda s: (s["signal_date"], s["code"]))
    st = _stats(all_signals)
    # 分段统计：按信号日期分组（便于看时间稳定性）
    by_date: dict[str, list] = {}
    for s in all_signals:
        by_date.setdefault(s["signal_date"], []).append(s)
    daily = [{"date": d, "n": len(v), **_stats(v)} for d, v in sorted(by_date.items())]
    if verbose:
        print(f"[resonance] 回测{lookback}日 universe={len(universe)} 信号={len(all_signals)} "
              f"胜率={st.get('success_rate')}% 盈亏比={st.get('profit_factor')} 耗时={time.time()-t0:.1f}s")
    result = {
        "ok": True,
        "asof": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "lookback": lookback, "universe": len(universe),
        "elapsed": round(time.time() - t0, 1),
        "stats": st, "signal_count": len(all_signals),
        "signals": all_signals[:SAMPLE_LIMIT * 3],   # 报告样例
        "daily": daily[-30:],                        # 最近30个有信号的交易日
        "cost_rate": COST_RATE,
        "cached_ts": time.time(),
        "method": ("信号日T收盘两形态共振；T+1开盘买/收盘卖；收益扣除双边0.2%成本；"
                   "成功率=盈利信号占比；盈亏比=平均盈利/abs(平均亏损)。纯历史日K重放，无未来函数。"),
        "caveat": ("CC 值在 REF(AA,3) 接近0时会因除以0.001而放大到极大值，属公式数学特性；"
                   "统计只看次日收益，不受 CC 数值大小影响。样本数少，结论仅供验证参考。"),
    }
    # 落盘缓存（仅全票池回测缓存，自定义 universe 不缓存）
    if universe is not None and len(universe) > 1000:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            BACKTEST_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"[resonance] 缓存写入失败: {type(e).__name__}: {e}")
    return result


def today_cross() -> dict:
    """今日交叉信号：两个实盘扫描器结果的交集。"""
    import urllib.request
    def fetch(url, timeout=40):
        try:
            return json.load(urllib.request.urlopen(url, timeout=timeout))
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    gz = fetch("http://127.0.0.1:8765/api/genzhuang_scan")
    bot = fetch("http://127.0.0.1:8765/api/bottom_surge")
    gz_codes = {h["code"] for h in (gz.get("hits") or [])}
    bot_all = {r["code"]: r for r in (bot.get("rows") or [])}
    bot_rows = {c: r for c, r in bot_all.items() if r.get("recommend")}
    bot_names = {c: r.get("name", "") for c, r in bot_all.items()}

    def _pack(h, b, strict):
        return {
            "code": h["code"], "name": h["name"] or bot_names.get(h["code"], ""),
            "price": h.get("price"), "pct": h.get("pct"),
            "aa": h.get("aa"), "cc": h.get("cc"),
            "gz_up_days": h.get("up_days"),
            "bottom_score": b.get("score"), "bottom_rank": b.get("rank_rec"),
            "bottom_factors": {k: b.get(k) for k in ("s_pos", "s_dd", "s_vol", "s_pct", "s_liq")},
            "tier": "严格共振" if strict else "形态共振(底部票未达推荐门槛)",
            "asof": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    # 严格共振：跟庄建仓命中 ∩ 底部放量【推荐】
    cross = []
    for h in (gz.get("hits") or []):
        if h["code"] in bot_rows:
            cross.append(_pack(h, bot_rows[h["code"]], True))
    cross.sort(key=lambda x: (-(x.get("bottom_score") or 0), -(x.get("cc") or 0)))
    # 近似共振：跟庄建仓命中 ∩ 底部放量【命中票】（即使未达每日推荐上限）
    near = []
    if not cross:                       # 严格共振为空时才给近似层，避免重复
        for h in (gz.get("hits") or []):
            if h["code"] in bot_all:
                near.append(_pack(h, bot_all[h["code"]], False))
        near.sort(key=lambda x: (-(x.get("bottom_score") or 0), -(x.get("cc") or 0)))
    # bottom_surge API 用 state=ok 而非 ok=true（两种都要兼容）
    bottom_ok = bool(bot.get("ok")) or bot.get("state") == "ok"
    return {
        "ok": True, "asof": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "gz_ok": bool(gz.get("ok")), "bottom_ok": bottom_ok,
        "gz_hits": len(gz.get("hits") or []),
        "bottom_rows": len(bot_all), "bottom_recs": len(bot_rows),
        "cross_count": len(cross), "cross": cross,
        "near_count": len(near), "near": near,
    }


# ---------------------------------------------------------------------------
# 台账
# ---------------------------------------------------------------------------
def append_ledger(signals: list[dict], source: str = "today_cross"):
    """信号落盘 JSONL（按 code+signal_date 去重），只写信号不碰账户。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    existing = set()
    if LEDGER_FILE.exists():
        for line in LEDGER_FILE.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                existing.add((r["code"], r["signal_date"]))
            except Exception:
                continue
    added = 0
    with LEDGER_FILE.open("a", encoding="utf-8") as f:
        for s in signals:
            key = (s["code"], s.get("signal_date") or datetime.now().strftime("%Y-%m-%d"))
            if key in existing:
                continue
            rec = {**s, "source": source, "recorded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   "next_ret": s.get("ret"), "verified": s.get("ret") is not None}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            existing.add(key)
            added += 1
    return added


def load_ledger() -> list[dict]:
    if not LEDGER_FILE.exists():
        return []
    out = []
    for line in LEDGER_FILE.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def verify_ledger() -> dict:
    """回填台账信号的次日实际走势（用历史日K），返回更新统计。"""
    rows = load_ledger()
    updated = 0
    for r in rows:
        if r.get("verified") or not r.get("signal_date"):
            continue
        k = bs.daily_k(r["code"], n=250)
        if not k:
            continue
        sd = r["signal_date"]
        idx = next((i for i, x in enumerate(k) if x["date"] == sd), None)
        if idx is None or idx + 1 >= len(k):
            continue
        nxt = k[idx + 1]
        buy, sell = float(nxt["open"]), float(nxt["close"])
        if buy <= 0:
            continue
        r["next_open"] = round(buy, 2); r["next_close"] = round(sell, 2)
        r["next_date"] = nxt["date"]
        r["next_ret"] = round(((sell - buy) / buy - COST_RATE) * 100, 2)
        r["verified"] = True
        updated += 1
    if updated:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        LEDGER_FILE.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    verified = [r for r in rows if r.get("verified") and r.get("next_ret") is not None]
    return {"total": len(rows), "updated": updated, "verified": len(verified),
            "stats": _stats([{"ret": r["next_ret"]} for r in verified])}


def selftest():
    print("=== resonance selftest ===")
    # 构造底部横盘微抬头 + 放量形态的模拟K线（160根）
    import random
    random.seed(11)
    k = []
    px = 20.0
    for i in range(160):
        if i < 100:
            px -= 0.08
        elif i < 130:
            px = 8 + (i % 3) * 0.03
        elif i < 158:
            px = 8.1 + (i - 130) * 0.01
        else:
            px = 8.4 + (i - 158) * 0.15
        c = px
        o = c - 0.05
        h = c + 0.2
        l = c - 0.2
        v = 90000 if i < 155 else 260000
        k.append({"date": f"d{i:03d}", "open": o, "close": c, "high": h, "low": l,
                  "vol_shou": v})
    g = _genzhuang_at(k, 159)
    b = _bottom_at(k, 159)
    print("genzhuang:", bool(g), "bottom:", bool(b))
    # 统计逻辑单测
    st = _stats([{"ret": 3.0}, {"ret": -1.5}, {"ret": 2.0}, {"ret": -1.0}])
    assert st["count"] == 4 and st["success_rate"] == 50.0, st
    assert abs(st["avg_win"] - 2.5) < 0.01 and abs(st["avg_loss"] + 1.25) < 0.01, st
    assert abs(st["profit_factor"] - 2.0) < 0.01, st
    print("[1] 统计口径 OK:", st)
    print("SELFTEST ALL PASS")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        selftest()
    elif "--backtest" in sys.argv:
        n = int(sys.argv[sys.argv.index("--backtest") + 1]) if len(sys.argv) > sys.argv.index("--backtest") + 1 else DEFAULT_LOOKBACK
        print(json.dumps(backtest(n), ensure_ascii=False, indent=2))
    elif "--today" in sys.argv:
        print(json.dumps(today_cross(), ensure_ascii=False, indent=2))
    elif "--verify" in sys.argv:
        print(json.dumps(verify_ledger(), ensure_ascii=False, indent=2))
    else:
        print(json.dumps({"today": today_cross(), "backtest": backtest(60)}, ensure_ascii=False, indent=2))

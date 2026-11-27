# -*- coding: utf-8 -*-
"""首板蓄势形态评分引擎（ADR-0053）

用户诉求（2026-11-23）：
  分析每天的首板，挑出「涨停前 K 线上涨趋势 + 持续几天资金流入 + 稳步放量」的那一只。

方法论（关键：不编造数据）
--------------------------------------------------
东财 push2his / 腾讯 ffundFlow 本机均不可用；同花顺 stock_fund_flow_individual
返回数据错乱（301000 查到唐山港），不可信。因此本模块**不用任何第三方"资金流"数字**，
只用腾讯日 K 的真实 OHLCV 推导标准量价指标来刻画「资金持续流入」：

  · CMF（Chaikin Money Flow, 20日）：Σ[((C-L)-(H-C))/(H-L) × V] / ΣV
      收盘靠近当日高位且放量 → 正值 = 主力吸筹；这是国际通用的资金流量指标，
      不是估算的资金流金额。
  · OBV（能量潮）斜率：涨日加量、跌日减量，近 N 日上行 = 买盘持续占优。
  · 红肥绿瘦：上涨日均量 / 下跌日均量 > 1 = 资金在涨时进场、跌时缩手。
  · 稳步放量：成交量线性回归斜率为正 + 量比温和（vol5/vol20 落在 1.0~2.5）
      + 拒绝单日暴量（max5/median20 不过大）——暴量往往是出货不是蓄势。

分析窗口 = **涨停之前**的 K 线（剔除今日涨停这根），严格对应用户说的「涨停前形态」。

评分（三维度各 0~100，加权总分）
  trend 趋势 35%  /  flow 资金 35%  /  volume 量能 30%
  ≥72 强候选 · 60~72 观察 · <60 淘汰

铁律：本模块只读、纯函数，不写任何文件、不进派单/弹窗链路。
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Iterable

W_TREND, W_FLOW, W_VOL = 0.35, 0.35, 0.30
W_SETUP, W_SEAL = 0.65, 0.35   # 综合分：涨停前形态 65% + 今日封板质量 35%
MIN_BARS = 16          # 至少需要 16 根涨停前 K 线
STRONG, WATCH = 72.0, 60.0


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _slope(xs):
    """最小二乘斜率（每根的变化量），xs 等长索引隐含 0..n-1。"""
    n = len(xs)
    if n < 2:
        return 0.0
    mx = (n - 1) / 2.0
    my = _mean(xs)
    num = sum((i - mx) * (v - my) for i, v in enumerate(xs))
    den = sum((i - mx) ** 2 for i in range(n))
    return num / den if den else 0.0


def pre_breakout_bars(klines: list[dict], today: str | None = None) -> list[dict]:
    """剔除今日（涨停日）K 线，只保留涨停前的形态。"""
    today = today or datetime.now().strftime("%Y-%m-%d")
    pre = [k for k in (klines or []) if str(k.get("date", ""))[:10] != today]
    return pre


def _trend_score(c, h, l):
    """上涨趋势分：均线多头 + 重心上移 + 回撤浅 + 逼近前高。"""
    s, why = 0.0, []
    ma5, ma10, ma20 = _mean(c[-5:]), _mean(c[-10:]), _mean(c[-20:])
    if ma5 > ma10 > ma20:
        s += 30; why.append("均线多头排列(MA5>MA10>MA20)")
    elif ma5 > ma10:
        s += 15; why.append("短期均线转强(MA5>MA10)")
    else:
        why.append("均线未走多")
    last = c[-1]
    if last > ma20:
        s += 18; why.append("收盘站上MA20")
    if last > c[-6]:
        gain = (last / c[-6] - 1) * 100
        s += 17 if 2 <= gain <= 18 else (10 if gain > 0 else 0)
        why.append(f"近5日重心上移{gain:+.1f}%")
    # 近 10 日最大回撤（浅 = 洗盘充分）
    seg = c[-10:]
    peak, mdd = seg[0], 0.0
    for x in seg:
        peak = max(peak, x)
        mdd = min(mdd, x / peak - 1)
    if mdd > -0.09:
        s += 17; why.append(f"回撤浅{mdd*100:.1f}%")
    elif mdd > -0.15:
        s += 9; why.append(f"回撤中等{mdd*100:.1f}%")
    # 距 20 日前高
    if last >= max(h[-20:]) * 0.97:
        s += 18; why.append("逼近/突破20日前高")
    elif last >= max(h[-20:]) * 0.90:
        s += 9; why.append("接近20日前高")
    return min(s, 100.0), why


def _cmf(c, h, l, v, n=20):
    num = den = 0.0
    for i in range(-n, 0):
        rng = h[i] - l[i]
        mfm = ((c[i] - l[i]) - (h[i] - c[i])) / rng if rng > 0 else 0.0
        num += mfm * v[i]
        den += v[i]
    return num / den if den else 0.0


def _flow_score(c, h, l, v):
    """资金流入分：CMF + OBV 斜率 + 红肥绿瘦。全部来自真实量价。"""
    s, why = 0.0, []
    cmf = _cmf(c, h, l, v, 20)
    if cmf > 0.20:
        s += 40; why.append(f"CMF20={cmf:+.2f} 强吸筹")
    elif cmf > 0.08:
        s += 28; why.append(f"CMF20={cmf:+.2f} 温和流入")
    elif cmf > 0:
        s += 14; why.append(f"CMF20={cmf:+.2f} 弱流入")
    else:
        why.append(f"CMF20={cmf:+.2f} 净流出")
    # OBV 斜率（归一化：相对均量）
    obv, base = [], v[0]
    for i in range(1, len(c)):
        base += v[i] if c[i] > c[i - 1] else (-v[i] if c[i] < c[i - 1] else 0)
        obv.append(base)
    avg_v = _mean(v) or 1
    obv_s = _slope(obv[-12:]) / avg_v
    if obv_s > 0.30:
        s += 30; why.append(f"OBV持续上行({obv_s:+.2f})")
    elif obv_s > 0.08:
        s += 20; why.append(f"OBV缓升({obv_s:+.2f})")
    elif obv_s > 0:
        s += 10; why.append(f"OBV微升({obv_s:+.2f})")
    else:
        why.append(f"OBV走弱({obv_s:+.2f})")
    # 红肥绿瘦
    up_v = [v[i] for i in range(1, len(c)) if c[i] > c[i - 1]]
    dn_v = [v[i] for i in range(1, len(c)) if c[i] < c[i - 1]]
    if up_v and dn_v:
        ratio = _mean(up_v) / (_mean(dn_v) or 1)
        if ratio > 1.5:
            s += 30; why.append(f"红肥绿瘦{ratio:.2f}×")
        elif ratio > 1.1:
            s += 20; why.append(f"涨放量跌缩量{ratio:.2f}×")
        elif ratio > 1.0:
            s += 10; why.append(f"量价基本均衡{ratio:.2f}×")
        else:
            why.append(f"绿肥红瘦{ratio:.2f}×")
    return min(s, 100.0), why, cmf, obv_s


def _volume_score(v, c):
    """稳步放量分：量能斜率为正 + 温和放大 + 拒绝单日暴量。"""
    s, why = 0.0, []
    v20 = v[-20:]
    vs = _slope(v20)
    avg = _mean(v20) or 1
    if vs > 0.12 * avg:
        s += 35; why.append("成交量持续放大")
    elif vs > 0.03 * avg:
        s += 24; why.append("成交量温和抬升")
    elif vs > 0:
        s += 12; why.append("成交量微增")
    else:
        why.append("成交量未放大")
    r = _mean(v[-5:]) / (_mean(v[-20:]) or 1)
    if 1.15 <= r <= 2.5:
        s += 35; why.append(f"量比温和{r:.2f}×")
    elif 1.0 <= r < 1.15:
        s += 22; why.append(f"量比平缓{r:.2f}×")
    elif 2.5 < r <= 3.5:
        s += 12; why.append(f"量比偏大{r:.2f}×")
    else:
        why.append(f"量比异常{r:.2f}×")
    med = sorted(v20)[len(v20) // 2] or 1
    burst = max(v[-5:]) / med
    if burst <= 2.5:
        s += 30; why.append(f"无单日暴量({burst:.1f}×)")
    elif burst <= 4:
        s += 16; why.append(f"略有放量峰({burst:.1f}×)")
    else:
        why.append(f"单日暴量({burst:.1f}×)疑似出货")
    return min(s, 100.0), why


def _seal_score(meta: dict) -> tuple[float, list[str]]:
    """今日封板质量分（0~100）：首封时间 + 封单厚度 + 炸板次数 + 换手健康度。

    用户要「做哪一只首板」，形态只是必要条件；封板质量决定今天能不能上车、
    明天有没有溢价。纯用东财涨停池已有字段，零估算。
    """
    s, why = 0.0, []
    ft = str(meta.get("first_time") or "").strip()
    # 首封时间：09:30 前秒板 30；10:00 前 24；10:30 前 16；之后 8
    if ft and len(ft) >= 4:
        try:
            hhmm = int(ft[:4])
            if hhmm <= 930:
                s += 30; why.append(f"秒板({ft[:2]}:{ft[2:4]})")
            elif hhmm <= 1000:
                s += 24; why.append(f"早盘板({ft[:2]}:{ft[2:4]})")
            elif hhmm <= 1030:
                s += 16; why.append(f"中盘板({ft[:2]}:{ft[2:4]})")
            else:
                s += 8; why.append(f"偏晚板({ft[:2]}:{ft[2:4]})")
        except ValueError:
            s += 8; why.append("首封时间异常")
    else:
        s += 8; why.append("首封时间缺失")
    # 封单厚度：封单/市值 千分比
    fund = meta.get("fund_yi") or 0
    mv = meta.get("mv_yi") or 0
    ratio = (fund / mv * 1000) if mv else 0     # ‰
    if ratio >= 40:
        s += 30; why.append(f"封单厚{ratio:.0f}‰")
    elif ratio >= 20:
        s += 24; why.append(f"封单较厚{ratio:.0f}‰")
    elif ratio >= 10:
        s += 16; why.append(f"封单中等{ratio:.0f}‰")
    else:
        s += 8; why.append(f"封单偏薄{ratio:.0f}‰")
    # 炸板次数：0 次满分
    bc = int(meta.get("break_count") or 0)
    if bc == 0:
        s += 25; why.append("零炸板")
    elif bc == 1:
        s += 15; why.append("炸板1次")
    elif bc == 2:
        s += 7; why.append("炸板2次分歧大")
    else:
        why.append(f"炸板{bc}次严重")
    # 换手健康度：首板 3~12% 最佳，过高=分歧，过低=无量一字难上车
    to = meta.get("turnover") or 0
    if 3 <= to <= 12:
        s += 15; why.append(f"换手健康{to:.1f}%")
    elif 1.5 <= to < 3 or 12 < to <= 18:
        s += 9; why.append(f"换手尚可{to:.1f}%")
    else:
        s += 3; why.append(f"换手异常{to:.1f}%")
    return min(s, 100.0), why


def analyze_one(code: str, klines: list[dict], today: str | None = None,
                meta: dict | None = None) -> dict:
    """对单只首板打分。klines 为腾讯日K（含今日），内部自动剔除今日。"""
    pre = pre_breakout_bars(klines, today)
    base = {"code": str(code), "name": (meta or {}).get("name", code),
            "industry": (meta or {}).get("industry", ""),
            "bars": len(pre), "verdict": "数据不足", "score": 0.0}
    if len(pre) < MIN_BARS:
        base["reason"] = f"涨停前K线仅{len(pre)}根，需≥{MIN_BARS}根"
        return base
    c = [float(k["close"]) for k in pre]
    h = [float(k["high"]) for k in pre]
    l = [float(k["low"]) for k in pre]
    v = [float(k.get("vol_shou", 0)) for k in pre]
    ts, tw = _trend_score(c, h, l)
    fs, fw, cmf, obv_s = _flow_score(c, h, l, v)
    vs, vw = _volume_score(v, c)
    setup = W_TREND * ts + W_FLOW * fs + W_VOL * vs
    seal, sw = _seal_score(meta or {})
    total = W_SETUP * setup + W_SEAL * seal
    verdict = "强候选" if total >= STRONG else ("观察" if total >= WATCH else "淘汰")
    out = {
        **base,
        "score": round(total, 1), "verdict": verdict,
        "setup": round(setup, 1), "seal": round(seal, 1),
        "trend": round(ts, 1), "flow": round(fs, 1), "volume": round(vs, 1),
        "cmf": round(cmf, 3), "obv_slope": round(obv_s, 3),
        "trend_why": tw, "flow_why": fw, "volume_why": vw, "seal_why": sw,
        "pre_last_date": pre[-1]["date"], "pre_close": c[-1],
        "ma5": round(_mean(c[-5:]), 2), "ma20": round(_mean(c[-20:]), 2),
        "reason": "｜".join((tw[:2] + fw[:2] + vw[:2] + sw[:2])),
        # 今日涨停信息（仅展示，不参与形态分）
        "zt": {k: meta.get(k) for k in
               ("pct", "first_time", "fund_yi", "amount_yi", "turnover",
                "mv_yi", "break_count", "stat") if meta and k in meta},
    }
    return out


def rank(firstboards: Iterable[dict], kline_fn, today: str | None = None) -> list[dict]:
    """批量评分并降序返回。kline_fn(code) -> list[dict]（腾讯日K）。"""
    out = []
    for fb in firstboards or []:
        code = str(fb.get("code", ""))
        if not code:
            continue
        try:
            k = kline_fn(code)
            out.append(analyze_one(code, k, today, fb))
        except Exception as e:
            out.append({"code": code, "name": fb.get("name", code), "score": 0.0,
                        "verdict": "取数失败", "reason": f"{type(e).__name__}: {e}",
                        "bars": 0, "trend": 0, "flow": 0, "volume": 0})
    return sorted(out, key=lambda x: x.get("score", 0), reverse=True)


def selftest():
    print("=== firstboard_setup selftest ===")
    def mk(n, start=10.0, drift=0.02, vol_base=1000, vol_drift=0.0,
           red_fat=False):
        rows, c = [], start
        for i in range(n):
            o = c
            c = c * (1 + drift)
            hi, lo = c * 1.012, o * 0.988
            v = vol_base * (1 + vol_drift * i)
            if red_fat:
                v = v * (1.3 if i % 2 == 0 else 0.7)
            rows.append({"date": f"2026-11-{i+1:02d}", "open": o, "close": c,
                         "high": hi, "low": lo, "vol_shou": v})
        return rows
    good_seal = {"first_time": "093000", "fund_yi": 2.0, "mv_yi": 50,
                 "break_count": 0, "turnover": 6.0}
    # A. 标准蓄势形态：稳步上涨 + 温和放量 + 红肥绿瘦 + 强封板
    a = analyze_one("A", mk(25, drift=0.012, vol_drift=0.06, red_fat=True),
                    "2026-11-99", good_seal)
    # B. 横盘缩量
    b = analyze_one("B", mk(25, drift=0.0, vol_drift=-0.02), "2026-11-99", good_seal)
    # C. 下跌出货
    cc = analyze_one("C", mk(25, drift=-0.01, vol_drift=0.10), "2026-11-99", good_seal)
    print("A蓄势", a["score"], a["verdict"], "Setup/Seal=", a["setup"], a["seal"])
    print("B横盘", b["score"], b["verdict"], "Setup/Seal=", b["setup"], b["seal"])
    print("C下跌", cc["score"], cc["verdict"], "Setup/Seal=", cc["setup"], cc["seal"])
    assert a["score"] > b["score"] and a["score"] > cc["score"], "蓄势形态必须最高分"
    assert a["setup"] > b["setup"] and a["setup"] > cc["setup"], "形态分单调性失败"
    assert a["verdict"] == "强候选", a
    assert b["verdict"] == "淘汰" and cc["verdict"] == "淘汰", "横盘/下跌不应入选"
    # 封板质量单调性：差封板必须拉低综合分
    bad_seal = {"first_time": "110000", "fund_yi": 0.1, "mv_yi": 50,
                "break_count": 3, "turnover": 25.0}
    a_bad = analyze_one("A2", mk(25, drift=0.012, vol_drift=0.06, red_fat=True),
                        "2026-11-99", bad_seal)
    assert a_bad["seal"] < a["seal"] and a_bad["score"] < a["score"], "封板质量分应拉低综合分"
    print("封板质量单调性 OK:", a["seal"], ">", a_bad["seal"])
    assert analyze_one("D", mk(5), "2026-11-99")["verdict"] == "数据不足"
    # 剔除今日
    k = mk(20, drift=0.01); k[-1]["date"] = datetime.now().strftime("%Y-%m-%d")
    assert len(pre_breakout_bars(k)) == 19
    print("SELFTEST ALL PASS")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        selftest()

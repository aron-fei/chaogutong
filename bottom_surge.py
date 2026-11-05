#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
底部放量雷达 (bottom_surge.py) —— A股「底部放量」实时扫描

设计动机（ADR-0033）
-------------------
既有选股通道（09:25 打板 / 13:40 午后强势 / 14:40 尾盘龙首阴）的候选源全部来自
**涨停池 / 强势股池 / 连板主线**，本质是「已经涨起来」的票：
  · 09:25 派 BUY_LIMIT 挂涨停排单 —— 封单大的票根本买不进（华瓷股份 5 连板一字板，封单 2.79 亿）；
  · 13:40 派「午后强势未涨停票」现价买入 —— 仍是从高位池里挑，属于追高。
用户诉求：改抓「底部放量启动」这一形态（样板：2026-09-22 江淮汽车 600418，
昨收 19.38 开盘后放量拉升至 21.32 = +10.01%，量比 3.87，换手 6.04%，成交额 27.7 亿），
要求「最起码不要追太高」，且买入标的必须**当下可成交**（非封死涨停）。

本模块只做「扫描 + 打分 + 排序」，返回候选清单；不修改任何回测库、不下单。
下游接法：
  · 工作台：直接 import 本模块 (scan()) 或读 data/bottom_surge_latest.json；
  · market-advisor：`python3 bottom_surge.py --json --top 20` 取 stdout JSON，
    转成 contribution(source="底部放量") 并入统一操作池（见 ADR-0033 接入方案）。

数据源（2026-09-22 实测）
------------------------
  · 全市场实时快照：腾讯批量 qt.gtimg.cn（batch=200，5212 只 ≈ 27 批；字段位见 fetch_full_market）
  · 日K（120 根）：新浪 getKLineData 优先 → 腾讯 web.ifzq.gtimg.cn/appstock/app/kline/kline 兜底
    ⚠️ 腾讯 fqkline（前复权）端点在本机出口 IP 被 WAF 拦截（HTTP 501），**不可用**；
       新浪与 ifzq 旧端点返回的 120 根日K数值一致（未复权），实测无差异 → 用未复权即可判定底部位置。
  · 分时：腾讯 web.ifzq.gtimg.cn/appstock/app/minute/query（--replay 用，做信号时点回放）

用法
----
  # 实时扫描（默认 Top 15，人读格式）
  /opt/homebrew/anaconda3/bin/python3 bottom_surge.py

  # 机器读：JSON 到 stdout，同时落盘 data/bottom_surge_latest.json
  /opt/homebrew/anaconda3/bin/python3 bottom_surge.py --json --top 20 --save

  # 阈值实测：打印粗筛池各维度分布，用于阈值校准（先实测真实分布再定阈值）
  /opt/homebrew/anaconda3/bin/python3 bottom_surge.py --explore

  # 单票信号回放：逐时间点展示「若此刻触发，信号长什么样」
  /opt/homebrew/anaconda3/bin/python3 bottom_surge.py --replay 600418
"""

import argparse
import log_util
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
CODES_CACHE = DATA_DIR / "a_codes.json"
LATEST_FILE = DATA_DIR / "bottom_surge_latest.json"
KLINE_CACHE_DIR = DATA_DIR / "bottom_kline_cache"

UA = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")
}

# ---------------------------------------------------------------------------
# 阈值集中配置（改这里即可调整全盘行为；--explore 用于实测校准）
# ---------------------------------------------------------------------------
# —— 粗筛层：实时快照即可判定，用来把 5200 只压到几十只 ——
PCT_LO, PCT_HI = 1.0, 7.5      # 当日涨幅区间（下限排除阴跌，上限排除追高/涨停）
VR_MIN = 1.5                   # 量比下限（腾讯 qt 量比已按时间进度折算，可直接比）
TURN_MIN = 1.5                 # 换手率下限 %
AMT_MIN = 1.5                  # 当日成交额下限（亿）
MV_LO, MV_HI = 15.0, 800.0     # 流通市值区间（亿）：排除微盘操纵盘与大盘迟钝盘
PRICE_MIN = 2.0                # 股价下限（元）
K_FETCH_MAX = 150              # 粗筛后进入日K判定的上限（控制请求量；实测 150 只日K约 2s）

# —— 底部层：需要 120 根日K ——
K_N = 120                      # 日K根数
POS_MAX = 0.40                 # 现价在 120 日区间中的位置上限（0=区间最低，1=区间最高）
DD_MIN = 0.25                  # 距 120 日最高价的最小回撤（25%）
MA60_MAX = 1.10                # 现价 / MA60 上限（不允许已明显偏离中期均线）

# —— 放量层 ——
VR_STRONG = 2.0                # 量比强放量门槛
AMT_MULT_MIN = 1.6             # 预估全天成交额 / 近 20 日均成交额 门槛
MIN_AVG_VOL_SHOU = 30000       # 近 5 日均量下限（手）：量比高但绝对量太小的不算

# —— 打分权重（合计 100）——
W_POS, W_DD, W_VOL, W_PCT, W_LIQ = 35.0, 15.0, 30.0, 10.0, 10.0
PCT_BEST, PCT_SIGMA = 4.0, 2.5   # 「温和上涨」最优点与宽度（%）
VR_TOP = 4.0                     # 量比归一化上限（>= 此值量能分满分）
AMT_FULL = 8.0                   # 成交额（亿）归一化满分

# —— 输出与推荐口径（2026-09-22 用户决策：推荐越少越好，每天 1~2 只）——
# 阈值来源：当日 43 只命中的分数分布实测 —— ≥75 分 2 只 / ≥70 分 6 只 / ≥65 分 7 只。
# 采用「绝对分下限 + 只数硬上限」双门而非单点阈值：弱市可 0 只，强市也不超上限。
MIN_SCORE_OUT = 55.0         # 输出下限：砍掉尾巴噪声票（43 → 约 15），雷达里仍全量可见
RECOMMEND_MIN_SCORE = 70.0   # 推荐门槛：低于此分不进策略引擎、UI 不高亮
RECOMMEND_MAX = 2            # 每日最多推荐只数（用户要求「一到两只，太多不好操作」）
TOP_FOR_UI = 20              # 工作台 tab 展示行数上限


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _session():
    s = requests.Session()
    s.trust_env = False          # 绕过本机代理（沙箱代理会拦行情站点，等价 curl --noproxy）
    s.headers.update(UA)
    return s


_S = _session()


def _num(parts, idx):
    try:
        v = parts[idx]
    except IndexError:
        return None
    if v in ("", "-", None):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _vol_shou(code, raw):
    """腾讯 qt [6]/[36] 成交量 →「手」。

    ⚠️ 实测量纲差异（2026-09-22 校验）：主板/创业板返回**手**，科创板返回**股**，相差 100 倍。
       验证法：`成交额(万元)×1e4 / 均价 / 100` 应与该字段一致。
       例：688301 奕瑞科技 qt=10,049,800 vs 推算 100,685 手 → /100 吻合；
           600418 江淮汽车 qt=1,367,700 vs 推算 1,303,959 手 → 原值即为手。
       若不修正，科创板量能倍数会被放大 100 倍，`amt_mult20` 门槛对科创板形同失效。
    """
    if raw is None:
        return None
    if str(code).startswith(("688", "689")):
        return raw / 100.0
    return raw


def _qt_symbol(code):
    """6 位数字 → 腾讯行情符号。
    2026-09-29 修复：原实现要求 `code.isdigit()`，而回测库 active_positions 里
    confirm 流程写入的是 "300450.SZ" 形式 → 直接返回空 → 持仓盯盘取不到行情
    （实测显示「无法获取实时行情，本轮不评估」）。现统一剥离非数字字符，
    兼容 000066 / 000066.SZ / sz000066 / SZ000066 四种写法。纯数字入参行为不变。
    """
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if not code:
        return ""
    if code.startswith(("6", "9", "5")):
        return "sh" + code
    if code.startswith(("0", "3", "2")):
        return "sz" + code
    return ""            # 北交所(4/8) 排除：流动性差，不纳入底部放量池


def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def load_codes():
    """全A代码表：优先读 data/a_codes.json（工作台缓存），无则要求先跑一次工作台。"""
    if CODES_CACHE.exists():
        try:
            d = json.loads(CODES_CACHE.read_text(encoding="utf-8"))
            codes = d.get("codes") or []
            if len(codes) > 3000:
                return [c for c in codes if _qt_symbol(c)]
        except Exception as e:
            print(f"[WARN] a_codes.json 解析失败: {e}", file=sys.stderr)
    raise RuntimeError(
        f"缺少全A代码缓存 {CODES_CACHE}，请先启动一次工作台(app.py)生成，或手动写入 {{\"codes\": [...]}}")


def trading_progress(now=None):
    """交易时间进度 0~1（上午 09:30-11:30 = 120min，下午 13:00-15:00 = 120min）。"""
    now = now or datetime.now()
    m = now.hour * 60 + now.minute + now.second / 60.0
    o_am, c_am = 9 * 60 + 30, 11 * 60 + 30
    o_pm, c_pm = 13 * 60, 15 * 60
    if m <= o_am:
        return 0.0
    if m < c_am:
        return (m - o_am) / 240.0
    if m <= o_pm:
        return 120.0 / 240.0
    if m < c_pm:
        return (120.0 + (m - o_pm)) / 240.0
    return 1.0


# ---------------------------------------------------------------------------
# 数据拉取
# ---------------------------------------------------------------------------
def fetch_full_market(codes, batch=200, progress_hook=None):
    """腾讯批量行情：返回全A快照 list[dict]。字段位与工作台 data_fetcher 保持一致。"""
    rows, total = [], len(codes)
    for i in range(0, total, batch):
        syms = [_qt_symbol(c) for c in codes[i:i + batch]]
        syms = [s for s in syms if s]
        if not syms:
            continue
        try:
            r = _S.get("https://qt.gtimg.cn/q=" + ",".join(syms), timeout=8)
            r.encoding = "gbk"
        except Exception as e:
            print(f"[WARN] 批量行情第 {i // batch + 1} 批失败({type(e).__name__}: {e})，跳过", file=sys.stderr)
            continue
        for line in r.text.split(";"):
            line = line.strip()
            if not line.startswith("v_"):
                continue
            seg = line.split('="', 1)
            if len(seg) < 2:
                continue
            p = seg[1].rstrip('"').split("~")
            if len(p) < 52:
                continue
            price = _num(p, 3)
            prev_close = _num(p, 4)
            if not price or not prev_close:
                continue
            rows.append({
                "code": p[2], "name": p[1],
                "price": price, "prev_close": prev_close,
                "open": _num(p, 5), "pct": _num(p, 32),
                "high": _num(p, 33), "low": _num(p, 34),
                "vol_shou": _vol_shou(p[2], _num(p, 6)),   # 成交量(手) —— 科创板量纲修正见 _vol_shou
                "amount_wan": _num(p, 37),       # 成交额(万元)
                "turnover": _num(p, 38),         # 换手率 %
                "mv_yi": _num(p, 44),            # 流通市值(亿)
                "zt_price": _num(p, 47),         # 涨停价
                "vol_ratio": _num(p, 49),        # 量比
                "avg_price": _num(p, 51),        # 均价
                "outer": _num(p, 7), "inner": _num(p, 8),
            })
        if progress_hook:
            progress_hook(min(i + batch, total), total)
    if len(rows) < 3000:
        raise RuntimeError(f"腾讯批量返回异常：{len(rows)} 行（<3000），疑似被限流，建议稍后重试")
    return rows


def _kline_sina(sym, n=K_N):
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen={n}")
    r = _S.get(url, timeout=8)
    if r.status_code != 200 or not r.text.strip().startswith("["):
        raise RuntimeError(f"sina kline http={r.status_code} body={r.text[:60]!r}")
    arr = json.loads(r.text)
    out = []
    for d in arr:
        out.append({
            "date": d["day"], "open": float(d["open"]), "close": float(d["close"]),
            "high": float(d["high"]), "low": float(d["low"]),
            "vol_shou": float(d["volume"]) / 100.0,      # sina 返回股 → 手
        })
    return out


def _kline_ifzq(sym, n=K_N):
    url = f"https://web.ifzq.gtimg.cn/appstock/app/kline/kline?param={sym},day,,,{n}"
    r = _S.get(url, timeout=8)
    d = json.loads(r.text)
    node = d["data"][sym]
    rows = node.get("day") or node.get("qfqday") or []
    out = []
    for it in rows:
        # [date, open, close, high, low, vol(手)]
        out.append({"date": it[0], "open": float(it[1]), "close": float(it[2]),
                    "high": float(it[3]), "low": float(it[4]), "vol_shou": float(it[5])})
    if not out:
        raise RuntimeError("ifzq kline 空")
    return out


def daily_k(code, n=K_N, use_cache=True):
    """日K：新浪优先 → ifzq 兜底。返回 list[dict]（按日期升序，含今日）。"""
    sym = _qt_symbol(code)
    if not sym:
        return []
    cache_f = KLINE_CACHE_DIR / f"{sym}.json"
    if use_cache and cache_f.exists():
        try:
            c = json.loads(cache_f.read_text(encoding="utf-8"))
            if time.time() - c.get("ts", 0) < 300:       # 5 分钟缓存
                return c["rows"]

        except Exception as _e:
            log_util.quiet_warn("bottom_surge.py:296", _e)

    rows = None
    last_err = None
    for fn in (_kline_sina, _kline_ifzq):
        try:
            rows = fn(sym, n)
            if rows:
                break
        except Exception as e:
            last_err = f"{fn.__name__}: {type(e).__name__}: {e}"
    if not rows:
        print(f"[WARN] {code} 日K获取失败（{last_err}）", file=sys.stderr)
        return []
    try:
        KLINE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_f.write_text(json.dumps({"ts": time.time(), "rows": rows}), encoding="utf-8")

    except Exception as _e:
        log_util.quiet_warn("bottom_surge.py:313", _e)

    return rows


def fetch_minute(code):
    """当日分时：返回 [(HHMM, price, vol_shou_cum, amount_cum)]。"""
    sym = _qt_symbol(code)
    url = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={sym}"
    r = _S.get(url, timeout=8)
    d = json.loads(r.text)
    arr = d["data"][sym]["data"]["data"]
    out = []
    for s in arr:
        f = s.split()
        if len(f) >= 3:
            out.append((f[0], float(f[1]), float(f[2]), float(f[3]) if len(f) > 3 else 0.0))
    return out


# ---------------------------------------------------------------------------
# 判定与打分
# ---------------------------------------------------------------------------
def _kstats(k, today_price, today_vol_shou, progress):
    """基于 120 根日K（最后一根即今日盘中）计算底部/量能特征。"""
    if len(k) < 60:
        return None
    # 历史基准剔除最后一根（今日未收盘）与最新一根之后的判断
    hist = k[:-1] if k and k[-1]["date"] >= datetime.now().strftime("%Y-%m-%d") else k
    if len(hist) < 60:
        hist = k
    highs = [r["high"] for r in hist]
    lows = [r["low"] for r in hist]
    h120, l120 = max(highs), min(lows)
    closes = [r["close"] for r in hist]
    ma20 = sum(closes[-20:]) / 20.0
    ma60 = sum(closes[-60:]) / 60.0
    avg_vol20 = sum(r["vol_shou"] for r in hist[-20:]) / 20.0
    avg_vol5 = sum(r["vol_shou"] for r in hist[-5:]) / 5.0
    pos = (today_price - l120) / (h120 - l120) if h120 > l120 else 1.0
    dd = (h120 - today_price) / h120 if h120 > 0 else 0.0
    vol_mult = (today_vol_shou / progress) / avg_vol5 if (progress > 0.05 and avg_vol5 > 0) else None
    return {
        "h120": round(h120, 2), "l120": round(l120, 2),
        "pos120": round(pos, 3), "dd120": round(dd, 3),
        "ma20": round(ma20, 2), "ma60": round(ma60, 2),
        "price_over_ma60": round(today_price / ma60, 3) if ma60 else None,
        "avg_vol5_shou": round(avg_vol5), "avg_vol20_shou": round(avg_vol20),
        "vol_mult5": round(vol_mult, 2) if vol_mult else None,
        "k_days": len(hist),
    }


def _score(row):
    """底部放量综合分 0~100（权重见模块常量，可解释、可调）。"""
    pos = row["pos120"]
    dd = row["dd120"]
    vr = row.get("vol_ratio") or 0.0
    pct = row.get("pct") or 0.0
    amt = row.get("amount_proj_yi") or 0.0
    s_pos = W_POS * _clamp((POS_MAX - pos) / POS_MAX)
    s_dd = W_DD * _clamp((dd - DD_MIN) / (0.60 - DD_MIN))
    s_vol = W_VOL * _clamp((vr - VR_STRONG) / (VR_TOP - VR_STRONG))
    s_pct = W_PCT * pow(2.718281828, -0.5 * ((pct - PCT_BEST) / PCT_SIGMA) ** 2)
    s_liq = W_LIQ * _clamp(amt / AMT_FULL)
    return {
        "score": round(s_pos + s_dd + s_vol + s_pct + s_liq, 1),
        "s_pos": round(s_pos, 1), "s_dd": round(s_dd, 1), "s_vol": round(s_vol, 1),
        "s_pct": round(s_pct, 1), "s_liq": round(s_liq, 1),
    }


def _coarse(rows, cfg):
    """粗筛：只用实时快照，不拉日K。返回 (通过列表, 统计 dict)。"""
    out, stat = [], {"total": len(rows), "pct0": 0, "vr0": 0, "turn0": 0,
                     "mv0": 0, "amt0": 0, "bad": 0, "zt": 0}
    for r in rows:
        name = r.get("name") or ""
        code = r.get("code") or ""
        price = r.get("price") or 0
        pct = r.get("pct")
        vr = r.get("vol_ratio")
        turn = r.get("turnover")
        mv = r.get("mv_yi")
        amt = (r.get("amount_wan") or 0) / 1e4
        zt = r.get("zt_price")
        if price <= 0 or pct is None:
            stat["bad"] += 1
            continue
        # 排除 ST / 退市 / 停牌（成交额为 0）
        if "ST" in name.upper() or "退" in name:
            stat["bad"] += 1
            continue
        if amt <= 0:
            stat["bad"] += 1
            continue
        # 排除已封涨停：现价触及涨停价（-1 分钱容差）→ 买不进
        if zt and price >= zt - 0.011:
            stat["zt"] += 1
            continue
        if not (cfg["pct_lo"] <= pct <= cfg["pct_hi"]):
            stat["pct0"] += 1
            continue
        if not (vr is not None and vr >= cfg["vr_min"]):
            stat["vr0"] += 1
            continue
        if not (turn is not None and turn >= cfg["turn_min"]):
            stat["turn0"] += 1
            continue
        if not (mv is not None and cfg["mv_lo"] <= mv <= cfg["mv_hi"]):
            stat["mv0"] += 1
            continue
        if amt < cfg["amt_min"] or price < cfg["price_min"]:
            stat["amt0"] += 1
            continue
        r = dict(r)
        r["amount_yi"] = round(amt, 2)
        out.append(r)
    stat["coarse"] = len(out)
    return out, stat


def _pick_probe(coarse, limit):
    """挑选进入日K判定的候选。

    重要：不能「按量比全局 Top N」——那样选出来的一半是**已经拉起来**的高位放量票
    （实测 2026-09-22 14:28：量比 Top90 里 38 只 pos120 > 0.40 被淘汰，而量比 3~4 区间
    真正的底部启动票（如江淮汽车 量比 3.87）反而被挤出前 90，命中 0）。
    正确做法：按涨幅分档（底部放量启动多在 +1%~+6%），档内按量比排序后轮转取样，
    保证「温和上涨档」的放量票不被高涨幅档的高量比票淹没。
    """
    bands = [(0.0, 2.0), (2.0, 4.0), (4.0, 6.0), (6.0, 99.0)]
    buckets = []
    for lo, hi in bands:
        b = [r for r in coarse if lo <= (r.get("pct") or 0) < hi]
        b.sort(key=lambda r: (r.get("vol_ratio") or 0), reverse=True)
        buckets.append(b)
    out, idx = [], 0
    while len(out) < limit and any(buckets):
        progressed = False
        for b in buckets:
            if idx < len(b):
                out.append(b[idx])
                progressed = True
                if len(out) >= limit:
                    break
        if not progressed:
            break
        idx += 1
    return out


def scan(top=15, cfg=None, verbose=True, cache_kline=True, now=None):
    """主扫描入口。返回 {'meta': {...}, 'rows': [...]}。"""
    cfg = cfg or {}
    cfg.setdefault("pct_lo", PCT_LO)
    cfg.setdefault("pct_hi", PCT_HI)
    cfg.setdefault("vr_min", VR_MIN)
    cfg.setdefault("turn_min", TURN_MIN)
    cfg.setdefault("amt_min", AMT_MIN)
    cfg.setdefault("mv_lo", MV_LO)
    cfg.setdefault("mv_hi", MV_HI)
    cfg.setdefault("price_min", PRICE_MIN)
    cfg.setdefault("pos_max", POS_MAX)
    cfg.setdefault("dd_min", DD_MIN)
    cfg.setdefault("ma60_max", MA60_MAX)
    cfg.setdefault("vr_strong", VR_STRONG)
    cfg.setdefault("amt_mult_min", AMT_MULT_MIN)

    now = now or datetime.now()
    progress = trading_progress(now)
    codes = load_codes()
    t0 = time.time()
    rows = fetch_full_market(codes)
    t_fetch = time.time() - t0

    coarse, stat = _coarse(rows, cfg)
    # 粗筛后按「量比 × 涨幅温和度」预排序，取前 K_FETCH_MAX 只做日K判定（控请求量）
    coarse.sort(key=lambda r: (r.get("vol_ratio") or 0), reverse=True)
    probe = coarse[:K_FETCH_MAX]

    hits, skipped = [], []
    with ThreadPoolExecutor(max_workers=8) as ex:
        ks = list(ex.map(lambda r: daily_k(r["code"], use_cache=cache_kline), probe))
    for r, k in zip(probe, ks):
        if not k:
            skipped.append((r["code"], "日K缺失"))
            continue
        st = _kstats(k, r["price"], r.get("vol_shou") or 0, progress)
        if not st:
            skipped.append((r["code"], "日K不足"))
            continue
        row = dict(r)
        row.update(st)
        amt_proj = (row["amount_yi"] / progress) if progress > 0.05 else row["amount_yi"]
        row["amount_proj_yi"] = round(amt_proj, 2)
        # 近20日均成交额(亿) = 均量(手) × 100 股/手 × 现价 / 1e8 = 均量 × 现价 / 1e6
        avg_amt20_yi = (row["avg_vol20_shou"] * row["price"] / 1e6) if row["avg_vol20_shou"] else 0.0
        row["avg_amt20_yi"] = round(avg_amt20_yi, 2)
        row["amt_mult20"] = round(amt_proj / avg_amt20_yi, 2) if avg_amt20_yi > 0 else None
        # —— 底部层过滤 ——
        if row["pos120"] > cfg["pos_max"]:
            skipped.append((row["code"], f"位置偏高 pos={row['pos120']}"))
            continue
        if row["dd120"] < cfg["dd_min"]:
            skipped.append((row["code"], f"回撤不足 dd={row['dd120']}"))
            continue
        if row["price_over_ma60"] is not None and row["price_over_ma60"] > cfg["ma60_max"]:
            skipped.append((row["code"], f"偏离MA60 {row['price_over_ma60']}"))
            continue
        # —— 放量层过滤 ——
        if (row.get("vol_ratio") or 0) < cfg["vr_strong"]:
            skipped.append((row["code"], f"量比不足 {row.get('vol_ratio')}"))
            continue
        if row["avg_vol5_shou"] < MIN_AVG_VOL_SHOU:
            skipped.append((row["code"], "近期绝对量过小"))
            continue
        # 与自身近20日均额对比（量比是「对5日均量」的口径，此处再加一道「对20日」的口径做交叉确认）
        if row["amt_mult20"] is not None and row["amt_mult20"] < cfg["amt_mult_min"]:
            skipped.append((row["code"], f"20日量能倍数不足 {row['amt_mult20']}"))
            continue
        row.update(_score(row))
        row["remain_to_zt_pct"] = (round((row["zt_price"] / row["price"] - 1) * 100, 2)
                                   if row.get("zt_price") else None)
        hits.append(row)

    hits.sort(key=lambda r: r["score"], reverse=True)
    # 输出下限：砍掉分数尾巴的噪声票（雷达仍保留全部命中数在 meta.hits 里可对比）
    shown = [r for r in hits if r["score"] >= MIN_SCORE_OUT]
    # 推荐标记：绝对分达标 + 只数硬上限，双门控（这一天最多 RECOMMEND_MAX 只）
    rec_n = 0
    for r in shown:
        ok = r["score"] >= RECOMMEND_MIN_SCORE and rec_n < RECOMMEND_MAX
        r["recommend"] = bool(ok)
        if ok:
            rec_n += 1
            r["rank_rec"] = rec_n
    meta = {
        "asof": now.strftime("%Y-%m-%d %H:%M:%S"),
        "trade_date": now.strftime("%Y-%m-%d"),
        "progress": round(progress, 3),
        "universe": stat["total"],
        "coarse": stat["coarse"],
        "k_probed": len(probe),
        "hits": len(hits),
        "out": len(shown),
        "recommend": rec_n,
        "min_score_out": MIN_SCORE_OUT,
        "recommend_min_score": RECOMMEND_MIN_SCORE,
        "recommend_max": RECOMMEND_MAX,
        "fetch_sec": round(t_fetch, 1),
        "thresholds": cfg,
        "skip_reasons": _tally([s[1].split()[0] for s in skipped]),
    }
    if verbose:
        print(f"[bottom_surge] {meta['asof']} 进度{progress:.0%} | 全A {stat['total']} → "
              f"粗筛 {stat['coarse']} → 日K {len(probe)} → 命中 {len(hits)} → 输出 {len(shown)}"
              f" → 推荐 {rec_n} | 快照 {t_fetch:.1f}s",
              file=sys.stderr)
    return {"meta": meta, "rows": shown[:top], "all_hits": hits}


def _tally(items):
    d = {}
    for k in items:
        d[k] = d.get(k, 0) + 1
    return dict(sorted(d.items(), key=lambda kv: -kv[1]))


# ---------------------------------------------------------------------------
# 信号回放（验证用）：给定一票，逐时间点展示「若此刻触发信号是什么样」
# ---------------------------------------------------------------------------
def replay(code, times=("0945", "1000", "1015", "1030", "1100", "1130",
                        "1300", "1330", "1400", "1430", "1445", "1450")):
    k = daily_k(code, use_cache=True)
    if not k:
        print(f"{code} 日K缺失，无法回放")
        return
    k = [r for r in k if r["date"] < datetime.now().strftime("%Y-%m-%d")] or k
    minute = fetch_minute(code)
    if not minute:
        print(f"{code} 分时缺失")
        return
    hist_high = max(r["high"] for r in k)
    hist_low = min(r["low"] for r in k)
    ma60 = sum(r["close"] for r in k[-60:]) / 60.0
    avg_vol5 = sum(r["vol_shou"] for r in k[-5:]) / 5.0
    prev_close = minute[0][1] if minute else 0
    print(f"\n=== 信号回放 · {code} ===")
    print(f"120日 高/低 = {hist_high:.2f} / {hist_low:.2f} | MA60 = {ma60:.2f} | 近5日均量 = {avg_vol5:.0f} 手")
    print(f"今日 昨收 = {prev_close:.2f} | 全市场分钟数 = {len(minute)}")
    print(f"{'时刻':>6} {'价格':>8} {'涨幅%':>7} {'累计量(手)':>11} {'量倍数':>7} "
          f"{'区间位置':>8} {'距高点回撤':>9} {'价/MA60':>8}")
    by_t = {m[0]: m for m in minute}
    for t in times:
        cand = [m for m in minute if m[0] <= t]
        if not cand:
            continue
        m = cand[-1]
        real_t = m[0]
        price, cum_vol = m[1], _vol_shou(code, m[2])   # 分时累计量同样需科创板量纲修正
        mm = int(real_t[:2]) * 60 + int(real_t[2:])
        mins = (mm - 570) if mm <= 690 else (120 + (mm - 780))
        mins = max(mins, 1)
        pace = cum_vol / mins
        vol_mult = pace / (avg_vol5 / 240.0) if avg_vol5 else 0
        pct = (price / prev_close - 1) * 100 if prev_close else 0
        pos = (price - hist_low) / (hist_high - hist_low) if hist_high > hist_low else 1
        dd = (hist_high - price) / hist_high if hist_high else 0
        print(f"{real_t:>6} {price:>8.2f} {pct:>+7.2f} {cum_vol:>11.0f} {vol_mult:>7.2f} "
              f"{pos:>8.3f} {dd:>8.1%} {price / ma60:>8.3f}")
    last = minute[-1]
    print(f"最新 {last[0]} 价 {last[1]:.2f} 累计量 {last[2]:.0f} 手")


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
def render_text(res, top=15):
    m = res["meta"]
    L = []
    L.append(f"底部放量雷达 · {m['asof']}（交易进度 {m['progress']:.0%}）")
    L.append(f"全A {m['universe']} 只 → 粗筛 {m['coarse']} → 日K判定 {m['k_probed']} → "
             f"命中 {m['hits']} → 输出 {m.get('out', m['hits'])} → 推荐 {m.get('recommend', 0)} 只"
             f"（门槛 ≥{m.get('recommend_min_score', RECOMMEND_MIN_SCORE)}，上限 {m.get('recommend_max', RECOMMEND_MAX)} 只）")
    L.append("")
    if not res["rows"]:
        L.append(f"（无输出：当前无「底部 + 放量 + 未涨停 + 可成交」标的，或全部低于 "
                 f"{m.get('min_score_out', MIN_SCORE_OUT)} 分）")
        L.append(f"淘汰原因分布：{m['skip_reasons']}")
        return "\n".join(L)
    hdr = (f"{'推荐':<5}{'#':>2} {'名称':<8}{'代码':<8}{'现价':>7}{'涨幅%':>7}{'量比':>6}{'换手%':>7}"
           f"{'成交额亿':>8}{'位置':>6}{'回撤':>7}{'距高':>7}{'流通亿':>8}{'总分':>6}")
    L.append(hdr)
    L.append("-" * len(hdr))
    for i, r in enumerate(res["rows"][:top], 1):
        tag = "★推荐" if r.get("recommend") else ""
        L.append(f"{tag:<5}{i:>2} {r['name'][:6]:<8}{r['code']:<8}{r['price']:>7.2f}{r['pct']:>+7.2f}"
                 f"{r.get('vol_ratio') or 0:>6.2f}{r.get('turnover') or 0:>7.2f}"
                 f"{r['amount_yi']:>8.2f}{r['pos120']:>6.2f}{r['dd120']:>7.1%}"
                 f"{r['remain_to_zt_pct'] or 0:>6.1f}%{r.get('mv_yi') or 0:>8.0f}{r['score']:>6.1f}")
    L.append("")
    L.append("明细（总分 = 位置35 + 回撤15 + 量能30 + 涨幅10 + 流动性10）：")
    for r in res["rows"][:top]:
        L.append(f"  · {r['name']}({r['code']}) {r['score']:.1f} = "
                 f"位置{r['s_pos']}/回撤{r['s_dd']}/量能{r['s_vol']}/涨幅{r['s_pct']}/流动{r['s_liq']}"
                 f" | 120日 {r['l120']}~{r['h120']} 现处 {r['pos120']:.0%} 位 | "
                 f"MA60={r['ma60']} 价/MA60={r['price_over_ma60']} | "
                 f"近5日均量 {r['avg_vol5_shou']} 手 预估量倍数 {r['vol_mult5']} | "
                 f"距涨停还有 {r['remain_to_zt_pct']}%")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="A股底部放量实时扫描")
    ap.add_argument("--top", type=int, default=TOP_FOR_UI)
    ap.add_argument("--json", action="store_true", help="输出 JSON 到 stdout")
    ap.add_argument("--save", action="store_true", help="落盘 data/bottom_surge_latest.json")
    ap.add_argument("--explore", action="store_true", help="打印粗筛池分布，用于阈值实测")
    ap.add_argument("--replay", metavar="CODE", help="单票信号回放")
    ap.add_argument("--no-cache", action="store_true", help="不使用日K缓存")
    args = ap.parse_args()

    if args.replay:
        replay(args.replay)
        return

    if args.explore:
        codes = load_codes()
        rows = fetch_full_market(codes)
        print(f"全A {len(rows)} 只快照已取（{datetime.now():%H:%M:%S}）")
        for label, vals, cuts in [
            ("涨幅%", [r["pct"] for r in rows if r.get("pct") is not None],
             [-10, -5, -2, 0, 2, 4, 6, 8, 9.5, 100]),
            ("量比", [r["vol_ratio"] for r in rows if r.get("vol_ratio")],
             [0, 0.8, 1.2, 1.5, 2, 3, 5, 10, 1e9]),
            ("换手%", [r["turnover"] for r in rows if r.get("turnover")],
             [0, 1, 2, 3, 5, 10, 20, 1e9]),
            ("流通市值亿", [r["mv_yi"] for r in rows if r.get("mv_yi")],
             [0, 20, 50, 100, 300, 800, 3000, 1e9]),
            ("成交额亿", [(r.get("amount_wan") or 0) / 1e4 for r in rows],
             [0, 0.5, 1, 2, 5, 10, 30, 1e9]),
        ]:
            print(f"\n--- {label} 分布 ---")
            n = len(vals)
            for a, b in zip(cuts, cuts[1:]):
                c = sum(1 for v in vals if a <= v < b)
                print(f"  [{a:>8g}, {b:>8g}) : {c:>5} 只  {c / n * 100:>5.1f}%")
        return

    res = scan(top=args.top, cache_kline=not args.no_cache)
    if args.save:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        LATEST_FILE.write_text(json.dumps({"meta": res["meta"], "rows": res["rows"]},
                                          ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"已落盘 {LATEST_FILE}", file=sys.stderr)
    if args.json:
        print(json.dumps({"meta": res["meta"], "rows": res["rows"]}, ensure_ascii=False, indent=1))
    else:
        print(render_text(res, top=args.top))


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""跟庄建仓 · 全市场扫描器（2026-11-23）

用户诉求：把他提供的通达信「跟庄建仓」公式接到工作台，能看到全市场命中的票。

公式（与用户截图逐行一致）：
  AA:=100*(C-LLV(L,30))/(HHV(H,30)-LLV(L,30)+0.001);   {当前在30日区间的位置 0~100}
  BB:=EMA(AA,8);                                        {定义但未用于 XG}
  CC:=(AA-REF(AA,3))*100/(REF(AA,3)+0.001);            {相对3天前的拐头强度 %}
  DD:=MA(V,5)>MA(V,10);                                 {放量}
  EE:=C>REF(C,1);                                       {收阳}
  XG:AA<13 AND CC>13 AND DD AND EE;                     {底部13% + 拐头 + 放量 + 收阳}

数学语义（实测确认）：AA<13 且 CC>13 = 当前仍在30日底部13%区间，但相对3天前已抬升>13%
= 「底部横盘后刚拐头」的启动点。用加速赶底/高位票测试均不命中，用底部横盘微抬头样例命中（AA=8.3/CC=193）。

架构：复用 bottom_surge 的全A帧获取 + 日K缓存（同一份数据，零重复请求）：
  全A帧粗筛（收阳+温和放量+非涨停+非ST） → 并发拉日K → genzhuang_xg() 逐只判定 → 命中按 CC 排序

只读、不写任何文件、不进派单/弹窗链路。
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import bottom_surge
import strategy_router

# 粗筛参数（把全A压到可拉日K的量级）
PCT_LO, PCT_HI = 0.3, 8.0       # 收阳但不过热（公式要 C>REF(C,1)）
VR_MIN = 1.15                   # 温和放量（公式要 MA5>MA10，实时量比是前置信号）
AMT_MIN = 0.3                   # 成交额下限（亿），排除死水票
PRICE_MIN = 2.0
PROBE_MAX = 300                 # 进入日K判定的上限（8线程约 4s）
K_N = 40                        # 日K根数（公式需 33 根）
MAX_WORKERS = 8


def _coarse(rows: list[dict]) -> tuple[list[dict], dict]:
    """用全A实时帧粗筛：收阳 + 温和放量 + 排除噪声。"""
    stat = {"total": 0, "bad": 0, "pct": 0, "vr": 0, "zt": 0, "coarse": 0}
    out = []
    for r in rows or []:
        stat["total"] += 1
        name = str(r.get("name") or "")
        price = r.get("price") or 0
        pct = r.get("pct")
        vr = r.get("vol_ratio")
        amt = (r.get("amount_wan") or 0) / 1e4
        zt = r.get("zt_price")
        if price < PRICE_MIN or pct is None or not price:
            stat["bad"] += 1
            continue
        if "ST" in name.upper() or "退" in name:
            stat["bad"] += 1
            continue
        if amt < AMT_MIN:
            stat["bad"] += 1
            continue
        if zt and price >= zt - 0.011:        # 已封涨停，买不进
            stat["zt"] += 1
            continue
        if not (PCT_LO <= pct <= PCT_HI):     # 必须收阳、不过热
            stat["pct"] += 1
            continue
        if vr is None or vr < VR_MIN:
            stat["vr"] += 1
            continue
        out.append(r)
    # 按量比降序，取前 PROBE_MAX
    out.sort(key=lambda r: (r.get("vol_ratio") or 0), reverse=True)
    out = out[:PROBE_MAX]
    stat["coarse"] = len(out)
    return out, stat


def _append_today(k: list[dict], rec: dict) -> list[dict]:
    """把今日实时帧当作今天的日K接上去。

    关键：日K缓存截止上一交易日，而公式要算「今天」的 AA/CC/DD/EE。
    必须用实时帧的今日 OHLCV 替换/追加，否则 DD(放量)/EE(收阳)算的是上周五，
    与用户看到的今日盘面不符。
    """
    k = list(k or [])
    price = rec.get("price")
    if not price:
        return k
    today = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "open": float(rec.get("open") or price),
        "close": float(price),
        "high": float(rec.get("high") or price),
        "low": float(rec.get("low") or price),
        "vol_shou": float(rec.get("vol_shou") or 0),
    }
    # 若缓存里已有今日 bar（盘后日K已更新），替换；否则追加
    if k and k[-1].get("date") == today["date"]:
        k[-1] = today
    else:
        k.append(today)
    return k


def _eval_one(rec: dict, k: list[dict]) -> dict | None:
    """对单只票跑公式，命中返回结果行，否则 None。"""
    k = _append_today(k, rec)
    if not k or len(k) < 33:
        return None
    c = [float(x["close"]) for x in k]
    h = [float(x["high"]) for x in k]
    l = [float(x["low"]) for x in k]
    v = [float(x.get("vol_shou", 0)) for x in k]
    g = strategy_router.genzhuang_xg(c, h, l, v)
    if not g.get("hit"):
        return None
    # 命中：附加上升加速度与形态完整性判断
    aa, cc = g["aa"], g["cc"]
    # 连续几日抬升？数最近满足 close 递增的天数
    up_days = 0
    for i in range(len(c) - 1, 0, -1):
        if c[i] > c[i - 1]:
            up_days += 1
        else:
            break
    return {
        "code": str(rec.get("code") or "").zfill(6),
        "name": rec.get("name") or "",
        "price": rec.get("price"),
        "pct": rec.get("pct"),
        "vol_ratio": rec.get("vol_ratio"),
        "turnover": rec.get("turnover"),
        "amount_yi": round((rec.get("amount_wan") or 0) / 1e4, 2),
        "mv_yi": rec.get("mv_yi"),
        "aa": aa, "cc": cc, "up_days": up_days,
        "ma5_gt_ma10": g["dd"], "close_up": g["ee"],
        "ma5": round(sum(c[-5:]) / 5, 2),
        "ma10": round(sum(c[-10:]) / 10, 2),
        "pos30": round(aa / 100, 3),
        "desc": g["desc"],
        "asof": k[-1]["date"],
    }


def scan(force: bool = False) -> dict:
    """主扫描入口。返回 {'ok','asof','stat','hits','top'}。"""
    t0 = time.time()
    codes = bottom_surge.load_codes()          # 全A代码表（复用）
    rows = bottom_surge.fetch_full_market(codes)  # 全A实时帧（复用）
    coarse, stat = _coarse(rows)
    # 并发拉日K（复用 bottom_surge 的 5 分钟缓存）
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        ks = list(ex.map(lambda r: bottom_surge.daily_k(r["code"], n=K_N), coarse))
    hits = []
    for rec, k in zip(coarse, ks):
        hit = _eval_one(rec, k)
        if hit:
            hits.append(hit)
    # 按 CC（拐头强度）降序，CC 越高说明从底部抬升越猛
    hits.sort(key=lambda x: (x.get("cc") or 0), reverse=True)
    return {
        "ok": True,
        "asof": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed": round(time.time() - t0, 2),
        "stat": stat,
        "universe": stat["total"],
        "probed": stat["coarse"],
        "hit_count": len(hits),
        "hits": hits,
        "formula": {
            "name": "跟庄建仓",
            "lines": [
                "AA:=100*(C-LLV(L,30))/(HHV(H,30)-LLV(L,30)+0.001);",
                "BB:=EMA(AA,8);",
                "CC:=(AA-REF(AA,3))*100/(REF(AA,3)+0.001);",
                "DD:=MA(V,5)>MA(V,10);",
                "EE:=C>REF(C,1);",
                "XG:AA<13 AND CC>13 AND DD AND EE;",
            ],
            "meaning": "AA<13=股价在30日区间底部13%以内；CC>13=相对3天前已拐头抬升>13%；"
                       "DD=5日均量>10日均量(放量)；EE=当日收阳。四条件同时满足=底部横盘后刚启动。",
        },
    }


def selftest():
    print("=== genzhuang_scan selftest ===")
    # 粗筛逻辑单测（纯数据）
    rows = [
        {"code": "000001", "name": "正常票", "price": 10, "pct": 3, "vol_ratio": 2.0,
         "amount_wan": 5000, "zt_price": 11},
        {"code": "000002", "name": "ST票", "price": 10, "pct": 3, "vol_ratio": 2.0,
         "amount_wan": 5000, "zt_price": 11},
        {"code": "000003", "name": "涨停票", "price": 11, "pct": 10, "vol_ratio": 3.0,
         "amount_wan": 8000, "zt_price": 11},
        {"code": "000004", "name": "阴跌票", "price": 10, "pct": -2, "vol_ratio": 2.0,
         "amount_wan": 5000, "zt_price": 11},
    ]
    out, stat = _coarse(rows)
    codes = {r["code"] for r in out}
    assert "000001" in codes and "000002" not in codes and "000003" not in codes and "000004" not in codes, out
    print("[1] 粗筛 OK:", stat)

    # 公式命中单测：底部横盘微抬头
    c, h, l, v = [], [], [], []
    px = 30.0
    for i in range(45):
        if i < 30:      px -= 0.5
        elif i < 42:    px = 10 + (i % 3) * 0.05
        else:           px = 10.3 + (i - 41) * 0.15
        c.append(px); h.append(px + 0.3); l.append(px - 0.3)
        v.append(1000000 if i < 40 else (1300000 if i < 43 else 1800000))
    k = [{"date": f"d{i}", "close": c[i], "high": h[i], "low": l[i], "vol_shou": v[i]}
         for i in range(45)]
    hit = _eval_one({"code": "999999", "name": "测试"}, k)
    assert hit and hit["aa"] < 13 and hit["cc"] > 13, hit
    print("[2] 公式命中 OK: AA=%.1f CC=%.1f" % (hit["aa"], hit["cc"]))

    # 高位票不应命中
    k2 = [{"date": f"d{i}", "close": 20 + i * 0.5, "high": 21 + i * 0.5,
           "low": 19 + i * 0.5, "vol_shou": 1000000} for i in range(45)]
    assert _eval_one({"code": "000000", "name": "高位"}, k2) is None
    print("[3] 高位票正确排除 OK")
    print("SELFTEST ALL PASS")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        selftest()
    else:
        import json
        print(json.dumps(scan(), ensure_ascii=False, indent=2))

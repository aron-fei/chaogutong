#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
盘后综合行情复盘（market_review.py）—— 2026-11-18 新增，与 15:30 策略复盘（recap_confirm）
互补成双档闭环：

  · 15:30 策略复盘：验证昨日建议、评估规则纪律、给策略调整建议（人审台账）
  · 17:35 行情复盘（本脚本）：龙虎榜披露后，产出「指数/主力资金/板块/个股/龙虎榜」
    全方位行情复盘 + 【明日竞价作战计划】

作战计划同时落两份产物，形成机器可读闭环：
  1. market_review_<日期>.md          —— 人看的完整复盘报告
  2. battle_plan_<次日>.json          —— 调度器供票源「盘后作战」（ADR-0052）读取
     battle_plan_latest.json          —— 固定名副本，调度器兼容读取

铁律：
  · 任何数据源失败 → 该节标注「数据缺失」，绝不阻断整体产出（fail-open）
  · 只输出建议与计划，绝不自动修改策略代码/配置/回测库
  · 网络一律绕过代理；akshare 用 anaconda python3（managed venv 缺 akshare）

用法：
  python3 market_review.py                    # 复盘今日（默认）
  python3 market_review.py --date 2026-11-18  # 指定日期
  python3 market_review.py --selftest         # 离线自检（不拉网络）
"""

import os
import sys
import json
import time
import datetime as _dt

# ---- 网络环境：绕过代理（qt.gtimg/东财均直连）----
for _k in list(os.environ):
    if "proxy" in _k.lower():
        os.environ.pop(_k, None)

REPORTS_DIR = os.path.expanduser(os.environ.get("REPORTS_DIR", "~/Work/reports"))
MONITOR_DIR = os.path.dirname(os.path.abspath(__file__))

INDEX_CODES = ["sh000001", "sz399001", "sz399006", "sh000688"]
INDEX_NAMES = {"sh000001": "上证指数", "sz399001": "深证成指",
               "sz399006": "创业板指", "sh000688": "科创50"}

try:
    import requests
except ImportError:
    requests = None

# ---- 东财 WAF 会按 TLS 指纹识别 python-requests 并断连（RemoteDisconnected），
# ---- curl 正常而 requests 必挂（2026-11-25 实测定位）。分流：eastmoney 域名改走
# ---- curl_cffi 伪装 Chrome 指纹（akshare 内部 requests.get 同进程生效），其余域名原生。
if requests is not None:
    try:
        from curl_cffi import requests as _cr

        _orig_get = requests.get

        def _get_by_host(url, *a, **kw):
            if "eastmoney.com" in url:
                kw.pop("proxies", None)
                kw.pop("verify", None)
                kw.setdefault("timeout", 10)
                return _cr.get(url, *a, impersonate="chrome", **kw)
            return _orig_get(url, *a, **kw)

        requests.get = _get_by_host
    except ImportError:
        # curl_cffi 不可用 → 保持原生 requests（与下方 akshare 分支同款哨兵写法，
        # 禁止 except: pass —— 交付铁律：静默吞异常必须显式化）
        _cr = None

try:
    import akshare as ak
except ImportError:
    ak = None


def _fail(section, e):
    return {"_error": "%s: %s" % (type(e).__name__, str(e)[:120])}


def _safe_fetch(label, fn, *args, _retries=4):
    """带重试的抓取包装：东财接口频繁瞬断（RemoteDisconnected/ConnectionError），
    最多 _retries 次递增退避；全部失败 → 该节 _fail 降级，绝不阻断整体产出（fail-open）。"""
    last = None
    for i in range(_retries):
        try:
            return fn(*args)
        except Exception as e:
            last = e
            if i < _retries - 1:
                time.sleep([3, 8, 15, 25][min(i, 3)])  # 东财对突发连发限流，需长退避
    print("  [warn] %s 重试 %d 次仍失败：%s" % (label, _retries, str(last)[:80]))
    return _fail(label, last)


def _fmt_yi(v):
    """元 → 亿元字符串"""
    try:
        return "%.2f亿" % (float(v) / 1e8)
    except (TypeError, ValueError):
        return "—"


def _norm_code(c):
    s = str(c or "").strip()
    if s.endswith(".SS") or s.endswith(".SZ"):
        s = s[:-3]
    return s.zfill(6) if s.isdigit() else s


def is_trading_day(date_str):
    """akshare 官方交易日历；异常时按周一~周五放行（fail-open）。"""
    try:
        df = ak.tool_trade_date_hist_sina()
        days = set(str(d).replace("-", "") for d in df["trade_date"])
        return date_str.replace("-", "") in days
    except Exception:
        return _dt.datetime.strptime(date_str, "%Y-%m-%d").weekday() < 5


def next_trade_date(date_str):
    df = ak.tool_trade_date_hist_sina()
    days = sorted(str(d).replace("-", "") for d in df["trade_date"])
    cur = date_str.replace("-", "")
    later = [d for d in days if d > cur]
    if not later:
        return None
    d = later[0]
    return "%s-%s-%s" % (d[:4], d[4:6], d[6:8])


# ---------------------------------------------------------------- 指数与日内节奏
def fetch_indices():
    if requests is None:
        return _fail("指数", "no requests")
    r = requests.get(
        "https://qt.gtimg.cn/q=" + ",".join(INDEX_CODES),
        timeout=10, proxies={"http": None, "https": None})
    r.encoding = "gbk"
    out = {}
    for line in r.text.split(";"):
        if '="' not in line:
            continue
        key, payload = line.split('="', 1)
        code = key.replace("v_", "").strip()
        f = payload.rstrip('"').split("~")
        if len(f) < 40:
            continue
        out[code] = {"name": f[1], "close": float(f[3]), "prev": float(f[4]),
                     "open": float(f[5]), "high": float(f[33]), "low": float(f[34]),
                     "amount_亿": float(f[37]) / 1e4 if float(f[37] or 0) > 0 else None}
    # 日内节奏：10:40 位置（上证）
    try:
        r2 = requests.get(
            "https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=sh000001",
            timeout=10, proxies={"http": None, "https": None})
        mins = r2.json()["data"]["sh000001"]["data"]["data"]
        p1040 = None
        for m in mins:
            t, p = m.split()[0], float(m.split()[1])
            if t == "1040":
                p1040 = p
        q = out.get("sh000001")
        if q and p1040 and q["high"] != q["low"]:
            q["p1040"] = p1040
            q["pos1040"] = (q["high"] - p1040) / (q["high"] - q["low"]) * 100
            q["chg_after_1040"] = (q["close"] / p1040 - 1) * 100
    except Exception as e:
        out["_1040_error"] = str(e)[:80]
    return out


# ---------------------------------------------------------------- 两市主力资金
def fetch_market_flow(date_str):
    if ak is None:
        return _fail("两市资金", "no akshare")
    df = ak.stock_market_fund_flow()
    dates = [str(x)[:10] for x in df["日期"]]
    if date_str not in dates:
        return _fail("两市资金", "当日数据未披露")
    row = df.iloc[dates.index(date_str)].to_dict()
    trend = [{"date": str(r["日期"])[:10], "main_yi": float(r["主力净流入-净额"]) / 1e8}
             for _, r in df.tail(6).iterrows()]
    return {"main_yi": float(row["主力净流入-净额"]) / 1e8,
            "main_pct": float(row["主力净流入-净占比"]),
            "xl_yi": float(row["超大单净流入-净额"]) / 1e8,
            "trend": trend}


# ---------------------------------------------------------------- 板块资金流
def fetch_sector_flow():
    if ak is None:
        return _fail("板块资金", "no akshare")
    df = ak.stock_sector_fund_flow_rank(indicator="今日", sector_type="行业资金流")
    df2 = ak.stock_sector_fund_flow_rank(indicator="今日", sector_type="概念资金流")
    def _top(d, n, asc):
        d = d.dropna(subset=["今日主力净流入-净额"])
        d = d.sort_values("今日主力净流入-净额", ascending=asc).head(n)
        # 两级行业名去重（「银行」与「银行Ⅱ」同源）：短名是长名前缀时丢弃短名
        names = [r["名称"] for _, r in d.iterrows()]
        keep = [n for n in names
                if not any(o != n and o.startswith(n) for o in names)]
        return [{"name": r["名称"], "pct": float(r["今日涨跌幅"]),
                 "main_yi": float(r["今日主力净流入-净额"]) / 1e8,
                 "lead": r.get("今日主力净流入最大股", "")}
                for _, r in d.iterrows() if r["名称"] in keep]
    return {"in": _top(df, 10, False), "out": _top(df, 5, True),
            "concept_in": _top(df2, 5, False)}


# ---------------------------------------------------------------- 个股资金流
def fetch_stock_flow():
    if ak is None:
        return _fail("个股资金", "no akshare")
    df = ak.stock_individual_fund_flow_rank(indicator="今日")
    df["今日主力净流入-净额"] = __import__("pandas").to_numeric(
        df["今日主力净流入-净额"], errors="coerce")
    df["今日涨跌幅"] = __import__("pandas").to_numeric(
        df["今日涨跌幅"], errors="coerce")
    df = df.dropna(subset=["今日主力净流入-净额"])
    def _pick(d, n, max_pct=None, min_pct=None, asc=False):
        d = d.sort_values("今日主力净流入-净额", ascending=asc)
        rows = []
        for _, r in d.iterrows():
            pct = r["今日涨跌幅"]
            if max_pct is not None and pct > max_pct:
                continue
            if min_pct is not None and pct < min_pct:
                continue
            rows.append({"code": _norm_code(r["代码"]), "name": r["名称"],
                         "close": r["最新价"], "pct": float(pct),
                         "main_yi": float(r["今日主力净流入-净额"]) / 1e8,
                         "main_pct": float(r["今日主力净流入-净占比"])})
            if len(rows) >= n:
                break
        return rows
    return {"in_top": _pick(df, 12),
            "absorbable": _pick(df, 6, max_pct=7.0),   # 未涨停可低吸
            "out_top": _pick(df, 8, asc=True)}


# ---------------------------------------------------------------- 龙虎榜
def fetch_lhb(date_str):
    if ak is None:
        return _fail("龙虎榜", "no akshare")
    ymd = date_str.replace("-", "")
    df = ak.stock_lhb_detail_em(start_date=ymd, end_date=ymd)
    if df.empty:
        return _fail("龙虎榜", "当日未披露（通常 17:00 后）")
    df = df[df["龙虎榜净买额"].notna()]
    # 按代码去重：取净买额绝对值最大的那条上榜原因
    best = {}
    for _, r in df.iterrows():
        code = _norm_code(r["代码"])
        v = float(r["龙虎榜净买额"])
        if code not in best or abs(v) > abs(best[code]["netbuy"]):
            best[code] = {"code": code, "name": r["名称"],
                          "close": float(r["收盘价"]), "pct": float(r["涨跌幅"]),
                          "netbuy": v, "reason": str(r["上榜原因"])[:24],
                          "read": str(r["解读"])[:40], "turnover": float(r["换手率"])}
    rows = sorted(best.values(), key=lambda x: -x["netbuy"])
    return {"all_n": len(rows),
            "netbuy_top": rows[:10],
            "netsell_top": sorted(rows, key=lambda x: x["netbuy"])[:5]}


# ---------------------------------------------------------------- 涨停梯队
def fetch_zt_pool(date_str):
    if ak is None:
        return _fail("涨停梯队", "no akshare")
    df = ak.stock_zt_pool_em(date=date_str.replace("-", ""))
    rows = [{"code": _norm_code(r["代码"]), "name": r["名称"],
             "pct": float(r["涨跌幅"]), "boards": int(r["连板数"]),
             "first_time": str(r["首次封板时间"]),
             "seal_亿": float(r.get("封板资金", 0)) / 1e8,
             "breaks": int(r.get("炸板次数", 0)),
             "reason": str(r.get("所属行业", ""))[:16]}
            for _, r in df.iterrows()]
    ladders = {}
    for r in rows:
        ladders.setdefault(r["boards"], []).append(r)
    prev = ak.stock_zt_pool_previous_em(date=date_str.replace("-", ""))
    prev_pcts = [float(p) for p in prev["涨跌幅"].dropna()]
    return {"count": len(rows),
            "ladders": {k: sorted(v, key=lambda x: -x["seal_亿"])
                        for k, v in sorted(ladders.items(), reverse=True)},
            "max_board": max((r["boards"] for r in rows), default=0),
            "prev_zt_avg_pct": sum(prev_pcts) / len(prev_pcts) if prev_pcts else None}


# ---------------------------------------------------------------- 今日派单票（竞价复核名单）
def load_today_order_picks(date_str):
    picks, seen = [], set()
    for stage in ("0925", "1340", "1440"):
        p = os.path.join(REPORTS_DIR, "order_plan_%s.json" % stage)
        try:
            j = json.load(open(p, encoding="utf-8"))
            for c in (j.get("buy_candidates") or []):
                code = _norm_code(c.get("symbol"))
                if code and code not in seen:
                    seen.add(code)
                    picks.append({"code": code, "name": c.get("name"),
                                  "price": c.get("price"), "stage": stage[:2] + ":" + stage[2:]})
        except Exception:
            continue
    return picks


# ---------------------------------------------------------------- 闸门焦点池
def load_gate_focus():
    p = os.path.join(REPORTS_DIR, "trade_gate_current.json")
    try:
        g = json.load(open(p, encoding="utf-8"))
        return {"asof": g.get("asof"), "stage": g.get("stage"),
                "hard_block": g.get("hard_block"),
                "cap": g.get("position_cap_pct"), "max_picks": g.get("max_picks"),
                "focus": [{"code": _norm_code(f.get("symbol")), "name": f.get("name"),
                           "board_days": f.get("board_days"), "why": (f.get("why") or "")[:50]}
                          for f in (g.get("focus") or [])]}
    except Exception as e:
        return _fail("焦点池", e)


# ---------------------------------------------------------------- 作战计划构造
def build_battle_plan(date_str, next_date, indices, lhb, stock_flow, sector_flow,
                      zt, order_picks, focus):
    """规则透明的盯盘票清单。选票规则全部写死、可解释，n 小防灌水。"""
    picks, seen = [], set()

    def add(code, name, tag, thesis, plan, source_weight):
        code = _norm_code(code)
        if not code or len(code) != 6 or code in seen:
            return
        seen.add(code)
        picks.append({"code": code, "name": name, "tag": tag,
                      "thesis": thesis, "plan": plan, "source_weight": source_weight})

    # 1) 龙虎榜净买 TOP（净买 ≥5000 万，非 ST）：主力真金白银承接
    for r in (lhb.get("netbuy_top") or []):
        if r["netbuy"] < 5e7 or "ST" in r["name"].upper():
            continue
        add(r["code"], r["name"], "龙虎榜净买",
            "净买 %s｜换手 %.0f%%｜%s" % (_fmt_yi(r["netbuy"]), r["turnover"], r["read"]),
            "竞价高开 2~5% 放量承接 → 可轻仓低吸；高开 >7% 或低开破昨收 → 放弃",
            3)
        if len([p for p in picks if p["tag"] == "龙虎榜净买"]) >= 4:
            break

    # 2) 主力净流入可低吸票（未涨停 ≤7%、板块净流入 TOP 背景不强求）
    sector_in_names = {s["lead"] for s in (sector_flow.get("in") or [])}
    for r in (stock_flow.get("absorbable") or []):
        if "ST" in r["name"].upper():
            continue
        tag = "主力净流入" + ("·板块共振" if r["name"] in sector_in_names else "")
        add(r["code"], r["name"], tag,
            "主力净流入 %.2f亿（占比 %.1f%%）｜涨 %.1f%%" % (r["main_yi"], r["main_pct"], r["pct"]),
            "不追高：竞价平开~小高开回踩不破均价 → 分批；跌破昨收 −2% → 放弃",
            2)
        if len([p for p in picks if p["tag"].startswith("主力净流入")]) >= 3:
            break

    # 3) 今日派单票复核（含 skip 票：次日竞价表现决定 skip 台账去留）
    for p in order_picks:
        add(p["code"], p["name"], "今日建议·竞价复核",
            "今日 %s 建议价 %.2f，用户未成交" % (p["stage"], p["price"] or 0),
            "竞价强承接（高开 1~4% 且量能齐）→ 按打板/低吸规则重评；弱开 → 观察",
            1)

    # 4) 闸门焦点池可成交票（明日 15:35 闸门为准，此处只列风向标）
    for f in (focus.get("focus") or [])[:4]:
        add(f["code"], f["name"], "复盘重点",
            "%d 板风向标｜%s" % (f.get("board_days") or 1, f["why"]),
            "一字不可成交只作情绪标尺；非一字换手接力看竞价承接",
            1)

    plan = {
        "version": 1,
        "for_date": next_date,
        "source_review_date": date_str,
        "generated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "market_posture": {
            "gate_asof": focus.get("asof"), "gate_stage": focus.get("stage"),
            "hard_block": focus.get("hard_block"),
            "position_cap_pct": focus.get("cap"),
            "note": "以上为今日收盘口径闸门姿态；明日以 15:35 生成的 asof=次日闸门为准，"
                    "盘中若实时温度≥60 逃生阀可降级硬阻断。",
        },
        "auction_watch": [
            "三指数竞价涨幅与红盘比（红盘比 <40% → 全天防守）",
            "最高板 %s 晋级/断板（空间高度风向标）" % _max_board_name(zt),
            "昨日涨停今日溢价 %.1f%%（接力热度）" % zt["prev_zt_avg_pct"] if zt.get("prev_zt_avg_pct") is not None else "昨日涨停溢价数据缺失",
            "温度读数：09:25 报告温度若 ≥60 且收盘口径为退潮 → 逃生阀降级，仍只做轻仓",
        ],
        "picks": picks,
        "exit_rules": [
            "T+1：今日买入明日才可卖，买入即锁定，勿用\"当日买当日卖\"幻想",
            "10:40 持仓处理为唯一卖出决策点：pct≥9.5 HOLD；4~9.5 温度≥50 HOLD 否则全清；"
            "−2~4 温度≥55 半仓否则全清；≤−5 或温度<45 全清",
            "打板排单票：封单流失 30% 立即撤单，成交后 10:40 按强弱出清",
            "低吸票：跌破买入逻辑位（昨收 −2% / 均价失守）→ 次日 10:40 优先处理",
        ],
    }
    return plan


def _max_board_name(zt):
    try:
        top = zt["ladders"][zt["max_board"]]
        return "%s（%d板）" % (top[0]["name"], zt["max_board"])
    except Exception:
        return "数据缺失"


# ---------------------------------------------------------------- 渲染
def render_md(date_str, next_date, indices, mkt_flow, sector_flow, stock_flow,
              lhb, zt, order_picks, focus, plan):
    L = []
    L.append("# A股盘后综合行情复盘 · %s\n" % date_str)
    L.append("> 龙虎榜档（17:35 后披露数据）+ 明日竞价作战计划。策略侧复盘见 recap_%s.md。\n" % date_str)

    # 1 指数
    L.append("\n## 一、指数与日内节奏\n")
    L.append("| 指数 | 收盘 | 涨跌 | 开盘 | 最高 | 最低 | 成交(亿) |")
    L.append("|---|---|---|---|---|---|---|")
    for c in INDEX_CODES:
        q = indices.get(c)
        if not q:
            L.append("| %s | 数据缺失 | | | | | |" % INDEX_NAMES[c])
            continue
        pct = (q["close"] / q["prev"] - 1) * 100
        L.append("| %s | %.2f | %+.2f%% | %.2f | %.2f | %.2f | %s |" % (
            q["name"], q["close"], pct, q["open"], q["high"], q["low"],
            ("%.0f" % q["amount_亿"]) if q.get("amount_亿") else "—"))
    q = indices.get("sh000001")
    if q and q.get("p1040"):
        L.append("\n- 上证 10:40 = %.2f（日内回落分位 %.0f%%），10:40 后 **%+.2f%%** → "
                 "10:40 按强弱出清规则今日%s。" % (
                     q["p1040"], q.get("pos1040", 0), q.get("chg_after_1040", 0),
                     "吃亏方向（午后修复）" if q.get("chg_after_1040", 0) > 0.3
                     else "占优方向（午后走弱）" if q.get("chg_after_1040", 0) < -0.3
                     else "中性"))

    # 2 两市资金
    L.append("\n## 二、两市主力资金\n")
    if "_error" in mkt_flow:
        L.append("- 数据缺失：%s" % mkt_flow["_error"])
    else:
        L.append("- 今日主力净流入 **%+.2f 亿**（占比 %+.2f%%），超大单 %+.2f 亿。"
                 % (mkt_flow["main_yi"], mkt_flow["main_pct"], mkt_flow["xl_yi"]))
        t = " → ".join("%s %+.1f" % (x["date"][5:], x["main_yi"]) for x in mkt_flow["trend"])
        L.append("- 近 6 日主力净流入轨迹（亿）：`%s`" % t)

    # 3 板块
    L.append("\n## 三、板块资金流向\n")
    if "_error" in sector_flow:
        L.append("- 数据缺失：%s" % sector_flow["_error"])
    else:
        L.append("**净流入 TOP10（行业）**\n")
        L.append("| 板块 | 涨跌 | 主力净流入 | 领涨股 |")
        L.append("|---|---|---|---|")
        for s in sector_flow["in"]:
            L.append("| %s | %+.2f%% | %+.2f亿 | %s |" % (s["name"], s["pct"], s["main_yi"], s["lead"]))
        L.append("\n**净流出 TOP5（行业）**\n")
        L.append("| 板块 | 涨跌 | 主力净流出 |")
        L.append("|---|---|---|")
        for s in sector_flow["out"]:
            L.append("| %s | %+.2f%% | %.2f亿 |" % (s["name"], s["pct"], s["main_yi"]))
        ci = sector_flow.get("concept_in") or []
        if ci:
            L.append("\n**概念净流入 TOP5**：%s" % "、".join(
                "%s(%+.1f亿)" % (s["name"], s["main_yi"]) for s in ci))

    # 4 个股
    L.append("\n## 四、个股主力资金\n")
    if "_error" in stock_flow:
        L.append("- 数据缺失：%s" % stock_flow["_error"])
    else:
        L.append("**主力净流入 TOP12**\n")
        L.append("| 代码 | 名称 | 收盘 | 涨跌 | 主力净流入 | 净占比 |")
        L.append("|---|---|---|---|---|---|")
        for r in stock_flow["in_top"]:
            L.append("| %s | %s | %s | %+.2f%% | %+.2f亿 | %+.1f%% |" % (
                r["code"], r["name"], r["close"], r["pct"], r["main_yi"], r["main_pct"]))
        L.append("\n**主力净流出 TOP8**：%s" % "、".join(
            "%s(%.1f亿)" % (r["name"], r["main_yi"]) for r in stock_flow["out_top"]))

    # 5 龙虎榜
    L.append("\n## 五、龙虎榜复盘（东财口径，近似开盘啦）\n")
    if "_error" in lhb:
        L.append("- 数据缺失：%s" % lhb["_error"])
    else:
        L.append("上榜 %d 只。**净买额 TOP10**：\n" % lhb["all_n"])
        L.append("| 代码 | 名称 | 收盘 | 涨跌 | 净买额 | 换手 | 解读 | 上榜原因 |")
        L.append("|---|---|---|---|---|---|---|---|")
        for r in lhb["netbuy_top"]:
            L.append("| %s | %s | %s | %+.2f%% | %+.2f亿 | %.0f%% | %s | %s |" % (
                r["code"], r["name"], r["close"], r["pct"], r["netbuy"] / 1e8,
                r["turnover"], r["read"], r["reason"]))
        ns = lhb["netsell_top"]
        if ns:
            L.append("\n**净卖出 TOP5**：%s" % "、".join(
                "%s(%.2f亿)" % (r["name"], r["netbuy"] / 1e8) for r in ns))

    # 6 涨停梯队
    L.append("\n## 六、涨停梯队与晋级\n")
    if "_error" in zt:
        L.append("- 数据缺失：%s" % zt["_error"])
    else:
        L.append("- 涨停 %d 只，最高 %d 板，昨日涨停今日溢价 %.2f%%。" % (
            zt["count"], zt["max_board"],
            zt["prev_zt_avg_pct"] if zt.get("prev_zt_avg_pct") is not None else 0))
        for b, rows in zt["ladders"].items():
            def _t(s):
                s = s.zfill(6)
                return s[:2] + ":" + s[2:4]
            L.append("- **%d 板（%d 只）**：%s" % (b, len(rows), "、".join(
                "%s(封%.1f亿/%s/%d次炸)" % (r["name"], r["seal_亿"], _t(r["first_time"]), r["breaks"])
                for r in rows[:6])))

    # 7 焦点池
    L.append("\n## 七、复盘焦点池（今日收盘闸门口径 asof=%s · %s）\n" % (
        focus.get("asof", "—"), focus.get("stage", "—")))
    if "_error" in focus:
        L.append("- 数据缺失：%s" % focus["_error"])
    else:
        for f in focus["focus"]:
            L.append("- %s（%d板）：%s" % (f["name"], f.get("board_days") or 1, f["why"]))

    # 8 作战计划
    L.append("\n## 八、明日（%s）竞价作战计划\n" % next_date)
    L.append("**姿态**：%s｜仓位上限 %s%%｜hard_block=%s（逃生阀：实时温度≥60 降级）" % (
        plan["market_posture"]["gate_stage"] or "—",
        plan["market_posture"]["position_cap_pct"],
        plan["market_posture"]["hard_block"]))
    L.append("\n**竞价看什么**：")
    for w in plan["auction_watch"]:
        L.append("- %s" % w)
    L.append("\n**盯盘票清单（来源规则透明、共 %d 只）**：\n" % len(plan["picks"]))
    L.append("| 股票 | 标签 | 依据 | 操作计划 |")
    L.append("|---|---|---|---|")
    for p in plan["picks"]:
        L.append("| %s %s | %s | %s | %s |" % (p["code"], p["name"], p["tag"], p["thesis"], p["plan"]))
    L.append("\n**如何买**：竞价不追高开 >7% 的票；打板票 09:25 挂涨停价排单（封单流失 30% 撤单）；"
             "低吸票分批、破位放弃。仓位服从明日闸门 cap（上表），单票再受闸门预算约束。")
    L.append("\n**退票逻辑**：")
    for r in plan["exit_rules"]:
        L.append("- %s" % r)
    L.append("\n**闭环说明**：本清单已落 `battle_plan_%s.json`，下一交易日调度器以"
             "「盘后作战」供票源接入 09:25/13:40/14:40 候选池（ADR-0052，fail-open）；"
             "09:25 报告的时段建议即在此基础上叠加竞价实时温度与闸门过滤。\n" % next_date)

    L.append("\n---\n*只输出建议，未修改任何策略代码/配置/回测库。数据源：腾讯行情 + 东方财富（akshare）。*")
    return "\n".join(L)


# ---------------------------------------------------------------- main
def main():
    date_arg = None
    selftest = "--selftest" in sys.argv
    if "--date" in sys.argv:
        date_arg = sys.argv[sys.argv.index("--date") + 1]
    date_str = date_arg or _dt.date.today().strftime("%Y-%m-%d")

    if selftest:
        # 离线自检：渲染函数用合成数据跑通即可
        fake = {"netbuy_top": [], "netsell_top": [], "all_n": 0}
        plan = {"market_posture": {"gate_stage": "退潮", "position_cap_pct": 10,
                                   "hard_block": True},
                "auction_watch": ["x"], "picks": [], "exit_rules": ["y"]}
        md = render_md("2026-01-01", "2026-01-02", {}, {"_error": "selftest"},
                       {"_error": "selftest"}, {"_error": "selftest"},
                       fake, {"_error": "selftest"}, [], {"_error": "selftest"}, plan)
        assert "盘后综合行情复盘" in md and "竞价作战计划" in md
        print("[selftest] OK")
        return 0

    if not is_trading_day(date_str):
        print("非交易日（%s），跳过" % date_str)
        return 0

    print("[1/8] 指数…");      indices = _safe_fetch("指数", fetch_indices)
    print("[2/8] 两市资金…");  mkt_flow = _safe_fetch("两市资金", fetch_market_flow, date_str)
    print("[3/8] 板块资金…");  sector_flow = _safe_fetch("板块资金", fetch_sector_flow)
    print("[4/8] 个股资金…");  stock_flow = _safe_fetch("个股资金", fetch_stock_flow)
    print("[5/8] 龙虎榜…");    lhb = _safe_fetch("龙虎榜", fetch_lhb, date_str)
    print("[6/8] 涨停梯队…");  zt = _safe_fetch("涨停梯队", fetch_zt_pool, date_str)
    order_picks = load_today_order_picks(date_str)
    focus = load_gate_focus()
    try:
        next_date = next_trade_date(date_str)
    except Exception:
        next_date = ( _dt.date.fromisoformat(date_str) + _dt.timedelta(days=1)).strftime("%Y-%m-%d")
    print("[7/8] 构造作战计划…")
    plan = build_battle_plan(date_str, next_date, indices, lhb, stock_flow,
                             sector_flow, zt, order_picks, focus)
    print("[8/8] 渲染…")
    md = render_md(date_str, next_date, indices, mkt_flow, sector_flow,
                   stock_flow, lhb, zt, order_picks, focus, plan)

    md_path = os.path.join(REPORTS_DIR, "market_review_%s.md" % date_str)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    # 战斗计划：日期名 + latest 双写（调度器读 latest 并校验 for_date）
    bp_path = os.path.join(REPORTS_DIR, "battle_plan_%s.json" % next_date)
    latest_path = os.path.join(REPORTS_DIR, "battle_plan_latest.json")
    for p, obj in ((bp_path, plan), (latest_path, plan)):
        with open(p, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
    print("OK → %s | %s | %s" % (md_path, bp_path, latest_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())

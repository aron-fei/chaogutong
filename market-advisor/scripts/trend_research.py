import json
import math
import os
import time
from datetime import datetime, timedelta

import akshare as ak
import pandas as pd


REPORTS_DIR = os.path.expanduser("/Users/williamli/Work/reports")
TREND_FILE = os.path.join(REPORTS_DIR, "trend_research_brief.json")
TREND_TEXT_FILE = os.path.join(REPORTS_DIR, "trend_research_brief.txt")
# 【2026-11-06 · ADR-0050】事件日志：状态翻转事件必须持久化。
# 只与「上一份 brief」对比是易失的 —— 两次刷新之间状态不再变化，事件就消失，
# 下一次 pro_report 恰好读到 events=[] 的那份，用户永远看不到已经发生过的反转。
TREND_EVENTS_FILE = os.path.join(REPORTS_DIR, "trend_research_events.json")
TREND_EVENTS_KEEP_DAYS = 5      # 事件日志保留最近 5 个交易日
TREND_FAILED_FILE = os.path.join(REPORTS_DIR, "trend_research_brief.last_failed.json")
TREND_FAILED_TEXT_FILE = os.path.join(REPORTS_DIR, "trend_research_brief.last_failed.txt")

# AkShare 的东财接口是国内源，走本地外网代理反而容易失败。
for _proxy_key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_proxy_key, None)
os.environ.setdefault("NO_PROXY", "*")

# 工作台自选池（market-monitor）—— scope=trend 的票由趋势池接手（分账闭环，ADR-0050 附录）
MONITOR_WATCHLIST_JSON = os.environ.get(
    "MA_MONITOR_WATCHLIST",
    "/Users/williamli/Work/market-monitor/data/watchlist.json")
WATCHLIST_THEME = "自选·趋势核心（工作台导入）"


def load_trend_scope_watchlist():
    """把工作台自选里 scope=trend 的票并入趋势池宇宙。

    为什么必须有这一步（2026-11-06 分账铁律）：scope=trend 的票已被
    load_self_watchlist_rows() 从短线供票源（13:40/14:40）剔除，若趋势池也不接，
    「题材/权重分账」就变成「两边都不管」——隔离必须配一条归属通道。

    读文件失败 / 无 trend 票 → 返回 []（趋势池仍有种子库，不因自选缺失整体失败）。
    """
    try:
        with open(MONITOR_WATCHLIST_JSON, encoding="utf-8") as f:
            items = json.load(f)
    except Exception:
        return []
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        if str(it.get("scope") or "").strip().lower() != "trend":
            continue
        code = str(it.get("code") or "").strip()
        if not (code.isdigit() and len(code) == 6):
            continue
        out.append({
            "code": code,
            "name": str(it.get("name") or code),
            "theme": WATCHLIST_THEME,
            "chain": "用户自选权重/核心资产",
            "global_binding": "",
            "source": "watchlist",
        })
    return out


def build_universe():
    """趋势池宇宙 = 种子库 ∪ 工作台自选 scope=trend，按 code 去重（种子库优先，含更丰富主题信息）。"""
    universe = list(SEED_WATCHLIST)
    seen = {str(i["code"]) for i in universe}
    for w in load_trend_scope_watchlist():
        if w["code"] not in seen:
            universe.append(w)
            seen.add(w["code"])
    return universe


# 这是中线产业趋势池的“种子库”，不是买入清单。
# 后续可以把它扩展成行业研究报告/财报/公告自动维护的股票池。
SEED_WATCHLIST = [
    {
        "code": "601138",
        "name": "工业富联",
        "theme": "AI服务器/云厂商资本开支",
        "chain": "全球AI算力硬件制造链",
        "global_binding": "北美云厂商、AI服务器产业链",
    },
    {
        "code": "002463",
        "name": "沪电股份",
        "theme": "AI服务器PCB",
        "chain": "高速PCB/交换机/服务器",
        "global_binding": "AI服务器与高速网络设备链",
    },
    {
        "code": "300476",
        "name": "胜宏科技",
        "theme": "AI服务器PCB",
        "chain": "高多层PCB/算力硬件",
        "global_binding": "AI服务器与海外算力链",
    },
    {
        "code": "300502",
        "name": "新易盛",
        "theme": "光模块/CPO",
        "chain": "数据中心光通信",
        "global_binding": "北美AI数据中心光模块需求",
    },
    {
        "code": "300308",
        "name": "中际旭创",
        "theme": "光模块/CPO",
        "chain": "高速光模块",
        "global_binding": "北美AI数据中心光模块需求",
    },
    {
        "code": "688256",
        "name": "寒武纪",
        "theme": "国产AI芯片",
        "chain": "AI推理/训练芯片国产化",
        "global_binding": "国产算力替代",
    },
    {
        "code": "688041",
        "name": "海光信息",
        "theme": "国产CPU/GPU",
        "chain": "国产算力底座",
        "global_binding": "国产服务器与信创算力",
    },
    {
        "code": "002475",
        "name": "立讯精密",
        "theme": "消费电子/AI终端",
        "chain": "苹果/消费电子精密制造",
        "global_binding": "Apple供应链与AI终端升级",
    },
    {
        "code": "300433",
        "name": "蓝思科技",
        "theme": "消费电子/AI终端",
        "chain": "智能终端结构件/玻璃",
        "global_binding": "Apple及全球消费电子链",
    },
    {
        "code": "601127",
        "name": "赛力斯",
        "theme": "智能汽车",
        "chain": "智能电动车/华为车链",
        "global_binding": "智能驾驶与新能源车需求",
    },
    {
        "code": "002594",
        "name": "比亚迪",
        "theme": "新能源车出海",
        "chain": "整车/电池/出口",
        "global_binding": "全球新能源车渗透率提升",
    },
    {
        "code": "300750",
        "name": "宁德时代",
        "theme": "动力电池/储能",
        "chain": "动力电池与储能",
        "global_binding": "全球新能源车与储能产业链",
    },
    {
        "code": "002371",
        "name": "北方华创",
        "theme": "半导体设备",
        "chain": "国产半导体设备",
        "global_binding": "晶圆厂资本开支与国产替代",
    },
    {
        "code": "688012",
        "name": "中微公司",
        "theme": "半导体设备",
        "chain": "刻蚀/MOCVD设备",
        "global_binding": "晶圆厂资本开支与国产替代",
    },
    # 【2026-11-06 补】用户明确点名的核心自选三只此前不在种子库 —— 趋势池天天出报告，
    # 却对它们的底部反转只字不提（详见 ADR-0050）。
    {
        "code": "688981",
        "name": "中芯国际",
        "theme": "晶圆制造",
        "chain": "国产晶圆代工龙头",
        "global_binding": "国产算力芯片制造与先进制程扩产",
    },
    {
        "code": "688347",
        "name": "华虹公司",
        "theme": "晶圆制造",
        "chain": "特色工艺晶圆代工",
        "global_binding": "功率/模拟等特色工艺国产化",
    },
    {
        "code": "300274",
        "name": "阳光电源",
        "theme": "动力电池/储能",
        "chain": "光伏逆变器与储能系统",
        "global_binding": "全球光储装机与出海",
    },
]


def _safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        if isinstance(value, float) and math.isnan(value):
            return default
        return float(value)
    except Exception:
        return default


def _pct(now, base):
    base = _safe_float(base)
    if base == 0:
        return 0.0
    return (_safe_float(now) / base - 1.0) * 100.0


def _market_prefix(code):
    return "SH" + code if code.startswith(("6", "9")) else "SZ" + code


def _tx_symbol(code):
    return ("sh" if code.startswith(("6", "9")) else "sz") + code


def _normalize_hist(df):
    if df is None or df.empty:
        return df

    rename_map = {
        "date": "日期",
        "open": "开盘",
        "close": "收盘",
        "high": "最高",
        "low": "最低",
        "amount": "成交额",
    }
    df = df.rename(columns=rename_map).copy()

    for col in ("收盘", "最高", "成交额"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # 腾讯源的 amount 常见为成交量口径，补成近似成交额，避免流动性评分失真。
    if "成交额" in df.columns and "收盘" in df.columns:
        amount_max = _safe_float(df["成交额"].max())
        if amount_max and amount_max < 100000000:
            df["成交额"] = df["成交额"] * df["收盘"] * 100

    if "换手率" not in df.columns:
        df["换手率"] = 0

    return df


def fetch_hist(code):
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=430)).strftime("%Y%m%d")
    first_error = None
    try:
        df = ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust="qfq",
            timeout=8,
        )
        if df is not None and not df.empty:
            return _normalize_hist(df)
    except Exception as exc:
        first_error = exc

    try:
        df = ak.stock_zh_a_hist_tx(
            symbol=_tx_symbol(code),
            start_date=start_date,
            end_date=end_date,
            adjust="qfq",
            timeout=8,
        )
        if df is not None and not df.empty:
            return _normalize_hist(df)
    except Exception as exc:
        if first_error:
            raise RuntimeError(f"东财源失败: {first_error}; 腾讯源失败: {exc}")
        raise

    if first_error:
        raise RuntimeError(f"东财源失败: {first_error}; 腾讯源为空")
    raise RuntimeError("历史行情为空")



def fetch_research_heat(code):
    try:
        df = ak.stock_research_report_em(symbol=code)
        if df is None or df.empty:
            return {"count_90d": 0, "latest": []}

        latest = []
        count_90d = 0
        cutoff = datetime.now() - timedelta(days=90)
        for _, row in df.head(8).iterrows():
            date_text = str(row.get("日期", ""))
            try:
                report_date = datetime.strptime(date_text[:10], "%Y-%m-%d")
            except Exception:
                report_date = None
            if report_date and report_date >= cutoff:
                count_90d += 1
            latest.append(
                {
                    "date": date_text,
                    "institution": str(row.get("机构", "")),
                    "rating": str(row.get("东财评级", "")),
                    "title": str(row.get("报告名称", "")),
                }
            )
        return {"count_90d": count_90d, "latest": latest[:3]}
    except Exception as exc:
        return {"count_90d": 0, "latest": [], "error": str(exc)}


def fetch_revenue_snapshot(code):
    try:
        df = ak.stock_profit_sheet_by_report_em(symbol=_market_prefix(code))
        if df is None or df.empty:
            return {}
        revenue_col = None
        for col in df.columns:
            if "营业总收入" in str(col) or "营业收入" in str(col):
                revenue_col = col
                break
        date_col = None
        for col in df.columns:
            if "报告日" in str(col) or "日期" in str(col):
                date_col = col
                break
        if revenue_col is None:
            return {}
        row = df.iloc[0]
        return {
            "report_date": str(row.get(date_col, "")) if date_col else "",
            "revenue": _safe_float(row.get(revenue_col, 0)),
        }
    except Exception as exc:
        return {"error": str(exc)}


def analyze_stock(item):
    code = item["code"]
    hist = fetch_hist(code)
    if hist is None or hist.empty or len(hist) < 80:
        raise RuntimeError(f"{code} 历史行情不足")

    hist = hist.copy()
    close = pd.to_numeric(hist["收盘"], errors="coerce")
    high = pd.to_numeric(hist["最高"], errors="coerce")
    volume = pd.to_numeric(hist.get("成交额", 0), errors="coerce")
    turnover = pd.to_numeric(hist.get("换手率", 0), errors="coerce")

    last = _safe_float(close.iloc[-1])
    ma20 = _safe_float(close.tail(20).mean())
    ma60 = _safe_float(close.tail(60).mean())
    ma120 = _safe_float(close.tail(120).mean()) if len(close) >= 120 else ma60
    high_250 = _safe_float(high.tail(250).max())
    low_60 = _safe_float(close.tail(60).min())
    # 【2026-11-06 · ADR-0050】底部反转检测所需的额外序列。
    # ⚠️ 事件判定必须用**已收盘 K 线**：盘中（尤其午休）拉到的今日 bar 只有半日成交额，
    # 量能会被腰斩（实测 12:59 跑出来全是 0.4~0.9×，反转日全部漏判）。
    # 现价/均线等展示指标仍用全量（盘中价也有意义），只有「上穿/放量」用收盘数据。
    _today_str = str(hist["日期"].iloc[-1])[:10] if "日期" in hist.columns else ""
    _now_hm = datetime.now().strftime("%H:%M")
    _last_is_today_unclosed = (_today_str == datetime.now().strftime("%Y-%m-%d") and _now_hm < "15:00")
    _closed = hist.iloc[:-1] if _last_is_today_unclosed else hist
    close_c = pd.to_numeric(_closed["收盘"], errors="coerce")
    amt_c = pd.to_numeric(_closed.get("成交额", 0), errors="coerce")
    ma20c = close_c.rolling(20).mean()
    # 最近一次「收盘上穿 MA20」距今几个交易日（收盘口径；找不到=一直上方/下方）
    days_since_cross20 = None
    cross_idx = None
    for k in range(len(close_c) - 1, 0, -1):
        c_k, m_k = _safe_float(close_c.iloc[k]), _safe_float(ma20c.iloc[k])
        c_p, m_p = _safe_float(close_c.iloc[k - 1]), _safe_float(ma20c.iloc[k - 1])
        if c_k > m_k and c_p <= m_p:
            days_since_cross20 = len(close_c) - 1 - k
            cross_idx = k
            break
    # 放量确认 = 上穿**当天**的成交额 ÷ 上穿前 5 日均额（不是最近一天 —— 上穿后
    # 第二天缩量很常见，不影响「上穿日是否放量」这个事实）
    vol_surge = 0.0
    if cross_idx is not None and cross_idx >= 5:
        amt_cross = _safe_float(amt_c.iloc[cross_idx])
        amt5_before = _safe_float(amt_c.iloc[cross_idx - 5:cross_idx].mean())
        vol_surge = (amt_cross / amt5_before) if amt5_before > 0 else 0.0
    # 上穿日收盘距 120 日低点（收盘口径）
    low_120_c = _safe_float(close_c.tail(120).min()) if len(close_c) >= 120 else _safe_float(close_c.min())
    off_low120 = _pct(_safe_float(close_c.iloc[-1]), low_120_c) if cross_idx is not None else _pct(last, low_120_c)
    ret20 = _pct(last, close.iloc[-21]) if len(close) > 21 else 0.0
    ret60 = _pct(last, close.iloc[-61]) if len(close) > 61 else 0.0
    ret120 = _pct(last, close.iloc[-121]) if len(close) > 121 else 0.0
    dist_ma20 = _pct(last, ma20)
    dist_ma60 = _pct(last, ma60)
    drawdown_high = _pct(last, high_250)
    avg_amount_20 = _safe_float(volume.tail(20).mean()) / 100000000.0
    avg_turnover_20 = _safe_float(turnover.tail(20).mean())

    research = fetch_research_heat(code)
    revenue = fetch_revenue_snapshot(code)

    score = 0
    score += 18 if last > ma20 > ma60 else 8 if last > ma60 else 0
    score += 14 if ma60 > ma120 else 5
    score += min(max(ret60, 0), 60) * 0.45
    score += min(max(ret120, 0), 120) * 0.20
    score += 12 if drawdown_high >= -8 else 7 if drawdown_high >= -15 else 0
    score += 8 if avg_amount_20 >= 10 else 5 if avg_amount_20 >= 3 else 0
    score += min(research.get("count_90d", 0), 6) * 2
    score += 10 if "北美" in item.get("global_binding", "") or "Apple" in item.get("global_binding", "") else 6

    if dist_ma20 > 25:
        score -= 8
    if last < ma60:
        score -= 18
    if ret20 < -12:
        score -= 10

    score = round(max(0, min(score, 100)), 1)

    # 【2026-11-06 · ADR-0050】底部反转启动判定（日线级，权重核心票专用）。
    # 旧逻辑的盲区：last < ma60 一律「失守防线」—— 底部放量上穿 MA20 的那个瞬间
    # （中线最有价值的左侧点）被压在同一个词里，永远不会被提示。
    # 判定五条（全部满足才成立，宁缺勿滥，全部用已收盘数据）：
    #   ① 仍在 60 日线下方（last < ma60）—— 还在深坑里才叫「底部反转」
    #   ② 已站上 MA20 且是近 5 个交易日内上穿的（days_since_cross20 ∈ [0,5]）
    #   ③ 上穿当日成交额 ≥ 前 5 日均额 × 1.25（放量确认，不是无量反弹）
    #   ④ 上穿日收盘距 120 日低点 ≤ 20%（仍在底部区域，不是半山腰反弹）
    #   ⑤ 距 250 日高点 ≤ -20%（跌得够深，反转才有空间）
    reversal_started = bool(
        last < ma60
        and last > ma20
        and days_since_cross20 is not None and days_since_cross20 <= 5
        and vol_surge >= 1.25
        and off_low120 <= 20.0
        and drawdown_high <= -20.0
    )

    if reversal_started:
        status = "反转启动"
        tactic = "底部放量上穿20日线：中线左侧信号。可小仓试探建观察仓；正式趋势仓仍等站上60日线再给。"
    elif last > ma20 > ma60 and ret60 > 15 and drawdown_high >= -12:
        status = "核心趋势"
        tactic = "保留核心观察；急涨远离20日线时只做减T，不轻易清空趋势仓。"
    elif last > ma60 and ret60 > 5:
        status = "趋势观察"
        tactic = "等待20日线附近或放量突破后确认；不追连续加速。"
    elif last < ma60:
        status = "失守防线"
        tactic = "跌破60日线先降级观察；没有重新站回前不做中线加仓。"
    else:
        status = "蓄势观察"
        tactic = "产业逻辑保留，但等价格重新走强。"

    if reversal_started:
        t_signal = "🔥 底部反转启动：放量上穿20日线（距120日低点 %.1f%%，量能 %.2f 倍）。" % (off_low120, vol_surge)
    elif dist_ma20 >= 18:
        t_signal = "偏离20日线过大，适合减T/等待回踩。"
    elif dist_ma20 <= -8 and last > ma60:
        t_signal = "回踩但60日线未破，可观察低吸T点。"
    elif last < ma60:
        t_signal = "趋势防线失守，不做低吸T。"
    else:
        t_signal = "处于趋势带内，少折腾核心仓。"

    return {
        **item,
        "last_price": round(last, 2),
        "score": score,
        "status": status,
        "tactic": tactic,
        "t_signal": t_signal,
        "metrics": {
            "ret20_pct": round(ret20, 2),
            "ret60_pct": round(ret60, 2),
            "ret120_pct": round(ret120, 2),
            "ma20": round(ma20, 2),
            "ma60": round(ma60, 2),
            "ma120": round(ma120, 2),
            "dist_ma20_pct": round(dist_ma20, 2),
            "dist_ma60_pct": round(dist_ma60, 2),
            "drawdown_from_250d_high_pct": round(drawdown_high, 2),
            "low_60": round(low_60, 2),
            "off_low120_pct": round(off_low120, 2),
            "vol_surge_x": round(vol_surge, 2),
            "days_since_cross20": days_since_cross20,
            "avg_amount_20_yi": round(avg_amount_20, 2),
            "avg_turnover_20_pct": round(avg_turnover_20, 2),
        },
        "research": research,
        "revenue_snapshot": revenue,
    }


def build_brief():
    os.makedirs(REPORTS_DIR, exist_ok=True)
    results = []
    errors = []
    universe = build_universe()
    print(f"[Trend Research] 宇宙 {len(universe)} 只"
          f"（种子 {len(SEED_WATCHLIST)} + 工作台自选 trend {len(universe) - len(SEED_WATCHLIST)}）")
    for item in universe:
        try:
            results.append(analyze_stock(item))
            time.sleep(0.25)
        except Exception as exc:
            errors.append({"code": item["code"], "name": item["name"], "error": str(exc)})

    results.sort(key=lambda x: x["score"], reverse=True)

    if not results:
        failure = {
            "last_failed": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "engine": "trend_research_v1_no_llm_required",
            "error": "all seed watchlist fetches failed; preserved previous trend_research_brief files",
            "errors": errors,
        }
        with open(TREND_FAILED_FILE, "w", encoding="utf-8") as f:
            json.dump(failure, f, ensure_ascii=False, indent=2)
        with open(TREND_FAILED_TEXT_FILE, "w", encoding="utf-8") as f:
            f.write("【中线产业趋势研究池刷新失败】\n")
            f.write(f"失败时间: {failure['last_failed']}\n")
            f.write("原因: 所有种子股行情源抓取失败，已保留上一份有效趋势池。\n\n")
            for err in errors:
                f.write(f"- {err['code']} {err['name']}: {err['error']}\n")
        print(f"[Trend Research] All fetches failed; preserved {TREND_FILE}")
        print(f"[Trend Research] Saved failure diagnostics {TREND_FAILED_FILE}")
        raise SystemExit(1)

    by_theme = {}
    for row in results:
        by_theme.setdefault(row["theme"], []).append(row)

    # 【2026-11-06 · ADR-0050】与上一份 brief 对比 → 状态翻转事件。
    # 旧逻辑只输出快照：状态从「失守防线」翻成「趋势观察」时报告里只是悄悄换了个词，
    # 用户根本注意不到 —— 这正是「核心票反转了却没人提示」的第二层原因。
    events = []
    prev_status = {}
    try:
        if os.path.exists(TREND_FILE):
            with open(TREND_FILE, encoding="utf-8") as f:
                _prev = json.load(f)
            for x in (_prev.get("all") or []):
                prev_status[x.get("code")] = x.get("status")
    except Exception as exc:
        print(f"[Trend Research] 读取上一份趋势池失败(跳过事件对比): {exc}")
    # 状态严重度排序：反转启动是事件本身；其余按趋势健康度
    _rank = {"失守防线": 0, "反转启动": 1, "蓄势观察": 2, "趋势观察": 3, "核心趋势": 4}
    for row in results:
        cur, prev = row["status"], prev_status.get(row["code"])
        if prev is None:      # 新入池种子（如 2026-11-06 补入的中芯/华虹/阳光电源）
            if cur == "反转启动":
                events.append({
                    "code": row["code"], "name": row["name"], "theme": row["theme"],
                    "kind": "reversal", "from": "(新入池)", "to": cur,
                    "note": "新入池即处于反转启动状态",
                })
            continue
        if cur == prev:
            continue
        if cur == "反转启动":
            events.append({
                "code": row["code"], "name": row["name"], "theme": row["theme"],
                "kind": "reversal", "from": prev, "to": cur,
                "note": "底部放量上穿20日线",
            })
        elif _rank.get(cur, 0) > _rank.get(prev, 0):
            events.append({
                "code": row["code"], "name": row["name"], "theme": row["theme"],
                "kind": "upgrade", "from": prev, "to": cur,
                "note": "趋势状态上台阶",
            })
        else:
            events.append({
                "code": row["code"], "name": row["name"], "theme": row["theme"],
                "kind": "downgrade", "from": prev, "to": cur,
                "note": "趋势状态回落",
            })

    # 事件持久化：当日事件与日志中已有的当日事件**合并**（按 code+kind 去重，新覆盖旧）。
    # 不能整日替换 —— 否则「13:02 抓到反转事件、13:06 重跑（状态未再变 events=[]）」
    # 会把已经发生的事件抹掉，下一次 pro_report 恰好读到空事件，用户永远看不到。
    today_str = datetime.now().strftime("%Y-%m-%d")
    events_log = []
    try:
        if os.path.exists(TREND_EVENTS_FILE):
            with open(TREND_EVENTS_FILE, encoding="utf-8") as f:
                events_log = json.load(f)
            if not isinstance(events_log, list):
                events_log = []
    except Exception as exc:
        print(f"[Trend Research] 事件日志损坏(重建): {exc}")
        events_log = []

    def _ekey(e):
        return (e.get("code"), e.get("kind"))

    prev_today = next((x for x in events_log
                       if isinstance(x, dict) and x.get("date") == today_str), None)
    merged = list((prev_today or {}).get("events") or [])
    new_keys = {_ekey(e) for e in events}
    merged = [e for e in merged if _ekey(e) not in new_keys] + list(events)
    events_log = [x for x in events_log if isinstance(x, dict) and x.get("date") != today_str]
    if merged:
        events_log.append({"date": today_str, "events": merged})
    events_log = events_log[-TREND_EVENTS_KEEP_DAYS:]
    try:
        with open(TREND_EVENTS_FILE, "w", encoding="utf-8") as f:
            json.dump(events_log, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        print(f"[Trend Research] 事件日志写入失败(不影响主流程): {exc}")
    # 近 3 个交易日的事件（扁平，供报告直接渲染）
    events_recent = [dict(ev, date=d.get("date"))
                     for d in events_log[-3:] for ev in (d.get("events") or [])]

    data = {
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "engine": "trend_research_v1_no_llm_required",
        "purpose": "中线产业趋势池：寻找全球科技巨头/产业资本开支绑定、营收逻辑较硬、价格趋势持续的观察标的。",
        "portfolio_rules": [
            "短线龙头仓和中线趋势仓分账管理，不能用打板纪律随意砍趋势核心，也不能用趋势逻辑死扛短线失败。",
            "趋势仓只在价格站上60日线、20/60日均线结构向上、产业逻辑未证伪时持有或做T。",
            "价格远离20日线18%以上偏减T；回踩20日线附近且不破60日线才考虑低吸T。",
            "单一趋势方向不超过总资产20%-25%，单票不超过8%-12%；系统性杀跌时趋势仓也要降风险。",
        ],
        "top": results[:8],
        "events": events,
        "events_recent": events_recent,
        "events_note": "events=本次扫描相对上一份 brief 的翻转；events_recent=事件日志近 3 日（持久化，重跑不丢）；reversal=底部反转启动",
        "themes": {
            theme: [
                {
                    "code": x["code"],
                    "name": x["name"],
                    "score": x["score"],
                    "status": x["status"],
                    "ret60_pct": x["metrics"]["ret60_pct"],
                    "drawdown_high_pct": x["metrics"]["drawdown_from_250d_high_pct"],
                }
                for x in rows[:5]
            ]
            for theme, rows in sorted(by_theme.items())
        },
        "all": results,
        "errors": errors,
    }

    with open(TREND_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    lines = [
        "【中线产业趋势研究池】",
        f"更新时间: {data['last_updated']}",
        "定位: 只做趋势研究与仓位框架，不等同于即时买入指令。",
        "",
        "组合纪律:",
    ]
    lines.extend([f"  - {rule}" for rule in data["portfolio_rules"]])
    lines.append("")
    if events_recent:
        lines.append("🔥 反转与状态翻转事件（近 3 个交易日，含当日）:")
        for ev in events_recent:
            tag = "🔥反转启动" if ev["kind"] == "reversal" else (
                "↑升级" if ev["kind"] == "upgrade" else "↓回落")
            lines.append(f"  {tag} [{ev.get('date', '')}] {ev['code']} {ev['name']}（{ev['theme']}）："
                         f"{ev['from']} → {ev['to']} · {ev['note']}")
        lines.append("")
    lines.append("Top趋势观察:")
    for idx, row in enumerate(data["top"], 1):
        m = row["metrics"]
        lines.append(
            f"{idx}. {row['code']} {row['name']} | {row['theme']} | 评分 {row['score']} | {row['status']}"
        )
        lines.append(
            f"   绑定: {row['global_binding']} | 60日涨幅 {m['ret60_pct']}% | 距20日线 {m['dist_ma20_pct']}% | 距250日高点 {m['drawdown_from_250d_high_pct']}%"
        )
        lines.append(f"   做T: {row['t_signal']} | 策略: {row['tactic']}")
    if errors:
        lines.append("")
        lines.append("抓取失败:")
        for err in errors:
            lines.append(f"  - {err['code']} {err['name']}: {err['error']}")

    with open(TREND_TEXT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"[Trend Research] Saved {TREND_FILE}")
    print(f"[Trend Research] Saved {TREND_TEXT_FILE}")


if __name__ == "__main__":
    build_brief()

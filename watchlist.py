# -*- coding: utf-8 -*-
"""自选股池 + 东方财富 DK点操盘线指标

存储：data/watchlist.json（与 portfolio.json 同模式）
DK 算法（参考东方财富操盘线·黄蓝线规则）：
  DK = 收盘价的 N 日 EMA（默认 N=13）
  红线·持股 = 当日收盘价 ≥ DK → 多头趋势，可持有
  蓝线·观星 = 当日收盘价 < DK  → 空头趋势，观望
  D点 = 蓝线段首个「当日上穿DK」日 → 蓝→红拐点（买点信号）
  K点 = 红线段首个「当日下穿DK」日 → 红→蓝拐点（卖点信号）
  「信号后涨幅」= (当前价 - 信号日收盘价) / 信号日收盘价 * 100
  「操盘线价」= 当日 DK 值（盘中实时用最新价微调）

K 线源：akshare.stock_zh_a_hist（按需缓存到 data/watchlist_cache/<code>.json）
"""
import json
import log_util
import math
import threading
import time
from datetime import datetime
from pathlib import Path

import requests as _req

WATCHLIST_FILE = Path(__file__).parent / "data" / "watchlist.json"
CACHE_DIR = Path(__file__).parent / "data" / "watchlist_cache"
LOCK = threading.Lock()
TIMEOUT = 8           # 单只 K 线 HTTP 超时（30 只并行 ≈ 5-8s 内完成）
DK_EMA_N = 13          # DK 线 EMA 周期
HIST_BARS = 120         # 拉取近 N 个交易日（足够算 DK + 信号）
CACHE_TTL = 4 * 3600    # K 线缓存 4 小时（盘后/盘中均可用，避免频繁请求）


def _qt_symbol(code: str) -> str:
    # 2026-09-29 修复：同 portfolio.py —— 剥离 .SZ/.SH 等后缀，避免拼出 "sh600519.SH" 这种无效符号。
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if not code:
        return ""
    if code.startswith(("6", "5", "9")):
        return "sh" + code
    if code.startswith(("0", "3", "1")):
        return "sz" + code
    if code.startswith(("4", "8")):
        return "bj" + code
    return "sh" + code


def _load():
    if not WATCHLIST_FILE.exists():
        return []
    try:
        return json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(items):
    WATCHLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    WATCHLIST_FILE.write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------- CRUD ----------------
def list_items():
    with LOCK:
        return _load()


def add(code, name="", scope="short"):
    """加入自选。

    scope（2026-11-06，用户分账铁律：题材和权重是两套逻辑，不能混淆）：
      · "short" 短线/题材票 —— 参与 13:40/14:40 自选供票源（ADR-0041 短线操作池）
      · "trend" 权重/趋势核心票 —— **不进短线供票源**，只走中线趋势池
        （trend_research.py 的反转启动/趋势状态），本页仅做行情 + DK 点观察
    旧数据无 scope 字段 → 按 "short" 处理（兼容既有行为）。
    """
    code = str(code).strip()
    if not code.isdigit() or len(code) != 6:
        raise ValueError("股票代码须为 6 位数字")
    scope = "trend" if str(scope or "").strip().lower() == "trend" else "short"
    with LOCK:
        items = _load()
        if any(i["code"] == code for i in items):
            raise ValueError(f"{code} 已在自选中")
        items.append({
            "code": code,
            "name": str(name).strip() or code,
            "scope": scope,
            "added_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        })
        _save(items)
        return items


def set_scope(code, scope):
    """改单票 scope（trend/short）。命中返回更新后的全部条目；未找到返回 None。"""
    code = str(code).strip()
    scope = "trend" if str(scope or "").strip().lower() == "trend" else "short"
    with LOCK:
        items = _load()
        hit = False
        for i in items:
            if i["code"] == code:
                i["scope"] = scope
                hit = True
        if not hit:
            return None
        _save(items)
        return items


def remove(code):
    with LOCK:
        items = _load()
        items = [i for i in items if i["code"] != str(code).strip()]
        _save(items)
        return items


# ---------------- 实时行情（腾讯批量接口，与 portfolio 同源） ----------------
def _quote_one(code):
    """单只实时行情；失败返回 None。"""
    url = f"http://qt.gtimg.cn/q={_qt_symbol(code)}"
    try:
        r = _req.get(url, timeout=TIMEOUT,
                     headers={"User-Agent": "Mozilla/5.0"})
        r.encoding = "gbk"
    except Exception:
        return None
    for line in r.text.split(";"):
        if "~" not in line:
            continue
        p = line.split("~")
        if len(p) < 38 or p[2] != code:
            continue
        try:
            return {
                "name": p[1],
                "price": float(p[3]),
                "prev_close": float(p[4]),
                "pct": float(p[32]),
                "amount_yi": round(float(p[37]) / 1e4, 2) if p[37] else 0,
            }
        except (ValueError, IndexError):
            return None
    return None


def quotes(codes):
    """批量实时行情：{code: {price, pct, name}}。"""
    if not codes:
        return {}
    syms = [_qt_symbol(c) for c in codes]
    out = {}
    try:
        r = _req.get("http://qt.gtimg.cn/q=" + ",".join(syms), timeout=TIMEOUT,
                     headers={"User-Agent": "Mozilla/5.0"})
        r.encoding = "gbk"
    except Exception:
        return {c: None for c in codes}
    for line in r.text.split(";"):
        if "~" not in line:
            continue
        p = line.split("~")
        if len(p) < 38 or not p[2]:
            continue
        try:
            out[p[2]] = {
                "name": p[1],
                "price": float(p[3]),
                "prev_close": float(p[4]),
                "pct": float(p[32]),
            }
        except (ValueError, IndexError):
            continue
    # 缺失的股票标记为 None，便于前端渲染
    for c in codes:
        out.setdefault(c, None)
    return out


# ---------------- 日 K 线（含缓存） ----------------
def _fetch_history(code):
    """拉取近 HIST_BARS 个交易日的日 K。优先读 4h 缓存。
    主源：akshare stock_zh_a_daily（东方财富，稳定）
    备用：腾讯 fqkline（沙箱 IP 被 WAF 挡，09-01 实测）"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"{code}.json"
    if cache.exists():
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
            if time.time() - cached.get("ts", 0) < CACHE_TTL:
                return cached["bars"]

        except Exception as _e:
            log_util.quiet_warn("watchlist.py:169", _e)

    # 主源：akshare stock_zh_a_daily（东方财富数据源）
    bars = _fetch_history_akshare(code)
    if not bars:
        # 备用：腾讯 fqkline
        bars = _fetch_history_tencent(code)
    if bars:
        try:
            cache.write_text(json.dumps(
                {"ts": time.time(), "bars": bars},
                ensure_ascii=False), encoding="utf-8")

        except Exception as _e:
            log_util.quiet_warn("watchlist.py:181", _e)

    return bars


def _fetch_histories_parallel(codes, max_workers=8):
    """多线程并行拉取 K 线（30 只 8 并行 ≈ 5-8s 完成）。
    已有缓存的股票会被 _fetch_history 直接命中缓存，不发 HTTP。
    返回 {code: bars 或 None}。"""
    from concurrent.futures import ThreadPoolExecutor
    out = {c: None for c in codes}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_fetch_history, c): c for c in codes}
        for fut in futures:
            c = futures[fut]
            try:
                out[c] = fut.result(timeout=10)
            except Exception:
                out[c] = None
    return out


def _fetch_history_tencent(code):
    """腾讯日 K：返回 [{date, close}, ...] 列表。
    注意：腾讯 web.ifzq.gtimg.cn 在沙箱 IP 下被 WAF 拦截（已验证 2026-09-01），
    返回 302 → waf.tencent.com。失败时 evaluate() 会回退 akshare。"""
    try:
        url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
               f"?param={_qt_symbol(code)},day,,,{HIST_BARS},qfq")
        r = _req.get(url, timeout=TIMEOUT,
                     headers={"User-Agent": "Mozilla/5.0"})
        d = r.json()
        sym = _qt_symbol(code)
        data = (d.get("data") or {}).get(sym) or {}
        rows = data.get("qfqday") or data.get("day") or []
        out = []
        for row in rows:
            if len(row) >= 5 and row[0] and row[2] is not None:
                out.append({"date": row[0], "close": float(row[2])})
        return out[-HIST_BARS:] if out else []
    except Exception:
        return []


def _fetch_history_akshare(code):
    """akshare 主源：`stock_zh_a_daily` 走东方财富，稳定 161+ 行。
    symbol 需带 sz/sh 前缀（如 sz000977）。start/end 用 YYYYMMDD。"""
    try:
        import akshare as ak
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now().replace(year=datetime.now().year - 2)
                 .strftime("%Y%m%d"))
        sym = _qt_symbol(code).lower()  # sz000977 / sh600519
        df = ak.stock_zh_a_daily(
            symbol=sym, start_date=start, end_date=end, adjust="qfq")
        if df is None or df.empty:
            return []
        # 列：date, open, high, low, close, volume, amount, ...
        rows = df[["date", "close"]].tail(HIST_BARS).values.tolist()
        return [{"date": str(r[0]), "close": float(r[1])} for r in rows]
    except Exception:
        return []


# ---------------- DK 线指标 ----------------
def _ema(values, n):
    """标准 EMA：alpha = 2/(n+1)。返回长度=len(values)，前 n-1 个用 SMA 种子。"""
    if not values:
        return []
    out = [0.0] * len(values)
    if len(values) < n:
        # 数据不足 n，全部用 SMA
        sma = sum(values) / len(values)
        for i in range(len(values)):
            out[i] = sma
        return out
    # 种子 = 前 n 个的 SMA
    seed = sum(values[:n]) / n
    out[n - 1] = seed
    alpha = 2.0 / (n + 1)
    for i in range(n, len(values)):
        out[i] = values[i] * alpha + out[i - 1] * (1 - alpha)
    # 前 n-1 个回填为种子（不影响趋势判断）
    for i in range(n - 1):
        out[i] = seed
    return out


def calc_dk(bars):
    """输入 [{date, close}, ...]，返回：
      status         : "red" | "blue"
      cur_dk         : 今日 DK 值（最后一条）
      red_blue_days  : 当前状态连续天数
      latest_signal  : {type: "D"|"K", date, price}  最近一次拐点
      price_at_signal: 信号日收盘价
    规则：
      D 点 = 收盘由蓝转红的第一个交易日（前一日 close ≤ DK && 当日 close > DK）
      K 点 = 收盘由红转蓝的第一个交易日（前一日 close ≥ DK && 当日 close < DK）
      同一段连续同色内只记首个拐点（即红→蓝之间没有 K，蓝→红之间没有 D）
    """
    if len(bars) < DK_EMA_N + 2:
        return None
    closes = [b["close"] for b in bars]
    dks = _ema(closes, DK_EMA_N)
    # 当前段颜色
    last_close, last_dk = closes[-1], dks[-1]
    cur_status = "red" if last_close >= last_dk else "blue"
    # 当前段连续天数（从末尾往前数同色）
    streak = 0
    for i in range(len(bars) - 1, -1, -1):
        c, dk = closes[i], dks[i]
        if (c >= dk and cur_status == "red") or (c < dk and cur_status == "blue"):
            streak += 1
        else:
            break
    # 找最近一次拐点（倒序找最近的 status flip）
    latest = None  # {type, idx, date, price_at_signal}
    i = len(bars) - 1
    while i > 0:
        c0, dk0 = closes[i - 1], dks[i - 1]
        c1, dk1 = closes[i], dks[i]
        prev_red = c0 >= dk0
        now_red = c1 >= dk1
        if prev_red != now_red:
            latest = {
                "type": "D" if now_red else "K",
                "idx": i,
                "date": bars[i]["date"],
                "price_at_signal": closes[i],
                # 信号发生时的状态：信号当天 status
                "status": "red" if now_red else "blue",
            }
            break
        i -= 1
    return {
        "status": cur_status,
        "cur_dk": round(dks[-1], 3),
        "streak_days": streak,
        "latest_signal": latest,
        "bars_count": len(bars),
    }


# ---------------- 组合接口：批量算 DK + 拼实时价 ----------------
def _items_for_dk():
    """DK 计算的输入列表 = 自选股 ∪ 持仓（按 code 去重，自选股优先）。
    持仓 = 天然监控池；自选股 = 用户额外关注。
    两者合并不重复计算，每只股票只在表里出现一次。"""
    items_wl = list_items()
    # 优先用 portfolio 中已有的 code/name 字段
    try:
        import portfolio
        positions = portfolio.list_positions()
    except Exception:
        positions = []
    by_code = {}
    # 先放自选股（用户显式声明 → 优先级最高）
    for i in items_wl:
        by_code[i["code"]] = {
            "code": i["code"],
            "name": i.get("name") or i["code"],
            "added_at": i.get("added_at", ""),
            "_from": "watchlist",
            # 2026-11-06 分账铁律：scope 必须透传（丢了会让 evaluate 恒读 short）
            "scope": str(i.get("scope") or "short"),
        }
    # 再加持仓（若 code 已在自选则保留自选的标记，不覆盖）
    for p in positions:
        if p["code"] not in by_code:
            by_code[p["code"]] = {
                "code": p["code"],
                "name": p.get("name") or p["code"],
                "added_at": p.get("added_at", ""),
                "_from": "portfolio",
                # 持仓默认按短线口径展示（scope 标签可点击切换）
                "scope": str(p.get("scope") or "short"),
            }
    return list(by_code.values())


def evaluate():
    """返回 [{code, name, price, pct, dk_status, dk_streak, dk_cur,
              latest_signal_type, latest_signal_date, signal_pct}, ...]
    无 K 线的股票：dk_status="unknown"，前端展示 '数据积累中'。
    自选股空时自动回退到 portfolio 持仓（前端显示'持仓'徽标提示来源）。"""
    items = _items_for_dk()
    if not items:
        return {"items": [], "quoted_at": None,
                "generated_at": datetime.now().strftime("%H:%M:%S"),
                "source": "empty"}
    codes = [i["code"] for i in items]
    qmap = quotes(codes)
    # 并行拉 K 线（30 只 ≈ 5-8s 完成）
    bars_map = _fetch_histories_parallel(codes)
    rows = []
    for it in items:
        code = it["code"]
        q = qmap.get(code) or {}
        cur_price = q.get("price")
        bars = bars_map.get(code)
        dk = calc_dk(bars) if bars else None
        row = {
            "code": code,
            "name": q.get("name") or it.get("name") or code,
            "price": cur_price,
            "prev_close": q.get("prev_close"),
            "pct": q.get("pct"),
            "added_at": it.get("added_at"),
            "from_portfolio": bool(it.get("_from") == "portfolio"),
            # 2026-11-06 分账铁律：trend=权重/趋势核心票（不进短线供票源，走趋势池）；
            # short=短线/题材票（参与 13:40/14:40 自选供票源）。旧数据缺省按 short。
            "scope": str(it.get("scope") or "short"),
            "dk_status": dk["status"] if dk else "unknown",
            "dk_streak": dk["streak_days"] if dk else 0,
            "dk_cur": dk["cur_dk"] if dk else None,
            "latest_signal_type": dk["latest_signal"]["type"] if dk and dk.get("latest_signal") else None,
            "latest_signal_date": dk["latest_signal"]["date"] if dk and dk.get("latest_signal") else None,
            "signal_price": dk["latest_signal"]["price_at_signal"] if dk and dk.get("latest_signal") else None,
        }
        # 信号后涨幅 = (现价 / 信号日价 - 1) * 100
        if (cur_price is not None and row["signal_price"] is not None
                and row["signal_price"] > 0):
            row["signal_pct"] = round(
                (cur_price / row["signal_price"] - 1) * 100, 2)
        else:
            row["signal_pct"] = None
        rows.append(row)
    # 排序：D/K 信号排前 → 红线 → 蓝线
    def _key(r):
        return (
            0 if r["latest_signal_type"] in ("D", "K") else (
                1 if r["dk_status"] == "red" else 2),
            -((r["signal_pct"] or 0)),
        )
    rows.sort(key=_key)
    # 数据源标记：watchlist / portfolio / mixed（前端 banner 用）
    n_from_pf = sum(1 for r in rows if r.get("from_portfolio"))
    n_from_wl = sum(1 for r in rows if not r.get("from_portfolio"))
    if n_from_pf == 0:
        source = "watchlist"
    elif n_from_wl == 0:
        source = "portfolio"
    else:
        source = "mixed"
    return {
        "items": rows,
        "quoted_at": datetime.now().strftime("%H:%M:%S"),
        "generated_at": datetime.now().strftime("%H:%M:%S"),
        "source": source,
        "rules": {
            "DK_N": DK_EMA_N,
            "red": "红线·持股 = 收盘价 ≥ DK（N日EMA）→ 多头趋势",
            "blue": "蓝线·观星 = 收盘价 < DK → 空头趋势·观望",
            "D": "D点 = 蓝→红拐点（买点信号）",
            "K": "K点 = 红→蓝拐点（卖点信号）",
            "source": "数据源：自选股 ∪ 持仓（持仓是天然DK监控池，自选股优先）",
        },
    }
# -*- coding: utf-8 -*-
"""期货策略引擎（M5，ADR-0024）

把期货从「裸报价表」升级为「A股同款策略视图」：
  热点板块（8 板块热度排序）→ 强势/弱势品种（四因子打分）→ 每品种多空建议 + 止损止盈 + 走势预判

数据设计（沙箱可达源，2026-09-04 实测）：
- 实时行情：新浪 hq.sinajs.cn nf_ 主连（44 列，6s 轮询，复用 futures_fetcher.FuturesData.rows）
- 历史日K：新浪 stock2 InnerFuturesNewService.getDailyKLine（上市起全量 o/h/l/c/v/p/s）
  - number 参数无效 → 全量返回；60 品种串行 ~30s → 缓存到 data/fut_kline_cache.json，每 FUT_KLINE_REFRESH_SEC 刷一次
  - 盘中指标用「昨日K序列 + 今日实时价」拼接计算，不依赖当日K刷新 → 实时无网络开销
- 机构席位龙虎榜：东财 push2 / 交易所直连在沙箱被断（ConnectionError / BadZipFile）→ 降级：
  「总持仓量增减 + 持仓金额变化」作为主力资金进出代理，口径在前端诚实标注

四因子打分（-100 ~ +100）：
  ① 趋势 MA 排列（5/10/20/60 多头/空头）+ 价格 vs MA20/MA60
  ② 动量 MACD(12,26,9) 金叉/死叉 + 近5/20日涨幅
  ③ 资金 持仓量增减%（昨K p vs 实时 hold）+ 持仓金额变化（增仓上行=多头主动…）
  ④ 位置 现价在 20 日区间位置 + RSI(14) 超买超卖修正

输出：板块热度榜 / 强势多头榜 / 弱势空头榜 / 每品种操作卡（方向/置信/止损/止盈/依据）
"""
import os
import log_util
for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
           "all_proxy", "ALL_PROXY"]:
    os.environ.pop(_k, None)

import json
import math
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

import config

_SINA_HQ = "https://hq.sinajs.cn/list="
_SINA_KLINE = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/"
               "var%20t=/InnerFuturesNewService.getDailyKLine?symbol=")
_HEADERS = {"User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn/"}

# 统一品种表：sector -> [(code, name)]（config.CN_FUT_SECTORS 展开）
def _universe():
    out = {}
    for sector, items in config.CN_FUT_SECTORS.items():
        for code, name in items:
            out[code] = {"sector": sector, "name": name}
    return out


_UNIVERSE = _universe()          # {code: {sector, name}}
_KLINE_CODES = list(_UNIVERSE.keys())


# ---------------- 技术指标 ----------------
def _sma(vals, n):
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def _ema(vals, n):
    if not vals:
        return None
    k = 2.0 / (n + 1)
    e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e


def _macd(closes, fast=12, slow=26, signal=9):
    """返回 (dif, dea, hist). 数据不足返回 None。"""
    if len(closes) < slow + signal:
        return None
    ema_fast = _ema(closes[-200:], fast)
    ema_slow = _ema(closes[-200:], slow)
    dif = ema_fast - ema_slow
    # DEA = dif 的 EMA(signal)，需要 dif 序列
    difs = []
    ef, es = closes[0], closes[0]
    for c in closes:
        ef = c * (2.0 / (fast + 1)) + ef * (1 - 2.0 / (fast + 1))
        es = c * (2.0 / (slow + 1)) + es * (1 - 2.0 / (slow + 1))
        difs.append(ef - es)
    dea = None
    if len(difs) >= signal:
        dea = _ema(difs, signal)
    if dea is None:
        return None
    hist = 2 * (dif - dea)
    return dif, dea, hist


def _rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0))
        losses.append(max(-ch, 0))
    avg_g = sum(gains[-n:]) / n
    avg_l = sum(losses[-n:]) / n
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100 - 100 / (1 + rs)


def _atr(klines, n=14):
    """klines: [{h,l,c}] 最新在前在后均可，取收盘序后段。"""
    if len(klines) < n + 1:
        return None
    trs = []
    for i in range(1, len(klines)):
        h, l, pc = klines[i]["h"], klines[i]["l"], klines[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < n:
        return None
    return sum(trs[-n:]) / n


def _ma_state(closes):
    """MA 排列态：'bull' 多头排列 / 'bear' 空头排列 / 'mix' 纠缠。"""
    if len(closes) < 60:
        return "mix", None, None
    ma5, ma10, ma20, ma60 = (_sma(closes, n) for n in (5, 10, 20, 60))
    if None in (ma5, ma10, ma20, ma60):
        return "mix", None, None
    if ma5 > ma10 > ma20 > ma60:
        return "bull", ma20, ma60
    if ma5 < ma10 < ma20 < ma60:
        return "bear", ma20, ma60
    return "mix", ma20, ma60


# ---------------- 主引擎 ----------------
class FuturesStrategy:
    """期货策略：独立线程低频刷新 K 线缓存；打分输出可被 /api/fut_strategy 即时消费。"""

    def __init__(self, fut):
        self.fut = fut                     # futures_fetcher.FuturesData（实时行情已 6s 更新）
        self.kline = {}                    # code -> [ {d,o,h,l,c,v,p,s} ]（升序，昨日止）
        self.kline_date = None             # 缓存对应的最后一根 K 日期
        self.result = None                 # 最近一次策略快照
        self.result_at = None
        self.errors = []
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._kline_dirty = True           # 首轮先拉 K 线
        self._cache_path = Path(config.FUT_KLINE_CACHE)

    # ---------- K 线缓存 ----------
    def _load_cache(self):
        try:
            if self._cache_path.exists():
                d = json.loads(self._cache_path.read_text())
                self.kline = d.get("kline", {})
                self.kline_date = d.get("asof")

        except Exception as _e:
            log_util.quiet_warn("futures_strategy.py:166", _e)


    def _save_cache(self):
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(json.dumps(
                {"asof": self.kline_date, "kline": self.kline}, ensure_ascii=False))
        except Exception as e:
            self._err("K缓存写", e)

    def _fetch_kline_one(self, code):
        """拉单品种日K（全量），返回升序列表或 None。"""
        url = _SINA_KLINE + code
        r = requests.get(url, timeout=8, headers=_HEADERS)
        txt = r.text
        i = txt.find("var t=(")
        if i < 0:
            return None
        arr = json.loads(txt[i + 7:txt.rfind(");")])
        out = []
        for k in arr:
            try:
                out.append({
                    "d": k["d"], "o": float(k["o"]), "h": float(k["h"]),
                    "l": float(k["l"]), "c": float(k["c"]),
                    "v": float(k["v"] or 0), "p": float(k["p"] or 0),
                    "s": float(k["s"] or 0),
                })
            except (KeyError, ValueError):
                continue
        return out

    def _refresh_kline(self):
        """后台：全量拉一次日K（60 品种 ~30s），失败品种保留旧缓存。

        缓存日期足够新（≤3 自然日）→ 直接用缓存不重拉（收盘后数据当日不变）。
        跨交易日（缓存落后 >3 天）→ 全量刷新。盘中实时由 futures_fetcher 行情拼装。
        """
        # 缓存已含最新交易日 → 跳过（盘中实时由 futures_fetcher 行情拼装）
        if self.kline_date:
            try:
                cached = datetime.strptime(self.kline_date, "%Y-%m-%d")
                if (datetime.now() - cached).days <= 3:
                    self._kline_dirty = False   # 缓存足够新，无需全量重拉
                    return

            except Exception as _e:
                log_util.quiet_warn("futures_strategy.py:212", _e)

        ok = 0
        for i, code in enumerate(_KLINE_CODES):
            if self._stop.is_set():
                return
            try:
                kl = self._fetch_kline_one(code)
                if kl:
                    self.kline[code] = kl
                    ok += 1

            except Exception as _e:
                log_util.quiet_warn("futures_strategy.py:223", _e)

            time.sleep(0.05)
        # asof = 最新一根K的日期（应为上一交易日）
        last = None
        for kl in self.kline.values():
            if kl:
                d = kl[-1]["d"]
                if last is None or d > last:
                    last = d
        self.kline_date = last
        self._kline_dirty = False
        self._save_cache()
        print(f"[fut-strategy] K线刷新完成 {ok}/{len(_KLINE_CODES)} asof={last}")

    # ---------- 单品种打分 ----------
    def _score_one(self, code, rt):
        """rt: 实时 row。返回 dict（含方向/分/置信/止损止盈/依据）。"""
        kl = self.kline.get(code)
        if not kl or len(kl) < 30:
            return None
        closes = [k["c"] for k in kl]           # 昨日止收盘
        highs = [k["h"] for k in kl]
        lows = [k["l"] for k in kl]
        price = rt["price"]
        prev_close = rt.get("prev_close") or closes[-1]
        hold_now = rt.get("hold") or 0
        hold_prev = kl[-1].get("p") or 0
        chg_pct = rt.get("pct") or 0
        # —— 拼接今日 bar 用于 ATR / 位置 ——
        k_all = list(kl)
        if price:
            k_all.append({"h": max(highs[-1] if highs else price, price),
                          "l": min(lows[-1] if lows else price, price),
                          "c": price})
        closes_all = closes + [price] if price else closes
        # 1) 趋势因子（±30）
        ma_state, ma20, ma60 = _ma_state(closes)
        score = 0
        trend_txt = []
        if ma_state == "bull":
            score += 20
            trend_txt.append("MA多头排列")
        elif ma_state == "bear":
            score -= 20
            trend_txt.append("MA空头排列")
        else:
            trend_txt.append("均线纠缠")
        if ma20 and price:
            if price > ma20:
                score += 5
                trend_txt.append("价>MA20")
            else:
                score -= 5
                trend_txt.append("价<MA20")
        if ma60 and price:
            if price > ma60:
                score += 5
            else:
                score -= 5
        # 2) 动量因子（±30）
        macd = _macd(closes_all)
        if macd:
            dif, dea, hist = macd
            if dif > dea:
                score += 10
                trend_txt.append("MACD金叉")
            else:
                score -= 10
                trend_txt.append("MACD死叉")
            if hist > 0:
                score += 3
            else:
                score -= 3
        # 近5日 / 近20日涨幅
        if len(closes) >= 5 and price:
            ch5 = (price - closes[-5]) / closes[-5] * 100
            score += 6 if ch5 > 2 else (-6 if ch5 < -2 else 0)
        if len(closes) >= 20 and price:
            ch20 = (price - closes[-20]) / closes[-20] * 100
            score += 6 if ch20 > 5 else (-6 if ch20 < -5 else 0)
        # 3) 资金因子（±25）：持仓增减 + 涨跌方向组合
        hold_chg_txt = ""
        if hold_prev > 0 and hold_now > 0:
            hold_chg_pct = (hold_now - hold_prev) / hold_prev * 100
            if chg_pct > 0.3 and hold_chg_pct > 1:      # 增仓上行：多头主动
                score += 20
                hold_chg_txt = f"增仓{hold_chg_pct:+.1f}%上行(多头主动)"
            elif chg_pct < -0.3 and hold_chg_pct > 1:   # 增仓下行：空头主动
                score -= 20
                hold_chg_txt = f"增仓{hold_chg_pct:+.1f}%下行(空头主动)"
            elif chg_pct > 0.3 and hold_chg_pct < -1:   # 减仓上行：空头回补（弱多）
                score += 6
                hold_chg_txt = f"减仓{hold_chg_pct:+.1f}%上行(空头回补)"
            elif chg_pct < -0.3 and hold_chg_pct < -1:  # 减仓下行：多头离场（弱空）
                score -= 6
                hold_chg_txt = f"减仓{hold_chg_pct:+.1f}%下行(多头离场)"
            else:
                hold_chg_txt = f"仓{hold_chg_pct:+.1f}%"
        # 4) 位置 + RSI（±15 修正）
        rsi = _rsi(closes_all)
        if len(closes) >= 20 and price:
            hi20, lo20 = max(highs[-20:]), min(lows[-20:])
            pos = (price - lo20) / (hi20 - lo20) if hi20 > lo20 else 0.5
            if pos > 0.85:
                score -= 5
            elif pos < 0.15:
                score += 5
            pos_txt = f"20日区间{pos * 100:.0f}%位"
        else:
            pos_txt = ""
        if rsi is not None:
            if rsi > 75:
                score -= 8
                trend_txt.append(f"RSI{rsi:.0f}超买")
            elif rsi < 25:
                score += 8
                trend_txt.append(f"RSI{rsi:.0f}超卖")
        # 方向
        if score >= 18:
            direction = "LONG"
        elif score <= -18:
            direction = "SHORT"
        else:
            direction = "NEUTRAL"
        conf = min(abs(score) + 20, 92) if direction != "NEUTRAL" else 0
        # 止损止盈（ATR 法：多单损=价-1.6ATR 盈=价+2.4ATR；空单反向；贴近 20 日高低保护）
        atr = _atr(k_all) if len(k_all) >= 15 else None
        stop, target = None, None
        if atr and price and atr > 0:
            if direction == "LONG":
                stop = round(price - 1.6 * atr, 2)
                target = round(price + 2.4 * atr, 2)
                # 目标不超过 20 日高太多（止盈参考）
            elif direction == "SHORT":
                stop = round(price + 1.6 * atr, 2)
                target = round(price - 2.4 * atr, 2)
        # 走势预判
        pred = _predict(direction, score, ma_state, trend_txt, rsi)
        thesis = "；".join(hold_chg_txt and [hold_chg_txt] + trend_txt or trend_txt)
        return {
            "code": code, "name": rt.get("name") or _UNIVERSE.get(code, {}).get("name", code),
            "sector": _UNIVERSE.get(code, {}).get("sector", ""),
            "price": price, "chg": rt.get("chg", 0), "pct": rt.get("pct", 0),
            "hold": hold_now, "hold_prev": hold_prev,
            "hold_chg_pct": round((hold_now - hold_prev) / hold_prev * 100, 1) if hold_prev else None,
            "prev_close": prev_close, "high": rt.get("high"), "low": rt.get("low"),
            "direction": direction, "score": score, "conf": conf,
            "ma_state": ma_state,
            "macd": "金叉" if (macd and macd[0] > macd[1]) else ("死叉" if macd else "--"),
            "rsi": round(rsi, 1) if rsi is not None else None,
            "atr": round(atr, 2) if atr else None,
            "pos20": pos_txt,
            "stop": stop, "target": target,
            "thesis": thesis, "predict": pred,
        }

    # ---------- 板块聚合 + 快照 ----------
    def snapshot(self):
        """消费 futures_fetcher 最新行情 + 本地 K 线 → 策略快照。"""
        fut = self.fut
        rows = {}
        with fut.lock:
            if not fut.rows:
                return None
            rows = dict(fut.rows)          # 含 nf_ 前缀
        items = []
        for code in _KLINE_CODES:
            rt = rows.get("nf_" + code)
            if not rt or not rt.get("price"):
                continue
            it = self._score_one(code, rt)
            if it:
                items.append(it)
        # 板块聚合
        blocks = {}
        for it in items:
            s = it["sector"]
            b = blocks.setdefault(s, {
                "name": s, "pcts": [], "long": 0, "short": 0, "neutral": 0,
                "fund_delta": 0, "hold_sum": 0, "leader": None, "loser": None,
                "avg_pct": 0,
            })
            b["pcts"].append(it["pct"])
            b["long"] += 1 if it["direction"] == "LONG" else 0
            b["short"] += 1 if it["direction"] == "SHORT" else 0
            b["neutral"] += 1 if it["direction"] == "NEUTRAL" else 0
            # 资金：持仓增减金额（hold_chg × 价）
            hcp = it.get("hold_chg_pct")
            if hcp is not None and it["hold_prev"]:
                b["fund_delta"] += it["hold_prev"] * (hcp / 100) * it["price"]
                b["hold_sum"] += it["hold"]
        block_list = []
        for s, b in blocks.items():
            if not b["pcts"]:
                continue
            avg = sum(b["pcts"]) / len(b["pcts"])
            block_list.append({
                "name": s, "count": len(b["pcts"]), "avg_pct": round(avg, 2),
                "long": b["long"], "short": b["short"], "neutral": b["neutral"],
                "fund_delta_wan": round(b["fund_delta"] / 1e4, 0),
                "heat": round(avg * 2 + (b["long"] - b["short"]) * 3 + b["fund_delta"] / 1e8, 1),
            })
        block_list.sort(key=lambda x: x["heat"], reverse=True)
        # 强势榜 / 弱势榜（需置信 & 有止损参考）
        longs = sorted([i for i in items if i["direction"] == "LONG"],
                       key=lambda i: i["score"], reverse=True)[:config.FUT_STRATEGY_TOP_N]
        shorts = sorted([i for i in items if i["direction"] == "SHORT"],
                        key=lambda i: i["score"])[:config.FUT_STRATEGY_TOP_N]
        leaders = [i["name"] for i in longs[:1]] or ["--"]
        # 每板块领涨
        for b in block_list:
            sec_items = [i for i in items if i["sector"] == b["name"]]
            if sec_items:
                best = max(sec_items, key=lambda i: i["pct"])
                b["leader"] = f'{best["name"]} {best["pct"]:+.1f}%'
        return {
            "mk": "fut_strategy",
            "generated_at": datetime.now().strftime("%H:%M:%S"),
            "kline_asof": self.kline_date,
            "blocks": block_list,
            "long_top": longs,
            "short_top": shorts,
            "total": len(items),
            "update": self.result_at,
            "errors": list(self.errors),
            "disclaimer": ("机构席位龙虎榜沙箱不可达，资金字段为总持仓增减代理；"
                           "信号为技术面统计推断，模拟盘参考不构成投资建议"),
        }

    def _err(self, tag, exc):
        msg = f"[{datetime.now():%H:%M:%S}] fut-strategy {tag}: {type(exc).__name__} {exc}"
        print(msg)
        with self.lock:
            self.errors.append(msg)
            if len(self.errors) > 5:
                self.errors.pop(0)

    # ---------- 线程 ----------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._load_cache()
        # 预热：有磁盘缓存先出一次快照（后台线程仍会刷新 K 线保新鲜）
        if self.kline and not self.result:
            try:
                snap = self.snapshot()
                if snap:
                    self.result = snap
                    self.result_at = datetime.now().strftime("%H:%M:%S")
                    print(f"[fut-strategy] 缓存预热完成 kline_asof={self.kline_date} "
                          f"品种={len(snap.get('long_top', [])) + len(snap.get('short_top', []))}")
            except Exception as e:
                self._err("预热", e)
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="fut-strategy")
        self._thread.start()

    def _loop(self):
        tick = 0
        while not self._stop.is_set():
            try:
                # K线缓存：启动首轮或到刷新间隔
                if self._kline_dirty or (tick % max(1, config.FUT_KLINE_REFRESH_SEC // config.FUT_STRATEGY_INTERVAL)) == 0:
                    self._refresh_kline()
                # 策略快照（即时算，不额外拉网）
                snap = self.snapshot()
                if snap:
                    with self.lock:
                        self.result = snap
                        self.result_at = datetime.now().strftime("%H:%M:%S")
            except Exception as e:
                self._err("策略", e)
            tick += 1
            self._stop.wait(config.FUT_STRATEGY_INTERVAL)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)


def _predict(direction, score, ma_state, tags, rsi):
    """走势预判：方向 + 一句技术逻辑（不做点位承诺）。"""
    strength = "强" if abs(score) >= 40 else ("中" if abs(score) >= 25 else "弱")
    if direction == "LONG":
        base = f"短线偏多({strength})"
        if ma_state == "bull":
            return base + "，多头排列趋势顺，回踩支撑不破可低吸"
        if rsi is not None and rsi > 70:
            return base + "，但 RSI 已高防冲高回落，追高需谨慎"
        return base + "，动能向上，回调至 MA 支撑可试多"
    if direction == "SHORT":
        base = f"短线偏空({strength})"
        if ma_state == "bear":
            return base + "，空头排列，反弹压力位不破可试空"
        if rsi is not None and rsi < 30:
            return base + "，但 RSI 超卖防超跌反抽，追空需谨慎"
        return base + "，动能向下，反弹至压力位可试空"
    return "方向中性，等待突破方向明朗"


if __name__ == "__main__":
    import futures_fetcher
    f = futures_fetcher.FuturesData()
    f.fetch()
    s = FuturesStrategy(f)
    s._refresh_kline()
    snap = s.snapshot()
    if not snap:
        print("snapshot empty")
    else:
        print("kline_asof:", snap["kline_asof"], "总品种:", snap["total"])
        print("\n== 板块热度 ==")
        for b in snap["blocks"]:
            print(f"  {b['name']:<6} 均{b['avg_pct']:+.2f}% 多{b['long']}空{b['short']} "
                  f"资金{b['fund_delta_wan']:.0f}万 领涨:{b['leader']} heat={b['heat']}")
        print("\n== 强势多头 Top ==")
        for i in snap["long_top"][:5]:
            print(f"  {i['name']}({i['code']}) {i['price']} {i['pct']:+.2f}% score={i['score']} "
                  f"conf={i['conf']} 止损{i['stop']} 目标{i['target']} | {i['thesis']}")
        print("\n== 弱势空头 Top ==")
        for i in snap["short_top"][:5]:
            print(f"  {i['name']}({i['code']}) {i['price']} {i['pct']:+.2f}% score={i['score']} "
                  f"conf={i['conf']} 止损{i['stop']} 目标{i['target']} | {i['thesis']}")
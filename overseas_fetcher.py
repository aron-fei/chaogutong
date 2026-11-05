# -*- coding: utf-8 -*-
"""多市场行情抓取（M1：港股；M2：美股隔夜；M3 期货/外盘）

设计（ADR-0014 / ADR-0015）：
- 独立线程 + 内存缓存 + 错误隔离，绝不阻塞 A股主流程（data_fetcher.MarketData）
- M1 港股 / M2 美股数据源 = 腾讯 qt（hk/us 前缀，与 A股同网关，2026-09-03 实测）
- 美股免费源盘中约 15 分钟延迟，行情参考用途（隔夜视图为主）
- market_state(mk)：按北京时判定各市场交易状态，供前端 MarketShell 外壳显示

腾讯 qt 字段位（港股/美股同一布局，实测：港股 78 字段 / 美股 71 字段）：
  [1]=名称 [2]=市场代码 [3]=现价 [4]=昨收 [5]=今开 [30]=时间戳(美股为美东日期时间)
  [31]=涨跌 [32]=涨跌幅% [33]=最高 [34]=最低 [36]=成交量 [37]=成交额(元/美元)
  指数行也可能带 [37]，须按「是否个股」判断后才解析成交量/额。
"""
import os
for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
           "all_proxy", "ALL_PROXY"]:
    os.environ.pop(_k, None)

import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

import config

_QT = "https://qt.gtimg.cn/q="
_HEADERS = {"User-Agent": "Mozilla/5.0"}
_MAX_ERR = 5

# 美东/北京时区（美股时段判定，DST 自适应）
_ET = ZoneInfo("America/New_York")
_BJ = ZoneInfo("Asia/Shanghai")


def _qt_text(syms):
    """批量请求腾讯 qt（sym 已含前缀，如 hk00700 / usAAPL / hkHSI）。"""
    r = requests.get(_QT + ",".join(syms), timeout=5, headers=_HEADERS)
    r.encoding = "gbk"
    return r.text


def _to_et(now):
    """北京时（本机 naive 视为北京）→ 美东。"""
    if now.tzinfo is None:
        now = now.replace(tzinfo=_BJ)
    return now.astimezone(_ET)


def _bj_clock(et_dt, hh, mm):
    """美东 et_dt 当天 hh:mm 换算成北京钟面文本，跨天加「次日」。"""
    dt = datetime(et_dt.year, et_dt.month, et_dt.day, hh, mm, tzinfo=_ET)
    bj = dt.astimezone(_BJ)
    tag = "次日" if bj.date() > et_dt.date() else ""
    return tag + bj.strftime("%H:%M")


def _us_state(now):
    """美股时段（美东判定 + 夏令时自适应），标签换算京时。
    免费源盘中约 15 分钟延迟（前端在 trading 态自动灰标提示）。"""
    et = _to_et(now)
    if et.weekday() >= 5:
        return {"text": "周末休市（美东）", "cls": "closed"}
    hm = et.hour * 60 + et.minute
    if 9 * 60 + 30 <= hm < 16 * 60:
        return {"text": f"盘中交易 · 京时{_bj_clock(et, 9, 30)}~{_bj_clock(et, 16, 0)}",
                "cls": "trading"}
    if 16 * 60 <= hm < 20 * 60:
        return {"text": "盘后交易（美东16:00-20:00）", "cls": "closed"}
    if 4 * 60 <= hm < 9 * 60 + 30:
        return {"text": "盘前时段（美东04:00-09:30）", "cls": "bid"}
    return {"text": "已收盘 · 隔夜数据", "cls": "closed"}


def market_state(mk, now=None):
    """按北京时（本机时区即北京时间）判定市场交易状态。
    返回 {"text": str, "cls": "trading"|"closed"|"bid"}，供前端壳与状态点用。
    M1 港股；M2 美股（美东判定、DST 自适应、京时换算）；期货待 M3。
    """
    now = now or datetime.now()
    wd = now.weekday()
    hm = now.hour * 60 + now.minute
    if mk == "hk":
        if wd >= 5:
            return {"text": "周末休市", "cls": "closed"}
        if 9 * 60 <= hm < 9 * 60 + 30:
            return {"text": "竞价时段 09:00-09:30", "cls": "bid"}
        if 9 * 60 + 30 <= hm < 12 * 60:
            return {"text": "早市交易中 09:30-12:00", "cls": "trading"}
        if 12 * 60 <= hm < 13 * 60:
            return {"text": "午间休市 12:00-13:00", "cls": "closed"}
        if 13 * 60 <= hm < 16 * 60:
            return {"text": "午市交易中 13:00-16:00", "cls": "trading"}
        return {"text": "已收盘", "cls": "closed"}
    if mk == "us":
        return _us_state(now)
    return {"text": "待接入（M3 期货/外盘）", "cls": "closed"}


def _parse_qt_lines(text, want_keys, stock_keys, prefix):
    """解析 qt 多行返回 → {key: row}。key 为去掉市场前缀后的代码（如 00700 / AAPL）。
    stock_keys：作为个股解析成交量/额的 key 集合（指数行即使带 [37] 也不解析）。
    港股/美股同一字段布局（见模块 docstring）。"""
    out = {}
    for line in text.split(";"):
        if "~" not in line or "=" not in line:
            continue
        var, payload = line.split("=", 1)
        sym = var.replace("v_", "").strip()
        key = sym[len(prefix):] if sym.lower().startswith(prefix) else sym
        if key not in want_keys:
            continue
        p = payload.split("~")
        if len(p) < 40:
            continue
        try:
            price = float(p[3])
            prev = float(p[4])
            row = {
                "name": p[1],
                "code": key,
                "price": price,
                "prev_close": prev,
                "open": float(p[5]) if p[5] else price,
                "chg": float(p[31]) if p[31] else 0.0,
                "pct": float(p[32]) if p[32] else 0.0,
                "high": float(p[33]) if p[33] else price,
                "low": float(p[34]) if p[34] else price,
                "time": p[30] if len(p) > 30 else "",
            }
            if key in stock_keys and len(p) > 37 and p[37]:
                row["volume"] = float(p[36]) if p[36] else 0.0
                row["amount_yi"] = round(float(p[37]) / 1e8, 2)
            out[key] = row
        except (ValueError, IndexError):
            continue
    return out


class HkData:
    """港股行情：后台线程轮询腾讯 qt（指数 + 热点池），内存缓存。"""

    def __init__(self):
        self.lock = threading.RLock()
        self.indices = []
        self.pool = []            # 热点池（含涨跌/成交额）
        self.pool_at = None       # 榜单快照时间（"HH:MM:SS"）
        self.errors = []
        self._stop = threading.Event()
        self._thread = None

    # ---------------- 工具 ----------------
    def _err(self, tag, exc):
        msg = f"[{datetime.now():%H:%M:%S}] {tag}: {type(exc).__name__} {exc}"
        print(msg)
        with self.lock:
            self.errors.append(msg)
            if len(self.errors) > _MAX_ERR:
                self.errors.pop(0)

    def _parse_quote_lines(self, text, want_keys):
        """港股解析委托共享函数；个股 = 数字代码（5 位）。"""
        return _parse_qt_lines(text, want_keys,
                               stock_keys={k for k in want_keys
                                           if str(k).isdigit()},
                               prefix="hk")

    # ---------------- 拉取 ----------------
    def fetch(self):
        # config 里指数代码形如 "hkHSI"（带前缀），池代码为纯 5 位数字
        idx_codes = [c[2:] if c.lower().startswith("hk") else c
                     for c, _ in config.HK_INDICES]
        want = set(idx_codes + config.HK_HOT)
        text = _qt_text(["hk" + k for k in want])
        rows = self._parse_quote_lines(text, want)

        indices = []
        for code, name in config.HK_INDICES:
            key = code[2:] if code.lower().startswith("hk") else code
            r = rows.get(key)
            if not r:
                continue
            indices.append({
                "code": r["code"], "name": r["name"] or name,
                "price": r["price"], "chg": r["chg"], "pct": r["pct"],
                "high": r["high"], "low": r["low"],
            })

        pool = []
        for code in config.HK_HOT:
            r = rows.get(code)
            if not r:
                continue
            pool.append({
                "code": r["code"], "name": r["name"],
                "price": r["price"], "chg": r["chg"], "pct": r["pct"],
                "amount_yi": r.get("amount_yi", 0.0),
            })
        pool.sort(key=lambda x: x["pct"], reverse=True)
        pool = pool[: config.HK_POOL_TOP]
        pool_at = datetime.now().strftime("%H:%M:%S")

        with self.lock:
            if indices:
                self.indices = indices
            if pool:
                self.pool = pool
                self.pool_at = pool_at

    # ---------------- 快照 ----------------
    def snapshot(self):
        with self.lock:
            pool = list(self.pool)
            indices = list(self.indices)
            amount_top = sorted(pool, key=lambda x: x["amount_yi"], reverse=True)[:12]
            return {
                "mk": "hk",
                "state": market_state("hk"),
                "indices": indices,
                "pool": pool,            # 已按涨跌幅降序
                "amount_top": amount_top,  # 已按成交额降序
                "update": self.pool_at,
                "errors": list(self.errors),
                "now": datetime.now().strftime("%H:%M:%S"),
            }

    # ---------------- 线程 ----------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="hk-data")
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.fetch()
            except Exception as e:
                self._err("港股", e)
            time.sleep(config.HK_INTERVAL)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)


class UsData:
    """美股隔夜行情（M2，ADR-0015）：后台线程轮询腾讯 qt（指数 + 科技 + 中概）。
    免费源盘中约 15 分钟延迟，行情参考用途。API 与 HkData.snapshot() 对齐。"""

    def __init__(self):
        self.lock = threading.RLock()
        self.indices = []
        self.pool = []           # 科技/热门观察池（按涨跌幅降序）
        self.pool_cn = []        # 中概观察池（按涨跌幅降序）
        self.amount_top = []     # 两池合并成交额 Top12
        self.last_quote = ""     # 行情截至（美东 MM-DD HH:MM，取自指数行时间戳）
        self.pool_at = None      # 榜单快照时间（"HH:MM:SS"，北京时）
        self.errors = []
        self._stop = threading.Event()
        self._thread = None

    def _err(self, tag, exc):
        msg = f"[{datetime.now():%H:%M:%S}] {tag}: {type(exc).__name__} {exc}"
        print(msg)
        with self.lock:
            self.errors.append(msg)
            if len(self.errors) > _MAX_ERR:
                self.errors.pop(0)

    def fetch(self):
        # config 指数代码形如 "usDJI"（带前缀），观察池为裸代码
        idx_codes = [c[2:] if c.lower().startswith("us") else c
                     for c, _ in config.US_INDICES]
        tech = [c[2:] if c.lower().startswith("us") else c
                for c in config.US_TECH]
        cn = [c[2:] if c.lower().startswith("us") else c
              for c in config.US_CN]
        want = set(idx_codes + tech + cn)
        text = _qt_text(["us" + k for k in want])
        # 个股 = 科技 + 中概（字母代码；不能像港股那样用 isdigit 判断）
        rows = _parse_qt_lines(text, want,
                               stock_keys=set(tech + cn), prefix="us")

        indices = []
        last_quote = ""
        for code, name in config.US_INDICES:
            key = code[2:] if code.lower().startswith("us") else code
            r = rows.get(key)
            if not r:
                continue
            indices.append({
                "code": r["code"], "name": r["name"] or name,
                "price": r["price"], "chg": r["chg"], "pct": r["pct"],
                "high": r["high"], "low": r["low"],
            })
            t = r.get("time", "")
            if len(t) >= 16:
                last_quote = last_quote or t[5:16]   # MM-DD HH:MM（美东）

        def _sorted(codes):
            out = []
            for c in codes:
                r = rows.get(c)
                if not r:
                    continue
                out.append({"code": r["code"], "name": r["name"],
                            "price": r["price"], "chg": r["chg"],
                            "pct": r["pct"],
                            "amount_yi": r.get("amount_yi", 0.0)})
            out.sort(key=lambda x: x["pct"], reverse=True)
            return out

        pool = _sorted(tech)[: config.US_POOL_TOP]
        pool_cn = _sorted(cn)
        amount_top = sorted(pool + pool_cn,
                            key=lambda x: x["amount_yi"], reverse=True)[:12]
        pool_at = datetime.now().strftime("%H:%M:%S")

        with self.lock:
            if indices:
                self.indices = indices
            if pool:
                self.pool = pool
                self.pool_cn = pool_cn
                self.amount_top = amount_top
                self.last_quote = last_quote
                self.pool_at = pool_at

    def snapshot(self):
        with self.lock:
            return {
                "mk": "us",
                "state": market_state("us"),
                "indices": list(self.indices),
                "pool": list(self.pool),
                "pool_cn": list(self.pool_cn),
                "amount_top": list(self.amount_top),
                "quote": self.last_quote,
                "update": self.pool_at,
                "errors": list(self.errors),
                "now": datetime.now().strftime("%H:%M:%S"),
            }

    # ---------------- 线程 ----------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="us-data")
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.fetch()
            except Exception as e:
                self._err("美股", e)
            time.sleep(config.US_INTERVAL)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)


if __name__ == "__main__":
    h = HkData()
    h.fetch()
    snap = h.snapshot()
    print("state:", snap["state"])
    print("indices:", len(snap["indices"]), "pool:", len(snap["pool"]))
    for i in snap["indices"]:
        print("  idx", i["name"], i["price"], i["pct"])
    for p in snap["pool"][:5]:
        print("  hot", p["name"], p["price"], p["pct"], p["amount_yi"], "亿")

    u = UsData()
    u.fetch()
    us = u.snapshot()
    print("us state:", us["state"])
    print("us indices:", len(us["indices"]), "pool:", len(us["pool"]),
          "cn:", len(us["pool_cn"]), "quote:", us["quote"])
    for i in us["indices"]:
        print("  us-idx", i["name"], i["price"], i["pct"])
    for p in us["pool"][:5]:
        print("  us-hot", p["name"], p["price"], p["pct"], p["amount_yi"], "亿")
    for p in us["pool_cn"][:5]:
        print("  us-cn", p["name"], p["price"], p["pct"], p["amount_yi"], "亿")

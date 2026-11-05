# -*- coding: utf-8 -*-
"""期货行情抓取（M3：国内期货主连 + 外盘商品）

设计（ADR-0016）：
- 单线程 6s 轮询新浪 hq.sinajs.cn（沙箱下需 Referer: finance.sina.com.cn，已实测）
- 三类行情格式差异大：国内商品主连 44 列 / 国内股指主连 50 列 / 外盘商品 15 列
  → 模块级 `_parse_sina_futures()` 按列数自适应，统一到 {symbol: row} dict
- 单类聚合快照：indices / main_board / sectors / night / overseas 5 个视图，行情族可复用
- market_state("fut") 国内 + market_state("ov_fut") 外盘，按北京时独立判定（外盘近 24h）
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

_SINA = "https://hq.sinajs.cn/list="
_HEADERS = {"User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn/"}
_BJ = ZoneInfo("Asia/Shanghai")
_ET = ZoneInfo("America/New_York")
_MAX_ERR = 5


# ---------------- 数据源拉取 ----------------
def _sina_csv(syms):
    r = requests.get(_SINA + ",".join(syms), timeout=6, headers=_HEADERS)
    r.encoding = "gb18030"
    return r.text


def _to_et(now):
    if now.tzinfo is None:
        now = now.replace(tzinfo=_BJ)
    return now.astimezone(_ET)


def _bj_clock(et_dt, hh, mm):
    dt = datetime(et_dt.year, et_dt.month, et_dt.day, hh, mm, tzinfo=_ET)
    bj = dt.astimezone(_BJ)
    tag = "次日" if bj.date() > et_dt.date() else ""
    return tag + bj.strftime("%H:%M")


# ---------------- 状态机 ----------------
def _fut_domestic_state(now):
    """国内期货时段：日盘集合竞价/日盘/午休/夜盘/已收盘/周末。M3 简化版（不区分品种）。"""
    wd = now.weekday()
    hm = now.hour * 60 + now.minute
    if wd >= 5:
        return {"text": "周末休市", "cls": "closed"}
    if 8 * 60 + 55 <= hm < 9 * 60:
        return {"text": "日盘集合竞价 08:55-09:00", "cls": "bid"}
    if (9 * 60 <= hm < 10 * 60 + 15) or (10 * 60 + 30 <= hm < 11 * 60 + 30) or (13 * 60 + 30 <= hm < 15 * 60):
        return {"text": "日盘交易中", "cls": "trading"}
    if 11 * 60 + 30 <= hm < 13 * 60 + 30:
        return {"text": "午间休市 11:30-13:30", "cls": "closed"}
    if 21 * 60 <= hm or hm < 2 * 60 + 30:
        return {"text": "夜盘交易中 21:00-次日02:30", "cls": "trading"}
    return {"text": "已收盘", "cls": "closed"}


def _overseas_state(now):
    """外盘商品（黄金/白银/原油/美铜）：工作日近 24h；周末休市（粗略）。"""
    wd = now.weekday()
    if wd >= 5:
        return {"text": "周末休市（外盘）", "cls": "closed"}
    et = _to_et(now)
    hm = et.hour * 60 + et.minute
    if 17 * 60 <= hm < 18 * 60:
        return {"text": "外盘每日维护 17:00-18:00（美东）", "cls": "closed"}
    return {"text": "交易中（外盘近 24h）", "cls": "trading"}


# ---------------- 解析器 ----------------
def _parse_sina_futures(text):
    """解析新浪期货 CSV 返回。
    50 列 = 股指主连；44 列 = 商品主连；15 列 = 外盘商品。
    统一为 row dict：name / code / price / open / high / low / prev_close / chg / pct /
                   hold / volume / time / date / category / market
    """
    out = {}
    for line in text.split(";"):
        if "=" not in line:
            continue
        var, payload = line.split("=", 1)
        sym = var.replace("var hq_str_", "").strip()
        payload = payload.strip().strip('"').strip()
        if not payload:
            continue
        cols = payload.split(",")
        n = len(cols)
        try:
            if n >= 50:  # 股指主连（实测 IF0/IH0/IC0/IM0 共 50 列）
                # sina nf_IF0 字段序（2026-09-03 实证 + WebSearch 验证）：
                #   cols[0]=开盘  [1]=最高  [2]=最低  [3]=最新价  [4]=持仓量
                #   [5]=成交额  [6]=成交量  [13]=昨结算（pct 基准）  [36]=日期
                #   [37]=时间  [49]=name
                row = {
                    "name": cols[49].strip(),
                    "code": sym.replace("nf_", ""),
                    "price": float(cols[3]),
                    "open": float(cols[0]),
                    "high": float(cols[1]),
                    "low": float(cols[2]),
                    "prev_close": float(cols[13]) if cols[13] else float(cols[0]),
                    "hold": float(cols[4]) if cols[4] else 0.0,
                    "volume": float(cols[6]) if cols[6] else 0.0,
                    "time": cols[37] if len(cols) > 37 else "",
                    "date": cols[36] if len(cols) > 36 else "",
                    "category": "指数",
                }
            elif n >= 40:  # 国内商品主连（实测 44 列）
                # sina nf_M0 字段序（2016 集搜客 + 2018 CSDN + 2022 博客 + 2026-09-03 实证）：
                #   cols[0]=name  [2]=开盘  [3]=最高  [4]=最低  [8]=最新价
                #   [10]=昨结算（pct 基准）  [13]=持仓量  [14]=成交量  [17]=日期
                row = {
                    "name": cols[0].strip(),
                    "code": sym.replace("nf_", ""),
                    "price": float(cols[8]),
                    "open": float(cols[2]) if cols[2] else 0.0,
                    "high": float(cols[3]) if cols[3] else 0.0,
                    "low": float(cols[4]) if cols[4] else 0.0,
                    "prev_close": float(cols[10]) if cols[10] else float(cols[8]),
                    "hold": float(cols[13]) if cols[13] else 0.0,
                    "volume": float(cols[14]) if cols[14] else 0.0,
                    "market": cols[15] if len(cols) > 15 else "",
                    "date": cols[17] if len(cols) > 17 else "",
                    "category": "商品",
                }
            elif n >= 10:  # 外盘商品（实测 15 列；hf_AL/hf_DXY 不支持为空返回 1 列会跳过）
                # sina hf_GC/hf_CL 字段序（2026-09-03 实证）：
                #   cols[0]=最新价  [2]=开盘  [4]=最高  [5]=最低  [6]=时间(HH:MM:SS)
                #   [7]=昨结  [12]=日期  [13]=name
                row = {
                    "name": cols[13].strip(),
                    "code": sym.replace("hf_", ""),
                    "price": float(cols[0]),
                    "open": float(cols[2]) if cols[2] else float(cols[0]),
                    "high": float(cols[4]) if cols[4] else float(cols[0]),
                    "low": float(cols[5]) if cols[5] else float(cols[0]),
                    "prev_close": float(cols[7]) if cols[7] else float(cols[0]),
                    "time": cols[6] if len(cols) > 6 else "",
                    "date": cols[12] if len(cols) > 12 else "",
                    "category": "外盘",
                }
            else:
                continue
        except (ValueError, IndexError):
            continue
        prev = row.get("prev_close", 0.0) or 0.0
        cur = row["price"]
        if prev:
            row["chg"] = round(cur - prev, 4)
            row["pct"] = round((cur - prev) / prev * 100, 2)
        else:
            row["chg"] = 0.0
            row["pct"] = 0.0
        out[sym] = row
    return out


# ---------------- 配置驱动的 symbol 列表 ----------------
def _all_symbols_with_sector():
    """返回 [(sina_sym, sector_or_None), ...]：sector 用于板块榜归类。"""
    out = []
    for code, _ in config.CN_FUT_INDICES:
        out.append(("nf_" + code, None))
    for sector, items in config.CN_FUT_SECTORS.items():
        for code, _ in items:
            out.append(("nf_" + code, sector))
    for code, _ in config.OVERSEAS_FUT:
        out.append(("hf_" + code, None))
    return out


def _row_for(rows, sym_prefix, code):
    return rows.get(sym_prefix + code)


# ---------------- 主抓取器 ----------------
class FuturesData:
    """期货行情（M3，ADR-0016）：新浪 hq 单线程 6s 轮询，国内主连+外盘商品。"""

    def __init__(self):
        self.lock = threading.RLock()
        self.rows = {}        # sina_sym -> row dict
        self.update = None
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
        symbols = _all_symbols_with_sector()
        sina_syms = [s for s, _ in symbols]
        text = _sina_csv(sina_syms)
        rows = _parse_sina_futures(text)
        with self.lock:
            if rows:
                self.rows = rows
                self.update = datetime.now().strftime("%H:%M:%S")

    def snapshot(self):
        with self.lock:
            rows = self.rows
            now = datetime.now()
            # 1) 股指主连卡（指数概览顶部）
            indices = []
            for code, name in config.CN_FUT_INDICES:
                r = _row_for(rows, "nf_", code)
                if not r:
                    continue
                indices.append({
                    "code": r["code"], "name": r["name"] or name,
                    "price": r["price"], "chg": r["chg"], "pct": r["pct"],
                    "open": r["open"], "high": r.get("high", 0.0), "low": r.get("low", 0.0),
                })
            # 2) 商品指数代表卡（每板块取首只主连）→ 与 M2 us ov 类似的「指数卡条」
            goods_index = []
            for sector, items in config.CN_FUT_SECTORS.items():
                if not items:
                    continue
                code, _ = items[0]
                r = _row_for(rows, "nf_", code)
                if not r:
                    continue
                goods_index.append({
                    "code": r["code"], "name": r["name"], "sector": sector,
                    "price": r["price"], "chg": r["chg"], "pct": r["pct"],
                })
            # 3) 主力涨幅榜（国内所有主连 = 股指+商品）
            main_board = []
            for s, _ in _all_symbols_with_sector():
                r = rows.get(s)
                if not r or r["category"] == "外盘":
                    continue
                main_board.append({
                    "code": r["code"], "name": r["name"],
                    "price": r["price"], "chg": r["chg"], "pct": r["pct"],
                    "category": r["category"],
                })
            main_board.sort(key=lambda x: x["pct"], reverse=True)
            # 4) 板块榜（按板块分组）
            sectors = {}
            for sector, items in config.CN_FUT_SECTORS.items():
                rows_in = []
                for code, name in items:
                    r = _row_for(rows, "nf_", code)
                    if not r:
                        continue
                    rows_in.append({
                        "code": r["code"], "name": r["name"] or name,
                        "price": r["price"], "chg": r["chg"], "pct": r["pct"],
                    })
                if rows_in:
                    sectors[sector] = rows_in
            # 5) 夜盘关注（夜盘品种按涨跌幅排序）
            night = []
            for code, name in config.CN_FUT_NIGHT:
                r = _row_for(rows, "nf_", code)
                if not r:
                    continue
                night.append({
                    "code": r["code"], "name": r["name"] or name,
                    "price": r["price"], "chg": r["chg"], "pct": r["pct"],
                })
            night.sort(key=lambda x: x["pct"], reverse=True)
            # 6) 外盘商品
            overseas = []
            for code, name in config.OVERSEAS_FUT:
                r = _row_for(rows, "hf_", code)
                if not r:
                    continue
                overseas.append({
                    "code": r["code"], "name": r["name"] or name,
                    "price": r["price"], "chg": r["chg"], "pct": r["pct"],
                    "open": r["open"], "high": r["high"], "low": r["low"],
                    "time": r["time"], "date": r["date"],
                })
            return {
                "mk": "fut",
                # state = 国内时段（与 hk/us snapshot 的 state 语义一致，供 shell/守卫复用）
                "state": _fut_domestic_state(now),
                "state_domestic": _fut_domestic_state(now),
                "state_overseas": _overseas_state(now),
                "indices": indices,
                "goods_index": goods_index,
                "main_board": main_board,
                "sectors": sectors,
                "night": night,
                "overseas": overseas,
                "update": self.update,
                "errors": list(self.errors),
                "now": now.strftime("%H:%M:%S"),
            }

    # ---------------- 线程 ----------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="fut-data")
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.fetch()
            except Exception as e:
                self._err("期货", e)
            time.sleep(config.FUT_INTERVAL)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)


if __name__ == "__main__":
    f = FuturesData()
    f.fetch()
    s = f.snapshot()
    print("domestic state:", s["state_domestic"])
    print("overseas state:", s["state_overseas"])
    print("indices:", len(s["indices"]), "goods_index:", len(s["goods_index"]),
          "main:", len(s["main_board"]), "sectors:",
          {k: len(v) for k, v in s["sectors"].items()},
          "night:", len(s["night"]), "overseas:", len(s["overseas"]))
    for i in s["indices"]:
        print("  idx", i["name"], i["price"], f"{i['pct']:+.2f}%")
    for i in s["goods_index"]:
        print("  goods", i["sector"], i["name"], i["price"], f"{i['pct']:+.2f}%")
    print("main top5:")
    for p in s["main_board"][:5]:
        print("  ", p["category"], p["name"], p["price"], f"{p['pct']:+.2f}%")
    print("night top3:")
    for p in s["night"][:3]:
        print("  night", p["name"], p["price"], f"{p['pct']:+.2f}%")
    print("overseas:")
    for p in s["overseas"]:
        print("  ov", p["name"], p["price"], f"{p['pct']:+.2f}%")
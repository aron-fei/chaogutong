# -*- coding: utf-8 -*-
"""腾讯板块榜抓取（M7 短线精灵的「板块涨速 / 概念涨速」数据源）

背景：原 THS 行业板块接口 `ak.stock_board_industry_summary_ths()` 盘中涨跌幅不刷新，
1 分钟差分恒为 0 被置空 → 工作台「板块涨速」长期显示 `--`，短线精灵的板块涨速事件无法产生。

替换源（2026-09-14 实测可用，与 qt.gtimg.cn 同属腾讯，无需 Referer）：
  https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank
    ?board_type=hy|gn|dy & sort_type=speed|price|turnover & direct=down & offset=0 & count=N

字段（已核验单位）：
  name 板块名 / zdf 今日涨跌幅(%) / speed 腾讯官方涨速(%) / lb 量比 / hsl 换手率(%)
  turnover 成交额(万元) / zljlr 主力净流入(万元) / lzg 领涨股{code,name,zdf} / zxj 最新价
  核验方式：31 个一级行业 turnover 合计 = 15,019 亿 ≈ 全A 当日成交额 14,975 亿 ✅
            zljlr 合计 -159 亿（全市场主力净流出，符号合理）✅

  board_type：hy = 一级行业（31 个，全量）；gn = 概念（可分页，取涨速前 N）；dy = 地域
  sort_type：speed / price / turnover 可用；zdf / hsl / lb 返回空（实测）

产出：self.boards 列表 + 喂给 sprite 引擎（自算 5/15 分钟涨速 + 板块涨速事件）。
"""
import threading
import time
import traceback
from datetime import datetime

import requests

import config

RANK_URL = "https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank"


class BoardRankFetcher:
    def __init__(self):
        self.lock = threading.RLock()
        self.boards = []          # [{type,name,zdf,speed,lb,hsl,amount_yi,netflow_yi,lead,lead_pct}]
        self.last_update = None
        self.errors = []
        self._stop = threading.Event()
        self._thread = None
        self._closed_done = False

    def _err(self, tag, exc):
        msg = f"[{datetime.now():%H:%M:%S}] board-{tag}: {type(exc).__name__} {exc}"
        print(msg)
        with self.lock:
            self.errors.append(msg)
            if len(self.errors) > 20:
                self.errors.pop(0)

    # ------------------------------------------------------------------ 抓取
    @staticmethod
    def _fetch_one(board_type, count):
        r = requests.get(RANK_URL, params={
            "board_type": board_type, "sort_type": "speed",
            "direct": "down", "offset": 0, "count": count,
        }, timeout=8)
        j = r.json()
        if j.get("code") != 0:
            raise RuntimeError(f"rank api code={j.get('code')} msg={j.get('msg')}")
        return ((j.get("data") or {}).get("rank_list")) or []

    def _fetch(self):
        out = []
        for btype, count in (("hy", config.BOARD_RANK_HY),
                             ("gn", config.BOARD_RANK_GN)):
            try:
                raw = self._fetch_one(btype, count)
            except Exception as e:
                self._err(btype, e)
                continue
            for x in raw:
                try:
                    lzg = x.get("lzg") or {}
                    out.append({
                        "type": btype,
                        "code": x.get("code") or "",
                        "name": (x.get("name") or "").strip(),
                        "zdf": _f(x.get("zdf")),
                        "speed": _f(x.get("speed")),
                        "lb": _f(x.get("lb")),
                        "hsl": _f(x.get("hsl")),
                        "amount_yi": round((_f(x.get("turnover")) or 0) / 1e4, 1),
                        "netflow_yi": round((_f(x.get("zljlr")) or 0) / 1e4, 2),
                        "lead": lzg.get("name") or "",
                        "lead_code": lzg.get("code") or "",
                        "lead_pct": _f(lzg.get("zdf")),
                    })
                except Exception:
                    continue
        return out

    def fetch_once(self):
        boards = self._fetch()
        if not boards:
            raise RuntimeError("板块榜返回空")
        with self.lock:
            self.boards = boards
            self.last_update = datetime.now()
        # 喂给短线精灵（板块涨速事件 + 板块涨速榜）
        try:
            from data_fetcher import market_status
            status, _ = market_status()
        except Exception:
            status = "trading"
        try:
            import sprite
            sprite.instance().feed_boards(
                boards, ts=time.time(), in_trading=(status == "trading"))
        except Exception as e:
            self._err("feed", e)

    # ------------------------------------------------------------------ 线程
    def _loop(self):
        while not self._stop.is_set():
            try:
                from data_fetcher import market_status
                status, _ = market_status()
            except Exception:
                status = "trading"
            if status in ("closed", "closed_all_day"):
                if status == "closed" and not self._closed_done:
                    try:
                        self.fetch_once()
                        self._closed_done = True
                        print(f"[{datetime.now():%H:%M:%S}] 板块榜 收盘定版完成")
                    except Exception as e:
                        self._err("收盘定版", e)
                        self._closed_done = True
                self._stop.wait(600)
                continue
            t0 = time.time()
            try:
                self.fetch_once()
                print(f"[{datetime.now():%H:%M:%S}] 板块榜 ok ({time.time() - t0:.1f}s, "
                      f"{len(self.boards)} 个)")
            except Exception as e:
                self._err("loop", e)
                traceback.print_exc()
            self._stop.wait(max(config.BOARD_RANK_INTERVAL - (time.time() - t0), 3))

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="fetch-板块榜")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def snapshot(self):
        with self.lock:
            return {
                "boards": self.boards,
                "last_update": (self.last_update.strftime("%H:%M:%S")
                                if self.last_update else None),
                "errors": self.errors[-3:],
            }


def _f(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


_INSTANCE = None
_LOCK = threading.Lock()


def instance():
    global _INSTANCE
    with _LOCK:
        if _INSTANCE is None:
            _INSTANCE = BoardRankFetcher()
        return _INSTANCE

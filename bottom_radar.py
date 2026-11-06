# -*- coding: utf-8 -*-
"""M8 底部放量雷达服务层（ADR-0033）

把 `bottom_surge.scan()` 包成「带缓存 + 后台刷新」的只读数据源，供工作台
`/api/bottom_surge` 秒回。

为什么要缓存：整轮扫描 = 全A快照(约3.6s) + 150只日K(约1.5s) ≈ 5~6s，
不能放在 HTTP 请求里同步跑（前端轮询会堆积、手机端会超时）。

策略：
  · 缓存 TTL 90s，命中直接返回，请求永不阻塞；
  · 缓存过期时：**先返回旧快照**（带 stale / age_sec 让前端标注），再唤醒后台线程刷新；
  · 完全无缓存（首次访问）→ 同步跑一次（唯一一次阻塞 ~6s），保证 tab 首屏有数据；
  · 非交易时段不自动刷新（避免无意义请求），`force=1` 可手动触发；
  · 任何异常都被捕获并保留旧快照，绝不抛给 Flask 请求。
"""
import threading
import time
from datetime import datetime

import bottom_surge

CACHE_TTL = 90           # 秒
_TRADING_STATES = ("trading", "auction", "pre")   # 需要自动刷新的市场状态


class BottomRadar:
    def __init__(self):
        self._lock = threading.Lock()
        self._rows = None
        self._meta = None
        self._ts = 0.0
        self._err = None
        self._refreshing = False

    # ---------------- 内部 ----------------
    def _do_scan(self):
        """真正跑一次扫描。返回 (rows, meta) 或抛异常。"""
        res = bottom_surge.scan(top=bottom_surge.TOP_FOR_UI, verbose=False)
        return res["rows"], res["meta"]

    def _refresh(self, sync=False):
        """刷新缓存。sync=True 时同步执行（仅首次/手动强制用）。"""
        with self._lock:
            if self._refreshing:
                return
            self._refreshing = True
        try:
            rows, meta = self._do_scan()
            with self._lock:
                self._rows, self._meta, self._ts, self._err = rows, meta, time.time(), None
            print(f"[bottom_radar] 刷新完成 {meta.get('asof')} 命中 {meta.get('hits')}")
        except Exception as e:
            with self._lock:
                self._err = f"{type(e).__name__}: {e}"
            print(f"[bottom_radar] 刷新失败(保留旧快照): {self._err}")
        finally:
            with self._lock:
                self._refreshing = False

    @staticmethod
    def _in_trading():
        try:
            import data_fetcher
            status, _ = data_fetcher.market_status()
            return status in _TRADING_STATES
        except Exception:
            return True    # 判断失败时按交易中处理，宁可多刷

    # ---------------- 对外 ----------------
    def warm(self):
        """服务启动后后台预热一次，避免首个 HTTP 请求撞上整轮扫描。"""
        threading.Thread(target=self._refresh, daemon=True).start()

    def snapshot(self, force=False):
        """返回可直接 jsonify 的 dict。force=True 时同步刷新（用户点「立即刷新」）。"""
        if force:
            self._refresh(sync=True)
        else:
            age = time.time() - self._ts
            need = (self._ts == 0.0) or (age > CACHE_TTL and self._in_trading())
            if need:
                # 2026-11-05 体检剩余项：首次访问也走后台，绝不在 HTTP 请求里同步跑
                # 整轮扫描（实测服务重启后首个请求 13.1s）。服务启动时已 warm() 预热；
                # 即使未预热，返回 init + retry_after，前端下一轮轮询即可拿到数据。
                threading.Thread(target=self._refresh, daemon=True).start()

        with self._lock:
            age = (time.time() - self._ts) if self._ts else None
            return {
                "mk": "bottom_surge",
                "state": "ok" if self._rows is not None else ("error" if self._err else "init"),
                "error": self._err,
                "stale": bool(age is not None and age > CACHE_TTL),
                "age_sec": round(age, 1) if age is not None else None,
                "cache_ttl": CACHE_TTL,
                "retry_after": 3 if self._rows is None else None,
                "meta": self._meta or {},
                "rows": self._rows or [],
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }


_INSTANCE = None
_INSTANCE_LOCK = threading.Lock()


def instance():
    global _INSTANCE
    if _INSTANCE is None:
        with _INSTANCE_LOCK:
            if _INSTANCE is None:
                _INSTANCE = BottomRadar()
    return _INSTANCE

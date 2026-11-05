# -*- coding: utf-8 -*-
"""轻量降噪告警（体检 P1-4）。

项目里原有 30 处 `except Exception: pass`，违反「异常必须可追溯」的交付铁律。
但后台线程每 5s 一轮，若全部逐次打印会重新制造日志刷屏（P2-3）。
本工具按 tag 做时间窗抑制：同一位置 60 秒内只打印一次，并汇总被抑制的次数。

用法：
    import log_util
    try: ...
    except Exception as _e:
        log_util.quiet_warn("app.py:836", _e)
"""
import threading
import time
from datetime import datetime

_lock = threading.Lock()
_last = {}
_suppressed = {}


def quiet_warn(tag, exc, min_interval=60):
    """打印一次带上下文的告警；窗口内的重复异常只计数不打印。"""
    now = time.time()
    with _lock:
        if now - _last.get(tag, 0) < min_interval:
            _suppressed[tag] = _suppressed.get(tag, 0) + 1
            return
        n = _suppressed.pop(tag, 0)
        _last[tag] = now
    extra = f"（窗口内另有 {n} 次同类异常被抑制）" if n else ""
    print(f"[{datetime.now():%H:%M:%S}] [ignored] {tag}: "
          f"{type(exc).__name__}: {exc}{extra}", flush=True)


def stats():
    """给 /api/health 用：返回当前被抑制的异常计数（只读快照）。"""
    with _lock:
        return {"tracked_tags": len(_last),
                "suppressed_total": sum(_suppressed.values()),
                "suppressed_by_tag": dict(_suppressed)}


if __name__ == "__main__":
    # 自检：连打 3 次，应只输出 1 行，随后 stats 显示 2 次被抑制
    for _ in range(3):
        quiet_warn("selftest", ValueError("boom"))
    print("[selftest]", stats())
    assert stats()["suppressed_total"] == 2
    print("[selftest] PASS")

# -*- coding: utf-8 -*-
"""新闻 / 传闻催化层（2026-11-05 首板雷达 v2 · L0 的一个因子）

用户原话：「市场也有新闻 传闻 首板」——题材要成趋势，往往先有消息面催化。
本模块只做一件事：把**真实**财经快讯按关键词映射到题材，给出「近 N 小时
每个题材有几条催化」，供 L0 板块强度分加分、L1 候选打催化标签、UI 展示原文链接。

数据源（实测 2026-11-05 可用）：
    akshare.stock_info_global_em()  → 东财全球财经快讯 200 条
    字段：标题 / 摘要 / 发布时间 / 链接

设计约束
--------
* 绝不编造快讯：接口失败时 state=degraded、heat 沿用旧缓存并标注 stale，
  UI 明确显示「催化源不可用」，不用占位文案冒充新闻。
* 纯读，不触碰任何买卖通道；akshare 延迟导入，不拖慢 Flask 启动。
* 不留 except: pass，所有异常打印带上下文。
* 关键词只做「题材级」匹配，不做个股名匹配（股票名表成本高、易误命中）。

预热 / 自检：
    /opt/homebrew/anaconda3/bin/python3 news_catalyst.py --warm
    /opt/homebrew/anaconda3/bin/python3 news_catalyst.py --selftest
"""
import json
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import config

CACHE_FILE = Path(__file__).parent / "data" / "news_catalyst_cache.json"


def _parse_time(s):
    """东财快讯时间形如 '2026-11-05 16:47:00'；解析失败返回 None。"""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(str(s).strip(), fmt)
        except (ValueError, TypeError):
            continue
    return None


def match_themes(text):
    """返回该条快讯命中的题材名列表（大小写不敏感的英文关键词单独处理）。"""
    if not text:
        return []
    low = str(text)
    out = []
    for theme, kws in getattr(config, "FB_THEME_KEYWORDS", {}).items():
        for kw in kws:
            if kw.isascii():
                # 英文/数字短词必须按词边界匹配，否则 "AI" 会命中 AIR / DAILY 等
                pat = r"(?<![A-Za-z0-9])" + re.escape(kw) + r"(?![A-Za-z0-9])"
                hit = re.search(pat, low, re.I) is not None
            else:
                hit = kw in low
            if hit:
                out.append(theme)
                break
    return out


class NewsCatalyst:
    def __init__(self):
        self._lock = threading.RLock()
        self._items = []          # [{title, summary, time, url, themes}]
        self._heat = {}           # 题材 -> {count, titles:[...]}
        self._state = "init"      # init / ready / degraded
        self._err = ""
        self._updated_at = 0.0
        self._thread = None
        self._stop = threading.Event()
        self._load_cache()

    # ---------------- 缓存 ----------------
    def _load_cache(self):
        try:
            if not CACHE_FILE.exists():
                return
            d = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            with self._lock:
                self._items = d.get("items") or []
                self._heat = d.get("heat") or {}
                self._updated_at = float(d.get("updated_at") or 0)
                self._state = "ready" if self._items else "init"
        except Exception as e:
            print(f"[news] 读缓存失败（将重建）: {type(e).__name__}: {e}")

    def _save_cache(self):
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = CACHE_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({
                "updated_at": time.time(),
                "items": self._items,
                "heat": self._heat,
            }, ensure_ascii=False), encoding="utf-8")
            tmp.replace(CACHE_FILE)
        except Exception as e:
            print(f"[news] 写缓存失败: {type(e).__name__}: {e}")

    # ---------------- 抓取 ----------------
    def refresh(self):
        """拉一次快讯，重算题材催化。返回 bool。"""
        t0 = time.time()
        try:
            import akshare as ak
            df = ak.stock_info_global_em()
            if df is None or not len(df):
                raise RuntimeError("东财快讯返回空")
            lookback = float(getattr(config, "FB_NEWS_LOOKBACK_H", 4))
            cutoff = datetime.now() - timedelta(hours=lookback)
            keep = int(getattr(config, "FB_NEWS_KEEP", 200))
            items, heat = [], {}
            for _, r in df.iterrows():
                title = str(r.get("标题") or "").strip()
                summary = str(r.get("摘要") or "").strip()
                ts = _parse_time(r.get("发布时间"))
                if not title and not summary:
                    continue
                # 无时间或超出回溯窗口的快讯：仍保留作展示，但不计入催化热度
                fresh = ts is not None and ts >= cutoff
                themes = match_themes(title + " " + summary)
                item = {
                    "title": title or summary[:40],
                    "summary": summary[:160],
                    "time": ts.strftime("%m-%d %H:%M") if ts else None,
                    "url": str(r.get("链接") or ""),
                    "themes": themes,
                    "fresh": bool(fresh),
                }
                items.append(item)
                if fresh:
                    for t in themes:
                        h = heat.setdefault(t, {"count": 0, "titles": []})
                        h["count"] += 1
                        if len(h["titles"]) < 3:
                            h["titles"].append(item["title"])
            with self._lock:
                self._items = items[:keep]
                self._heat = heat
                self._updated_at = time.time()
                self._state = "ready"
                self._err = ""
            self._save_cache()
            print(f"[news] 刷新完成: {len(self._items)} 条快讯 / "
                  f"{len(heat)} 个题材有催化 / {time.time() - t0:.1f}s")
            return True
        except Exception as e:
            with self._lock:
                self._state = "degraded" if self._items else "init"
                self._err = f"{type(e).__name__}: {str(e)[:140]}"
            print(f"[news] 刷新失败（沿用旧催化，不编造）: {self._err}")
            return False

    # ---------------- 后台线程 ----------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="news-catalyst", daemon=True)
        self._thread.start()
        print("[news] 快讯催化线程已启动")

    def _loop(self):
        self.refresh()
        interval = int(getattr(config, "FB_NEWS_REFRESH_SEC", 180))
        while not self._stop.wait(interval):
            self.refresh()

    # ---------------- 查询 ----------------
    def heat(self):
        """题材 -> {count, titles}；调用方只读。"""
        with self._lock:
            return {k: {"count": v["count"], "titles": list(v["titles"])}
                    for k, v in self._heat.items()}

    def heat_for(self, theme):
        with self._lock:
            return int((self._heat.get(theme) or {}).get("count") or 0)

    def items(self, limit=30, theme=None):
        with self._lock:
            src = [x for x in self._items
                   if not theme or theme in x.get("themes", ())] if theme else self._items
            return [dict(x) for x in src[:limit]]

    def snapshot(self):
        with self._lock:
            return {
                "state": self._state,
                "err": self._err,
                "stale": bool(self._updated_at and
                              time.time() - self._updated_at >
                              int(getattr(config, "FB_NEWS_REFRESH_SEC", 180)) * 3),
                "updated_at": datetime.fromtimestamp(self._updated_at).strftime("%m-%d %H:%M")
                              if self._updated_at else None,
                "item_count": len(self._items),
                "theme_count": len(self._heat),
                "top": sorted(((k, v["count"]) for k, v in self._heat.items()),
                              key=lambda x: -x[1])[:8],
            }


_INSTANCE = None
_INST_LOCK = threading.Lock()


def instance():
    global _INSTANCE
    with _INST_LOCK:
        if _INSTANCE is None:
            _INSTANCE = NewsCatalyst()
        return _INSTANCE


if __name__ == "__main__":
    import sys
    if "--warm" in sys.argv:
        print("warm:", "OK" if instance().refresh() else "FAILED", instance().snapshot())
    elif "--selftest" in sys.argv:
        got = match_themes("【商业航天】某卫星互联网星座完成首批组网发射")
        print("[1] 题材匹配:", got)
        assert "商业航天" in got, "商业航天关键词未命中"
        cpo = match_themes("800G 光模块需求超预期，CPO 渗透率提升")
        print("[2] 题材匹配:", cpo)
        assert "光模块·CPO" in cpo, "光模块关键词未命中"
        assert match_themes("") == [], "空文本不应命中"
        print("[3] 时间解析:", _parse_time("2026-11-05 16:47:00"))
        assert _parse_time("bad") is None
        print("[4] snapshot:", instance().snapshot())
        print("[selftest] PASS")
    else:
        print(__doc__)

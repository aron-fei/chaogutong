# -*- coding: utf-8 -*-
"""联动观察引擎（M4，ADR-0017）

聚合 A 股板块实时榜 + hk/us/fut 行情 + 映射表（data/us_a_map.json +
data/fut_sector_map.json），为 A 股第 14 tab「🌐 联动观察」提供三区块数据：
  1. 隔夜外盘影响链：美股科技 → A 股板块 + 关注个股
  2. 夜盘期货预期：夜盘品种 → A 股板块 + 关注个股
  3. 港股 45min 快照：4 指数 + top8 涨幅 + top5 成交额（A股开盘前决策温度计）

无独立线程 — 前端 30s 轮询 /api/linkage，即时聚合（各 fetcher 内部已缓存）。
"""
import os
import sys
for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
           "all_proxy", "ALL_PROXY"]:
    os.environ.pop(_k, None)

import json
import threading
from datetime import datetime
from pathlib import Path


_MAP_DIR = Path(__file__).parent / "data"
_MAX_ERR = 5


def _load(name):
    """容错加载映射表：缺失/损坏时返回空 dict 并打印告警，不让整个 app 导入期崩溃。

    历史教训（2026-11-06 cutover）：data/us_a_map.json / fut_sector_map.json 是静态参考
    映射表，被误归档后 linkage 在导入期硬读 → FileNotFoundError 拖垮整个 Flask 启动。
    改为容错后，即使映射文件缺失也只影响「联动观察」单接口，而非全站宕机。
    """
    try:
        return json.loads((_MAP_DIR / name).read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"[linkage] 警告：映射表 {name} 缺失，联动观察将返回空数据（不影响其余功能）",
              file=sys.stderr)
        return {}
    except (json.JSONDecodeError, OSError) as e:
        print(f"[linkage] 警告：映射表 {name} 解析失败：{e}", file=sys.stderr)
        return {}


US_A_MAP = _load("us_a_map.json")
FUT_MAP = _load("fut_sector_map.json")


def _sector_match(keywords, sectors):
    """板块关键词模糊匹配：板块名包含关键词 或 关键词包含板块名。"""
    out = []
    used = set()
    for s in sectors:
        name = s.get("name", "")
        if not name or name in used:
            continue
        for kw in keywords:
            if kw in name or name in kw:
                out.append({
                    "name": name,
                    "pct": s.get("pct", 0.0),
                    "leader": s.get("leader", ""),
                    "leader_pct": s.get("leader_pct", 0.0),
                })
                used.add(name)
                break
    return out


class LinkageData:
    """联动观察：build() 由前端拉取时即时构建；start/stop 仅为接口对齐（不耗线程）。"""

    def __init__(self, md, hk, us, fut):
        self._md = md
        self._hk = hk
        self._us = us
        self.fut = fut
        self.lock = threading.RLock()
        self.update = None
        self.errors = []

    def _err(self, tag, exc):
        msg = f"[{datetime.now():%H:%M:%S}] {tag}: {type(exc).__name__} {exc}"
        with self.lock:
            self.errors.append(msg)
            if len(self.errors) > _MAX_ERR:
                self.errors.pop(0)

    def build(self):
        try:
            md_snap = self._md.snapshot()
            sectors = md_snap.get("sectors", []) or []
            us_snap = self._us.snapshot()
            fut_snap = self.fut.snapshot()
            hk_snap = self._hk.snapshot()

            # ----- ① 美股 → A 股 -----
            us_pool = {p["code"]: p for p in (us_snap.get("pool", []) or [])}
            us_indices = {i["code"]: i for i in (us_snap.get("indices", []) or [])}
            us_to_a = []
            for m in US_A_MAP["mapping"]:
                pcts = []
                for uc in m["us_codes"]:
                    p = us_pool.get(uc) or us_indices.get(uc) or {}
                    pct = p.get("pct")
                    if pct is not None:
                        pcts.append(pct)
                us_avg = round(sum(pcts) / len(pcts), 2) if pcts else None
                a_secs = _sector_match(m["a_keywords"], sectors)
                us_to_a.append({
                    "us_codes": m["us_codes"],
                    "us_label": m["us_label"],
                    "us_avg_pct": us_avg,
                    "a_sectors": a_secs,
                    "a_leaders": m["a_leaders"],
                    "note": m.get("note", ""),
                    "active": bool(pcts) or bool(a_secs),
                })

            # ----- ② 夜盘期货 → A 股板块 -----
            # 来源：夜盘（10 只）+ goods_index（5 板块代表）+ main_board 中商品类（覆盖焦煤/焦炭/铁矿等非夜盘但板块关联的主连）
            fut_pct_map = {}
            for f in (fut_snap.get("night", []) or []) + \
                     (fut_snap.get("goods_index", []) or []):
                fut_pct_map[f["code"]] = f.get("pct", 0.0)
            for f in (fut_snap.get("main_board", []) or []):
                if f.get("category") == "商品":
                    fut_pct_map[f["code"]] = f.get("pct", 0.0)
            fut_to_sector = []
            for m in FUT_MAP["mapping"]:
                pcts = [fut_pct_map[c] for c in m["fut_codes"]
                        if c in fut_pct_map]
                fut_pct = round(sum(pcts) / len(pcts), 2) if pcts else None
                a_secs = _sector_match(m["a_keywords"], sectors)
                fut_to_sector.append({
                    "fut_codes": m["fut_codes"],
                    "fut_label": m["fut_label"],
                    "fut_pct": fut_pct,
                    "a_sectors": a_secs,
                    "a_leaders": m["a_leaders"],
                    "note": m.get("note", ""),
                    "active": fut_pct is not None or bool(a_secs),
                })

            # ----- ③ 港股 45min 快照（A 股 09:15 前港股已开盘） -----
            hk_pool = hk_snap.get("pool", []) or []
            hk_top_pct = sorted(hk_pool, key=lambda x: -(x.get("pct") or 0))[:8]
            hk_top_amount = sorted(hk_pool,
                                   key=lambda x: -(x.get("amount_yi") or 0))[:5]
            hk_snapshot = {
                "indices": hk_snap.get("indices", []) or [],
                "top_pct": hk_top_pct,
                "top_amount": hk_top_amount,
                "state": hk_snap.get("state", {}),
                "update": hk_snap.get("update"),
            }

            now = datetime.now()
            with self.lock:
                self.update = now.strftime("%H:%M:%S")
            return {
                "generated_at": now.strftime("%H:%M:%S"),
                "us_to_a": us_to_a,
                "fut_to_sector": fut_to_sector,
                "hk_snapshot": hk_snapshot,
                "update": self.update,
                "errors": list(self.errors),
                "now": now.strftime("%H:%M:%S"),
            }
        except Exception as e:
            self._err("linkage", e)
            return {
                "generated_at": datetime.now().strftime("%H:%M:%S"),
                "us_to_a": [], "fut_to_sector": [], "hk_snapshot": {},
                "errors": list(self.errors) + [str(e)],
                "now": datetime.now().strftime("%H:%M:%S"),
            }

    # 占位接口（与 hk/us/fut/start/stop 风格对齐；联动无独立线程）
    def start(self):
        pass

    def stop(self):
        pass


if __name__ == "__main__":
    import data_fetcher
    import overseas_fetcher
    import futures_fetcher
    md = data_fetcher.MarketData()
    hk = overseas_fetcher.HkData()
    us = overseas_fetcher.UsData()
    fut = futures_fetcher.FuturesData()
    md.start(); hk.start(); us.start(); fut.start()
    import time
    time.sleep(8)
    lk = LinkageData(md, hk, us, fut)
    out = lk.build()
    print("generated_at:", out["generated_at"])
    print("us_to_a n:", len(out["us_to_a"]),
          "active:", sum(1 for x in out["us_to_a"] if x["active"]))
    print("fut_to_sector n:", len(out["fut_to_sector"]),
          "active:", sum(1 for x in out["fut_to_sector"] if x["active"]))
    print("hk_snapshot keys:", list(out["hk_snapshot"].keys()))
    for x in out["us_to_a"][:3]:
        print("  us", x["us_label"], "us_avg_pct", x["us_avg_pct"],
              "a_secs", [s["name"] for s in x["a_sectors"]],
              "leaders", [l["name"] for l in x["a_leaders"][:2]])
    for x in out["fut_to_sector"][:3]:
        print("  fut", x["fut_label"], "fut_pct", x["fut_pct"],
              "a_secs", [s["name"] for s in x["a_sectors"]])
# -*- coding: utf-8 -*-
"""细分题材板块引擎（2026-11-05 涨停池 v2）

解决的问题
----------
涨停池原始字段只有东财一级行业（「工业金属」「光伏设备」），看不到资金真正在炒的
细分主线（「光模块 / CPO」「商业航天」「固态电池」「羊字辈」）。本模块把
「涨停股 → 细分题材」建成一个可多对多的反向索引：

  东财概念板块榜 (stock_board_concept_name_em, 531 个概念，自带涨跌幅/领涨股)
        │  过滤伪概念（昨日首板 / 历史新高 / 东方财富热股 …）
        │  取涨幅前 N + 常驻 PIN 题材
        ▼
  并发拉概念成分股 (stock_board_concept_cons_em)  →  code -> [题材...]
        │
        ▼
  本地缓存 data/theme_cons_cache.json（stale-while-revalidate，重启秒级可用）
        │
        ▼
  app._build_zt_theme_groups() 聚合出一股多题材的「板块涨停分布」

设计约束
--------
* 纯读，绝不触碰任何买卖/下单通道。
* 网络失败一律降级：读旧缓存 → 用行业兜底，不让工作台白屏。
* akshare 延迟导入，避免拖慢 Flask 启动与 import 顺序（data_fetcher 必须最先）。
* 不留 except: pass，所有异常打印带上下文。

预热 / 自检：
    /opt/homebrew/anaconda3/bin/python3 theme_board.py --warm
    /opt/homebrew/anaconda3/bin/python3 theme_board.py --selftest
"""
import json
import threading
import time
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import config

CACHE_FILE = Path(__file__).parent / "data" / "theme_cons_cache.json"


def _is_pseudo(name):
    """过滤「技术型/统计型」伪概念，只保留有产业含义的题材。"""
    return any(k in name for k in getattr(config, "ZT_THEME_EXCLUDE_KW", ()))


def _norm(code):
    return str(code or "").strip().zfill(6)


class ThemeBoard:
    def __init__(self):
        self._lock = threading.RLock()
        self._by_code = {}       # "600000" -> ["商业航天", "卫星互联网", ...]
        self._by_theme = {}      # 2026-11-05 v2：题材名 -> set(代码)，供低位挖掘正向取成分股
        self._meta = {}          # 题材名 -> {zdf, lead, lead_pct, up_count, down_count, rank}
        self._built_at = 0.0
        self._state = "init"     # init / building / ready / degraded
        self._err = ""
        self._concept_total = 0
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
                self._by_code = d.get("by_code") or {}
                self._meta = d.get("meta") or {}
                # 兼容旧缓存（没有 by_theme 字段）：从反向索引即时重建
                self._by_theme = {k: set(v) for k, v in (d.get("by_theme") or {}).items()}
                if not self._by_theme and self._by_code:
                    for _c, _ts in self._by_code.items():
                        for _t in _ts:
                            self._by_theme.setdefault(_t, set()).add(_c)
                self._built_at = float(d.get("built_at") or 0)
                self._concept_total = int(d.get("concept_total") or 0)
                self._state = "ready" if self._by_code else "init"
            print(f"[theme] 读缓存 OK: {len(self._by_code)} 只股票 / "
                  f"{len(self._meta)} 个题材 / {datetime.fromtimestamp(self._built_at):%m-%d %H:%M}")
        except Exception as e:
            print(f"[theme] 读缓存失败（将重建）: {type(e).__name__}: {e}")

    def _save_cache(self, by_code, by_theme, meta, concept_total):
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = CACHE_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({
                "built_at": time.time(),
                "date": datetime.now().strftime("%Y%m%d"),
                "concept_total": concept_total,
                "by_code": by_code,
                "by_theme": {k: sorted(v) for k, v in by_theme.items()},
                "meta": meta,
            }, ensure_ascii=False), encoding="utf-8")
            tmp.replace(CACHE_FILE)
        except Exception as e:
            print(f"[theme] 写缓存失败: {type(e).__name__}: {e}")

    # ---------------- 构建 ----------------
    def _pull_one(self, ak, name, rank, info):
        """返回 (题材名, 成分股代码集合, 元信息)。单个失败不影响整体。"""
        try:
            df = ak.stock_board_concept_cons_em(symbol=name)
            codes = set()
            if df is not None and len(df):
                col = "代码" if "代码" in df.columns else df.columns[1]
                codes = {_norm(x) for x in df[col].astype(str).tolist()}
            return name, codes, dict(
                zdf=float(info.get("zdf") or 0),
                lead=str(info.get("lead") or ""),
                lead_pct=info.get("lead_pct"),
                up_count=int(info.get("up_count") or 0),
                down_count=int(info.get("down_count") or 0),
                rank=rank,
            )
        except Exception as e:
            print(f"[theme] 成分股拉取失败 {name}: {type(e).__name__}: {str(e)[:80]}")
            return name, set(), None

    def build(self, force=False):
        """拉概念榜 → 过滤 → 并发拉成分 → 合并缓存。返回 bool 成功与否。"""
        with self._lock:
            if self._state == "building":
                return False
            self._state = "building"
        t0 = time.time()
        try:
            import akshare as ak
            df = ak.stock_board_concept_name_em()
            if df is None or not len(df):
                raise RuntimeError("概念板块榜返回空")
            rows = []
            for i, r in df.iterrows():
                nm = str(r.get("板块名称") or "").strip()
                if not nm or _is_pseudo(nm):
                    continue
                try:
                    rows.append({
                        "name": nm,
                        "info": {
                            "zdf": float(r.get("涨跌幅") or 0),
                            "lead": str(r.get("领涨股票") or ""),
                            "lead_pct": float(r.get("领涨股票-涨跌幅") or 0),
                            "up_count": int(r.get("上涨家数") or 0),
                            "down_count": int(r.get("下跌家数") or 0),
                        },
                    })
                except (TypeError, ValueError) as e:
                    print(f"[theme] 概念行解析跳过 {nm}: {e}")
            top_n = int(getattr(config, "ZT_THEME_TOP_N", 110))
            pinned = list(getattr(config, "ZT_THEME_PINNED", ()))
            chosen, seen = [], set()
            # PIN 常驻题材优先，其次按概念榜涨幅（df 已按排名/涨幅排序）
            for nm in pinned + [x["name"] for x in rows[:top_n]]:
                if nm and nm not in seen:
                    seen.add(nm)
                    chosen.append(nm)
            meta_src = {x["name"]: x["info"] for x in rows}

            results = {}
            workers = int(getattr(config, "ZT_THEME_WORKERS", 12))
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = [ex.submit(self._pull_one, ak, nm, i + 1,
                                  meta_src.get(nm, {}))
                        for i, nm in enumerate(chosen)]
                for f in as_completed(futs):
                    nm, codes, meta = f.result()
                    if codes:
                        results[nm] = (codes, meta or {"rank": 9999})

            if not results:
                raise RuntimeError("所有概念成分股均拉取失败")

            by_code, by_theme, meta = {}, {}, {}
            for nm, (codes, m) in results.items():
                meta[nm] = m
                by_theme[nm] = set(codes)
                for c in codes:
                    by_code.setdefault(c, []).append(nm)
            for c in by_code:
                by_code[c].sort(key=lambda n: meta.get(n, {}).get("rank", 9999))

            with self._lock:
                self._by_code, self._by_theme, self._meta = by_code, by_theme, meta
                self._built_at = time.time()
                self._concept_total = len(results)
                self._state = "ready"
                self._err = ""
            self._save_cache(by_code, by_theme, meta, len(results))
            print(f"[theme] 索引构建完成: {len(results)} 题材 / {len(by_code)} 只成分股 / "
                  f"{time.time() - t0:.1f}s")
            return True
        except Exception as e:
            with self._lock:
                self._state = "degraded" if self._by_code else "init"
                self._err = f"{type(e).__name__}: {str(e)[:120]}"
            print(f"[theme] 索引构建失败（降级用行业兜底）: {self._err}")
            return False

    # ---------------- 后台线程 ----------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="theme-board", daemon=True)
        self._thread.start()
        print("[theme] 细分题材索引线程已启动")

    def _loop(self):
        # 首轮：缓存新鲜(1h 内)则跳过，避免每次重启都打接口
        fresh = (time.time() - self._built_at) < 3600
        if not fresh:
            self.build()
        interval = int(getattr(config, "ZT_THEME_REFRESH_MIN", 30)) * 60
        while not self._stop.wait(interval):
            self.build()

    # ---------------- 查询 ----------------
    def themes_for(self, code):
        with self._lock:
            return list(self._by_code.get(_norm(code), ()))

    def codes_for(self, name):
        """正向取某题材的全部成分股代码（L1 低位挖掘用）。

        题材名可能是合并后的别名（如「光模块·CPO」），此时把映射到它的
        原始概念成分股取并集，避免别名合并后成分股丢失。
        """
        alias = getattr(config, "ZT_THEME_ALIASES", {})
        with self._lock:
            names = [name] + [k for k, v in alias.items() if v == name]
            out = set()
            for nm in names:
                out |= self._by_theme.get(nm, set())
            return sorted(out)

    def meta(self, name):
        with self._lock:
            return dict(self._meta.get(name, {}))

    def snapshot(self):
        with self._lock:
            return {
                "state": self._state,
                "built_at": datetime.fromtimestamp(self._built_at).strftime("%m-%d %H:%M")
                            if self._built_at else None,
                "theme_count": len(self._meta),
                "code_count": len(self._by_code),
                "concept_total": self._concept_total,
                "err": self._err,
            }


_INSTANCE = None
_INST_LOCK = threading.Lock()


def instance():
    global _INSTANCE
    with _INST_LOCK:
        if _INSTANCE is None:
            _INSTANCE = ThemeBoard()
        return _INSTANCE


# ---------------- 字辈自动发现（概念库里没有的游资黑话） ----------------
def auto_name_themes(stocks):
    """统计涨停股名称里的高频汉字，自动挖出「X字辈」炒作（如羊字辈/马字辈）。

    只把「出现次数 >= 阈值」且不在停用字表里的字生成题材，避免把
    「科技 / 股份 / 电子」这类通用字误判成主线。
    """
    threshold = int(getattr(config, "ZT_THEME_NAME_MIN", 2))
    stop = set(getattr(config, "ZT_THEME_NAME_STOP", ()))
    allow = set(getattr(config, "ZT_THEME_NAME_WHITELIST", ()))
    cnt = {}
    for s in stocks:
        nm = str(s.get("name") or "").replace(" ", "").replace("\u3000", "")
        for ch in set(nm):
            if "\u4e00" <= ch <= "\u9fff" and ch not in stop and (not allow or ch in allow):
                cnt[ch] = cnt.get(ch, 0) + 1
    return {f"{ch}字辈": [s for s in stocks
                        if ch in str(s.get("name") or "").replace(" ", "")]
            for ch, n in cnt.items() if n >= threshold}


def rule_themes(stocks):
    """config.ZT_THEME_RULES：显式代码 / 名称关键词 / 行业关键词 三类规则。"""
    out = {}
    for rule in getattr(config, "ZT_THEME_RULES", ()):
        nm = rule.get("name")
        if not nm:
            continue
        codes = {_norm(c) for c in rule.get("codes", ())}
        kws = tuple(rule.get("name_kw", ()))
        iws = tuple(rule.get("industry_kw", ()))
        hit = []
        for s in stocks:
            stock_name = str(s.get("name") or "")
            industry = str(s.get("industry") or "")
            if (_norm(s.get("code")) in codes
                    or (kws and any(k in stock_name for k in kws))
                    or (iws and any(k in industry for k in iws))):
                hit.append(s)
        if hit:
            out[nm] = hit
    return out


if __name__ == "__main__":
    import sys
    if "--warm" in sys.argv:
        ok = instance().build(force=True)
        print("warm:", "OK" if ok else "FAILED", instance().snapshot())
        sys.exit(0 if ok else 1)
    if "--selftest" in sys.argv:
        s = instance().snapshot()
        print("[1] snapshot:", s)
        fake = [{"name": "三羊马", "code": "001317"},
                {"name": "羊绒股份", "code": "600001"},
                {"name": "金羊科技", "code": "300001"},
                {"name": "光迅科技", "code": "002281"}]
        got = auto_name_themes(fake)
        print("[2] 字辈发现:", list(got.keys()))
        assert "羊字辈" in got and len(got["羊字辈"]) == 3, "字辈规则异常"
        print("[3] themes_for(002281):", instance().themes_for("002281"))
        print("[selftest] PASS")
    else:
        print(__doc__)

# -*- coding: utf-8 -*-
"""GJD（国家队 / 汇金系）宽基 ETF 资金面跟踪器（2026-11-06）

背景
----
用户给了第三方「汪汪队 ETF 跟踪器」，要求把「国家队是否放量买入」接进推票条件。
实测该站无公开 API，但数据来自两个**公开**源：

  · 基准（20 日均额 + 日内成交额分布 + 汇金持仓）：站点每日盘后产出的 snapshot.json
  · 实时（成交额 / 外盘内盘 / 现价）：腾讯行情 qt.gtimg.cn

所以本模块**自建主链路**：实时全靠腾讯（与工作台全 A 行情同源，零新依赖），
基准每日从第三方快照取一次并缓存本地。第三方不可用时降级：
  ① 沿用旧基准（标 stale，注明基准日期）
  ② 完全无基准 → 用东财 ETF 日线自算 20 日均额 + 线性交易进度（标 fallback，倍数精度下降）

指标口径（全部可复现，不编造）
------------------------------
同时点放量倍数 ratio（页面「成交额放大倍数」同口径）
    基准_i = avg20_i × shape_i(当前时点)      # shape = 该 ETF 日内累计成交额占比曲线
    ratio  = Σ今日成交额_i / Σ基准_i
    这样不同 ETF 的成交节奏差异被各自 curve 吸收，不会因为「大票成交晚」误判。

主动净买入 net_buy（页面「主动净买入」同口径）
    net_i = (外盘手数 − 内盘手数) × 100 × 当日成交均价 / 1e8   （亿元）
    成交均价 = 成交额 / (成交量×100)，比用最新价更贴近真实成交。

池内加权涨跌 pct：按最新规模加权，反映 GJD 持仓篮子的整体涨跌。

**放量不等于买入**：ratio 高但 net_buy 为负 → 判定「放量出货/对冲」，不给高分。
这是本模块相对第三方页面额外做的区分，也是接进推票时最关键的一条。
"""

from __future__ import annotations

import json
import math
import threading
import time
from datetime import datetime
from pathlib import Path

# 不依赖本仓库的 config 模块：调度器（另一个进程/目录）也要 import 本文件，
# 若这里 `import config`，对方 sys.path 里的同名模块会被抢先命中，BASE_DIR 会指错。
# 自包含 = 谁 import 都指向本文件所在目录，行为可预测。
BASE_DIR = Path(__file__).resolve().parent

BASE_FILE = BASE_DIR / "data" / "gjd_etf_base.json"
BASE_URL = "https://etf-turnover-tracker.app.workbuddy.host/data/snapshot.json"
SHARECODE = "2V35TELSL4"
LIVE_URL = "https://qt.gtimg.cn/q="
REFRESH_SEC = 60
BASE_STALE_DAYS = 7          # 基准超过这个天数未刷新 → 标记过期（仍可用，但要提示）

# 分档：本地口径，写清楚来源，不冒充官方
LEVELS = (
    # (code, 名称, ratio 下限, 需要 net_buy > 0, score)
    ("strong_buy", "强力买入", 2.00, True, 100),
    ("buy",        "明显买入", 1.50, True, 80),
    ("mild_buy",   "温和放量", 1.20, False, 60),
    ("normal",     "常态",     0.80, False, 45),
    ("shrink",     "缩量观望", 0.00, False, 25),
)

# 闸门修正映射（唯一来源：工作台展示 + 调度器实际生效都用这一份，避免两边对不上）
#
# ⚠️ 设计要点：GJD 是**池级**信号，只能作用于「闸门（总仓上限 / 只数上限）」，
#    绝不能做成个股排序分的加分项 —— 对池内所有票同加一个常数，排序结果不变，
#    等于白改代码却让人误以为「已经考虑了国家队」。这是池级信号唯一正确的用法。
#
# 方向保守：宁可不加，也不因为「国家队买了」就放开仓位。
#   · 只有「放量 + 主动净买入」才放宽
#   · 「放量 + 主动净卖出」重罚（这是出货信号，比缩量更危险）
#   · 缩量只小幅收紧（国家队缺席不等于要跌）
#   · 无数据不加不减（fail-open，不凭猜测改仓位）
GATE_EFFECT = {
    "strong_buy":   {"pos_delta": +6.0, "pick_delta": +1, "dir": "loosen",
                     "why": "放量且主动净买入，长钱在托底"},
    "buy":          {"pos_delta": +4.0, "pick_delta": 0, "dir": "loosen",
                     "why": "明显放量且主动净买入"},
    "mild_buy":     {"pos_delta": 0.0, "pick_delta": 0, "dir": "hold",
                     "why": "有量但主动买盘不占优"},
    "normal":       {"pos_delta": 0.0, "pick_delta": 0, "dir": "hold",
                     "why": "成交额在 20 日均额常态区间"},
    "shrink":       {"pos_delta": -2.0, "pick_delta": 0, "dir": "tighten",
                     "why": "缩量，国家队缺席"},
    "distribution": {"pos_delta": -8.0, "pick_delta": -1, "dir": "tighten",
                     "why": "放量却主动净卖出，更像高位派发（比缩量更危险）"},
    "unknown":      {"pos_delta": 0.0, "pick_delta": 0, "dir": "hold",
                     "why": "无数据（盘前或基准缺失）"},
}


def gate_effect(level_code):
    """把 GJD 梯队翻译成闸门修正量。返回 dict（永不为 None，未知档按 hold 处理）。"""
    e = GATE_EFFECT.get(level_code) or GATE_EFFECT["unknown"]
    return {"pos_delta": e["pos_delta"], "pick_delta": e["pick_delta"],
            "dir": e["dir"], "why": e["why"]}


def _f(v, default=None):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def _session(now=None):
    """返回 (会话态, 是否可算 ratio)。"""
    now = now or datetime.now()
    hm = now.hour * 60 + now.minute
    if now.weekday() >= 5:
        return "closed", True          # 周末看最近一个交易日定版
    if hm < 9 * 60 + 30:
        return "preopen", False        # 未开盘：成交额还是 0 或昨日残值，倍数无意义
    if 11 * 60 + 30 <= hm < 13 * 60:
        return "lunch", True           # 午休：用 11:30 档
    if hm >= 15 * 60:
        return "closed", True
    return "trading", True


def _progress(shape, hm):
    """日内累计成交额占比，取 <= 当前时间的最后一档（shape: [["09:35", 0.07], ...]）。"""
    if not shape:
        return None
    best = None
    for pair in shape:
        try:
            t, v = pair[0], _f(pair[1])
        except (TypeError, IndexError):
            continue
        if t and v is not None and t <= hm:
            best = v
        else:
            break
    return best


def _linear_progress(hm):
    """无 shape 时的降级进度：按 A 股 240 个交易分钟线性摊。
    仅用于「基准不可用」的兜底，精度明显低于真实 curve，调用方会标 fallback。"""
    def mins(h, m):
        return h * 60 + m
    am_open, am_close = mins(9, 30), mins(11, 30)
    pm_open, pm_close = mins(13, 0), mins(15, 0)
    t = mins(int(hm[:2]), int(hm[3:5]))
    if t <= am_open:
        return 0.0
    if t <= am_close:
        used = t - am_open
    elif t <= pm_open:
        used = 120
    elif t <= pm_close:
        used = 120 + (t - pm_open)
    else:
        used = 240
    return round(min(used / 240.0, 1.0), 4)


# ---------------------------------------------------------------- 基准


def fetch_base(force=False):
    """从第三方快照取基准（每日一次）。返回 (ok, msg)。"""
    import requests
    try:
        r = requests.get(BASE_URL, params={"sharecode": SHARECODE}, timeout=25)
        r.raise_for_status()
        d = r.json()
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    items = d.get("items") or []
    if not items:
        return False, "快照 items 为空"
    slim = []
    for x in items:
        code = str(x.get("code") or "").strip()
        if not code:
            continue
        slim.append({
            "code": code,
            "tcode": x.get("tcode") or ("sh" + code if code[0] in "56" else "sz" + code),
            "name": x.get("name") or "",
            "index": x.get("index") or "",
            "group": x.get("group") or "core",
            "huijin_yi": _f(x.get("huijin_yi"), 0.0),
            "avg20_yi": _f(x.get("avg20_yi"), 0.0),
            # shape 只留 [[t, 累计占比], ...]，去掉无关字段
            "shape": [[p[0], _f(p[1], 0.0)] for p in (x.get("shape") or [])
                      if isinstance(p, (list, tuple)) and len(p) >= 2],
            "mktcap_yi": _f(x.get("mktcap_yi"), 0.0),
        })
    payload = {
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "trade_date": d.get("trade_date") or "",
        "source": "thirdparty snapshot（基准口径：腾讯行情 + 新浪 5 分钟 K 线日内分布）",
        "items": slim,
    }
    try:
        BASE_FILE.parent.mkdir(parents=True, exist_ok=True)
        BASE_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        return False, f"基准落盘失败: {e}"
    return True, f"{len(slim)} 只 / trade_date={payload['trade_date']}"


def load_base():
    """读本地基准。返回 (items, meta)。meta 里带是否过期。"""
    if not BASE_FILE.exists():
        return [], {"ok": False, "reason": "无本地基准"}
    try:
        d = json.loads(BASE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return [], {"ok": False, "reason": f"基准损坏: {e}"}
    items = d.get("items") or []
    fetched = d.get("fetched_at") or ""
    stale = False
    try:
        age = (datetime.now() - datetime.strptime(fetched, "%Y-%m-%d %H:%M:%S")).days
        stale = age > BASE_STALE_DAYS
    except (TypeError, ValueError):
        stale = True
    return items, {"ok": bool(items), "fetched_at": fetched, "stale": stale,
                   "trade_date": d.get("trade_date") or "",
                   "source": d.get("source") or ""}


# ---------------------------------------------------------------- 实时


def fetch_live(tcodes, timeout=12):
    """腾讯行情实时。返回 {code: {...}}；失败返回 {}（不抛，调用方判定 degraded）。"""
    import requests
    if not tcodes:
        return {}
    out = {}
    try:
        r = requests.get(LIVE_URL + ",".join(tcodes), timeout=timeout)
        r.encoding = "gbk"
        txt = r.text
    except Exception:
        return {}
    for line in txt.strip().split(";"):
        if "=" not in line:
            continue
        parts = line.split("=", 1)[1].strip().strip('"').split("~")
        if len(parts) < 40:
            continue
        code = (parts[2] or "").strip()
        if not code:
            continue
        amount_wan = _f(parts[37], 0.0) or 0.0
        vol_lots = _f(parts[6], 0.0) or 0.0
        price = _f(parts[3], 0.0) or 0.0
        avg_px = (amount_wan * 1e4) / (vol_lots * 100.0) if vol_lots > 0 else price
        out[code] = {
            "price": price,
            "prev_close": _f(parts[4], 0.0) or 0.0,
            "pct": _f(parts[32]) if len(parts) > 32 else None,
            "amount_yi": amount_wan / 1e4,
            "vol_lots": vol_lots,
            "outer": _f(parts[7], 0.0) or 0.0,
            "inner": _f(parts[8], 0.0) or 0.0,
            "avg_px": avg_px,
        }
    return out


# ---------------------------------------------------------------- 打分


def grade(ratio, net_buy):
    """按「放量倍数 + 主动净买入」定档。放量但净卖出 → 判为出货，不给高分。"""
    if ratio is None:
        return {"code": "unknown", "label": "无数据", "score": 45,
                "note": "未开盘或基准缺失，按中性处理"}
    if ratio >= 1.20 and net_buy is not None and net_buy < 0:
        return {"code": "distribution", "label": "放量出货", "score": 30,
                "note": "成交额放大但主动净卖出，更像对冲/调仓而非增持"}
    # 放量却拿不到内外盘 → 分不清是增持还是出货，半档处理，不给 strong/buy
    if ratio >= 1.50 and net_buy is None:
        return {"code": "mild_buy", "label": "放量待确认", "score": 50,
                "note": "成交额明显放大但缺内外盘数据，无法确认是主动买入，降档"}
    for code, label, lo, need_pos, score in LEVELS:
        if ratio >= lo and (net_buy is None or not need_pos or net_buy > 0):
            return {"code": code, "label": label, "score": score,
                    "note": "" if code != "mild_buy" else "成交量高于常态但主动买盘不占优"}
    return {"code": "shrink", "label": "缩量观望", "score": 25, "note": ""}


def compute(base_items, live, now=None):
    """算 core / watch 两个池子的合计指标。"""
    now = now or datetime.now()
    hm = now.strftime("%H:%M")
    sess, ratio_ok = _session(now)

    def bucket(group):
        amt = base = 0.0
        net = 0.0
        net_n = 0
        wsum = wpct = 0.0
        rows = []
        used_shape = used_linear = 0
        for x in base_items:
            if (x.get("group") or "core") != group:
                continue
            lv = live.get(x["code"])
            if not lv:
                continue
            prog = _progress(x.get("shape"), hm)
            if prog is None or prog <= 0:
                prog = _linear_progress(hm)
                used_linear += 1
            else:
                used_shape += 1
            b = (x.get("avg20_yi") or 0.0) * prog
            a = lv["amount_yi"]
            # 内外盘缺失时不能把「没有数据」算成「主动净买 0」——那会被误读成中性偏多
            valid_bs = (lv["outer"] + lv["inner"]) > 0
            n = ((lv["outer"] - lv["inner"]) * 100.0 * lv["avg_px"] / 1e8) if valid_bs else None
            amt += a
            base += b
            if n is not None:
                net += n
                net_n += 1
            mc = x.get("mktcap_yi") or 0.0
            p = lv.get("pct")
            if p is None and lv["prev_close"]:
                p = (lv["price"] / lv["prev_close"] - 1.0) * 100.0
            if p is not None and mc > 0:
                wsum += mc
                wpct += mc * p
            rows.append({
                "code": x["code"], "name": x["name"], "index": x["index"],
                "huijin_yi": x.get("huijin_yi"),
                "amount_yi": round(a, 2),
                "net_buy_yi": round(n, 2) if n is not None else None,
                "base_yi": round(b, 2),
                "ratio": round(a / b, 2) if b > 0 else None,
                "progress": round(prog, 3),
                "pct": round(p, 2) if p is not None else None,
                "shares_yi": None,
            })
        ratio = round(amt / base, 2) if (base > 0 and ratio_ok) else None
        net_buy = round(net, 2) if net_n else None
        out = {
            "amount_yi": round(amt, 1),
            "base_yi": round(base, 1),
            "ratio": ratio,
            "net_buy_yi": net_buy,
            "net_cover": net_n,          # 有多少只拿到了内外盘，前端据此标注覆盖度
            "pct": round(wpct / wsum, 2) if wsum > 0 else None,
            "progress": round(base / sum((x.get("avg20_yi") or 0) for x in base_items
                                         if (x.get("group") or "core") == group) , 3)
                        if base > 0 else None,
            "count": len(rows),
            "shape_driven": used_shape,
            "linear_fallback": used_linear,
            "items": sorted(rows, key=lambda r: -(r["amount_yi"] or 0)),
        }
        g = grade(ratio, out["net_buy_yi"])
        out.update({"level_code": g["code"], "level": g["label"],
                    "score": g["score"], "level_note": g["note"]})
        return out

    core = bucket("core")
    watch = bucket("watch")
    return {
        "asof": now.strftime("%H:%M:%S"),
        "session": sess,
        "core": core,
        "watch": watch,
        # 闸门修正：工作台展示与调度器生效共用这一份
        "effect": gate_effect(core.get("level_code")),
    }


# ---------------------------------------------------------------- 单例


class Tracker:
    def __init__(self):
        self._lock = threading.Lock()
        self._snap = {"ok": False, "reason": "未启动"}
        self._started = False
        self._last_live = 0.0
        self._base_refreshed_date = ""

    def refresh(self, force_base=False):
        base_items, bmeta = load_base()
        # 跨日 / 无基准 / 强制 → 拉一次第三方快照
        today = datetime.now().strftime("%Y%m%d")
        need_base = force_base or not base_items or self._base_refreshed_date != today
        if need_base:
            ok, msg = fetch_base()
            if ok:
                self._base_refreshed_date = today
                base_items, bmeta = load_base()
                print(f"[gjd] 基准刷新成功: {msg}")
            else:
                # 降级：保留旧基准；若完全没有基准则本次只报不可用
                print(f"[gjd] 基准刷新失败（沿用本地 {bmeta.get('trade_date') or '无'}）: {msg}")
                if not base_items and bmeta.get("ok") is False:
                    with self._lock:
                        self._snap = {"ok": False, "reason": f"基准不可用且无本地缓存: {msg}",
                                      "asof": datetime.now().strftime("%H:%M:%S")}
                    return self._snap
        live = fetch_live([x["tcode"] for x in base_items])
        computed = compute(base_items, live)
        computed.update({
            "ok": bool(live),
            "reason": "" if live else "腾讯行情取数失败（本轮沿用上一帧）",
            "base": bmeta,
            "base_source": ("thirdparty" if not bmeta.get("stale") else "thirdparty_stale"),
        })
        if not live:
            with self._lock:
                old = dict(self._snap)
            if old.get("ok"):
                old["reason"] = "腾讯行情取数失败，展示上一帧"
                return old
        with self._lock:
            self._snap = computed
        self._last_live = time.time()
        return computed

    def snapshot(self):
        with self._lock:
            return dict(self._snap)

    def start(self):
        if self._started:
            return
        self._started = True

        def loop():
            while True:
                try:
                    self.refresh()
                except Exception as e:
                    print(f"[gjd] 刷新异常: {type(e).__name__}: {e}")
                # 非交易时段降频到 5 分钟，避免白白打腾讯接口
                sess, _ = _session()
                time.sleep(REFRESH_SEC if sess in ("trading", "lunch") else 300)

        threading.Thread(target=loop, name="gjd-etf", daemon=True).start()


_inst = None
_inst_lock = threading.Lock()


def quick_snapshot(timeout=6):
    """给**调度器**用的轻量同步取数（工作台走 Tracker 后台线程，不调这个）。

    与 Tracker 的区别：
      · 不启动线程、不缓存，一次调用一次结果 —— 调度器是短生命周期进程
      · 超时更短（默认 6s）：它跑在派单的关键路径上，宁可拿不到也不能拖住整个时段
      · 任何异常/缺基准/取数失败 → ok=False + effect 走 unknown（**hold，不加不减**）
        绝不因为「取数失败」而收紧仓位 —— 那等于让网络抖动决定实盘仓位
    """
    try:
        items, meta = load_base()
        if not items:
            return {"ok": False, "reason": "无本地基准（gjd_etf_base.json 缺失）",
                    "effect": gate_effect("unknown"), "session": None, "core": {}}
        live = fetch_live([x["tcode"] for x in items], timeout=timeout)
        if not live:
            return {"ok": False, "reason": "腾讯 ETF 行情取数失败",
                    "effect": gate_effect("unknown"), "session": None, "core": {}}
        r = compute(items, live)
        r.update({"ok": True, "reason": "", "base": meta})
        return r
    except Exception as e:       # noqa: BLE001 —— 关键路径必须 fail-open，但要留痕
        return {"ok": False, "reason": f"{type(e).__name__}: {e}",
                "effect": gate_effect("unknown"), "session": None, "core": {}}


def instance():
    global _inst
    with _inst_lock:
        if _inst is None:
            _inst = Tracker()
        return _inst


# ---------------------------------------------------------------- selftest


def _selftest():
    ok = True

    def chk(name, cond, extra=""):
        nonlocal ok
        print(("  PASS " if cond else "  FAIL ") + name + (f" {extra}" if extra else ""))
        if not cond:
            ok = False

    print("[1] 日内进度取档（向下取整，不得取未来档）")
    shape = [["09:35", 0.07], ["09:40", 0.11], ["10:45", 0.40], ["10:50", 0.44], ["15:00", 1.0]]
    chk("10:47 → 0.40", _progress(shape, "10:47") == 0.40, f"got={_progress(shape, '10:47')}")
    chk("10:50 → 0.44", _progress(shape, "10:50") == 0.44)
    chk("09:30 → None（无已过档）", _progress(shape, "09:30") is None)
    chk("15:00 → 1.0", _progress(shape, "15:00") == 1.0)

    print("[2] 降级线性进度")
    chk("11:30 → 0.5", _linear_progress("11:30") == 0.5, f"got={_linear_progress('11:30')}")
    chk("15:00 → 1.0", _linear_progress("15:00") == 1.0)
    chk("09:30 → 0.0", _linear_progress("09:30") == 0.0)

    print("[3] 分档：放量但净卖出必须判出货，不能给高分")
    g = grade(2.5, -8.0)
    chk("2.5× 且净卖出 → 放量出货", g["code"] == "distribution" and g["score"] <= 30, f"got={g}")
    g = grade(2.5, 8.0)
    chk("2.5× 且净买入 → 强力买入 100", g["code"] == "strong_buy" and g["score"] == 100)
    g = grade(1.6, 3.0)
    chk("1.6× 且净买入 → 明显买入 80", g["code"] == "buy" and g["score"] == 80)
    g = grade(0.5, -20.0)
    chk("0.5× → 缩量观望", g["code"] == "shrink")
    chk("None → 无数据中性", grade(None, None)["code"] == "unknown")

    print("[4] compute：合计倍数与净买入（夹具自洽：成交额 = 手数×100×均价，外盘+内盘 = 总手数）")
    # 夹具构造规则，便于手工复核：
    #   vol_lots  = amount_yi*1e8 / (price*100)
    #   outer+inner = vol_lots
    #   net_buy_yi = (outer-inner)*100*price/1e8
    base = [
        {"code": "510300", "tcode": "sh510300", "name": "沪深300ETF", "index": "沪深300",
         "group": "core", "huijin_yi": 735.0, "avg20_yi": 80.0, "mktcap_yi": 1800.0,
         "shape": [["10:45", 0.40], ["15:00", 1.0]]},
        {"code": "159915", "tcode": "sz159915", "name": "创业板ETF", "index": "创业板指",
         "group": "core", "huijin_yi": 170.0, "avg20_yi": 20.0, "mktcap_yi": 400.0,
         "shape": [["10:45", 0.50], ["15:00", 1.0]]},
        {"code": "588000", "tcode": "sh588000", "name": "科创50ETF", "index": "科创50",
         "group": "watch", "huijin_yi": 0.0, "avg20_yi": 100.0, "mktcap_yi": 1000.0,
         "shape": [["10:45", 0.30], ["15:00", 1.0]]},
    ]
    live = {
        # 基准 80×0.40=32 亿；成交 64 亿 → 2.00×
        # 64e8/(4.0×100)=16,000,000 手；外 9,000,000 + 内 7,000,000 = 16,000,000 ✓
        # 净买入 (9e6-7e6)×100×4.0/1e8 = +8.00 亿
        "510300": {"price": 4.0, "prev_close": 4.0, "pct": 0.0, "amount_yi": 64.0,
                   "vol_lots": 16000000.0, "outer": 9000000.0, "inner": 7000000.0,
                   "avg_px": 4.0},
        # 基准 20×0.50=10 亿；成交 15 亿 → 1.50×
        # 15e8/(2.5×100)=6,000,000 手；外 2,000,000 + 内 4,000,000 = 6,000,000 ✓
        # 净买入 (2e6-4e6)×100×2.5/1e8 = -5.00 亿
        "159915": {"price": 2.5, "prev_close": 2.5, "pct": 0.0, "amount_yi": 15.0,
                   "vol_lots": 6000000.0, "outer": 2000000.0, "inner": 4000000.0,
                   "avg_px": 2.5},
        # 基准 100×0.30=30 亿；成交 30 亿 → 1.00×，净买入 0
        "588000": {"price": 1.3, "prev_close": 1.3, "pct": 0.0, "amount_yi": 30.0,
                   "vol_lots": 23076923.0, "outer": 11538461.0, "inner": 11538462.0,
                   "avg_px": 1.3},
    }
    at = datetime(2026, 11, 6, 10, 47)
    res = compute(base, live, now=at)
    core = res["core"]
    chk("core 成交额 79.0（64+15）", core["amount_yi"] == 79.0, f"got={core['amount_yi']}")
    chk("core 基准 42.0（32+10）", core["base_yi"] == 42.0, f"got={core['base_yi']}")
    chk("core 倍数 1.88（79/42）", core["ratio"] == 1.88, f"got={core['ratio']}")
    # 单只倍数也要对：各自用自己的 shape，不是笼统摊 0.42
    r510 = next(r for r in core["items"] if r["code"] == "510300")
    r159 = next(r for r in core["items"] if r["code"] == "159915")
    chk("510300 单独 2.00×", r510["ratio"] == 2.0, f"got={r510['ratio']}")
    chk("159915 单独 1.50×", r159["ratio"] == 1.5, f"got={r159['ratio']}")
    # 净买入：(9e6-7e6)*100*4.0/1e8 = +8.00 ; (2e6-4e6)*100*2.5/1e8 = -5.00 → +3.00
    chk("core 净买入 +3.00（+8.00-5.00）", core["net_buy_yi"] == 3.0,
        f"got={core['net_buy_yi']}")
    chk("watch 不计入 core", core["count"] == 2 and res["watch"]["count"] == 1,
        f"{core['count']}/{res['watch']['count']}")
    chk("core 梯队 = 明显买入", core["level_code"] == "buy", f"got={core['level_code']}")

    print("[4b] 缺内外盘数据：不能把「无数据」当成「净买入 0」")
    live2 = {k: dict(v) for k, v in live.items()}
    for v in live2.values():
        v["outer"] = v["inner"] = 0.0          # 模拟腾讯不返回内外盘
    r2 = compute(base, live2, now=at)["core"]
    chk("净买入 → None（不是 0）", r2["net_buy_yi"] is None, f"got={r2['net_buy_yi']}")
    chk("倍数仍可算（1.88）", r2["ratio"] == 1.88, f"got={r2['ratio']}")
    chk("无净买入数据 → 降档不放行满分", r2["level_code"] == "mild_buy" and r2["score"] < 80,
        f"got={r2['level_code']}/{r2['score']}")

    print("[5] 会话态：盘前不算倍数")
    res2 = compute(base, live, now=datetime(2026, 11, 6, 9, 10))
    chk("盘前 ratio=None", res2["core"]["ratio"] is None and res2["session"] == "preopen",
        f"got={res2['session']} ratio={res2['core']['ratio']}")
    # 盘前无数据 → 闸门不许动（fail-open），否则开盘前就凭空白收紧仓位
    chk("盘前闸门不动（unknown→hold）",
        res2["effect"]["pos_delta"] == 0 and res2["effect"]["pick_delta"] == 0,
        f"got={res2['effect']}")

    print("[6] 闸门修正映射：方向必须与风险一致")
    e = gate_effect("distribution")
    chk("放量出货 → 收紧 -8pp / 只数-1", e["pos_delta"] == -8.0 and e["pick_delta"] == -1
        and e["dir"] == "tighten", f"got={e}")
    e = gate_effect("strong_buy")
    chk("强力买入 → 放宽 +6pp / 只数+1", e["pos_delta"] == 6.0 and e["pick_delta"] == 1
        and e["dir"] == "loosen", f"got={e}")
    chk("未知档 → hold 不加不减", gate_effect("whatever")["dir"] == "hold")
    chk("允许的档位都覆盖到",
        all(k in GATE_EFFECT for k in
            ("strong_buy", "buy", "mild_buy", "normal", "shrink", "distribution", "unknown")))
    # 出货的惩罚必须重于缩量，且正常档为 0
    chk("出货罚 > 缩量罚 > 常态",
        GATE_EFFECT["distribution"]["pos_delta"] < GATE_EFFECT["shrink"]["pos_delta"]
        < GATE_EFFECT["normal"]["pos_delta"])

    print("SELFTEST", "ALL PASS" if ok else "HAS FAIL")
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if _selftest() else 1)

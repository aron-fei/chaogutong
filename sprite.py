# -*- coding: utf-8 -*-
"""短线精灵 · 异动雷达（M7，ADR-0028）

对标通达信 / 大智慧 / 益盟「短线精灵」的**可实现子集**。

通达信原版共 24 类监控项，其中「大笔买入/卖出、机构吃货/吐货、拖拉机单、撤单、新单」
依赖 L2 逐笔委托队列，「有大买盘/大卖盘」依赖五档盘口——腾讯 Level-1 快照（qt.gtimg.cn）
拿不到，故**不做**（不做假数据）。

本引擎用腾讯全A批量快照（约 20s 一帧，5558 只 / 200 一批 ≈ 5.2s）+ 涨停池 + 板块数据
实现下列事件类型：

  🚀 rocket    火箭发射 / 极速拉升   60s 内快速拉升且创当日新高
  🔥 surge     极速拉升（强档）      180s 涨幅 ≥ 阈值
  💧 dive      高台跳水              由上涨状态转为 60s 快速下跌
  🏆 limit_up  封涨停板              新进入涨停池
  💔 open_up   打开涨停              从涨停池跌出（炸板）
  🔥 sector    板块涨速              板块 1 分钟涨幅超阈值
  📊 volratio  量比异动              高量比 + 正在拉升
  ⚡ speed     个股涨速榜（榜单，不产生提醒事件）

采样粒度说明：窗口涨速按**真实时间戳回溯**（不依赖帧间隔），但粒度受限于 20s 轮询，
属「20 秒级」异动，不是逐笔级——这是数据源的物理上限，已在 UI 标注。

级别 level：1 = 关注股（自选/持仓）· 2 = 活跃资金异动 / 涨停炸板 · 3 = 全市场一般异动
"""
import threading
import time
from collections import deque
from datetime import datetime

import config

_SINGLETON = None
_SINGLETON_LOCK = threading.Lock()


def instance():
    """进程内单例（data_fetcher / app 共用同一份事件流）。"""
    global _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is None:
            _SINGLETON = SpriteEngine()
        return _SINGLETON


def _f(v):
    """宽松转 float：腾讯板块榜字段是字符串，含 None/'-' 等。"""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


KIND_META = {
    "rocket":   {"icon": "🚀", "label": "火箭发射", "level": 2},
    "surge":    {"icon": "🔥", "label": "极速拉升", "level": 2},
    "dive":     {"icon": "💧", "label": "高台跳水", "level": 2},
    "limit_up": {"icon": "🏆", "label": "封涨停板", "level": 2},
    "open_up":  {"icon": "💔", "label": "打开涨停", "level": 2},
    "sector":   {"icon": "🔥", "label": "板块涨速", "level": 2},
    "volratio": {"icon": "📊", "label": "量比异动", "level": 3},
    "first_board": {"icon": "🎯", "label": "首板雷达", "level": 1},
}


class SpriteEngine:
    def __init__(self):
        self.lock = threading.RLock()
        self._hist = {}            # code -> deque[(ts, pct, price)]
        self._meta = {}            # code -> {name, price, pct, high, low, amount_yi, vol_ratio, zt_price}
        self._zt_prev = set()      # 上一帧涨停池代码
        self._zt_ever = set()      # 今日曾涨停（用于判定「打开涨停」）
        self._pool_primed = False  # 首帧预热标记（避免重启后存量涨停被当新封板刷屏）
        self._cool = {}            # (kind, code) -> ts
        self._sector_cool = {}     # "hy:名称" -> ts
        self._board_hist = {}      # (type, name) -> deque[(ts, zdf)]
        self._focus_provider = None
        self._focus = set()
        self.events = deque(maxlen=config.SPRITE_EVENTS_MAX)
        self.seq = 0
        self._boards = {"rocket": [], "speed": [], "sector": [],
                        "board_hy": [], "board_gn": [],
                        "limit_up": [], "dive": [], "first_board": [],
                        "first_board_watch": []}   # v2：接近达标但被挡的票（可诊断）
        self._stats = {
            "frames": 0, "last_frame": None, "day": "",
            "events_total": 0, "focus_size": 0, "hist_size": 0, "board_size": 0,
            "last_full_at": None, "last_pool_at": None, "last_sector_at": None,
        }
        self.errors = []
        # 首板雷达 v2：每只股票的双EMA状态 + 连续确认 + 大盘/情绪闸门
        # _fbs[code] = {ts, ema_fast, ema_slow, rise, streak, last_emit, last_reason}
        self._fbs = {}
        self._gate_provider = None
        self._gate = {"allow": True, "reason": "未接入大盘/情绪闸门",
                      "temp": None, "stage": None}
        self._prev_zt_provider = None   # 昨日涨停代码集合（排除连板接力）
        self._prev_zt = set()
        self._prev_zt_day = ""
        self._trend_provider = None     # 趋势题材名集合（L0 结果，仅用于事件标签）
        self._trends = []
        self._fb_stats = {"emitted": 0, "watch": 0, "filtered": {}, "frames": 0}

    # ------------------------------------------------------------------ 工具
    def _err(self, tag, exc):
        msg = f"[{datetime.now():%H:%M:%S}] sprite-{tag}: {type(exc).__name__} {exc}"
        print(msg)
        with self.lock:
            self.errors.append(msg)
            if len(self.errors) > 20:
                self.errors.pop(0)

    def set_focus_provider(self, fn):
        """注入「关注股」集合提供函数（自选股池 + 持仓），由 app.py 传入。"""
        self._focus_provider = fn

    def set_market_gate_provider(self, fn):
        """注入大盘/情绪闸门提供函数（返回 {"allow","reason","temp","stage"}），由 app.py 传入。
        首板雷达仅在 allow=True 时发声，避免在冰点/退潮/大跌中无脑接力。"""
        self._gate_provider = fn

    def _refresh_gate(self):
        if not self._gate_provider:
            return
        try:
            g = self._gate_provider() or {}
            with self.lock:
                self._gate = {
                    "allow": bool(g.get("allow", True)),
                    "reason": g.get("reason", ""),
                    "temp": g.get("temp"),
                    "stage": g.get("stage"),
                }
        except Exception as e:
            self._err("gate", e)

    @staticmethod
    def _ema_update(state, price, dt, tau):
        """增量指数移动平均：state 为 None 时以 price 初始化；返回 (ema, state)。
        alpha = 1 - exp(-dt/tau)，对长间隔自动收敛、短间隔平滑。"""
        if state is None or not (isinstance(state, (int, float)) and state > 0):
            return price, price
        alpha = 1.0 - pow(2.718281828459045, -max(dt, 0.1) / max(tau, 1))
        alpha = max(0.01, min(0.6, alpha))
        ema = state + alpha * (price - state)
        return ema, ema

    # ------------------------------------------------------------------
    # 首板雷达 v2 · L2 临板首板（2026-11-05）
    # 用户原则：只放「当天第一次涨停」+「即将涨停」+「涨停意愿特别强」的票，
    # 不再在 +3~4% 早段异动就推。以下所有因子都来自真实快照字段。
    # ------------------------------------------------------------------
    def set_prev_zt_provider(self, fn):
        """注入「昨日涨停代码集合」提供函数，用于排除连板接力（非当天第一次涨停）。"""
        self._prev_zt_provider = fn

    def set_trend_provider(self, fn):
        """注入 L0 趋势题材名列表，仅用于给 L2 事件打题材标签（不参与硬门槛）。"""
        self._trend_provider = fn

    def _refresh_prev_zt(self):
        day = datetime.now().strftime("%Y-%m-%d")
        if not self._prev_zt_provider or (self._prev_zt_day == day and self._prev_zt):
            return
        if time.time() - getattr(self, "_prev_zt_at", 0) < 300:
            return
        self._prev_zt_at = time.time()
        try:
            codes = self._prev_zt_provider() or set()
            with self.lock:
                self._prev_zt = {str(c).strip().zfill(6) for c in codes}
                self._prev_zt_day = day if codes else ""
            print(f"[first_board] 昨日涨停基线 {len(self._prev_zt)} 只（排除连板接力）")
        except Exception as e:
            self._err("prev_zt", e)

    def _refresh_trends(self):
        if not self._trend_provider or time.time() - getattr(self, "_trend_at", 0) < 30:
            return
        self._trend_at = time.time()
        try:
            self._trends = list(self._trend_provider() or [])[:30]
        except Exception as e:
            self._err("trend", e)

    @staticmethod
    def _board_progress(rec):
        """板进度 = (现价-昨收)/(涨停价-昨收)。归一化后自动适配 10/20/30/5% 涨停。"""
        price, prev, zt = _f(rec.get("price")), _f(rec.get("prev_close")), _f(rec.get("zt_price"))
        if price is None or prev is None or zt is None or zt <= prev or price <= 0:
            return None
        return max(0.0, min(1.2, (price - prev) / (zt - prev)))

    @staticmethod
    def _fb_intent(rec, sp1, price, high):
        """封板意愿评分（0~100）：只用腾讯 qt 真实五档/委差/外盘/均价字段。

        返回 (score, coverage, parts, missing)。缺失因子不参与计分并按可用权重
        归一化，避免「数据缺失 = 自动扣分」造成误判；coverage 过低时调用方拒绝推送。
        """
        w = dict(getattr(config, "FB_INTENT_W", {}))
        parts, missing = {}, []

        def put(k, val):
            if val is None:
                missing.append(k)
            else:
                parts[k] = max(0.0, min(1.0, float(val)))

        wc = _f(rec.get("weicha"))
        b5 = _f(rec.get("bid5_vol")) or 0.0
        a5 = _f(rec.get("ask5_vol")) or 0.0
        put("weicha", None if (wc is None or b5 + a5 <= 0) else wc / (b5 + a5) * 2.0)

        b1, a1 = _f(rec.get("bid1_vol")), _f(rec.get("ask1_vol"))
        put("ask_thin", None if (b1 is None and a1 is None)
            else 1.0 - ((a1 or 0.0) / max((a1 or 0.0) + (b1 or 0.0), 1.0)))

        outer, inner = _f(rec.get("outer")) or 0.0, _f(rec.get("inner")) or 0.0
        put("bid_stack", None if (b1 is None or outer + inner <= 0)
            else min((b1 or 0.0) / (outer + inner) * 3.0, 1.0))
        put("outer", None if outer + inner <= 0
            else (outer / (outer + inner) - 0.4) / 0.4)
        put("new_high", None if not high else (1.0 if price >= high * 0.998 else 0.0))
        avgp = _f(rec.get("avg_price"))
        put("above_avg", None if avgp is None else (1.0 if price > avgp else 0.0))
        put("sp1", None if sp1 is None else min(sp1 / 0.8, 1.0))

        total_w = sum(w.values()) or 1.0
        avail_w = sum(w.get(k, 0) for k in parts)
        coverage = avail_w / total_w
        score = (100.0 * sum(w.get(k, 0) * v for k, v in parts.items()) / avail_w
                 if avail_w > 0 else 0.0)
        return (round(score, 1), round(coverage, 2),
                {k: round(v, 2) for k, v in parts.items()}, missing)

    @staticmethod
    def _fb_quality(rec, rise, sp2, impulse, trend_up, breakout):
        """趋势质量分（0~100）：v1 首板雷达的因子集合，改成加权而非 AND 硬链。"""
        w = dict(getattr(config, "FB_QUALITY_W", {}))
        vr = _f(rec.get("vol_ratio"))
        if vr is not None and 1.30 <= vr <= 1.80:
            gentle = 1.0
        elif vr is not None and 1.00 <= vr <= 2.50:
            gentle = 0.5
        else:
            gentle = 0.0
        parts = {
            "ma_rising": min(rise / max(config.SPRITE_FB_RISE_FRAMES, 1), 1.0),
            "trend_up": 1.0 if trend_up else 0.0,
            "breakout": 1.0 if breakout else 0.0,
            "impulse": min(max(impulse or 0.0, 0.0) /
                           max(getattr(config, "FB_IMPULSE_MIN", 3.0) * 1.5, 0.01), 1.0),
            "gentle_vol": gentle,
            "sp3": min(max(sp2 or 0.0, 0.0) / 2.0, 1.0),
        }
        score = sum(w.get(k, 0) * v for k, v in parts.items())
        return round(score, 1), {k: round(v, 2) for k, v in parts.items()}

    def _fb_bump(self, reason):
        with self.lock:
            f = self._fb_stats["filtered"]
            f[reason] = f.get(reason, 0) + 1

    def _fb_theme_tag(self, code):
        """给临板事件打「趋势题材」标签：优先取 L0 选中的题材，否则取首个所属题材。"""
        try:
            import theme_board
            names = theme_board.instance().themes_for(code)
            for n in names:
                if n in self._trends:
                    return n
            return names[0] if names else ""
        except Exception as e:
            self._err("theme_tag", e)
            return ""

    def _refresh_focus(self):
        if not self._focus_provider:
            return
        try:
            codes = self._focus_provider() or set()
            with self.lock:
                self._focus = set(str(c) for c in codes)
                self._stats["focus_size"] = len(self._focus)
        except Exception as e:
            self._err("focus", e)

    def _cooled(self, kind, code, ts, cooldown=None):
        """同 (kind, code) 在冷却期内不重复报。返回 True = 应跳过。"""
        cd = cooldown or config.SPRITE_EVENT_COOLDOWN
        key = (kind, code)
        last = self._cool.get(key)
        if last is not None and ts - last < cd:
            return True
        self._cool[key] = ts
        return False

    def _prune_cool(self, ts):
        if len(self._cool) < 4000:
            return
        cut = ts - max(config.SPRITE_EVENT_COOLDOWN, config.SPRITE_SECTOR_COOLDOWN) * 2
        self._cool = {k: v for k, v in self._cool.items() if v >= cut}

    def _emit(self, kind, ts, code="", name="", price=None, pct=None,
              sp=None, window=None, amount_yi=None, vol_ratio=None,
              text="", level=None, extra=None):
        meta = KIND_META.get(kind, {"icon": "•", "label": kind, "level": 3})
        if level is None:
            level = meta["level"]
            if code and code in self._focus:
                level = 1
            elif code and (amount_yi or 0) < config.SPRITE_HOT_AMOUNT:
                level = 3
        self.seq += 1
        ev = {
            "id": self.seq,
            "ts": round(ts, 2),
            "t": datetime.fromtimestamp(ts).strftime("%H:%M:%S"),
            "kind": kind,
            "icon": meta["icon"],
            "label": meta["label"],
            "level": level,
            "code": code,
            "name": name,
            "price": price,
            "pct": pct,
            "sp": sp,
            "window": window,
            "amount_yi": amount_yi,
            "vol_ratio": vol_ratio,
            "focus": bool(code and code in self._focus),
            "text": text,
        }
        if extra:
            ev.update(extra)
        self.events.append(ev)
        self._stats["events_total"] += 1
        return ev

    @staticmethod
    def _lookup(q, target):
        """在 (ts, pct) 序列里找时间上最接近 target 的通胀前值；返回 pct 或 None。"""
        best = None
        for pt, pv in q:
            if pt <= target + 8:
                best = pv
            else:
                break
        return best

    @staticmethod
    def _win_sp(q, ts, window):
        """从历史序列回溯 window 秒的涨跌幅差（真实时间戳，不依赖帧间隔）。"""
        target = ts - window
        best = None
        for pt, ppct, _pprice in q:
            if pt <= target + 8:
                best = (pt, ppct)
            else:
                break
        if best is None:
            return None
        if ts - best[0] > window * 1.8:
            return None
        return best

    def _rollover(self):
        """跨交易日重置：清空昨日涨停基线/冷却/板块历史（新日首帧需重新预热）。"""
        day = datetime.now().strftime("%Y-%m-%d")
        with self.lock:
            if self._stats["day"] == day:
                return False
            self._stats["day"] = day
            self._zt_ever.clear()
            self._zt_prev.clear()
            self._pool_primed = False
            self._cool.clear()
            self._sector_cool.clear()
            self._board_hist.clear()
            # v2：跨日清空首板确认状态 / 统计 / 昨日涨停基线（新日重拉）
            self._fbs.clear()
            self._fb_stats = {"emitted": 0, "watch": 0, "filtered": {}, "frames": 0}
            self._prev_zt.clear()
            self._prev_zt_day = ""
            return True

    # ------------------------------------------------------- 全A帧（个股事件）
    def feed_full(self, rows, ts=None, in_trading=True):
        """全A快照帧：维护个股历史 + 生成 火箭发射/极速拉升/高台跳水/量比异动 + 涨速榜。"""
        if not rows:
            return
        ts = ts or time.time()
        try:
            self._refresh_focus()
            self._refresh_gate()
            self._refresh_prev_zt()
            self._refresh_trends()
            self._rollover()
            speed_list, dive_list, fb_list, fb_watch = [], [], [], []
            with self.lock:
                self._fb_stats["frames"] += 1
            w1, w2 = (config.SPRITE_WINDOWS + [180])[:2]
            for rec in rows:
                code = rec.get("code")
                if not code:
                    continue
                pct = rec.get("pct")
                price = rec.get("price")
                if pct is None or price is None:
                    continue
                with self.lock:
                    q = self._hist.get(code)
                    if q is None:
                        q = self._hist[code] = deque(maxlen=config.SPRITE_HIST_MAXLEN)
                    q.append((ts, pct, price))
                    while q and ts - q[0][0] > config.SPRITE_HIST_SECONDS:
                        q.popleft()
                    self._meta[code] = {
                        "name": rec.get("name", ""), "price": price, "pct": pct,
                        "high": rec.get("high"), "low": rec.get("low"),
                        "amount_yi": rec.get("amount_yi"),
                        "vol_ratio": rec.get("vol_ratio"),
                        "zt_price": rec.get("zt_price"),
                    }
                    if not in_trading:
                        continue
                    b1 = self._win_sp(q, ts, w1)
                    b2 = self._win_sp(q, ts, w2)
                sp1 = round(pct - b1[1], 2) if b1 else None
                sp2 = round(pct - b2[1], 2) if b2 else None
                name = rec.get("name", "")
                amount = rec.get("amount_yi") or 0
                vr = rec.get("vol_ratio")
                high = rec.get("high")
                # ---- 榜单候选（成交额过滤冷门票）----
                if amount >= config.SPRITE_ROCKET_MIN_AMOUNT and sp1 is not None:
                    speed_list.append({
                        "code": code, "name": name, "price": price, "pct": pct,
                        "sp": sp1, "sp_180": sp2, "amount_yi": amount,
                        "vol_ratio": vr, "focus": code in self._focus,
                    })
                # ---- 🚀 火箭发射（60s 档）/ 🔥 极速拉升（180s 强档，独立判定）----
                try:
                    new_high = bool(high) and price >= high * 0.999
                    liquid = amount >= config.SPRITE_ROCKET_MIN_AMOUNT
                    not_limit = pct < config.SPRITE_ROCKET_MAX_PCT
                    if sp1 is not None and sp1 >= config.SPRITE_ROCKET_PCT and liquid and not_limit:
                        if not self._cooled("rocket", code, ts):
                            tip = "创当日新高" if new_high else "快速拉升"
                            self._emit(
                                "rocket", ts, code, name, price, pct, sp1, w1, amount, vr,
                                f"{w1//60}分钟 +{sp1:.2f}% · {tip} · 额{amount:.1f}亿")
                    if new_high and sp2 is not None and sp2 >= config.SPRITE_ROCKET_PCT_180 \
                            and liquid and not_limit:
                        if not self._cooled("surge", code, ts):
                            self._emit(
                                "surge", ts, code, name, price, pct, sp2, w2, amount, vr,
                                f"{w2//60}分钟 +{sp2:.2f}%（强档拉升 · 创当日新高）· 额{amount:.1f}亿")
                    # ---- 💧 高台跳水：由上涨转快速下跌 ----
                    if sp2 is not None and sp2 <= config.SPRITE_DIVE_PCT:
                        was_up = (pct - sp2) >= 3.0        # 3 分钟前还在 +3% 以上
                        if was_up and not self._cooled("dive", code, ts):
                            dive_list.append({
                                "code": code, "name": name, "price": price, "pct": pct,
                                "sp": sp2, "amount_yi": amount, "focus": code in self._focus,
                            })
                            self._emit(
                                "dive", ts, code, name, price, pct, sp2, w2, amount, vr,
                                f"{w2//60}分钟 {sp2:.2f}%（自 {(pct - sp2):.1f}% 高位跳水）· 额{amount:.1f}亿")
                    # ---- 📊 量比异动 ----
                    if vr and vr >= 3.0 and sp1 is not None and sp1 >= 0.8 \
                            and amount >= config.SPRITE_HOT_AMOUNT \
                            and not self._cooled("volratio", code, ts):
                        self._emit(
                            "volratio", ts, code, name, price, pct, sp1, w1, amount, vr,
                            f"量比 {vr:.1f} + {w1//60}分钟 {sp1:+.2f}% · 额{amount:.1f}亿")
                except Exception as e:
                    self._err("emit", e)
                # ---- 🎯 首板雷达 v2 · L2 临板首板（打板前最后一脚，唯一强播报）----
                # 用户反馈：v1 推过 4 次都是 +3~4%，买了就跌。根因是 v1 用
                # 「涨幅≥1.5% + 3分钟涨速>3%」= 早段异动触发。v2 把触发点推到
                # 「板进度 ≥0.85」，并要求真实五档盘口的封板意愿极强 + 连续确认。
                if (config.SPRITE_FB_ENABLED and getattr(config, "FB_V2_ENABLED", False)
                        and in_trading):
                    try:
                        s = self._fbs.get(code) or {}
                        dt = (ts - s["ts"]) if s.get("ts") else w2
                        ef_new = self._ema_update(s.get("ema_fast"), price, dt,
                                                  config.SPRITE_FB_EMA_FAST)[1]
                        es_new = self._ema_update(s.get("ema_slow"), price, dt,
                                                  config.SPRITE_FB_EMA_SLOW)[1]
                        rise = ((s.get("rise", 0) + 1)
                                if (s and es_new > s.get("ema_slow", 0)) else 0)
                        state = {"ts": ts, "ema_fast": ef_new, "ema_slow": es_new,
                                 "rise": rise, "streak": 0,
                                 "last_emit": s.get("last_emit", 0), "last_reason": ""}
                        prog = self._board_progress(rec)
                        near_lo = getattr(config, "FB_NEAR_PROGRESS", 0.85)
                        near_hi = getattr(config, "FB_NEAR_PROGRESS_MAX", 0.995)
                        # 非临板区（含 +3~4% 早段）只更新 EMA 状态，不打分、不推
                        if prog is not None and near_lo <= prog < near_hi:
                            nm = str(name or "").upper().replace(" ", "")
                            vr_ = _f(rec.get("vol_ratio"))
                            bad = None
                            if code in self._zt_ever:
                                bad = "今日已封过板"
                            elif code in self._zt_prev:
                                bad = "当前已封板"
                            elif code in self._prev_zt:
                                bad = "昨日涨停(连板接力)"
                            elif "ST" in nm or "退" in nm:
                                bad = "ST/退市"
                            elif nm.startswith(("N", "C")):
                                bad = "新股"
                            elif amount < config.SPRITE_FB_MIN_AMOUNT:
                                bad = "成交额不足"
                            elif vr_ is not None and vr_ > getattr(config, "FB_VOL_HARD_MAX", 6.0):
                                bad = "巨量出货"
                            if bad:
                                self._fb_bump(bad)
                                state["last_reason"] = bad
                            else:
                                intent, coverage, iparts, missing = self._fb_intent(
                                    rec, sp1, price, high)
                                lo = None
                                for pt, ppct, _pp in (self._hist.get(code) or ()):
                                    if ts - pt <= w2:
                                        lo = ppct if lo is None else min(lo, ppct)
                                impulse = (pct - lo) if lo is not None else None
                                op = _f(rec.get("open"))
                                pcl = _f(rec.get("prev_close"))
                                avgp = _f(rec.get("avg_price"))
                                ma_rising = rise >= config.SPRITE_FB_RISE_FRAMES
                                trend_up = (price > es_new
                                            and (op is None or price > op)
                                            and (pcl is None or price > pcl)
                                            and (avgp is None or price > avgp))
                                breakout = (bool(high) and price >= high * 0.99
                                            and ef_new > es_new)
                                quality, qparts = self._fb_quality(
                                    rec, rise, sp2, impulse, trend_up, breakout)
                                total = round(0.6 * intent + 0.4 * quality, 1)
                                gate_ok = self._gate.get("allow", True)
                                ok = (intent >= config.FB_INTENT_MIN
                                      and quality >= config.FB_QUALITY_MIN
                                      and total >= config.FB_TOTAL_MIN
                                      and coverage >= config.FB_INTENT_COVERAGE_MIN
                                      and gate_ok)
                                if ok:
                                    state["streak"] = int(s.get("streak", 0)) + 1
                                else:
                                    why = ("意愿不足" if intent < config.FB_INTENT_MIN else
                                           "趋势质量不足" if quality < config.FB_QUALITY_MIN else
                                           "综合分不足" if total < config.FB_TOTAL_MIN else
                                           "盘口数据不足" if coverage < config.FB_INTENT_COVERAGE_MIN else
                                           "闸门限制")
                                    state["last_reason"] = why
                                    self._fb_bump(why)
                                common = {
                                    "code": code, "name": name, "price": price,
                                    "pct": pct, "amount_yi": amount, "vol_ratio": vr_,
                                    "board_progress": round(prog, 3),
                                    "intent": intent, "quality": quality,
                                    "total": total, "coverage": coverage,
                                    "intent_parts": iparts, "quality_parts": qparts,
                                    "impulse": (round(impulse, 2) if impulse is not None else None),
                                    "sp": sp2, "sp1": sp1, "streak": state["streak"],
                                    "reason": state["last_reason"] or "达标",
                                    "missing": missing, "focus": code in self._focus,
                                }
                                need = int(getattr(config, "FB_CONFIRM_FRAMES", 2))
                                cap = int(getattr(config, "FB_MAX_PER_FRAME", 3))
                                if state["streak"] >= need and gate_ok and len(fb_list) < cap:
                                    if not self._cooled("first_board", code, ts,
                                                        getattr(config, "FB_COOLDOWN", 300)):
                                        ztp = _f(rec.get("zt_price"))
                                        dist = ((ztp - price) / price * 100.0
                                                if ztp and price else 0.0)
                                        highlights = []
                                        if iparts.get("ask_thin", 0) >= 0.8:
                                            highlights.append("卖压清空")
                                        if iparts.get("weicha", 0) >= 0.6:
                                            highlights.append("委买压倒")
                                        if iparts.get("bid_stack", 0) >= 0.6:
                                            highlights.append("买盘承接厚")
                                        if iparts.get("outer", 0) >= 0.6:
                                            highlights.append("主动买盘强")
                                        if iparts.get("new_high", 0) >= 0.9:
                                            highlights.append("创当日新高")
                                        if (impulse or 0) >= getattr(config, "FB_IMPULSE_MIN", 3.0):
                                            highlights.append(f"{w2 // 60}分钟拉升{impulse:.1f}%")
                                        tag = self._fb_theme_tag(code)
                                        if tag:
                                            highlights.append(f"趋势题材 {tag}")
                                        hl = "、".join(highlights[:4]) or "多因子共振"
                                        fb_text = (f"临板首板 {name} 涨{pct:+.2f}% "
                                                   f"距涨停{dist:.2f}% 封板意愿{intent:.0f}分 "
                                                   f"趋势质量{quality:.0f}分 · {hl}")
                                        voice = (f"首板临门，{name}，涨{pct:.1f}%，"
                                                 f"距涨停{dist:.1f}%，封板意愿{intent:.0f}分，"
                                                 f"{hl}。注意打板风险")
                                        self._emit(
                                            "first_board", ts, code, name, price, pct,
                                            sp2, w2, amount, vr_, fb_text, level=1,
                                            extra={**common, "dist_pct": round(dist, 2),
                                                   "highlights": highlights,
                                                   "voice": voice, "theme": tag,
                                                   "gate": self._gate.get("reason", "")})
                                        fb_list.append({**common, "sp": sp2})
                                        state["last_emit"] = ts
                                        state["streak"] = 0
                                        with self.lock:
                                            self._fb_stats["emitted"] += 1
                                else:
                                    fb_watch.append(common)
                        self._fbs[code] = state
                    except Exception as e:
                        self._err("first_board", e)
            # ---- 榜单落库 ----
            speed_list.sort(key=lambda x: -(x["sp"] or 0))
            dive_list.sort(key=lambda x: (x["sp"] or 0))
            rocket_rank = [x for x in speed_list if (x["sp"] or 0) > 0][:config.SPRITE_SPEED_TOP]
            with self.lock:
                self._boards["speed"] = speed_list[:config.SPRITE_SPEED_TOP]
                self._boards["rocket"] = rocket_rank
                self._boards["dive"] = dive_list[:config.SPRITE_SPEED_TOP]
                self._boards["first_board"] = sorted(
                    fb_list, key=lambda x: (-x["total"], -x["intent"]))[:config.SPRITE_SPEED_TOP]
                self._boards["first_board_watch"] = sorted(
                    fb_watch, key=lambda x: (-x["total"], -x["board_progress"]))[:12]
                self._fb_stats["watch"] = len(fb_watch)
                self._stats["frames"] += 1
                self._stats["last_frame"] = datetime.now().strftime("%H:%M:%S")
                self._stats["last_full_at"] = datetime.now().strftime("%H:%M:%S")
                self._stats["hist_size"] = len(self._hist)
                # 清理长期无更新的股票
                dead = [c for c, q in self._hist.items()
                        if not q or ts - q[-1][0] > config.SPRITE_HIST_SECONDS * 2]
                for c in dead:
                    self._hist.pop(c, None)
                    self._meta.pop(c, None)
                    self._fbs.pop(c, None)
                self._prune_cool(ts)
        except Exception as e:
            self._err("feed_full", e)

    # --------------------------------------------------- 涨停池帧（封板/炸板）
    def feed_pools(self, zt, zb=None, ts=None, in_trading=True):
        """涨停池帧：封涨停板 / 打开涨停事件；同时刷新涨停榜。"""
        ts = ts or time.time()
        try:
            self._rollover()
            zt = zt or []
            zb = zb or []
            zt_codes = {s.get("code") for s in zt if s.get("code")}
            zb_map = {s.get("code"): s for s in zb if s.get("code")}
            with self.lock:
                prev = set(self._zt_prev)
                primed = self._pool_primed
            new_codes = zt_codes - prev
            gone_codes = prev - zt_codes
            # 首帧预热：进程刚启动时 prev 为空，若不跳过会把当日存量涨停全部当成
            # 「新封板」刷屏（实测重启即产生 55 条）。首帧只建基线，不产生事件。
            if in_trading and primed:
                name_map = {s.get("code"): s for s in zt}
                for code in new_codes:
                    s = name_map.get(code) or {}
                    if not self._cooled("limit_up", code, ts):
                        self._emit(
                            "limit_up", ts, code, s.get("name", ""), None, s.get("pct"),
                            None, None, s.get("amount_yi"), None,
                            f"{s.get('lianban', 1)}板封板 · {s.get('industry', '')} · 额{s.get('amount_yi') or 0:.1f}亿",
                            level=1 if code in self._focus else 2)
                for code in gone_codes:
                    s = zb_map.get(code) or self._meta.get(code) or {}
                    if not self._cooled("open_up", code, ts):
                        self._emit(
                            "open_up", ts, code, s.get("name", ""), s.get("price"), s.get("pct"),
                            None, None, s.get("amount_yi"), None,
                            "炸板（打开涨停）" if code in zb_map else "跌出涨停池",
                            level=1 if code in self._focus else 2)
            with self.lock:
                self._zt_prev = zt_codes
                self._zt_ever |= zt_codes
                self._pool_primed = True
                self._boards["limit_up"] = sorted(
                    zt, key=lambda x: (-(x.get("lianban") or 0), str(x.get("first_time") or ""))
                )[:config.SPRITE_SPEED_TOP]
                self._stats["last_pool_at"] = datetime.now().strftime("%H:%M:%S")
        except Exception as e:
            self._err("feed_pools", e)

    # ------------------------------------------------- 板块榜帧（板块/概念涨速）
    def feed_boards(self, boards, ts=None, in_trading=True):
        """腾讯板块榜帧：自算 5/15 分钟涨速 + 板块涨速事件 + 板块涨速榜。

        数据源实测（2026-09-14）：https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank
          board_type=hy（31 个一级行业）/ gn（概念，前 N）
          字段：name/zdf(今日%)/speed(腾讯官方涨速)/lb(量比)/hsl(换手)/
                turnover(成交额,万元)/zljlr(主力净流入,万元)/lzg(领涨股)
        替代原 THS 板块涨速（stock_board_industry_summary_ths 盘中 pct 不刷新，
        1 分钟差分恒为 0 被置空 → 板块涨速长期显示「--」）。
        """
        ts = ts or time.time()
        try:
            boards = boards or []
            ranked = []
            for b in boards:
                name = b.get("name")
                zdf = b.get("zdf")
                if not name or zdf is None:
                    continue
                btype = b.get("type") or "hy"
                key = (btype, name)
                with self.lock:
                    q = self._board_hist.get(key)
                    if q is None:
                        q = self._board_hist[key] = deque(maxlen=120)
                    q.append((ts, zdf))
                    while q and ts - q[0][0] > config.BOARD_HIST_SECONDS:
                        q.popleft()
                    base5 = self._lookup(q, ts - 300)
                    base15 = self._lookup(q, ts - 900)
                sp5 = round(zdf - base5, 2) if base5 is not None else None
                sp15 = round(zdf - base15, 2) if base15 is not None else None
                official = _f(b.get("speed"))
                row = {
                    "type": btype, "name": name, "zdf": zdf,
                    "sp5": sp5, "sp15": sp15, "official": official,
                    "lb": _f(b.get("lb")), "hsl": _f(b.get("hsl")),
                    # board_rank 已把 turnover/zljlr(万元) 折算为亿元
                    "amount_yi": _f(b.get("amount_yi")) or 0.0,
                    "netflow_yi": _f(b.get("netflow_yi")) or 0.0,
                    "lead": b.get("lead") or "", "lead_pct": _f(b.get("lead_pct")),
                }
                ranked.append(row)
                if not in_trading:
                    continue
                thr = config.SPRITE_BOARD_SPEED_GN if btype == "gn" else config.SPRITE_BOARD_SPEED_HY
                thr_off = config.SPRITE_BOARD_OFFICIAL_GN if btype == "gn" \
                    else config.SPRITE_BOARD_OFFICIAL_HY
                # 只报「当日为正」的板块：下跌板块的短时技术性反弹噪声极大
                up_day = zdf is not None and zdf >= config.SPRITE_BOARD_MIN_ZDF
                hot = up_day and ((sp5 is not None and sp5 >= thr) or
                                  (official is not None and official >= thr_off))
                if not hot:
                    continue
                ck = f"{btype}:{name}"
                last = self._sector_cool.get(ck)
                if last is not None and ts - last < config.SPRITE_SECTOR_COOLDOWN:
                    continue
                self._sector_cool[ck] = ts
                tag = "概念" if btype == "gn" else "行业"
                if sp5 is not None:
                    head = f"{tag} 5分钟 {sp5:+.2f}%"
                    parts = [head, f"今日 {zdf:+.2f}%"]
                    if official is not None:
                        parts.append(f"官方涨速 {official:+.2f}%")
                else:
                    parts = [f"{tag}官方涨速 {(official or 0):+.2f}%", f"今日 {zdf:+.2f}%"]
                if sp15 is not None:
                    parts.append(f"15分钟 {sp15:+.2f}%")
                if row["lead"]:
                    parts.append(f"领涨 {row['lead']}")
                self._emit("sector", ts, "", name, None, zdf, sp5, 300, row["amount_yi"],
                           row["lb"], " · ".join(parts), level=2,
                           extra={"board_type": btype, "official": official,
                                  "sp15": sp15, "lead": row["lead"],
                                  "netflow_yi": row["netflow_yi"]})
            # 排序：自算 5 分钟涨速优先，未满 5 分钟历史时退回腾讯官方涨速
            def _bkey(x):
                return x["sp5"] if x["sp5"] is not None else \
                    (x["official"] if x["official"] is not None else -99)
            ranked.sort(key=lambda x: -_bkey(x))
            with self.lock:
                self._boards["board_hy"] = [x for x in ranked if x["type"] == "hy"][:config.SPRITE_BOARD_TOP]
                self._boards["board_gn"] = [x for x in ranked if x["type"] == "gn"][:config.SPRITE_BOARD_TOP]
                self._boards["sector"] = ranked[:config.SPRITE_BOARD_TOP]
                self._stats["last_sector_at"] = datetime.now().strftime("%H:%M:%S")
                self._stats["board_size"] = len(ranked)
        except Exception as e:
            self._err("feed_boards", e)

    # ------------------------------------------------------------------ 快照
    def snapshot(self, since=0, limit=200):
        with self.lock:
            evs = [e for e in self.events if e["id"] > since][-limit:]
            return {
                "seq": self.seq,
                "generated_at": datetime.now().strftime("%H:%M:%S"),
                "events": evs,
                "boards": self._boards,
                "stats": dict(self._stats),
                "focus_count": len(self._focus),
                "sampling_sec": config.FULLMarket_INTERVAL,
                "gate": dict(self._gate),
                "thresholds": {
                    "rocket_pct": config.SPRITE_ROCKET_PCT,
                    "rocket_180": config.SPRITE_ROCKET_PCT_180,
                    "dive_pct": config.SPRITE_DIVE_PCT,
                    "board_hy": config.SPRITE_BOARD_SPEED_HY,
                    "board_gn": config.SPRITE_BOARD_SPEED_GN,
                    "board_official_hy": config.SPRITE_BOARD_OFFICIAL_HY,
                    "board_official_gn": config.SPRITE_BOARD_OFFICIAL_GN,
                    "board_min_zdf": config.SPRITE_BOARD_MIN_ZDF,
                    "windows": config.SPRITE_WINDOWS,
                    "fb": {
                        "enabled": config.SPRITE_FB_ENABLED,
                        "min_pct": config.SPRITE_FB_MIN_PCT,
                        "max_pct": config.SPRITE_FB_MAX_PCT,
                        "speed": config.SPRITE_FB_SPEED,
                        "vol_min": config.SPRITE_FB_VOL_MIN,
                        "vol_max": config.SPRITE_FB_VOL_MAX,
                        "vol_giant": config.SPRITE_FB_VOL_GIANT,
                        "ema_fast": config.SPRITE_FB_EMA_FAST,
                        "ema_slow": config.SPRITE_FB_EMA_SLOW,
                        "rise_frames": config.SPRITE_FB_RISE_FRAMES,
                        "break_pct": config.SPRITE_FB_BREAK_PCT,
                        "voice": config.SPRITE_FB_VOICE,
                        "gate_temp": config.SPRITE_FB_GATE_TEMP,
                        "gate_stages": list(config.SPRITE_FB_GATE_STAGES),
                    },
                    # v2 实际生效的口径（前端说明条与诊断用，避免展示过期的 v1 阈值）
                    "fb_v2": {
                        "enabled": getattr(config, "FB_V2_ENABLED", False),
                        "near_progress": getattr(config, "FB_NEAR_PROGRESS", 0.85),
                        "near_progress_max": getattr(config, "FB_NEAR_PROGRESS_MAX", 0.995),
                        "intent_min": getattr(config, "FB_INTENT_MIN", 68),
                        "quality_min": getattr(config, "FB_QUALITY_MIN", 55),
                        "total_min": getattr(config, "FB_TOTAL_MIN", 66),
                        "coverage_min": getattr(config, "FB_INTENT_COVERAGE_MIN", 0.6),
                        "confirm_frames": getattr(config, "FB_CONFIRM_FRAMES", 2),
                        "impulse_min": getattr(config, "FB_IMPULSE_MIN", 3.0),
                        "vol_hard_max": getattr(config, "FB_VOL_HARD_MAX", 6.0),
                        "cooldown": getattr(config, "FB_COOLDOWN", 300),
                        "intent_w": dict(getattr(config, "FB_INTENT_W", {})),
                        "quality_w": dict(getattr(config, "FB_QUALITY_W", {})),
                    },
                },
                "fb": {
                    "stats": dict(self._fb_stats),
                    "prev_zt_count": len(self._prev_zt),
                    "prev_zt_day": self._prev_zt_day,
                    "trends": list(self._trends)[:12],
                    "tracked": len(self._fbs),
                },
                "errors": self.errors[-5:],
            }

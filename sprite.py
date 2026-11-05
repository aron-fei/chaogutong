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
                        "limit_up": [], "dive": [], "first_board": []}
        self._stats = {
            "frames": 0, "last_frame": None, "day": "",
            "events_total": 0, "focus_size": 0, "hist_size": 0, "board_size": 0,
            "last_full_at": None, "last_pool_at": None, "last_sector_at": None,
        }
        self.errors = []
        # 首板雷达：每只股票的双EMA状态 + 大盘/情绪闸门
        self._fbs = {}                  # code -> {ema_fast, ema_slow, rise, prev_ema_slow}
        self._gate_provider = None
        self._gate = {"allow": True, "reason": "未接入大盘/情绪闸门",
                      "temp": None, "stage": None}

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
            self._rollover()
            speed_list, dive_list, fb_list = [], [], []
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
                # ---- 🎯 首板雷达（抓趋势上涨突破首板）----
                # 条件：均线稳步抬升(日内慢线连续上升) + 上涨趋势(价在慢线/开盘/昨收之上)
                #       + 突然放量(量比30~50%且非巨量) + 三分钟涨速>3% + 突破趋势(创新高且站上慢线)
                #       + 大盘/情绪闸门允许。尽量在涨停之前提示。
                if config.SPRITE_FB_ENABLED and in_trading:
                    try:
                        fbs = self._fbs.get(code)
                        prev_ts = fbs["ts"] if fbs else None
                        dt = (ts - prev_ts) if prev_ts else w2
                        ef0 = fbs["ema_fast"] if fbs else None
                        es0 = fbs["ema_slow"] if fbs else None
                        ef_new = self._ema_update(ef0, price, dt, config.SPRITE_FB_EMA_FAST)[1]
                        es_new = self._ema_update(es0, price, dt, config.SPRITE_FB_EMA_SLOW)[1]
                        rise = (fbs["rise"] + 1) if (fbs and es_new > fbs["ema_slow"]) else 0
                        self._fbs[code] = {"ts": ts, "ema_fast": ef_new,
                                           "ema_slow": es_new, "rise": rise}
                        open_ = rec.get("open")
                        prev_close = rec.get("prev_close")
                        vr_ = rec.get("vol_ratio")
                        ma_rising = rise >= config.SPRITE_FB_RISE_FRAMES     # 均线稳步抬升
                        trend_up = (price > es_new
                                    and (open_ is None or price > open_)
                                    and (prev_close is None or price > prev_close))  # 上涨趋势
                        vol_ok = (vr_ is not None
                                  and config.SPRITE_FB_VOL_MIN <= vr_ <= config.SPRITE_FB_VOL_MAX
                                  and vr_ < config.SPRITE_FB_VOL_GIANT)      # 放量30~50% 且非巨量
                        speed_ok = (sp2 is not None and sp2 >= config.SPRITE_FB_SPEED)  # 三分钟涨速>3%
                        breakout = (high and price >= high * 0.99           # 创新高
                                    and ef_new > es_new                     # 快线在慢线之上
                                    and price > es_new * (1 + config.SPRITE_FB_BREAK_PCT / 100.0))  # 站上慢线=突破
                        pct_ok = config.SPRITE_FB_MIN_PCT <= pct < config.SPRITE_FB_MAX_PCT
                        liq_ok = amount >= config.SPRITE_FB_MIN_AMOUNT
                        hit = ma_rising and trend_up and vol_ok and speed_ok and breakout and pct_ok and liq_ok
                        if hit and self._gate.get("allow", True):
                            if not self._cooled("first_board", code, ts, config.SPRITE_FB_COOLDOWN):
                                vol_pct = int(round((vr_ - 1) * 100)) if vr_ else 0
                                fb_text = (f"首板预警 {name} 现价{price:.2f} 涨{pct:+.2f}% "
                                           f"放量约{vol_pct}% 三分钟涨速{sp2:+.2f}% 突破前高 均线多头 建议重点关注")
                                self._emit(
                                    "first_board", ts, code, name, price, pct, sp2, w2, amount, vr_,
                                    fb_text, level=1,
                                    extra={"ma_rising": ma_rising, "trend_up": trend_up,
                                           "vol_ok": vol_ok, "speed_ok": speed_ok,
                                           "breakout": breakout, "ema_fast": round(ef_new, 3),
                                           "ema_slow": round(es_new, 3), "vol_pct": vol_pct,
                                           "gate": self._gate.get("reason", "")})
                        if hit:
                            fb_list.append({
                                "code": code, "name": name, "price": price, "pct": pct,
                                "sp": sp2, "amount_yi": amount, "vol_ratio": vr_,
                                "ma_rising": ma_rising, "trend_up": trend_up,
                                "breakout": breakout,
                                "vol_pct": int(round((vr_ - 1) * 100)) if vr_ else 0,
                                "focus": code in self._focus,
                            })
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
                    fb_list, key=lambda x: -x["pct"])[:config.SPRITE_SPEED_TOP]
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
                },
                "errors": self.errors[-5:],
            }

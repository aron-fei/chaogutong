# -*- coding: utf-8 -*-
"""市场监控台 Flask 服务

启动: /opt/homebrew/anaconda3/bin/python3 app.py
访问: http://127.0.0.1:8765
"""
import data_fetcher  # 侧效应：清代理环境变量，必须最先导入
import log_util

from flask import Flask, jsonify, render_template, request, redirect, url_for, send_file, g
from pathlib import Path
import os

import config
import portfolio
import advisor
import review
import watchlist
import overseas_fetcher  # 多市场（M1 港股 / M2 美股：独立线程，错误隔离不阻塞 A股）
import futures_fetcher  # M3 期货/外盘（独立线程，错误隔离不阻塞 A股）
import futures_strategy  # M5 期货策略（K线缓存 + 四因子打分，ADR-0024）
import linkage  # M4 联动观察（无独立线程，前端 30s 轮询即时聚合）
import fut_advisor  # M6 期货超短顾问（T+0 日内信号，ADR-0026）
import sprite  # M7 短线精灵异动引擎（全局事件流，data_fetcher 直接喂数据）
import mood_engine  # 情绪周期（首板雷达的大盘/情绪闸门）
import board_rank  # M7 板块榜（腾讯官方 speed 涨速字段，替代失效的 THS 板块涨速）
import theme_board  # M10 细分题材引擎（东财概念成分股反向索引，2026-11-05）
import theme_picker  # 2026-11-05 v2：L0 题材趋势 + L1 低位挖掘（纯内存打分）
import news_catalyst  # 2026-11-05 v2：新闻/传闻催化（东财快讯真实源）
import dispatch_diag  # 2026-11-05 体检剩余项：零派单根因诊断（只读）
import bottom_radar  # M8 底部放量雷达（ADR-0033，缓存+后台刷新，扫描整轮 5~6s）
import position_watch  # M9 持仓实时跟踪（ADR-0034，交易时段逐帧评估持仓 → 分层操作建议+异动）
from datetime import datetime, timedelta
import json
import secrets
import threading
import time

app = Flask(__name__)
# 防止浏览器/代理缓存模板（开发期频繁改动，老缓存会导致用户看不到新版）
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0
app.jinja_env.auto_reload = True


# ---- 远程访问门禁（config.HOST=0.0.0.0，手机/LAN 需密钥，本机回环放行）----
ACCESS_KEY_FILE = Path(__file__).parent / "data" / "access_key.json"


def _load_access_key():
    """首启生成随机密钥并落盘 data/access_key.json；环境变量 WORKBENCH_KEY 可覆盖。"""
    env_key = os.environ.get("WORKBENCH_KEY", "").strip()
    if env_key:
        return env_key
    try:
        ACCESS_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        if ACCESS_KEY_FILE.exists():
            try:
                ACCESS_KEY_FILE.chmod(0o600)   # 体检 P3-1：密钥文件仅属主可读写
            except OSError as e:
                print(f"[access_key] 收紧权限失败: {e}")
            return json.loads(ACCESS_KEY_FILE.read_text(encoding="utf-8"))["key"]
        key = secrets.token_urlsafe(9)
        ACCESS_KEY_FILE.write_text(
            json.dumps({"key": key}, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            ACCESS_KEY_FILE.chmod(0o600)
        except OSError as e:
            print(f"[access_key] 收紧权限失败: {e}")
        return key
    except Exception as e:
        print(f"[access_key] 读取失败回退默认: {e}")
        return "wb4demo"


ACCESS_KEY = _load_access_key()
LOOPBACK_PREFIXES = ("127.0.0.1", "::1", "localhost")


def _is_loopback(addr):
    return addr.startswith(LOOPBACK_PREFIXES)


@app.before_request
def access_gate():
    """本机回环直连放行；非回环(手机/LAN)必须带 wb_k cookie 或 ?k=ACCESS_KEY。
    首次访问带 ?k= → after_request 种 wb_k cookie（30天），后续免带。"""
    if _is_loopback(request.remote_addr or ""):
        return None
    if request.path.startswith("/static/"):
        return None
    q = request.args.get("k", "")
    if q and secrets.compare_digest(q, ACCESS_KEY):
        g.wb_set_cookie = True
        return None
    ck = request.cookies.get("wb_k") or ""
    if ck and secrets.compare_digest(ck, ACCESS_KEY):
        return None
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "access denied: missing key"}), 403
    # 安全：403 页面不得回显密钥（体检 P0-1）。密钥请从 data/access_key.json
    # 或启动日志本机查看，避免未认证的 LAN 访客直接拿到密钥。
    return ("<h3>访问受限</h3>"
            "<p>请在地址后追加 <code>?k=&lt;访问密钥&gt;</code> 后重新打开。</p>"
            "<p>密钥可在服务本机 <code>data/access_key.json</code> 查看，"
            "或通过环境变量 <code>WORKBENCH_KEY</code> 指定。</p>", 403)


@app.after_request
def no_cache(resp):
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    if getattr(g, "wb_set_cookie", False):
        resp.set_cookie("wb_k", ACCESS_KEY, max_age=60 * 60 * 24 * 30,
                        httponly=True, samesite="Lax")
    return resp

md = data_fetcher.MarketData()
adv = advisor.Advisor(md)
hk = overseas_fetcher.HkData()   # 港股行情（独立线程，随服务启动）
us = overseas_fetcher.UsData()   # 美股隔夜行情（独立线程，随服务启动）
fut = futures_fetcher.FuturesData()  # 国内期货+外盘商品（M3，独立线程）
fut_strategy = futures_strategy.FuturesStrategy(fut)  # M5 期货策略（消费 fut 行情 + 日K缓存）
lk = linkage.LinkageData(md, hk, us, fut)  # M4 联动观察（无独立线程，聚合）
fadv = fut_advisor.instance(fs=fut_strategy)  # M6 超短顾问（消费 fut_strategy 主连日K算 ATR）
brank = board_rank.instance()  # M7 板块榜（腾讯官方涨速，喂 sprite）
tboard = theme_board.instance()  # M10 细分题材反向索引（缓存优先，失败降级行业兜底）

# M7 短线精灵：注入「关注股」来源（自选股池 + 持仓）→ 这些股的任何异动弹最高级提醒
def _sprite_focus():
    codes = set()
    try:
        codes |= {str(i.get("code")) for i in watchlist.list_items() if i.get("code")}

    except Exception as _e:
        log_util.quiet_warn("app.py:123", _e)

    try:
        codes |= {str(p.get("code")) for p in portfolio.list_positions() if p.get("code")}

    except Exception as _e:
        log_util.quiet_warn("app.py:127", _e)

    return codes


sprite.instance().set_focus_provider(_sprite_focus)


# M7 首板雷达·大盘/情绪闸门（根据大盘温度 + 情绪周期，决定首板预警是否发声）
def _sprite_market_gate():
    """返回 {"allow","reason","temp","stage"}：冰点/退潮/空仓防守 或 大盘温度过低时限制首板预警。"""
    try:
        b = md.snapshot().get("breadth") or {}
        up, down = b.get("up"), b.get("down")
        temp = round(float(up) / float(up + down) * 100.0, 1) if (up or down) else None
        stage = (mood_engine.cycle_judge(md) or {}).get("stage", "")
        # 限制条件：情绪处于防守阶段
        if stage in config.SPRITE_FB_GATE_STAGES:
            return {"allow": False, "reason": f"情绪处于【{stage}】防守段，限制首板接力",
                    "temp": temp, "stage": stage}
        # 限制条件：大盘温度过低（跌多涨少）
        if temp is not None and temp < config.SPRITE_FB_GATE_TEMP:
            return {"allow": False, "reason": f"大盘温度 {temp}% 偏低（<{config.SPRITE_FB_GATE_TEMP}%），限制首板接力",
                    "temp": temp, "stage": stage}
        return {"allow": True, "reason": f"大盘温度 {temp}% · 情绪【{stage}】· 允许首板预警",
                "temp": temp, "stage": stage}
    except Exception as e:
        print(f"[sprite-gate] 取数失败: {type(e).__name__}: {e}")
        return {"allow": True, "reason": "闸门取数异常，默认放行", "temp": None, "stage": None}


sprite.instance().set_market_gate_provider(_sprite_market_gate)

# ============================================================================
# 2026-11-05 首板雷达 v2：L0 / L1 策略依赖
# 全部走「低频后台刷新 + 内存只读」，绝不阻塞 20s 一轮的全A帧。
# ============================================================================
nc = news_catalyst.instance()          # 新闻 / 传闻催化（东财快讯真实源）
_trend_lock = threading.Lock()
_trend_names = []
_prev_zt_lock = threading.Lock()
_prev_zt_cache = {"date": "", "source_date": "", "codes": []}
PREV_ZT_FILE = config.BASE_DIR / "data" / "prev_zt.json"


def _prev_zt_codes():
    """供 L2 排除连板接力：只读内存。取不到时返回空集（fail-open，不编造）。"""
    with _prev_zt_lock:
        return set(_prev_zt_cache["codes"])


def _trend_names_provider():
    with _trend_lock:
        return list(_trend_names)


def _refresh_trend_names():
    """每 60s 重算 L0 趋势题材名，供 L2 事件打标签（不重算全A快照）。"""
    global _trend_names
    try:
        groups = _build_zt_theme_groups(md.snapshot(), brank.snapshot())
        names = [x["name"] for x in theme_picker.rank_trends(groups, heat=nc.heat())]
        with _trend_lock:
            _trend_names = names
    except Exception as e:
        print(f"[strategy] 趋势题材刷新失败: {type(e).__name__}: {e}")


def _refresh_prev_zt():
    """每日取上一交易日涨停池 → 排除「连板接力」（不是当天第一次涨停）。

    网络失败时保留旧缓存；完全没有则留空并在 /api/health 标注，绝不编造。
    """
    today = datetime.now().strftime("%Y%m%d")
    with _prev_zt_lock:
        if _prev_zt_cache["date"] == today and _prev_zt_cache["codes"]:
            return
    try:
        if PREV_ZT_FILE.exists():
            d = json.loads(PREV_ZT_FILE.read_text(encoding="utf-8"))
            if d.get("date") == today and d.get("codes"):
                with _prev_zt_lock:
                    _prev_zt_cache.update(d)
                return
    except Exception as e:
        print(f"[strategy] 读昨日涨停缓存失败: {type(e).__name__}: {e}")
    try:
        import akshare as ak
        day = datetime.now() - timedelta(days=1)
        for _ in range(8):
            if day.weekday() >= 5:
                day -= timedelta(days=1)
                continue
            ds = day.strftime("%Y%m%d")
            try:
                df = ak.stock_zt_pool_em(date=ds)
            except Exception as e:
                print(f"[strategy] 涨停池 {ds} 拉取失败: {type(e).__name__}: {e}")
                day -= timedelta(days=1)
                continue
            if df is not None and len(df):
                codes = sorted({str(c).strip().zfill(6)
                                for c in df["代码"].astype(str).tolist()})
                data = {"date": today, "source_date": ds, "codes": codes}
                with _prev_zt_lock:
                    _prev_zt_cache.update(data)
                PREV_ZT_FILE.parent.mkdir(parents=True, exist_ok=True)
                PREV_ZT_FILE.write_text(json.dumps(data, ensure_ascii=False),
                                        encoding="utf-8")
                print(f"[strategy] 昨日涨停基线 {ds}: {len(codes)} 只")
                return
            day -= timedelta(days=1)
        print("[strategy] 近 8 天未取到涨停池，首板基线暂空（fail-open，不编造）")
    except Exception as e:
        print(f"[strategy] 昨日涨停基线失败: {type(e).__name__}: {e}")


def _strategy_worker():
    # 启动首分钟每 10s 刷一次（此时板块榜/涨停池刚就绪，避免题材标签空窗），之后 60s
    tick = 0
    while True:
        _refresh_trend_names()
        _refresh_prev_zt()
        tick += 1
        time.sleep(10 if tick < 6 else 60)


def start_strategy():
    threading.Thread(target=_strategy_worker, name="strategy-v2", daemon=True).start()
    nc.start()


sprite.instance().set_prev_zt_provider(_prev_zt_codes)
sprite.instance().set_trend_provider(_trend_names_provider)

# M9 持仓实时跟踪（ADR-0034）：
#  ①持仓来源 = 50 万模拟盘 backtest_database.json（策略建议的短线持仓，只读）
#  ②大盘温度来源 = 复用已缓存的 dashboard 宽度（涨家数占比），零额外请求
pwatch = position_watch.instance()


def _pwatch_mkt_temp():
    """大盘温度代理：涨家数 / (涨+跌) × 100（与 advisor 情绪档同向，取已缓存的宽度数据）。"""
    try:
        b = md.snapshot().get("breadth") or {}
        up, down = b.get("up"), b.get("down")
        if not up and not down:
            return None
        return round(float(up) / float(up + down) * 100.0, 2)
    except Exception as e:
        print(f"[position_watch] 大盘温度取数失败: {type(e).__name__}: {e}")
        return None


pwatch.set_mkt_temp_provider(_pwatch_mkt_temp)

# 构建戳（ADR-0023）— 每次进程启动变化，用于检测内置浏览器快照陈旧问题
BUILD_ID = datetime.now().strftime("%H%M%S") + "-" + secrets.token_hex(2).upper()
STARTED_AT = time.time()


# ---- 只读慢接口 TTL 缓存（体检 P1-1） ------------------------------------
# /api/review 与 /api/daily_review 原为同步直调，内部多次拉 K 线/实时行情，
# 实测稳定 11~13s 且无缓存无兜底。这里统一加 TTL + stale-on-error：
# 命中直接返回；生成失败时优先回吐上一次成功值并标注 _warning，前端不白屏。
_SLOW_CACHE = {}
_SLOW_LOCK = threading.Lock()


def _cached(key, ttl, producer, force=False):
    now = time.time()
    with _SLOW_LOCK:
        hit = _SLOW_CACHE.get(key)
        if not force and hit and (now - hit["at"]) < ttl:
            return {**hit["data"], "_cache": {"hit": True,
                                             "age_s": round(now - hit["at"], 1)}}, 200
    try:
        data = producer()
        if not isinstance(data, dict):
            data = {"data": data}
        with _SLOW_LOCK:
            _SLOW_CACHE[key] = {"at": time.time(), "data": data}
        return {**data, "_cache": {"hit": False, "age_s": 0}}, 200
    except Exception as e:
        print(f"[slow-cache] {key} 生成失败: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        with _SLOW_LOCK:
            old = (_SLOW_CACHE.get(key) or {}).get("data")
        if old:
            return {**old, "_cache": {"hit": True, "stale": True},
                    "_warning": f"{type(e).__name__}: {e}"}, 200
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}, 502


@app.route("/")
def index():
    # 监控台已融入工作台（左侧 Tab 切换），首页直接跳转
    return redirect(url_for("workbench"))


@app.route("/workbench")
def workbench():
    return render_template("workbench.html", build_id=BUILD_ID)


def _build_zt_board_groups(md_snap, brank_snap):
    """涨停池按板块分组聚合（M-涨停池优化）：
       每个板块 -> 涨停个数 / 最高连板数 / 板块当天净流入(亿) / 板块涨速(%) / 领涨股。
       板块行情（净流入/涨速）来自腾讯板块榜 board_rank，按「行业名」模糊匹配东财 industry。
    """
    zt = md_snap.get("zt_pool_all") or md_snap.get("zt_pool") or []
    boards = (brank_snap or {}).get("boards") or []
    # 板块榜按 name 建索引（hy 行业 / gn 概念）
    board_by_name = {}
    for b in boards:
        nm = (b.get("name") or "").strip()
        if nm:
            board_by_name.setdefault(nm, b)

    def match_board(industry):
        if not industry:
            return {}
        if industry in board_by_name:
            return board_by_name[industry]
        # 模糊匹配：一方包含另一方（东财「半导体」vs 腾讯概念「半导体」等）
        for nm, b in board_by_name.items():
            if nm and (nm in industry or industry in nm):
                return b
        # 回退：东财行业名 → 腾讯一级行业别名表（解决「工业金属→有色金属」等命名差异）
        alias_map = getattr(config, "ZT_INDUSTRY_TO_TENCENT_BOARD", {})
        alias = alias_map.get(industry)
        if not alias:
            # 前缀兜底：应对东财行业名被截断（如「家电零部」→「家电零部件」）
            for k, v in alias_map.items():
                if k and (industry.startswith(k) or k.startswith(industry)):
                    alias = v
                    break
        if alias and alias in board_by_name:
            return board_by_name[alias]
        return {}

    groups = {}
    for s in zt:
        ind = (s.get("industry") or "").strip() or "未分类"
        g = groups.get(ind)
        if g is None:
            g = groups[ind] = {
                "industry": ind, "count": 0, "max_lianban": 0,
                "netflow_yi": None, "speed": None, "zdf": None,
                "lead": None, "lead_pct": None, "type": None, "stocks": [],
            }
        g["count"] += 1
        lb = s.get("lianban") or 1
        g["max_lianban"] = max(g["max_lianban"], lb)
        g["stocks"].append({
            "name": s.get("name"), "code": s.get("code"),
            "lianban": lb, "pct": s.get("pct"),
            "amount_yi": s.get("amount_yi"), "first_time": s.get("first_time"),
            # v2：行业维度下钻表格与题材维度共用列，字段补齐
            "fund_yi": s.get("fund_yi"), "industry": ind,
            "turnover": s.get("turnover"),
        })
    # 匹配板块行情指标
    for ind, g in groups.items():
        b = match_board(ind)
        if b:
            g["netflow_yi"] = b.get("netflow_yi")
            g["speed"] = b.get("speed")
            g["zdf"] = b.get("zdf")
            g["lead"] = b.get("lead")
            g["lead_pct"] = b.get("lead_pct")
            g["type"] = b.get("type")
    # 组内股票按连板数降序
    for g in groups.values():
        g["stocks"].sort(key=lambda x: (-(x.get("lianban") or 1), str(x.get("first_time") or "")))
    # 板块排序：最高连板降序 → 涨停个数降序（连板数从高往下排）
    return sorted(groups.values(), key=lambda x: (-x["max_lianban"], -x["count"]))


def _build_zt_theme_groups(md_snap, brank_snap):
    """细分题材涨停分布（2026-11-05 v2）：一股可属多个题材，按涨停家数降序。

    kind: concept=东财产业概念 / rule=显式规则 / name=字辈自动发现 / industry=行业兜底
    行业兜底保证「索引未就绪」时也不丢票；concept 组附带东财概念榜的今日涨跌幅、
    领涨股、涨跌家数，并用成分股主行业补上腾讯板块榜的净流入与涨速。
    """
    zt = md_snap.get("zt_pool_all") or md_snap.get("zt_pool") or []
    alias = getattr(config, "ZT_THEME_ALIASES", {})
    ind_meta = {g["industry"]: g for g in _build_zt_board_groups(md_snap, brank_snap)}

    rule_map = theme_board.rule_themes(zt)
    auto_map = theme_board.auto_name_themes(zt)
    # 预先算好每只票的题材名，避免 O(n²)
    tags = {}
    for s in zt:
        code = str(s.get("code") or "")
        names, kinds = [], {}
        for t in tboard.themes_for(code):
            # 双保险：旧缓存里可能残留风格/统计型伪概念，聚合层再挡一次
            if theme_board._is_pseudo(t):
                continue
            nm = alias.get(t, t)
            if nm not in names:
                names.append(nm)
                kinds[nm] = "concept"
        for nm, members in list(rule_map.items()) + list(auto_map.items()):
            if any(str(m.get("code") or "") == code for m in members) and nm not in names:
                names.append(nm)
                kinds[nm] = "rule" if nm in rule_map else "name"
        if not names:
            nm = (s.get("industry") or "未分类").strip() or "未分类"
            names, kinds = [nm], {nm: "industry"}
        tags[code] = [(nm, kinds[nm]) for nm in names]

    groups = {}
    for s in zt:
        code = str(s.get("code") or "")
        lb = s.get("lianban") or 1
        item = {
            "name": s.get("name"), "code": code, "lianban": lb,
            "pct": s.get("pct"), "amount_yi": s.get("amount_yi"),
            "fund_yi": s.get("fund_yi"), "first_time": s.get("first_time"),
            "industry": s.get("industry"), "break_count": s.get("break_count"),
            "turnover": s.get("turnover"),
        }
        for nm, kind in tags.get(code, []):
            g = groups.get(nm)
            if g is None:
                g = groups[nm] = {
                    "name": nm, "kind": kind, "count": 0, "max_lianban": 0,
                    "zdf": None, "lead": None, "lead_pct": None,
                    "up_count": None, "down_count": None,
                    "netflow_yi": None, "speed": None,
                    "stocks": [], "_codes": set(), "_ind": {},
                }
            if code in g["_codes"]:
                continue
            g["_codes"].add(code)
            g["count"] += 1
            g["max_lianban"] = max(g["max_lianban"], lb)
            g["stocks"].append(item)
            ind = (s.get("industry") or "").strip()
            if ind:
                g["_ind"][ind] = g["_ind"].get(ind, 0) + 1

    # 补行情：concept 用东财概念榜元信息（取组内 rank 最小者）；资金/涨速用主行业近似
    meta_cache = {}
    for nm, g in groups.items():
        if g["kind"] == "concept":
            # 反查该合并题材对应的原始概念名，取排名最靠前的行情
            cands = [nm] + [k for k, v in alias.items() if v == nm]
            best = None
            for c in cands:
                m = tboard.meta(c)
                if m and (best is None or m.get("rank", 9999) < best.get("rank", 9999)):
                    best = m
            meta_cache[nm] = best or {}
            if best:
                g["zdf"] = best.get("zdf")
                g["lead"] = best.get("lead") or None
                g["lead_pct"] = best.get("lead_pct")
                g["up_count"] = best.get("up_count")
                g["down_count"] = best.get("down_count")
        # 主行业 → 腾讯板块榜净流入/涨速；行业兜底组直接用行业行情
        if g["_ind"]:
            main_ind = max(g["_ind"].items(), key=lambda x: x[1])[0]
            im = ind_meta.get(main_ind, {})
            # 题材级没有权威资金流字段，用成分股「主行业」的腾讯板块榜数据近似，
            # 并在 payload 里标明 proxy_industry，前端 label 会带「近似」二字，不伪装。
            g["netflow_yi"] = im.get("netflow_yi")
            g["speed"] = im.get("speed")
            g["proxy_industry"] = main_ind if g["kind"] != "industry" else None
            if g["kind"] == "industry":
                g["zdf"] = im.get("zdf")
                g["lead"] = im.get("lead")
                g["up_count"] = None
                g["down_count"] = None
        g["stocks"].sort(key=lambda x: (-(x.get("lianban") or 1),
                                       str(x.get("first_time") or "")))
        del g["_codes"], g["_ind"]
    # 用户要求：涨停家数最多的板块排最前；同数再比最高连板、板块涨幅
    return sorted(groups.values(),
                  key=lambda x: (-x["count"], -x["max_lianban"], -(x["zdf"] or 0)))


@app.route("/api/dashboard")
def api_dashboard():
    snap = md.snapshot()
    try:
        brank_snap = brank.snapshot()
        snap["zt_board_groups"] = _build_zt_board_groups(snap, brank_snap)
        snap["zt_theme_groups"] = _build_zt_theme_groups(snap, brank_snap)
    except Exception as e:
        print(f"[dashboard] 涨停池板块分组失败: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        snap["zt_board_groups"] = []
        snap["zt_theme_groups"] = []
    try:
        snap["theme_status"] = tboard.snapshot()
    except Exception as e:
        print(f"[dashboard] 题材状态读取失败: {type(e).__name__}: {e}")
        snap["theme_status"] = {"state": "degraded", "err": str(e)[:120]}
    return jsonify(snap)


@app.route("/api/global")
def api_global():
    """多市场行情：hk=M1 港股 / us=M2 美股隔夜 / fut=M3 国内期货+外盘商品。"""
    mk = (request.args.get("mk") or "hk").lower()
    if mk == "hk":
        return jsonify(hk.snapshot())
    if mk == "us":
        return jsonify(us.snapshot())
    if mk == "fut":
        return jsonify(fut.snapshot())
    return jsonify({"ok": False, "error": f"market '{mk}' 尚未接入"})


@app.route("/api/linkage")
def api_linkage():
    """M4 联动观察（A 股第 14 tab）：聚合 md/hk/us/fut + 映射表 → 三区块数据。"""
    return jsonify(lk.build())


@app.route("/api/dragon")
def api_dragon():
    return jsonify(md.dragon_radar())


@app.route("/api/theme_arb")
def api_theme_arb():
    """题材联动·短线套利（M1）：题材库龙头触发(≥2板) → 同题材成分股实时扫描。
    竞价倍数列 M1 降级(无竞价归档)，以高开幅度/量比/距涨停代理抢筹。"""
    import theme_arb
    try:
        return jsonify(theme_arb.scan(md))
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/advisor")
def api_advisor():
    return jsonify(adv.advise())


@app.route("/api/sprite")
def api_sprite():
    """M7 短线精灵：异动事件流（游标增量拉取）+ 4 组榜单 + 板块涨速榜。

    前端以 ?since=<seq> 拉增量用于弹窗提醒，首次不带 since 拉最近 200 条。
    """
    try:
        since = int(request.args.get("since") or 0)
    except (TypeError, ValueError):
        since = 0
    try:
        limit = min(int(request.args.get("limit") or 200), 400)
    except (TypeError, ValueError):
        limit = 200
    snap = sprite.instance().snapshot(since=since, limit=limit)
    snap["board_rank_at"] = brank.snapshot().get("last_update")
    snap["board_total"] = len(brank.snapshot().get("boards") or [])
    # 盘前/收盘没有全 A 帧喂入时，sprite 内部基线尚未刷新。用 app 层权威缓存覆盖，
    # 避免前端把「昨日涨停基线」显示成 0 只（真实 84 只）造成误导。
    fb = snap.setdefault("fb", {})
    fb["prev_zt_count"] = len(_prev_zt_codes())
    fb["prev_zt_day"] = _prev_zt_cache.get("source_date", "")
    fb["trends"] = _trend_names_provider()[:12] or fb.get("trends") or []
    return jsonify(snap)


@app.route("/api/dispatch_diag")
def api_dispatch_diag():
    """体检剩余项：把「报告 ready 但零派单」从黑盒变成逐时段可自查的结论。只读。"""
    payload, code = _cached("dispatch_diag", 60, dispatch_diag.diagnose)
    return jsonify(payload), code


@app.route("/api/theme_pick")
def api_theme_pick():
    """首板雷达 v2：L0 趋势题材榜 + L1 低位挖掘观察池。

    全部复用已缓存数据（涨停池分组 / 全A帧 / 题材成分股 / 快讯催化），
    纯内存打分零额外行情请求；TTL 30s，?refresh=1 强制重算。
    """
    force = request.args.get("refresh") in ("1", "true", "yes")

    def produce():
        snap = md.snapshot()
        groups = _build_zt_theme_groups(snap, brank.snapshot())
        rows, rows_at = md.get_full_rows()
        zt_codes = {str(s.get("code") or "").strip().zfill(6)
                    for s in (snap.get("zt_pool_all") or []) if s.get("code")}
        zb_codes = {str(s.get("code") or "").strip().zfill(6)
                    for s in (snap.get("zb_pool") or []) if s.get("code")}
        avoid = zt_codes | zb_codes | _prev_zt_codes()
        payload = theme_picker.pick(
            groups, rows, tboard, heat=nc.heat(),
            avoid_codes=avoid, zt_codes=zt_codes,
            news_state=nc.snapshot(), theme_state=tboard.snapshot(), rows_at=rows_at)
        # 盘前/盘后当日涨停池为空 → L0 全空，用户打开看不到方向。
        # 用「昨日涨停成分股所属题材 + 真实快讯」给隔夜方向榜，明确标注口径，
        # 不冒充当日 L0 趋势、不编造数据；开盘产生涨停后自动切回实时榜。
        sess = (payload.get("meta") or {}).get("session")
        if (not (payload.get("trends") or []) and not groups
                and sess in ("preopen", "auction", "lunch", "closed")):
            try:
                ov = theme_picker.overnight_trends(_prev_zt_codes(), tboard, heat=nc.heat())
            except Exception as e:
                ov = []
                print(f"[theme_pick] 隔夜榜失败: {type(e).__name__}: {e}")
            if ov:
                payload["trends"] = ov
                meta = payload.setdefault("meta", {})
                meta["trend_count"] = len(ov)
                meta["trend_source"] = "overnight"
                meta["trend_note"] = ("昨日涨停成分股所属题材 + 近 4 小时真实快讯，"
                                      "不是当日 L0 趋势；开盘产生涨停后自动切换为实时榜")
        return payload

    payload, code = _cached("theme_pick", 30, produce, force=force)
    return jsonify(payload), code


@app.route("/api/bottom_surge")
def api_bottom_surge():
    """M8 底部放量雷达（ADR-0033）：全A实时扫描「底部 + 放量 + 未涨停 + 可成交」标的。

    整轮扫描 5~6s，故走缓存（TTL 90s + 后台刷新）：请求永远秒回，过期时返回旧快照
    并带 stale=true / age_sec 供前端标注。?force=1 强制同步刷新（用户点「立即刷新」）。
    """
    force = request.args.get("force") in ("1", "true", "yes")
    try:
        return jsonify(bottom_radar.instance().snapshot(force=force))
    except Exception as e:
        # 兜底：雷达故障绝不允许 500 拖垮工作台其它 tab
        return jsonify({"mk": "bottom_surge", "state": "error", "error": f"{type(e).__name__}: {e}",
                        "stale": True, "age_sec": None, "meta": {}, "rows": []})


@app.route("/api/position_watch")
def api_position_watch():
    """M9 持仓实时跟踪（ADR-0034）：50 万模拟盘持仓的分层操作建议 + 异动事件流。

    由后台线程每 PW_INTERVAL 秒刷新，本接口只读缓存，永不阻塞。
    ?since=<seq> 增量取事件（前端游标）；?force=1 强制立刻评估一轮（诊断用）。
    """
    try:
        since = int(request.args.get("since") or 0)
    except (TypeError, ValueError):
        since = 0
    force = request.args.get("force") in ("1", "true", "yes")
    try:
        return jsonify(pwatch.snapshot(since=since, force=force))
    except Exception as e:
        # 兜底：盯盘故障绝不允许 500 拖垮工作台其它 tab
        return jsonify({"mk": "position_watch", "state": "error",
                        "error": f"{type(e).__name__}: {e}", "rows": [], "events": [],
                        "seq": 0, "account": {}, "stats": {}})


@app.route("/api/review")
def api_review():
    payload, code = _cached("review", 120, lambda: review.review(),
                            force=request.args.get("refresh") in ("1", "true"))
    return jsonify(payload), code


@app.route("/api/fut_strategy")
def api_fut_strategy():
    """期货策略（M5，ADR-0024）：热点板块 → 强势/弱势品种 → 多空建议+止损止盈。"""
    if fut_strategy.result is None:
        return jsonify({"mk": "fut_strategy", "state": "init",
                        "generated_at": None, "blocks": [], "long_top": [],
                        "short_top": [], "total": 0,
                        "disclaimer": "策略引擎初始化中（K线首轮拉取 ~40s）…"})
    return jsonify(fut_strategy.result)


@app.route("/api/fut_advisor", methods=["GET", "POST"])
def api_fut_advisor():
    """期货超短顾问（M6，ADR-0026）：GET=盯盘快照（行情+信号+快讯+当日进度）；
    POST：action=profile 改资金参数 / action=pnl 记一笔已实现盈亏 / action=watch 增删品种。"""
    if request.method == "GET":
        return jsonify(fadv.snapshot())
    d = request.get_json(silent=True) or {}
    action = d.get("action")
    try:
        if action == "profile":
            return jsonify({"ok": True,
                            "profile": fadv.set_profile(capital=d.get("capital"),
                                                        daily_goal=d.get("daily_goal"),
                                                        per_trade_risk=d.get("per_trade_risk"))})
        elif action == "pnl":
            return jsonify({"ok": True, **fadv.record_pnl(d.get("amount", 0))})
        elif action == "watch":
            n = fadv.set_watch(d.get("op"), str(d.get("kind", "")).upper())
            return jsonify({"ok": True, "count": n})
        return jsonify({"ok": False, "error": "action 须为 profile/pnl/watch"})
    except (KeyError, ValueError) as e:
        return jsonify({"ok": False, "error": str(e)})
    except Exception as e:
        return jsonify({"ok": False, "error": f"系统错误: {e}"})


@app.route("/api/daily_review")
def api_daily_review():
    """当日综合复盘：市场复盘 + 账户操作复盘 + 建议准确率（TTL 120s 缓存）。"""
    payload, code = _cached("daily_review", 120, lambda: review.daily_review(md, adv),
                            force=request.args.get("refresh") in ("1", "true"))
    return jsonify(payload), code


@app.route("/api/review_deep")
def api_review_deep():
    """复盘模式（ADR-0039）：六引擎 + 次日交易预案 + 仓位闸门建议。

    只读接口：不改动任何买卖通道。?noarch=1 跳过当日归档（诊断用）。
    六引擎各自 try/except 兜底，单引擎异常不影响其余部分。
    """
    try:
        import review_deep
        do_arch = request.args.get("noarch") not in ("1", "true", "yes")
        return jsonify(review_deep.review_deep(md, adv, do_archive=do_arch))
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}",
                        "cycle": {}, "verify": {}, "loss": {}, "main": {},
                        "wind": [], "discipline": {}, "plan": {},
                        "errors": [f"{type(e).__name__}: {e}"]})


@app.route("/api/review_gate", methods=["GET", "POST"])
def api_review_gate():
    """复盘闸门（ADR-0040）：查看 / 切换强度。

    GET  → {gate, gate_active, inactive_reason, summary, applied, effectiveness}
    POST → {"mode": "enforce"|"shadow"|"toggle"}，只改 trade_gate.json 的 mode 字段。

    这是「复盘结论 → 次日操作约束」闭环的可见化入口；不修改任何策略代码/回测库。
    """
    try:
        import review_gate
    except Exception as e:
        return jsonify({"ok": False, "error": f"review_gate 不可用: {e}"})
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        g = review_gate.load_gate(strict=False)
        if not g:
            return jsonify({"ok": False, "error": "无闸门文件可切换（请先在交易日盘后运行复盘模式）"})
        want = str(body.get("mode") or "toggle")
        if want == "toggle":
            want = "shadow" if g.get("mode") == "enforce" else "enforce"
        if want not in ("enforce", "shadow"):
            return jsonify({"ok": False, "error": "mode 须为 enforce / shadow / toggle"})
        g["mode"] = want
        review_gate._write_json(review_gate.GATE_FILE, g)
        return jsonify({"ok": True, "mode": want, "gate": g})
    try:
        st = review_gate.status()
        st["ok"] = True
        st["summary_text"] = review_gate.summary_text(st.get("gate"))
        return jsonify(st)
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"})


@app.route("/api/gate_health")
def api_gate_health():
    """闸门健康度（2026-10-19 新增）：工作台红条告警的数据源。

    断裂期间 scheduler 只在日志里打 fail-open，界面完全无感 —— 复盘报告里
    连续 7 天写「闸门链断裂」，用户却要靠读报告才知道。本接口把状态提到界面。
    """
    try:
        import review_gate
        h = review_gate.gate_health()
        h["ok"] = True
        return jsonify(h)
    except Exception as e:
        return jsonify({"ok": False, "level": "error",
                        "message": "闸门健康度检测失败: %s" % e})


@app.route("/api/suggestions", methods=["GET", "POST"])
def api_suggestions():
    """复盘建议台账（2026-10-19 新增）：建议的落地入口。

    GET  → {ok, stats, items:[{id, level, title, detail, status, occurrences,
                                first_date, last_date}]}
           可选 query：level=P0 / status=pending
    POST → {"id": "<id>", "status": "accepted"|"implemented"|"dismissed"|"pending",
            "note": "..."} 更新单条状态

    【设计边界】本接口只读写建议状态台账，**绝不触碰任何策略代码/回测库**。
    「是否实施」仍由人拍板，这里只解决「哪条还没处理、什么时候提的、复现几次」。
    """
    try:
        import suggestion_ledger as sl
    except Exception as e:
        return jsonify({"ok": False, "error": "suggestion_ledger 不可用: %s" % e})

    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        sid = str(body.get("id") or "").strip()
        status = str(body.get("status") or "").strip()
        note = str(body.get("note") or "")
        if not sid:
            return jsonify({"ok": False, "error": "缺少 id"})
        if status not in sl.STATUSES:
            return jsonify({"ok": False,
                            "error": "status 须为 %s" % "/".join(sl.STATUSES)})
        if sl.set_status(sid, status, note):
            return jsonify({"ok": True, "id": sid, "status": status,
                            "status_label": sl.STATUS_LABEL.get(status, status)})
        return jsonify({"ok": False, "error": "状态写入失败"})

    try:
        # 【2026-10-21 修】先取**完整**结果用于清理幽灵状态，再做过滤。
        # 旧代码在过滤后调用 purge_status(过滤后的 id 集) → 把未显示的建议状态全删了
        # （实测 27 条状态被删到 18 条，待评估从 0 反弹到 9）。
        all_rows = sl.scan()
        dropped = sl.purge_status({r["id"] for r in all_rows})
        rows = all_rows
        lv = (request.args.get("level") or "").strip()
        stt = (request.args.get("status") or "").strip()
        if lv:
            rows = [r for r in rows if r["level"] == lv]
        if stt:
            rows = [r for r in rows if r["status"] == stt]

        stats = {"total": len(rows)}
        for r in rows:
            stats[r["level"]] = stats.get(r["level"], 0) + 1
            stats[r["status"]] = stats.get(r["status"], 0) + 1

        items = [{
            "id": r["id"], "level": r["level"], "title": r["title"],
            "detail": r["detail"], "status": r["status"],
            "status_label": sl.STATUS_LABEL.get(r["status"], r["status"]),
            "occurrences": r.get("occurrences", 1),
            "first_date": r.get("first_date"), "last_date": r.get("last_date"),
            "source": r.get("source"), "note": r.get("note", ""),
        } for r in rows]
        return jsonify({"ok": True, "stats": stats, "items": items,
                        "pruned": dropped})
    except Exception as e:
        return jsonify({"ok": False, "error": "%s: %s" % (type(e).__name__, e)})


@app.route("/api/mind")
def api_mind():
    """操盘心法：情绪周期阶段 + 7阶段进度 + 操作要点 + 拒绝追高 + 防守三维
       + 赚钱效应CM结构 + 沪深300vs国证2000 + 心法8律 + 涨停基因榜。"""
    import mood_engine
    cycle = mood_engine.cycle_judge(md)
    indices = {i["name"]: i for i in md.indices if i.get("name")}
    defense = mood_engine.defense_dimensions(indices, md.breadth)
    gene, gene_days = mood_engine.sector_gene(md)
    # 跌停数（已有 dt_pool）
    dt_count = len(getattr(md, "dt_pool", None) or [])
    # 7 阶段进度
    progress = mood_engine.stage_progress(cycle.get("stage", "观察"))
    # 操作要点
    points = mood_engine.operation_points(cycle.get("stage", "观察"))
    # 拒绝追高
    alert = mood_engine.risk_alert(cycle.get("stage", "观察"), cycle.get("max_board", 0))
    # 涨停 CM 结构（按板分类）
    cm_bars, cm_total = mood_engine.cm_structure(md.zt_pool)
    # 沪深300 vs 国证2000
    breadth = mood_engine.breadth_indices(md.indices)
    # 板块反包
    rebound = mood_engine.sector_rebound(md)
    # 赚钱效应 5 维
    money = mood_engine.money_effect(md.zt_pool, md.breadth)
    return jsonify({
        "cycle": cycle,
        "progress": progress,
        "points": points,
        "alert": alert,
        "defense": defense,
        "cm_bars": cm_bars,
        "cm_total": cm_total,
        "breadth": breadth,
        "rebound": rebound,
        "money": money,
        "dt_count": dt_count,
        "laws": mood_engine.mind_laws(),
        "gene": gene,
        "gene_days": gene_days,
        "generated_at": datetime.now().strftime("%H:%M:%S"),
    })


# ---- 时段报告（market-advisor 每日 4 段 pro_report，只读展示） ----
REPORTS_DIR = config.REPORTS_DIR


@app.route("/api/reports")
def api_reports():
    """扫描 reports 目录，返回最近几天的时段报告列表。"""
    import re
    out = []
    try:
        for f in sorted(REPORTS_DIR.glob("pro_report_*.html"), reverse=True):
            m = re.match(r"pro_report_(\d{8})_(\d{4})_(\d{4})\.html", f.name)
            if not m:
                continue
            date_s, gen_hm, window = m.groups()
            # 报告时段名称映射
            window_name = {
                "0925": "早盘 · 强者恒强", "1140": "午盘 · 盘中研判",
                "1340": "午后 · 补涨挖掘", "1440": "尾盘 · 确认台",
            }.get(window, f"时段 {window}")
            out.append({
                "date": date_s,
                "window": window,
                "window_name": window_name,
                "file": f.name,
                "url": f"/reports/{f.name}",
                "gen_time": f"{gen_hm[:2]}:{gen_hm[2:]}",
            })
            if len(out) >= 20:
                break
    except Exception as e:
        return jsonify({"error": str(e)})
    return jsonify(out)


@app.route("/api/reports/today_status")
def api_reports_today_status():
    """【2026-10-19 新增】今日时段报告的未读/派单状态（badge 数据源）。

    解决的问题：用户反馈「三个时段有推票但工作台没弹」。查证后发现两层原因 ——
      ① 界面层：时段报告区没有任何未读提示，新报告产出后侧栏毫无变化；
      ② 数据层：报告【九】章节的 Top3 是**操作池排序候选**，而真正的派单是
         order_plan_<时段>.json 的 buy_candidates（当日可能为 0）。
         两者不同源 → 看上去"有票"，实际零派单，自然也没有确认单可弹。
    本接口把两者**显式分开**返回，让界面能诚实展示：
      · ready       报告是否已生成
      · dispatched  该时段是否真的产生了买入派单（buy_candidates > 0）
    """
    import re as _re
    from datetime import date as _date
    today = _date.today().strftime("%Y%m%d")
    windows = ["0925", "1040", "1140", "1340", "1440"]
    slots, no_dispatch = {}, []
    for w in windows:
        try:
            files = sorted(REPORTS_DIR.glob("pro_report_%s_*_%s.html" % (today, w)))
        except Exception:
            files = []
        rep = {"ready": bool(files), "gen_time": "", "dispatched": False,
               "candidates": 0, "gate_active": None}
        if files:
            m = _re.search(r"_(\d{4})_%s\.html$" % w, files[-1].name)
            if m:
                hm = m.group(1)
                rep["gen_time"] = "%s:%s" % (hm[:2], hm[2:])
        op = REPORTS_DIR / ("order_plan_%s.json" % w)
        if op.exists():
            try:
                d = json.loads(op.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    cands = d.get("buy_candidates")
                    if isinstance(cands, list):
                        rep["candidates"] = len(cands)
                        rep["dispatched"] = len(cands) > 0
                    rg = d.get("review_gate")
                    if isinstance(rg, dict):
                        rep["gate_active"] = bool(rg.get("active"))

            except Exception as _e:
                log_util.quiet_warn("app.py:837", _e)

        slots[w] = rep
        if rep["ready"] and not rep["dispatched"]:
            no_dispatch.append(w)

    return jsonify({
        "ok": True,
        "date": today,
        "slots": slots,
        "ready_count": sum(1 for w in windows if slots[w]["ready"]),
        "total_windows": len(windows),
        "dispatch_count": sum(1 for w in windows if slots[w]["dispatched"]),
        "no_dispatch_windows": no_dispatch,
    })
    return jsonify(out)


@app.route("/reports/<path:filename>")
def serve_report(filename):
    """服务报告 HTML（iframe 嵌入需同源）。"""
    f = (REPORTS_DIR / filename).resolve()
    if not str(f).startswith(str(REPORTS_DIR.resolve())) or not f.exists():
        return "not found", 404
    return send_file(f)


# ---- 操作确认台（market-advisor pending_buy_confirm / pending_sell_confirm） ----
# ADR-0032：确认入口收敛到工作台后，这里补齐了原桌面确认台(confirm_gui.py)的三项能力：
#   ① 卖出确认  ② 确认完立即写库  ③ 现金/持仓校验
BACKTEST_DB = REPORTS_DIR / "backtest_database.json"
SCHEDULER_PY = Path(os.environ.get(
    "MARKET_SCHEDULER",
    str(config.MARKET_ADVISOR_SCRIPT)))
SCHEDULER_PYTHON = os.environ.get("MARKET_SCHEDULER_PYTHON", "/opt/homebrew/anaconda3/bin/python3")
# 买入费用口径必须与 market_scheduler.record_buys_to_db 一致：0.03% 佣金 + 0.1% 滑点
BUY_FEE_RATE = 0.0013
# 卖出费用口径与 confirm_pending_sell 一致：0.03% 佣金 + 0.05% 印花 + 0.01% 过户
SELL_FEE_RATE = 0.0009


def _load_backtest_db():
    """只读回测库（50万模拟盘），仅用于可用现金/持仓校验，绝不写。"""
    try:
        return json.loads(BACKTEST_DB.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[confirm] 读取回测库失败: {e}")
        return {}


def _pending_paths(kind):
    """kind='buy'/'sell'。必须排除 .confirmed.json：
    scheduler 写库后会把它改名，但文件内 status 仍是 pending，
    不排除就会让确认台把"已写库"的单子反复弹出来。"""
    today = datetime.now().strftime("%Y-%m-%d")
    pat = (f"pending_buy_confirm_*_{today}*.json" if kind == "buy"
           else f"pending_sell_confirm_{today}*.json")
    out = []
    try:
        for f in sorted(REPORTS_DIR.glob(pat), reverse=True):
            if not f.name.endswith(".confirmed.json"):
                out.append(f)

    except Exception as _e:
        log_util.quiet_warn("app.py:899", _e)

    return out


def _read_pending(kind):
    """今日最新一份、status=pending 且仍有 items 的确认单（附 _file/_kind）。"""
    for f in _pending_paths(kind):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if d.get("status") == "pending" and d.get("items"):
            d["_file"] = f.name
            d["_kind"] = kind
            return d
    return None


@app.route("/api/confirm/pending")
def api_confirm_pending():
    """返回今日待处理确认单：{buy: {...}|null, sell: {...}|null, cash: {...}}。
    为兼容可能被内置浏览器缓存的旧前端，存在买入单时同时把其字段平铺到顶层。"""
    try:
        buy = _read_pending("buy")
        sell = _read_pending("sell")
        payload = {
            "buy": buy,
            "sell": sell,
            "cash": {
                "available": round(float(_load_backtest_db().get("available_cash", 0) or 0), 2),
                "sell_inflow": round(_sell_inflow_estimate(), 2),
            },
        }
        if buy:
            payload.update(buy)
        return jsonify(payload)
    except Exception as e:
        return jsonify({"error": str(e), "buy": None, "sell": None, "cash": None})


def _lot_size_for(symbol):
    """最小交易单位（股）：科创板 688 为 200 股/手，其余 100 股/手。"""
    return 200 if str(symbol).split(".")[0].startswith("688") else 100


def _run_confirm_now(target):
    """触发 market_scheduler 定向写库，返回 (ok, detail)。
    target='sell' 或买入时段('13:40')。与桌面确认台 confirm --now 语义一致：
    同步执行 + 超时保护，避免"点了确认但账户没动"的静默失败。"""
    import subprocess
    args = [SCHEDULER_PYTHON, str(SCHEDULER_PY), "confirm", "--now"]
    args += ["--sell"] if target == "sell" else ["--session", str(target)]
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=90,
                           cwd=str(SCHEDULER_PY.parent))
        out = ((p.stdout or "") + (p.stderr or "")).strip()
        return (p.returncode == 0, out[-800:])
    except Exception as e:
        return (False, f"调用写库失败: {e}")


def _sell_inflow_estimate():
    """今日已确认卖出可回笼的现金（保守：只算已点确认 actual_sell=true 的）。"""
    d = _read_pending("sell")
    if not d:
        return 0.0
    total = 0.0
    for it in d.get("items", []):
        if it.get("actual_sell"):
            px = float(it.get("actual_sell_price") or it.get("price") or 0)
            total += px * int(it.get("actual_shares") or 0) * (1 - SELL_FEE_RATE)
    return total


@app.route("/api/confirm/buy", methods=["POST"])
def api_confirm_buy():
    """确认买入 / 不买。语义与桌面确认台(confirm_gui.py)对齐：
      · buy  → 记录 actual_lots/actual_shares（可手改手数，默认建议手数）
               + actual_price（可改成交价，默认建议价；±30% 偏离拦截，2026-09-22 新增）
      · skip → 从 items 中删除该项
      · 本单全部处理完 → 立即触发定向写库 confirm --now --session <时段>

    ⚠️ 这里**不写 status=confirmed**。status 是 scheduler 的"是否已写库"标记，
    由 confirm 流程处理完把文件改名(.confirmed.json)来体现。
    历史 bug（2026-09-22 查明）：这里抢先置 confirmed → 写库流程判定"已确认"，
    只改名不写库 → 用户在工作台确认的买入**永远进不了回测库**，
    与"50万库连续多日 0 持仓"现象吻合。

    ⚠️ 也不再写 portfolio.json（真实持仓）。理由：用户目标态是
    "真实持仓与操作建议解耦"（模拟盘归 backtest_database.json，
    portfolio.json 只放真实持仓）。旧实现会把模拟买入写进真实持仓，属数据污染。
    """
    d = request.get_json(silent=True) or {}
    file_name = d.get("file")
    symbol = d.get("symbol")
    action = d.get("action")
    if not file_name or not symbol or action not in ("buy", "skip"):
        return jsonify({"ok": False, "error": "参数缺失或 action 非法"})
    f = (REPORTS_DIR / file_name).resolve()
    if not str(f).startswith(str(REPORTS_DIR.resolve())) or not f.exists():
        return jsonify({"ok": False, "error": "confirm 文件不存在"})
    if f.name.endswith(".confirmed.json"):
        return jsonify({"ok": False, "error": "该确认单已写库完成，无需重复确认"})
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        items = data.get("items", [])
        if action == "buy":
            item = next((i for i in items if i["symbol"] == symbol), None)
            if not item:
                return jsonify({"ok": False, "error": "symbol 不在 confirm 列表"})
            lot_size = _lot_size_for(symbol)
            # 手数：前端可传 actual_lots（手）；缺省用建议手数
            try:
                lots = int(d.get("actual_lots") or 0)
            except Exception:
                lots = 0
            if lots <= 0:
                lots = int(item.get("suggest_lots") or 0)
            if lots <= 0:
                lots = int(item.get("suggest_shares") or 0) // lot_size
            if lots <= 0:
                return jsonify({"ok": False, "error": "手数为 0，无法确认买入"})
            shares = lots * lot_size
            # 成交价（2026-09-22 新增）：前端可传 actual_price；缺省回退建议价。
            # 合法性：>0 且相对建议价偏离 ≤±30%（拦手滑 ×10/÷10；极端行情可改 JSON 兜底）
            suggest = float(item.get("suggest_price") or 0)
            try:
                px = float(d.get("actual_price") or 0)
            except Exception:
                px = 0.0
            if px <= 0:
                px = suggest
            if px <= 0:
                return jsonify({"ok": False, "error": "买入价无效（建议价为 0 且未填成交价）"})
            if suggest > 0 and not (0.7 <= px / suggest <= 1.3):
                return jsonify({"ok": False, "kind": "price_out_of_range",
                                "suggest_price": suggest,
                                "error": (f"成交价 ¥{px:g} 与建议价 ¥{suggest:g} 偏离超过 30%，"
                                          "疑似输错。请核对后重填；确属极端行情请改 JSON 文件里的 suggest_price")})
            price = px
            # ③ 买入超额现金校验（与 confirm_gui.py 同口径，含已确认卖出回笼）
            db = _load_backtest_db()
            cash = float(db.get("available_cash", 0) or 0) + _sell_inflow_estimate()
            committed = 0.0
            for i in items:
                if i["symbol"] == symbol:
                    continue
                if int(i.get("actual_lots") or 0) > 0:
                    committed += (float(i.get("actual_price") or i.get("suggest_price") or 0)
                                  * int(i.get("actual_shares") or 0) * (1 + BUY_FEE_RATE))
            need = price * shares * (1 + BUY_FEE_RATE)
            if need + committed > cash:
                return jsonify({
                    "ok": False, "kind": "insufficient_cash",
                    "available_cash": round(cash, 2),
                    "required": round(need + committed, 2),
                    "error": (f"可用现金不足：本笔需 ¥{need:,.0f}"
                              + (f"，本单其他已确认项占 ¥{committed:,.0f}" if committed else "")
                              + f"，可用 ¥{cash:,.0f}（含已确认卖出回笼）。"
                                f"可减少手数，或先到「待确认卖出」确认卖出后再买。")})
            for i in items:
                if i["symbol"] == symbol:
                    i["actual_lots"] = lots
                    i["actual_shares"] = shares
                    i["actual_price"] = round(price, 3)
                    break
        else:   # skip：删除该项，等价于"这只不买"
            items = [i for i in items if i["symbol"] != symbol]
            data["items"] = items
        # 本单是否已全部处理完（skip 已删项，故剩下的都是已确认买入）
        all_done = (not items) or all(i.get("actual_lots", 0) > 0 for i in items)
        f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        # ② 立即写库：只在全单处理完时触发一次（避免每次点击都全量扫描）
        db_ok, db_msg, db_detail = None, "", ""
        if all_done and data.get("session"):
            db_ok, db_detail = _run_confirm_now(data["session"])
            db_msg = ("✅ 已立即写入 50万模拟盘账户" if db_ok
                      else "⚠️ 立即写库未完成，15:00 兜底流程会再试一次")
        return jsonify({
            "ok": True, "status": data.get("status"),
            "remaining": len(items), "items": items, "all_done": all_done,
            "db_written": db_ok, "db_message": db_msg,
            "db_detail": (db_detail or "")[-300:],
            "date": data.get("date"), "session": data.get("session"),
            "deadline": data.get("deadline"),
        })
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)})
    except Exception as e:
        return jsonify({"ok": False, "error": f"系统错误: {e}"})


@app.route("/api/confirm/sell", methods=["POST"])
def api_confirm_sell():
    """确认卖出 / 不卖（ADR-0032 新增，补齐桌面确认台的卖出确认能力）。
      · sell → actual_sell=true + actual_lots/actual_shares/actual_sell_price
      · skip → actual_sell=false，并标 resolved
      · 全部 resolved → 立即触发定向写库 confirm --now --sell

    与买入的"删除该项"语义不同：卖出单是当日唯一一份、且会被 10:40 重写，
    删除后会被重新提出，故这里用 resolved 标记而保留记录。
    校验与 confirm_gui.py 对齐：手数 >0 且不超过持仓手数；填满即视为全清
    （scheduler 侧会连带清掉历史零股）。"""
    d = request.get_json(silent=True) or {}
    file_name = d.get("file")
    symbol = d.get("symbol")
    action = d.get("action")
    if not file_name or not symbol or action not in ("sell", "skip"):
        return jsonify({"ok": False, "error": "参数缺失或 action 非法"})
    f = (REPORTS_DIR / file_name).resolve()
    if not str(f).startswith(str(REPORTS_DIR.resolve())) or not f.exists():
        return jsonify({"ok": False, "error": "confirm 文件不存在"})
    if f.name.endswith(".confirmed.json"):
        return jsonify({"ok": False, "error": "该确认单已写库完成，无需重复确认"})
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        items = data.get("items", [])
        item = next((i for i in items if i["symbol"] == symbol), None)
        if not item:
            return jsonify({"ok": False, "error": "symbol 不在 confirm 列表"})
        lot_size = _lot_size_for(symbol)
        held_lots = int(item.get("held_lots") or 0)
        if held_lots <= 0:
            held_lots = int(item.get("held_shares") or 0) // lot_size
        if action == "sell":
            try:
                lots = int(d.get("actual_lots") or 0)
            except Exception:
                lots = 0
            if lots <= 0:
                lots = int(item.get("recommend_sell_lots") or 0)
            if lots <= 0:
                return jsonify({"ok": False,
                                "error": "卖出 0 手无意义；若不卖出请点「不卖」"})
            # 卖出超持仓校验
            if held_lots > 0 and lots > held_lots:
                return jsonify({"ok": False, "kind": "over_hold",
                                "held_lots": held_lots, "error":
                                f"卖出 {lots} 手超过持仓 {held_lots} 手"})
            # 手数填满持仓 = 全清（连带零股）
            if held_lots > 0 and lots >= held_lots:
                shares = int(item.get("held_shares") or (lots * lot_size))
            else:
                shares = lots * lot_size
            price = float(d.get("actual_sell_price") or 0) or float(item.get("price") or 0)
            item["actual_sell"] = True
            item["actual_lots"] = lots
            item["actual_shares"] = shares
            item["actual_sell_price"] = price
            item["resolved"] = True
        else:
            item["actual_sell"] = False
            item["actual_lots"] = 0
            item["actual_shares"] = 0
            item["resolved"] = True
        all_done = (not items) or all(i.get("resolved") for i in items)
        f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        db_ok, db_msg, db_detail = None, "", ""
        if all_done:
            db_ok, db_detail = _run_confirm_now("sell")
            db_msg = ("✅ 已立即写入 50万模拟盘账户" if db_ok
                      else "⚠️ 立即写库未完成，15:00 兜底流程会再试一次")
        return jsonify({
            "ok": True, "status": data.get("status"),
            "remaining": sum(1 for i in items if not i.get("resolved")),
            "items": items, "all_done": all_done,
            "db_written": db_ok, "db_message": db_msg,
            "db_detail": (db_detail or "")[-300:],
            "date": data.get("date"), "deadline": data.get("deadline"),
        })
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)})
    except Exception as e:
        return jsonify({"ok": False, "error": f"系统错误: {e}"})


# ---- 持仓（手动录入，只读行情估值，不碰券商账号） ----
@app.route("/api/portfolio", methods=["GET"])
def api_portfolio_get():
    return jsonify(portfolio.valuation())


@app.route("/api/portfolio", methods=["POST"])
def api_portfolio_post():
    d = request.get_json(silent=True) or {}
    action = d.get("action")
    try:
        if action == "add":
            positions = portfolio.add_position(
                d["code"], d.get("name", ""), d["cost"], d["shares"],
                d.get("note", ""))
        elif action == "update":
            positions = portfolio.update_position(
                d["code"], d["cost"], d["shares"],
                name=d.get("name"), note=d.get("note"))
        elif action == "remove":
            positions = portfolio.remove_position(d["code"])
        else:
            return jsonify({"ok": False, "error": "action 须为 add/update/remove"})
        return jsonify({"ok": True, "positions": positions})
    except (KeyError, ValueError) as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/health")
def api_health():
    """体检 P2-4：可读的健康快照 —— 线程存活 / 数据新鲜度 / 错误 / 缓存 / 题材索引。
    纯内存读取，不做任何网络调用，可作为监控探活端点。"""
    out = {
        "ok": True,
        "time": data_fetcher.datetime.now().strftime("%H:%M:%S"),
        "build_id": BUILD_ID,
        "uptime_s": int(time.time() - STARTED_AT),
    }
    try:
        threads = [t.name for t in threading.enumerate() if t.is_alive()]
        out["threads"] = {"alive_count": len(threads), "names": sorted(set(threads))}
    except Exception as e:
        out["threads"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        snap = md.snapshot()
        out["data"] = {
            "last_update": snap.get("last_update") or {},
            "zt_count": snap.get("zt_count"),
            "errors_recent": (snap.get("errors") or [])[-3:],
        }
    except Exception as e:
        out["data"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        out["theme"] = tboard.snapshot()
    except Exception as e:
        out["theme"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        with _SLOW_LOCK:
            out["slow_cache"] = {
                k: {"age_s": round(time.time() - v["at"], 1)}
                for k, v in _SLOW_CACHE.items()
            }
    except Exception as e:
        out["slow_cache"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        out["suppressed_exceptions"] = log_util.stats()
    except Exception as e:
        out["suppressed_exceptions"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        import review_gate
        out["gate"] = review_gate.health() if hasattr(review_gate, "health") else "n/a"
    except Exception as e:
        out["gate"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        out["first_board"] = {
            "v2": getattr(config, "FB_V2_ENABLED", False),
            "news": nc.snapshot(),
            "sprite": sprite.instance().snapshot(limit=1).get("fb") or {},
            "prev_zt": {"count": len(_prev_zt_codes()),
                        "source_date": _prev_zt_cache.get("source_date")},
            "trends": _trend_names_provider()[:8],
        }
    except Exception as e:
        out["first_board"] = {"error": f"{type(e).__name__}: {e}"}
    return jsonify(out)


# ---- 自选股池（DK点操盘线） ----
@app.route("/api/watchlist", methods=["GET"])
def api_watchlist_get():
    """自选股列表 + 实时行情 + DK 指标（D/K拐点·当前红/蓝线·信号后涨幅）。"""
    return jsonify(watchlist.evaluate())


@app.route("/api/watchlist", methods=["POST"])
def api_watchlist_post():
    """自选股 CRUD：action=add|remove|clear_cache。"""
    d = request.get_json(silent=True) or {}
    action = d.get("action")
    try:
        if action == "add":
            items = watchlist.add(d["code"], d.get("name", ""))
            return jsonify({"ok": True, "items": items})
        elif action == "remove":
            items = watchlist.remove(d["code"])
            return jsonify({"ok": True, "items": items})
        elif action == "clear_cache":
            # 强制重拉 K 线（盘中调过 K 线历史时用）
            import shutil
            cache = watchlist.CACHE_DIR
            if cache.exists():
                shutil.rmtree(cache)
            return jsonify({"ok": True, "cleared": True})
        else:
            return jsonify({"ok": False, "error": "action 须为 add/remove/clear_cache"})
    except (KeyError, ValueError) as e:
        return jsonify({"ok": False, "error": str(e)})
    except Exception as e:
        return jsonify({"ok": False, "error": f"系统错误: {e}"})


if __name__ == "__main__":
    print("=" * 56)
    print("  A股市场监控台 · 通达信风格实时看板")
    print(f"  访问: http://{config.HOST}:{config.PORT}")
    print(f"  构建戳: {BUILD_ID}   ← 重启后会变，前端据此检测新版")
    print("  Ctrl+C 退出")
    print("=" * 56)
    md.start()
    adv.start()   # ADR-0020：盘中自动跑 advisor（窗口不依赖用户打开工作台触发）
    hk.start()
    us.start()
    fut.start()
    fut_strategy.start()   # M5：期货策略（K线缓存线程）
    lk.start()
    fadv.start()   # M6：期货超短顾问（主力探测+实时/分时+快讯线程）
    brank.start()  # M7：板块榜（腾讯官方涨速 → 短线精灵板块/概念涨速事件）
    tboard.start()  # M10：细分题材反向索引（缓存命中则跳过，失败降级行业兜底）
    start_strategy()  # 首板雷达 v2：趋势题材 / 昨日涨停基线 / 快讯催化（后台低频）
    bottom_radar.instance().warm()  # 体检剩余项：预热慢接口，首个请求不再阻塞 13s
    pwatch.start() # M9：持仓实时跟踪（交易时段逐帧评估 50 万模拟盘持仓 → 分层建议+异动）
    app.run(host=config.HOST, port=config.PORT,
            debug=False, threaded=True, use_reloader=False)

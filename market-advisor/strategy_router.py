# -*- coding: utf-8 -*-
"""多策略路由层（2026-11-17，ADR-0051）

用户诉求原话：
  「策略要有很多，根据大盘混合着用。大盘不好就改策略防守，大盘好就激进打板。
    张江高科底部反弹之后每天小阴小阳一路涨、牧原猪肉、很多防守板块…」

根因（实测 2026-11-17）：候选池当天有 44 只票（底部放量/龙头资金/情报/复盘重点都在），
但 market_scheduler 的 risk_gate_triggered 在温度<35 时【一刀切清空全部买入候选】，
且 _sentiment_pos_factor(temp<40)=0 → 冰点天永远空仓、没有任何防守赛道。
这不是「没票」，是「有票但被单一打板闸门全砍」，与用户「大盘不好改防守」直接冲突。

本模块职责（单一、纯函数、fail-open）：
  根据大盘状态识别策略模式，输出该模式应【放行哪些供票源 + 仓位上限 + 选股侧重】。
  不下单、不直接改候选，只产出路由决策 dict，由调用方决定如何落地。

四种模式（借鉴图片「打板七要素」+ 用户防守诉求）：
  AGGRESSIVE 打板进攻：温度≥55 且情绪非退潮 → 放行连板/题材/首板/午后强势，仓位高
  TREND      趋势跟随：温度45~55 或情绪发酵/确认 → 放行龙头资金/趋势票/复盘重点，中仓
  REVERSAL   底部反转：温度35~45 或情绪冰点/分歧 → 放行底部放量/龙首阴/自选/情报低吸，轻仓
  DEFENSE    防守收缩：温度<35 或退潮 → 放行防守型来源(底部放量/情报权重/自选)，极轻仓

重要：防守/反转不是「放宽打板标准去凑单」，而是【换一套选股逻辑】——
  打板看强度(连板/封板)，防守看位置(底部/低估值/权重)与催化(情报)，两者独立打分。
  仍遵守用户「宁可错过不可乱做」：每个模式都要过自己的门槛，不是无脑放行。

自检：/opt/homebrew/anaconda3/bin/python3 -B strategy_router.py --selftest
"""
from __future__ import annotations

# 四种模式常量
MODE_AGGRESSIVE = "AGGRESSIVE"   # 打板进攻
MODE_TREND = "TREND"             # 趋势跟随
MODE_REVERSAL = "REVERSAL"       # 底部反转
MODE_DEFENSE = "DEFENSE"         # 防守收缩

MODE_LABEL = {
    MODE_AGGRESSIVE: "🚀 打板进攻",
    MODE_TREND: "📈 趋势跟随",
    MODE_REVERSAL: "🔄 底部反转",
    MODE_DEFENSE: "🛡️ 防守收缩",
}

# 每种模式放行的供票源（对应 market_scheduler contribs 的 source 值）
# 防守/反转为「低位+权重+催化」逻辑，与打板的「强度」逻辑物理隔离
# 【2026-11-18 · ADR-0042】新增「盘后作战」源（龙虎榜净买/主力净流入低吸票，
#   盘后复盘 17:35 产出 battle_plan_latest.json）——四个模式均放行：
#   该源 ≤6 只、资金净买背书、天然非一字，属于防守型低吸催化，不与打板强度逻辑冲突。
SOURCE_BY_MODE = {
    MODE_AGGRESSIVE: {
        "连板主线", "题材双雄", "底部突围", "午后强势", "补涨·首阴",
        "补涨池·高低切", "龙首阴", "底部放量", "题材套利", "复盘重点", "自选", "情报",
        "盘后作战",
    },
    MODE_TREND: {
        "龙头资金", "复盘重点", "自选", "情报", "题材双雄", "底部放量", "龙首阴",
        "盘后作战",
    },
    MODE_REVERSAL: {
        "底部放量", "龙首阴", "自选", "情报", "复盘重点", "补涨·首阴",
        "盘后作战",
    },
    MODE_DEFENSE: {
        "底部放量", "情报", "自选", "复盘重点", "盘后作战",
    },
}

# 每种模式的仓位上限（占可用现金比例）与最多只数
POSITION_BY_MODE = {
    MODE_AGGRESSIVE: {"max_pos": 0.45, "max_picks": 2},
    MODE_TREND:      {"max_pos": 0.30, "max_picks": 2},
    MODE_REVERSAL:   {"max_pos": 0.20, "max_picks": 2},
    MODE_DEFENSE:    {"max_pos": 0.15, "max_picks": 1},
}

# 退潮/冰点等情绪阶段（复用 mood_engine.cycle_judge 的 stage 取值）
_RISK_STAGES = {"退潮", "冰点"}
_WARM_STAGES = {"发酵", "加速", "确认"}
_MID_STAGES = {"启动", "分歧", "反包", "观察"}


def _f(v, default=None):
    """安全转 float；None/异常返回 default。"""
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def judge_mode(temp=None, stage=None, cy_pct=None, sh_pct=None, sz_pct=None,
               up=None, down=None, broken_ratio=None, limit_down=None) -> dict:
    """根据大盘状态识别当前应采用的策略模式。

    参数均可缺省（fail-open：缺数据不臆造，退回 TREND 中性模式）。
    返回 dict：
      mode       模式常量
      label      中文标签
      sources    该模式放行来源集合(set)
      max_pos    仓位上限(占可用现金比例)
      max_picks  最多买入只数
      reason     人类可读的判定理由
      gate_open  是否允许开新仓（真·系统性风险日= False，防守模式仍 True）
    """
    t = _f(temp, 50.0)
    st = (stage or "").strip()
    # 完全无大盘数据 → fail-open 到中性趋势模式（不臆造冰点/退潮）
    if temp is None and not st:
        return _pack(MODE_TREND, "无大盘状态数据 → 中性趋势模式（fail-open，不臆造）")
    cy = _f(cy_pct)
    sh = _f(sh_pct)
    sz = _f(sz_pct)
    br = _f(broken_ratio)
    ld = _f(limit_down)
    _u, _d = _f(up), _f(down)
    ratio = None
    if _u is not None and _d and _d > 0:
        ratio = _u / _d

    # 真·系统性风险：创业板暴跌 / 沪深双杀 / 大面积跌停 → 唯一允许空仓的情形
    # （保留用户「宁可错过不可乱做」底线，只有这种极端日才彻底关闸）
    systemic = (
        (cy is not None and cy <= -3.0)
        or (sh is not None and sz is not None and sh <= -2.0 and sz <= -2.5)
        or (ld is not None and ld >= 30)
    )
    if systemic:
        bits = []
        if cy is not None and cy <= -3.0:
            bits.append(f"创业板{cy:.1f}%")
        if sh is not None and sz is not None and sh <= -2.0 and sz <= -2.5:
            bits.append(f"沪深双杀{sh:.1f}/{sz:.1f}%")
        if ld is not None and ld >= 30:
            bits.append(f"跌停{int(ld)}家")
        return _pack(MODE_DEFENSE,
                     "真·系统性风险（%s）→ 全面空仓，唯一允许完全不开仓的情形" % "、".join(bits),
                     gate_open=False)

    # 温度为主轴，情绪阶段做修正；两者取更保守者
    if st in _RISK_STAGES:
        mode = MODE_DEFENSE if (st == "退潮" or t < 40) else MODE_REVERSAL
        reason = f"情绪阶段「{st}」+ 温度{t:.0f} → 防守优先，切低位/权重赛道"
    elif t < 35:
        mode = MODE_DEFENSE
        reason = f"温度{t:.0f}<35 冰点 → 不打板，切防守型低吸（底部/权重/催化）"
    elif t < 45:
        mode = MODE_REVERSAL
        reason = f"温度{t:.0f} 35~45 → 底部反转窗口，做超跌企稳+温和放量"
    elif t < 55:
        mode = MODE_TREND if st in _WARM_STAGES | _MID_STAGES else MODE_REVERSAL
        reason = f"温度{t:.0f} 45~55 → {'趋势跟随主线' if mode == MODE_TREND else '题材未成主线，低位反转为主'}"
    else:
        if br is not None and br >= 40:
            mode = MODE_REVERSAL
            reason = f"温度{t:.0f}≥55 但炸板率{br:.0f}%≥40 → 高标不稳，降档做反转"
        elif st in _RISK_STAGES:
            mode = MODE_REVERSAL
            reason = f"温度{t:.0f}高但情绪{st} → 不追高，做反转"
        else:
            mode = MODE_AGGRESSIVE
            reason = f"温度{t:.0f}≥55 且情绪{'/'.join([st]) if st else '正常'} → 打板进攻，强者恒强"

    # 涨跌比极差时再降一档（人气不在，防守优先——对应七要素「人气」）
    if ratio is not None and ratio < 0.5 and mode in (MODE_AGGRESSIVE, MODE_TREND):
        mode = MODE_DEFENSE if t < 40 else MODE_REVERSAL
        reason += f"；涨跌比{ratio:.2f}赚钱效应差，再降档防守"

    return _pack(mode, reason, gate_open=True)


def _pack(mode, reason, gate_open=True):
    cfg = POSITION_BY_MODE[mode]
    return {
        "mode": mode,
        "label": MODE_LABEL[mode],
        "sources": set(SOURCE_BY_MODE[mode]),
        "max_pos": cfg["max_pos"],
        "max_picks": cfg["max_picks"],
        "reason": reason,
        "gate_open": gate_open,
    }


def allow_source(route: dict, sources) -> bool:
    """某候选的来源集合是否在当前模式放行白名单内。"""
    if not route or route.get("gate_open") is False:
        return False
    allow = route.get("sources") or set()
    if isinstance(sources, str):
        sources = [sources]
    return bool(allow & set(sources or []))


# ---------- 图片公式落地：跟庄建仓 XG（低位启动）----------
def genzhuang_xg(closes, highs, lows, vols) -> dict:
    """把图片「跟庄建仓」通达信公式翻译为 Python（逐行对应）。

    AA:=100*(C-LLV(L,30))/(HHV(H,30)-LLV(L,30)+0.001);
    BB:=EMA(AA,8);                              # 原公式定义但未在 XG 使用，保留供观察
    CC:=(AA-REF(AA,3))*100/(REF(AA,3)+0.001);
    DD:=MA(V,5)>MA(V,10);
    EE:=C>REF(C,1);
    XG:AA<13 AND CC>13 AND DD AND EE;

    返回 dict(aa, cc, dd, ee, hit, desc)。数据不足(需≥31根)返回 hit=False。
    """
    n = 30
    if not (closes and highs and lows and vols) or len(closes) < n + 1:
        return {"aa": None, "cc": None, "dd": None, "ee": None,
                "hit": False, "desc": "数据不足(需≥31根K线)"}
    try:
        C, H, L, V = closes[-1], highs[-1], lows[-1], vols[-1]
        llv = min(lows[-n:])
        hhv = max(highs[-n:])
        aa = 100.0 * (C - llv) / (hhv - llv + 0.001)
        aa_3 = 100.0 * (closes[-4] - min(lows[-33:-3])) / (max(highs[-33:-3]) - min(lows[-33:-3]) + 0.001)
        cc = (aa - aa_3) * 100.0 / (aa_3 + 0.001)
        dd = (sum(vols[-5:]) / 5.0) > (sum(vols[-10:]) / 10.0)
        ee = C > closes[-2]
        hit = bool(aa < 13 and cc > 13 and dd and ee)
        return {"aa": round(aa, 2), "cc": round(cc, 2), "dd": bool(dd), "ee": bool(ee),
                "hit": hit,
                "desc": f"AA={aa:.1f}(需<13低位) CC={cc:.1f}(需>13拐头) 放量={dd} 收阳={ee} → {'命中跟庄建仓' if hit else '未命中'}"}
    except Exception as e:
        return {"aa": None, "cc": None, "dd": None, "ee": None,
                "hit": False, "desc": f"计算异常: {type(e).__name__}: {e}"}


# ---------- 图片「打板七要素」评分框架 ----------
# 人气/合力/换手/量能/强度 (盘中) + 股性/题材 (复盘)
SEVEN_FACTORS = [
    ("人气", "大盘与板块两层面，人气所在相对安全"),
    ("合力", "市场资金共同推动，非一方单独坐庄"),
    ("换手", "换手是否合适，看盘面功力"),
    ("量能", "量能放大是否健康，无绝对标准"),
    ("强度", "封板快不快、坚决不坚决"),
    ("股性", "平时活跃度 + 涨停后次日溢价"),
    ("题材", "题材够新够大，决定资金跟进意愿"),
]


def selftest():
    print("=== strategy_router selftest ===")
    # [1] 冰点防守（今天实况 temp 23~32）
    for t in (23, 28, 32):
        r = judge_mode(temp=t, stage="退潮")
        assert r["mode"] == MODE_DEFENSE, (t, r)
        assert r["gate_open"] is True, "防守模式仍允许低吸，不是全面空仓"
        assert r["max_pos"] <= 0.15
    print("[1] 冰点防守 OK:", judge_mode(temp=28, stage="退潮")["reason"])

    # [2] 高温打板
    r = judge_mode(temp=70, stage="发酵")
    assert r["mode"] == MODE_AGGRESSIVE
    assert "连板主线" in r["sources"]
    print("[2] 打板进攻 OK:", r["reason"])

    # [3] 趋势 / 反转
    assert judge_mode(temp=50, stage="确认")["mode"] == MODE_TREND
    assert judge_mode(temp=40, stage="分歧")["mode"] == MODE_REVERSAL
    print("[3] 趋势/反转 OK")

    # [4] 真系统性风险 → 唯一全面空仓
    r = judge_mode(temp=20, cy_pct=-4.0, stage="退潮")
    assert r["gate_open"] is False
    assert not allow_source(r, ["底部放量"])
    print("[4] 系统性风险空仓 OK:", r["reason"])

    # [5] 白名单放行判定
    r = judge_mode(temp=28, stage="退潮")
    assert allow_source(r, ["底部放量"])
    assert allow_source(r, ["情报", "自选"])
    assert not allow_source(r, ["连板主线"])   # 冰点不放行打板
    print("[5] 来源放行 OK")

    # [6] 跟庄建仓公式：构造低位拐头样例（需≥33根：30日区间 + REF前移3根）
    #    30日区间 8~12，今日收盘 8.4(AA≈10 低位)，3日前 8.05(AA≈1.25)；放量收阳 → 应命中
    closes = [10.0] * 29 + [8.05, 8.15, 8.25, 8.4]
    highs = [12.0] * 33
    lows = [8.0] * 33
    vols = [100.0] * 28 + [300.0] * 5
    r = genzhuang_xg(closes, highs, lows, vols)
    assert r["aa"] is not None and r["aa"] < 13 and r["cc"] > 13 and r["hit"], r
    print("[6] 跟庄建仓公式 OK:", r["desc"])

    # [7] 数据不足 fail-open
    r = genzhuang_xg([1, 2, 3], [1, 2, 3], [1, 2, 3], [1, 2, 3])
    assert r["hit"] is False
    print("[7] 数据不足 fail-open OK")

    # [8] 缺参数 fail-open → TREND
    r = judge_mode()
    assert r["mode"] == MODE_TREND
    print("[8] 全缺省 OK:", r["mode"])

    print("[selftest] ALL PASS")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        selftest()
    else:
        import json
        r = judge_mode(temp=float(sys.argv[1]) if len(sys.argv) > 1 else None)
        print(json.dumps({**r, "sources": sorted(r["sources"])}, ensure_ascii=False, indent=2))

# -*- coding: utf-8 -*-
"""ADR-0041 常驻集成测试：三个新供票源（复盘重点 / 自选 / 题材套利）全链验证。

跑法：
  /opt/homebrew/anaconda3/bin/python3 test_adr0041_push_sources.py

覆盖：
  1) load_review_focus_rows：真实 v2 闸门（10-08）→ 只放「非一字板 + 板数达标」的风向标
  2) 时段门控（仅 13:40/14:40）+ shadow/hard_block/缺 board_days/板数超标 全部不放行
  3) load_self_watchlist_rows：自选股读取 + ST/坏代码过滤
  4) fetch_theme_arb_rows：只收 抢筹逼近/跟涨强、去重、上限、HTTP 失败 fail-open
  5) contribs 注入 + build_operation_pool 合并 + rank_operation_pool 的 rv_bonus=32 精确校验
  6) 白名单：新三源在 13:40 放行、陌生源仍被拦
  7) 推票解释性：_source_flash_line / order_plan.push_sources / 操作项 note 的「选股源」标签

隔离铁律（ADR-0040 测试同款）：REPORTS_DIR 与 BACKTEST_DB_FILE 都是**导入期常量**，
必须两个一起改到 tmp，否则会读写真实 reports/ 目录。
"""
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, "/Users/williamli/Work/market-monitor")

import market_scheduler as ms   # noqa: E402

TMP = tempfile.mkdtemp(prefix="adr0041_")
# load_review_gate 校验 asof==今天、rank_operation_pool 校验 last_seen==今天，二者都必须用真实今天
TODAY = datetime.now().strftime("%Y-%m-%d")
OLD_REPORTS = ms.REPORTS_DIR
OLD_DB = ms.BACKTEST_DB_FILE
ms.REPORTS_DIR = TMP
ms.BACKTEST_DB_FILE = os.path.join(TMP, "backtest_database.json")

_OK = [0]
_BAD = [0]


def chk(cond, msg):
    if cond:
        _OK[0] += 1
        print("  ✅ " + msg)
    else:
        _BAD[0] += 1
        print("  ❌ " + msg)


def cleanup():
    shutil.rmtree(TMP, ignore_errors=True)
    ms.REPORTS_DIR = OLD_REPORTS
    ms.BACKTEST_DB_FILE = OLD_DB


def write_gate(d):
    p = os.path.join(TMP, "trade_gate.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    ms.REVIEW_GATE_FILE = p
    return p


def main():
    print("=" * 68)
    print("ADR-0041 推票供票源集成测试")
    print("=" * 68)

    # ---------- 1) 真实 v2 闸门（只读，不落盘） ----------
    print("\n[1] 真实 10-08 闸门 → load_review_focus_rows")
    real_gate_path = os.path.join(OLD_REPORTS, "trade_gate.json")
    if os.path.exists(real_gate_path):
        rg = json.load(open(real_gate_path, encoding="utf-8"))
        chk(rg.get("version") == 2, "真实闸门已是 v2（version=%s）" % rg.get("version"))
        chk(all(f.get("board_days") for f in rg.get("focus") or []),
            "真实闸门 focus 全部带 board_days")
        # 真实数据走全路径：把真实闸门写入 tmp 并把 asof 设为今天（模拟"当天就是 10-08"）
        write_gate(dict(rg, asof=TODAY))
        rows = ms.load_review_focus_rows("13:40")
        names = [r.get("name") for r in rows]
        one_words = {f.get("name") for f in (rg.get("focus") or []) if f.get("one_word")}
        over = {f.get("name") for f in (rg.get("focus") or [])
                if (f.get("board_days") or 0) > (rg.get("max_board_days") or 99)}
        chk(not (set(names) & (one_words | over)),
            "一字板(%s)与超板数(%s)均被排除" % (one_words or "-", over or "-"))
        chk(len(rows) >= 1, "放行 %d 只：%s" % (len(rows), names))
        chk(all((r.get("board_days") or 0) <= (rg.get("max_board_days") or 99) for r in rows),
            "放行板数全部 ≤ max_board_days=%s" % rg.get("max_board_days"))
    else:
        print("  ⚠️ 找不到真实闸门，跳过该组")

    # ---------- 2) 时段门控 ----------
    print("\n[2] 时段门控")
    chk(ms.load_review_focus_rows("09:25") == [], "09:25 打板时段不接复盘重点源")
    chk(ms.load_review_focus_rows("10:40") == [], "非买入时段不接复盘重点源")
    chk(ms.fetch_theme_arb_rows("09:25") == [], "09:25 不接题材套利源")

    # ---------- 3) 合成闸门的各种不放行分支 ----------
    print("\n[3] 合成闸门分支（tmp 隔离）")
    base = {"version": 2, "mode": "enforce", "asof": TODAY, "stage": "确认",
            "hard_block": False, "max_board_days": 3,
            "focus": [
                {"name": "一字龙", "symbol": "600001", "code": "600001",
                 "one_word": True, "board_days": 5, "kind_label": "空间龙头"},
                {"name": "高位票", "symbol": "600002", "code": "600002",
                 "one_word": False, "board_days": 4, "kind_label": "换手接力"},
                {"name": "无板数", "symbol": "600003", "code": "600003",
                 "one_word": False, "kind_label": "弱转强"},
                {"name": "可参与", "symbol": "600004", "code": "600004",
                 "one_word": False, "board_days": 2, "kind_label": "换手接力"},
            ]}
    write_gate(base)
    r = ms.load_review_focus_rows("13:40")
    chk([x["name"] for x in r] == ["可参与"],
        "只放行 可参与（一字板/4板>3/缺board_days 均不放行）→ %s" % [x["name"] for x in r])

    ms.REVIEW_GATE_FILE = os.path.join(TMP, "none.json")   # 不存在 → fail-open
    chk(ms.load_review_focus_rows("13:40") == [], "闸门文件缺失 → 空（fail-open）")

    write_gate(dict(base, mode="shadow"))
    chk(ms.load_review_focus_rows("13:40") == [], "shadow 模式闸门 → 复盘重点源整体不启用")

    write_gate(dict(base, hard_block=True))
    chk(ms.load_review_focus_rows("13:40") == [], "hard_block 闸门 → 不加票（由 apply 统一清空）")

    # ---------- 4) 自选股 ----------
    print("\n[4] load_self_watchlist_rows")
    wl = os.path.join(TMP, "watchlist.json")
    with open(wl, "w", encoding="utf-8") as f:
        json.dump([{"code": "600699", "name": "均胜电子"},
                   {"code": "600707", "name": "ST 坏例子"},
                   {"code": "abc", "name": "坏代码"},
                   {"code": "000011", "name": "深物业A"}], f, ensure_ascii=False)
    ms.WATCHLIST_JSON = wl
    s = ms.load_self_watchlist_rows()
    chk([x["name"] for x in s] == ["均胜电子", "深物业A"],
        "自选读取 + ST/坏代码过滤 → %s" % [x["name"] for x in s])
    chk(all(len(ms._norm_code(x["symbol"])) == 6 for x in s), "自选代码全部归一为 6 位")
    ms.WATCHLIST_JSON = os.path.join(TMP, "nope.json")
    chk(ms.load_self_watchlist_rows() == [], "自选文件缺失 → 空（fail-open）")

    # ---------- 5) 题材套利 ----------
    print("\n[5] fetch_theme_arb_rows（mock HTTP）")
    payload = {"pool": [
        {"code": "600004", "name": "可参与A", "theme_name": "固态电池", "state": "抢筹逼近",
         "state_cls": "near", "gap_zt": 0.8, "pct": 8.9, "leader_label": "龙头X·2板"},
        {"code": "600005", "name": "封板中", "theme_name": "固态电池", "state": "封板中",
         "state_cls": "zt", "gap_zt": 0.0, "pct": 10.0, "leader_label": "龙头X·2板"},
        {"code": "600006", "name": "跟涨强", "theme_name": "军工", "state": "跟涨强",
         "state_cls": "strong", "gap_zt": 3.1, "pct": 5.5, "leader_label": "龙头Y·3板"},
        {"code": "600007", "name": "走弱", "theme_name": "军工", "state": "走弱",
         "state_cls": "down", "gap_zt": None, "pct": -1.2, "leader_label": "龙头Y·3板"},
        {"code": "600004", "name": "重复", "theme_name": "固态电池", "state": "跟涨强",
         "state_cls": "strong", "gap_zt": 1.0, "pct": 8.0, "leader_label": "龙头X·2板"},
        {"code": "bad", "name": "坏代码", "theme_name": "x", "state": "跟涨强",
         "state_cls": "strong", "gap_zt": 1.0, "pct": 5.0, "leader_label": "z"},
    ]}

    class _R:
        status_code = 200

        def json(self):
            return payload

    ms.requests.get = lambda *a, **k: _R()
    t = ms.fetch_theme_arb_rows("13:40")
    chk([x["name"] for x in t] == ["可参与A", "跟涨强"],
        "只收 抢筹逼近/跟涨强 且去重/滤坏代码 → %s" % [x["name"] for x in t])
    chk(t[0]["theme_name"] == "固态电池" and t[0]["leader_label"] == "龙头X·2板",
        "题材/龙头信息随行携带（供 thesis）")

    def _boom(*a, **k):
        raise RuntimeError("workbench down")

    ms.requests.get = _boom
    chk(ms.fetch_theme_arb_rows("13:40") == [], "HTTP 失败 → 空（fail-open，不阻断主流程）")

    # ---------- 6) contribs + 池 + 排序 rv_bonus ----------
    print("\n[6] contribs 注入 → build_operation_pool → rank_operation_pool")
    contribs = ms._gather_pool_contributions(
        {}, [], [], {}, {}, [], [], [],
        bottom_surge_rows=[],
        review_focus_rows=[{"symbol": "600004", "name": "可参与A", "board_days": 1,
                            "kind_label": "换手接力", "why": "2板有量能承接"}],
        self_rows=[{"symbol": "600699", "name": "均胜电子"}],
        theme_arb_rows=[{"symbol": "600006", "name": "跟涨强", "theme_name": "军工",
                         "leader_label": "龙头Y·3板", "state": "跟涨强", "gap_zt": 3.1,
                         "pct": 5.5}])
    src_map = {}
    for c in contribs:
        src_map.setdefault(c["source"], []).append(c["name"])
    chk(src_map.get("复盘重点") == ["可参与A"], "复盘重点 contrib 注入 → %s" % src_map.get("复盘重点"))
    chk(src_map.get("自选") == ["均胜电子"], "自选 contrib 注入 → %s" % src_map.get("自选"))
    chk(src_map.get("题材套利") == ["跟涨强"], "题材套利 contrib 注入 → %s" % src_map.get("题材套利"))
    fc = [c for c in contribs if c["source"] == "复盘重点"][0]
    chk("复盘重点·换手接力" in fc["thesis"], "复盘重点 thesis 带身份标签 → %s" % fc["thesis"])

    today = TODAY
    db = {"available_cash": 450000.0, "total_equity": 500000.0, "active_positions": []}
    with open(ms.BACKTEST_DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False)

    def rank_with(with_focus):
        cs = [c for c in contribs if with_focus or c["source"] != "复盘重点"]
        entries = ms.build_operation_pool(cs, {}, today)
        return ms.rank_operation_pool(entries, {}, db, "13:40", [], 50, None)

    with_f = rank_with(True)
    a = {x["name"]: x["score"] for x in with_f}
    chk("可参与A" in a, "复盘重点票进入 13:40 排序输出 → %s" % list(a))
    chk(a.get("可参与A", 0) > 78, "复盘重点票排序分 %.1f，压过典型竞争票(63)" % a.get("可参与A", 0))
    # rv_bonus 精确校验：两只属性完全相同、仅 source 不同的票，分差必须恰为 32
    pair = [{"symbol": "600004", "name": "复盘票", "source": "复盘重点", "signal": 75.0,
             "board_days": 1, "last_pct": None, "plates": [], "thesis": "x"},
            {"symbol": "600014", "name": "对照票", "source": "自选", "signal": 75.0,
             "board_days": 1, "last_pct": None, "plates": [], "thesis": "x"}]
    pr = {x["name"]: x["score"] for x in
          ms.rank_operation_pool(ms.build_operation_pool(pair, {}, today), {}, db, "13:40", [], 50, None)}
    diff = round(pr.get("复盘票", 0) - pr.get("对照票", 0), 2)
    chk(abs(diff - ms.REVIEW_FOCUS_RANK_BONUS) < 1e-6,
        "rv_bonus 精确 = %.1f（实得差 %.2f）" % (ms.REVIEW_FOCUS_RANK_BONUS, diff))
    # 三源都过白名单
    got_srcs = {x["name"]: x.get("sources") for x in with_f}
    chk("自选" in (got_srcs.get("均胜电子") or []), "自选源过 13:40 白名单 → %s" % got_srcs.get("均胜电子"))
    chk("题材套利" in (got_srcs.get("跟涨强") or []), "题材套利源过 13:40 白名单 → %s" % got_srcs.get("跟涨强"))
    # 陌生源仍被白名单拦
    entries_x = ms.build_operation_pool(
        [{"symbol": "600099", "name": "陌生源票", "source": "龙首阴", "signal": 90,
          "board_days": 1, "last_pct": 1.0, "plates": [], "thesis": "x"}], {}, today)
    out_x = ms.rank_operation_pool(entries_x, {}, db, "13:40", [], 50, None)
    chk(all(x["name"] != "陌生源票" for x in out_x), "白名单外的源（龙首阴@13:40）仍被拦")

    # ---------- 7) 推票解释性 ----------
    print("\n[7] 推票解释性（闪卡 / 订单计划 / 操作项 note）")
    cand = [x for x in with_f if x["name"] == "可参与A"]
    line = ms._source_flash_line(cand)
    chk("推票参会源" in line and "复盘重点" in line, "闪卡参会源行 → %s" % line)

    orig_sina = ms.get_stock_detailed_sina
    orig_lim = ms.get_stock_limits
    price = 10.0
    vol = 5.0e7
    ms.get_stock_detailed_sina = lambda *a, **k: {
        "current_price": price, "yesterday_close": 9.9, "open_price": 9.95,
        "high": 10.05, "low": 9.9, "volume": vol, "amount": price * vol * 0.995}
    ms.get_stock_limits = lambda *a, **k: (11.0, 9.9)
    try:
        plan = ms.build_order_plan("13:40", cand, 0.15, 50, None)
        buys = [i for i in plan["actions"] if i.get("type") == "BUY"]
        chk(len(buys) == 1, "订单计划产生 1 笔买入")
        chk("选股源：复盘重点" in (buys[0].get("note") or ""),
            "操作项 note 带「选股源」标签 → %s" % buys[0].get("note"))
        chk(buys[0].get("sources") == ["复盘重点"], "操作项带结构化 sources 字段")
        ps = plan.get("push_sources") or []
        chk(len(ps) == 1 and ps[0]["sources"] == ["复盘重点"],
            "order_plan.push_sources 落盘 → %s" % ps)
    finally:
        ms.get_stock_detailed_sina = orig_sina
        ms.get_stock_limits = orig_lim

    print("\n" + "=" * 68)
    print("结果：%d 通过 / %d 失败" % (_OK[0], _BAD[0]))
    print("=" * 68)
    cleanup()
    return 0 if _BAD[0] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""
持仓动态体检（独立小窗任务）
================================
读取用户真实持仓快照 reports/real_positions_*.json，
拉取每只标的近 60 日行情，给出：
  · 含金量评分（高/中/低）
  · 20 日趋势（上升/下降/震荡）+ 距高点回撤
  · 操作建议（卖出 / 持有 / 补仓）及理由

设计铁律：本脚本与五时段操作框架(market_scheduler.generate_report)完全解耦，
绝不调用、不修改、不触发任何五时段逻辑，确保「不扰动之前的时段操作建议」。
仅作为「盘后持仓体检」独立运行（默认 15:30，盘后行情落定后）。

用法：
  python holdings_review.py            # 生成当日报告并打印摘要
  python holdings_review.py --notify   # 额外弹 macOS 通知
"""
import os
import sys
import json
import glob
import time
import argparse
import subprocess
from datetime import datetime, timedelta

REPORTS_DIR = "/Users/williamli/Work/reports"


def load_real_holdings():
    cand = sorted(glob.glob(os.path.join(REPORTS_DIR, "real_positions_*.json")))
    if not cand:
        print("[holdings_review] 未找到 real_positions_*.json，退出")
        return []
    latest = cand[-1]
    with open(latest, "r", encoding="utf-8") as f:
        data = json.load(f)
    print("[holdings_review] 读取真实持仓快照: %s" % os.path.basename(latest))
    return data.get("positions", [])


def normalize_code(sym):
    """000725.SZ -> 000725（补齐6位，腾讯接口要求 sz000725 而非 725）"""
    s = str(sym).upper().replace(".SS", "").replace(".SZ", "").replace("SH", "").replace("SZ", "")
    return s.zfill(6)


def fetch_hist(code, days=60, retry=3):
    """腾讯前复权日K（轻量、稳，显式绕过可能损坏的代理）。
    code 为 6 位纯数字（如 600206 / 000725）。
    返回: 收盘价序列 list[float]（按日期升序），失败返回 None。"""
    import requests
    prefix = "sh" if code.startswith("6") else "sz"
    sec = "%s%s" % (prefix, code)
    # 注意：腾讯接口带明确起止日期时会返回空列表，故用空日期形式(day,,,N,qfq)取近 N 日
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,%d,qfq" % (sec, days)
    last_err = None
    for _ in range(retry):
        try:
            # 显式不走环境代理（沙箱代理可能损坏），直接连腾讯
            r = requests.get(url, timeout=10, proxies={"http": None, "https": None})
            if r.status_code == 200:
                d = r.json()
                data = d.get("data")
                if isinstance(data, dict):
                    node = data.get(sec, {})
                    key = "qfqday" if "qfqday" in node else ("day" if "day" in node else None)
                    rows = node.get(key) if key else None
                elif isinstance(data, list):
                    rows = data if data else None
                else:
                    rows = None
                if rows:
                    return [float(x[2]) for x in rows]  # [date,open,close,high,low,vol]
        except Exception as e:
            last_err = e
            time.sleep(0.5)
    if last_err:
        print("  [WARN] %s 行情获取失败: %s" % (code, last_err))
    return None


def analyze_one(pos):
    code = normalize_code(pos.get("symbol", ""))
    name = pos.get("name", "")
    cost = float(pos.get("cost_price", 0.0))
    snap_price = float(pos.get("current_price", 0.0))
    shares = int(float(pos.get("shares", 0)))

    rec = {
        "symbol": pos.get("symbol", ""),
        "name": name,
        "shares": shares,
        "cost_price": cost,
        "snap_price": snap_price,
    }

    closes = fetch_hist(code, 60)
    if not closes or len(closes) < 5:
        rec.update({
            "last": snap_price, "ma20": snap_price, "slope": 0.0,
            "high20": snap_price, "low20": snap_price, "drawdown": 0.0,
            "ret20": 0.0, "trend": "未知(无数据)", "trend_score": 0,
            "pnl_pct": ((snap_price / cost - 1) * 100) if cost else 0.0,
            "suggestion": "持有观望", "reason": "无法获取行情，暂按持有观望处理。",
            "gold": "中", "gold_score": 0,
        })
        return rec

    last = closes[-1]
    n = len(closes)
    ma5 = sum(closes[-5:]) / 5.0
    ma10 = sum(closes[-10:]) / 10.0
    ma20 = sum(closes[-20:]) / 20.0 if n >= 20 else (sum(closes[-10:]) / 10.0)
    ma20_prev = sum(closes[-25:-5]) / 20.0 if n >= 25 else ma20
    slope = ((ma20 - ma20_prev) / ma20_prev * 100.0) if ma20_prev else 0.0
    window = closes[-20:] if n >= 20 else closes
    high20 = max(window)
    low20 = min(window)
    drawdown = (last - high20) / high20 * 100.0
    ret20 = (last / closes[-20] - 1) * 100.0 if n >= 20 else 0.0

    # 趋势分类
    if slope > 1.5 and last >= ma20:
        trend, tscore = "上升", 2
    elif slope < -1.5 or last < ma20 * 0.97:
        trend, tscore = "下降", -2
    else:
        trend, tscore = "震荡", 0

    pnl_pct = (last / cost - 1) * 100.0 if cost else 0.0

    # 盈亏得分
    if pnl_pct > 15:
        pscore = 2
    elif pnl_pct >= 0:
        pscore = 1
    elif pnl_pct > -10:
        pscore = 0
    elif pnl_pct > -25:
        pscore = -1
    else:
        pscore = -2

    gold_score = tscore + pscore
    if gold_score >= 3:
        gold = "高"
    elif gold_score >= 0:
        gold = "中"
    else:
        gold = "低"

    # 操作建议（卖出 / 持有 / 补仓）
    near_support = (last <= low20 * 1.03)  # 接近 20 日低点
    if trend == "上升":
        if pnl_pct > 15:
            sugg, reason = "持有(可部分止盈)", "趋势向上且盈利丰厚，让利润奔跑；冲高分批落袋锁定。"
        elif pnl_pct >= 0:
            sugg, reason = "持有", "上升趋势中，持有待涨。"
        else:
            if drawdown < -8:
                sugg, reason = "补仓(低吸)", "上升趋势中的回踩，且已回落至阶段低位，可逢低补仓摊薄成本。"
            else:
                sugg, reason = "持有观望", "趋势向上但仍在成本下方，给时间修复；若放量破位再止损。"
    elif trend == "下降":
        if pnl_pct > 0:
            sugg, reason = "减仓/卖出", "趋势走弱但仍有盈利，趁反弹锁定利润，避免回吐。"
        elif pnl_pct > -15:
            sugg, reason = "卖出", "下行趋势且小幅亏损，避免深套，果断止损。"
        else:
            if near_support:
                sugg, reason = "减仓观望", "深套且趋势向下，但在阶段支撑位；先减仓控风险，反抽无力则清。"
            else:
                sugg, reason = "卖出(严控)", "深套且破位下行，控制亏损扩大为第一要务。"
    else:  # 震荡
        if pnl_pct > 10:
            sugg, reason = "持有/逢高减", "震荡中盈利，可高抛低吸降低持仓成本。"
        elif pnl_pct >= -10:
            sugg, reason = "持有观望", "震荡区间，观望等待方向选择，不追不杀。"
        else:
            if near_support:
                sugg, reason = "补仓(试探)", "震荡且小幅亏损，已跌至阶段支撑，可小仓试探补仓。"
            else:
                sugg, reason = "减仓", "震荡但已亏损且远离支撑，降低仓位等待企稳。"

    rec.update({
        "last": round(last, 2), "ma20": round(ma20, 2), "slope": round(slope, 2),
        "high20": round(high20, 2), "low20": round(low20, 2),
        "drawdown": round(drawdown, 2), "ret20": round(ret20, 2),
        "trend": trend, "trend_score": tscore,
        "pnl_pct": round(pnl_pct, 2),
        "suggestion": sugg, "reason": reason,
        "gold": gold, "gold_score": gold_score,
    })
    return rec


def build_report(records):
    total_cost = sum(r["cost_price"] * r["shares"] for r in records)
    total_mv = sum(r["last"] * r["shares"] for r in records)
    total_pnl = total_mv - total_cost
    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost else 0.0

    from collections import Counter

    def bucket(s):
        if s.startswith(("卖出", "减仓")):
            return "卖出/减仓"
        if s.startswith("补仓"):
            return "补仓"
        return "持有/观望"

    cnt = Counter(bucket(r["suggestion"]) for r in records)
    sell = [r for r in records if bucket(r["suggestion"]) == "卖出/减仓"]
    add = [r for r in records if bucket(r["suggestion"]) == "补仓"]
    # 排序：含金量低 + 亏损优先（最该处理）
    records_sorted = sorted(records, key=lambda r: (r["gold_score"], r["pnl_pct"]))

    lines = []
    lines.append("=" * 70)
    lines.append("  🩺 持仓动态体检报告（盘后独立小窗 · 不扰动五时段操作建议）")
    lines.append("  生成时间: %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    lines.append("=" * 70)
    lines.append("")
    lines.append("【一、 持仓总览】")
    lines.append("  • 持仓标的数: %d 只" % len(records))
    lines.append("  • 持仓总成本: ¥%s" % format(total_cost, ",.0f"))
    lines.append("  • 持仓总市值: ¥%s" % format(total_mv, ",.0f"))
    lines.append("  • 浮动盈亏:   %s¥%s (%.2f%%)" % (
        "+" if total_pnl >= 0 else "", format(total_pnl, ",.0f"), total_pnl_pct))
    lines.append("  • 建议分布: 卖出/减仓 %d · 持有/观望 %d · 补仓 %d" % (
        cnt.get("卖出/减仓", 0), cnt.get("持有/观望", 0), cnt.get("补仓", 0)))
    lines.append("")
    lines.append("【二、 逐标的分析（按 含金量↓ + 盈亏↓ 排序，最该处理的在前）】")
    for i, r in enumerate(records_sorted, 1):
        lines.append("")
        lines.append("  %d. %s (%s)  [%s含金量]" % (i, r["name"], r["symbol"], r["gold"]))
        lines.append("     持仓: %d 股 | 真实成本 ¥%.3f | 最新 ¥%.2f | 盈亏 %+.2f%%"
                     % (r["shares"], r["cost_price"], r["last"], r["pnl_pct"]))
        lines.append("     20日趋势: %s | MA20斜率 %+.2f%% | 距20日高点 %+.2f%% | 20日收益 %+.2f%%"
                     % (r["trend"], r["slope"], r["drawdown"], r["ret20"]))
        lines.append("     ➡️ 操作建议: 【%s】" % r["suggestion"])
        lines.append("     依据: %s" % r["reason"])
    lines.append("")
    lines.append("【三、 重点处理清单】")
    if sell:
        lines.append("  🔴 建议卖出/减仓（优先处理，释放资金）：")
        for r in sorted(sell, key=lambda x: x["pnl_pct"]):
            lines.append("     - %s(%s): %s | 盈亏 %+.2f%%" % (r["name"], r["symbol"], r["suggestion"], r["pnl_pct"]))
    else:
        lines.append("  🔴 建议卖出/减仓：无")
    if add:
        lines.append("  🟢 建议补仓（仅限回踩强支撑/上升回档）：")
        for r in add:
            lines.append("     - %s(%s): %s" % (r["name"], r["symbol"], r["suggestion"]))
    else:
        lines.append("  🟢 建议补仓：当前无明确低吸机会")
    lines.append("")
    lines.append("【四、 整体建议】")
    lines.append("  • 当前 %d 只持仓偏多偏杂，建议聚焦：先处理「低含金量+亏损」标的（见上方卖出清单），" % len(records))
    lines.append("    把资金向「高含金量+上升趋势」的核心标的集中，避免撒胡椒面。")
    lines.append("  • 本体检仅在盘后独立运行，与 09:25/10:40/11:40/13:40/14:40 五时段框架互不干扰。")
    lines.append("=" * 70)

    return "\n".join(lines), {
        "total_cost": total_cost, "total_mv": total_mv,
        "total_pnl": total_pnl, "total_pnl_pct": total_pnl_pct,
        "cnt": dict(cnt), "sell": [r["name"] for r in sell],
        "add": [r["name"] for r in add],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--notify", action="store_true", help="额外弹 macOS 通知")
    args = ap.parse_args()

    positions = load_real_holdings()
    if not positions:
        return

    print("[holdings_review] 开始逐标的拉取行情并分析（%d 只）..." % len(positions))
    records = []
    for idx, p in enumerate(positions, 1):
        sys.stdout.write("  [%d/%d] %s ... " % (idx, len(positions), p.get("name", "?")))
        sys.stdout.flush()
        r = analyze_one(p)
        records.append(r)
        print("趋势=%s 盈亏=%+.2f%% 建议=%s" % (r["trend"], r["pnl_pct"], r["suggestion"]))
        time.sleep(0.25)  # 轻量限速，避免接口限流

    report, summary = build_report(records)

    date_str = datetime.now().strftime("%Y%m%d")
    fname = os.path.join(REPORTS_DIR, "holdings_review_%s.txt" % date_str)
    with open(fname, "w", encoding="utf-8") as f:
        f.write(report)
    print("\n[holdings_review] 报告已写出: %s" % fname)
    print("\n" + report)

    if args.notify:
        try:
            msg = "体检完成：%d只 | 总盈亏 %+.2f%% | 卖出%d/持有%d/补仓%d" % (
                len(records), summary["total_pnl_pct"],
                summary["cnt"].get("卖出/减仓", 0),
                summary["cnt"].get("持有/观望", 0),
                summary["cnt"].get("补仓", 0))
            subprocess.run(["osascript", "-e",
                            'display notification "%s" with title "持仓动态体检"' % msg])
        except Exception as e:
            print("[WARN] 通知失败: %s" % e)

    json_path = os.path.join(REPORTS_DIR, "holdings_review_%s.json" % date_str)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "records": records}, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()

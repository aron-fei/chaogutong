# -*- coding: utf-8 -*-
"""时段派单诊断（2026-11-05 体检剩余项）

回答一个长期黑盒问题：「报告 ready，为什么工作台没有派单弹窗？」
已知口径（务必记住，见项目档案）：
  * 报告【九、统一操作池总览】的 Top3 只是**操作池排序候选**，不是派单；
  * 真正的派单看 order_plan_<时段>.json 的 buy_candidates，
    落地确认单是 pending_buy_confirm_<时段>_<日期>.json。

本模块只读 reports 目录，分类每个时段零派单的根因，绝不触碰任何下单通道，
也不擅自放宽策略闸门（用户策略观：宁可错过，不可乱做）。

自检：/opt/homebrew/anaconda3/bin/python3 dispatch_diag.py --selftest
"""
import json
from datetime import datetime
from pathlib import Path

import config

STAGES = ["09:25", "10:40", "11:40", "13:40", "14:40"]
STATUS_LABEL = {
    "dispatched": "已派单",
    "gate_temp": "情绪闸门拦截",
    "empty_pool": "候选池为空",
    "gate_quota": "闸门额度为 0",
    "no_plan": "未生成派单文件",
    "other": "无买入候选",
}


def _load(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"}


def _classify(d, note, buys, temp, quota):
    """返回 (status, reason)。顺序即优先级，避免多重原因时误报。"""
    if buys:
        return "dispatched", f"已派单 {len(buys)} 只，工作台应生成待确认买单"
    if temp is not None and temp < 40:
        return ("gate_temp",
                f"大盘温度 {temp:.0f} 属冰点(<40)，情绪闸门主动空仓 —— 符合「宁可错过不可乱做」，不建议放宽")
    if "无明确" in note or "不操作" in note or "空手" in note:
        return "empty_pool", "该时段过滤后无合格标的（候选池为空）"
    if (quota or {}).get("gate_max_picks") == 0:
        return "gate_quota", "闸门 max_picks=0，本时段不允许开新仓"
    return "other", "无买入候选（详见时段说明与报告 ⑨ 节）"


def diagnose(reports_dir=None):
    rd = Path(reports_dir or config.REPORTS_DIR)
    today = datetime.now().strftime("%Y%m%d")
    slot = _load(rd / "slot_status.json")
    stages = []
    for st in STAGES:
        key = st.replace(":", "")
        f = rd / f"order_plan_{key}.json"
        info = slot.get(st) or {} if isinstance(slot, dict) else {}
        row = {
            "stage": st,
            "slot_exit": info.get("exit_code"),
            "slot_run": info.get("last_run"),
            "slot_note": info.get("note"),
            "report": info.get("report"),
        }
        if not f.exists():
            row.update({"status": "no_plan", "label": STATUS_LABEL["no_plan"],
                        "reason": "该时段未生成 order_plan 文件（非派单时段或调度未执行）",
                        "temp": None, "buys": 0, "no_gate": False, "note": ""})
            stages.append(row)
            continue
        d = _load(f)
        if "_error" in d:
            row.update({"status": "other", "label": "文件异常",
                        "reason": d["_error"], "temp": None, "buys": 0,
                        "no_gate": False, "note": ""})
            stages.append(row)
            continue
        buys = d.get("buy_candidates") or []
        acts = d.get("actions") or []
        note = " / ".join(str(a.get("note") or "")
                          for a in acts if isinstance(a, dict))
        quota = d.get("quota_diag") or {}
        temp = d.get("market_temp")
        status, reason = _classify(d, note, buys, temp, quota)
        row.update({
            "status": status, "label": STATUS_LABEL.get(status, status),
            "reason": reason, "temp": round(temp, 1) if isinstance(temp, (int, float)) else None,
            "buys": len(buys), "note": note[:300],
            "recommended_position": d.get("recommended_position"),
            "no_gate": (quota.get("gate_position_cap_pct") is None
                        and quota.get("gate_max_picks") is not None),
            "generated_at": d.get("generated_at"),
        })
        stages.append(row)

    pending = sorted(p.name for p in rd.glob(f"pending_buy_confirm_*_{today}.json"))
    gate_cur = rd / "trade_gate_current.json"
    gate_file = rd / "trade_gate.json"
    date_iso = datetime.now().strftime("%Y-%m-%d")

    def _gate_info(p):
        if not p.exists():
            return {"file": p.name, "exists": False}
        d = _load(p)
        asof = str((d or {}).get("asof") or "")[:10]
        return {"file": p.name, "exists": True, "asof": asof,
                "generated_at": (d or {}).get("generated_at"),
                "covers_today": asof == date_iso,
                "max_picks": (d or {}).get("max_picks"),
                "position_cap_pct": (d or {}).get("position_cap_pct")}

    gate = {"today": date_iso, "current": _gate_info(gate_cur),
            "main": _gate_info(gate_file),
            "usable_today": any(_gate_info(p).get("covers_today")
                                for p in (gate_cur, gate_file))}
    if not gate["usable_today"]:
        hints.append(
            f"闸门 asof 口径检查：current={gate['current'].get('asof') or '缺失'} / "
            f"main={gate['main'].get('asof') or '缺失'}，均不等于今日 {date_iso} → "
            "调度器 load_review_gate 会作废并 fail-open（不干预）。"
            "常见原因是盘后生成的「明日闸门」覆盖了今日副本；"
            "这是额度诊断显示「无闸门」的原因，但不是零派单的主因。")
    dispatched = sum(1 for s in stages if s["status"] == "dispatched")
    hints = []
    if dispatched == 0:
        blocked = sum(1 for s in stages if s["status"] == "gate_temp")
        empty = sum(1 for s in stages if s["status"] == "empty_pool")
        hints.append(f"今日全时段零派单：{blocked} 个时段被情绪闸门拦截、"
                     f"{empty} 个时段候选池为空。报告「已就绪」只代表生成成功，不代表有派单。")
    if any(s.get("no_gate") for s in stages):
        hints.append("order_plan 的 quota_diag 显示「无闸门约束」"
                     "(gate_position_cap_pct=null)，建议检查 review_gate --auto 与 "
                     "trade_gate_current.json 是否覆盖到今日。")
    if not gate_cur.exists() and not gate_file.exists():
        hints.append("reports 目录下未找到 trade_gate_current.json / trade_gate.json："
                     "当日闸门可能缺失，盘中会 fail-open（放宽），需跑复盘闸门自动生成。")
    hints.append("「报告 Top3 候选」≠「派单」：工作台弹窗只认 pending_buy_confirm_*.json，"
                 "本面板与 /api/reports/today_status 已把两者分开。")

    return {
        "ok": True,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "generated_at": datetime.now().strftime("%H:%M:%S"),
        "reports_dir": str(rd),
        "summary": {
            "stages": len(stages),
            "dispatched": dispatched,
            "zero_dispatch": dispatched == 0,
            "pending_files": len(pending),
            "gate_current_exists": gate_cur.exists(),
        },
        "stages": stages,
        "gate": gate,
        "pending_files": pending[:10],
        "hints": hints,
        "note": "只读诊断，不修改任何派单/闸门/持仓文件；也不擅自放宽策略。",
    }


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        d = diagnose()
        print("[1] stages:", [(x["stage"], x["label"], x["buys"]) for x in d["stages"]])
        assert len(d["stages"]) == len(STAGES)
        assert isinstance(d["summary"]["zero_dispatch"], bool)
        assert d["hints"]
        print("[2] summary:", d["summary"])
        print("[3] hints:")
        for h in d["hints"]:
            print("   -", h)
        print("[4] classify 单测")
        assert _classify({}, "", [], 31, {})[0] == "gate_temp"
        assert _classify({}, "", [{"code": "300001"}], 31, {})[0] == "dispatched"
        assert _classify({}, "午后无明确未涨停强势标的，不操作", [], 71, {})[0] == "empty_pool"
        assert _classify({}, "", [], 71, {"gate_max_picks": 0})[0] == "gate_quota"
        print("[selftest] PASS")
    else:
        print(json.dumps(diagnose(), ensure_ascii=False, indent=2))

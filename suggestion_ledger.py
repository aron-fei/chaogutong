#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
suggestion_ledger.py · 复盘建议台账（2026-10-19 新增）

【为什么建这个】
复盘任务每天产出 P0/P1/P2 分级建议（`recap_YYYY-MM-DD.md` 第 ⑤ 节），但此前：
  · 建议只进报告，不进任何系统 → 无法查询「哪条还没处理」
  · automation prompt 写死「只输出建议，绝不自动修改策略」→ 这是刻意的安全设计，
    但配套没建：没有台账，于是 16 份报告的建议全部沉积，用户找不到入口。
本模块补上「落地入口」：**解析 → 去重合并 → 状态机 → 工作台可查可勾选**。

【核心设计】
1. **解析**：扫 `recap_*.md`，抓 `| **P0-1** | **标题** | 依据… |` 三列表格行。
2. **去重合并**：同一条建议连续多日复现（如「闸门链断裂」横跨 10-08~10-19），
   合并为一条，累加 `occurrences`（复现次数 + 首次/最近出现日）。
   合并键 = 标题归一化后的关键词指纹（去数字/去标点/取前若干字）。
3. **状态机**：pending(待评估) → accepted(已采纳) → implemented(已实施) / dismissed(已否决)。
   状态存在本目录 `data/suggestion_state.json`，与解析结果分离（重解析不丢状态）。
4. **绝不自动改代码**：本模块只做「读报告 + 记状态」，不碰任何策略/回测文件。

用法：
    python3 suggestion_ledger.py --scan           # 解析入库 + 打印概览
    python3 suggestion_ledger.py --list [--level P0] [--status pending]
    python3 suggestion_ledger.py --set <id> <status>   # 更新状态
    python3 suggestion_ledger.py --selftest
"""
import config

import argparse
import json
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
REPORTS = config.REPORTS_DIR
STATE_FILE = BASE / "data" / "suggestion_state.json"

# 状态机定义（唯一合法取值）
STATUSES = ("pending", "accepted", "implemented", "dismissed")
STATUS_LABEL = {
    "pending": "待评估",
    "accepted": "已采纳",
    "implemented": "已实施",
    "dismissed": "已否决",
}
# 级别权重：数字越小越紧急
LEVEL_ORDER = {"P0": 0, "P1": 1, "P2": 2}

# 匹配 recap 第 ⑤ 节建议表格行：
#   | **P0-1** | **标题** | 依据与具体建议 |
# 容忍：无外层粗体、编号缺失、列数变化（取前3列）。
_ROW_RE = re.compile(
    r"^\s*\|\s*\*{0,2}(P[012])[-\s]*(\d{1,2})\*{0,2}\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*$",
    re.M,
)
# 只在「策略调整建议」小节内解析，避免误抓其它表格
_SECTION_RE = re.compile(
    r"#{2,3}\s*[^\n]*策略调整建议(?P<body>.*?)(?=\n#{2,3}\s|\n---\s*\n\s*\n##|\Z)",
    re.S,
)
_RECAP_RE = re.compile(r"recap_(\d{4})-(\d{2})-(\d{2})\.md$")


# ---------------------------------------------------------------- 工具函数

def _norm_text(s):
    """标题归一化：去 markdown 强调、去标点空白、全角转半角、转小写。"""
    s = unicodedata.normalize("NFKC", str(s or ""))
    s = re.sub(r"[*_`]", "", s)
    s = re.sub(r"[\s　]+", "", s)
    s = re.sub(r"[，。、；：,.;:!?！？「」『』（）()【】\[\]…—\-]+", "", s)
    return s.strip().lower()


def _fingerprint(title):
    """建议指纹：识别「同一问题被反复提出」。

    难点：同一条建议在不同日期的措辞会变（"断裂第 5 个交易日" vs "断裂第 7 个交易日"），
    仅靠去数字不足以合并。策略：
      1) 归一化 + 去除序号类 token（第N个/连续/N日等）
      2) 抽取核心关键词（按分隔符切分，取有信息量的片段）
      3) 指纹 = 头部主题词 + 关键实体词，确保同一议题稳定收敛

    例：「复盘闸门链断裂第 5 个交易日」与「复盘闸门链断裂第 7 个交易日 —— 需自愈机制」
      → 都收敛到 '复盘闸门链断裂' 主题
    """
    t = _norm_text(title)
    # 去掉常见的动态修饰：第N个/连续/N日/N次、括号补充说明
    t = re.sub(r"第[一二三四五六七八九十\d]+[个次天日位笔条]", "", t)
    t = re.sub(r"连续", "", t)
    t = re.sub(r"[\d]+", "", t)
    # 去掉纯时间/程度修饰词（对议题同一性无贡献，却会因表述差异拆散同一建议）
    for noise in ("交易日", "新发现", "本身", "生成机制", "的生成机制"):
        t = t.replace(noise, "")

    # ===== 【2026-10-21 新增】关键实体词提取 =====
    # 动机：「11:40 调度耗时恶化（新纪录）」/「11:40 调度耗时 1,146s（+20m11s），连续第 3 日最差」
    #      /「11:40 调度耗时连续第 3 日 >30 分钟」—— 同一议题三种措辞，
    #      按"最长片段"会拆成三条，破坏去重。
    # 注意：_norm_text 的全角转换会把 "11:40" 变成 "1140"（冒号被当标点去掉），
    #      所以时段必须**在归一化之前**从原始标题提取。
    _raw = unicodedata.normalize("NFKC", str(title or ""))
    _tm = re.search(r"(09:25|10:40|11:40|13:40|14:40)", _raw)
    _slot = _tm.group(1) if _tm else None
    if _slot:
        if re.search(r"耗时|时长|慢|恶化|超时", _raw):
            return "time_cost_%s" % _slot
        if re.search(r"报告|标题|标签|版式", _raw):
            return "time_report_%s" % _slot
        if re.search(r"通道", _raw):
            return "time_chan_%s" % _slot
        if re.search(r"炸板率|炸板", _raw):
            return "time_zbrate_%s" % _slot
        if re.search(r"派单|距高|距当日最高", _raw):
            return "time_dispatch_%s" % _slot
        return "time_%s" % _slot

    # 以主题关键词切分：取最长的中文/英文片段作为主题
    parts = [p for p in re.split(r"[与和及的了]", t) if len(p) >= 3]
    if parts:
        # 取包含核心动词/名词的较长片段
        parts.sort(key=len, reverse=True)
        topic = parts[0]
    else:
        topic = t
    # 【二次收敛】若主题以「连续第/第N个」等动态词结尾，截到该词之前。
    # 这样「闸门链断裂第7个交易日——需自愈」与「闸门链断裂连续第5个交易日」都归到「闸门链断裂」。
    topic = re.split(r"(第|连续|新发现|需|应|建议|导致|已|未)", topic)[0]
    topic = topic[:14]
    return topic or _norm_text(title)[:14]


def _strip_md(s):
    s = str(s or "")
    s = s.replace("**", "").replace("`", "")
    return re.sub(r"\s+", " ", s).strip()


def _read_state():
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception as e:
            print("[ledger] WARN 状态文件损坏，按空处理: %s" % e, file=sys.stderr)
    return {}


def _write_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _stable_id(fp):
    """由指纹生成稳定短 id：同名建议跨日保持同一 id，状态才不会丢。"""
    import hashlib
    return hashlib.sha1(fp.encode("utf-8")).hexdigest()[:10]


# ---------------------------------------------------------------- 解析

def parse_recap(path):
    """解析一份 recap markdown → [建议 dict]"""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return []
    m = _RECAP_RE.search(path.name)
    if not m:
        return []
    date = "%s-%s-%s" % (m.group(1), m.group(2), m.group(3))

    rows = []
    sec = _SECTION_RE.search(text)
    body = sec.group("body") if sec else ""
    if not body:
        # 退化：全文兜底（少数报告小节标题措辞不同）
        body = text

    for mm in _ROW_RE.finditer(body):
        level, _num, title, detail = mm.groups()
        title = _strip_md(title)
        detail = _strip_md(detail)
        if not title:
            continue
        fp = _fingerprint(title)
        rows.append({
            "id": _stable_id(fp),
            "fingerprint": fp,
            "level": level,
            "title": title,
            "detail": detail,
            "first_date": date,
            "last_date": date,
            "source": path.name,
            "occurrences": 1,
            "dates": [date],
        })
    return rows


def scan(verbose=False):
    """扫全部 recap → 合并同指纹建议 → 返回列表（按级别/最近日期排序）。

    合并规则：同一 fingerprint 视为同一条建议，累加 occurrences 并记录全部日期；
    level 取历次最高（P0 最紧急），title/detail 取**最近一次**的表述。
    """
    if not REPORTS.exists():
        print("[ledger] 报告目录不存在: %s" % REPORTS, file=sys.stderr)
        return []
    files = sorted(REPORTS.glob("recap_*.md"))
    merged = {}
    for f in files:
        for row in parse_recap(f):
            fp = row["fingerprint"]
            cur = merged.get(fp)
            if not cur:
                merged[fp] = dict(row)
                continue
            cur["occurrences"] = cur.get("occurrences", 1) + 1
            d = row["first_date"]
            if d not in cur["dates"]:
                cur["dates"].append(d)
            cur["dates"] = sorted(cur["dates"])
            if d < cur["first_date"]:
                cur["first_date"] = d
            # 级别取最高优先级
            if LEVEL_ORDER.get(row["level"], 9) < LEVEL_ORDER.get(cur["level"], 9):
                cur["level"] = row["level"]
            if d >= cur["last_date"]:
                cur["last_date"] = d
                cur["title"] = row["title"]
                cur["detail"] = row["detail"]
                cur["source"] = row["source"]

    rows = list(merged.values())
    # 排序：先级别，同级按复现次数倒序（越顽固越前），再按最近日期倒序
    rows.sort(key=lambda r: (LEVEL_ORDER.get(r["level"], 9),
                             -r.get("occurrences", 1),
                             _neg_date(r.get("last_date", ""))))
    state = _read_state()
    for r in rows:
        st = state.get(r["id"]) or {}
        r["status"] = st.get("status", "pending")
        r["note"] = st.get("note", "")
        r["updated_at"] = st.get("updated_at", "")
    if verbose:
        print("[ledger] 扫描 %d 份报告 → %d 条去重建议（原始 %d 行）"
              % (len(files), len(rows),
                 sum(len(parse_recap(f)) for f in files)))
    return rows


class _neg_date:
    """排序辅助：把日期字符串反向（新的在前）。"""
    def __init__(self, d):
        self.d = d or ""

    def __lt__(self, other):
        return self.d > other.d

    def __gt__(self, other):
        return self.d < other.d

    def __eq__(self, other):
        return self.d == other.d


# ---------------------------------------------------------------- 状态操作

def set_status(sid, status, note=""):
    if status not in STATUSES:
        print("[ledger] 非法状态: %s（可选 %s）" % (status, "/".join(STATUSES)),
              file=sys.stderr)
        return False
    state = _read_state()
    state[sid] = {
        "status": status,
        "note": note or (state.get(sid, {}) or {}).get("note", ""),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _write_state(state)
    return True


def purge_status(keep_ids):
    """清理已不存在的建议的状态记录（解析结果变化时调用，避免幽灵条目）。

    【2026-10-21 修·重要】必须传入**完整**（未过滤）的建议 id 集合。
    旧实现在 API 里被这样调用：
        dropped = sl.purge_status({r["id"] for r in sl.scan()})
    而 scan() 的结果**已被 level/status 过滤** → 传进来的是过滤后的子集
    → 其余建议的状态被当成"幽灵"删除 → 每次刷新 API 都会清掉一批状态。
    实测症状：27 条建议的状态被删到只剩 18 条，待评估从 0 反弹到 9。
    现在加保护：keep_ids 若明显小于已知状态数，判定为误用并跳过清理。
    """
    state = _read_state()
    if not state:
        return 0
    keep = set(keep_ids or ())
    if len(keep) < len(state):
        missing = len(state) - len(keep)
        if missing > 2:
            print("[ledger] WARN purge_status 收到过滤后的 id 集合"
                  "（keep=%d < 已知状态=%d），跳过清理以防误删"
                  % (len(keep), len(state)), file=sys.stderr)
            return 0
    pruned = {k: v for k, v in state.items() if k in keep}
    if len(pruned) != len(state):
        _write_state(pruned)
        return len(state) - len(pruned)
    return 0


# ---------------------------------------------------------------- 自检

def _selftest():
    ok = True

    def chk(cond, desc):
        nonlocal ok
        print(("  ✅ " if cond else "  ❌ ") + desc)
        if not cond:
            ok = False

    print("[selftest] 标题归一与指纹")
    chk(_fingerprint("复盘闸门链断裂第 **5** 个交易日")
        == _fingerprint("复盘闸门链断裂第 7 个交易日"),
        "同义建议（数字不同）指纹一致 → 会合并为一条")
    chk(_fingerprint("修 09:25 炸板率口径 bug")
        != _fingerprint("14:40 接入距最高 %判据"),
        "不同建议指纹不同")

    print("\n[selftest] 行解析")
    sample = ("## ⑤ 明日策略调整建议（仅建议，未实施）\n\n"
              "| 级别 | 方向 | 依据与具体建议 |\n"
              "|---|---|---|\n"
              "| **P0-1** | **修一个 bug** | 依据甲 |\n"
              "| **P1-2** | 另一件事 | 依据乙 |\n")
    import tempfile
    tmpdir = Path(tempfile.mkdtemp())
    tmp = tmpdir / "recap_2026-10-19.md"
    tmp.write_text(sample, encoding="utf-8")
    rows = parse_recap(tmp)
    chk(len(rows) == 2, "解析出 2 条建议（实际 %d）" % len(rows))
    if len(rows) == 2:
        chk(rows[0]["level"] == "P0" and rows[0]["title"] == "修一个 bug",
            "级别与标题正确：%s / %s" % (rows[0]["level"], rows[0]["title"]))
        chk(rows[0]["first_date"] == "2026-10-19",
            "日期取自文件名：%s" % rows[0]["first_date"])
        chk(len(rows[0]["id"]) == 10, "生成稳定 id")
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)

    print("\n[selftest] 状态机")
    st = _read_state()
    chk(set_status("__selftest__", "bogus") is False, "非法状态被拒绝")
    chk(set_status("__selftest__", "accepted", "自检") is True, "合法状态可写入")
    chk(_read_state().get("__selftest__", {}).get("status") == "accepted", "状态已持久化")
    st = _read_state()
    st.pop("__selftest__", None)
    _write_state(st)
    chk("__selftest__" not in _read_state(), "自检残留已清理")

    print("\n结果: " + ("全部通过 ✅" if ok else "存在失败 ❌"))
    return 0 if ok else 1


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description="复盘建议台账")
    ap.add_argument("--scan", action="store_true", help="解析全部报告并打印概览")
    ap.add_argument("--list", action="store_true", help="列出建议")
    ap.add_argument("--level", help="过滤级别 P0/P1/P2")
    ap.add_argument("--status", help="过滤状态 %s" % "/".join(STATUSES))
    ap.add_argument("--set", nargs=2, metavar=("ID", "STATUS"), help="更新状态")
    ap.add_argument("--note", default="", help="配合 --set 写备注")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)

    if a.selftest:
        return _selftest()

    if a.set:
        sid, status = a.set
        if set_status(sid, status, a.note):
            print("✅ %s → %s" % (sid, STATUS_LABEL.get(status, status)))
            return 0
        return 1

    rows = scan(verbose=True)
    if not rows:
        print("暂无建议（先跑每日复盘任务产出 recap_*.md）")
        return 0

    if a.level:
        rows = [r for r in rows if r["level"] == a.level]
    if a.status:
        rows = [r for r in rows if r["status"] == a.status]

    if a.list or a.level or a.status or a.scan:
        # 概览统计
        by_lvl = {}
        by_st = {}
        for r in rows:
            by_lvl[r["level"]] = by_lvl.get(r["level"], 0) + 1
            by_st[r["status"]] = by_st.get(r["status"], 0) + 1
        print("\n级别分布: " + "  ".join(
            "%s=%d" % (k, by_lvl.get(k, 0)) for k in ("P0", "P1", "P2")))
        print("状态分布: " + "  ".join(
            "%s=%d" % (STATUS_LABEL[k], v) for k, v in sorted(by_st.items())))
        print("-" * 78)
        for r in rows:
            occ = " ×%d" % r["occurrences"] if r.get("occurrences", 1) > 1 else ""
            print("[%s]%s %s  (%s ~ %s, %d次)"
                  % (r["level"], occ, r["title"],
                     r["first_date"], r["last_date"], r["occurrences"]))
            print("      id=%s  状态=%s" % (r["id"], STATUS_LABEL.get(r["status"])))
            print("      %s" % (r["detail"][:110] + ("…" if len(r["detail"]) > 110 else "")))
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
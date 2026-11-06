#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性工具：指纹算法升级后，把旧状态按标题相似度迁移到新 id。
【2026-10-21】用完即可删（保留在仓库里作为指纹变更时的迁移参考）。
"""
import hashlib
import json
import os
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
import suggestion_ledger as sl  # noqa: E402

STATE = BASE_DIR / "data" / "suggestion_state.json"
BACKUP = BASE_DIR / "data" / "suggestion_state.json.bak_20261021_fingerprint"


def old_fingerprint(title):
    """指纹算法升级**之前**的版本（用于反查旧 id）。"""
    t = sl._norm_text(title)
    t = re.sub(r"第[一二三四五六七八九十\d]+[个次天日位笔条]", "", t)
    t = re.sub(r"连续", "", t)
    t = re.sub(r"[\d]+", "", t)
    for n in ("交易日", "新发现", "本身", "生成机制", "的生成机制"):
        t = t.replace(n, "")
    parts = [p for p in re.split(r"[与和及的了]", t) if len(p) >= 3]
    topic = sorted(parts, key=len, reverse=True)[0] if parts else t
    topic = re.split(r"(第|连续|新发现|需|应|建议|导致|已|未)", topic)[0][:14]
    return topic or sl._norm_text(title)[:14]


def build_old_id_titles():
    m = {}
    for f in sorted(sl.REPORTS.glob("recap_*.md")):
        for row in sl.parse_recap(f):
            oid = hashlib.sha1(old_fingerprint(row["title"]).encode("utf-8")).hexdigest()[:10]
            m.setdefault(oid, []).append(row["title"])
    return m


def best_old_id(new_title, old_map):
    nt = sl._norm_text(new_title)
    best, best_score = None, 0
    for oid, titles in old_map.items():
        for t in titles:
            ot = sl._norm_text(t)
            if nt in ot or ot in nt:
                sc = min(len(nt), len(ot))
            else:
                sc = len(os.path.commonprefix([nt, ot]))
            if sc > best_score:
                best, best_score = oid, sc
    return best


def main():
    cur = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    bak = json.loads(BACKUP.read_text(encoding="utf-8")) if BACKUP.exists() else {}
    old_map = build_old_id_titles()
    rows = sl.scan()
    added = 0
    for r in rows:
        if r["id"] in cur:
            continue
        oid = best_old_id(r["title"], old_map)
        if oid and oid in bak:
            cur[r["id"]] = dict(bak[oid])
            added += 1
    STATE.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    print("迁移补全 %d 条 → 状态文件共 %d 条" % (added, len(cur)))


if __name__ == "__main__":
    main()
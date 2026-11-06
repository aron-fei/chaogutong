# -*- coding: utf-8 -*-
"""数据 cutover：以 2026-11-06 为界，把老数据 / 老候选池 / 全部缓存归档封存。

用户指令（2026-11-06）：「以前的老数据包括候选池包括一切缓存都做切割，我们按照新的建议来做」。

与旧脚本 prune_caches.py 的区别：
  * prune_caches.py 只清 7 天前 K 线缓存，且是 **真删除 unlink**，不可回滚。
  * 本脚本是**归档式切割**（移动 + MANIFEST），可一键 --rollback，绝不 unlink。

三条硬约束：
  1. 默认 DRY_RUN，只有 --apply 才动磁盘；--rollback 从 MANIFEST 原样还原。
  2. 红线文件永不切割：密钥 / 真实持仓 / 自选股 / 建议台账 / 复盘状态 / 闸门历史 /
     模拟盘账本 / 派单与调度状态 / git 与 .workbuddy。
  3. 盘中热数据（mtime >= cutover 当日 0 点）默认保留，避免打断正在跑的策略；
     确需全切用 --include-today（建议收盘后执行）。

用法：
    /opt/homebrew/anaconda3/bin/python3 scripts/cutover_data.py                # DRY_RUN 预览
    /opt/homebrew/anaconda3/bin/python3 scripts/cutover_data.py --apply        # 执行归档切割
    /opt/homebrew/anaconda3/bin/python3 scripts/cutover_data.py --rollback     # 一键还原
    /opt/homebrew/anaconda3/bin/python3 scripts/cutover_data.py --json         # 机器可读清单

可选范围（默认全部关闭，需要才开）：
    --include-today    连今日盘中生成的缓存一起切
    --include-bak      连 .bak 备份一起切（用户铁律：默认不动备份）
    --include-shots    连验证截图一起切
    --include-reports  连 /Users/williamli/Work/reports 的老文件一起切（跨项目，需谨慎）
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CUTOVER_DATE = "2026-11-06"
ARCHIVE = ROOT / ".archive" / f"cutover_{CUTOVER_DATE.replace('-', '')}"
MANIFEST = ARCHIVE / "MANIFEST.json"
REPORTS_DIR = Path(os.environ.get("REPORTS_DIR", str(ROOT.parent / "reports")))
MAX_BATCH = 200

PRUNE_DIRS = {".git", ".workbuddy", "__pycache__", "node_modules", ".archive", ".venv", "venv"}

# ---- 红线：任何参数组合下都不得切割 ----
RED_EXACT = {
    "data/access_key.json", "data/suggestion_state.json", "data/watchlist.json",
    "data/theme_library.json",  # 题材库含人工整理条目，非纯缓存
    "portfolio.json", "requirements.lock.txt", "requirements.txt",
}
RED_PREFIX = (
    "data/reviews/", "data/suggestions/", "data/review_modes/", "data/gate_history/",
    "data/suggestion_state", "data/watchlist.json", ".workbuddy/", ".git/",
    "tests/", "scripts/", "docs/", "static/", "templates/",
)
RED_NAME_CONTAINS = ("access_key", "portfolio", "backtest_database", "slot_status",
                     "trade_gate", "gate_effectiveness", "pending_buy_confirm",
                     "pending_sell_confirm", "operation_pool", "order_plan")

# reports 目录（cross-project）里同样要保留的当日/状态类文件
REPORTS_KEEP_NAME = ("slot_status.json", "trade_gate.json", "trade_gate_current.json",
                     "backtest_database.json", "gate_effectiveness.json",
                     "order_plan_", "pending_buy_confirm_", "pending_sell_confirm_",
                     "operation_pool.json")


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def is_red_line(rel):
    if rel in RED_EXACT:
        return True
    if any(rel.startswith(p) for p in RED_PREFIX):
        return True
    name = rel.rsplit("/", 1)[-1]
    return any(k in name for k in RED_NAME_CONTAINS)


def scope_of(rel, args):
    """返回 (分类名, 是否纳入)。返回 None 表示不在任何切割范围。"""
    if is_red_line(rel):
        return None
    if rel.startswith(("data/bottom_kline_cache/", "data/watchlist_cache/")):
        return ("K线缓存", True)
    if rel == "data/theme_cons_cache.json.tmp":
        return ("题材索引临时文件", True)
    if rel.startswith("data/") and rel.endswith(".json"):
        return ("运行时数据/候选池", True)
    if "/" not in rel and rel.endswith(".json"):
        return ("根目录数据文件", True)
    if ".bak" in rel:
        return ("备份", args.include_bak)
    if rel.startswith((".verify_shots/", ".playwright-cli/", "reports_shots/")):
        return ("验证截图", args.include_shots)
    return None


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def collect(args):
    cutoff = dt.datetime.strptime(CUTOVER_DATE, "%Y-%m-%d").timestamp()
    items, skipped_hot, red_cnt = [], 0, 0
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in PRUNE_DIRS]
        for fn in filenames:
            p = Path(dirpath) / fn
            try:
                if not p.is_file():
                    continue
                rel = p.relative_to(ROOT).as_posix()
                sc = scope_of(rel, args)
                if sc is None:
                    if is_red_line(rel):
                        red_cnt += 1
                    continue
                cat, enabled = sc
                if not enabled:
                    continue
                st = p.stat()
                if st.st_mtime >= cutoff and not args.include_today:
                    skipped_hot += 1
                    continue
                items.append({"src": rel, "cat": cat, "size": st.st_size,
                              "mtime": st.st_mtime,
                              "at": dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")})
            except OSError as e:
                print(f"[warn] 跳过 {p}: {e}", file=sys.stderr)
    if args.include_reports and REPORTS_DIR.exists():
        for dirpath, dirnames, filenames in os.walk(REPORTS_DIR):
            dirnames[:] = [d for d in dirnames if d not in PRUNE_DIRS]
            for fn in filenames:
                p = Path(dirpath) / fn
                try:
                    rel = "reports/" + p.relative_to(REPORTS_DIR).as_posix()
                    # 跨项目目录同样走统一红线判定，防止 trade_gate_applied.json
                    # 之类未列进 REPORTS_KEEP_NAME 的状态文件被误切。
                    if not p.is_file() or is_red_line(rel):
                        continue
                    if any(k in fn for k in REPORTS_KEEP_NAME):
                        continue
                    st = p.stat()
                    if st.st_mtime >= cutoff and not args.include_today:
                        skipped_hot += 1
                        continue
                    if ".bak" in fn and not args.include_bak:
                        continue
                    items.append({"src": rel, "cat": "跨项目历史报告", "size": st.st_size,
                                  "mtime": st.st_mtime,
                                  "at": dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                                  "external": str(p)})
                except OSError as e:
                    print(f"[warn] 跳过 {p}: {e}", file=sys.stderr)
    return items, skipped_hot, red_cnt


def resolve(p):
    return Path(p["external"]) if p.get("external") else ROOT / p["src"]


def dest_of(p):
    return ARCHIVE / p["src"]


def do_apply(items):
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    records, done = [], 0
    for i in range(0, len(items), MAX_BATCH):
        batch = items[i:i + MAX_BATCH]
        for p in batch:
            src, dst = resolve(p), dest_of(p)
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                digest = sha256(src)
                shutil.move(str(src), str(dst))
                records.append({**p, "dst": dst.relative_to(ROOT).as_posix(), "sha256": digest})
                done += 1
            except OSError as e:
                print(f"[FATAL] 归档失败 {src}: {e}", file=sys.stderr)
                print(f"  已成功 {done} 个，立即停止。用 --rollback 还原。", file=sys.stderr)
                MANIFEST.write_text(json.dumps({
                    "cutover_date": CUTOVER_DATE, "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "partial": True, "items": records}, ensure_ascii=False, indent=2), encoding="utf-8")
                return 1
        # 逐批校验：目标存在、源已消失
        bad = [p for p in batch if not dest_of(p).exists() or resolve(p).exists()]
        if bad:
            print(f"[FATAL] 第 {i // MAX_BATCH + 1} 批校验失败 {len(bad)} 个，立即停止", file=sys.stderr)
            return 1
        print(f"  批次 {i // MAX_BATCH + 1}: 已归档 {done}/{len(items)}")
    MANIFEST.write_text(json.dumps({
        "cutover_date": CUTOVER_DATE, "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "partial": False, "count": len(records),
        "total_size": sum(r["size"] for r in records), "items": records},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  完成：归档 {len(records)} 个文件 / {human(sum(r['size'] for r in records))}")
    print(f"  MANIFEST: {MANIFEST.relative_to(ROOT)}")
    print(f"  回滚：python3 scripts/cutover_data.py --rollback")
    return 0


def do_rollback():
    if not MANIFEST.exists():
        print(f"[FATAL] 找不到 {MANIFEST}，无法回滚", file=sys.stderr)
        return 1
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    items = data.get("items", [])
    done = 0
    for r in reversed(items):
        src = Path(r["external"]) if r.get("external") else ROOT / r["src"]
        dst = ROOT / r["dst"]
        try:
            if not dst.exists():
                print(f"[warn] 归档文件已不存在，跳过 {dst}", file=sys.stderr)
                continue
            src.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dst), str(src))
            done += 1
        except OSError as e:
            print(f"[FATAL] 回滚失败 {dst}: {e}，已还原 {done} 个", file=sys.stderr)
            return 1
    print(f"  回滚完成：{done}/{len(items)} 个文件已回到原位")
    return 0


def main():
    ap = argparse.ArgumentParser(description="数据 cutover 归档切割（默认 DRY_RUN，可逆）")
    ap.add_argument("--apply", action="store_true", help="执行归档切割")
    ap.add_argument("--rollback", action="store_true", help="从 MANIFEST 一键还原")
    ap.add_argument("--include-today", action="store_true", help="连今日盘中热数据一起切")
    ap.add_argument("--include-bak", action="store_true", help="连 .bak 备份一起切")
    ap.add_argument("--include-shots", action="store_true", help="连验证截图一起切")
    ap.add_argument("--include-reports", action="store_true", help="连 reports 跨项目老文件一起切")
    ap.add_argument("--json", action="store_true", help="输出机器可读清单")
    args = ap.parse_args()

    if args.rollback:
        return do_rollback()

    items, hot, red = collect(args)
    total = sum(x["size"] for x in items)
    by_cat = {}
    for x in items:
        c = by_cat.setdefault(x["cat"], [0, 0])
        c[0] += 1
        c[1] += x["size"]

    if args.json:
        print(json.dumps({"cutover_date": CUTOVER_DATE, "count": len(items),
                          "total_size": total, "by_cat": by_cat,
                          "skipped_today_hot": hot, "red_line_seen": red,
                          "items": items}, ensure_ascii=False, indent=2))
        return 0

    mode = "APPLY" if args.apply else "DRY_RUN"
    print(f"=== market-monitor 数据 cutover [{mode}] · 切割线 {CUTOVER_DATE} 00:00 ===")
    print(f"  命中 {len(items)} 个老文件 / {human(total)}；今日热文件保留 {hot} 个；红线跳过 {red} 个")
    for cat, (n, sz) in sorted(by_cat.items(), key=lambda x: -x[1][1]):
        print(f"    {cat:<20} {n:>5} 个  {human(sz):>10}")
    for x in items[:12]:
        print(f"      {x['at']}  {human(x['size']):>9}  {x['src']}")
    if len(items) > 12:
        print(f"      ... 其余 {len(items) - 12} 个省略")
    if not args.apply:
        print("  这是 DRY_RUN，磁盘未被改动。确认影响范围后加 --apply。")
        return 0
    print("  ⚠️ 即将执行归档切割（移动，不删除；可 --rollback 还原）")
    return do_apply(items)


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""market-monitor 缓存淘汰（体检 P2-6）。

默认 DRY_RUN：只列出「将要删除」的文件与总大小，绝不改动磁盘。
    /opt/homebrew/anaconda3/bin/python3 scripts/prune_caches.py            # 预览
    /opt/homebrew/anaconda3/bin/python3 scripts/prune_caches.py --apply    # 执行

安全约定：
  * 默认**不动** `*.bak*` 备份（用户铁律：大改前备份、备份不入库但也不自动删）；
    确需清理请显式加 --include-bak，且仍受 --apply 约束。
  * 只按 mtime 淘汰 K 线缓存与临时文件，不碰任何策略 / 持仓 / 报告数据。
  * 单批最多 500 个文件，逐批校验；任何异常立即停止并报错。
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
KEEP_DAYS = 7
MAX_BATCH = 500

TARGETS = [
    ("K线缓存文件", DATA / "bottom_kline_cache", "*", KEEP_DAYS * 24 * 3600),
    ("K线缓存目录", DATA / "watchlist_cache", "*", KEEP_DAYS * 24 * 3600),
    ("题材索引临时文件", DATA, "theme_cons_cache.json.tmp", 3600),
]


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def collect(include_bak=False):
    now = time.time()
    found = []
    for label, base, pattern, ttl in TARGETS:
        if not base.exists():
            continue
        for p in base.glob(pattern):
            try:
                if not p.is_file() or now - p.stat().st_mtime < ttl:
                    continue
                found.append((label, p, p.stat().st_size))
            except OSError as e:
                print(f"[warn] 跳过 {p}: {e}", file=sys.stderr)
    if include_bak:
        for p in ROOT.rglob("*.bak*"):
            if ".workbuddy" in p.parts or "tests" in p.parts:
                continue
            try:
                if p.is_file() and now - p.stat().st_mtime > 90 * 86400:
                    found.append(("90天前备份", p, p.stat().st_size))
            except OSError as e:
                print(f"[warn] 跳过 {p}: {e}", file=sys.stderr)
    return found


def main():
    ap = argparse.ArgumentParser(description="market-monitor 缓存淘汰（默认 DRY_RUN）")
    ap.add_argument("--apply", action="store_true", help="真正执行删除")
    ap.add_argument("--include-bak", action="store_true", help="同时清理 90 天前的 .bak 备份")
    ap.add_argument("--days", type=int, default=KEEP_DAYS, help="K 线缓存保留天数")
    args = ap.parse_args()

    global TARGETS
    if args.days != KEEP_DAYS:
        TARGETS = [(l, b, p, args.days * 86400 if "K线" in l else t)
                   for l, b, p, t in TARGETS]

    items = collect(args.include_bak)
    total = sum(s for _, _, s in items)
    mode = "APPLY" if args.apply else "DRY_RUN"
    print(f"=== market-monitor 缓存淘汰 [{mode}] ===")
    print(f"  命中 {len(items)} 个文件，合计 {human(total)}")
    for label, p, size in items[:20]:
        print(f"    {label:<14} {human(size):>9}  {p.relative_to(ROOT)}")
    if len(items) > 20:
        print(f"    ... 其余 {len(items) - 20} 个文件省略")
    if not args.apply:
        print("  这是 DRY_RUN，磁盘未被改动。确认影响范围后加 --apply。")
        return 0
    if not items:
        print("  无需清理。")
        return 0
    print(f"  ⚠️ 即将删除 {min(len(items), MAX_BATCH)} 个文件（本批上限 {MAX_BATCH}）")
    done = 0
    for _, p, _ in items[:MAX_BATCH]:
        try:
            p.unlink()
            done += 1
        except OSError as e:
            print(f"[FATAL] 删除失败 {p}: {e}", file=sys.stderr)
            print(f"  已成功删除 {done} 个，立即停止。", file=sys.stderr)
            return 1
    print(f"  完成：删除 {done} 个文件，释放约 {human(sum(s for _, _, s in items[:done]))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

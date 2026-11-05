#!/bin/bash
# market-monitor 日志切割（体检 P2-3）
#
# 默认 DRY_RUN：只报告，不改任何文件。确认影响范围后再加 --apply。
#   bash scripts/rotate_logs.sh            # 预览
#   bash scripts/rotate_logs.sh --apply    # 执行
#
# 关键点：launchd 持有 stdout/stderr 的文件描述符，直接 mv 会让进程继续写
# 已改名的旧 inode。这里先复制成 .gz 再「截断」原文件（: > file），
# fd 不变、日志不会丢，切割后文件大小归零。

set -euo pipefail
APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1
LOGS=("/tmp/market-monitor.log" "/tmp/market-monitor.err.log")
MAX_BYTES=$((10 * 1024 * 1024))   # 超过 10MB 才切
KEEP=7
STAMP=$(date +%Y%m%d_%H%M%S)

echo "=== market-monitor 日志切割 $( [ $APPLY -eq 1 ] && echo '[APPLY]' || echo '[DRY_RUN]' ) ==="
for f in "${LOGS[@]}"; do
  [ -f "$f" ] || { echo "  skip  $f (不存在)"; continue; }
  size=$(stat -f%z "$f" 2>/dev/null || echo 0)
  printf "  %-34s %10s bytes\n" "$f" "$size"
  if [ "$size" -lt "$MAX_BYTES" ]; then
    echo "    → 未达阈值，跳过"
    continue
  fi
  arc="${f}.${STAMP}.gz"
  if [ "$APPLY" -eq 1 ]; then
    gzip -c "$f" > "$arc"
    : > "$f"                      # 截断，保持 launchd 的 fd 有效
    echo "    → 已归档 $arc 并截断原文件"
    # 只保留最近 KEEP 份
    ls -1t "${f}".*.gz 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
      rm -f "$old" && echo "    → 清理旧档 $old"
    done
  else
    echo "    → 将归档 $arc 并截断原文件（未执行）"
  fi
done
echo "=== 完成 ==="

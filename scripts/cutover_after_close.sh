#!/bin/bash
# -*- coding: utf-8 -*-
# 数据 cutover 方案 B · 收盘后一键执行（用户 2026-11-06 09:45 拍板）
#
#   ./scripts/cutover_after_close.sh            # DRY_RUN 预检，磁盘不动
#   ./scripts/cutover_after_close.sh --apply   # 真执行（15:05 后才允许）
#
# 时序：停服务 → 归档切割（含今日热文件）→ launchd 自动拉起并重建缓存
#       → 全量回归 → 红线存在性校验 → 输出回滚命令
# 任何一步失败立即退出；切割本身可用 cutover_data.py --rollback 一键还原。
set -euo pipefail

PY=/opt/homebrew/anaconda3/bin/python3
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

APPLY=0
for a in "$@"; do
  if [ "$a" = "--apply" ]; then APPLY=1; fi
done

MODE=DRY_RUN
[ "$APPLY" = 1 ] && MODE=APPLY
echo "=== cutover 方案 B [$MODE] · $(date '+%Y-%m-%d %H:%M:%S') ==="

# 0) 时段硬闸：盘中禁止全切（会打断 L0 题材索引 / L2 昨日涨停基线 / 派单状态）
HM=$((10#$(date +%H%M)))
if [ "$APPLY" = 1 ] && [ "$HM" -lt 1505 ]; then
  echo "[ABORT] 当前 $(date +%H:%M) 未到 15:05，盘中禁止执行全切。"
  echo "        方案 B 会切走今日热文件，必须收盘后跑；盘中请改用方案 A。"
  exit 1
fi

# 1) DRY_RUN 预检（无论是否 --apply 都先打一遍影响范围）
"$PY" scripts/cutover_data.py --include-reports --include-shots --include-today
if [ "$APPLY" = 0 ]; then
  echo "DRY_RUN 完成，磁盘未被改动。收盘后加 --apply 执行。"
  exit 0
fi

# 2) 停服务（launchd KeepAlive 会在切割后自动拉起，用磁盘上的新代码重建缓存）
PID=$(lsof -ti :8765 | head -1 || true)
if [ -n "$PID" ]; then
  kill -9 "$PID" 2>/dev/null || true
  echo "已停服务 pid=$PID"
else
  echo "[warn] 8765 端口无进程，继续切割"
fi

# 3) 归档切割
"$PY" scripts/cutover_data.py --include-reports --include-shots --include-today --apply

# 4) 等待 launchd 拉起并探活
UP=0
for i in $(seq 1 40); do
  CODE=$(curl -s -o /tmp/mm_health.json -w '%{http_code}' http://127.0.0.1:8765/api/health 2>/dev/null || true)
  if [ "$CODE" = "200" ]; then
    echo "服务已恢复（${i}s）"
    UP=1
    break
  fi
  sleep 1
done
if [ "$UP" != 1 ]; then
  echo "[FATAL] 服务 40s 内未恢复，先排查；切割可用 --rollback 还原"
  exit 1
fi

# 5) 全量回归
"$PY" -m unittest discover -s tests 2>&1 | tail -5

# 6) 红线存在性校验（切错就立刻发现）
for f in data/access_key.json data/suggestion_state.json data/watchlist.json portfolio.json \
         data/theme_library.json; do
  if [ -f "$f" ]; then
    echo "OK 红线保留 $f"
  else
    echo "[FATAL] 红线文件丢失: $f —— 立即执行 --rollback"
    exit 1
  fi
done
for f in /Users/williamli/Work/reports/slot_status.json \
         /Users/williamli/Work/reports/backtest_database.json; do
  [ -f "$f" ] && echo "OK 跨项目红线保留 $f" || echo "[warn] 跨项目文件不存在（可能本就未生成）: $f"
done

# 7) 关键接口
for ep in /api/theme_pick /api/sprite /api/health /api/dispatch_diag; do
  C=$(curl -s -o /tmp/mm_ep.json -w '%{http_code}' "http://127.0.0.1:8765$ep" 2>/dev/null || true)
  "$PY" -c "import json,sys;json.load(open('/tmp/mm_ep.json'))" 2>/dev/null \
    && echo "OK $ep http=$C strict-json" || echo "[warn] $ep http=$C 非严格 JSON，需排查"
done

echo
echo "=== cutover 完成 ==="
echo "归档位置: .archive/cutover_20261106/"
echo "一键回滚: $PY scripts/cutover_data.py --rollback"
echo "下一步: 启动题材强度历史落盘（schema v2，仅记录 cutover 之后的数据）"

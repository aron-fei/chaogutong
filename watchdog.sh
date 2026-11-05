#!/bin/bash
# market-monitor 守护脚本：检测 8765 端口是否存活，挂了自动拉起
# 用法：后台运行  nohup bash watchdog.sh > /tmp/market-monitor-watchdog.log 2>&1 &

PORT=8765
LOG=/tmp/market-monitor.log
PIDFILE=/tmp/market-monitor.pid
PY=/opt/homebrew/anaconda3/bin/python3
APP_DIR=/Users/williamli/Work/market-monitor

while true; do
  # 用端口检测（最可靠）：8765 有 LISTEN 就视为存活
  LISTEN_PID=$(lsof -nP -iTCP:$PORT -sTCP:LISTEN 2>/dev/null | tail -1 | awk '{print $2}')
  if [ -z "$LISTEN_PID" ]; then
    echo "[$(date '+%F %T')] 端口 $PORT 无监听，拉起服务..." >> "$LOG.watchdog"
    cd "$APP_DIR" || exit 1
    nohup "$PY" -u app.py > "$LOG" 2>&1 &
    NEWPID=$!
    echo $NEWPID > "$PIDFILE"
    echo "[$(date '+%F %T')] 已启动 PID=$NEWPID" >> "$LOG.watchdog"
    sleep 8
  else
    # 同步 pid 文件为实际监听进程（避免 stale pid）
    if [ "$(cat "$PIDFILE" 2>/dev/null)" != "$LISTEN_PID" ]; then
      echo $LISTEN_PID > "$PIDFILE"
    fi
  fi
  sleep 20
done

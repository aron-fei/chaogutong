#!/bin/bash
# crontab 版守护：每分钟执行，8765 无监听则拉起 app.py
PORT=8765
LOG=/tmp/market-monitor.log
PIDFILE=/tmp/market-monitor.pid
PY=/opt/homebrew/anaconda3/bin/python3
APP_DIR=/Users/williamli/Work/market-monitor

LISTEN_PID=$(lsof -nP -iTCP:$PORT -sTCP:LISTEN 2>/dev/null | tail -1 | awk '{print $2}')
if [ -z "$LISTEN_PID" ]; then
  cd "$APP_DIR" || exit 1
  nohup "$PY" -u app.py > "$LOG" 2>&1 &
  echo $! > "$PIDFILE"
  echo "[$(date '+%F %T')] 拉起服务 PID=$!" >> "$LOG.watchdog"
else
  echo $LISTEN_PID > "$PIDFILE"
fi

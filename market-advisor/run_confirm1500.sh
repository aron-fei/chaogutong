#!/bin/zsh
# -*- mode: shell-script; coding: utf-8 -*-
#
# market-advisor 15:00 兜底确认写库 · launchd 包装器（ADR-0032）
# ---------------------------------------------------------------------------
# 用途：当日 09:25/10:40/13:40/14:40 运行时派生的 detached 写库子进程若因故未挂
#       （应用没跑、进程被杀、机器睡眠打断），这里由 launchd 在 15:00 直接触发一次
#       全量 confirm，保证「用户已确认的买入/卖出」一定能进 50万模拟盘。
#
# 触发：launchd StartCalendarInterval Weekday 1-5 15:00
# 幂等：confirm_pending_buy / confirm_pending_sell 对「不存在」或「已改名
#       (.confirmed.json)」的单直接跳过，重复触发零副作用。
# 日志：/Users/williamli/Work/reports/confirm1500_runner.log
# ---------------------------------------------------------------------------

set -u
PY=/opt/homebrew/anaconda3/bin/python3
SCRIPT_DIR=/Users/williamli/.workbuddy/skills/market-advisor/scripts
REPORT_DIR=/Users/williamli/Work/reports
LOG="$REPORT_DIR/confirm1500_runner.log"

# UTF-8 输出环境（launchd 下 locale 可能为 C，脚本大量 print 中文）
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
export LANG="${LANG:-en_US.UTF-8}"
export LC_ALL="${LC_ALL:-en_US.UTF-8}"
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy 2>/dev/null

mkdir -p "$REPORT_DIR"
echo "[$(date '+%F %T')] confirm1500 兜底触发" >> "$LOG"
"$PY" "$SCRIPT_DIR/market_scheduler.py" confirm >> "$LOG" 2>&1
RC=$?
echo "[$(date '+%F %T')] confirm1500 结束 exit=$RC" >> "$LOG"
exit $RC

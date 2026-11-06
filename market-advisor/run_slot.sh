#!/bin/zsh
# -*- mode: shell-script; coding: utf-8 -*-
#
# market-advisor 四时段报告 · launchd 包装器
# ---------------------------------------------------------------------------
# 用途：让 09:25 / 11:40 / 13:40 / 14:40 四个时段报告脱离「WorkBuddy 应用必须
#       常驻」的约束，由 launchd 直接按点触发（ADR-0030）。
#
# 用法：
#   run_slot.sh 09:25            正常运行一个时段（launchd 调用）
#   run_slot.sh --selftest       自检：只验证依赖与路径，不触发任何行情请求/写盘
#
# 退出码：0 成功 / 非 0 = 调度器退出码；参数缺失或环境异常返回 2
#
# 历史坑（务必保留此段）：
#   * WorkBuddy 的 LocalAutomationScheduler 只活在桌面应用进程内，应用退出期间
#     任务完全不触发；启动后才对 missedWindowMs(12h) 内的过期任务做补跑，补跑
#     产物带真实生成时间戳（如 pro_report_20260922_1012_0925.html），时效已失。
#   * launchd 环境没有 shell profile，HTTP_PROXY 等变量缺失是**预期的**：
#     market_scheduler.py 自身会在启动时清除所有代理变量，直连国内行情源。
#   * launchd 的 locale 可能是 C/POSIX，脚本大量 print 中文，必须强制 UTF-8
#     输出，否则可能因 UnicodeEncodeError 直接中断报告生成。
# ---------------------------------------------------------------------------

set -u

STAGE="${1:-}"
# 跨机可移植：路径全部参数化，默认值适配本机；新机器通过环境变量覆盖
PY="${MARKET_PY:-/opt/homebrew/anaconda3/bin/python3}"
SCRIPT_DIR="${SCRIPT_DIR:-${0:A:h}}"          # 由本脚本位置推导，不写死
REPORT_DIR="${REPORTS_DIR:-$HOME/Work/reports}"
# 导出给 market_scheduler.py 使用（其默认路径为本机路径，新机器必须覆盖）
export REPORTS_DIR="$REPORT_DIR"
export MARKET_MONITOR_DIR="${MARKET_MONITOR_DIR:-$HOME/Work/market-monitor}"
RUNNER_LOG="$REPORT_DIR/slot_runner.log"
STATUS_JSON="$REPORT_DIR/slot_status.json"
# 2026-10-21 新增 10:40：10:40 是唯一卖出决策点，此前无 launchd 兜底（只靠 WorkBuddy 应用内自动化）
# → 10-15 报告缺失、10-21 延迟 9 分钟。补齐后与其它四段同构，消除卖出通道单点依赖。
VALID_STAGES=(09:25 10:40 11:40 13:40 14:40)

# ---- 通用：UTF-8 输出环境（launchd 下 locale 可能为 C）----
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
export LANG="${LANG:-en_US.UTF-8}"
export LC_ALL="${LC_ALL:-en_US.UTF-8}"
# 显式清掉代理，避免依赖用户 shell 里的残留设置
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy 2>/dev/null

mkdir -p "$REPORT_DIR"

log() {
  print -r -- "[$(date '+%Y-%m-%d %H:%M:%S')] [${STAGE:-selftest}] $*" >> "$RUNNER_LOG"
}

# ---- 状态落盘（便于「今天到底跑没跑」一眼可查）----
write_status() {
  local slot="$1" rc="$2" report="$3" note="$4"
  "$PY" - "$STATUS_JSON" "$slot" "$rc" "$report" "$note" <<'PYEOF' 2>/dev/null
import json, os, sys, datetime
path, slot, rc, report, note = sys.argv[1:6]
try:
    data = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
except Exception:
    data = {}
data[slot] = {
    "last_run": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "exit_code": int(rc),
    "report": report,
    "note": note,
}
json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PYEOF
}

# ===========================================================================
# --selftest：零副作用自检
# ===========================================================================
if [ "$STAGE" = "--selftest" ]; then
  FAIL=0
  chk() {  # chk <描述> <命令...>
    local desc="$1"; shift
    if "$@" >/dev/null 2>&1; then
      print -r -- "  [PASS] $desc"
    else
      print -r -- "  [FAIL] $desc"
      FAIL=1
    fi
  }
  print -r -- "== run_slot.sh 自检 =="
  chk "python 可执行: $PY"            test -x "$PY"
  chk "scheduler 存在"                test -f "$SCRIPT_DIR/market_scheduler.py"
  chk "txt_to_html 存在"              test -f "$SCRIPT_DIR/txt_to_html.py"
  chk "报告目录可写"                  test -w "$REPORT_DIR"
  chk "cwd 可进入"                    test -d "$SCRIPT_DIR"

  if [ -x "$PY" ]; then
    V=$("$PY" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])' 2>/dev/null)
    print -r -- "  [INFO] python 版本: $V"
    # 语法完整性（不执行）
    if "$PY" - "$SCRIPT_DIR/market_scheduler.py" <<'PYEOF' >/dev/null 2>&1
import ast, sys
ast.parse(open(sys.argv[1], encoding="utf-8").read())
PYEOF
    then print -r -- "  [PASS] scheduler 语法完整（ast.parse）"
    else print -r -- "  [FAIL] scheduler 语法解析失败"; FAIL=1
    fi
    # 关键依赖
    for m in requests akshare pandas; do
      if "$PY" -c "import $m" >/dev/null 2>&1; then
        print -r -- "  [PASS] 依赖 $m"
      else
        print -r -- "  [WARN] 依赖 $m 不可导入（非致命，脚本内部有降级分支）"
      fi
    done
  fi

  print -r -- "  [INFO] HTTP_PROXY=${HTTP_PROXY:-<未设置，预期>}"
  print -r -- "  [INFO] LANG=$LANG  PYTHONUTF8=$PYTHONUTF8"
  # 注意：自检**不写** production 日志/状态文件，保证 --selftest 零副作用
  print -r -- "== 自检结束: $([ $FAIL -eq 0 ] && echo 全部通过 || echo 存在 FAIL) =="
  exit $FAIL
fi

# ===========================================================================
# 正常运行
# ===========================================================================
if [[ ! " ${VALID_STAGES[*]} " == *" $STAGE "* ]]; then
  log "FATAL: 非法时段参数 '${STAGE}'（应为 ${VALID_STAGES[*]} 之一）"
  exit 2
fi

FORCE=0
[ "${2:-}" = "--force" ] && FORCE=1

# 周末守卫（plist 已用 Weekday 1-5 限制，这里是双保险）
DOW=$(date +%u)   # 1=周一 … 6=周六 7=周日
if [ "$DOW" -ge 6 ]; then
  log "skip: 非交易日（周$(date +%u)），不运行"
  exit 0
fi

# 防重复触发守卫：迁移重叠期 launchd 与 WorkBuddy 自动化可能同时命中同一时段，
# 机器唤醒后的补跑也可能与计划时点撞车。同日同档 5 分钟内的第二次触发直接跳过。
# 手动补跑用 `run_slot.sh <时段> --force` 绕过。
LOCK="$REPORT_DIR/.slot_lock_$(date +%Y%m%d)_${STAGE//:/}"
if [ "$FORCE" -eq 0 ] && [ -f "$LOCK" ]; then
  AGE=$(( $(date +%s) - $(stat -f %m "$LOCK") ))
  if [ "$AGE" -lt 300 ]; then
    log "skip: 同日同档 ${AGE}s 前已触发过（防重复触发），本次跳过；如需强制运行加 --force"
    exit 0
  fi
fi
touch "$LOCK"

cd "$SCRIPT_DIR" || { log "FATAL: 无法进入 $SCRIPT_DIR"; write_status "$STAGE" 2 "" "cd 失败"; exit 2; }

log "=== start ==="
START_TS=$(date +%s)

run_scheduler() {
  "$PY" -u market_scheduler.py "$STAGE" >> "$RUNNER_LOG" 2>&1
}

run_scheduler
RC=$?
log "scheduler exit=$RC"

# 找本次新产出的报告（mtime >= START_TS）
find_new_report() {
  local f
  for f in "$REPORT_DIR"/pro_report_*.txt(Nom); do
    if [ "$(stat -f %m "$f" 2>/dev/null)" -ge "$START_TS" ]; then
      print -r -- "$f"
      return 0
    fi
  done
  return 1
}

NEW=$(find_new_report)

# 无新报告 → 判为硬失败，等 30s 重试一次（当日只重试一次，避免刷屏/重复报告）
if [ -z "$NEW" ]; then
  log "WARN: 未发现新报告，30s 后重试一次"
  sleep 30
  run_scheduler
  RC=$?
  log "retry scheduler exit=$RC"
  NEW=$(find_new_report)
fi

if [ -z "$NEW" ]; then
  log "ERROR: 重试后仍未生成 pro_report，请查看本次日志上文报错"
  write_status "$STAGE" "$RC" "" "未生成报告"
  exit $RC
fi

HTML="${NEW%.txt}.html"
if "$PY" -u txt_to_html.py "$NEW" >> "$RUNNER_LOG" 2>&1; then
  log "html ok: $(basename "$HTML")"
else
  log "WARN: txt_to_html 失败，txt 已生成: $(basename "$NEW")"
fi

write_status "$STAGE" "$RC" "$HTML" "ok"
log "=== done exit=$RC 耗时 $(( $(date +%s) - START_TS ))s 报告=$(basename "$NEW") ==="
exit $RC

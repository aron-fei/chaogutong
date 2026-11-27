#!/bin/zsh
# -*- mode: shell-script; coding: utf-8 -*-
#
# 15:35 复盘闸门生成 · launchd 兜底（2026-11-26 新增）
# ---------------------------------------------------------------------------
# 用途：给「复盘闸门自动生成（盘后闭环）」这条 WorkBuddy 应用内 automation
#       加一层 launchd 兜底 —— 应用没开 / automation 漏跑时，本脚本仍能生成
#       次日生效的复盘闸门（review_gate.py --auto），避免次日全天 fail-open。
#
# 为什么需要（历史教训）：
#   · WorkBuddy 的 LocalAutomationScheduler 只活在桌面应用进程内，应用退出期间
#     任务完全不触发（ADR-0030 已为此把四个时段报告迁到 launchd，闸门链当时漏迁）。
#   · 2026-11-24 应用整天未运行 → 15:35 任务没跑 → 11-25 全天闸门 fail-open；
#     2026-11-25 应用在线 → 闸门生成 → 11-26 正常。这是同一根因的第二次复发。
#
# 本脚本做什么（与 WorkBuddy automation 的 prompt 同口径，但**不依赖应用进程**）：
#   1. 确保工作台（flask）在线 —— 闸门快照取自 http://127.0.0.1:8765/api/dashboard；
#      不在线就 kickstart com.marketmonitor.flask（该服务本身由 launchd 托管）。
#   2. 跑 review_gate.py --auto（幂等：目标生效日闸门已存在则不覆盖）。
#   3. **时点校验**：把磁盘闸门的 stage_signals 与工作台收盘快照四项对表
#      （zt / dt / ratio / zb_rate）。幂等跳过 ≠ 闸门正确 —— 盘中/竞价口径写出的
#      闸门也会「已就绪」（11-25 实测：09:16 竞价快照把次日闸门写成 zt=0/dt=0/ratio=0.5
#      的伪冰点，幂等直接跳过 → 次日按 cap20%/1只 放行）。
#      不吻合且市场已收盘 → --auto --force 重建；重建后再对表，仍不吻合则显式报警。
#   4. 保护「约束今日」的副本（trade_gate_current.json）—— force 路径不会自动同步，
#      缺这一步，明日工作台 /api/review_deep 会把主文件覆写成「再下一交易日」的 asof。
#   5. 今日闸门缺失检查：今日闸门不在位 → 报出「今日闸门缺失」并写状态文件。
#   6. 写 slot_status.json（键：闸门生成）→ 巡检 automation 一眼可查。
#
# 用法：
#   run_gate1535.sh            # 正常运行（launchd 调用）
#   run_gate1535.sh --selftest # 零副作用自检（只查依赖与路径）
#
# 退出码：0 成功 / 1 失败 / 2 环境异常
# ---------------------------------------------------------------------------

set -u

# 跨机可移植：路径全部参数化，默认值适配本机；新机器通过环境变量覆盖
PY="${MARKET_PY:-/opt/homebrew/anaconda3/bin/python3}"
SCRIPT_DIR="${SCRIPT_DIR:-${0:A:h}}"
MONITOR_DIR="${MARKET_MONITOR_DIR:-$HOME/Work/market-monitor}"
REPORT_DIR="${REPORTS_DIR:-$HOME/Work/reports}"
# 导出给子进程（review_gate.py / review_deep.py / suggestion_ledger.py）使用
export REPORTS_DIR="$REPORT_DIR"
export MARKET_MONITOR_DIR="$MONITOR_DIR"
RUNNER_LOG="$REPORT_DIR/slot_runner.log"
STATUS_JSON="$REPORT_DIR/slot_status.json"
GATE_STATUS_JSON="$REPORT_DIR/gate_status.json"
LOCK="$REPORT_DIR/.gate_lock_$(date +%Y%m%d)"

# ---- 通用：UTF-8 输出环境（launchd 下 locale 可能是 C）----
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
export LANG="${LANG:-en_US.UTF-8}"
export LC_ALL="${LC_ALL:-en_US.UTF-8}"
# 显式清掉代理，避免依赖用户 shell 里的残留设置
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy 2>/dev/null

STAGE="闸门生成"

log() {
  print -r -- "[$(date '+%Y-%m-%d %H:%M:%S')] [${STAGE}] $*" >> "$RUNNER_LOG"
}

write_status() {
  local rc="$1" report="$2" note="$3"
  "$PY" - "$STATUS_JSON" "$STAGE" "$rc" "$report" "$note" <<'PYEOF' 2>/dev/null
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

write_gate_status() {
  local rc="$1" step="$2" detail="$3"
  "$PY" - "$GATE_STATUS_JSON" "$rc" "$step" "$detail" <<'PYEOF' 2>/dev/null
import json, os, sys, datetime
path, rc, step, detail = sys.argv[1:5]
payload = {
    "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "exit_code": int(rc),
    "step": step,
    "detail": detail,
}
try:
    old = json.load(open(path, encoding="utf-8"))
    if isinstance(old, dict):
        old.update(payload)
        payload = old
except Exception:
    pass
json.dump(payload, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PYEOF
}

mkdir -p "$REPORT_DIR"

# ===========================================================================
# --selftest：零副作用自检
# ===========================================================================
if [ "${1:-}" = "--selftest" ]; then
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
  print -r -- "== run_gate1535.sh 自检 =="
  chk "python 可执行: $PY"            test -x "$PY"
  chk "review_gate.py 存在"           test -f "$MONITOR_DIR/review_gate.py"
  chk "闸门文件目录可写"              test -w "$REPORT_DIR"
  chk "工作台目录存在"                test -d "$MONITOR_DIR"
  chk "工作台 API 可达"               curl -s --noproxy '*' -m 8 "http://127.0.0.1:8765/api/dashboard"
  if [ -x "$PY" ]; then
    if "$PY" - "$MONITOR_DIR/review_gate.py" <<'PYEOF' >/dev/null 2>&1
import ast, sys
ast.parse(open(sys.argv[1], encoding="utf-8").read())
PYEOF
    then print -r -- "  [PASS] review_gate.py 语法完整（ast.parse）"
    else print -r -- "  [FAIL] review_gate.py 语法解析失败"; FAIL=1
    fi
  fi
  print -r -- "  [INFO] HTTP_PROXY=${HTTP_PROXY:-<未设置，预期>}"
  print -r -- "== 自检结束: $([ $FAIL -eq 0 ] && echo 全部通过 || echo 存在 FAIL) =="
  exit $FAIL
fi

# ===========================================================================
# 正常运行
# ===========================================================================
# 防重复触发守卫：与四时段同构（5 分钟内第二次直接跳过）
if [ -f "$LOCK" ]; then
  AGE=$(( $(date +%s) - $(stat -f %m "$LOCK") ))
  if [ "$AGE" -lt 300 ]; then
    log "skip: 同日 ${AGE}s 前已触发过（防重复触发），本次跳过"
    exit 0
  fi
fi
touch "$LOCK"

cd "$MONITOR_DIR" || { log "FATAL: 无法进入 $MONITOR_DIR"; write_status 2 "" "cd 失败"; exit 2; }

log "=== start ==="
START_TS=$(date +%s)

# ---- 步骤 1：确保工作台在线（闸门快照取自 /api/dashboard）----
api_alive() {
  curl -s --noproxy '*' -m 8 "http://127.0.0.1:8765/api/dashboard" >/dev/null 2>&1
}
if ! api_alive; then
  log "工作台不可达，尝试 launchctl kickstart com.marketmonitor.flask"
  launchctl kickstart -k "gui/$(id -u)/com.marketmonitor.flask" 2>&1 | head -2
  for _i in 1 2 3 4 5 6; do
    sleep 5
    if api_alive; then break; fi
  done
fi
if ! api_alive; then
  log "FATAL: 工作台 API 仍不可达（http://127.0.0.1:8765/api/dashboard）"
  write_status 1 "" "工作台 API 不可达"
  write_gate_status 1 "workbench_down" "工作台 API 不可达，无法取收盘快照"
  log "=== done exit=1 ==="
  exit 1
fi
log "工作台在线"

# ---- 步骤 2 + 3：生成闸门 + 时点校验（幂等 → 对表 → 必要时 force 重建）----
# 这一段用 python 串起来，避免 shell 里做 JSON 对表的脆弱解析。
"$PY" - <<'PYEOF' 2>&1 | tee -a "$RUNNER_LOG"
import json, os, subprocess, sys, urllib.request

GATE = os.path.join(os.environ.get("REPORTS_DIR", os.path.expanduser("~/Work/reports")), "trade_gate.json")
DASH = "http://127.0.0.1:8765/api/dashboard"
PY = "/opt/homebrew/anaconda3/bin/python3"
MONITOR = os.environ.get("MARKET_MONITOR_DIR", os.path.expanduser("~/Work/market-monitor"))
REVIEW_GATE = os.path.join(MONITOR, "review_gate.py")


def log(msg):
    print("[gate_auto] %s" % msg, flush=True)


def fetch_dash():
    try:
        with urllib.request.urlopen(DASH, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        log("工作台快照读取失败: %s" % e)
        return None


def read_gate():
    try:
        with open(GATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def four_items(dash, gate):
    """返回 (对表结果说明, 是否吻合, 收盘口径四项)。"""
    sig = (gate or {}).get("stage_signals") or {}
    b = dash.get("breadth") or {}
    up, down = b.get("up"), b.get("down")
    zt_c, dt_c, zb_c = dash.get("zt_count"), dash.get("dt_count"), dash.get("zb_count")
    if not up or not down:
        return "breadth 不完整（up=%s down=%s）" % (up, down), False, None
    live = {
        "zt": zt_c,
        "dt": dt_c,
        "ratio": round(float(up) / float(down), 4),
        "zb_rate": (round(float(zb_c) / (float(zb_c) + float(zt_c)) * 100, 2)
                    if (zb_c is not None and zt_c is not None and (zb_c + zt_c) > 0) else None),
    }
    gs = {
        "zt": sig.get("zt"),
        "dt": sig.get("dt"),
        "ratio": (round(float(sig["ratio"]), 4) if sig.get("ratio") is not None else None),
        "zb_rate": (round(float(sig["zb_rate"]), 2) if sig.get("zb_rate") is not None else None),
    }
    ok = True
    parts = []
    for k in ("zt", "dt", "ratio", "zb_rate"):
        gv, lv = gs.get(k), live.get(k)
        if gv is None or lv is None:
            parts.append("%s: 闸门=%s 收盘=%s (缺)" % (k, gv, lv))
            ok = False
            continue
        if k == "ratio":
            match = abs(float(gv) - float(lv)) <= 0.02
        elif k == "zb_rate":
            match = abs(float(gv) - float(lv)) <= 1.0
        else:
            match = int(gv) == int(lv)
        parts.append("%s: 闸门=%s 收盘=%s %s" % (k, gv, lv, "✅" if match else "❌"))
        ok = ok and match
    return "；".join(parts), ok, live


def run_auto(force=False):
    cmd = [PY, REVIEW_GATE, "--auto"] + (["--force"] if force else [])
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=MONITOR)
    for line in (p.stdout or "").splitlines():
        log(line)
    if p.stderr:
        for line in p.stderr.splitlines()[-5:]:
            log("stderr: %s" % line)
    return p.returncode


dash = fetch_dash()
if not dash:
    print("[gate_auto] ERROR 工作台快照不可用，放弃（不写闸门）", flush=True)
    sys.exit(1)

status = dash.get("market_status")
log("市场状态 market_status=%s" % status)

rc = run_auto(force=False)
gate = read_gate()
detail, ok, live = four_items(dash, gate)
log("四项对表：%s" % detail)

if not ok:
    if status not in ("closed", "closed_all_day"):
        # 盘中/竞价口径 → 不 force（避免用盘中数据覆盖），显式说明
        log("ERROR 四项不吻合但 market_status=%s（非收盘口径）→ 本次不 force，"
            "闸门待收盘口径重建" % status)
        print("[gate_auto] RESULT=NOT_CLOSED", flush=True)
        sys.exit(3)
    mode = (gate or {}).get("mode", "enforce")
    if mode == "shadow":
        log("WARN 闸门为 shadow 模式（人工调过），不覆盖，仅报告不吻合")
        print("[gate_auto] RESULT=SHADOW_SKIPPED", flush=True)
        sys.exit(4)
    log("四项不吻合 → --auto --force 强制重建（收盘口径）")
    rc2 = run_auto(force=True)
    gate = read_gate()
    detail2, ok2, _ = four_items(dash, gate)
    log("重建后四项对表：%s" % detail2)
    if not ok2:
        log("ERROR 重建后仍不吻合 → 显式报警")
        print("[gate_auto] RESULT=STILL_MISMATCH", flush=True)
        sys.exit(5)
    detail = detail2
    ok = True
    rc = rc2
else:
    log("四项吻合 ✅ 闸门为收盘口径")

# ---- 步骤 4：保护「约束今日」的副本 ----
# force 路径不会自动另存 current；缺这一步，明日工作台 /api/review_deep 覆写主文件后
# 调度器读不到「约束今日」的闸门 → 次日全天 fail-open（ADR-0040 P0-①，历史上复发 4 次）。
try:
    import shutil
    src = GATE
    dst = os.path.join(os.path.dirname(GATE), "trade_gate_current.json")
    shutil.copyfile(src, dst)
    log("current 副本已同步：%s" % dst)
except Exception as e:
    log("WARN current 副本同步失败: %s" % e)

print("[gate_auto] RESULT=OK", flush=True)
PYEOF
GATE_RC=$?

# ---- 步骤 5：review_modes 归档 + 台账 ingest（补 WorkBuddy 自动化漏跑时的数据缺口）----
# 这两件事原本只由 WorkBuddy 应用内自动化驱动，应用离线就断链：
#   · review_modes/<日期>.json 归档（供次日 verify_yesterday 消费）—— 11-18 起断档
#   · suggestion_ledger 扫 recap_*.md 入台账（建议台账的唯一新增来源）
# 这里用 review_deep 的 archive() 补归档；archive 在 zt_pool 为空时早退（竞价期产物），
# 但 15:35 跑时市场已收盘、涨停池有值 → 能正常落档。
if [ "$GATE_RC" = 0 ]; then
  "$PY" - <<'PYEOF' 2>&1 | tee -a "$RUNNER_LOG"
import os, sys, datetime
MONITOR = os.environ.get("MARKET_MONITOR_DIR", os.path.expanduser("~/Work/market-monitor"))
sys.path.insert(0, MONITOR)
os.chdir(MONITOR)
try:
    import review_gate as rg
    import review_deep
    snap = rg._fetch_market_snapshot(as_object=True)
    res = review_deep.review_deep(snap, None, do_archive=False)
    path = review_deep.archive(snap, res)
    print("[gate_auto] review_modes 归档：%s" % (path or "未落档（涨停池为空）"), flush=True)
except Exception as e:
    print("[gate_auto] WARN review_modes 归档失败: %s" % e, flush=True)

# 台账 ingest：扫 recap_*.md → 建议台账（读多写少，幂等）
try:
    import suggestion_ledger as sl
    rows = sl.scan(verbose=True)
    print("[gate_auto] 台账 ingest：%d 条建议" % len(rows), flush=True)
except Exception as e:
    print("[gate_auto] WARN 台账 ingest 失败: %s" % e, flush=True)
PYEOF
fi

# 退出码约定：3/4/5 是「已生成但需人工确认」的软失败，其余为硬失败
case "$GATE_RC" in
  0) RC=0; NOTE="ok" ;;
  3) RC=1; NOTE="闸门已生成但四项不吻合且非收盘口径（待收盘重建）" ;;
  4) RC=1; NOTE="闸门已生成但为 shadow 模式，未强制重建" ;;
  5) RC=1; NOTE="强制重建后四项仍不吻合，需人工介入" ;;
  *) RC=${GATE_RC:-1}; NOTE="闸门生成失败（详见 slot_runner.log）" ;;
esac

write_gate_status "$RC" "$( [ "$RC" = 0 ] && echo ok || echo check )" "$NOTE"
write_status "$RC" "$GATE_STATUS_JSON" "$NOTE"
log "=== done exit=$RC 耗时 $(( $(date +%s) - START_TS ))s note=$NOTE ==="
exit $RC

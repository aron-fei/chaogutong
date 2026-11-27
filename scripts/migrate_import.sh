#!/bin/zsh
# -*- mode: shell-script; coding: utf-8 -*-
#
# market-monitor 跨机迁移 · 导入端（新 Mac 上运行）
# ---------------------------------------------------------------------------
# 前置：1) 新机已 git clone 仓库到 $MARKET_MONITOR_DIR（默认 ~/Work/market-monitor）
#       2) 旧机 scripts/migrate_export.sh 产出的 tar.gz 已传到本机
#
# 用法：
#   scripts/migrate_import.sh /path/to/market-monitor-migration_YYYYMMDD.tar.gz
#
# 做什么：
#   1. 解包到临时目录（不落任何东西前先校验 MANIFEST）
#   2. reports/   → ${REPORTS_DIR:-$HOME/Work/reports}（rsync 合并，不删除）
#   3. data/      → 仓库 data/（rsync 合并）
#   4. launchagents → ~/Library/LaunchAgents：若本机用户名与 plist 内路径不同，
#      自动把旧用户家目录前缀改写为本机 $HOME，再 launchctl bootstrap
#   5. workbuddy/ → ~/.workbuddy/skills/market-advisor（恢复运行副本）
#   6. env/env.sh → 缺失的密钥变量追加进 ~/.zshrc
#   7. 自检：run_slot / run_gate1535 / review_gate 三套 selftest + flask 健康检查
#
# 退出码：0 全部成功 / 1 有失败项（见输出）
# ---------------------------------------------------------------------------
set -u

BUNDLE="${1:-}"
REPO_DIR="${MARKET_MONITOR_DIR:-${0:A:h}/..}"
REPO_DIR="${REPO_DIR:A}"
# 2026-11-27 重构：报表目录在仓库根内（单根架构）
REPORT_DIR="${REPORTS_DIR:-$REPO_DIR/reports}"
PY="${MARKET_PY:-/opt/homebrew/anaconda3/bin/python3}"
OLD_HOME_PREFIX="${MARKET_MIGRATE_OLD_HOME:-/Users/williamli}"
FAIL=0

note()  { print -r -- "$*" }
fail()  { print -r -- "  [FAIL] $*"; FAIL=1 }
pass()  { print -r -- "  [PASS] $*" }

[ -n "$BUNDLE" ] && [ -f "$BUNDLE" ] || { note "用法: $0 <migrate_export.sh 产出的 tar.gz>"; exit 2 }
[ -d "$REPO_DIR/market-advisor" ] || { note "FATAL: 请先 git clone 仓库（缺 market-advisor/）: $REPO_DIR"; exit 2 }

TMP="$(mktemp -d)"
note "==> [1/7] 解包 $BUNDLE"
tar -xzf "$BUNDLE" -C "$TMP" || { note "FATAL: 解包失败"; exit 2 }
_cands=("$TMP"/market-monitor-migration_*(N))
if [ ${#_cands} -ne 1 ]; then note "FATAL: 解包后没找到唯一目录: ${_cands[*]}"; exit 2; fi
SRC="${_cands[1]}"
[ -f "$SRC/MANIFEST.txt" ] || { note "FATAL: 不是本项目的迁移包（缺 MANIFEST.txt）"; exit 2 }
pass "MANIFEST 校验通过（$SRC）"

note "==> [2/7] reports → $REPORT_DIR"
mkdir -p "$REPORT_DIR"
rsync -a "$SRC/reports/" "$REPORT_DIR/" && pass "报告目录恢复（$(ls "$REPORT_DIR" | wc -l | tr -d ' ') 项）" \
  || fail "reports 恢复失败"

note "==> [3/7] data → $REPO_DIR/data"
mkdir -p "$REPO_DIR/data"
rsync -a "$SRC/data/" "$REPO_DIR/data/" && pass "data 运行时状态合并" || fail "data 合并失败"

note "==> [4/7] LaunchAgents（用户名不同则改写绝对路径）"
mkdir -p "$HOME/Library/LaunchAgents"
for p in "$SRC/launchagents"/*.plist(N); do
  name="${p:t}"
  tgt="$HOME/Library/LaunchAgents/$name"
  /bin/cp "$p" "$tgt"
  if [ "$HOME" != "$OLD_HOME_PREFIX" ]; then
    /usr/bin/sed -i '' "s|$OLD_HOME_PREFIX|$HOME|g" "$tgt"
    pass "$name（路径已改写为 $HOME）"
  else
    pass "$name"
  fi
  label="${name:r}"
  launchctl bootout "gui/$(id -u)/$label" >/dev/null 2>&1
  if launchctl bootstrap "gui/$(id -u)" "$tgt" >/dev/null 2>&1; then
    pass "  已加载 $label"
  else
    fail "  $label bootstrap 失败（多半下次登录才被扫描，可先手工验证）"
  fi
done

note "==> [5/7] market-advisor 运行密钥 + workbuddy 软链"
# 5a. gitignored 运行文件（broker_config.json / pushplus_token.txt / outputs/）
if [ -d "$SRC/ma_runtime" ]; then
  rsync -a "$SRC/ma_runtime/" "$REPO_DIR/market-advisor/scripts/" \
    && pass "ma_runtime 密钥/输出恢复 → market-advisor/scripts/" \
    || fail "ma_runtime 恢复失败"
else
  note "  [INFO] bundle 无 ma_runtime（旧版包，跳过）"
fi
# 5b. workbuddy 软链（launchd plist 指向 ~/.workbuddy/skills/market-advisor/scripts/）
mkdir -p "$HOME/.workbuddy/skills"
if [ -e "$HOME/.workbuddy/skills/market-advisor" ] \
   && [ ! -L "$HOME/.workbuddy/skills/market-advisor" ]; then
  fail "~/.workbuddy/skills/market-advisor 已是实体目录（非软链），请先手工处理"
else
  ln -sfn "$REPO_DIR/market-advisor" "$HOME/.workbuddy/skills/market-advisor"
  [ -f "$HOME/.workbuddy/skills/market-advisor/scripts/run_slot.sh" ] \
    && pass "workbuddy 软链 → $REPO_DIR/market-advisor" \
    || fail "workbuddy 软链创建异常"
fi

note "==> [6/7] 密钥环境变量"
if [ -s "$SRC/env/env.sh" ]; then
  while read -r line; do
    key="${line%%=*}"
    if /usr/bin/grep -q "export ${key}=" "$HOME/.zshrc" 2>/dev/null; then
      pass "$key 已存在，跳过"
    else
      print -r -- "$line" >> "$HOME/.zshrc"
      pass "$key 已追加到 ~/.zshrc"
    fi
  done < "$SRC/env/env.sh"
else
  note "  [INFO] bundle 无密钥变量"
fi

note "==> [7/7] 自检"
[ -x "$PY" ] || fail "python 不存在: $PY（装 anaconda 或设 MARKET_PY）"
if [ -x "$PY" ]; then
  "$PY" -c 'import akshare, pandas, requests' 2>/dev/null \
    && pass "依赖 akshare/pandas/requests 可导入" \
    || fail "依赖缺失：$PY -m pip install -r $REPO_DIR/requirements.lock.txt"
fi
cd "$REPO_DIR/market-advisor/scripts"
./run_slot.sh --selftest >/dev/null 2>&1 && pass "run_slot 自检" || fail "run_slot 自检"
./run_gate1535.sh --selftest >/dev/null 2>&1 && pass "run_gate1535 自检" || fail "run_gate1535 自检"
[ -x "$PY" ] && "$PY" "$REPO_DIR/review_gate.py" --selftest >/dev/null 2>&1 \
  && pass "review_gate 自检" || fail "review_gate 自检"
cd "$REPO_DIR"
sleep 3
curl -sf http://127.0.0.1:8765/api/health >/dev/null 2>&1 \
  && pass "工作台在线 (127.0.0.1:8765)" \
  || fail "工作台未响应（flask 刚 bootstrap 可能有延迟，等 1 分钟再 curl 一次）"

rm -rf "$TMP"
note ""
if [ $FAIL -eq 0 ]; then
  note "== 迁移导入完成 ✅  手动收尾清单："
else
  note "== 导入完成但有失败项 ⚠  手动收尾清单："
fi
note "   1) ~/.ssh 私钥+config 手动拷（git push 要用；新机 GitHub 也可改走 HTTPS+token）"
note "   2) WorkBuddy 桌面应用重装 + 重建 15:30 盘后复盘 automation（闸门已有 launchd 兜底，不装不影响闸门）"
note "   3) Clash/代理（github/Gemini 需要走）"
note "   4) source ~/.zshrc 让 PUSHPLUS_TOKEN 生效"
exit $FAIL

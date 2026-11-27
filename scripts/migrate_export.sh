#!/bin/zsh
# -*- mode: shell-script; coding: utf-8 -*-
#
# market-monitor 跨机迁移 · 导出端（旧 Mac 上运行）
# ---------------------------------------------------------------------------
# 用途：把 README §7.1 列出的「仓库外资产」打包成一个可搬移的 bundle。
#       代码本身走 git clone，不进 bundle；密钥/运行时状态/launchd 配置进 bundle。
#
# 用法：
#   scripts/migrate_export.sh                  # 打包到 ~/Desktop
#   scripts/migrate_export.sh /path/to/out.tar.gz
#
# bundle 内部结构（全部相对路径，导入端自动映射到新机 $HOME）：
#   reports/        ← 仓库根/reports（默认 $MARKET_MONITOR_DIR/reports，含闸门/模拟盘账户/历史报告）
#   data/           ← 仓库 data/ 全部运行时状态（排除可重建的 K 线缓存）
#   ma_runtime/     ← market-advisor/scripts/ 的 gitignored 运行文件
#                    （broker_config.json / pushplus_token.txt / outputs/）
#   launchagents/   ← com.marketadvisor.* / com.marketmonitor.flask plists
#   env.sh          ← 从 ~/.zshrc 抓出的 PUSHPLUS_TOKEN 等推送密钥
#   MANIFEST.txt    ← 原始路径 → 新路径的映射说明（导入脚本读它）
#
# 说明（2026-11-27 单根重构后）：代码 + 运行密钥 + 报表全部以仓库为单一根；
#   ~/.workbuddy/skills/market-advisor 在旧机已是指向仓库的软链，不再需要打包
#   —— 导入端会直接在新机创建同样的软链。
#
# 退出码：0 成功 / 2 环境异常
# ---------------------------------------------------------------------------
set -u

REPO_DIR="${MARKET_MONITOR_DIR:-${0:A:h}/..}"
REPO_DIR="${REPO_DIR:A}"
# 2026-11-27 重构：报表目录在仓库根内（单根架构）
REPORT_DIR="${REPORTS_DIR:-$REPO_DIR/reports}"
STAMP="$(date +%Y%m%d)"
OUT="${1:-$HOME/Desktop/market-monitor-migration_${STAMP}.tar.gz}"

# 依赖检查
for c in tar rsync; do
  command -v "$c" >/dev/null 2>&1 || { print "FATAL: 缺少 $c"; exit 2; }
done
[ -d "$REPORT_DIR" ] || { print "FATAL: 报告目录不存在 $REPORT_DIR"; exit 2; }
[ -d "$REPO_DIR/data" ] || { print "FATAL: 仓库 data/ 不存在（REPO_DIR=$REPO_DIR）"; exit 2; }

STAGE="$(mktemp -d)/market-monitor-migration_${STAMP}"
mkdir -p "$STAGE"

print "==> [1/5] 报告目录（闸门/账户/历史） $REPORT_DIR"
mkdir -p "$STAGE/reports"
rsync -a --exclude='.slot_lock_*' --exclude='.gate_lock_*' \
      "$REPORT_DIR/" "$STAGE/reports/"

print "==> [2/5] data/ 运行时状态（排除可重建缓存）"
mkdir -p "$STAGE/data"
rsync -a --exclude='bottom_kline_cache' --exclude='fut_kline_cache.json' \
      --exclude='*.bak_*' "$REPO_DIR/data/" "$STAGE/data/"

print "==> [3/5] LaunchAgents（com.market*）"
mkdir -p "$STAGE/launchagents"
for p in ~/Library/LaunchAgents/com.marketadvisor.*.plist \
         ~/Library/LaunchAgents/com.marketmonitor.*.plist; do
  [ -f "$p" ] && /bin/cp "$p" "$STAGE/launchagents/"
done

print "==> [4/5] market-advisor 运行密钥（gitignored）+ 推送密钥"
MA_RT="$REPO_DIR/market-advisor/scripts"
mkdir -p "$STAGE/ma_runtime"
for f in broker_config.json pushplus_token.txt; do
  [ -f "$MA_RT/$f" ] && /bin/cp "$MA_RT/$f" "$STAGE/ma_runtime/" \
    && print "    打包 $f" || print "    [WARN] 缺 $MA_RT/$f"
done
[ -d "$MA_RT/outputs" ] && rsync -a "$MA_RT/outputs/" "$STAGE/ma_runtime/outputs/" \
  || print "    [WARN] 缺 outputs/（历史提示产物，可无）"
mkdir -p "$STAGE/env"
: > "$STAGE/env/env.sh"
/usr/bin/grep -E "PUSHPLUS_TOKEN|WORKBENCH_KEY" "$HOME/.zshrc" 2>/dev/null \
  | while read -r line; do print -r -- "$line" >> "$STAGE/env/env.sh"; done
if [ -s "$STAGE/env/env.sh" ]; then
  print "    抓到密钥环境变量：$(/usr/bin/wc -l < "$STAGE/env/env.sh" | tr -d ' ') 行"
else
  print "    [WARN] ~/.zshrc 里没抓到 PUSHPLUS_TOKEN/WORKBENCH_KEY"
fi

print "==> [5/5] MANIFEST + 打包"
cat > "$STAGE/MANIFEST.txt" <<EOF
market-monitor 迁移包 ${STAMP}（2026-11-27 单根架构版）
- reports/     → 新机仓库根/reports（\${MARKET_MONITOR_DIR:-\$HOME/Work/market-monitor}/reports）
- data/        → 新机仓库根/data（合并，不删新机已有文件）
- ma_runtime/  → 新机仓库根/market-advisor/scripts/（broker_config.json/pushplus_token.txt/outputs）
- launchagents/ → ~/Library/LaunchAgents（若新机用户名不同，导入脚本自动改写绝对路径）
- env/env.sh   → 追加进新机 ~/.zshrc（若缺失）
【导入端自动创建】
- ~/.workbuddy/skills/market-advisor → 软链指向仓库 market-advisor（launchd plist 路径依赖它）
【不进 bundle、需手动处理】
- ~/.ssh 私钥与 config（git push 需要，出于安全不打包）
- Python 环境：新机装 anaconda 或 pip install -r requirements.lock.txt
- WorkBuddy 桌面应用 + 15:30 盘后复盘 automation（可选；闸门链有 launchd 兜底，
  机械版 recap 有 run_slot.sh 尾部钩子，应用不装不影响闸门与复盘产出）
- Clash/代理配置（github/Gemini 需要）
EOF

tar -czf "$OUT" -C "${STAGE:h}" "market-monitor-migration_${STAMP}"
print ""
print "== 导出完成: ${OUT}（$(du -h "$OUT" | cut -f1)） =="
print "   传输方式任选：AirDrop / scp / U盘。"
print "   新机：git clone 仓库 → scripts/migrate_import.sh ${OUT}"

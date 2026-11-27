# market-monitor 项目地图（ARCHITECTURE.md）

> 2026-11-27 单根重构版。目标：**一眼定位**——报表在哪、脚本在哪、状态在哪、
> 出问题先看哪。新机克隆下来即同结构（迁移见 README §7）。

---

## 1. 单根架构总图

```
~/Work/market-monitor/            ← 唯一项目根（git 仓库 chaogutong）
├── app.py                        # Flask 工作台 :8765（launchd 常驻）
├── config.py                     # 全项目统一路径/参数基准（路径从这里取，别处不写死）
├── ~40 个业务 .py                # 平铺模块，按 §2 功能分组理解
├── market-advisor/               # 调度链（launchd 直接指向这里）
│   ├── SKILL.md                  # 调度链使用手册 + Pitfalls 铁律
│   └── scripts/                  # market_scheduler.py 主调度器 + run_*.sh 包装器
│       ├── broker_config.json    # 券商连接（gitignored，迁移走 ma_runtime）
│       └── pushplus_token.txt    # 微信推送 token（同上）
├── reports/                      # ★ 所有报表/闸门/账户/日志（gitignored 运行时状态）
├── data/                         # 行情抓取缓存 + watchlist 等状态（gitignored 为主）
├── scripts/                      # 运维脚本：迁移/看门狗/盘后 cutover
├── tests/                        # 集成测试（root 各模块）+ market-advisor/scripts/test_adr*.py
├── docs/                         # 文档与验证截图
├── templates/ static/            # Flask 页面
├── wechat-miniprogram/           # 小程序端
└── .archive/                     # 历史备份坟场（本地保留，git 不跟踪）

仓库外仅剩 3 个挂点（都是「入口」不是「资产」）：
  ~/.workbuddy/skills/market-advisor   → 软链指向仓库 market-advisor/（launchd 路径依赖）
  ~/Work/reports                        → 软链指向仓库 reports/（旧入口兼容）
  ~/Library/LaunchAgents/com.market*   → 9 个 plist 定时任务（§3）
```

**为什么平铺 .py 不再拆子目录**：所有模块互相平级 import，拆子目录要重写全部
import + Flask 模板解析路径，收益却只是「目录好看」。定位问题靠本文件 §2 的
职责表，不靠文件夹名。若未来要拆，须安排在周末无交易日，全量跑 tests/。

---

## 2. 模块职责表（root 平铺 .py 按功能分组）

| 分组 | 文件 | 职责 |
|---|---|---|
| **工作台** | app.py | Flask :8765 监控台首页/全部 API |
| | dispatch_diag.py | 时段派单诊断（为什么派/没派这只票） |
| | log_util.py | 统一日志工具 |
| | daemon_launcher.py | 手动拉起后台抓取线程的入口 |
| **行情抓取** | data_fetcher.py | 后台线程+内存缓存主抓取器（akshare/腾讯） |
| | board_rank.py | 腾讯板块榜（涨速数据源） |
| | overseas_fetcher.py | 港股/美股隔夜/外盘 |
| | futures_fetcher.py | 期货行情 |
| | news_catalyst.py | 新闻催化层（首板雷达 L0 因子） |
| | watchlist.py | 自选股池 + DK 操盘线 |
| | gjd_etf.py | 国家队 ETF 资金面跟踪 |
| | zt_board_stat.py | 板块强度榜/涨停晋级率 |
| **建议引擎群** | advisor.py | 时段操作建议主引擎 |
| | mood_engine.py | 操盘心法量化规则 |
| | theme_arb.py / theme_board.py / theme_picker.py | 题材联动套利/细分板块/首板推票 |
| | sprite.py | 短线精灵·异动雷达 M7 |
| | bottom_surge.py / bottom_radar.py | 底部放量扫描 M8 |
| | firstboard_setup.py | 首板蓄势形态评分 ADR-0053 |
| | genzhuang_scan.py | 跟庄建仓全市场扫描 |
| | resonance.py | 跟庄×底部双信号共振 ADR-0055 |
| | linkage.py | 联动观察 M4 |
| | fut_advisor.py / futures_strategy.py | 期货超短顾问 M6/策略 M5 |
| | strategy_router.py | 多策略路由 ADR-0051（四模式分发） |
| **复盘与闸门** | review.py | 建议持久化+准确率 |
| | review_deep.py | 六引擎复盘 ADR-0039 |
| | review_gate.py | ★ 复盘闸门 ADR-0040（T日结论→T+1交易约束表） |
| | recap_confirm.py | 操作确认单盈亏核算 |
| | market_review.py | 盘后综合行情复盘（15:30 slot 用） |
| **组合与账户** | portfolio.py / portfolio.json | 持仓+实时估值 |
| | position_watch.py | 持仓实时跟踪+异动弹窗 M9 |
| | suggestion_ledger.py | 复盘建议台账 |
| **一次性运维** | apply_manual_sell.py / fix_rj_position_20261021.py / migrate_suggestion_state.py | 历史手工修账（留档，勿日常运行） |

`market-advisor/scripts/` 内：market_scheduler.py（★ 350KB 主调度器，选股/派单/
推送/逃生阀全在这）、strategy_router.py 同名副本（调度链专用）、run_slot.sh /
run_gate1535.sh / run_confirm1500.sh（launchd 包装器）、build_mechanical_recap.py
（零 AI 机械复盘）、test_adr*.py（调度链回归测试）、holdings_review.py /
intelligence_crawler.py / broker_easytrader.py / confirm_gui.py / popup_gui.py 等
调度链配套。

---

## 3. 运行拓扑（launchd 9 任务）

| 时间 | plist | 干什么 | 产物在 reports/ |
|---|---|---|---|
| 常驻 | com.marketmonitor.flask | app.py 工作台 :8765 | — |
| 09:25 | slot0925 | 早盘选股+首单派单 | pro_report_*_0925.html, slot_status.json |
| 10:40 | slot1040 | 唯一卖出决策点 | pro_report_*_1040.html |
| 11:40 | slot1140 | 午盘报告 | pro_report_*_1140.html |
| 13:40 | slot1340 | 午后报告 | pro_report_*_1340.html |
| 14:40 | slot1440 | 尾盘报告 | pro_report_*_1440.html |
| 15:00 | confirm1500 | 操作确认单 | recap_confirm_*.json |
| 15:30 | slot1530 | 盘后复盘（尾部钩子：机械版 recap） | review_*.json, recap_YYYY-MM-DD.md |
| 15:35 | gate1535 | ★ 复盘闸门生成 | trade_gate.json, gate_status.json |

所有 plist 最终执行的都是 `~/.workbuddy/skills/market-advisor/scripts/`（软链
→ 仓库）。**改调度链代码 = 直接改仓库 market-advisor/scripts/，无需再同步第二份。**

## 4. 状态与数据文件（reports/ 内，坏了丢哪找哪）

| 文件 | 是什么 | 谁写 | 谁读 |
|---|---|---|---|
| trade_gate.json | ★ 次日交易闸门（cap/hard_block/max_picks） | gate1535 | 09:25 调度器、工作台 |
| backtest_database.json | ★ 模拟盘账户（每笔假设成交） | 各 slot | recap_confirm、工作台 |
| strategy_state.json | ★ 龙头/主线延续状态（跨日） | 各 slot | 调度器 |
| slot_status.json | 各时段执行状态 | run_slot.sh | 诊断 |
| gate_status.json | 闸门生成状态/四项对表 | run_gate1535.sh | 诊断 |
| slot_runner.log / confirm1500_runner.log | 各链路运行日志 | run_*.sh | ★ 排障第一入口 |
| recap_YYYY-MM-DD.md | 每日复盘（AI 版缺失时机械版兜底） | slot1530 尾部钩子 | 人 |
| pro_report_*.html | 各时段报告（460+，可定期清理旧档） | 各 slot | 人 |

## 5. 故障排查速查表（症状 → 第一眼看哪）

| 症状 | 先看 |
|---|---|
| 某时段报告没出 | `launchctl list | grep market`（exit code）→ tail reports/slot_runner.log → slot_status.json |
| 派单数/仓位不对 | reports/trade_gate.json（闸门内容）→ dispatch_diag.py → market_scheduler.py 对应段 |
| 次日没有闸门 | gate_status.json → tail slot_runner.log 的 1535 段 → 手跑 run_gate1535.sh |
| 工作台打不开 | curl :8765/api/health → launchctl kickstart flask → app.py 报错看 plist 重定向 |
| 推送没收到 | slot_runner.log 里 push 段 → pushplus_token.txt / ~/.zshrc PUSHPLUS_TOKEN |
| git push 失败 | Clash Verge 是否在跑（fake-IP DNS 半死时 github 解析成 127.0.0.1）→ 仓库 core.sshCommand 443 端口规则勿删 |
| 想确认双副本没漂移 | `readlink ~/.workbuddy/skills/market-advisor` 应指向仓库（现在是软链，天然不会漂） |

## 6. 项目铁律（违反必出事故）

1. **涨跌停家数一律 akshare 股池实数**（zt_count/dt_count），统计用 *_all 全量池，
   不用截断池，不用 breadth 估算（11-26 recap 177→43 事故）。
2. **0 值语义 = 禁止开仓**，判定必须 `is None`，不能 if truthiness（10-21 炸板率、
   11-27 逃生阀两起同型事故）。
3. **报告仓位 = 派单仓位**（ADR-0049），必须走 _gate_position_text，不得直写分档建议。
4. 改 market-advisor 代码直接改仓库 scripts/ 即可（软链使 launchd 生效）；
   ~~双副本手动同步~~ 已于 2026-11-27 消灭。
5. REPORTS_DIR 默认 = 仓库根/reports；如需外置，用环境变量覆盖，不要改代码写死。

## 7. 新机引导（30 分钟）

1. `git clone git@github.com:aron-fei/chaogutong.git ~/Work/market-monitor`
2. 装 anaconda（/opt/homebrew/anaconda3）→ `pip install -r requirements.lock.txt`
3. 旧机跑 `scripts/migrate_export.sh` → bundle 拷过来 → `scripts/migrate_import.sh <bundle>`
   （自动恢复 reports/data/密钥/launchd + 建 workbuddy 软链 + 全套自检）
4. 手动：~/.ssh 私钥、Clash/代理、（可选）WorkBuddy 桌面应用。

## 8. 本地残留说明

`.archive/`（git 不跟踪）里是 2026-11-27 归档的：~180 个 .bak_* 历史备份、
workbuddy 旧实体副本（workbuddy_skill_20261127，软链出问题时可回滚）、
cutover 快照。确认稳定运行 2 周后可整体删除。

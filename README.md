# market-monitor · A 股工作台

本地 Flask 实时监控台，服务端口 **8765**，由 launchd 常驻托管。
纯读行情 + 模拟盘建议，**不接任何实盘下单通道**；A 股持仓严格 T+1 口径。

> 最近一次结构重整：2026-11-05（树形导航 + 细分题材板块），详见 `docs/health_check_20261105.html` 与复检报告。

---

## 1. 启动 / 重启 / 探活

```bash
PY=/opt/homebrew/anaconda3/bin/python3      # 必须用 anaconda：自带 akshare / pandas

# 服务由 launchd 托管，改完代码杀掉进程即会自动按新代码拉起
lsof -ti :8765 | head -1 | xargs kill -9
curl -s http://127.0.0.1:8765/api/health | python3 -m json.tool

# 前台调试（launchd 会抢端口，先停用或换端口）
$PY app.py
```

- plist：`~/Library/LaunchAgents/com.marketmonitor.flask.plist`（`RunAtLoad` + `KeepAlive`）
- 日志：`/tmp/market-monitor.log`（stdout）、`/tmp/market-monitor.err.log`（stderr）
- **模板有缓存**：改 `templates/workbench.html` 后必须重启进程，否则浏览器拿到的仍是旧 HTML。
- `/api/health` 返回线程存活表、各数据源新鲜度、最近错误、慢接口缓存年龄、题材索引状态、被抑制异常统计。

本机回环直连免密；手机 / LAN 访问需 `?k=<ACCESS_KEY>`（密钥在 `data/access_key.json`，或环境变量 `WORKBENCH_KEY` 覆盖）。403 页面不会回显密钥。

## 2. 页面结构（6 个一级组 / 16 个视图）

侧栏为**可展开树形菜单**：点一级组展开二级视图，内容区顶部有面包屑与取数时间。

| 一级组 | 二级视图 |
| --- | --- |
| 🎯 决策产出 | 时段建议 · 时段报告 |
| 📊 市场全景 | 大盘速览 · 板块涨幅 · 涨停与风险 · 联动观察 |
| ⚡ 异动信号 | 短线精灵/首板雷达 · 个股异动 · 龙头候选 |
| 🎣 候选池 | 底部放量 · 题材套利 · 自选股池·DK点 |
| 💼 持仓 | 持仓管理（真实持仓·只读）· 持仓盯盘（50 万模拟盘） |
| 🛡️ 复盘与规则 | 复盘模式 · 操盘心法 |

另有多市场参考：港股 / 美股隔夜 / 国内期货（M1/M2/M3），各自带二级视图。
「风险池」已物理并入「涨停与风险」，同源 `/api/dashboard` 一次渲染。

### 板块涨停分布（v2）

- **两个维度**：`细分题材`（默认）/ `一级行业`，可一键切换。
- **排序**：默认按涨停家数降序（最多的板块在最前），可切最高连板 / 板块涨幅 / 涨速。
- **二级下钻**：卡片默认折叠，前 3 个自动展开；点卡片头展开成分股表格与连板梯队。
- **工具**：板块/个股搜索、`≥2家` 过滤（默认开）、全部展开、显示其余板块（默认前 20）。
- **细分题材来源**：`theme_board.py` 用东财概念板块成分股建「个股 → 多题材」反向索引，过滤「连续亏损/最近多板」等伪概念，近义概念按 `ZT_THEME_ALIASES` 合并（光模块·CPO / 商业航天 / 光伏新技术…），并自动发现「X字辈」炒作（白名单限生肖/祥瑞字，防噪音）。
- **口径诚实**：题材级无权威资金流字段，卡片上的「主行业资金\*/涨速\*」按成分股主行业近似；一级行业维度给的是腾讯板块榜原始净流入/涨速。
- 首板雷达 / 短线精灵为**语音播报**（Web Speech API），无弹窗。

### 首板推票 v2 · 三层漏斗（2026-11-06 起）

用户拍板原则：**打首板之前只推「当天第一次涨停 + 即将涨停 + 封板意愿特别强」**。
v1 的「+3~4% 早段异动」已彻底移除（实盘反馈：推过 4 次，买了就跌）。

| 层 | 做什么 | 接口 | 语音 |
| --- | --- | --- | --- |
| L0 趋势题材 | 涨停家数 / 连板梯队 / 题材涨幅 / 涨速 / 主行业资金 / 新闻催化加权 → 趋势题材榜 | `/api/theme_pick` | 否 |
| L1 低位挖掘 | 只在趋势题材成分股里找未涨停、题材内相对低位、量能刚抬头的票 | `/api/theme_pick` | 否 |
| L2 临板首板 | 板进度 ≥0.85 + 真实五档封板意愿 ≥68 + 趋势质量 ≥55 + 连续 2 帧确认 | `/api/sprite` | **是，唯一强播报** |

- **板进度** = `(现价-昨收)/(涨停价-昨收)`，自动适配主板 10% / 创业科创 20% / 北交 30% / ST 5%。
- **封板意愿**只用腾讯 `qt.gtimg.cn` 真实五档字段（委差、卖一量、买五档、外盘、均价、新高、60s 涨速），取不到就降权并标注，**不编指标**。
- 排除项：今日已封板 / 当前涨停池 / 昨日涨停（连板接力）/ ST / 退市 / N·C 新股 / 巨量出货；冷却 300s、每帧最多 3 只、大盘情绪闸门沿用。
- 新闻催化：`news_catalyst.py` 走东财全球财经快讯（近 4h / 200 条），只做真实催化加分与标签，源不可用标 degraded，**绝不编造传闻**。
- **会话态六分**：`preopen / auction / trading / lunch / paused / closed` 由交易时钟 + 实时帧共同判定。只有 `trading` 才允许标「即将冒头」，杜绝把盘前或躺平票包装成爆发信号。
- **盘前隔夜榜**：当日涨停池为空时，用「昨日涨停成分股所属题材 + 真实快讯」给方向榜，前端明确标「隔夜口径 · 昨涨停」，开盘产生涨停后自动切回实时 L0。
- 回归测试：`tests/test_first_board_v2.py` 锁死「+3~4% 早段 0 事件」「弱意愿不推」「连板接力排除」；全量 `unittest` 23 项。

### 派单诊断

`/api/dispatch_diag` 把「报告 ready 但零派单」从黑盒变成逐时段结论：读 `slot_status.json`、
`order_plan_*.json`、`pending_buy_confirm_*.json` 与闸门 asof，区分「情绪闸门拦截 / 候选池为空 / 未生成派单文件」。
**报告 Top3 候选 ≠ 有派单**，弹窗只认 `pending_buy_confirm_*.json`。

### 相关报告

`docs/kaipanla_research_20261106.html`：开盘啦 App 功能与页面设计调研（含可借鉴清单与数据源可行性矩阵）。

## 3. 数据口径与数据源

| 用途 | 来源 | 备注 |
| --- | --- | --- |
| 全 A 行情 / 宽度 / 涨速 | 腾讯 `qt.gtimg.cn` 批量 | 约 5200 只，47 批 ≈13s；替代被 WAF 封禁的新浪全市场 |
| 指数实时 | 腾讯 `qt.gtimg.cn` | 快（~0.1s） |
| 指数 / 个股日 K、MA20/30 | 新浪 `ak.stock_zh_index_daily`、东财 `ak.stock_zh_a_hist` | **腾讯 `web.ifzq.gtimg.cn/fqkline` 已失效（返回非 JSON），勿再用** |
| 涨停 / 跌停 / 炸板池 | 东财 `stock_zt_pool_*_em` | 含所属行业、连板数、封板资金 |
| 概念板块榜 + 成分股 | 东财 `stock_board_concept_name_em` / `stock_board_concept_cons_em` | 531 个概念，取热点前 N + 常驻题材并发拉，缓存 `data/theme_cons_cache.json` |
| 板块涨速 / 净流入 | 腾讯板块榜 `board_rank.py` | 东财行业名 → 腾讯板块名映射见 `config.ZT_INDUSTRY_TO_TENCENT_BOARD` |
| 板块 / 概念清单 | 同花顺 THS | 部分接口已失效，见代码内降级分支 |

- **真实持仓**（`portfolio.json`）只读参照；**50 万模拟盘**是策略口径，两者在 UI 上有 `scope-tag` 区分，不要混用。
- 所有券商侧自动化保持 `DRY_RUN` / simulated，绝不实盘下单。

## 4. 目录与关键文件

```
app.py                 Flask 入口、路由、门禁、dashboard 聚合、慢接口 TTL 缓存
config.py              全部阈值与映射（SPRITE_FB_* / ZT_THEME_* / ZT_INDUSTRY_TO_TENCENT_BOARD）
data_fetcher.py        指数/板块/涨停池/全A 抓取、宽度、涨速、MA
theme_board.py         细分题材反向索引（并发 + 本地缓存 + 降级）
review.py              建议持久化与复盘准确率（日K 双源 + 并发预热）
review_deep.py / review_gate.py / suggestion_ledger.py   六引擎复盘 / 次日闸门 / 建议台账
sprite.py              首板雷达与异动事件流
board_rank.py / bottom_radar.py / position_watch.py       板块榜 / 底部雷达 / 持仓盯盘
log_util.py            降噪告警（同一异常 60s 内只打一次）
templates/workbench.html  唯一主模板（内联 CSS + JS，约 5.3k 行）
tests/test_smoke.py    最小回归测试集
scripts/               日志轮转、缓存清理、闸门修复验证脚本
docs/                  体检报告、验证截图、设计文档
```

运行时数据全部在 `data/`，**密钥与真实持仓不入库**（见 `.gitignore`）。
改代码前的本地备份命名为 `*.bak_<日期>_<原因>`，由全局 `~/.gitignore_global` 统一忽略，不进 git。

## 5. 验证（交付前必跑）

```bash
PY=/opt/homebrew/anaconda3/bin/python3

$PY -m unittest discover -s tests -v        # 15 项回归，约 4s
$PY theme_board.py --selftest               # 字辈/索引自检
$PY log_util.py                             # 降噪告警自检
$PY review_gate.py --health && $PY review_gate.py --selftest
$PY scripts/prune_caches.py                 # DRY_RUN，确认影响范围后才加 --apply
bash scripts/rotate_logs.sh                 # DRY_RUN，确认后才加 --apply
```

前端改动另需浏览器验收：6 组逐组展开、二级视图切换、板块卡片下钻、控制台 0 error、截图复核。
大改流程：**备份 → 静态检查（ast / node --check）→ 集成测试 → 重启 → 截图 → ADR/记忆归档**。

## 6. 运维

- 日志切割：`scripts/rotate_logs.sh --apply`（超过 10MB 才切，保留 7 份；用截断而非 mv，避免 launchd 持有旧 fd）。
- 缓存淘汰：`scripts/prune_caches.py --apply`（默认 DRY_RUN，仅清理过期 K 线缓存与临时文件，**默认不动 `.bak` 备份**）。
- 题材索引缓存 TTL 30 分钟，重启优先读本地缓存；接口全挂时自动降级为「行业兜底」，前端会明确标注，不伪装成细分题材。
- 依赖精确版本见 `requirements.lock.txt`；日常安装用 `requirements.txt`。
- 决策与架构记录：`~/Work/ObsidianVault/05-决策记录ADR/`（`market-monitor` 项目档案在 `06-项目档案/`）。

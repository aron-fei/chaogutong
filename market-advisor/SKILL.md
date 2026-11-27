---
name: market-advisor
description: Automated monitoring and analysis of global financial markets for trading support, focusing on A-share Dragon Head (龙头战法) strategy using Akshare and Xuangubao APIs.
---

# Market Advisor Skill

This skill provides automated monitoring and analysis of global financial markets to support trading decisions in A-shares, HK-shares, and futures, focusing on the "Dragon Head" (龙头战法) short-term trading strategy.

## Configuration
- **Data sources**: 
  1. `akshare` library (Sina/Eastmoney APIs) for macro indices and broad market data.
  2. `Xuangubao` (选股宝/选股通) internal REST APIs (`https://flash-api.xuangubao.com.cn`) for tick-level sentiment, market temperature, explosive sector changes, and limit-up pool logic.
- **Environment**: Executed using the local Python environment where `akshare` and `requests` are installed.
- **Frequency**: Configured via macOS LaunchAgents at exactly 09:25, 11:40, 13:40, and 14:40 to align with early auction, midday data, intraday holding management, and late-day layouts. (Note: The 09:00 and 13:50 runs are deprecated).
- **10:40 是唯一的卖出决策点 (CRITICAL, 勿误删)**: `market_scheduler.py` 中 `SELL_STAGES = ("10:40",)`，只有 10:40 时段的 `run_backtest(emit_sell=True)` 会调用 `decide_exit(pct, mkt_temp)` 派卖单（生成 `pending_sell_confirm_<date>.json`，不自动平仓）；其余时段的 `run_backtest(emit_sell=False)` 只看板、绝不派卖单。10:40 **不在** `slot_status.json` 跟踪的 4 个 launchd 槽位内（它由 agent 侧 automation 驱动）→ 判断当日是否已跑时，改看是否已存在当日 `pro_report_*_1040.txt` / `pending_sell_confirm_<date>.json`。**漏跑 10:40 会导致所有持仓永远收不到卖出建议。**
- **Scheduling entry point (ADR-0030, 2026-09-22)**: the four slots are triggered by LaunchAgents `com.marketadvisor.slot{0925,1140,1340,1440}` (plists in `~/Library/LaunchAgents/`), which all call the single wrapper `scripts/run_slot.sh <slot>`. The wrapper forces a UTF-8 env (launchd locale may be C while the script prints CJK), unsets proxy vars, guards weekends and duplicate triggers within 5 minutes (`.slot_lock_<date>_<slot>`, bypass with `--force`), runs `market_scheduler.py <slot>`, converts the freshly written `pro_report_*.txt` to HTML via `txt_to_html.py`, retries once after 30 s if no report appeared, and records `reports/slot_status.json` + `reports/slot_runner.log`. Zero-side-effect check: `scripts/run_slot.sh --selftest`.
- **Do NOT schedule these slots via WorkBuddy automations**: the WorkBuddy `LocalAutomationScheduler` only lives inside the desktop app process, so any slot it owns simply does not fire while the app is closed, and catch-up runs land hours late with a real (stale) timestamp. Only the 15:30 close-of-day recap stays on WorkBuddy, because it needs agent-side analysis and `present_files`.
- **Report Path**: Daily complete reports MUST be saved to `/Users/williamli/Work/reports/` per user preference. A compatibility double-saving mechanism is implemented to copy files to `/Users/williamli/Work/Python_Project/.trae/market_reports/[date]/` for working directory support.

## Workflow & Sentiment Analysis
The core of the strategy relies on quantitative sentiment indicators (游资情绪量化):
1. **Market Base**: Indices (Akshare) and Market Temperature/Rise-Fall counts (Xuangubao).
2. **Limit-up Ecosystem**: Limit Up/Down Counts, Broken Ratio (炸板率), and Yesterday's Premium (昨日涨停溢价) via Xuangubao.
3. **Space Dragon & Sector Logic**: Extracting the highest consecutive limit-up stock (`limit_up_days`) and top limit-up reasons (`surge_reason` -> `related_plates`) from Xuangubao's limit-up pool.
4. **Actionable Advice (胜率验证策略)**: Auto-generating specific position sizing (仓位控制), attack directions (进攻方向), defense warnings (防守警示), and next-day verification targets (验证指标).

## Advanced Trading System Features
Following extensive experiential refinements and user steering, the system features five advanced mechanisms:
1. **50w Account Capital Management & Dynamic Closed-Loop Backtest**:
   - Starting with a fixed **500,000 RMB starting capital**. Tracks real-time **Available Cash** vs **Position Value** vs **Net Asset Value (NAV)** curve with standard A-share T+1 settlement constraints.
   - Executes simulated buys (100k per trade, rounded to nearest 100 shares) at 09:25 (open price) and 14:40 (current price) under cash availability bottlenecks.
   - Midday and holding management sessions (`11:40` / `13:40`) automatically evaluate active positions, checking real-time Sina prices, and executing a **dynamic exit decision matrix** at 13:40:
     - *Price Change >= 9.5%*: 【锁仓晋级】 Keep holding.
     - *Price Change >= 4.0% + Temp >= 50*: 【持股锁仓 / 可小幅加仓】 Bullish continuation.
     - *Price Change >= 4.0% + Temp < 50*: 【冲高全清 / 锁定局利润】 Protect profit.
     - *Flat / Slight drop*: 【冲高卖出一半，其余清仓】 Defensive reduction. (Selling 50% shares and returning cash).
     - *Price Change < -5.0% or Temp < 40*: 【果断清仓止损 / 坚决卖出】 Unconditional stop-loss.
2. **Liquidity Trap Filters (一字板过滤) & Transaction Friction**:
   - **一字涨停 (Limit-up open, High == Low)**: At 09:25, skips purchasing because no real trading volume is available for momentum players.
   - **一字跌停 (Limit-down open, High == Low)**: At exit evaluation, force-holds the stock and flags it as a liquidity lock, preventing unrealistic early flat stop-losses.
   - **Friction**: Injects realistic transaction costs: **0.03% buy commission**, **0.03% sell commission + 0.05% sell stamp duty**, and **0.1% execution slippage** on both entry and exit.
3. **24H AI Forward-Looking Intelligence Scanner (旁系分支情报)**:
   - Evaluates a representative industry-leading basket of 13 sector leaders (Moutai, CATL, Seres, BYD, Foxconn, iFlytek, etc.).
   - Background scraper `intelligence_crawler.py` queries Eastmoney research reports and utilizes **Gemini-2.5-flash** via local socks5 proxy to extract forward-looking catalysts, logic, and watchlist stock candidates.
   - Unified dashboard automatically displays these concepts right above market indices for holistic macro-sentiment integration.
4. **Double-Engine 09:25 Call Auction & Capital Inflow Review**:
   - To prevent time-shifting artifacts and "following large-cap traps" (which trade huge volumes but fade/close down), the 09:25 block operates under dual logic:
     - *Real-Time 09:25 Run*: Identifies gapping-up plays with auction turnover `> 1500万` and free-float `> 30亿`.
     - *Intraday / After-Hours Run*: Ranks daily capital inflow leaders (>= 5亿). Computes the total limit-up vs broken-board ratio (主力突围率) of the top 10, issuing strict warnings if heavy stocks face profit-taking (e.g. limit up counts <= 4).
5. **Asymmetry of Holding Days Analysis**:
   - Tracks and compares average holding days for winning trades vs losing trades. Fires alarms if there is a tendency to "hold losing bags" (avg loss days > avg win days * 1.5).
6. **macOS GUI Auto-Open**:
   - To overcome small macOS notification alert constraints, the script triggers `open -a TextEdit [report_path]` to automatically pop up the report in TextEdit.
7. **Intraday Actionable Decision Flashcard (盘中决策闪卡) & Top 10 Capital Inflow**:
   - 操作提示与后台做账彻底剥离：闪卡只显示买卖指令，不显示持仓/余额。
   - **弹窗时机**：`09:25`(早盘·卖) 与 `14:40`(尾盘·买) 弹「操作闪卡」(`build_ops_flashcard`，极简：`卖 X(sh603137) 800股` / `买 X(sh600100) 4500股` / 情绪弱则整卡一行`不操作`) + 系统通知(强提醒)；`11:40`(午盘) 弹「账户梳理」(`build_account_review`：可用资金/持仓市值/总权益/今日已实现盈亏/当前浮盈 + 今日卖出清单 + 当前持仓) + 系统通知；`10:40`/`13:40` **静默弹完整市场报告**(`pro_report_*`，仅 TextEdit 弹窗、不发系统通知，区别于操作闪卡的强提醒)。
   - 早盘(09:25)以卖为主、受 `MORNING_GATE_TEMP=45` 情绪门控(低于则整卡"早盘不操作")；尾盘(14:40)只买不卖；二者严格互不出现。
   - 早盘"排单打板"作为可选项恢复：仅当 `mkt_temp>=MORNING_BOARD_TEMP_GATE(60)` 且 `is_board_strong()` 判定打板梯队强势(连板高度>=3、炸板率<25%、涨停>=30、龙头非极弱、昨曾溢价>0)时才出现，只取竞价最强1只、仓位同尾盘(可用×15%)；否则早盘不出现任何买入指令。
   - Integrates real-time **Top 10 Main Capital Net Inflows (主力资金净流入前十)** directly from Eastmoney (bypassing system proxies).
8. **Real-Time News Catalyst Monitoring & Institutional Analysis Engine**:
   - Foreground/background daemon (`news_monitor_daemon.py`) polls Sina Finance Roll News API (`feed.mix.sina.com.cn`) every 60s. Filters headings/intros for high-impact catalyst keywords (`断供`, `大涨`, `价格大涨`, `停产`, `国产替代`, etc.) to trigger analysis.
   - Analysis engine (`news_intelligence_analyst.py`) extracts candidate stock lists from AI knowledge, fetches live institutional survey statistics (`ak.stock_jgdy_tj_em`) and latest research reports (`ak.stock_research_report_em`), and leverages Gemini-2.5-flash to output next-day buying guides automatically opened in TextEdit.

## Scripts and Architecture
- `scripts/market_scheduler.py`: Main scheduling script. Fetches market indices and sentiment, updates mock positions, executes exits and buys under capital constraints, and prints the unified high-density report.
  - **操作指令弹窗(与做账剥离)**: `build_action_items(stage)` 只读派生当前时段指令——09:25只卖(受情绪门控)、14:40只买；`_fmt_action` 输出极简交易单(买/卖 X 代码 N股)。`build_ops_flashcard` 生成极简弹窗；`build_account_review` 在11:40做后台账目梳理(可用/持仓市值/盈亏/今日卖出/当前持仓)。原详细报告仍静默归档。
  - **接管账户(订单派发)**: 报告末尾调用 `build_order_plan()` + `dispatch_orders()`，把订单计划写出为 `reports/order_plan_<stage>.json`，并（默认 DRY_RUN）尝试交给 `broker_easytrader.py` 执行。**默认绝不真实下单**。
- `scripts/broker_easytrader.py`: 券商接管执行器（**在 Windows 端运行**）。通过 easytrader 驱动同花顺/通达信/东方财富客户端，消费 `order_plan_*.json` 真正下发委托。受 `broker_config.json` 控制：`live=false` 仅记录；`kill_switch=true` 拒绝一切下单；Mac 端因依赖不可用自动降级 DRY_RUN。
- `scripts/broker_config.json`: 接管账户配置（券商类型、客户端路径、单笔/总仓位上限、live、kill_switch）。
- `scripts/intelligence_crawler.py`: Background intelligence scraping script. Gathers institutional research report metadata across A-share leaders and employs Google GenAI (Gemini) to output catalyst briefs.
- `scripts/monitor_market.py`: Legacy alert wrapper.

## Pitfalls & Best Practices
- **Late Catch-Up Runs of a Slot Automation (CRITICAL, 2026-09-23)**: A slot automation may fire hours late (the desktop app catch-up path documented in ADR-0030). Before re-running `market_scheduler.py <slot>`, ALWAYS check `reports/slot_status.json` — if that slot already has `exit_code: 0` for today (produced by the launchd `run_slot.sh` wrapper), **reuse the existing `pro_report_*.txt/.html` and do NOT re-run**. Two reasons: (a) the `09:25` slot forces `is_live = True` unconditionally, so a post-close run fabricates an "auction" report out of closing data; (b) a re-run overwrites `reports/order_plan_<slot>.json` and `reports/strategy_state.json`, destroying the day's state chain that later slots already consumed. Re-running is only correct when the slot has genuinely not run today (or with an explicit user request).
- **Turnover Amount Unit Correction (CRITICAL)**: Xuangubao's `non_restricted_capital` is in Yuan, and `turnover_ratio` is a fraction. Daily turnover MUST be calculated as `(non_restricted_capital * turnover_ratio) / 100000000` (in 亿 Yuan). Do NOT multiply by price again, as non_restricted_capital is already capitalization, not shares.
- **Akshare Network Fragility**: Always wrap `akshare` and `requests` calls in a `robust_call` function with at least 3 retries and sleep delays to prevent silent cronjob failures.
- **Akshare Stderr Pollution**: Block `sys.stderr` during execution to prevent `tqdm` progress bars from polluting cronjob logs.
- **WAF Blocks on Financial Media (CRITICAL)**: Direct curling or polling of Cailian Press (财联社) node APIs is blocked by CloudWAF (`访问被拦截`). Use Sina Finance Roll News API (`https://feed.mix.sina.com.cn/api/roll/get`) instead, which is stable, standard JSON, and WAF-free.
- **Proxy Management for Heterogeneous APIs (CRITICAL)**: Always clear environmental proxy variables (`HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` etc.) before calling Akshare, Eastmoney, or Sina APIs to prevent proxy disconnection errors. Inject the socks5/http proxy (`http://127.0.0.1:7890`) specifically and only when initializing the Google GenAI client for Gemini access.
- **Limit-up/down Count 口径 (CRITICAL, 2026-11-26)**: 涨跌停家数有两个口径：① akshare 股池实数（`zt_count` / `dt_count`，即复盘闸门 `stage_signals` 的取值口径）；② breadth 估算（按全市场跌家扫描推的，如 11-25 估算 177 vs 股池实数 43）。**recap 摘要与闸门对表、以及任何阈值规则（如「前日跌停 ≥N → 降级」）必须统一用股池实数口径并标注来源**，禁止把 breadth 估算值当「跌停家数」引用。另：`zt_pool` / `zb_pool` 是展示用截断版（[:60]/[:40]），做统计（引擎/闸门/行业分布）一律用 `zt_pool_all` / `zb_pool_all` / `*_count`，否则涨停家数少算、炸板率虚高、闸门判段失真（11-26 实测 60 vs 65）。

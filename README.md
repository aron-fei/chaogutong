# A股市场监控台

通达信风格的实时盯盘看板，本地 Flask 服务。

## 功能

- **指数快照**：上证/深成/创业板/科创50，5s 刷新（腾讯批量接口）
- **板块行情**：同花顺行业板块，12s 刷新；含 1分钟/5分钟 涨速排名 ⚡
- **涨停/跌停/炸板池**：东财，20s 刷新；连板天梯自动分组
- **个股异动雷达**：全A 抓取 + 涨速/涨幅/成交额 三 Tab，75s 刷新
- **市场宽度 + 情绪温度计**：实时 涨/跌/平 + 0-100 情绪评分
- 通达信暗色风格，涨红跌绿（中国市场惯例）

## 启动

```bash
cd /Users/williamli/Work/market-monitor
/opt/homebrew/anaconda3/bin/python3 app.py
```

打开浏览器访问 http://127.0.0.1:8765

## 关键文件

| 文件 | 职责 |
|---|---|
| `app.py` | Flask 服务入口 |
| `data_fetcher.py` | 后台抓取线程 + 板块涨速跟踪器 |
| `config.py` | 刷新间隔 / 监控指数 / 阈值 |
| `templates/index.html` | 看板骨架 |
| `static/style.css` | 暗色主题 |
| `static/app.js` | 前端轮询 + 渲染 |

## 数据源（均已实测可用）

| 数据 | 源 | 接口 | 频率 |
|---|---|---|---|
| 指数 | 腾讯 | qt.gtimg.cn (80ms) | 5s |
| 板块 | 同花顺 | `stock_board_industry_summary_ths()` | 12s |
| 涨停池 | 东财 push2ex | `stock_zt_pool_em()` 等 | 20s |
| 全A | 新浪 | `stock_zh_a_spot()` (70页分页, 45s) | 75s |

> 注：东财 push2 接口被本地代理阻断，已自动绕过。

## launchd 开机启动（可选）

存为 `~/Library/LaunchAgents/com.william.market-monitor.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.william.market-monitor</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/homebrew/anaconda3/bin/python3</string>
    <string>/Users/williamli/Work/market-monitor/app.py</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/market-monitor.log</string>
  <key>StandardErrorPath</key><string>/tmp/market-monitor.err</string>
</dict>
</plist>
```

加载/卸载：
```bash
launchctl load ~/Library/LaunchAgents/com.william.market-monitor.plist
launchctl unload ~/Library/LaunchAgents/com.william.market-monitor.plist
```

## 停止运行中的服务

```bash
launchctl unload ~/Library/LaunchAgents/com.william.market-monitor.plist
# 或
pkill -f "market-monitor/app.py"
```

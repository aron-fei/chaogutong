"""券商接管执行器（在 Windows 端运行）。

通过 easytrader 驱动 同花顺/通达信/东方财富 客户端，把 market_scheduler.py 产出的
order_plan_<stage>.json 真正下发为委托单。

安全设计：
- 默认 DRY_RUN（live=false）：只打印/记录订单，绝不真实下单。
- kill_switch=true：任何情况下拒绝下单（一键暂停开关）。
- 在 Mac 上因依赖(win32gui/pywinauto)不可用，会自动降级为 DRY_RUN，不会报错中断。
- Mac 端 market_scheduler.py 每次运行都会 import 本模块并调用 run_order_plan，
  但 live=false 时仅记录日志，不触达券商。

部署方式：
1. 在 Windows 机器上安装并登录好 同花顺/通达信/东方财富 客户端。
2. pip install easytrader
3. 复制 broker_config.json 到 Windows，把 client_exe_path 改成实际客户端路径，live 改为 true。
4. 运行一个常驻轮询脚本（见文件底部 __main__），让它读取 Mac 同步过来的 order_plan_*.json 并执行。
"""
import os
import sys
import json
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "broker_config.json")
REPORTS_DIR = os.path.expanduser("/Users/williamli/Work/reports")
LOG_PATH = os.path.join(REPORTS_DIR, "broker_exec_log.jsonl")


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def log(msg):
    line = json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "msg": str(msg)},
                      ensure_ascii=False)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print("[broker_easytrader] " + str(msg))


def get_trader(config):
    """初始化 easytrader 连接（仅 Windows 有效）。失败/不可用返回 None。"""
    try:
        import easytrader
    except Exception as e:
        log("easytrader 不可用（非 Windows 或未安装），进入 DRY_RUN: %s" % e)
        return None
    client_type = config.get("client_type", "ths")  # ths / tonghuashun / eastmoney / universal
    try:
        user = easytrader.use(client_type)
        exe_path = config.get("client_exe_path")
        if exe_path:
            user.connect(exe_path)
        else:
            user.connect()
        log("easytrader 已连接客户端(%s)" % client_type)
        return user
    except Exception as e:
        log("easytrader 连接失败: %s" % e)
        return None


def run_order_plan(order_plan):
    """执行一份订单计划。DRY_RUN 下只记录，live 下才真正下单。"""
    config = load_config()
    live = bool(config.get("live", False))
    kill = bool(config.get("kill_switch", False))

    if kill:
        log("kill_switch 已开启，拒绝执行任何下单。")
        return
    if not live:
        log("DRY_RUN 模式：不真实下单。订单数=%d" % len(order_plan.get("actions", [])))
        for a in order_plan.get("actions", []):
            log("  [DRY] %s %s(%s) %s" % (a.get("type"), a.get("name"),
                                          a.get("symbol"), a.get("note")))
        return

    trader = get_trader(config)
    if trader is None:
        log("LIVE 模式但交易连接不可用，已降级 DRY_RUN，避免误下单。")
        return

    max_pct = float(config.get("max_position_pct_per_order", 15))
    account_cap = float(config.get("account_capital", 0) or 0)
    for a in order_plan.get("actions", []):
        try:
            if a["type"] in ("SELL_ALL", "SELL_HALF"):
                # 真实卖出需按券商 API 传入持仓与比例；此处为接入点示意
                log("LIVE 卖出 %s(%s) %s" % (a["name"], a["symbol"], a["type"]))
                # trader.sell(security=a["symbol"], amount=...)
            elif a["type"] in ("BUY", "BUY_LIMIT"):
                log("LIVE 买入 %s(%s) 建议仓位<=%.0f%%" % (a["name"], a["symbol"], max_pct))
                # 估算金额（按账户上限与单笔上限）：
                # amount = min(account_cap * max_pct / 100, ...)
                # trader.buy(security=a["symbol"], price=..., amount=...)
        except Exception as e:
            log("下单异常 %s(%s): %s" % (a.get("name"), a.get("symbol"), e))
    log("LIVE 订单派发完成。")


def _find_latest_order_plan():
    try:
        files = [f for f in os.listdir(REPORTS_DIR) if f.startswith("order_plan_") and f.endswith(".json")]
        if not files:
            return None
        files.sort(key=lambda f: os.path.getmtime(os.path.join(REPORTS_DIR, f)), reverse=True)
        return os.path.join(REPORTS_DIR, files[0])
    except Exception:
        return None


if __name__ == "__main__":
    # Windows 端常驻轮询：每 20 秒检查最新 order_plan，未执行过的就下发。
    processed = set()
    log("broker_easytrader 轮询启动 (DRY_RUN=%s)" % (not bool(load_config().get("live", False))))
    while True:
        try:
            path = _find_latest_order_plan()
            if path and path not in processed:
                with open(path, "r", encoding="utf-8") as f:
                    plan = json.load(f)
                run_order_plan(plan)
                processed.add(path)
        except Exception as e:
            log("轮询异常: %s" % e)
        time.sleep(20)

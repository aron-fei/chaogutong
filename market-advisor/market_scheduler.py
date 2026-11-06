import akshare as ak
import pandas as pd
import requests
import sys
import os
import warnings
import time
import json
import re
import glob
from datetime import datetime, timedelta
import ssl
import urllib.request
import subprocess
import threading
from collections import Counter, defaultdict

# 微信推送异步线程收集：Mac 弹窗优先（同步），微信推送异步（不阻塞主流程时效）
_WECHAT_THREADS = []

warnings.filterwarnings('ignore')

# 目录与日志路径设定（跨机可移植：环境变量可覆盖，默认指向本机实际路径）
REPORTS_DIR = os.path.expanduser(os.environ.get("REPORTS_DIR", "~/Work/reports"))
# 工作台(market-monitor)仓库根；其它机器通过 MARKET_MONITOR_DIR 覆盖
MARKET_MONITOR_DIR = os.environ.get("MARKET_MONITOR_DIR", "/Users/williamli/Work/market-monitor")
os.makedirs(REPORTS_DIR, exist_ok=True)
STATE_FILE = os.path.join(REPORTS_DIR, 'strategy_state.json')
INTRADAY_LOG_FILE = os.path.join(REPORTS_DIR, 'intraday_log.jsonl')
TREND_FILE = os.path.join(REPORTS_DIR, 'trend_research_brief.json')
TREND_MAX_AGE_HOURS = 20
INTEL_FILE = os.path.join(REPORTS_DIR, 'market_intel_briefs.json')
# 情报刷新间隔（分钟）：交易日各弹窗时段最小间隔为60分钟，故每次生成报告都会触发刷新，
# 保证“实时资讯”始终是当天最新最热的数据；30分钟内重复生成（如手动重跑）则跳过以免重复调用API。
INTEL_MAX_AGE_MINUTES = 30
# ===== 统一操作池 (Unified Operation Pool) 闭环配置 =====
OPERATION_POOL_FILE = os.path.join(REPORTS_DIR, 'operation_pool.json')
POOL_TOP_N = 3              # 每日从池子排序取 Top N 作为买入候选
POOL_MAX_NEW_BOARD = 5      # 新建仓连板股最高连板数(>5需≥2模块共识才买，防盲目追最高板)
POOL_DROP_GAP_DAYS = 7      # 连续 N 天未出现的跟踪标的移出池子(持仓中除外)
POOL_HARD_CAP = 300          # 池子硬上限(最后一道闸)：防止多年累积无界膨胀；持仓(held)永不裁剪
SIGNAL_DECAY = 0.6           # 信号日衰减系数：今天未被重新探测到的 tracking 票 signal ×0.6，防旧高分永久滞留

def robust_call(func, *args, max_retries=3, **kwargs):
    for i in range(max_retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if i == max_retries - 1:
                raise e
            time.sleep(1)

def robust_request_json(url, headers=None, timeout=5, max_retries=3):
    for i in range(max_retries):
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            if r.status_code == 200:
                return r.json()
        except Exception as e:
            if i == max_retries - 1:
                pass
            else:
                time.sleep(1)
    return {}

def wait_for_network(url="https://www.baidu.com", timeout=3, max_attempts=15):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 检测网络连通性...")
    for i in range(max_attempts):
        try:
            import urllib.request
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=timeout) as response:
                if response.status == 200:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] 网络已连通，连接国内API就绪！")
                    return True
        except Exception as e:
            # 如果是 HTTP 错误（例如 404/403/500），说明物理网络已就绪，只是返回的状态码不同
            if hasattr(e, 'code') or "HTTP Error" in str(e):
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 网络已连通 (物理连通已验证)，连接国内API就绪！")
                return True
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 网络未就绪或正在建立连接 (第 {i+1}/{max_attempts} 次尝试): {e}")
            time.sleep(2)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 网络检测超时，继续执行。")
    return False

def send_mac_notification(title, message):
    try:
        safe_message = str(message).replace('"', '\\"').replace("'", "\\'")
        safe_title = str(title).replace('"', '\\"').replace("'", "\\'")
        cmd = ['osascript', '-e', f'tell app "System Events" to display alert "{safe_title}" message "{safe_message}" giving up after 15']
        import subprocess
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"[WARN] 静默异常已显式化 @90: {e}")

def get_pushplus_token():
    """读取 PushPlus token：优先环境变量 PUSHPLUS_TOKEN，回退脚本同目录 pushplus_token.txt 首行。"""
    t = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    if t:
        return t
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pushplus_token.txt")
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        return line
    except Exception as e:
        print(f"[WARN] 静默异常已显式化 @106: {e}")
    return ""

def send_wechat_pushplus(title, content, template="txt"):
    """通过 PushPlus 将消息推送到用户个人微信(服务号)。失败静默跳过，不影响主流程。
    受 WECHAT_PUSH_ENABLED 开关控制(默认关闭)；设为 1/true/yes 启用。"""
    if os.getenv("WECHAT_PUSH_ENABLED", "false").strip().lower() not in {"1", "true", "yes", "y"}:
        print("[pushplus] 微信推送已关闭(WECHAT_PUSH_ENABLED=false)，跳过")
        return False
    token = get_pushplus_token()
    if not token:
        print("[pushplus] 未配置 token，跳过微信推送")
        return False
    try:
        resp = requests.post(
            "https://www.pushplus.plus/send",
            json={"token": token, "title": title, "content": content, "template": template},
            timeout=8
        )
        try:
            data = resp.json()
            if data.get("code") == 200:
                print("[pushplus] 微信推送成功")
                return True
            else:
                print(f"[pushplus] 推送返回异常: {data.get('msg')}")
        except Exception:
            print(f"[pushplus] 推送响应解析失败: {resp.status_code}")
    except Exception as e:
        print(f"[pushplus] 推送请求失败: {e}")
    return False

def open_report_in_textedit(filepath, title=None):
    """弹出报告/操作指令窗口。

    历史坑：原实现用 `open -a TextEdit file`。从 LaunchAgent 后台会话调用时，
    TextEdit 进程会被拉起但文档打不开(窗口空白/无窗口)，且不抢前台焦点，
    表现为"定时任务跑了、报告也生成了，但屏幕上什么都没弹"。
    改为优先用同会话内的 Tkinter 窗口(popup_gui.py)，不依赖任何 macOS 隐私权限；
    仅在 Tk 不可用时回退 TextEdit。

    【2026-11-05 弹窗收敛 · 用户授权「剩余项都可以做」】用户明确厌恶误导性
    通知/弹窗，且工作台已有语音播报 + 操作确认台 + 微信推送。默认改为**静默**：
    只写 intraday_alert_*.txt 并走微信推送，不再拉起 Tk / TextEdit。
    三档由环境变量 ADVISOR_POPUP_MODE 控制：
      silent（默认，静默） / voice（macOS say 短句） / popup（旧行为，一键回滚）
    """
    mode = os.environ.get("ADVISOR_POPUP_MODE", "silent").strip().lower()
    if mode == "voice":
        try:
            short = f"{title or '时段报告'} 已生成，请到工作台查看"
            subprocess.Popen(["say", "-v", "Ting-Ting", short],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"[Popup] 语音提示: {short}")
        except Exception as e:
            print(f"[Popup] 语音播报失败（不阻塞主流程）: {e}")
        return True
    if mode != "popup":
        print(f"[Popup] 静默模式（ADVISOR_POPUP_MODE={mode}）：仅写文件 + 微信推送，"
              f"不弹窗 —— {os.path.basename(filepath)}")
        return True
    try:
        popup = os.path.join(os.path.dirname(os.path.abspath(__file__)), "popup_gui.py")
        if os.path.exists(popup):
            args = [sys.executable, popup, filepath]
            if title:
                args.append(title)
            p = subprocess.Popen(
                args, start_new_session=True,
                stdout=open(os.path.join(REPORTS_DIR, "popup_gui.log"), "a"),
                stderr=subprocess.STDOUT,
            )
            time.sleep(1.2)                     # 给窗口初始化时间
            if p.poll() is None:                # 仍存活 = 窗口已弹出
                print(f"[Popup] 已弹出窗口: {os.path.basename(filepath)}")
                return True
            print("[Popup] Tk 弹窗进程秒退，回退 TextEdit。")
        else:
            print("[Popup] popup_gui.py 不存在，回退 TextEdit。")
    except Exception as e:
        print(f"[Popup] Tk 弹窗失败，回退 TextEdit: {e}")
    try:
        subprocess.run(['open', '-a', 'TextEdit', filepath],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        print(f"[Popup] TextEdit 回退也失败: {e}")
        return False

def _trend_last_updated(data):
    text = data.get('last_updated', '')
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None

def refresh_trend_research_if_stale(max_age_hours=TREND_MAX_AGE_HOURS):
    try:
        stale = True
        if os.path.exists(TREND_FILE):
            with open(TREND_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            last_dt = _trend_last_updated(data)
            has_valid_rows = bool(data.get("top") or data.get("all"))
            if has_valid_rows and last_dt and datetime.now() - last_dt <= timedelta(hours=max_age_hours):
                stale = False

        if not stale:
            return False, "fresh"

        script_path = os.path.join(os.path.dirname(__file__), "trend_research.py")
        result = subprocess.run(
            [sys.executable, script_path],
            cwd=os.path.dirname(script_path),
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode == 0:
            print(f"[Trend Research] refreshed stale trend file via {script_path}")
            return True, "refreshed"
        return False, (result.stderr or result.stdout or f"exit={result.returncode}")[-500:]
    except Exception as e:
        return False, str(e)

def is_suspended(name):
    """判断个股是否停牌。
    新浪接口特征：
      - current_price==0：典型停牌/未开盘
      - 开盘/最高/最低/成交量/买卖一档全为 0，但昨收作为最新价：典型全天停牌
        （如爱丽家居 603221 停牌时 open=high=low=bid1=ask1=volume=0, current=yest_close）
    尾盘时段调用时未开盘已不可能，故直接按停牌过滤。"""
    if not name:
        return True
    info = get_stock_detailed_sina(name)
    if not info:
        # 取不到行情时保守处理：不跳过（避免误杀），但打印日志
        print(f"[停牌过滤] 无法获取 {name} 行情，暂不判定为停牌")
        return False
    cur = info.get("current_price", 0.0)
    if cur == 0.0:
        print(f"[停牌过滤] {name}({info.get('symbol','')}) current_price=0，判定为停牌/未开盘，跳过")
        return True
    # 全天停牌：开盘、最高、最低、成交量、买卖一档均为 0，最新价=昨收
    open_p = info.get("open_price", 0.0)
    high = info.get("high", 0.0)
    low = info.get("low", 0.0)
    volume = info.get("volume", 0)
    bid1 = info.get("bid1", 0.0)
    ask1 = info.get("ask1", 0.0)
    yest = info.get("yesterday_close", 0.0)
    no_trade = (open_p == 0.0 and high == 0.0 and low == 0.0 and
                volume == 0 and bid1 == 0.0 and ask1 == 0.0 and
                abs(cur - yest) < 1e-6)
    if no_trade:
        print(f"[停牌过滤] {name}({info.get('symbol','')}) 全天无交易(昨收={yest})，判定为停牌，跳过")
        return True
    return False


def get_akshare_macro():
    try:
        indices = robust_call(ak.stock_zh_index_spot_sina)
        sh = indices[indices['名称'] == '上证指数'].iloc[0]
        sz = indices[indices['名称'] == '深证成指'].iloc[0]
        cy = indices[indices['名称'] == '创业板指'].iloc[0]
        
        total_vol = (sh['成交额'] + sz['成交额']) / 100000000
        return sh['涨跌幅'], sz['涨跌幅'], cy['涨跌幅'], total_vol
    except Exception as e:
        return 0.0, 0.0, 0.0, 0.0

def _extract_plates(item):
    sr = item.get("surge_reason") or {}
    pls = sr.get("related_plates") or []
    out = []
    for p in pls:
        name = p.get("plate_name")
        if name:
            out.append(name)
        else:
            out.append(str(p.get("plate_id")))
    return out or ["其他"]

def fetch_xuangutong_data():
    base = "https://flash-api.xuangubao.com.cn/api/"
    headers = {"User-Agent": "Mozilla/5.0"}
    
    def get_latest_line(endpoint):
        try:
            r = robust_request_json(base + endpoint, headers=headers, timeout=5)
            return r['data'][-1] if r.get('data') else {}
        except:
            return {}

    temp = get_latest_line("market_indicator/line?fields=market_temperature").get('market_temperature', 0)
    counts = get_latest_line("market_indicator/line?fields=rise_count,fall_count")
    limits = get_latest_line("market_indicator/line?fields=limit_up_count,limit_down_count")
    premium = get_latest_line("market_indicator/line?fields=yesterday_limit_up_avg_pcp").get('yesterday_limit_up_avg_pcp', 0) * 100
    broken = get_latest_line("market_indicator/line?fields=limit_up_broken_count,limit_up_broken_ratio")
    
    try:
        r = robust_request_json(base + "plate/incdec?move_type=1&limit=5", headers=headers, timeout=5)
        hot_sectors_raw = r['data']['items'] if r.get('data') else []
    except:
        hot_sectors_raw = []
        
    try:
        r = robust_request_json(base + "pool/detail?pool_name=limit_up", headers=headers, timeout=5)
        limit_up_pool = r['data'] if r.get('data') else []
    except:
        limit_up_pool = []

    try:
        r = robust_request_json(base + "pool/detail?pool_name=super_stock", headers=headers, timeout=5)
        super_stock_pool = r['data'] if r.get('data') else []
    except:
        super_stock_pool = []
        
    return {
        "temperature": temp,
        "rise_count": counts.get('rise_count', 0),
        "fall_count": counts.get('fall_count', 0),
        "limit_up": limits.get('limit_up_count', 0),
        "limit_down": limits.get('limit_down_count', 0),
        "premium": premium,
        "broken_ratio": broken.get('limit_up_broken_ratio', 0) * 100,
        # 【2026-10-21 修 P0-1】带出炸板**家数**，供上层判定 data_ready。
        # 只有 ratio 无法区分「真 0%」与「早盘除零产物」——两者 ratio 都是 0.0，
        # 但前者是数据成熟期的真实值，后者是分母为 0 的垃圾值。
        "broken_count": broken.get('limit_up_broken_count', None),
        "hot_sectors_raw": hot_sectors_raw,
        "limit_up_pool": limit_up_pool,
        "super_stock_pool": super_stock_pool
    }

def get_concept_pct(concept_names):
    """获取指定板块当前的涨跌幅"""
    try:
        df = robust_call(ak.stock_board_concept_name_em)
        results = {}
        for name in concept_names:
            match = df[df['板块名称'] == name]
            if not match.empty:
                results[name] = match.iloc[0]['涨跌幅']
        return results
    except:
        return {}

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"[WARN] 静默异常已显式化 @336: {e}")
    return None

def save_state(dragons, top_leaders, sectors, date_str, first_board_stars=None, force=False):
    """保存跨日基准状态。

    【护栏】2026-10-08 新增：空数据集（无龙 / 无主线 / 无板块龙头 / 无首板新星）一律
    拒绝覆盖 —— 盘前空快照曾把 09-30 的跨日基准抹成 "今日空状态"，导致当日正点运行的
    跨日追踪整段丢失。宁可保留上一交易日的旧基准（陈旧但可用），也不写入空错基准。
    """
    if not force and not any([dragons, top_leaders, sectors, first_board_stars]):
        print("[state] 空数据集(无龙/无主线/无板块龙头/无首板新星) → 拒绝覆盖 strategy_state.json，"
              "保留上一交易日基准（防盘前空快照抹掉跨日基准）")
        return
    data = {
        "date": date_str,
        "dragons": dragons,
        "top_leaders": top_leaders,
        "sectors": sectors,
        "first_board_stars": first_board_stars or []
    }
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        print(f"[WARN] 静默异常已显式化 @351: {e}")

# =====================================================================
# 统一操作池 (Unified Operation Pool)：汇总共线所有模块的候选股，
# 跨日跟踪连板进度，排序取 Top N 驱动 50万账户买卖，形成数据闭环+回测。
# 关键修复：取消「排除涨停股」限制，连板进阶股(如立新能源 1→6板)持续作为买点。
# =====================================================================
def load_operation_pool():
    if os.path.exists(OPERATION_POOL_FILE):
        try:
            with open(OPERATION_POOL_FILE, 'r', encoding='utf-8') as f:
                return json.load(f).get("entries", {})
        except Exception as e:
            print(f"[WARN] 静默异常已显式化 @364: {e}")
    return {}

def save_operation_pool(entries):
    # 硬上限护栏：防止池子多年无界膨胀。正常靠 POOL_DROP_GAP_DAYS 收敛，
    # 但清理异常被吞/数据异常时仍可能增长，故在持久化前加最后一道闸。
    # 持仓(held)永不裁剪，只裁剪 tracking；按「新鲜度(最近出现)*信号」保留最强的一批。
    if len(entries) > POOL_HARD_CAP:
        held = {s: e for s, e in entries.items() if e.get("status") == "held"}
        track = {s: e for s, e in entries.items() if e.get("status") != "held"}
        def _keep_key(e):
            try:
                ls_d = datetime.strptime(e.get("last_seen", "1970-01-01"), '%Y-%m-%d')
            except Exception:
                ls_d = datetime(1970, 1, 1)
            fresh = (datetime.now() - ls_d).days
            return (max(0, 60 - fresh), float(e.get("signal", 0)))
        keep_n = max(0, POOL_HARD_CAP - len(held))
        keep = dict(sorted(track.items(), key=lambda kv: _keep_key(kv[1]), reverse=True)[:keep_n])
        entries = {**held, **keep}
        print(f"[operation_pool] 触发硬上限({POOL_HARD_CAP})：tracking {len(track)} -> {len(keep)}")
    try:
        with open(OPERATION_POOL_FILE, 'w', encoding='utf-8') as f:
            json.dump({"updated": datetime.now().strftime('%Y-%m-%d'),
                       "entries": entries}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[operation_pool] 保存失败: {e}")

def _resolve_symbol(name):
    """名称→代码(经新浪 suggest)。失败返回 None。"""
    try:
        sym, _ = get_stock_price_sina(name)
        return sym
    except:
        return None

def _score_turnover(turnover_亿):
    """首板突围成交额→信号分(0-100)。≈24亿→82，≥30亿→100。"""
    try:
        return min(float(turnover_亿) / 30.0 * 100.0, 100.0)
    except:
        return 50.0

def _merge_thesis(old, source, new):
    head = (old or "").split(" | ")
    sources_seen = [h.split(":", 1)[0] for h in head if ":" in h]
    if source in sources_seen:
        return old
    add = f"{source}:{new}" if new else source
    return (old + " | " + add).strip(" |") if old else add

def _entry_in_hot(entry, hot_themes):
    plates = entry.get("plates") or []
    return any(p in (hot_themes or []) for p in plates)

def build_operation_pool(contribs, prev_pool, today_str):
    """归集当日各模块候选，与历史池合并(更新连板进度/共识/信号)，返回 entries(dict)。
    2026-09-28：历史池先经 _canon_merge_pool 归一键并合并同股双条目。"""
    entries = _canon_merge_pool(prev_pool)
    for c in contribs:
        sym = _canon_symbol(c.get("symbol"))
        if not sym:
            continue
        if sym in entries:
            e = entries[sym]
            e["sources"] = sorted(set(e.get("sources", []) + [c["source"]]))
            e["signal"] = max(float(e.get("signal", 0)), float(c.get("signal", 0)))
            if c.get("board_days") is not None:
                e["board_days"] = c["board_days"]
            if c.get("last_pct") is not None:
                e["last_pct"] = c["last_pct"]
            if c.get("plates"):
                e["plates"] = sorted(set(e.get("plates", []) + c["plates"]))
            e["last_seen"] = today_str
            e["name"] = c.get("name") or e.get("name", "")
            e["thesis"] = _merge_thesis(e.get("thesis", ""), c["source"], c.get("thesis", ""))
        else:
            entries[sym] = {
                "symbol": sym,
                "name": c.get("name", ""),
                "sources": [c["source"]],
                "signal": float(c.get("signal", 0)),
                "board_days": c.get("board_days", 1),
                "first_seen": today_str,
                "last_seen": today_str,
                "last_pct": c.get("last_pct", 0),
                "plates": c.get("plates") or [],
                "thesis": c.get("thesis", ""),
                "status": "tracking"
            }
    # 信号衰减：今天未被任一共识源重新探测到的 tracking 票，signal 每日 ×SIGNAL_DECAY，
    # 防止旧高分永久滞留(此前 signal=max(old,new) 只增不减)。held 不衰减、今日新进/重探测的不衰减。
    for sym, e in entries.items():
        if e.get("status") == "held":
            continue
        if e.get("last_seen") != today_str:
            e["signal"] = round(float(e.get("signal", 0)) * SIGNAL_DECAY, 1)

    # 淘汰长期未出现的跟踪标的(持仓中除外)
    try:
        today_d = datetime.strptime(today_str, '%Y-%m-%d')
        for sym in [s for s in entries]:
            e = entries[sym]
            if e.get("status") == "held":
                continue
            ls = e.get("last_seen")
            if not ls:
                continue
            gap = (today_d - datetime.strptime(ls, '%Y-%m-%d')).days
            if gap > POOL_DROP_GAP_DAYS:
                del entries[sym]
    except Exception as e:
        print(f"[operation_pool] 清理过期标的异常(已跳过本次清理): {e}")
    return entries

# ---------------------------------------------------------------------------
# ADR-0036 L2：板块资金流（主力净流入）信号
#   数据源与工作台 board_rank.py 同源（腾讯 getRank），此处内联实现以保持
#   market_scheduler 自包含（不依赖工作台进程存活）。
#   接口限额实测：hy 最多 31 条（全量）、gn count 上限 200（count>=300 报 count too large）。
# ---------------------------------------------------------------------------
_SECTOR_FLOW_CACHE = {"ts": 0.0, "data": None}
_SECTOR_FLOW_TTL = 120.0     # 秒；同一进程内复用（不同时段是不同进程，实际每次报告 1 次请求）


def _sf_f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _sf_empty(err=""):
    return {"by_name": {}, "by_lead": {}, "top": [], "ts": 0.0, "err": err}


def fetch_sector_flow(force=False):
    """腾讯板块榜 → 主力净流入(亿元)索引。

    返回 {"by_name": {板块名: 净流入亿}, "by_lead": {6位代码: (板块名, 净流入亿)},
          "top": [(板块名, 净流入亿, 领涨股名, 领涨股代码)], "ts": epoch, "err": ""}
    任何异常 → 返回空结构（静默降级，绝不影响出报告）。
    """
    now = time.time()
    if (not force) and _SECTOR_FLOW_CACHE["data"] is not None \
            and (now - _SECTOR_FLOW_CACHE["ts"]) < _SECTOR_FLOW_TTL:
        return _SECTOR_FLOW_CACHE["data"]
    if not SECTOR_FLOW_ENABLED:
        return _sf_empty("disabled")
    url = "https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank"
    by_name, by_lead, top = {}, {}, []
    try:
        for btype, cnt in (("hy", 64), ("gn", 200)):
            r = requests.get(url, params={
                "board_type": btype, "sort_type": "speed",
                "direct": "down", "offset": 0, "count": cnt,
            }, timeout=SECTOR_FLOW_TIMEOUT)
            j = r.json()
            if j.get("code") != 0:
                raise RuntimeError("rank api code=%s msg=%s" % (j.get("code"), j.get("msg")))
            for x in ((j.get("data") or {}).get("rank_list") or []):
                nm = (x.get("name") or "").strip()
                if not nm:
                    continue
                nf = round(_sf_f(x.get("zljlr")) / 1e4, 2)      # 万元 → 亿元
                by_name[nm] = nf
                lzg = x.get("lzg") or {}
                lcode = _norm_code(lzg.get("code"))
                if lcode:
                    prev = by_lead.get(lcode)
                    if prev is None or nf > prev[1]:
                        by_lead[lcode] = (nm, nf)
                top.append((nm, nf, lzg.get("name") or "", lcode))
    except Exception as e:
        print(f"[WARN] 板块资金流获取失败(已降级为无信号): {type(e).__name__} {e}")
        d = _sf_empty(str(e))
        _SECTOR_FLOW_CACHE.update({"ts": now, "data": d})
        return d
    top.sort(key=lambda t: t[1], reverse=True)
    data = {"by_name": by_name, "by_lead": by_lead, "top": top[:SECTOR_FLOW_TOP_N],
            "ts": now, "err": ""}
    _SECTOR_FLOW_CACHE.update({"ts": now, "data": data})
    return data


def _sf_tier(netflow):
    """主力净流入(亿元) → 排序加分。低于最低门槛返回 0（不入选）。"""
    for thr, bonus in SECTOR_FLOW_TIERS:
        if netflow >= thr:
            return bonus
    return 0.0


_SF_STRIP = ("概念", "板块", "指数", "Ⅱ", "II", "(申万)", "（申万）")


def _sf_norm_text(s):
    s = str(s or "").strip()
    for t in _SF_STRIP:
        s = s.replace(t, "")
    return s.strip()


def _sf_text_hit(entry, board_name):
    """板块名与标的 plates/thesis 的双向包含匹配（归一化去后缀后）。"""
    bn = _sf_norm_text(board_name)
    if len(bn) < 2:
        return False
    cands = list(entry.get("plates") or [])
    if entry.get("thesis"):
        cands.append(entry["thesis"])
    for c in cands:
        cn = _sf_norm_text(c)
        if len(cn) < 2:
            continue
        if bn in cn or cn in bn:
            return True
    return False


def sector_flow_signal(entry, flow):
    """标的 → 板块资金流加分。

    优先级：① 标的是某净流入板块的【领涨股/龙头】(满分)
            ② 标的自报板块与净流入板块文本匹配(成分股，×0.7 折价)
    返回 (加分, 命中说明文字)；无命中返回 (0.0, "")。
    """
    if not flow or not flow.get("by_name"):
        return 0.0, ""
    best, why = 0.0, ""
    code = _norm_code(entry.get("symbol"))
    lead = (flow.get("by_lead") or {}).get(code)
    if lead:
        b = _sf_tier(lead[1])
        if b > best:
            best, why = b, "板块龙头·%s(净流入%+.2f亿)" % (lead[0], lead[1])
    for nm, nf in (flow.get("by_name") or {}).items():
        if nf <= 0:
            continue
        if _sf_text_hit(entry, nm):
            b = round(_sf_tier(nf) * 0.7, 2)
            if b > best:
                best, why = b, "所属板块·%s(净流入%+.2f亿)" % (nm, nf)
    return min(best, SECTOR_FLOW_BONUS_CAP), why


def _sf_hot_names(flow, limit=5):
    """净流入 Top 板块名（用于报告展示）。"""
    if not flow or not flow.get("top"):
        return []
    out = []
    for nm, nf, _ln, _lc in flow["top"]:
        if nf >= SECTOR_FLOW_TIERS[-1][0]:
            out.append("%s%+.2f亿" % (nm, nf))
        if len(out) >= limit:
            break
    return out


def rank_operation_pool(entries, xgb, db, stage, hot_themes, mkt_temp=50, board=None,
                       auction_ref_map=None):
    """排序打分，返回 Top N 买入候选(归一化 dict，含 name/symbol)。
    打分 = 共识度*15 + 信号*0.35 + 连板动量 + 板块热度 + 底部放量补偿 + 板块资金流(ADR-0036)。
    ADR-0036：新增板块主力净流入加分，并允许"命中净流入板块但不在白名单源内"的
    未涨停标的被有限放行（SECTOR_FLOW_MAX_ADMIT 只），落实"用资金流选票"的诉求。"""
    today_str = datetime.now().strftime('%Y-%m-%d')
    # 2026-09-28：db 持仓代码归一化匹配(池键已 canon 为 000066.SZ，db 可能存 000066)
    held_syms = {_norm_code(p.get("symbol")) for p in db.get("active_positions", [])}
    # ADR-0036：板块资金流信号（仅午后/尾盘接入；拉取失败自动为空 → 功能静默失效）
    _flow = {}
    if SECTOR_FLOW_ENABLED and stage in SECTOR_FLOW_STAGES:
        _flow = fetch_sector_flow()
    scored = []
    sf_wait = []        # 白名单未命中、但命中净流入板块的候选（受 SECTOR_FLOW_MAX_ADMIT 限额）
    # 【2026-10-21】竞价参考加权记录（symbol → (bonus, why)），供输出层留痕
    _ar_bonus_map, _ar_why_map = {}, {}
    for sym, e in entries.items():
        if e.get("status") == "closed":
            continue
        if _norm_code(sym) in held_syms:    # 已持有不重复买(归一化匹配)
            continue
        # 停牌股无法买入，直接排除（避免尾盘确认台出现爱丽家居这类停牌标的）
        if is_suspended(e.get("name", "")):
            continue
        # 当日新鲜度门槛：只有今天被重新探测到的票(last_seen==今日)才进买入候选。
        # 防止历史高分、但今日已转弱/大跌的旧票被反复推荐(尾盘数据错乱根因)。
        if e.get("last_seen") != today_str:
            continue
        sources = e.get("sources", [])
        consensus = min(len(sources), 3)
        signal = min(float(e.get("signal", 0)), 100)
        board_days = e.get("board_days", 1) or 1
        last_pct = e.get("last_pct", 0) or 0

        # ADR-0036：板块资金流信号（先算，白名单放行判定要用）
        sf_bonus, sf_why = sector_flow_signal(e, _flow)

        # 三时段独立池白名单：不同时段只从各自对应性质的源选股(见 STAGE_SOURCE_WHITELIST)。
        # 解决"统一操作池全是涨停票 → 早盘/午后/尾盘都弹同一只涨停票"的根因。
        # ADR-0036：命中净流入板块的票可"待定放行"(sf_wait)，最终按分数取前 SECTOR_FLOW_MAX_ADMIT 只。
        wl = STAGE_SOURCE_WHITELIST.get(stage)
        wl_ok = (wl is None) or bool(set(sources) & wl)
        if not wl_ok and not (sf_bonus > 0):
            continue

        # 13:40 午后只推「尚未涨停」的强势票(有涨停趋势但未封板) → 已封板的一律跳过
        if stage == "13:40" and _is_limit_up_pct(sym, e.get("name", ""), last_pct):
            continue

        # 尾盘(14:40)布局不推涨停票：涨停/连板几乎买不进，改用龙首阴等未涨停潜伏标的
        if stage == "14:40" and _is_limit_up_pct(sym, e.get("name", ""), last_pct):
            continue

        prog = 0.0
        if board_days >= 2 and last_pct >= 9.0:
            # ADR-0036：连板动量降权 20→PROG_BOARD_BONUS，且超过 PROG_BOARD_MAX_DAYS
            # 的高位连板不再加分（原口径把"第 5~6 板"和"第 2 板"同等奖励，属追高激励）。
            # 注：13:40/14:40 已在上面过滤涨停票 → 该分支只可能命中 09:25 打板池。
            prog = PROG_BOARD_BONUS if board_days <= PROG_BOARD_MAX_DAYS else 0.0
        elif board_days == 1:
            prog = PROG_BOARD_BOARD1

        # 风险护栏：高位连板且无多模块共识→跳过新建仓
        if board_days > POOL_MAX_NEW_BOARD and consensus < 2:
            continue

        sector_bonus = 5.0 if _entry_in_hot(e, hot_themes) else 0.0
        # ADR-0033：午后/尾盘给「底部放量」源加分。该源天然只有 1 个共识源
        # (consensus=1 → 15分)、board_days=1 (prog=5)，不补偿则永远被连板动量项
        # (consensus 2~3 + prog 20) 压过 —— 实测 77 分的底部放量票排不过 83~98 分的连板票，
        # 新通道会形同虚设，用户的"不追高"诉求无法落地。
        bs_bonus = (BOTTOM_SURGE_RANK_BONUS
                    if (stage in BOTTOM_SURGE_STAGES and BOTTOM_SURGE_SOURCE in sources) else 0.0)
        # ADR-0041：复盘重点源补偿。理由见文件头定标算术 —— 复盘 focus 天然只有
        # 1 个共识源(15 分)，不补偿则永远被多源共识票压过，"复盘决定买什么"落不了地。
        rv_bonus = (REVIEW_FOCUS_RANK_BONUS
                    if (stage in REVIEW_FOCUS_STAGES and REVIEW_FOCUS_SOURCE in sources) else 0.0)
        # 【2026-10-21】集合竞价参考加权（买入侧）：竞价强承接 → 加分；竞价虚高/无量 → 减分。
        # 只在 13:40/14:40 两个买入时段生效；09:25 打板通道不叠加（竞价本身就是它的选股依据）。
        # 幅度保守（±6 分，signal 量级 ~35），避免单因子主导排序；无参考时为 0，排序不变。
        ar_bonus, ar_why = 0.0, ""
        if _auction_ref is not None and stage in AUCTION_REF_BUY_STAGES:
            try:
                ar_bonus, ar_why = _auction_ref.auction_score_bonus(sym, ref_map=auction_ref_map)
            except Exception:
                ar_bonus, ar_why = 0.0, ""
        score = (consensus * 15.0 + signal * 0.35 + prog + sector_bonus + bs_bonus
                 + sf_bonus + rv_bonus + ar_bonus)   # ADR-0036 资金流 / ADR-0041 复盘重点
                 # 【2026-10-21】/ 竞价参考
        if mkt_temp < MORNING_GATE_TEMP:     # 弱市整体压低(决策层仍会拦)
            score *= 0.3
        _rec = (score, e, consensus, prog, sf_bonus, sf_why)
        if ar_bonus:
            _ar_bonus_map[e.get("symbol")] = ar_bonus
            _ar_why_map[e.get("symbol")] = ar_why
        (scored if wl_ok else sf_wait).append(_rec)

    # ADR-0036：资金流渠道额外放行 —— 未命中白名单源、但所属板块主力净流入的
    # 未涨停标的，取分数最高的前 SECTOR_FLOW_MAX_ADMIT 只并入候选（防灌水）。
    if sf_wait:
        sf_wait.sort(key=lambda t: t[0], reverse=True)
        _admit = [t for t in sf_wait if t[4] > 0][:SECTOR_FLOW_MAX_ADMIT]
        if _admit:
            print("[sector_flow] 资金流渠道放行 %d 只: %s" % (
                len(_admit), "、".join("%s(%s)" % (t[1].get("name", ""), t[5]) for t in _admit)))
        scored.extend(_admit)

    scored.sort(key=lambda t: t[0], reverse=True)
    out = []
    for score, e, consensus, prog, sf_bonus, sf_why in scored[:POOL_TOP_N]:
        out.append({
            "symbol": e["symbol"],
            "name": e.get("name", ""),
            "board_days": e.get("board_days", 1) or 1,
            "sources": e.get("sources", []),
            "signal": round(e.get("signal", 0), 1),
            "score": round(score, 1),
            "thesis": e.get("thesis", ""),
            "status": e.get("status", "tracking"),
            "sector_flow_bonus": round(sf_bonus, 1),
            "sector_flow_why": sf_why,
            # 【2026-10-21】竞价参考加权（可追溯：报告与复盘能核对这一分是怎么来的）
            "auction_bonus": round(_ar_bonus_map.get(e.get("symbol"), 0.0), 1),
            "auction_why": _ar_why_map.get(e.get("symbol"), ""),
        })
    return out

def _norm_code(s):
    """提取 6 位纯数字代码，兼容 000066 / 000066.SZ / sz000066 等键格式差异。"""
    return "".join(filter(str.isdigit, str(s or "")))

def _canon_symbol(sym):
    """操作池键归一化：6 位纯数字补交易所后缀(与 _em_code_to_symbol 规则一致)，
    已带后缀的原样保留(仅统一大小写)。
    修复(2026-09-28)：不同选股源对同股产出 000066 / 000066.SZ 两种键，导致池内同股
    双条目(共识分裂、held 计数虚增)。统一归一后同股可合并。"""
    s = str(sym or "").strip()
    if s.upper().endswith((".SS", ".SZ", ".BJ")):
        return s[:-3] + s[-3:].upper()
    c = _norm_code(s)
    if len(c) == 6 and c == s:
        return _em_code_to_symbol(c)
    return s

def _canon_merge_pool(entries):
    """按 _canon_symbol 归一化池键并合并同股条目。合并规则：
    sources 并集 / signal 取大 / board_days 取大 / last_seen 取新 / first_seen 取早 /
    plates 并集 / thesis 非空优先 / 状态非 tracking 优先(held/closed 不被 tracking 覆盖)。"""
    merged = {}
    for k, v in (entries or {}).items():
        ck = _canon_symbol(k)
        v = dict(v)
        v["symbol"] = ck
        if ck in merged:
            o = merged[ck]
            o["sources"] = sorted(set(o.get("sources", []) + v.get("sources", [])))
            o["signal"] = max(float(o.get("signal", 0)), float(v.get("signal", 0)))
            try:
                o["board_days"] = max(int(o.get("board_days") or 0), int(v.get("board_days") or 0)) or o.get("board_days")
            except (TypeError, ValueError):
                pass
            o["last_seen"] = max(o.get("last_seen") or "", v.get("last_seen") or "")
            o["first_seen"] = min(o.get("first_seen") or o.get("last_seen") or "",
                                  v.get("first_seen") or v.get("last_seen") or "") or o.get("first_seen")
            o["plates"] = sorted(set((o.get("plates") or []) + (v.get("plates") or [])))
            o["thesis"] = o.get("thesis") or v.get("thesis")
            if o.get("status", "tracking") == "tracking" and v.get("status") in ("held", "closed"):
                o["status"] = v["status"]
        else:
            merged[ck] = v
    return merged

def _reconcile_pool_status(entries, db):
    """与回测库对账：持仓→held，已平仓→closed，其余一律降级 tracking。
    修复(2026-09-28)：旧版对「既不在 active_positions 也不在 completed_trades」的
    旧 held/closed 条目不降级(elif 分支跳过)，导致幽灵持仓永久滞留
    (芒果超媒 300413.SZ 自 09-18 起连续 6 日 held)。现改为无条件对账，
    并以 6 位代码归一化匹配(兼容 000066 与 000066.SZ 两种键格式)。"""
    held = {_norm_code(p.get("symbol")) for p in db.get("active_positions", [])}
    closed = {_norm_code(t.get("symbol")) for t in db.get("completed_trades", []) if t.get("symbol")}
    held.discard("")
    closed.discard("")
    for sym, e in entries.items():
        n = _norm_code(sym)
        if n in held:
            e["status"] = "held"
        elif n in closed:
            e["status"] = "closed"
        else:
            e["status"] = "tracking"

def get_intel_candidates():
    """从情报文件提取题材龙头/中军观察个股，供操作池共识加权。返回 [{name, thesis}]。"""
    out = []
    if not os.path.exists(INTEL_FILE):
        return out
    try:
        with open(INTEL_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        for sec in data.get("sectors", []):
            for st in (sec.get("stocks") or []):
                name = st.strip()
                if name and name != "暂无":
                    out.append({"name": name, "thesis": sec.get("logic", "")})
    except Exception as e:
        print(f"[WARN] 静默异常已显式化 @560: {e}")
    return out


def _em_code_to_symbol(code):
    """东财 6 位纯数字代码 → 带交易所后缀 symbol(与选股宝 super_stock_pool 格式一致)。
    例: 000965→000965.SZ, 603559→603559.SS, 920855→920855.BJ。"""
    c = "".join(filter(str.isdigit, str(code or "")))
    if len(c) != 6:
        return c
    if c.startswith(("60", "68", "90")):
        return c + ".SS"
    if c.startswith(("00", "30", "20")):
        return c + ".SZ"
    if c.startswith(("43", "83", "87", "88", "92", "4", "8")):
        return c + ".BJ"
    return c + ".SZ"


def _prev_trade_date():
    """上一交易日(YYYYMMDD 字符串)，失败返回 None。用于拉昨日涨停池。"""
    try:
        df = robust_call(ak.tool_trade_date_hist_sina)
        dates = sorted(str(x)[:10].replace("-", "") for x in df["trade_date"].tolist())
        today = datetime.now().strftime("%Y%m%d")
        prev = [d for d in dates if d < today]
        return prev[-1] if prev else None
    except Exception as e:
        print(f"[WARN] 静默异常已显式化 @prev_trade_date: {e}")
        return None


def _fetch_em_strong_fallback():
    """东财强势股池降级源：选股宝 super_stock_pool 失效(空)时替代，供「午后强势」源使用。
    ak.stock_zt_pool_strong_em(date=今日) 返回全市场强势股(含未涨停强势 + 已涨停)。
    转成与 super_stock_pool 对齐的结构：change_percent 为小数、limit_up_days 由最新价 vs 涨停价推导、
    surge_reason.related_plates 用所属行业填充。失败/空返回 []。"""
    out = []
    try:
        df = robust_call(ak.stock_zt_pool_strong_em, date=datetime.now().strftime("%Y%m%d"))
        for _, r in df.iterrows():
            sym = _em_code_to_symbol(str(r.get("代码", "")).strip())
            name = str(r.get("名称", "")).strip()
            if not sym or not name:
                continue
            try:
                latest = float(r.get("最新价", 0) or 0)
                lu_price = float(r.get("涨停价", 0) or 0)
                pct = float(r.get("涨跌幅", 0) or 0)   # 东财为百分数(如 3.85)
            except (TypeError, ValueError):
                continue
            boarded = lu_price > 0 and latest >= lu_price - 1e-6
            industry = str(r.get("所属行业", "")).strip()
            out.append({
                "symbol": sym,
                "stock_chi_name": name,
                "limit_up_days": 1 if boarded else 0,
                "change_percent": pct / 100.0,          # 统一成小数，与 super_stock_pool 一致
                "limit_up_price": lu_price,
                "latest_price": latest,
                "surge_reason": {"related_plates": [{"plate_name": industry}]} if industry else None,
            })
    except Exception as e:
        print(f"[WARN] 静默异常已显式化 @em_strong_fallback: {e}")
    return out


def _fetch_em_zt_yesterday():
    """东财昨日涨停池降级源：龙首阴的「昨日涨停」名单，选股宝 super_stock_pool 失效时使用。
    ak.stock_zt_pool_em(date=上一交易日) 返回 [{symbol,name,lianban,industry}](按连板数降序)。
    失败/空返回 []。"""
    out = []
    try:
        prev = _prev_trade_date()
        if not prev:
            return out
        df = robust_call(ak.stock_zt_pool_em, date=prev)
        rows = []
        for _, r in df.iterrows():
            sym = _em_code_to_symbol(str(r.get("代码", "")).strip())
            name = str(r.get("名称", "")).strip()
            if not sym or not name:
                continue
            try:
                lianban = int(r.get("连板数", 1) or 1)
            except (TypeError, ValueError):
                lianban = 1
            rows.append({"symbol": sym, "name": name, "lianban": lianban,
                         "industry": str(r.get("所属行业", "")).strip()})
        rows.sort(key=lambda x: x["lianban"], reverse=True)
        out = rows
    except Exception as e:
        print(f"[WARN] 静默异常已显式化 @em_zt_yesterday: {e}")
    return out


def scan_bottom_surge(top=None, stage=None):
    """调用工作台「底部放量雷达」取候选（ADR-0033）。

    返回雷达 rows(list[dict])；任何异常/超时/开关关闭都返回 [] —— 该通道是纯增量，
    绝不能因为扫描器故障影响 09:25/13:40/14:40 主流程出单。
    """
    if not BOTTOM_SURGE_ENABLED:
        return []
    if stage is not None and stage not in BOTTOM_SURGE_STAGES:
        return []
    if not os.path.exists(BOTTOM_SURGE_PY):
        print(f"[bottom_surge] 未找到扫描器 {BOTTOM_SURGE_PY}，跳过该通道")
        return []
    import subprocess
    try:
        p = subprocess.run(
            [BOTTOM_SURGE_PYTHON, BOTTOM_SURGE_PY, "--json",
             "--top", str(top or BOTTOM_SURGE_TOP)],
            capture_output=True, text=True, timeout=BOTTOM_SURGE_TIMEOUT,
            cwd=os.path.dirname(BOTTOM_SURGE_PY))
    except subprocess.TimeoutExpired:
        print(f"[bottom_surge] 扫描超时(>{BOTTOM_SURGE_TIMEOUT}s)，本轮跳过")
        return []
    except Exception as e:
        print(f"[bottom_surge] 扫描调用异常(已跳过): {type(e).__name__}: {e}")
        return []
    if p.returncode != 0:
        print(f"[bottom_surge] 扫描器退出码 {p.returncode}，stderr 尾部: {(p.stderr or '')[-300:]}")
        return []
    try:
        data = json.loads(p.stdout)
    except Exception as e:
        print(f"[bottom_surge] JSON 解析失败(已跳过): {e}; stdout 头部: {(p.stdout or '')[:120]!r}")
        return []
    rows = data.get("rows") or []
    m = data.get("meta") or {}
    print(f"[bottom_surge] {m.get('asof')} 全A{m.get('universe')}→粗筛{m.get('coarse')}"
          f"→命中{m.get('hits')}，并入池 {len(rows)} 只")
    return rows


def load_review_focus_rows(stage=None):
    """ADR-0041：复盘风向标池 → 供票源「复盘重点」的可参与子集。

    【修复的断点】此前闸门 focus[] 只被用来给候选打 _review_focus 标签，而该标签
    **没有任何消费方**（全仓仅 selftest 读）→ 复盘最贵的输出对推票零影响。
    现在让「非一字板 + 板数达标」的风向标反过来当供票源。

    过滤规则：
      · one_word=True（一字板）→ 不进池。保持 ADR-0039 定位「仅作情绪标尺」，
        一字板成本=建议价 的假设不成立，进池必产幽灵盈亏。
      · board_days > 闸门 max_board_days → 不进池。否则会占掉 Top N 名额后被闸门自己剔除。
      · mode != enforce / hard_block → 不进池（与闸门整体语义保持一致）。
    fail-open：任何异常 → []
    """
    try:
        if not REVIEW_FOCUS_ENABLED:
            return []
        if stage and stage not in REVIEW_FOCUS_STAGES:
            return []
        g = load_review_gate()
        if not g or str(g.get("mode") or "enforce") != "enforce":
            return []
        if g.get("hard_block"):
            return []
        max_bd = _rg_int(g.get("max_board_days"), 0)
        out = []
        for f in (g.get("focus") or []):
            if f.get("one_word"):
                continue
            # v2 闸门才带 board_days；旧 v1 缺失时**宁可不放行**——
            # 否则会把 4 板高位票当 1 板推出去，绕过闸门自己的风格上限（安全默认）。
            bd = _rg_int(f.get("board_days"), 0)
            if bd <= 0:
                continue
            if max_bd and bd > max_bd:
                continue
            sym = f.get("symbol") or f.get("code")
            if not sym:
                continue
            out.append({"symbol": sym, "name": f.get("name"), "board_days": bd,
                        "kind_label": f.get("kind_label"), "why": f.get("why")})
            if len(out) >= REVIEW_FOCUS_MAX:
                break
        return out
    except Exception as e:
        print("[review_focus] 读取失败(放行): %s" % e)
        return []


def load_self_watchlist_rows():
    """ADR-0041：工作台「自选股池」→ 供票源「自选」（人工先验）。

    纯文件读 market-monitor/data/watchlist.json，零网络、零副作用。
    fail-open：文件缺失 / 格式异常 → []

    【2026-11-06 · 用户分账铁律：题材和权重是两套逻辑，不能混淆】
    自选条目带 scope 字段：
      · scope != "trend"（短线/题材票，缺省）→ 进本供票源，参与短线操作池排序
      · scope == "trend"（权重/趋势核心票，如中芯国际/宁德时代）→ **跳过**。
        它们的买卖判定走中线趋势池（trend_research.py 反转启动/趋势状态），
        绝不混进 13:40/14:40 的短线派单 —— 用打板纪律推权重票、或用趋势逻辑
        死扛短线票，都是用户明令禁止的混淆。
    """
    try:
        if not SELF_ENABLED:
            return []
        with open(WATCHLIST_JSON, encoding="utf-8") as f:
            items = json.load(f)
        if not isinstance(items, list):
            return []
        out = []
        skipped_trend = 0
        for it in items:
            if not isinstance(it, dict):
                continue
            code = str(it.get("code") or "").strip()
            if len(_norm_code(code)) != 6:
                continue
            name = str(it.get("name") or code)
            if "ST" in name.upper():        # 自选里混入 ST 时剔除（与池内其它源口径一致）
                continue
            if str(it.get("scope") or "").strip() == "trend":
                skipped_trend += 1
                continue                     # 权重/趋势票不进短线供票源（分账铁律）
            out.append({"symbol": code, "name": name})
            if len(out) >= SELF_MAX:
                break
        if skipped_trend:
            print("[self_watch] 跳过 %d 只趋势 scope 自选（分账：权重票走趋势池，不进短线供票源）"
                  % skipped_trend)
        return out
    except Exception as e:
        print("[self_watch] 读取失败(放行): %s" % e)
        return []


def fetch_theme_arb_rows(stage=None):
    """ADR-0041：工作台「题材套利」引擎 → 供票源「题材套利」。

    引擎需要实时 md（涨停池 + 全题材成分股行情），故取本机工作台现算结果，
    而非在调度器里重复实现一份（ADR-0033 的 subprocess 范式用于独立脚本，
    本例天然已有 HTTP 面，直接复用引擎本体、零逻辑漂移）。
    只收 state_cls ∈ {抢筹逼近, 跟涨强}：已封板买不进，走弱/跟涨不作候选。
    fail-open：工作台未启动 / 超时 / 结构异常 → []
    """
    try:
        if not THEME_ARB_ENABLED:
            return []
        if stage and stage not in THEME_ARB_STAGES:
            return []
        r = requests.get(THEME_ARB_URL, timeout=THEME_ARB_TIMEOUT,
                         proxies={"http": None, "https": None})
        if r.status_code != 200:
            print("[theme_arb] 工作台返回 %s，本次跳过该源" % r.status_code)
            return []
        j = r.json()
        if not isinstance(j, dict) or j.get("error"):
            print("[theme_arb] 结构异常，本次跳过该源")
            return []
        out, seen = [], set()
        for row in (j.get("pool") or []):
            if not isinstance(row, dict) or row.get("state_cls") not in ("near", "strong"):
                continue
            code = _norm_code(row.get("code"))
            if len(code) != 6 or code in seen:
                continue
            seen.add(code)
            out.append({"symbol": code, "name": row.get("name"),
                        "theme_name": row.get("theme_name"),
                        "leader_label": row.get("leader_label"),
                        "state": row.get("state"), "gap_zt": row.get("gap_zt"),
                        "pct": row.get("pct")})
            if len(out) >= THEME_ARB_MAX:
                break
        return out
    except Exception as e:
        print("[theme_arb] 拉取失败(放行): %s" % e)
        return []


def _gather_pool_contributions(xgb, first_board_candidates, pool,
                               plate_limit_up_stocks, plate_all_active_stocks,
                               sorted_reasons, hot_themes, intel_candidates,
                               bottom_surge_rows=None,
                               review_focus_rows=None, self_rows=None,
                               theme_arb_rows=None):
    """归集所有模块的候选股，统一成 contribution 列表。"""
    contribs = []
    # 1) 底部突围 · 首板新星 (board=1)
    for x in (first_board_candidates or [])[:5]:
        contribs.append({
            "symbol": x.get("symbol"), "name": x.get("name"),
            "source": "底部突围", "signal": _score_turnover(x.get("turnover_亿", 0)),
            "board_days": 1, "last_pct": _real_pct(x.get("name")),
            "plates": x.get("plates") or [],
            "thesis": f"底部放量首板，成交额{x.get('turnover_亿',0):.1f}亿"
        })
    # 2) 连板主线 (board>=2) —— 确保立新能源式进度被持续跟踪
    for s in (pool or []):
        lb = s.get("limit_up_days", 1) or 1
        if lb >= 2 and "ST" not in (s.get("stock_chi_name") or ""):
            name = s.get("stock_chi_name")
            _lp = _real_pct(name)
            contribs.append({
                "symbol": s.get("symbol"), "name": name,
                "source": "连板主线", "signal": min(60 + lb * 5, 95),
                "board_days": lb, "last_pct": _lp,
                "plates": _extract_plates(s),
                "thesis": f"{lb}连板成龙主线"
            })
            # 补涨·首阴：连板龙头今日转弱(last_pct<0) → 尾盘首阴低吸候选
            if _lp is not None and _lp < 0:
                contribs.append({
                    "symbol": s.get("symbol"), "name": name,
                    "source": "补涨·首阴", "signal": 70,
                    "board_days": None, "last_pct": None,
                    "plates": _extract_plates(s)[:3],
                    "thesis": "核心龙头首阴低吸(连板转弱)"
                })
    # 3) 题材双雄 (龙头 + 中军) —— 每个热门板块取领涨龙与最大流通盘中军
    hot_plates = [t[0] for t in (sorted_reasons or []) if t[0] not in ('ST股', '业绩增长')][:5]
    for p in hot_plates:
        lus = plate_limit_up_stocks.get(p, [])
        lus.sort(key=lambda x: (x.get('limit_up_days', 0), -x.get('first_limit_up', float('inf'))), reverse=True)
        if lus:
            ld = lus[0]
            name = ld.get("stock_chi_name")
            _lp = _real_pct(name)
            contribs.append({
                "symbol": ld.get("symbol"), "name": name,
                "source": "题材双雄", "signal": 75,
                "board_days": ld.get("limit_up_days", 1), "last_pct": _lp,
                "plates": _extract_plates(ld), "thesis": f"{p}板块领涨龙"
            })
            # 补涨·首阴：双雄龙头今日转弱(last_pct<0)
            if _lp is not None and _lp < 0:
                contribs.append({
                    "symbol": ld.get("symbol"), "name": name,
                    "source": "补涨·首阴", "signal": 72,
                    "board_days": None, "last_pct": None,
                    "plates": _extract_plates(ld)[:3],
                    "thesis": f"{p}龙头首阴低吸(双雄龙头转弱)"
                })
        acts = plate_all_active_stocks.get(p, [])
        acts.sort(key=lambda x: x.get('non_restricted_capital', 0), reverse=True)
        if acts:
            core = acts[0]
            contribs.append({
                "symbol": core.get("symbol"), "name": core.get("stock_chi_name"),
                "source": "题材双雄", "signal": 65,
                "board_days": core.get("limit_up_days", 1), "last_pct": _real_pct(core.get("stock_chi_name")),
                "plates": _extract_plates(core), "thesis": f"{p}板块中军"
            })
    # 4) 情报 (研报/AI 题材龙头中军) —— 名称解析为代码后并入池
    for ic in (intel_candidates or []):
        sym = _resolve_symbol(ic.get("name", ""))
        if sym:
            contribs.append({
                "symbol": sym, "name": ic.get("name"),
                "source": "情报", "signal": 70,
                "board_days": None, "last_pct": None,
                "plates": [], "thesis": ic.get("thesis", "")
            })
    # 5) 龙头资金流入 (如接口可用)
    try:
        for f in get_top10_main_funds():
            contribs.append({
                "symbol": f.get("code"), "name": f.get("name"),
                "source": "龙头资金", "signal": min(50 + (f.get('net_inflow', 0) or 0) * 5, 90),
                "board_days": None, "last_pct": (f.get('pct', 0) or 0) * 100,
                "plates": [f.get("sector")] if f.get("sector") else [],
                "thesis": f"主力净流入{f.get('net_inflow',0):.1f}亿"
            })
    except Exception as e:
        print(f"[WARN] 静默异常已显式化 @632: {e}")
    # 6) 补涨池 (尾盘潜伏专用：核心龙头首阴低吸 + 板块内高低切补涨二板)
    #    6a 龙头首阴 已由 2)3) 中 last_pct<0 追加 "补涨·首阴" source 完成
    #    6b 高低切补涨二板：板块内非涨停强势股(涨 3% ~ 涨停阈值-1%)，每板块 top2,共 ≤8 只
    try:
        _lu_syms = {s.get("symbol") for s in (pool or []) if s.get("symbol")}
        _added = set()
        for _p in (hot_plates or [])[:5]:
            _acts = list(plate_all_active_stocks.get(_p, []) or [])
            # 按涨幅倒序(None/无效排末尾),让板块内最强补涨票优先入选
            def _cpct_key(_s):
                try:
                    return float(_s.get("change_percent"))
                except (TypeError, ValueError):
                    return -999.0
            _acts.sort(key=_cpct_key, reverse=True)
            _picked = 0
            for _s in _acts:
                _sym = _s.get("symbol")
                _name = _s.get("stock_chi_name") or _s.get("name") or ""
                if not _sym or _sym in _added or _sym in _lu_syms:
                    continue
                _cpct = _s.get("change_percent")
                if _cpct is None:
                    continue
                try:
                    _cpct_f = float(_cpct)
                except (TypeError, ValueError):
                    continue
                _pct_val = _cpct_f * 100 if abs(_cpct_f) < 5 else _cpct_f
                _limit_p = _limit_pct(_sym, _name) * 100
                if 3.0 <= _pct_val < _limit_p - 1.0:
                    contribs.append({
                        "symbol": _sym, "name": _name,
                        "source": "补涨池·高低切", "signal": 70,
                        "board_days": 0, "last_pct": round(_pct_val, 2),
                        "plates": _extract_plates(_s)[:3],
                        "thesis": f"{_p}板块高低切补涨(涨{_pct_val:.1f}%未封板)"
                    })
                    _added.add(_sym)
                    _picked += 1
                    if _picked >= 2:
                        break
            if len(_added) >= 8:
                break
    except Exception as e:
        print(f"[WARN] 静默异常已显式化 @rebound_pool: {e}")

    # 7) 午后强势 (13:40)：当日资金净流入最多板块内、有涨停趋势但尚未涨停的优质票。
    #    数据源 = super_stock_pool(强势池)。筛选：今日未涨停(limit_up_days==0 且未封板)
    #    且实时涨幅 3% ~ 涨停阈值-1%(有涨停趋势)。板块热度加分：命中热门板块 signal 更高。
    #    降级：super_stock_pool 空 → 东财强势股池 stock_zt_pool_strong_em(字段已对齐)。
    try:
        _super_pool = xgb.get('super_stock_pool', []) if isinstance(xgb, dict) else []
        if not _super_pool:
            _super_pool = _fetch_em_strong_fallback()
            if _super_pool:
                print(f"[afternoon_strong] super_stock_pool 空，已降级东财强势池 {len(_super_pool)} 只")
        # 【环境风险降级 · 2026-09-04 ADR-0021】炸板率 ≥40% 时午后强势视为高风险
        # （与 advice_def "炸板率畸高绝对禁止半路追高打板" 行为正相关），signal -20 + 警示标签
        _broken_ratio = float(xgb.get('broken_ratio') or 0) if isinstance(xgb, dict) else 0.0
        _env_high_risk = _broken_ratio >= 40.0
        if _env_high_risk:
            print(f"[afternoon_strong] ⚠ 炸板率 {_broken_ratio:.1f}%≥40%，午后强势候选全面风险降级 signal-20")
        for _s in _super_pool:
            _sym = _s.get("symbol")
            _name = _s.get("stock_chi_name") or _s.get("name") or ""
            if not _sym:
                continue
            _lu_days = _s.get("limit_up_days", 0) or 0
            if _lu_days >= 1:                 # 今日已涨停，跳过
                continue
            _cpct = _s.get("change_percent")
            if _cpct is None:
                continue
            try:
                _cpct_f = float(_cpct)
            except (TypeError, ValueError):
                continue
            _pct_val = _cpct_f * 100 if abs(_cpct_f) < 5 else _cpct_f
            if _is_limit_up_pct(_sym, _name, _pct_val):
                continue                     # 已封涨停，午后买不进
            _limit_p = _limit_pct(_sym, _name) * 100
            if 3.0 <= _pct_val < _limit_p - 1.0:
                _plates = _extract_plates(_s)[:3]
                _hot = any(p in hot_themes for p in _plates)
                _base_signal = (80 if _hot else 70)
                _risk_tag = ""
                if _env_high_risk:
                    _base_signal = max(40, _base_signal - 20)   # 高炸板率环境 signal -20，下限 40
                    _risk_tag = f" · ⚠炸板率{_broken_ratio:.0f}%高风险降级"
                contribs.append({
                    "symbol": _sym, "name": _name,
                    "source": "午后强势", "signal": _base_signal,
                    "board_days": 0, "last_pct": round(_pct_val, 2),
                    "plates": _plates,
                    "thesis": (("%s板块午后强势(涨%.1f%%未封板,有冲板趋势)" % (_plates[0], _pct_val)) if _plates else
                              f"午后强势股(涨{_pct_val:.1f}%未封板,有冲板趋势)") + _risk_tag
                })
    except Exception as e:
        print(f"[WARN] 静默异常已显式化 @afternoon_strong: {e}")

    # 8) 龙首阴 (14:40)：昨日涨停(yesterday_limit_up_days>=1)、今日0轴横盘(上下1-2个点)、
    #    无大资金流出(未大跌/未放量跌)的趋势板块热点票，作为尾盘低吸布局次日标的。
    #    主数据源 = super_stock_pool 的昨日连板字段；
    #    降级 = 东财昨涨停池(stock_zt_pool_em) + get_stock_detailed_sina 逐只取今日实时涨幅。
    try:
        _super_pool = xgb.get('super_stock_pool', []) if isinstance(xgb, dict) else []
        _added_dsy = set()
        if _super_pool:
            for _s in _super_pool:
                _sym = _s.get("symbol")
                _name = _s.get("stock_chi_name") or _s.get("name") or ""
                if not _sym or _sym in _added_dsy:
                    continue
                _yd = _s.get("yesterday_limit_up_days", 0) or 0
                if _yd < 1:                       # 昨日未涨停，不是龙首阴
                    continue
                _lu_days = _s.get("limit_up_days", 0) or 0
                if _lu_days >= 1:                 # 今日已涨停，不是首阴
                    continue
                _cpct = _s.get("change_percent")
                if _cpct is None:
                    continue
                try:
                    _cpct_f = float(_cpct)
                except (TypeError, ValueError):
                    continue
                _pct_val = _cpct_f * 100 if abs(_cpct_f) < 5 else _cpct_f
                if _is_limit_up_pct(_sym, _name, _pct_val):
                    continue
                # 0轴横盘：上下 1-2 个点整理(放宽到 ±2.5 容差)；大跌(放量跌/大资金流出)排除
                if -2.5 <= _pct_val <= 2.5:
                    _plates = _extract_plates(_s)[:3]
                    contribs.append({
                        "symbol": _sym, "name": _name,
                        "source": "龙首阴", "signal": 74,
                        "board_days": None, "last_pct": round(_pct_val, 2),
                        "plates": _plates,
                        "thesis": "昨%d连板今日%s%.1f%%横盘首阴(未放量跌)" % (
                            _yd, '+' if _pct_val >= 0 else '', _pct_val)
                    })
                    _added_dsy.add(_sym)
        else:
            # 降级路径：东财昨涨停池 + 今日实时涨幅(取连板数高的前 20 只控耗时)
            _zt_yd = _fetch_em_zt_yesterday()
            if _zt_yd:
                print(f"[dragon_first_shadow] super_stock_pool 空，已降级东财昨涨停池 {len(_zt_yd)} 只")
            for _z in (_zt_yd or [])[:20]:
                _sym = _z.get("symbol")
                _name = _z.get("name")
                if not _sym or not _name or _sym in _added_dsy:
                    continue
                _info = get_stock_detailed_sina(_name)
                if not _info:
                    continue
                _pct = _info.get("pct")
                if _pct is None:
                    continue
                try:
                    _pct_val = float(_pct)
                except (TypeError, ValueError):
                    continue
                if _is_limit_up_pct(_sym, _name, _pct_val):
                    continue
                if -2.5 <= _pct_val <= 2.5:
                    contribs.append({
                        "symbol": _sym, "name": _name,
                        "source": "龙首阴", "signal": 74,
                        "board_days": None, "last_pct": round(_pct_val, 2),
                        "plates": [_z.get("industry")] if _z.get("industry") else ["昨日涨停"],
                        "thesis": "昨%d连板今日%s%.1f%%横盘首阴(东财降级)" % (
                            _z.get("lianban", 1), '+' if _pct_val >= 0 else '', _pct_val)
                    })
                    _added_dsy.add(_sym)
    except Exception as e:
        print(f"[WARN] 静默异常已显式化 @dragon_first_shadow: {e}")

    # 7) 底部放量（ADR-0033）：工作台底部放量雷达的实时扫描结果并入统一操作池。
    #    与 1)「底部突围」的本质区别：底部突围 = **已首板涨停后**才纳入（追封板）；
    #    本源在**未涨停、且处于 120 日区间低位**时即纳入（底部位置 + 相对自身均量放量）。
    #    这是「不追高、可成交」的核心来源，用于替代长期三高推荐（涨停/大涨/高位）。
    try:
        _bs_kept = 0
        for _b in (bottom_surge_rows or []):
            _bsym = _b.get("code")
            if not _bsym:
                continue
            # 推荐口径以雷达为准（recommend 标记 = 分数达 RECOMMEND_MIN_SCORE 且未超 RECOMMEND_MAX）；
            # 字段缺失（旧版雷达）时回退到本地分数门槛，保证前后兼容。
            _bscore = float(_b.get("score") or 0)
            _ok = _b.get("recommend") if "recommend" in _b else (_bscore >= BOTTOM_SURGE_MIN_SCORE)
            if not _ok or _bscore < BOTTOM_SURGE_MIN_SCORE:
                continue
            if _bs_kept >= BOTTOM_SURGE_MAX_IN_POOL:     # 第二道保险：硬上限
                break
            contribs.append({
                "symbol": _bsym, "name": _b.get("name"),
                "source": BOTTOM_SURGE_SOURCE,
                "signal": round(min(_bscore, 100.0), 1),
                "board_days": 1,                      # 非连板标的，仅作首日纳入
                "last_pct": _b.get("pct"),            # 实时涨幅：供 13:40/14:40 未涨停硬过滤
                "plates": [],
                "thesis": "底部放量｜120日%.0f%%位 距高%.0f%% 量比%.1f 换手%.1f%% 距涨停%.1f%%（雷达第%s位）" % (
                    (float(_b.get("pos120") or 0)) * 100,
                    (float(_b.get("dd120") or 0)) * 100,
                    float(_b.get("vol_ratio") or 0),
                    float(_b.get("turnover") or 0),
                    float(_b.get("remain_to_zt_pct") or 0),
                    _b.get("rank_rec") or "-")
            })
            _bs_kept += 1
    except Exception as e:
        print(f"[WARN] 静默异常已显式化 @bottom_surge: {e}")

    # 11) 复盘重点（ADR-0041）——复盘从"只会拦"到"也会推"的关键一环。
    #     数据来自 trade_gate.json 的 focus[]（T 日盘后六引擎产出的风向标池），
    #     只取非一字板且板数达标者；一字板保持"仅情绪标尺"定位（幽灵盈亏教训）。
    try:
        for _f in (review_focus_rows or []):
            contribs.append({
                "symbol": _f.get("symbol"), "name": _f.get("name"),
                "source": REVIEW_FOCUS_SOURCE, "signal": REVIEW_FOCUS_SIGNAL,
                "board_days": _f.get("board_days"), "last_pct": None,
                "plates": [],
                "thesis": "复盘重点·%s｜%s" % (
                    _f.get("kind_label") or "风向标", (_f.get("why") or "")[:70])
            })
    except Exception as e:
        print(f"[WARN] 静默异常已显式化 @review_focus: {e}")

    # 12) 自选股（ADR-0041）——操作者手工挑的票，最强人工先验。
    try:
        for _s in (self_rows or []):
            contribs.append({
                "symbol": _s.get("symbol"), "name": _s.get("name"),
                "source": SELF_SOURCE, "signal": SELF_SIGNAL,
                "board_days": None, "last_pct": None,
                "plates": [],
                "thesis": "自选股池（人工先验，未含模型判断）"
            })
    except Exception as e:
        print(f"[WARN] 静默异常已显式化 @self_watch: {e}")

    # 13) 题材套利（ADR-0041）——题材库龙头 ≥2 板触发 → 同题材"抢筹逼近/跟涨强"成员。
    try:
        for _t in (theme_arb_rows or []):
            contribs.append({
                "symbol": _t.get("symbol"), "name": _t.get("name"),
                "source": THEME_ARB_SOURCE, "signal": THEME_ARB_SIGNAL,
                "board_days": None, "last_pct": _t.get("pct"),
                "plates": [_t.get("theme_name")] if _t.get("theme_name") else [],
                "thesis": "题材套利｜%s（%s）%s 距涨停%s" % (
                    _t.get("theme_name") or "", _t.get("leader_label") or "",
                    _t.get("state") or "",
                    ("%.1f%%" % float(_t["gap_zt"])) if _t.get("gap_zt") is not None else "—")
            })
    except Exception as e:
        print(f"[WARN] 静默异常已显式化 @theme_arb_contrib: {e}")
    return contribs

def is_trading_day():
    try:
        df = robust_call(ak.tool_trade_date_hist_sina)
        trade_dates = [str(x)[:10] for x in df['trade_date'].tolist()]
        today = datetime.now().strftime('%Y-%m-%d')
        return today in trade_dates
    except Exception as e:
        today_weekday = datetime.now().weekday()
        return today_weekday < 5

# =====================================================================
# 时段窗口守卫 + 行情就绪守卫（2026-10-08 新增，ADR-0042）
#
# 【背景】桌面端自动化在 app 唤醒后会把错过的日程"补跑"，于是节后首日 09:10/09:12/09:16
#   连跑 11:40/14:40 三档，抓的是盘前空快照（成交额 0、温度 0、涨停 0、现价 0），
#   其中 14:40 档还会把 strategy_state.json（跨日基准）覆盖成"今日空状态"，
#   导致当日正点运行的【跨日策略连续性追踪】整段丢失。
#
# 【铁律】不在本时段窗口内 / 行情未就绪 → 不生成报告、不写任何状态文件、不弹窗。
#   两道闸各自 fail-open（异常时放行），只有明确判定"不在窗口/无成交"才拦截；
#   需要人工覆盖时用 `--force`。
# =====================================================================
SLOT_WINDOWS = {
    "09:25":    ("09:15", "09:35"),
    "10:40":    ("10:30", "11:00"),
    "11:40":    ("11:25", "12:05"),
    "13:40":    ("13:30", "14:05"),
    "14:40":    ("14:30", "15:10"),
    "盘后复盘": ("15:00", "23:59"),
}


def slot_window_ok(stage, now=None):
    """时段窗口检查 → (ok, reason)。未定义时段（auto 未识别）一律放行（fail-open）。"""
    try:
        win = SLOT_WINDOWS.get(str(stage))
        if not win:
            return True, "未定义窗口(放行)"
        hm = (now or datetime.now()).strftime("%H:%M")
        return (win[0] <= hm <= win[1]), f"允许窗口 {win[0]}-{win[1]}，当前 {hm}"
    except Exception as e:
        return True, f"窗口判定异常(放行): {e}"


def market_data_ready(stage, tries=3, delay=5):
    """行情就绪探测 → (ready, detail)。

    仅对必须依赖"当日真实成交"的时段生效（10:40/11:40/13:40/14:40）；
    09:25 用集合竞价快照、盘后复盘用收盘快照 → 豁免。
    判据：沪深两市成交额 > 0。连续 tries 次拿到 0 → 判定盘前/数据源异常。
    """
    if stage in ("09:25", "盘后复盘"):
        return True, "该时段使用竞价/收盘快照，无需当日盘中成交"
    vol = 0.0
    for i in range(max(1, tries)):
        try:
            _sh, _sz, _cy, vol = get_akshare_macro()
            if vol and vol > 0:
                return True, f"沪深成交额 {vol:.0f} 亿"
        except Exception as e:
            print(f"[readiness] 探测异常(第{i + 1}次): {e}")
        if i < tries - 1:
            time.sleep(delay)
    return False, f"连续 {tries} 次探测沪深成交额均为 0（盘前或数据源异常）"

def append_intraday_log(stage, candidates):
    day_str = datetime.now().strftime('%Y-%m-%d')
    asof_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    payload = {
        "day": day_str,
        "asof": asof_str,
        "session": stage,
        "candidates": candidates
    }
    try:
        with open(INTRADAY_LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(payload, ensure_ascii=False) + '\n')
    except Exception as e:
        print(f"[intraday_log] 写入失败(交叉验证将缺失该时段): {e}")

def get_yesterday_candidates():
    """获取上一个交易日尾盘推荐的股票候选"""
    today_str = datetime.now().strftime('%Y-%m-%d')
    if not os.path.exists(INTRADAY_LOG_FILE):
        return None, []
    
    logs_by_date = defaultdict(list)
    try:
        with open(INTRADAY_LOG_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line: continue
                obj = json.loads(line)
                day = obj.get("day")
                if day and day != today_str:
                    logs_by_date[day].append(obj)
    except Exception as e:
        print(f"[intraday_log] 读取昨日候选失败: {e}")
        
    if not logs_by_date:
        return None, []
        
    sorted_dates = sorted(logs_by_date.keys(), reverse=True)
    prev_date = sorted_dates[0]
    
    candidates = []
    seen_syms = set()
    for obj in logs_by_date[prev_date]:
        if obj.get("session") in ["13:40", "14:40", "late"]:
            for c in obj.get("candidates") or []:
                sym = c.get("symbol")
                if sym and sym not in seen_syms:
                    seen_syms.add(sym)
                    candidates.append(c)
    return prev_date, candidates

BACKTEST_DB_FILE = os.path.join(REPORTS_DIR, 'backtest_database.json')

_QUOTE_CACHE = {}          # {name: (ts, info)} 跨进程行情缓存(落盘到 _QUOTE_CACHE_FILE)
_QUOTE_TTL = 90            # 秒；一次报告生成通常 30~60s 内跑完，同一只票只拉一次网。
                          # 注意：TTL 故意保持很短，确保跨进程也不会复用陈旧行情——
                          # 各时段(09:25/10:40/11:40/13:40/14:40)进程独立触发、间隔数小时，
                          # 缓存必然过期→重新拉网，保证「每个时段数据实时递进、不串味」。
_QUOTE_CACHE_FILE = os.path.join(REPORTS_DIR, ".quote_cache.json")

def _load_quote_cache():
    """启动时从落盘缓存载入；过期条目直接丢弃(保证跨进程也不会用陈旧数据)。"""
    global _QUOTE_CACHE
    _QUOTE_CACHE = {}
    try:
        if os.path.exists(_QUOTE_CACHE_FILE):
            with open(_QUOTE_CACHE_FILE, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            _now = time.time()
            for _n, (_ts, _info) in raw.items():
                if (_now - _ts) < _QUOTE_TTL:
                    _QUOTE_CACHE[_n] = (_ts, _info)
    except Exception as e:
        print(f"[WARN] 行情缓存载入失败(忽略, 重新拉网): {e}")
        _QUOTE_CACHE = {}

def _save_quote_cache():
    """把内存缓存落盘，供后续进程(下一时段/重跑)复用，减少重复打网。"""
    try:
        with open(_QUOTE_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(_QUOTE_CACHE, f, ensure_ascii=False)
    except Exception as e:
        print(f"[WARN] 行情缓存落盘失败(忽略): {e}")

_load_quote_cache()


# ---------------------------------------------------------------------------
# 腾讯行情兜底（2026-10-16 新增）
#
# 背景：2026-10-16 确认台「无法获取现价（suggest_price=0）」根因有两层：
#   ① 主因·URL 未编码：原代码 f-string 拼接中文名到 suggest 接口，带字母后缀的
#      股票名（「深物业A」「万科A」「TCL科技」…）被新浪静默丢弃 → 返回 None。
#      纯中文名（如「荣丰控股」）侥幸可用 → 表现为"部分票能取价、部分票不能"的
#      迷惑现象，此前一直未被识别。
#   ② 次因·WAF 不稳：新浪 suggest/hq 偶发被限流，需要有第二条腿。
# 方案：保留新浪为主（字段最全、语义不变），① 用 params 修编码，② 失败自动降级
# 腾讯 qt.gtimg.cn，两者返回同一 dict 形状 → 13 处调用点零改动。
# 腾讯同样走 90s 缓存，不额外增加打网。
# ---------------------------------------------------------------------------
_TENCENT_SINA_FAIL = {}   # {name: ts} sina 已确认失败的时刻，避免每次都先撞一次墙


def _tencent_resolve_symbol(name):
    """中文名 → 腾讯 symbol（sz000011 / sh600383）。走 smartbox 搜索，无需 Referer。

    响应格式：v_hint="sz~000011~深物业A~swya~GP-A"   ← 市场前缀在代码**前面**（分段）
    """
    try:
        r = requests.get("https://smartbox.gtimg.cn/s3/",
                         params={"q": name, "t": "all"}, timeout=3,
                         headers={"User-Agent": "Mozilla/5.0"})
        r.encoding = "gbk"
        m = re.search(r'v_hint="([^"]+)"', r.text)
        if not m:
            return None
        parts = m.group(1).split("~")
        if len(parts) < 2:
            return None
        market, code = parts[0].strip(), parts[1].strip()
        if market not in ("sh", "sz", "bj") or not re.fullmatch(r"\d{6}", code):
            return None
        return market + code
    except Exception as e:
        print(f"[WARN] 腾讯 smartbox 解析失败(忽略): {e}")
    return None


def _tencent_detailed(name):
    """腾讯 qt.gtimg.cn 单只取价，返回与 get_stock_detailed_sina 同形状的 dict。"""
    sym = _tencent_resolve_symbol(name)
    if not sym:
        return None
    try:
        r = requests.get("https://qt.gtimg.cn/q=" + sym, timeout=4,
                         headers={"User-Agent": "Mozilla/5.0"})
        r.encoding = "gbk"
        p = r.text.split("~")
        if len(p) < 50 or not p[3]:
            return None
        curr = float(p[3])
        prev_close = float(p[4])
        if curr <= 0:
            return None
        def _f(i, d=0.0):
            try:
                return float(p[i])
            except (ValueError, IndexError):
                return d
        return {
            "symbol": sym,
            "name": p[1],
            "open_price": _f(5),
            "yesterday_close": prev_close,
            "current_price": curr,
            "high": _f(33),
            "low": _f(34),
            "volume": int(_f(36)),
            "amount": _f(37) * 1e4,          # 腾讯成交额单位为万元
            "bid1": _f(9),                    # 买一价
            "ask1": _f(19),                   # 卖一价
            "pct": _f(32, (curr - prev_close) / prev_close * 100 if prev_close > 0 else 0.0),
            "date": (p[30][:8] if len(p) > 30 else ""),
            "time": (p[30][8:] if len(p) > 30 else ""),
            "_source": "tencent",
        }
    except Exception as e:
        print(f"[WARN] 腾讯行情获取失败(忽略): {e}")
        return None


def _tencent_detailed_cached(name):
    """带 90s 缓存 + sina 失败记忆的腾讯取价。"""
    _now = time.time()
    _hit = _QUOTE_CACHE.get(name)
    if _hit and (_now - _hit[0]) < _QUOTE_TTL:
        return _hit[1]
    info = _tencent_detailed(name)
    if info:
        _QUOTE_CACHE[name] = (_now, info)
        try:
            _save_quote_cache()
        except Exception:
            pass
    return info


def get_stock_detailed_sina(name):
    """获取单只股票的详细实时数据（新浪主源 + 腾讯兜底，带 90s 缓存）。

    2026-10-16：新浪已被 WAF 封禁，本函数签名与返回值保持不变，内部新增腾讯降级，
    避免 13 处调用点全部拿到 None（曾导致确认台 suggest_price=0）。

    性能背景：一次 generate_report 里 run_backtest / build_account_review /
    build_action_items / 首板追踪 会对同一批标的重复调用本函数，每次都是
    2 个串行 HTTP 请求（代码搜索 + 行情）。持仓 5 只时实测重复请求 40+ 次。
    加 TTL 缓存后同一只票每 90 秒最多打一次网，整体耗时下降一个数量级。
    """
    _now = time.time()
    _hit = _QUOTE_CACHE.get(name)
    if _hit and (_now - _hit[0]) < _QUOTE_TTL:
        return _hit[1]
    # 2026-10-16：新浪若在 10 分钟内已被确认失败，直接走腾讯，不再重复撞墙
    if _now - _TENCENT_SINA_FAIL.get(name, 0) < 600:
        return _tencent_detailed_cached(name)
    try:
        # 1. 搜索股票代码
        # 2026-10-16 修复：原代码用 f-string 把中文名直接拼进 URL，未做 URL 编码。
        # 实测「深物业A」这类带字母后缀的名字会被新浪静默丢弃 → suggestvalue="" →
        # 返回 None → 确认台 suggest_price=0。改用 params 让 requests 自动编码。
        r = requests.get("http://suggest3.sinajs.cn/suggest/type=&key=",
                         params={"key": name}, timeout=3)
        match = re.search(r'var suggestvalue=".*?,.*?,.*?,(sh\d+|sz\d+)', r.text)
        if not match:
            _TENCENT_SINA_FAIL[name] = _now     # 记忆失败 → 后续走腾讯
            return _tencent_detailed_cached(name)
        symbol = match.group(1)

        # 2. 查询实时价格
        headers = {"Referer": "http://finance.sina.com.cn/"}
        r2 = requests.get(f"http://hq.sinajs.cn/list={symbol}", headers=headers, timeout=3)
        data = r2.text.split(',')
        if len(data) > 30:
            stock_name = data[0].split('"')[-1]
            open_price = float(data[1])
            yest_close = float(data[2])
            curr_price = float(data[3])
            high = float(data[4])
            low = float(data[5])
            # 停牌判断需要成交量与买卖一档
            volume = int(data[8]) if len(data) > 8 else 0
            amount = float(data[9]) if len(data) > 9 else 0.0
            bid1 = float(data[6]) if len(data) > 6 else 0.0
            ask1 = float(data[7]) if len(data) > 7 else 0.0
            date_str = data[30]
            time_str = data[31]
            pct = (curr_price - yest_close) / yest_close * 100 if yest_close > 0 else 0.0
            _info = {
                "symbol": symbol,
                "name": stock_name,
                "open_price": open_price,
                "yesterday_close": yest_close,
                "current_price": curr_price,
                "high": high,
                "low": low,
                "volume": volume,
                "amount": amount,
                "bid1": bid1,
                "ask1": ask1,
                "pct": pct,
                "date": date_str,
                "time": time_str,
                "_source": "sina",
            }
            _QUOTE_CACHE[name] = (_now, _info)
            _save_quote_cache()   # 落盘，供后续进程复用(跨进程共享)
            return _info
        # 数据段长度不足 = 接口异常，同样降级
        _TENCENT_SINA_FAIL[name] = _now
        return _tencent_detailed_cached(name)
    except Exception as e:
        print(f"[WARN] 新浪取价失败，降级腾讯: {e}")
        _TENCENT_SINA_FAIL[name] = _now
    return _tencent_detailed_cached(name)


def _real_pct(name):
    """取个股实时涨幅(%)，失败返回 None。用于操作池真实涨幅，替代写死的 9.5。"""
    if not name:
        return None
    try:
        info = get_stock_detailed_sina(name)
        if info and info.get("pct") is not None:
            return float(info["pct"])
    except Exception as e:
        print(f"[WARN] 静默异常已显式化 @804: {e}")
    return None

def get_stock_price_sina(name):
    """通过新浪接口获取单只股票的实时涨跌幅 (保持向后兼容)"""
    info = get_stock_detailed_sina(name)
    if info:
        return info["symbol"], info["pct"]
    return None, 0.0

def build_first_board_followup(prev_state, stage):
    """次日追踪昨日『首板新星』个股的 1进2 晋级情况，给出明确的买/不买判定。
    在 09:25(竞价初判) 与盘中/盘后(晋级决策) 时段渲染。仅作监控提示，不直接下单。"""
    lines = []
    if not prev_state:
        return lines
    stars = prev_state.get("first_board_stars") or []
    if not stars:
        return lines
    if stage not in ["09:25", "10:40", "11:40", "13:40", "14:40", "盘后复盘"]:
        return lines

    prev_date = prev_state.get("date", "昨日")
    is_auction = (stage == "09:25")
    title = "竞价初判" if is_auction else "盘中晋级决策"
    lines.append(f"【四-B、 首板新星次日晋级追踪 (自 {prev_date} 启动股 · {title})】")
    lines.append(f"  📡 昨日监控到的首板新星今日表现与操作判定：")

    buy_count = 0
    for x in stars:
        name = x.get("name", "?")
        sym = x.get("symbol", "")
        plates = x.get("plates") or []
        sym_rt, pct = get_stock_price_sina(name)
        if sym_rt is None:
            lines.append(f"  • {name} ({sym}) | 概念: {','.join(plates)}")
            lines.append(f"    ➡️ 【数据缺失】: 行情接口未能获取，暂不判定，请自行核对。")
            lines.append("")
            continue
        # 盘前无成交保护（2026-10-08 新增）：开盘前接口 pct 恒为 0.00 且无法区分
        # "真平开"与"尚无行情"，若照常判定会被误读成"低开弱势 🚨 不买"。
        # 用昨收价佐证：昨收>0 但现价为 0 → 行情未就绪；昨收亦为 0 → 代码解析失败。
        _info_rt = get_stock_detailed_sina(name)
        if _info_rt and _info_rt.get("yesterday_close", 0) > 0 and _info_rt.get("current_price", 0) <= 0:
            lines.append(f"  • {name} ({sym}) | 概念: {','.join(plates)}")
            lines.append(f"    ➡️ 【行情未就绪】: 盘前/无成交阶段暂无分时数据，无法判定 1进2 承接，待开盘后重跑刷新。")
            lines.append("")
            continue

        if pct >= 9.5:
            state_txt = "🔥 已封板晋级二板"
            verdict = "👑 【持仓锁仓 / 空仓不追一字】 资金顶格封板确认成龙潜质。持仓者死锁不动；空仓者若盘中放量开板可轻仓排单博弈分歧回封，绝不追一字涨停。"
        elif pct >= 3.0:
            state_txt = "⚡️ 强势高开，1进2 进行中"
            buy_count += 1
            if is_auction:
                verdict = "⚡️ 【可挂单打板】 竞价高开≥3%且抢筹明显，1进2概率高。09:25以市价挂单打板买入，仓位轻(≤10%)；若秒封则排队，开板即撤单防守。"
            else:
                verdict = "⚡️ 【可低吸/打板】 盘中维持强势红盘。已封板则持股；未封板则等放量回封时打板或分时均线低吸，仓位轻(≤10%)，不追高。→ 买。"
        elif pct >= 0.0:
            state_txt = "⚪️ 平开震荡，分歧观察"
            verdict = "⚪️ 【暂不买】 竞价/盘中承接偏温和，1进2未确认。先观察半小时内能否放量封板，封板再决定是否打板；否则放弃。"
        else:
            state_txt = "🚨 低开弱势"
            verdict = "🚨 【不买】 低开走弱，弱于预期，1进2失败风险高。放弃买入；若你昨日已持有则借反抽冲高离场。"
        lines.append(f"  • {name} ({sym}) | 今日涨幅: {pct:+.2f}% | 概念: {','.join(plates)} | 状态: {state_txt}")
        lines.append(f"    ➡️ 【操作判定】: {verdict}")
        lines.append("")

    if buy_count > 0:
        lines.append(f"  📌 综合: 今日有 {buy_count} 只首板新星处于强势晋级区间，可轻仓参与 1进2，单股≤10%，严格止损(转跌超-3%离场)；晋级二板后转持有观察。")
    else:
        lines.append(f"  📌 综合: 今日首板新星整体未达强势晋级标准，按纪律空仓观望，等待分歧转一致或新题材。")
    lines.append("")
    return lines

def load_backtest_db():
    defaults = {
        "initial_capital": 500000.0,
        "available_cash": 500000.0,
        "total_equity": 500000.0,
        "active_positions": [],
        "completed_trades": []
    }
    if os.path.exists(BACKTEST_DB_FILE):
        try:
            with open(BACKTEST_DB_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                # Ensure all key fields are present
                for k, v in defaults.items():
                    if k not in data:
                        data[k] = v
                return data
        except Exception as e:
            print(f"[WARN] 静默异常已显式化 @889: {e}")
    return defaults

def save_backtest_db(db):
    try:
        with open(BACKTEST_DB_FILE, 'w', encoding='utf-8') as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[WARN] 静默异常已显式化 @897: {e}")

# ===== 真实持仓参考（仅作操作建议碰撞参考，严禁进入任何策略决策函数） =====
# 来源：用户同花顺截图识别快照 reports/real_positions_YYYY-MM-DD.json
# 设计铁律：本模块只读、只用于「操作建议输出层」的碰撞标注，绝不喂给
# rank_operation_pool / decide_exit / build_action_items / run_backtest 决策逻辑，
# 以保证「真实持仓不扰动当前操作策略」(ADR-0007)。
_REAL_HOLDINGS_CACHE = None

def _norm_sym_for_ref(sym):
    """标准化代码用于跨源匹配：去后缀(.SS/.SZ)、去市场字母、去前导0。"""
    if not sym:
        return ""
    s = str(sym).upper().replace(".SS", "").replace(".SZ", "").replace("SH", "").replace("SZ", "")
    return s.lstrip("0") or "0"

def load_real_holdings():
    """读取用户真实持仓快照（仅参考用），返回双索引 {by_sym, by_name}。
    取 reports/ 下最新一份 real_positions_*.json。文件缺失则返回空，零副作用。"""
    global _REAL_HOLDINGS_CACHE
    if _REAL_HOLDINGS_CACHE is not None:
        return _REAL_HOLDINGS_CACHE
    out = {"by_sym": {}, "by_name": {}}
    try:
        cand = sorted(glob.glob(os.path.join(REPORTS_DIR, "real_positions_*.json")))
        if not cand:
            _REAL_HOLDINGS_CACHE = out
            return out
        latest = cand[-1]
        with open(latest, "r", encoding="utf-8") as f:
            data = json.load(f)
        for p in data.get("positions", []):
            sym = p.get("symbol", "")
            name = p.get("name", "")
            rec = {
                "symbol": sym, "name": name,
                "shares": int(float(p.get("shares", 0))),
                "cost_price": float(p.get("cost_price", 0.0)),
                "current_price": float(p.get("current_price", 0.0)),
                "market_value": float(p.get("market_value", 0.0)),
            }
            ns = _norm_sym_for_ref(sym)
            if ns:
                out["by_sym"][ns] = rec
            if name:
                out["by_name"][name] = rec
    except Exception as e:
        print(f"[WARN] 静默异常已显式化 @load_real_holdings: {e}")
    _REAL_HOLDINGS_CACHE = out
    return out

def real_holding_reference(symbol="", name="", direction="BUY"):
    """碰撞参考：当操作建议标的与用户真实持仓重叠时，返回带真实成本/仓位的参考附注。
    direction: 'BUY' 买入建议碰撞(加仓参考) / 'SELL' 卖出建议碰撞(止盈止损参考) / 'INFO' 其他。
    仅返回文本附注，绝不返回任何会改变策略决策的内容。"""
    rh = load_real_holdings()
    rec = rh["by_sym"].get(_norm_sym_for_ref(symbol)) or rh["by_name"].get(name or "")
    if not rec:
        return ""
    shares = rec["shares"]
    cost = rec["cost_price"]
    cur = rec["current_price"]
    mv = rec["market_value"]
    pnl = (cur - cost) * shares if (cur and cost) else 0.0
    pnl_pct = ((cur / cost - 1) * 100) if (cur and cost) else 0.0
    if direction == "BUY":
        return (f"    📌【真实持仓碰撞·买进参考】你已持有 {shares} 股 @ 真实成本 ¥{cost:.3f} "
                f"（现价 ¥{cur:.2f}，{pnl_pct:+.1f}%，市值 ¥{mv:,.0f}）。"
                f"本次买入建议视为【加仓/补仓参考】，非新建仓；注意单票仓位上限与整体仓位。")
    if direction == "SELL":
        pl = "盈利" if pnl >= 0 else "亏损"
        return (f"    📌【真实持仓碰撞·卖出参考】你真实持有 {shares} 股 @ 真实成本 ¥{cost:.3f} "
                f"（现价 ¥{cur:.2f}，真实{pl} {pnl_pct:+.1f}%，{pl}额 ¥{pnl:,.0f}）。"
                f"可参考真实成本判断是否【止盈/止损】。")
    return (f"    📌【真实持仓碰撞】你已持有 {shares} 股 @ 真实成本 ¥{cost:.3f} "
            f"（现价 ¥{cur:.2f}，{pnl_pct:+.1f}%）。")


def _limit_pct(symbol, name):
    """涨停幅度(小数)。主板10% / 创业板科创板20% / 北交所30% / ST 5%。"""
    code = "".join(filter(str.isdigit, symbol or ""))
    if "ST" in (name or "") or "st" in (name or ""):
        return 0.05
    if code.startswith("300") or code.startswith("688"):
        return 0.20
    if code.startswith("43") or code.startswith("83") or code.startswith("87") or code.startswith("88"):
        return 0.30
    return 0.10


def _is_limit_up_pct(symbol, name, pct):
    """涨幅(%)是否已封涨停(含 0.5% 容差)。pct 为百分比数值(如 9.8)；None 视为未知→不判涨停。"""
    if pct is None:
        return False
    try:
        return float(pct) >= _limit_pct(symbol, name) * 100 - 0.5
    except (TypeError, ValueError):
        return False


def get_stock_limits(symbol, name, yesterday_close):
    # Determine price limit percentage based on code and name
    limit_pct = _limit_pct(symbol, name)
    limit_up = round(yesterday_close * (1 + limit_pct) + 0.0001, 2)
    limit_down = round(yesterday_close * (1 - limit_pct) + 0.0001, 2)
    return limit_up, limit_down

def record_buys_to_db(buy_intents, session_name):
    """
    依据【统一决策】产生的买入意图记录回测买入，与弹窗指令完全一致。
    buy_intents: [{name, symbol, price, buy_shares, note}]（由 decide_action / build_action_items 产出）。
    若意图未带价格/股数（兼容旧调用），回退到行情取价 + 固定10w分配。
    融入可用现金瓶颈约束、一字涨停买不进、交易费用滑点等逻辑。
    """
    today_str = datetime.now().strftime('%Y-%m-%d')
    db = load_backtest_db()
    active_symbols = {p["symbol"] for p in db["active_positions"]}
    
    recorded_any = False
    for it in buy_intents:
        symbol = it.get("symbol")
        name = it.get("name")
        if not symbol:
            continue
        if symbol in active_symbols:
            print(f"[Backtest DB] {name}({symbol}) 已在持仓中，跳过重复买入(暂不支持加仓合并)。")
            continue
            
        intent_price = float(it.get("price") or 0)
        intent_shares = int(it.get("buy_shares") or 0)
        
        info = get_stock_detailed_sina(name)
        price = intent_price
        shares = intent_shares
        if info:
            # 价格优先用意图价（与弹窗一致）；缺失则按会话取开盘价/最新价
            if price <= 0:
                price = info["open_price"] if session_name == "09:25" else info["current_price"]
                if price <= 0:
                    price = info["current_price"]
            if price <= 0:
                continue
            # 数量优先用意图手数（与弹窗一致）；缺失则按 10w 资金折算整手；科创板最小 2 手(200股/手)
            if shares <= 0:
                shares = _to_shares(_afford_lots(100000.0, price, symbol), symbol)
                if shares <= 0:
                    shares = _lot_for(symbol)
            # 进化逻辑 1: 一字涨停流动性陷阱检测（仅09:25）
            if session_name == "09:25":
                try:
                    limit_up, limit_down = get_stock_limits(symbol, name, info.get("yesterday_close", price))
                    if price >= limit_up and info.get("high") == info.get("low"):
                        print(f"[Backtest DB] {name}({symbol}) 一字涨停锁死，跳过买入。")
                        continue
                except Exception as e:
                    print(f"[WARN] 静默异常已显式化 @963: {e}")
        else:
            # 取不到行情且意图也未带价格/数量，跳过
            if price <= 0 or shares <= 0:
                continue

        # 兜底：任何进入账户的买入数量都必须是整手，杜绝碎股污染底账
        shares = _to_shares(_to_lots(shares, symbol), symbol)
        if shares <= 0:
            print(f"[Backtest DB] {name}({symbol}) 折算不足最小交易单位，跳过买入。")
            continue

        capital = shares * price
        # 进化逻辑 2: 真实交易摩擦成本 (买入费用: 0.03% 佣金 + 0.1% 滑点)
        buy_fee = capital * 0.0003 + capital * 0.001
        required_cash = capital + buy_fee
        
        # 进化逻辑 6: 资金池可用余额约束 (Cash Limit)
        if db["available_cash"] < required_cash:
            print(f"[Backtest DB] Skipped Buy: {name}({symbol}) due to insufficient available cash (Required: {required_cash:.2f}, Available: {db['available_cash']:.2f}元)")
            continue
            
        # 扣减可用资金
        db["available_cash"] -= required_cash
        
        db["active_positions"].append({
            "symbol": symbol,
            "name": name,
            "buy_date": today_str,
            "buy_price": price,
            "buy_session": session_name,
            "capital": capital,
            "shares": shares,
            "buy_fee": buy_fee
        })
        recorded_any = True
        print(f"[Backtest DB] Successful Buy: {name}({symbol}) at {price} × {_fmt_lots(shares, symbol)}. Cash deducted: {required_cash:.2f}元")
        
    if recorded_any:
        # 同步刷新净值（现金 + 持仓成本市值），避免"只确认买入未确认卖出"时净值滞留旧值
        db["total_equity"] = db["available_cash"] + sum(float(p.get("capital", 0)) for p in db["active_positions"])
        save_backtest_db(db)


# ===== 尾盘买入待确认机制（方案 B：自动写库前需用户确认实际成交）=====
# 原因：尾盘弹窗给出的“建议买”标的常已封涨停，实际买不进；若直接 record_buys_to_db
# 会让模拟账户误记“已买入”。改为生成【待确认买入单】，待用户收盘前填实际成交股数，
# 15:00 由延迟子进程(或 plist 15:00 触发)按确认结果写库；未确认视为未买入。
def _pending_confirm_path(session):
    today_str = datetime.now().strftime('%Y-%m-%d')
    sess_tag = session.replace(':', '')
    return os.path.join(REPORTS_DIR, f"pending_buy_confirm_{sess_tag}_{today_str}.json")


def write_pending_buy_confirm(session, intents):
    """尾盘(14:40)买入不直接写库，改为生成『待确认买入单』JSON，等待用户收盘前确认实际成交。
    返回文件路径；无意图则返回 None。"""
    if not intents:
        return None
    today_str = datetime.now().strftime('%Y-%m-%d')
    items = []
    for it in intents:
        _sh = int(it.get("buy_shares") or 0)
        _sym = it.get("symbol", "")
        items.append({
            "name": it.get("name", ""),
            "symbol": _sym,
            "session": session,    # 09:25 早盘打板排单 / 13:40 午后强势 / 14:40 尾盘龙首阴，确认台据此区分显示
            "suggest_price": float(it.get("price") or 0),
            "suggest_lots": int(it.get("buy_lots") or _to_lots(_sh, _sym)),
            "suggest_shares": _sh,
            "actual_lots": 0,     # 用户收盘前填写实际成交【手数】；0/缺省 = 未买入
            "actual_shares": 0,   # = actual_lots × 100，由确认台自动同步，勿手改
            "is_limit_up": bool(it.get("is_limit_up", False)),
            "note": it.get("note", ""),
        })
    data = {
        "date": today_str,
        "session": session,
        "deadline": "15:00",
        "status": "pending",
        "instruction": ("请在 15:00 前编辑本文件：将每只标的的 actual_lots 改为你【实际买入】的手数"
                        "（1手=最小交易单位：主板100股/科创板200股；未买入保持 0 或删除该项）；"
                        "如有实际成交价可同时填 actual_price（留 0 则按建议价写库）。"
                        "15:00 程序自动按实际成交项写库；超时未确认则视为未买入，不写库。"),
        "items": items,
    }
    path = _pending_confirm_path(session)
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"[PendingBuy] 已生成尾盘待确认买入单: {path}")
        return path
    except Exception as e:
        print(f"[PendingBuy] 生成待确认单失败: {e}")
        return None


def _mark_stale_board_pending_buys():
    """在午后/尾盘确认台前，检查当天 09:25 早盘打板排队单(及 13:40 午后买入单)的委托价是否已
    严重失效（如跌停、大跌 ≥7%）。给 item 打上 is_stale 标记并追加醒目提示，避免午后/尾盘确认台
    把早间委托价误当作当前买入建议。
    已填 actual_lots/actual_shares 的项视为用户已确认，不再改写。"""
    today = datetime.now().strftime('%Y-%m-%d')
    changed_any = False
    for stage in ("09:25", "13:40"):
        path = os.path.join(REPORTS_DIR, f"pending_buy_confirm_{stage.replace(':', '')}_{today}.json")
        if not os.path.exists(path):
            continue
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception as e:
            print(f"[StalePending] 读取失败 {path}: {e}")
            continue
        changed = False
        for it in data.get("items", []):
            if int(it.get("actual_lots") or 0) > 0 or int(it.get("actual_shares") or 0) > 0:
                continue
            name = it.get("name", "")
            sym = it.get("symbol", "")
            suggest_px = float(it.get("suggest_price") or 0)
            if suggest_px <= 0 or not name:
                continue
            info = get_stock_detailed_sina(name)
            if not info:
                continue
            cur_px = info.get("current_price", 0.0)
            if cur_px <= 0:
                continue
            try:
                lu, ld = get_stock_limits(sym, name, info.get("yesterday_close", cur_px))
            except Exception:
                lu, ld = 0, 0
            is_limit_down = (ld > 0 and abs(cur_px - ld) < 1e-6)
            # 跌停，或最新价较委托价下跌超过 7%：标记为失效
            if is_limit_down or cur_px < suggest_px * 0.93:
                it["is_stale"] = True
                note = it.get("note", "")
                stale_note = (f"⚠️ 委托价 ¥{suggest_px:.2f} 已失效（最新价 ¥{cur_px:.2f}，"
                              f"{'已跌停' if is_limit_down else '已大跌'}）；请填实际成交，0=未成交")
                if note and stale_note not in note:
                    it["note"] = stale_note + "｜" + note
                else:
                    it["note"] = stale_note
                changed = True
        if changed:
            try:
                with open(path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                print(f"[StalePending] 已标记失效排单: {path}")
                changed_any = True
            except Exception as e:
                print(f"[StalePending] 写回失败 {path}: {e}")
    return changed_any


def confirm_pending_buy(session):
    """读取待确认买入单，仅将 actual_shares>0 的项写入回测账户(尊重用户真实成交)。
    幂等：处理后文件重命名为 .confirmed.json，后续重复调用(15:00 plist)自动跳过。"""
    path = _pending_confirm_path(session)
    if not os.path.exists(path):
        print(f"[PendingBuy] 无待确认单({path})，跳过。")
        return
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        print(f"[PendingBuy] 读取待确认单失败: {e}")
        return
    if data.get("status") == "confirmed":
        # 已确认但可能未改名的残留单：补 rename，避免 confirm_gui 提交后的 leftover
        # 检查把它误判成"未全部写入"(2026-09-01 芒果超媒教训)
        _confirmed_path = path.replace(".json", ".confirmed.json")
        if not os.path.exists(_confirmed_path):
            try:
                os.rename(path, _confirmed_path)
            except Exception as e:
                print(f"[PendingBuy] 补改名失败 {path}: {e}")
        return
    real_intents = []
    for it in data.get("items", []):
        # 以【手】为准：actual_lots 优先；兼容仅填了 actual_shares 的旧单/手改单
        lots = int(it.get("actual_lots") or 0)
        if lots <= 0:
            lots = _to_lots(it.get("actual_shares") or 0, it.get("symbol"))
        actual = _to_shares(lots, it.get("symbol"))
        if actual > 0:
            # 成交价：优先用户在确认台填的 actual_price（2026-09-22 新增），
            # 留 0/缺省回退建议价（兼容旧单与未填价场景）
            _px = float(it.get("actual_price") or 0) or float(it.get("suggest_price") or 0)
            real_intents.append({
                "name": it.get("name"),
                "symbol": it.get("symbol"),
                "price": _px,
                "buy_shares": actual,
                "buy_lots": lots,
                "note": "用户确认成交(尾盘待确认单)",
            })
    confirmed_path = path.replace(".json", ".confirmed.json")
    if real_intents:
        record_buys_to_db(real_intents, session)
        print(f"[PendingBuy] 已按确认结果写入 {len(real_intents)} 只买入。")
    else:
        print(f"[PendingBuy] 待确认单中无实际成交(actual_shares 均为0)，不写库。")
    # 标记为已处理（幂等），避免 15:00 plist 触发重复写
    try:
        os.rename(path, confirmed_path)
    except Exception:
        try:
            os.remove(path)
        except Exception as e:
            print(f"[WARN] 静默异常已显式化 @1102: {e}")


# ===== 卖出待确认机制 =====
# 原因：run_backtest 原先在 10:40+ 直接对持仓执行平仓，系统替用户卖出，与买入同一类“自动执行无确认”毛病。
# 改为生成【待确认卖出单】，持仓保留在库，待用户盘中确认实际卖出，15:00 再按确认结果平仓。
# 多次调用(10:40/11:40/13:40/14:40)时保留用户已填的 actual_* 字段，仅刷新系统建议字段。
_PENDING_FILE_NOTES = []


def _pending_sell_path():
    today_str = datetime.now().strftime('%Y-%m-%d')
    return os.path.join(REPORTS_DIR, f"pending_sell_confirm_{today_str}.json")


def write_pending_sell_confirm(sell_recs):
    """卖出不直接写库，改为生成/更新【待确认卖出单】。保留用户 actual_* 字段。"""
    if not sell_recs:
        return None
    path = _pending_sell_path()
    existing = {}
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                for it in json.load(f).get("items", []):
                    existing[it.get("symbol")] = it
        except Exception as e:
            print(f"[WARN] 静默异常已显式化 @1129: {e}")
    items = []
    for r in sell_recs:
        sym = r.get("symbol")
        prev = existing.get(sym, {})
        _held = int(r.get("held_shares", 0))
        _rec = int(r.get("recommend_sell_shares", 0))
        items.append({
            "name": r.get("name", ""),
            "symbol": sym,
            "action": r.get("action", "SELL_ALL"),
            "held_lots": _to_lots(_held, sym),
            "held_shares": _held,
            "recommend_sell_lots": _to_lots(_rec, sym),
            "recommend_sell_shares": _rec,
            "price": float(r.get("price") or 0),
            "pct": float(r.get("pct") or 0),
            "note": r.get("note", ""),
            "actual_sell": bool(prev.get("actual_sell", False)),
            "actual_lots": int(prev.get("actual_lots", 0)),
            "actual_shares": int(prev.get("actual_shares", 0)),
            "actual_sell_price": float(prev.get("actual_sell_price", 0.0)),
        })
    data = {
        "date": datetime.now().strftime('%Y-%m-%d'),
        "deadline": "15:00",
        "status": "pending",
        "instruction": ("请在 15:00 前编辑本文件：将每只你【实际已卖出】的标的 actual_sell 改为 true，"
                        "并填写 actual_lots(实际卖出手数，1手=最小交易单位：主板100股/科创板200股) 与 actual_sell_price(实际卖出价，留0则用15:00现价)。"
                        "填满 held_lots 视为全清(含零股)。15:00 程序自动按 confirmed 项平仓；超时未确认视为未卖出，不平仓。"),
        "items": items,
    }
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"[PendingSell] 已更新待确认卖出单: {path}")
        return path
    except Exception as e:
        print(f"[PendingSell] 生成待确认单失败: {e}")
        return None


def confirm_pending_sell():
    """读取待确认卖出单，仅将 actual_sell=true 的项平仓写入回测账户(尊重用户真实操作)。"""
    path = _pending_sell_path()
    if not os.path.exists(path):
        print("[PendingSell] 无待确认卖出单，跳过。")
        return
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        print(f"[PendingSell] 读取待确认单失败: {e}")
        return
    if data.get("status") == "confirmed":
        # 已确认但可能未改名的残留单：补 rename，与 confirm_pending_buy 保持一致
        _confirmed_path = path.replace(".json", ".confirmed.json")
        if not os.path.exists(_confirmed_path):
            try:
                os.rename(path, _confirmed_path)
            except Exception as e:
                print(f"[PendingSell] 补改名失败 {path}: {e}")
        return
    db = load_backtest_db()
    confirmed_path = path.replace(".json", ".confirmed.json")
    executed = 0
    for it in data.get("items", []):
        if not it.get("actual_sell"):
            continue
        sym = it.get("symbol")
        pos = next((p for p in db["active_positions"] if p["symbol"] == sym), None)
        if not pos:
            print(f"[PendingSell] {sym} 已不在持仓中，跳过。")
            continue
        held = int(pos["shares"])
        held_lots = _to_lots(held, sym)
        # 以【手】为准：actual_lots 优先；兼容旧单只填 actual_shares
        lots = int(it.get("actual_lots") or 0)
        if lots <= 0:
            lots = _to_lots(it.get("actual_shares") or 0, sym)
        if lots <= 0:
            lots = held_lots if it.get("action") == "SELL_ALL" else held_lots // 2
        lots = min(lots, held_lots)
        # 手数填满 = 全清，连带清掉历史零股，避免账上留下无法交易的碎股
        actual = held if lots >= held_lots else _to_shares(lots, sym)
        actual = min(actual, held)
        if actual <= 0:
            continue
        sell_price = float(it.get("actual_sell_price") or 0)
        if sell_price <= 0:
            info = get_stock_detailed_sina(it.get("name", ""))
            sell_price = info["current_price"] if info else pos["buy_price"]
        buy_price = pos["buy_price"]
        buy_date = pos["buy_date"]
        buy_session = pos["buy_session"]
        capital = pos["capital"]
        buy_fee = pos.get("buy_fee", 0.0)
        sell_val = sell_price * actual
        sell_fee = sell_val * 0.0003 + sell_val * 0.0005 + sell_val * 0.001
        buy_fee_share = buy_fee * (actual / held)
        raw_pnl = (sell_price - buy_price) * actual
        net_pnl = raw_pnl - sell_fee - buy_fee_share
        cash_return = sell_val - sell_fee
        is_all = (actual >= held)
        db["completed_trades"].append({
            "symbol": sym,
            "name": it.get("name", ""),
            "buy_date": buy_date,
            "buy_price": buy_price,
            "buy_session": buy_session,
            "sell_date": data["date"],
            "sell_price": sell_price,
            "sell_session": "待确认平仓",
            "capital": capital * (actual / held),
            "shares": actual,
            "profit_loss_amount": net_pnl,
            "profit_loss_pct": net_pnl / (capital * (actual / held)) * 100 if capital else 0.0,
            "total_fees": sell_fee + buy_fee_share,
            "hold_days": max(1, (datetime.strptime(data["date"], "%Y-%m-%d") - datetime.strptime(buy_date, "%Y-%m-%d")).days),
        })
        db["available_cash"] += cash_return
        if is_all:
            db["active_positions"] = [p for p in db["active_positions"] if p["symbol"] != sym]
        else:
            pos["shares"] = held - actual
            pos["capital"] = capital * (1 - actual / held)
            pos["buy_fee"] = buy_fee * (1 - actual / held)
        executed += 1
        print(f"[PendingSell] 已平仓 {it.get('name')}({sym}) {_fmt_lots(actual, sym)} @ {sell_price:.2f}，回流 {cash_return:.2f}元。")
    if executed:
        db["total_equity"] = db["available_cash"] + sum(float(p.get("capital", 0)) for p in db["active_positions"])
        save_backtest_db(db)
        print(f"[PendingSell] 共平仓 {executed} 只。")
    else:
        print("[PendingSell] 待确认单中无实际卖出(actual_sell 均为 false)，不平仓。")
    try:
        os.rename(path, confirmed_path)
    except Exception:
        try:
            os.remove(path)
        except Exception as e:
            print(f"[WARN] 静默异常已显式化 @1263: {e}")


def reconcile_pending_vs_db(today_str=None):
    """回测库 vs 实际成交对账：比对「今日用户确认买入单(.confirmed.json 中 actual_shares>0)」
    与「回测库 active_positions 中今日新开仓(buy_date==today)」，差异即告警。
    返回 (mismatch_count, lines)；同时写出 reconcile_YYYY-MM-DD.txt。"""
    today_str = today_str or datetime.now().strftime('%Y-%m-%d')
    db = load_backtest_db()
    confirmed_buys = {}  # symbol -> name（用户确认实际成交）
    try:
        for fn in os.listdir(REPORTS_DIR):
            if fn.startswith("pending_buy_confirm_") and fn.endswith("%s.confirmed.json" % today_str):
                with open(os.path.join(REPORTS_DIR, fn), 'r', encoding='utf-8') as f:
                    d = json.load(f)
                for it in d.get("items", []):
                    if int(it.get("actual_shares") or 0) > 0:
                        confirmed_buys[it["symbol"]] = it.get("name", "")
    except Exception as e:
        print(f"[reconcile] 读取确认单失败: {e}")
    db_buys = {p["symbol"]: p.get("name", "")
               for p in db.get("active_positions", []) if p.get("buy_date") == today_str}
    only_confirmed = set(confirmed_buys) - set(db_buys)   # 确认了但库没记 → 疑似写库失败
    only_db = set(db_buys) - set(confirmed_buys)          # 库记了但无确认单 → 需人工核对
    # 今日是否产生过买入排单/建议意图（含已改名 .confirmed.json 与未改名文件）
    had_pending_intent = False
    try:
        for _fn in os.listdir(REPORTS_DIR):
            if _fn.startswith("pending_buy_confirm_") and today_str in _fn:
                had_pending_intent = True
                break
    except Exception:
        pass
    lines = ["【回测库 vs 实际成交对账】 %s" % today_str,
             "  用户确认买入: %d 只；回测库今日新开仓: %d 只" % (len(confirmed_buys), len(db_buys))]
    if only_confirmed:
        lines.append("  ⚠️ 已确认但库未记录(疑似写库失败): " +
                     ", ".join("%s(%s)" % (confirmed_buys[s], s) for s in only_confirmed))
    if only_db:
        lines.append("  ⚠️ 库已记录但无确认单(需人工核对): " +
                     ", ".join("%s(%s)" % (db_buys[s], s) for s in only_db))
    if not only_confirmed and not only_db:
        lines.append("  ✅ 对账一致：确认单与回测库今日新开仓完全匹配。")
    if had_pending_intent and not confirmed_buys and not db_buys:
        lines.append("  ⚠️ 今日有排单/买入建议，但最终 0 成交（确认台未勾选确认，或排单未成交）。")
    out = os.path.join(REPORTS_DIR, "reconcile_%s.txt" % today_str)
    try:
        with open(out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        print(f"[reconcile] 写对账文件失败: {e}")
    print("\n".join(lines))
    return (len(only_confirmed) + len(only_db), lines)


def _cleanup_orphan_pending_sells():
    """如果 active_positions 已清空，但历史 pending_sell 文件还在，则归档为 orphan。

    防止外部 UI 在后续时段(如 14:40 尾盘确认弹窗)继续展示已失效的真实持仓卖出单。
    """
    try:
        db = load_backtest_db()
        held_syms = {p["symbol"] for p in db.get("active_positions", [])}
        path = _pending_sell_path()
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        items = data.get("items", [])
        orphan = False
        if not held_syms:
            orphan = True
        else:
            orphan = all(it.get("symbol") not in held_syms for it in items)
        if orphan:
            orphan_path = path.replace(".json", ".orphan.json")
            if os.path.exists(orphan_path):
                base, ext = os.path.splitext(orphan_path)
                orphan_path = "%s_%s%s" % (base, datetime.now().strftime("%H%M%S"), ext)
            os.rename(path, orphan_path)
            print(f"[Cleanup] active_positions 为空/不匹配，已归档孤儿 pending_sell: {os.path.basename(orphan_path)}")
    except Exception as e:
        print(f"[Cleanup] 清理孤儿 pending_sell 失败: {e}")


def run_pending_confirmations():
    """15:00 自动确认：处理当日所有待确认单。
    顺序【先卖后买】——卖出回笼的现金要能用于当日买入，否则现金紧张时
    record_buys_to_db 会因"可用现金不足"静默跳过本该成交的买单。
    覆盖全部三个买入时段：09:25 早盘打板 / 13:40 午后强势 / 14:40 尾盘龙首阴。
    全部确认写库后自动执行「回测库 vs 实际成交对账」。"""
    confirm_pending_sell()
    for _s in BUY_STAGES:
        confirm_pending_buy(_s)
    reconcile_pending_vs_db()


def _spawn_deadline_confirm():
    """挂起一个 detached 子进程，等到 15:00 后自动按确认结果写库（买+卖）。
    当日只挂一次：用日期锁文件去重，避免 09:25/10:40/13:40/14:40 各挂一个
    造成四个进程同时抢着处理同一批待确认单。"""
    lock = os.path.join(REPORTS_DIR, ".confirm_spawned_%s" % datetime.now().strftime('%Y%m%d'))
    if os.path.exists(lock):
        return False
    try:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "confirm"],
            start_new_session=True,
            stdout=open(os.path.join(REPORTS_DIR, "pending_confirm.log"), "a"),
            stderr=subprocess.STDOUT,
        )
        with open(lock, "w") as f:
            f.write(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        print("[Pending] 已启动 15:00 自动确认子进程。")
        return True
    except Exception as e:
        print(f"[Pending] 启动确认子进程失败: {e}")
        return False


def launch_confirm_gui():
    """【已停用 · DEPRECATED 2026-09-22，ADR-0031】桌面版交互式确认台(confirm_gui.py)。

    停用原因：工作台(market-monitor /workbench)的操作确认台已稳定，且直接回写同一批
    pending_buy_confirm_*.json，故把【买入】收敛为单入口，避免同一批单被两处重复确认。
    注：桌面版并非"无手数"——2026-09-21(ADR-0029)起它会显示并可改「建议 N 手」。真正的
    能力差异在别处（见 docs/操作确认台双入口差异分析-20260922.md）：
      · 桌面版支持【卖出确认】(pending_sell_confirm_*)，工作台目前完全不支持；
      · 桌面版提交即 confirm --now 立即写库，工作台只改 JSON、等 15:00 子进程；
      · 桌面版有卖出超持仓/卖满清零股/买入超额现金三项校验，工作台没有。
    ⚠ 因此停用本函数后，卖出确认目前【没有】替代入口（当前 50万库 0 持仓故未暴露）。

    ⚠ 本函数已无任何调用点，保留仅为回滚便利；请勿在 generate_report 的弹窗派发段重新
    引入调用。若确需恢复桌面确认台，务必同步确认工作台侧入口已关闭。

    历史（保留原设计说明）：启动交互式确认台 GUI(confirm_gui.py)，替代原纯文本弹窗 + 手动改 JSON。
    读取当日未确认 pending 单，清单式逐笔确认，提交后自动写库。
    直接 detached 启动 Tk 窗口。

    历史坑：曾优先用 AppleScript 唤起 Terminal 前台运行，但 osascript 需要
    「自动化/辅助访问」权限，权限被系统重置后每次都卡满 10s 超时才回退，
    白白拖慢并制造"没反应"的错觉。Tk 在同会话内建窗口无需任何隐私权限，
    直接启动即可，故移除 AppleScript 路径。"""
    try:
        gui = os.path.join(os.path.dirname(os.path.abspath(__file__)), "confirm_gui.py")
        if not os.path.exists(gui):
            print("[ConfirmGUI] confirm_gui.py 不存在。")
            return False

        log_path = os.path.join(REPORTS_DIR, "confirm_gui.log")
        p = subprocess.Popen(
            [sys.executable, gui],
            start_new_session=True,
            stdout=open(log_path, "a"),
            stderr=subprocess.STDOUT,
        )
        time.sleep(1.5)                 # 给 Tk 初始化时间
        if p.poll() is not None:        # 已退出 = 没弹起来(无待确认单/Tk 异常)
            print(f"[ConfirmGUI] 确认台进程秒退(exit={p.returncode})，回退文本弹窗。详见 confirm_gui.log")
            return False
        print("[ConfirmGUI] 已弹出交互式确认台。")
        return True
    except Exception as e:
        print(f"[ConfirmGUI] 启动失败: {e}")
        return False


def _wait_until_deadline(hm):
    """阻塞至当日 hm(如 '15:00')；已过时立即返回。"""
    try:
        now = datetime.now()
        target = now.replace(hour=int(hm.split(':')[0]), minute=int(hm.split(':')[1]),
                             second=0, microsecond=0)
        if target <= now:
            return
        delta = (target - now).total_seconds()
        print(f"[PendingBuy] 等待至 {hm} 执行确认 (约 {delta:.0f}s)...")
        time.sleep(delta)
    except Exception as e:
        print(f"[WARN] 静默异常已显式化 @1387: {e}")


def run_backtest(xgb, emit_sell=True):
    """
    对当前模拟持仓进行滚动更新决策，统计长期胜率与盈亏比。
    出场决策矩阵统一走 decide_exit()（五时段框架：10:40 是唯一的卖出决策点）。
    emit_sell=True（仅 10:40 调用）→ 生成【待确认卖出单】真正派单；
    emit_sell=False（11:40 复盘等）→ 只做持仓看板与收益统计，不派卖单，避免跨时段重复平仓。
    融入一字跌停卖不出、真实摩擦成本、非对称性持股天数统计。
    """
    today_str = datetime.now().strftime('%Y-%m-%d')
    db = load_backtest_db()
    mkt_temp = xgb.get('temperature', 50)

    # 动态评估并平仓
    still_active = []
    sell_recs = []   # 待确认卖出建议（不再自动平仓，交由 15:00 确认流程）
    lines = []
    lines.append("===========================================================")
    lines.append("    📈 【强者恒强战法 - 模拟实盘回归测试与滚动持仓看板】")
    lines.append("===========================================================\\n")
    
    lines.append("【一、 50万固额账户多头活跃持仓监控】")
    current_market_value = 0.0
    
    if not db["active_positions"]:
        lines.append("  • 当前无活跃持仓标的。")
    else:
        for pos in db["active_positions"]:
            symbol = pos["symbol"]
            name = pos["name"]
            buy_date = pos["buy_date"]
            buy_price = pos["buy_price"]
            buy_session = pos["buy_session"]
            capital = pos["capital"]
            shares = pos["shares"]
            buy_fee = pos.get("buy_fee", 0.0)
            
            # T+1限制：当天买入的不能在当天卖出
            if buy_date >= today_str:
                still_active.append(pos)
                current_market_value += capital
                lines.append(f"  • ⚪ safety {name} ({symbol}) | 成本: {buy_price:.2f} | 持仓: {_fmt_lots(shares, symbol)} | 购于今日 {buy_session} | 状态: T+1 锁仓中")
                continue
                
            info = get_stock_detailed_sina(name)
            if not info:
                still_active.append(pos)
                current_market_value += capital
                lines.append(f"  • ⚪️ {name} ({symbol}) | 成本: {buy_price:.2f} | 持仓: {_fmt_lots(shares, symbol)} | 无法获取最新实时价，暂不决策。")
                continue
                
            current_price = info["current_price"]
            pct = info["pct"] # 今日较昨收的百分比

            # 盘前无成交保护（2026-10-08 新增）：腾讯/新浪接口在开盘前只回传昨收价，
            # current_price 会被解析为 0.0（真实成交额尚未产生）。若不拦截，0.0 会同时
            # 满足下方「current_price <= limit_down 且 high==low」→ 被误判成"一字跌停锁死"，
            # 并把账户净值写成现金额(-48.47%)。此处统一降级为「行情未就绪」，不做任何决策，
            # 持仓按成本计市值。零价绝不能被解释为跌停。
            if current_price <= 0:
                still_active.append(pos)
                current_market_value += capital
                lines.append(f"  • ⚪️ {name} ({symbol}) | 成本: {buy_price:.2f} | 持仓: {_fmt_lots(shares, symbol)} | 行情未就绪(盘前无成交价)，暂不决策。")
                continue

            # 出场策略评价矩阵 —— 单一权威来源 decide_exit（10:40 去弱留强）
            # 【2026-10-21】传入集合竞价参考（仅在 -2%~4% 档生效；无参考则行为不变）
            _auction_item = None
            if _auction_ref is not None:
                try:
                    _auction_item = _auction_ref.get_ref(symbol)
                except Exception:
                    _auction_item = None
            action, advice = decide_exit(pct, mkt_temp, _auction_item)
            
            # 进化逻辑 3: 一字跌停流动性锁死检测 (无法卖出平仓)
            limit_up, limit_down = get_stock_limits(symbol, name, info["yesterday_close"])
            if action in ["SELL_ALL", "SELL_HALF"] and current_price <= limit_down and info["high"] == info["low"]:
                still_active.append(pos)
                current_market_value += current_price * shares
                lines.append(f"  • 🚨 {name} ({symbol}) | 成本: {buy_price:.2f} -> 现价: {current_price:.2f} (一字跌停锁死) | 状态: 无法平仓卖出！")
                lines.append(f"    ➡️ 【流动性预警】: 一字跌停板无买单承接。被迫继续锁仓，等待流动性释放后再寻机平仓。")
                continue
                
            # 盈亏计算 (基于价格差)
            raw_pnl_amt = (current_price - buy_price) * shares
            pnl_pct = (current_price - buy_price) / buy_price * 100
            
            pnl_icon = "🟢" if raw_pnl_amt > 0 else ("🔴" if raw_pnl_amt < 0 else "⚪️")
            lines.append(f"  • {pnl_icon} {name} ({symbol}) | 成本: {buy_price:.2f} -> 现价: {current_price:.2f} | 盈亏: {raw_pnl_amt:+.2f}元 ({pnl_pct:+.2f}%) | 购于 {buy_date} {buy_session}")
            lines.append(f"    ➡️ 【实战决策指令】: {advice}")
            if action in ("SELL_ALL", "SELL_HALF"):
                # 碰撞参考：卖出建议命中真实持仓时，附真实成本/盈亏，供止盈止损决策(不改变 action)
                _rh_ref = real_holding_reference(symbol, name, "SELL")
                if _rh_ref:
                    lines.append(_rh_ref)

            if action == "HOLD":
                still_active.append(pos)
                current_market_value += current_price * shares
            elif action == "SELL_ALL":
                # 进化逻辑 4: 卖出摩擦成本统计 (0.03% 佣金 + 0.05% 印花税 + 0.1% 交易滑点)
                sell_val = current_price * shares
                sell_fee = sell_val * 0.0003 + sell_val * 0.0005 + sell_val * 0.001
                cash_return = sell_val - sell_fee
                # 不再自动平仓：收集为待确认卖出建议，持仓保留至 15:00 确认
                sell_recs.append({
                    "name": name, "symbol": symbol, "action": "SELL_ALL",
                    "held_shares": shares, "recommend_sell_shares": shares,
                    "price": current_price, "pct": pct, "note": advice,
                })
                still_active.append(pos)
                current_market_value += current_price * shares
                lines.append(f"    🟡 【建议卖出 - 待你确认】: 系统建议【全清 {_fmt_lots(shares, symbol)}】(现价 {current_price:.2f}，预估变现 {cash_return:.2f}元)。未确认前不自动平仓。")
            elif action == "SELL_HALF":
                # 记录卖出一半（按【手】拆分，不足 2 手无法对半 → 转全清）
                _held_lots = _to_lots(shares, symbol)
                _sell_lots = _held_lots // 2
                if _sell_lots <= 0:
                    action = "SELL_ALL"
                    sold_shares = shares
                else:
                    sold_shares = _to_shares(_sell_lots, symbol)
                sold_val = current_price * sold_shares
                half_sell_fee = sold_val * 0.0003 + sold_val * 0.0005 + sold_val * 0.001
                cash_return = sold_val - half_sell_fee
                # 不再自动平仓：收集为待确认卖出建议，持仓保留至 15:00 确认
                sell_recs.append({
                    "name": name, "symbol": symbol, "action": "SELL_HALF",
                    "held_shares": shares, "recommend_sell_shares": int(sold_shares),
                    "price": current_price, "pct": pct, "note": advice,
                })
                still_active.append(pos)
                current_market_value += current_price * shares
                lines.append(f"    🟡 【建议卖出 - 待你确认】: 系统建议【卖出 50% 仓位】(现价 {current_price:.2f}，预估变现 {cash_return:.2f}元)。未确认前不自动平仓。")

    # 待确认卖出单：仅 10:40 这一个卖出决策点真正派单；其余时段只看板不派单
    if sell_recs and emit_sell:
        sp = write_pending_sell_confirm(sell_recs)
        if sp:
            _PENDING_FILE_NOTES.append(f"待确认卖出单: {os.path.basename(sp)}")
            lines.append("")
            lines.append(f"  📝 已生成【待确认卖出单】(不自动平仓)：{os.path.basename(sp)}")
            lines.append("    请在 15:00 前编辑该文件，将实际卖出项的 actual_sell 改为 true 并填 actual_shares；")
            lines.append("    15:00 自动按确认结果平仓，未确认视为未卖出。")

    # 动态刷新账户的总净值
    db["active_positions"] = still_active
    db["total_equity"] = db["available_cash"] + current_market_value
    save_backtest_db(db)
        
    lines.append("\n【二、 强者恒强战法 - 50万账户长期运行看板】")
    trades = db["completed_trades"]

    # 兼容手工平账记录（如 2026-10-21 手动两笔用 pnl/gross_pnl/fee 旧键名）：
    # 缺标准统计键时补齐，防 KeyError 崩溃（2026-10-22 修复）
    for _t in trades:
        if "profit_loss_amount" not in _t:
            _cost = _t.get("buy_price", 0.0) * _t.get("shares", 0)
            _amt = _t.get("pnl", _t.get("gross_pnl", 0.0))
            _t["profit_loss_amount"] = _amt
            _t["profit_loss_pct"] = _amt / _cost * 100 if _cost else 0.0
            _t.setdefault("total_fees", _t.get("fee", 0.0))
            try:
                _bd = datetime.strptime(_t.get("buy_date", _t.get("sell_date", "1970-01-01")), "%Y-%m-%d")
                _sd = datetime.strptime(_t.get("sell_date", "1970-01-01"), "%Y-%m-%d")
                _t.setdefault("hold_days", max(1, (_sd - _bd).days))
            except Exception:
                _t.setdefault("hold_days", 1)
    
    # 账户净值指标
    acc_pnl_amt = db["total_equity"] - db["initial_capital"]
    acc_pnl_pct = acc_pnl_amt / db["initial_capital"] * 100
    
    lines.append(f"  • 账户初始资金: {db['initial_capital']:.2f} 元 | 账户总净值 (NAV): {db['total_equity']:.2f} 元")
    lines.append(f"  • 账户可用现金: {db['available_cash']:.2f} 元 | 当前持仓市值: {current_market_value:.2f} 元")
    pnl_sign = "+" if acc_pnl_amt >= 0 else ""
    lines.append(f"  • 账户累计盈亏: {pnl_sign}{acc_pnl_amt:.2f} 元 (账户累计收益率: {pnl_sign}{acc_pnl_pct:.2f}%)")
    
    lines.append("\n【三、 强者恒强战法 - 长期跟踪战绩统计看板】")
    if not trades:
        lines.append("  • 暂无平仓历史交易记录，开始跟踪后将在此处实时统计盈亏与成功率。")
    else:
        total_trades = len(trades)
        winning_trades = sum(1 for t in trades if t["profit_loss_amount"] > 0)
        losing_trades = total_trades - winning_trades
        win_rate = winning_trades / total_trades * 100
        total_fees_paid = sum(t.get("total_fees", 0.0) for t in trades)
        
        wins = [t["profit_loss_amount"] for t in trades if t["profit_loss_amount"] > 0]
        losses = [t["profit_loss_amount"] for t in trades if t["profit_loss_amount"] <= 0]
        
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0
        profit_loss_ratio = avg_win / abs(avg_loss) if avg_loss != 0 else float('inf')
        
        avg_pct = sum(t["profit_loss_pct"] for t in trades) / total_trades
        
        # 进化逻辑 5: 非对称性持股时间分析 (切忌短持赢利股，长扛亏损股)
        win_hold_days = [t["hold_days"] for t in trades if t["profit_loss_amount"] > 0]
        loss_hold_days = [t["hold_days"] for t in trades if t["profit_loss_amount"] <= 0]
        avg_win_hold = sum(win_hold_days) / len(win_hold_days) if win_hold_days else 0.0
        avg_loss_hold = sum(loss_hold_days) / len(loss_hold_days) if loss_hold_days else 0.0
        
        lines.append(f"  • 累计完成交易: {total_trades} 笔 | 盈利笔数: {winning_trades} 笔 | 亏损笔数: {losing_trades} 笔")
        lines.append(f"  • 战法胜率 (成功率): {win_rate:.1f}%")
        lines.append(f"  • 累计支付摩擦成本 (税费/滑点): {total_fees_paid:.2f} 元")
        lines.append(f"  • 单笔交易平均报酬率: {avg_pct:+.2f}%")
        lines.append(f"  • 平均单位盈利: {avg_win:.2f} 元 | 平均单位亏损: {avg_loss:.2f} 元")
        lines.append(f"  • 核心盈亏比 (Profit-Loss Ratio): {profit_loss_ratio:.2f}")
        lines.append(f"  • 平均持仓时效: 盈利股 {avg_win_hold:.1f} 天 | 亏损股 {avg_loss_hold:.1f} 天")
        
        # 针对持股时间的智能非对称警报
        if avg_loss_hold > avg_win_hold * 1.5 and avg_loss_hold > 2.0:
            lines.append("  🚨 【持仓警报】: 监测到致命的“抗单/死扛亏损股”倾向！亏损股持股时间显著长于盈利股，请务必坚决执行盘中防守核按钮，严禁自欺欺人格局！")
        elif avg_win_hold > avg_loss_hold * 1.2:
            lines.append("  🟢 【持仓优化】: 极佳！“截断亏损，让利润奔跑”的持仓知行合一特征显著。盈利股有足够格局空间，亏损股离场迅速。")
            
        # 给出模式健壮性定性
        if win_rate >= 55.0 and profit_loss_ratio >= 1.5:
            lines.append("  🔥 【模式评价】: 圣杯状态！具有较高的胜率 and 极佳的盈亏比，强者恒强策略在此波行情中属于印钞机模式！")
        elif win_rate >= 45.0 and profit_loss_ratio >= 1.2:
            lines.append("  🟢 【模式评价】: 稳健盈利。盈亏比良好可完全覆盖试错成本，系统可良性循环运转。")
        else:
            lines.append("  ⚠️ 【模式评价】: 磨损震荡期。胜率或盈亏比不足，表明高位退潮剧烈或主线不连贯，建议缩减买入仓位。")
            
    return lines

def refresh_intel_brief_if_stale(max_age_minutes=INTEL_MAX_AGE_MINUTES, blocking=True):
    """若 market_intel_briefs.json 不存在或不是今天/超过刷新间隔，则调用 intelligence_crawler.py 刷新。
    默认间隔 INTEL_MAX_AGE_MINUTES（30分钟）。交易日各弹窗时段最小间隔为60分钟，因此每次生成报告都会触发刷新，
    保证“实时资讯”始终是当天最新最热的数据。

    blocking=True 时同步等待刷新完成（用于预热/强制刷新）；blocking=False 时后台非阻塞启动刷新，
    不阻塞报告生成（弹窗不被延迟），本次报告先读取已有缓存。
    """
    try:
        stale = True
        last_dt = None
        if os.path.exists(INTEL_FILE):
            with open(INTEL_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            last_dt = _trend_last_updated(data)
            has_sectors = bool(data.get("sectors"))
            today_str = datetime.now().strftime('%Y-%m-%d')
            is_today = last_dt and last_dt.strftime('%Y-%m-%d') == today_str
            within = last_dt and (datetime.now() - last_dt <= timedelta(minutes=max_age_minutes))
            if has_sectors and is_today and within:
                stale = False

        if not stale:
            return False, "fresh"

        script_path = os.path.join(os.path.dirname(__file__), "intelligence_crawler.py")
        print(f"[Intel Brief] 情报缓存已过期(最后更新:{last_dt or '无'}), 正在刷新...")

        # 研报聚合拉13只龙头股研报约需150-180s，预热为独立进程且在弹窗前5分钟运行，给足超时。
        blk_timeout = 300
        if blocking:
            result = subprocess.run(
                [sys.executable, script_path],
                cwd=os.path.dirname(script_path),
                capture_output=True,
                text=True,
                timeout=blk_timeout,
            )
            if result.returncode == 0:
                print(f"[Intel Brief] 情报刷新成功")
                return True, "refreshed"
            err = (result.stderr or result.stdout or f"exit={result.returncode}")[-500:]
            print(f"[Intel Brief] 情报刷新失败: {err}")
            return False, err
        else:
            # 后台非阻塞：新会话启动，日志追加到 intel_crawler.log
            log_path = os.path.join(REPORTS_DIR, "intel_crawler.log")
            with open(log_path, "a", encoding="utf-8") as logf:
                subprocess.Popen(
                    [sys.executable, script_path],
                    cwd=os.path.dirname(script_path),
                    stdout=logf, stderr=logf,
                    start_new_session=True,
                )
            print(f"[Intel Brief] 已在后台启动情报刷新（不阻塞报告生成）")
            return True, "spawned"
    except subprocess.TimeoutExpired:
        print(f"[Intel Brief] 情报刷新超时(>300s)，本次使用缓存数据")
        return False, "timeout"
    except Exception as e:
        print(f"[Intel Brief] 情报刷新异常: {e}")
        return False, str(e)


def get_intel_brief_lines():
    lines = []
    lines.append("【旁系情报 - 24H AI前瞻题材催化剂与机构先锋内参】")

    refreshed, refresh_msg = refresh_intel_brief_if_stale(blocking=False)

    if not os.path.exists(INTEL_FILE):
        lines.append("  • 暂无AI前瞻题材情报记录，正在后台触发情报收集脚本 intelligence_crawler.py 刷新。")
        if refresh_msg and refresh_msg not in ("fresh", "spawned"):
            lines.append(f"  • 自动刷新失败: {refresh_msg}")
        lines.append("")
        return lines

    try:
        with open(INTEL_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)

        last_updated = data.get('last_updated', '未知时间')
        last_dt = _trend_last_updated(data)
        today_str = datetime.now().strftime('%Y-%m-%d')
        is_today = last_dt and last_dt.strftime('%Y-%m-%d') == today_str

        if not is_today:
            lines.append(f"  ⚠️ 前瞻扫描更新时间: {last_updated}（非今日数据，可能已过时）")
            if refresh_msg == "spawned":
                lines.append("  • 后台实时刷新已启动，下次报告将显示最新情报。")
            elif refresh_msg and refresh_msg not in ("fresh", "spawned"):
                lines.append(f"  ⚠️ 自动刷新尝试失败: {refresh_msg}")
        else:
            lines.append(f"  • 前瞻扫描更新时间: {last_updated}")
            if refresh_msg == "spawned":
                lines.append("  • 后台实时刷新已启动，本份报告先显示最近一次缓存，下次报告即更新。")
        lines.append("  📢 AI结合机构研报深度发掘的未来核心题材与风向龙头：")
        for idx, sec in enumerate(data.get("sectors", [])):
            name = sec.get("name", "未知题材")
            logic = sec.get("logic", "无催化逻辑")
            stocks = sec.get("stocks", [])
            lines.append(f"    [{idx+1}] {name}")
            lines.append(f"      - 核心催化逻辑: {logic}")
            if stocks:
                lines.append(f"      - 题材龙头/中军先锋观察个股: {', '.join(stocks)}")
            else:
                lines.append("      - 题材龙头/中军先锋观察个股: 暂无")
    except Exception as e:
        lines.append(f"  • 加载前瞻题材情报失败: {e}")

    lines.append("")
    return lines

def get_trend_research_lines():
    lines = []
    lines.append("【中线产业趋势池 - 全球科技链/产业资本开支/趋势复利】")
    refreshed, refresh_msg = refresh_trend_research_if_stale()

    if not os.path.exists(TREND_FILE):
        lines.append("  • 暂无中线趋势研究记录，可运行 trend_research.py 生成。")
        if refresh_msg and refresh_msg != "fresh":
            lines.append(f"  • 自动刷新失败: {refresh_msg}")
        lines.append("  • 定位: 与短线龙头战法分账管理；这里只输出趋势观察，不输出即时买入指令。")
        lines.append("")
        return lines

    try:
        with open(TREND_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)

        last_updated = data.get('last_updated', '未知时间')
        lines.append(f"  • 趋势扫描更新时间: {last_updated}")
        last_dt = _trend_last_updated(data)
        if last_dt and datetime.now() - last_dt > timedelta(hours=TREND_MAX_AGE_HOURS):
            age_hours = (datetime.now() - last_dt).total_seconds() / 3600.0
            lines.append(f"  • ⚠️ 趋势池数据已陈旧约 {age_hours:.1f} 小时，本次自动刷新未成功，请检查 trend_research.py / AkShare 数据源。")
        elif refreshed:
            lines.append("  • 本次报告已自动刷新趋势池。")
        lines.append("  • 定位: 寻找“产业逻辑硬 + 全球供应链绑定 + 价格趋势持续”的中线观察标的；短线买卖仍服从盘面风险门。")

        rules = data.get("portfolio_rules", [])
        if rules:
            lines.append("  • 组合纪律:")
            for rule in rules[:3]:
                lines.append(f"    - {rule}")

        # 【2026-11-06 · ADR-0050】状态翻转事件 —— 反转启动/趋势升级必须放在最显眼的位置。
        # 旧版只列快照：核心票从「失守防线」翻转的那天报告里只是悄悄换词，用户无从察觉，
        # 这正是「中芯/北方华创/宁德反转了却没提示」的第二层原因（第一层是判定本身没有
        # 「反转启动」这一档）。
        # 读 events_recent（事件日志近 3 日，持久化）而不是 events（只对比上一份、易失）：
        # 盘中多次刷新时 events 常为空，但事件已经发生过，不能让它消失。
        events = data.get("events_recent") or data.get("events") or []
        if events:
            lines.append("  🔥 趋势池事件（近 3 个交易日）:")
            for ev in events[:12]:
                tag = "🔥反转启动" if ev.get("kind") == "reversal" else (
                    "↑升级" if ev.get("kind") == "upgrade" else "↓回落")
                _day = ("[%s] " % ev.get("date")) if ev.get("date") else ""
                lines.append(f"    {tag} {_day}{ev.get('code')} {ev.get('name')}（{ev.get('theme')}）："
                             f"{ev.get('from')} → {ev.get('to')} · {ev.get('note')}")

        top_rows = data.get("top", [])
        if not top_rows:
            lines.append("  • 当前趋势池为空或抓取失败。")
        else:
            lines.append("  📌 趋势池Top观察:")
            for idx, row in enumerate(top_rows[:6]):
                metrics = row.get("metrics", {})
                code = row.get("code", "")
                name = row.get("name", "")
                theme = row.get("theme", "")
                score = row.get("score", 0)
                status = row.get("status", "观察")
                ret60 = metrics.get("ret60_pct", 0)
                dist20 = metrics.get("dist_ma20_pct", 0)
                drawdown = metrics.get("drawdown_from_250d_high_pct", 0)
                binding = row.get("global_binding", "")
                t_signal = row.get("t_signal", "")
                lines.append(f"    [{idx+1}] {code} {name} | {theme} | 评分 {score} | {status}")
                lines.append(f"        产业绑定: {binding}")
                lines.append(f"        趋势指标: 60日涨幅 {ret60}% | 距20日线 {dist20}% | 距250日高点 {drawdown}%")
                lines.append(f"        做T提示: {t_signal}")

        errors = data.get("errors", [])
        if errors:
            lines.append(f"  • 注: {len(errors)} 只种子股抓取失败，已跳过，不影响主报告生成。")
    except Exception as e:
        lines.append(f"  • 加载中线趋势研究失败: {e}")

    lines.append("")
    return lines

# 【2026-10-21】集合竞价样本（模块级传递：get_opening_auction_signals 内部采集，
# 由 09:25 主流程落盘到 auction_ref.json；不改动该函数签名，避免影响既有调用方）
_AUCTION_SCORED_STOCKS = []

# 【2026-10-21】买入侧竞价参考生效时段（09:25 打板不叠加：竞价本就是它的选股依据）
AUCTION_REF_BUY_STAGES = ("13:40", "14:40")


def get_opening_auction_signals(xgb, stage="auto", decision_buy_candidates=None):
    """09:25 集合竞价分析。实时运行与盘后复盘采用双引擎：
       - 实时09:25:
         1. 优先引入【昨日核心龙头/昨日涨停龙竞价判定】！若昨日龙头今天竞价依旧极强（高开>=3%且量能充足），立马亮起【09:25 连板突击狙击挂单指令】！
         2. 精密寻找高开 1.5%~9.5% 且竞价成交额 > 1500万，流通市值 > 30亿的抢筹主力股（确保大额机构及主流游资进入，绝对避开不涨停的温和跟风大盘股）。
       - 盘后复盘: 统计今日真实主力巨量资金涌入（成交额 >= 5亿）个股的最终实盘战绩（涨停率，炸板率），坚决过滤低流动性、虚强跟风大盘股。"""
    
    pool = xgb.get('limit_up_pool', [])
    super_pool = xgb.get('super_stock_pool', [])
    
    all_stocks = []
    seen = set()
    for s in pool + super_pool:
        sym = s.get("symbol")
        if not sym or sym in seen: continue
        seen.add(sym)
        all_stocks.append(s)
        
    scored_stocks = []
    for s in all_stocks:
        name = s.get("stock_chi_name", "")
        if "ST" in name or "st" in name: continue
        
        cap = s.get('non_restricted_capital', 0)
        tr = s.get('turnover_ratio', 0)
        turnover_亿 = (cap * tr) / 100000000
        
        pct = s.get('change_percent', 0) * 100
        
        scored_stocks.append({
            "name": name,
            "symbol": s.get("symbol"),
            "turnover_亿": turnover_亿,
            "pct": pct,
            "cap_亿": cap / 100000000,
            "plates": _extract_plates(s)[:2],
            "raw": s
        })
    # 【2026-10-21】把竞价样本存到模块级变量，供本函数返回后由 09:25 主流程落盘
    # （避免改动函数签名影响既有调用方）
    global _AUCTION_SCORED_STOCKS
    _AUCTION_SCORED_STOCKS = list(scored_stocks)
        
    now_hm = datetime.now().strftime("%H:%M")
    is_live = ("09:15" <= now_hm <= "09:30" and datetime.now().weekday() < 5) or stage == "09:25"
    
    lines = []
    if is_live:
        # ==== 1. 实时竞价抢筹狙击引擎 ====
        scored_stocks.sort(key=lambda x: x["turnover_亿"], reverse=True)
        
        # 👑 引入昨日核心龙的“第二天连板趋势判定与开盘突击买点” 👑
        prev_state = load_state()
        leader_lines = []
        if prev_state:
            prev_date = prev_state.get('date', '')
            prev_dragons = prev_state.get('dragons', [])
            prev_top_leaders = prev_state.get('top_leaders', [])
            yesterday_cores = list(set(prev_dragons + prev_top_leaders))
            
            if yesterday_cores:
                leader_lines.append(f"🔥 【昨日 ({prev_date}) 核心领涨龙头 - 今日连板晋级竞价挂单决策】")
                has_strong_leader = False
                for name in yesterday_cores:
                    match = [x for x in scored_stocks if x["name"] == name]
                    if match:
                        x = match[0]
                        pct = x["pct"]
                        turnover_auction_亿 = x["turnover_亿"]
                        
                        # 强弱研判
                        if pct >= 9.5:
                            advice = "👑 【一字涨停晋级 / 无法直接买入】 资金顶格一字封板。持仓者死锁不动；空仓者若盘中开板可轻仓排单或排队博弈。"
                        elif pct >= 3.0 and turnover_auction_亿 >= 0.1: # 高开 3% 以上且竞价成交额破 1000万 
                            advice = "⚡️ 【绝对强势抢筹 / 九点二十五分立马挂市价买入！】 竞价巨量净流入抢占先机，突破连板大金龙趋势极强！立刻以开盘市价挂单买入，抢占连板十几天大妖股筹码！"
                            has_strong_leader = True
                        elif 0.0 <= pct < 3.0:
                            advice = "⚪️ 【平开震荡 / 逢分歧观察】 竞价承接偏温和。建议不急于竞价挂单，先观察开盘半小时内是否能放量封板，再决定是否打板入场。"
                        else:
                            advice = "🚨 【弱势低开 / 放弃买入，持仓冲高离场】 龙头低开遭遇核按钮，弱于预期。今日放弃狙击买入；持仓股反抽冲高时无条件坚决分批出局！"
                            
                        leader_lines.append(f"  • 🐲 {x['name']} ({x['symbol']}) | 今日竞价高开: {pct:+.2f}% | 竞价成交: {turnover_auction_亿:.2f}亿 | 主线板块: {','.join(x['plates'])}")
                        leader_lines.append(f"    ➡️ 【竞价挂单指令】: {advice}")
                        leader_lines.append("")
                if not has_strong_leader:
                    leader_lines.append("  📢 竞价扫描提示: 昨日核心龙头今日竞价承接一般，均未触发“09:25极限强晋级挂单买点”。")
                leader_lines.append("")
                
        # 实时竞价抢筹：至少流入1500万 (0.15 亿)，且流通市值 > 30 亿
        lines.append("===========================================================")
        lines.append("    🎯 【09:25 集合竞价多头抢筹主力资金监控 & 买入指令(仅情绪+梯队双强才排单)】")
        lines.append("===========================================================\n")
        
        # 将昨日老龙头晋级判定提到竞价报告最前列！
        if leader_lines:
            lines.extend(leader_lines)
            lines.append("-----------------------------------------------------------\n")
            
        lines.append("【当前开盘新主力资金竞价净流入 Top 10 (筹码新博弈阵地)】")
        for idx, x in enumerate(scored_stocks[:10]):
            lines.append(f"  [{idx+1}] {x['name']} ({x['symbol']}) | 竞价成交: {x['turnover_亿']:.2f}亿 | 溢价幅: {x['pct']:+.2f}% | 题材: {','.join(x['plates'])}")
        lines.append("")
        
        # 【买入指令】严格复用统一决策：打板多因子综合分过线 + 个股今日仍强，
        # 才由 build_action_items 产出排单打板标的。与弹窗/回测账户完全一致。
        lines.append("【开盘强势抢筹先锋 - 实时突击狙击指令】")
        if not decision_buy_candidates:
            lines.append(f"  🧊 早盘未触发打板条件（多因子综合分 < {BOARD_SCORE_GATE} 或候选今日不够强），今日不买入，与弹窗指令一致。")
        else:
            lines.append("  📢 打板多因子共振达标，排单指令如下（与弹窗完全一致）：")
            for idx, it in enumerate(decision_buy_candidates):
                name = it.get("name", "")
                sym = it.get("symbol", "")
                price = it.get("price", 0) or 0
                shares = it.get("buy_shares", 0) or 0
                lines.append(f"  🔥 【排单打板标的 {idx+1}】: {name} ({sym})")
                if price > 0:
                    lines.append(f"    - 排单价格: {price:.2f} 元")
                if shares > 0:
                    lines.append(f"    - 建议排单量: {_fmt_lots(shares, sym)} (可用资金约15%)")
                lines.append(f"    - ⚔️ 【操作指令】: 🛒 09:25 集合竞价排单打板，封单>1亿续排；流失30%撤单。")
                lines.append("")
    else:
        # ==== 2. 盘后或日内：主力真实资金巨量流入 Top 10 实盘战绩追溯 (成交额必须 >= 5亿) ====
        heavy_stocks = [x for x in scored_stocks if x["turnover_亿"] >= 5.0]
        heavy_stocks.sort(key=lambda x: x["turnover_亿"], reverse=True)
        top10_heavy = heavy_stocks[:10]
        
        lines.append("===========================================================")
        lines.append("    📊 【主力资金巨量净流入 (>=5亿) 实盘领头羊今日战绩分析】")
        lines.append("===========================================================\n")
        
        lines.append("【一、 今日主力资金巨量流入榜 Top 10 (真金白银多头博弈重仓阵地)】")
        if not top10_heavy:
            lines.append("  • 今日无个股成交额超过 5 亿元，市场多头买入极为平淡。")
            return lines
            
        zt_count = 0
        zb_count = 0
        
        for idx, x in enumerate(top10_heavy):
            is_zt = x["pct"] >= 9.8
            status_str = "🟢 强势涨停封板" if is_zt else "🔴 未封涨停/走弱跟风"
            if is_zt:
                zt_count += 1
            else:
                zb_count += 1
            lines.append(f"  [{idx+1}] {x['name']} ({x['symbol']}) | 今日总成交额: {x['turnover_亿']:.2f}亿 | 今日最终涨幅: {x['pct']:+.2f}% | 实盘表现: {status_str} | 题材概念: {','.join(x['plates'])}")
            
        lines.append(f"\n【二、 超级主力重仓股战术合力评估】")
        lines.append(f"  • 今日成交额 >= 5亿 个股总数: {len(heavy_stocks)} 只 (反映机构与游资资金拥挤度)")
        lines.append(f"  • Top 10 超级重仓标的表现: 封死涨停 {zt_count} 只 | 炸板/未封涨停 {zb_count} 只 | 主力突围率: {(zt_count/10)*100:.1f}%")
        if zt_count >= 7:
            lines.append("  🔥 实战策略结论: 巨量主力资金抱团股封板率极高！资金多头做多信仰一致。当前市场正处于最强主升浪中，可积极顺势重仓 5 亿以上的大成交量领涨龙头股！")
        elif zt_count <= 4:
            lines.append("  ⚠️ 实战策略结论: 巨量大成交个股大面积冲高回落炸板！这表明高位筹码极度松动，主力资金正在逢高分歧派发。建议【坚决规避】日内任何未封板的大成交跟风股，绝对禁止盘中无脑追高，谨防巨额回撤套牢！")
        else:
            lines.append("  📊 实战策略结论: 大成交个股严重分化。属于高低位切换的个股独立战役。战术上应战略性放弃后排跟风股，只死死盯防主流主线（如芯片/光通信）内的绝对第一龙头。")
            
    return lines

def get_resonance_core(today_str=None):
    """从日内交叉验证日志中找出 ≥2 个买入时段(09:25/13:40/14:40)共同出现的真实候选股。
    返回 [(symbol, name, sessions)]，用于报告"今日真实共振核心股"小结，让强者恒强可见化。"""
    today_str = today_str or datetime.now().strftime('%Y-%m-%d')
    by_session = {}
    try:
        if os.path.exists(INTRADAY_LOG_FILE):
            with open(INTRADAY_LOG_FILE, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    if obj.get("day") != today_str:
                        continue
                    sess = obj.get("session")
                    if sess not in ("09:25", "13:40", "14:40"):
                        continue
                    for c in (obj.get("candidates") or []):
                        by_session.setdefault(sess, {})[c.get("symbol")] = c.get("name")
    except Exception as e:
        print(f"[intraday_log] 读取共振核心失败: {e}")
    sym_sessions = {}
    for sess, m in by_session.items():
        for sym, name in m.items():
            sym_sessions.setdefault(sym, {"name": name, "sessions": []})["sessions"].append(sess)
    core = [(sym, v["name"], v["sessions"]) for sym, v in sym_sessions.items() if len(v["sessions"]) >= 2]
    core.sort(key=lambda x: len(x[2]), reverse=True)
    return core


def get_intraday_cross_validation():
    day_str = datetime.now().strftime('%Y-%m-%d')
    by_session = {}
    try:
        if os.path.exists(INTRADAY_LOG_FILE):
            with open(INTRADAY_LOG_FILE, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line: continue
                    obj = json.loads(line)
                    if obj.get("day") == day_str:
                        sess = obj.get("session")
                        cands = obj.get("candidates") or []
                        by_session[sess] = {c.get("symbol"): c.get("name") for c in cands if c.get("symbol")}
    except Exception as e:
        print(f"[intraday_log] 读取交叉验证日志失败: {e}")

    sessions = [s for s in ["09:25", "10:40", "11:40", "13:40", "14:40"] if s in by_session]
    lines = []
    lines.append("【日内题材热度共振交叉验证】")
    if len(sessions) < 2:
        lines.append("  • 暂无充足多时段运行记录（需至少运行 2 个以上时段才可进行日内重合共振交叉验证）")
        return lines

    # Pairwise overlaps
    for i in range(len(sessions)):
        for j in range(i + 1, len(sessions)):
            s1, s2 = sessions[i], sessions[j]
            overlap_syms = set(by_session[s1].keys()) & set(by_session[s2].keys())
            if overlap_syms:
                overlap_names = [f"{by_session[s1][sym]}({sym})" for sym in overlap_syms]
                lines.append(f"  • {s1} 与 {s2} 交叉共振: {', '.join(overlap_names)}")
            else:
                lines.append(f"  • {s1} 与 {s2} 交叉共振: 无")

    # Triple/Quadruple overlaps
    all_sets = [set(by_session[s].keys()) for s in sessions]
    if len(sessions) >= 3:
        triple_overlap = set.intersection(*all_sets[:3])
        if triple_overlap:
            triple_names = [f"{by_session[sessions[0]][sym]}({sym})" for sym in triple_overlap]
            lines.append(f"  🔥 三时段资金持续聚焦 (核心龙头股): {', '.join(triple_names)}")
        else:
            lines.append("  • 三时段聚焦活跃股: 无")
            
    if len(sessions) >= 4:
        quad_overlap = set.intersection(*all_sets[:4])
        if quad_overlap:
            quad_names = [f"{by_session[sessions[0]][sym]}({sym})" for sym in quad_overlap]
            lines.append(f"  👑 四时段终极合力共振 (超强抱团信仰股): {', '.join(quad_names)}")

    return lines

def format_stock_info(stock):
    symbol = stock.get('symbol', '')
    name = stock.get('stock_chi_name', '')
    limit_days = stock.get('limit_up_days', 0)
    capital = stock.get('non_restricted_capital', 0) / 100000000 # Convert to 亿
    turnover_ratio = stock.get('turnover_ratio', 0)
    price = stock.get('price', 0)
    # 精准更新成交额计算，排除重复价格计算
    turnover_amount = capital * turnover_ratio 
    change_pct = stock.get('change_percent', 0)
    lock_vol = stock.get('buy_lock_volume_ratio', 0)
    
    return {
        "symbol": symbol,
        "name": name,
        "limit_days": limit_days,
        "capital_亿": capital,
        "turnover_amt_亿": turnover_amount,
        "change_pct": change_pct,
        "lock_vol": lock_vol
    }



def get_ai_sector_analysis(sector_name):
    import os
    from google import genai
    from google.genai import types
    from dotenv import load_dotenv
    
    # Load dotenv from my-chatbot/.env where GEMINI_API_KEY resides
    load_dotenv(os.environ.get("MA_CHATBOT_ENV", "/Users/williamli/Work/Python_Project/my-chatbot/.env"))
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return "  (未找到 GEMINI_API_KEY，无法进行 AI 题材因果深度逆向检索)"
        
    # Inject proxies for China network proxy bypass
    orig_http = os.environ.get("HTTP_PROXY")
    orig_https = os.environ.get("HTTPS_PROXY")
    os.environ["HTTP_PROXY"] = "http://127.0.0.1:7890"
    os.environ["HTTPS_PROXY"] = "http://127.0.0.1:7890"
    
    prompt = f"""你是一个顶级的A股短线量化策略和题材研究专家。今天上午A股中的【{sector_name}】板块大涨。
请在互联网上搜索并深度分析：
1. 该板块今天上午大涨的最新实时报道原因是什么（找到最硬核的核心催化剂）？
2. 拆解该概念的产业链：谁是核心龙头？谁是代表性中军？
3. 特别分析核心公司的供应链订单情况和机构调研密度，指出哪家公司成分最红、订单最多或机构调研频次极高。
4. 推荐1-2只目前尚未涨停、涨幅在3%-4%左右最适合尾盘潜伏的代表性种子标的（附上简短逻辑）。

请用简练、硬核的文字回答，每句话直击要害，拒绝废话。格式排版如下：
  • 🔍 【大涨核心因果】: ...
  • 💡 【产业链最正宗标的与订单/调研分析】: ...
  • 🎯 【推荐尾盘潜伏黄金种子选手(3%-4%且未涨停)】: ...
"""
    try:
        client = genai.Client(api_key=api_key)
        # Enable Google Search Grounding for real-time internet search
        config = types.GenerateContentConfig(
            tools=[{"google_search": {}}],
            temperature=0.2
        )
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=config
        )
        text = response.text.strip()
        return text
    except Exception as e:
        return f"  (AI 题材深度检索异常: {e})"
    finally:
        # Restore original proxy env variables
        if orig_http: os.environ["HTTP_PROXY"] = orig_http
        else: os.environ.pop("HTTP_PROXY", None)
        if orig_https: os.environ["HTTPS_PROXY"] = orig_https
        else: os.environ.pop("HTTPS_PROXY", None)

def get_top10_main_funds():
    url = 'https://push2.eastmoney.com/api/qt/clist/get?fs=m:0+t:6+f:!2,m:0+t:13+f:!2,m:0+t:80+f:!2,m:1+t:2+f:!2,m:1+t:23+f:!2&fields=f12,f14,f2,f3,f62,f184,f100&fid=f62&po=1&pz=10&pn=1&np=1&ut=b2884a393a59ad64002292a3e90d46a5'
    try:
        r = requests.get(url, proxies={'http': None, 'https': None}, timeout=3)
        if r.status_code == 200:
            items = r.json().get('data', {}).get('diff', [])
            out = []
            for item in items:
                code = item.get('f12', '-')
                name = item.get('f14', '-')
                price = item.get('f2', 0) / 100.0 if item.get('f2') else 0.0
                pct = item.get('f3', 0) / 100.0 if item.get('f3') else 0.0
                net_inflow = item.get('f62', 0.0) / 100000000.0 # to 亿
                net_ratio = item.get('f184', 0) / 100.0 if item.get('f184') else 0.0
                sector = item.get('f100', '-')
                out.append({
                    'code': code,
                    'name': name,
                    'price': price,
                    'pct': pct,
                    'net_inflow': net_inflow,
                    'net_ratio': net_ratio,
                    'sector': sector
                })
            return out
    except Exception as e:
        print(f"[WARN] 静默异常已显式化 @2099: {e}")
    return []

# =====================================================================
# 需求①+②：操作指令抽取 / 订单计划 / 接管账户派发（默认 DRY_RUN）
# =====================================================================
# ===== 交易单位：手 (A股 1手 = 100股) =====
# 全系统对外展示与用户输入一律用【手】；账户底账仍以股存储(价格×股数的金额计算需要)，
# 但所有由系统计算出的数量都保证是整手，杜绝"卖0股""买37股"这类无法执行的废单。
LOT_SIZE = 100


def _lot_for(symbol):
    """每只股票的最小交易单位(股/手)。科创板(688开头)1手=200股,其余1手=100股。"""
    s = str(symbol or "")
    code = "".join(filter(str.isdigit, s))
    if code.startswith("688"):
        return 200
    return LOT_SIZE


def _to_lots(shares, symbol=None):
    """股数 → 手数(向下取整),按代码板块的最小交易单位。"""
    try:
        return int(int(float(shares)) // _lot_for(symbol))
    except Exception:
        return 0


def _to_shares(lots, symbol=None):
    """手数 → 股数。"""
    try:
        return int(float(lots)) * _lot_for(symbol)
    except Exception:
        return 0


def _fmt_lots(shares, symbol=None):
    """按【手】展示数量。历史遗留零股(非整手)会额外标出，正常不会出现。"""
    try:
        s = int(float(shares))
    except Exception:
        return "0手"
    lot = _lot_for(symbol)
    sign = "-" if s < 0 else ""
    lots, odd = divmod(abs(s), lot)
    if odd:
        return "%s%d手%d股" % (sign, lots, odd) if lots else "%s%d股(不足1手)" % (sign, odd)
    return "%s%d手" % (sign, lots)


def _afford_lots(cash, price, symbol=None, min_one=True):
    """按可用金额与价格算可买【手数】。买不满最小手数但总资金够时按最小手数计。
    科创板(688)最小 2 手(400股)。"""
    try:
        price = float(price)
        cash = float(cash)
    except Exception:
        return 0
    if price <= 0:
        return 0
    lot = _lot_for(symbol)
    lot_cost = price * lot
    lots = int(cash // lot_cost)
    min_lots = 2 if _lot_for(symbol) == 200 else 1
    if lots < min_lots:
        if min_one:
            lots = min_lots
        else:
            lots = 0
    return max(0, lots)


def _fmt_money(x):
    try:
        return "{:,.0f}".format(float(x))
    except Exception:
        return str(x)

def _disp_symbol(symbol):
    """统一显示/下单代码：603137.SS -> sh603137；已是 sh/sz/bj 前缀则原样返回。"""
    s = str(symbol or "").strip()
    if not s:
        return ""
    if s.lower().startswith(("sh", "sz", "bj")):
        return s.lower()
    if s.endswith(".SS"):
        return "sh" + s[:-3]
    if s.endswith(".SZ"):
        return "sz" + s[:-3]
    if s.endswith(".BJ"):
        return "bj" + s[:-3]
    return s

# ============================================================================
#  五时段操作框架（唯一权威定义，所有派单逻辑以此为准）
#  09:25  早盘集合竞价涨停池 —— 昨日涨停群今日竞价仍强(一进二/二进三/三进四) → 挂涨停排单
#  10:40  持仓处理 —— 唯一卖出决策点：不强的出，强的留（见 run_backtest）
#  11:40  纯复盘   —— 只做早盘行情分析与账户梳理，不派任何单
#  13:40  午后强势池 —— 当日资金净流入最多板块内、有涨停趋势但尚未涨停的优质票 → 未涨停买入
#  14:40  尾盘龙首阴池 —— 昨日涨停今日0轴横盘(无大资金流出无放量跌) → 尾盘低吸布局次日
# ============================================================================
BUY_STAGES   = ("09:25", "13:40", "14:40")   # 只有这三个时段派买单
SELL_STAGES  = ("10:40",)                    # 只有 10:40 派卖单
BOARD_STAGES = ("09:25",)                    # 打板通道时段(仅早盘集合竞价；13:40 已改为午后强势未涨停池)

BOARD_SCORE_GATE = 55      # 打板多因子综合分门槛(0-100)，达标才排单
BOARD_POS_PCT    = 0.15    # 打板单只仓位占可用现金比例
BOARD_MAX_PICKS  = 1       # 打板单时段最多标的数（控风险）
BOARD_MIN_PCT    = {"09:25": 3.0}            # 「今日依然强势」实时涨幅门槛

LATE_POS_PCT     = 0.15    # 尾盘布局单只仓位比例
LATE_MAX_PICKS   = 2       # 尾盘/午后布局最多标的数（2026-09-22 由 3 降为 2：用户要求「一到两只，太多不好操作」）

# ============================================================================
# 底部放量通道（ADR-0033，2026-09-22 新增）
# ----------------------------------------------------------------------------
# 背景：既有候选源（底部突围=已首板涨停 / 连板主线 / 题材双雄）全部是「已经涨起来」的票，
#       导致推荐集长期呈现「涨停 / 长期大涨 / 高位」三高特征；09:25 打板标的封单大时
#       根本无法成交（09-21 华瓷股份 5 连板一字板，封单 2.79 亿 / 成交 0.51 亿）。
# 做法：接入工作台「底部放量雷达」(market-monitor/bottom_surge.py)，在**未涨停**且处于
#       120 日区间低位时纳入候选（底部位置 + 相对自身均量放量 + 温和涨幅 + 盘中可成交）。
# 铁律：该通道为纯增量、可一键关闭；扫描失败/超时一律返回 []，绝不阻断主流程。
BOTTOM_SURGE_ENABLED = True
BOTTOM_SURGE_PY      = os.environ.get("MA_BOTTOM_SURGE_PY", os.path.join(MARKET_MONITOR_DIR, "bottom_surge.py"))
BOTTOM_SURGE_PYTHON  = "/opt/homebrew/anaconda3/bin/python3"
BOTTOM_SURGE_TOP     = 10          # 取雷达前 N 只并入统一操作池
BOTTOM_SURGE_TIMEOUT = 60          # 秒；实测整轮(全A快照+150只日K)约 5~6s
BOTTOM_SURGE_SOURCE  = "底部放量"
BOTTOM_SURGE_STAGES  = ("13:40", "14:40")   # 只在午后/尾盘接入（09:25 是集合竞价打板通道）
# 排序加分定标（按 13:40/14:40 的**实际**竞争结构算术推导，非拍脑袋）：
#   rank 分 = consensus*15 + signal*0.35 + prog + sector_bonus(+5) + bs_bonus
#   关键前提：13:40/14:40 已有「未涨停硬过滤」(_is_limit_up_pct)，故竞争票的
#            prog 恒为 0~5（board_days>=2 且 last_pct>=9.0 才给 20，而那种票已被过滤），
#            即连板动量项在这两个时段根本吃不到 20 分 —— 与 09:25 打板时段完全不同。
#   竞争票上界 = consensus3/signal95/hot → 45 + 33.25 + 5 + 5 = 88.25
#   竞争票典型 = consensus2/signal80 → 30 + 28 + 5 = 63
#   底部放量票 = consensus1/signal s/prog5 → 20 + 0.35s + bs_bonus
#   → 取 bs_bonus=40：s=78 的雷达票得 87.3，压过典型竞争票(63)、逼近上界(88.25)；
#     弱信号(s=65)得 82.75，仍高于典型竞争票。既落实「不追高」，又不致完全锁死 Top N。
BOTTOM_SURGE_RANK_BONUS = 40.0
# 推荐口径（2026-09-22 用户决策：每天 1~2 只，太多不好操作）：
#   权威口径在雷达侧 bottom_surge.RECOMMEND_MIN_SCORE / RECOMMEND_MAX，输出行带 `recommend` 标记；
#   此处两个常量是**第二道保险**（防雷达被改坏或版本不一致时放出大量候选）。
#   实测当日分布：≥75 分 2 只 / ≥70 分 6 只 / ≥65 分 7 只 → 取 70 + 上限 2 得 2 只。
BOTTOM_SURGE_MIN_SCORE  = 70.0
BOTTOM_SURGE_MAX_IN_POOL = 2

# ============================================================================
# ADR-0041：宗旨对齐 —— 把「只看不用」的模块接成【供票源】
#
# 【问题（2026-10-06 审计）】工作台 17 个 A 股 tab 只有 4 个真正进了推票链路，
#   复盘模式虽然通过 trade_gate.json 约束了买入，但它的**最大输出「风向标池」是死标签**：
#   调度器读了 focus、命中后设置 c["_review_focus"]=True，却没有任何消费方读这个字段
#   → 复盘对推票**只有减法(拦)、没有加法(推)**，与"复盘决定第二天买什么"的定位不符。
#   同理，工作台的「自选股池」「题材套利」两个已算好的候选源也完全没接入。
#
# 【做法】三者统一按 ADR-0033「底部放量」的成熟范式接入：写 contribs → 进白名单 → 分值定标。
#   铁律：纯增量、任何失败返回 []、绝不阻断主流程（fail-open）。
#
# 【定标算术（沿用 ADR-0033 的口径，非拍脑袋）】
#   rank 分 = consensus*15 + signal*0.35 + prog + sector_bonus(+5) + bs_bonus + sf_bonus + rv_bonus
#   13:40/14:40 竞争结构（ADR-0033 实测）：竞争票典型 63、上界 88.25；底部放量强信号 87.3。
#   · 复盘重点：consensus=1(15) + signal75(26.25) + prog 5(首板)/12(2~3板) ≈ 46~53 基础分
#     → 取 rv_bonus=32：得 78~85，压过典型竞争票(63)、不越上界(88.25)。定位=「复盘认定的核心，
#       值得与底部放量同场竞技」，但不许凌驾风控（风控在 apply 阶段，仍在后面）。
#   · 自选股  ：signal 72 → 15+25.2+5 ≈ 45.2 基础分，不加补偿（人工先验只当一票共识，
#       靠"多源共识"自然加权，避免自选多时灌水）。
#   · 题材套利：signal 70 → 15+24.5+5 ≈ 44.5 基础分，不加补偿（理由同上）。
#
# 【时段】三者只在 13:40/14:40 接入：这两个时段是"未涨停可参与"通道（已有硬过滤），
#   09:25 是集合竞价打板通道、口径完全不同，不掺新源。
# ============================================================================
REVIEW_FOCUS_SOURCE   = "复盘重点"
REVIEW_FOCUS_SIGNAL   = 75.0
REVIEW_FOCUS_RANK_BONUS = 32.0
REVIEW_FOCUS_STAGES   = ("13:40", "14:40")
REVIEW_FOCUS_MAX      = 8       # 风向标池本身 ≤8，此处再兜一层
REVIEW_FOCUS_ENABLED  = True     # 总开关（一键回退到"复盘只拦不推"）

SELF_SOURCE           = "自选"
SELF_SIGNAL           = 72.0
SELF_MAX              = 10      # 自选股上限（防手工批量导入把池子灌满）
SELF_ENABLED          = True     # 总开关

THEME_ARB_SOURCE      = "题材套利"
THEME_ARB_SIGNAL      = 70.0
THEME_ARB_MAX         = 8       # 只取扫描池前 N 只"抢筹逼近/跟涨强"成员
THEME_ARB_STAGES      = ("13:40", "14:40")
THEME_ARB_ENABLED     = True     # 总开关；工作台没起时自动空转

# 工作台目录：与 BOTTOM_SURGE_PY 同源（同一台机、同一仓库），推导而非硬编码第二处
MONITOR_DIR = os.path.dirname(BOTTOM_SURGE_PY)
WATCHLIST_JSON = os.path.join(MONITOR_DIR, "data", "watchlist.json")
THEME_ARB_URL = "http://127.0.0.1:8765/api/theme_arb"
THEME_ARB_TIMEOUT = 4.0

# 三时段独立候选池白名单：不同时段从不同性质的源选股，杜绝"全时段共用一个全是涨停的池子"。
# 值为 None 表示该时段不按源过滤(如 09:25 打板，涨停/连板源全部保留)。
STAGE_SOURCE_WHITELIST = {
    "09:25": None,   # 早盘集合竞价涨停池：底部突围/连板主线/题材双雄龙头(涨停票为主，打板专用)
    "13:40": {"午后强势", "题材双雄", "底部突围", "底部放量",
              REVIEW_FOCUS_SOURCE, SELF_SOURCE, THEME_ARB_SOURCE},   # ADR-0041 接入三源
    "14:40": {"龙首阴", "题材双雄", "补涨·首阴", "补涨池·高低切", "底部放量",
              REVIEW_FOCUS_SOURCE, SELF_SOURCE, THEME_ARB_SOURCE},   # ADR-0041 接入三源
}

EXIT_TEMP_GATE   = 45      # 10:40 持仓处理：温度低于此值一律偏向清仓
MORNING_GATE_TEMP = 45     # 操作池排序沿用的弱市压分阈值

# ============================================================================
# ADR-0036：板块资金流接入 + 可成交性闸门（2026-09-28 用户反馈驱动的重构）
#
# 【问题实证（2026-08-24 ~ 09-28 全量回溯，66 条建议）】
#   1) 操作池 159 只中 91 只(59.5%) last_pct>=9.5 → 中位涨幅 9.99%，池子本质是"涨停票池"
#      （源分布：题材双雄70 / 连板主线61 / 底部突围51，前两大源天然只收涨停票）
#   2) 66 条建议实际成交仅 5 条（7.6%）；其中 09:25 打板通道 5 条建议 **0 成交**
#   3) 09:25 打板建议中 80% 当日涨停、60% 为一字板 → 挂涨停价排队属**结构性不可成交**
#   4) 逆向选择：BUY_LIMIT 只在封单被吃掉/开板时才成交 → 成交即"走坏"
#   5) 工作台 board_rank.py 早有 主力净流入(zljlr)/领涨股龙头(lzg)/涨速/量比，
#      但选股引擎只用一个 flat +5 的热点标签匹配 → 资金流信号**完全未被使用**
#
# 【本 ADR 三件事】
#   L1 可成交性闸门   _fill_gate()：现价须站上当日均价 + 成交额下限 → 挡掉"接飞刀"与流动性陷阱
#   L2 板块资金流接入 fetch_sector_flow() + 分级加分 → 净流入板块/其龙头直接加分并放行白名单
#   L3 连板动量降权   prog 20→12 且 board_days>PROG_BOARD_MAX_DAYS 归零 → 不再奖励高位连板
#
# 【安全设计】任一环节异常 → 静默降级为"无资金流信号"，绝不影响主流程出报告。
#
# 【L1 闸门的诚实结论（2026-09-28 离线回放，47 条建议）—— 不要高估它】
#   用 (高+低)/2 作均价代理，与"收盘≥均价"真值一致率 95.0%；拦截 18/66 = 27.3%。
#   但**拦截组次日 +0.45% ≈ 放行组 +1.93%**（拦截组甚至更差一点点）
#   → L1 **没有选股选择性**，它是"纪律/安全"约束（不接飞刀、不碰流动性陷阱），
#     **不是 alpha 来源**。真正有选择性的形态闸门是 ADR-0037（-3.60% vs +2.68%，价差 6.28pp）。
# ============================================================================
SECTOR_FLOW_ENABLED   = True    # 总开关（拉取失败自动降级为无信号）
SECTOR_FLOW_TIMEOUT   = 8       # 秒
SECTOR_FLOW_TOP_N     = 12      # 行业榜 + 概念榜各取前 N（按主力净流入排序后统一截断）
SECTOR_FLOW_TIERS     = ((5.0, 14.0), (2.0, 9.0), (0.8, 5.0))  # (主力净流入门槛 亿元, 排序加分)
SECTOR_FLOW_STAGES    = ("13:40", "14:40")   # 仅午后/尾盘接入（09:25 是集合竞价打板通道）
SECTOR_FLOW_MAX_ADMIT = 2       # 由资金流渠道"额外放行"白名单的候选上限（防灌水）
SECTOR_FLOW_BONUS_CAP = 14.0    # 单票资金流加分上限

FILL_GATE_ENABLED     = True    # 可成交性/承接闸门（_plain_buy_items 内生效）
FILL_MIN_AMOUNT_YI    = 1.0     # 成交额下限（亿）：低于此值难以足量成交
                                # 取数异常时**放行**并打 WARN，宁放过不错杀

PROG_BOARD_BONUS      = 12.0    # 连板确认晋级(board_days 2~MAX 且封板)的动量分（原 20）
PROG_BOARD_BOARD1     = 5.0     # 首板动量分（原 5，保持不变）
PROG_BOARD_MAX_DAYS   = 3       # 超过此连板数不再给动量分（高位连板不加分）

# ============================================================================
# ADR-0037：触板后开板闸门（2026-09-28 用户反馈第二轮 —— 用户假设被正面证实）
#
# 用户原话：「推的票几乎全部是涨停票 挂单追 风险太高 而且很大程度 买不进
#           买进的很大程度是开板了」
#
# 【实测（47 条可判定历史建议，2026-08-24~09-28，新浪日K逐条还原建议日形态）】
#   建议日形态        条数   次日收盘收益   中位      胜率   真实成交
#   封死涨停(收盘=板)   20     +4.08%     +6.31%    65%      1 笔
#   触板后开板(盘中触板  12     -3.60%     -3.22%    33%      3 笔   ← 用户指控组
#             收盘未封)
#   全天未触板          15     +0.81%     +0.62%    53%      1 笔
#   → 放行组(封死+未触板) 35 条均值 +2.68% vs 拦截组 12 条 -3.60%，**价差 6.28pp**
#   → 决定性证据：**5 笔真实成交中有 3 笔(60%)落在「触板后开板」组**，
#      且这 3 笔全部来自 14:40 通道 —— 用户"买进的都是开板票"是精确描述，不是错觉。
#      典型：中国长城 09-22 触板 16.32 收 15.87，成交 44 手 → 次日 -3.69%（用户实际持仓）
#            亚盛集团 09-09 触板 5.80 收 5.71，成交 124 手 → 次日 -9.48%
#            瑞尔特   09-14 触板 10.24 收  8.38            → 次日 -26.37%
#
# 【与 ADR-0036 L1 可成交性闸门的区别（重要）】
#   L1 用均价/成交额做代理，回溯显示**没有选股选择性**（拦截组 +0.45% ≈ 放行组 +1.93%），
#   只是"纪律/安全"约束；本闸门是按**日内实际走势形态**分类，**有明确选择性**（-3.60% vs +2.68%）
#   → 两者的证据强度不同，不可混为一谈。
#
# 【作用域】仅 13:40 / 14:40 的未涨停现价买入通道（_plain_buy_items）。
#   09:25 打板通道走 BUY_LIMIT 排单，其不可成交问题已由 ADR-0035 一字板流动性过滤处理。
# ============================================================================
OPEN_BOARD_GUARD_ENABLED = True   # 总开关（取数异常自动放行，见 _open_board_guard）
OPEN_BOARD_GUARD_TOL     = 0.002  # 距涨停价容差（相对）：日内最高 ≥ 涨停价×(1-TOL) 即视为"触过板"


def board_score(board, mkt_temp=50):
    """打板多因子综合评分(0-100) —— 取代原 is_board_strong 的「一票否决」死规则。

    强者恒强的本质是【多因子共振】，不是单条硬门槛。原逻辑要求
    「连板≥3 且 炸板<25% 且 涨停≥30 且 龙头不弱 且 溢价>0」五条全中，
    任何一条不达标就彻底放弃打板 —— 结构性行情里这等于常年空仓。
    现在改为加权打分：某个因子弱只是扣分，综合分过线依然可以出手。

    因子与权重：
      赚钱效应 premium       28  昨日涨停今日溢价，接力资金愿不愿买单（最核心）
      连板高度 max_lb        24  空间打开程度，梯队有没有高度
      封板质量 broken_ratio  18  炸板率越低封板越扎实
      市场宽度 limit_up      15  涨停家数，情绪面广度
      龙头健康 dragon_health 10  龙头是否还在带队
      大盘温度 mkt_temp       5  整体环境
    返回 (score:int, detail:str)

    【2026-10-21 修 P0-1 · 连续 8 个交易日「炸板率恒 100%」口径 bug】
    根因（实测确认，非推测）：
      1) xuangubao 的 limit_up_broken_ratio 在 09:25 那条记录是 **除零产物**：
         09:25 集合竞价时涨停池刚建、尚无任何炸板 → broken_count=0、分母 limit_up=8
         → ratio=0.0（真值 0，不是缺失）。
      2) 但本函数旧代码 `float(b.get("broken_ratio", 100) or 100)` —— Python 里
         `or` 对 **0 也是假值**，于是把合法的 0.0 当成"字段缺失"，强制改成 100！
      3) 炸板率 100% → 封板质量分 `s_brk = (40-100)/30` 截断为 **0/18**，
         恒扣 18 分。10-09 起 8 天逐字相同「炸板100%(0/18)」，因该值每早都一样 → 每天恒扣。
    修法（不调阈值，只修口径）：
      · 用 `is None` 判定缺失，**不再用 `or`**（0 是合法值，必须原样保留）
      · 新增 `data_ready` 标记：涨停池尚未成型（limit_up 极少 / 炸板数为 0 且分母为 0）
        时，炸板率**给中性分 9/18**（半档），而不是当作 100% 惩罚。
      · 阈值 BOARD_SCORE_GATE 保持 55 不动（10-20 复盘已证明阈值有效）。
    """
    b = board or {}
    try:
        max_lb   = int(b.get("max_lb", 0) or 0)
        # 【关键修复】缺失才用 100；0 是合法值（09:25 除零产物 = 真 0%），必须原样保留
        _brk_raw = b.get("broken_ratio", None)
        broken   = 100.0 if _brk_raw is None else float(_brk_raw)
        limit_up = int(b.get("limit_up", 0) or 0)
        health   = b.get("dragon_health", "中性")
        premium  = float(b.get("premium", 0) or 0)
        temp     = float(mkt_temp or 0)
    except Exception:
        return 0, "打板因子解析失败"

    # 1) 赚钱效应（昨日涨停今日溢价 %）：>3% 满分，-3% 归零，线性
    s_prem = max(0.0, min(1.0, (premium + 3.0) / 6.0)) * 28
    # 2) 连板高度：1板起步，5板及以上满分
    s_lb = max(0.0, min(1.0, (max_lb - 1) / 4.0)) * 24
    # 3) 封板质量：炸板率 10% 以下满分，40% 以上归零
    #    【2026-10-21】data_ready=False → 无数据，给中性半档 9/18；不再按 100% 惩罚
    data_ready = b.get("data_ready")
    if data_ready is None:
        # 未显式给出时按「有数据」处理（向后兼容），仅在明确标记为 False 时走中性分
        data_ready = True
    s_brk = max(0.0, min(1.0, (40.0 - broken) / 30.0)) * 18
    _brk_note = ""
    if not data_ready:
        s_brk = 9.0
        _brk_note = "(无数据·中性半档)"
    # 4) 市场宽度：涨停 15 家起步，50 家满分
    s_lu = max(0.0, min(1.0, (limit_up - 15) / 35.0)) * 15
    # 5) 龙头健康度
    s_dh = {"极强": 10, "强": 8.5, "中性": 5.5, "弱": 2.5, "极弱": 0}.get(health, 5.5)
    # 6) 大盘温度：40 起步，75 满分
    s_mt = max(0.0, min(1.0, (temp - 40) / 35.0)) * 5

    score = int(round(s_prem + s_lb + s_brk + s_lu + s_dh + s_mt))
    detail = ("溢价%.1f%%(%.0f/28) 高度%d板(%.0f/24) 炸板%.0f%%(%.0f/18)%s "
              "涨停%d家(%.0f/15) 龙头%s(%.0f/10) 温度%.0f(%.0f/5)") % (
        premium, s_prem, max_lb, s_lb, broken, s_brk, _brk_note,
        limit_up, s_lu, health, s_dh, temp, s_mt)
    return score, detail


def classify_market_phase(xgb, cy_pct, sh_pct, sz_pct, max_lb):
    """盘口定性（2026-09-28 由 generate_report 内联逻辑重构为纯函数，ADR-0035）。

    背景：09-21/09-23/09-24/09-28 四次复现「【八】盘口定性 vs 【跨日追踪】」结论冲突 ——
    原定性只看溢价/连板高度/跌停家数，完全没吃涨跌广度与温度；而跨日追踪正是用
    广度+温度下结论，两模块各说各话。本次补上广度权重：
      1) 广度比 = 涨家数/跌家数。<0.5 且温度<45 → 系统性失衡直接判强防守；
      2) 「修复/主升期」新增广度确认：广度>=0.9 或 温度>=55 才允许 50-70% 仓位，
         否则落混沌震荡(30-50%)，与跨日追踪口径对齐。
    返回 (emotion, advice_pos, advice_atk, advice_def, breadth_ratio)。
    """
    b = xgb if isinstance(xgb, dict) else {}
    temp = b.get('temperature', 50) or 50
    rise_n = b.get('rise_count', 0) or 0
    fall_n = b.get('fall_count', 0) or 0
    breadth = (rise_n / fall_n) if fall_n > 0 else (2.0 if rise_n > 0 else 1.0)

    emotion, advice_pos, advice_atk, advice_def = "", "", "", ""

    systemic_risk = (
        temp < 35
        or cy_pct <= -2.5
        or (sh_pct <= -1.5 and sz_pct <= -2.0)
        or (b.get('broken_ratio', 0) >= 35 and b.get('limit_down', 0) >= 10)
        or (breadth < 0.5 and temp < 45)  # 广度极端失衡(涨:跌<1:2)且偏冷 → 强防守
    )

    if systemic_risk:
        emotion = "🧊 【系统性杀跌/强防守期】"
        advice_pos = "0%-20% (空仓或极轻仓防守)"
        advice_atk = "只观察最强空间龙和逆势板块，不做盘中追高；若无先手，等待尾盘或次日确认。"
        advice_def = "指数与赚钱效应共振走弱，禁止重仓接力、禁止后排补涨、禁止把局部空间龙当成全市场主升。"
    elif b.get('limit_up', 0) > 80 and b.get('limit_down', 0) < 10 and b.get('premium', 0) > 2.0:
        emotion = "🔥 【高潮逼空期】"
        advice_pos = "70%-80%"
        advice_atk = "主线绝对龙头、最强分支的低位卡位换手板。"
        advice_def = "切忌盲目追高非核心概念的杂毛股，警惕高潮次日的分歧兑现。"
    elif b.get('limit_up', 0) < 30 and b.get('limit_down', 0) > 20 and b.get('premium', 0) < 0:
        emotion = "🧊 【退潮冰点期】"
        advice_pos = "0%-20% (空仓或极轻仓防守)"
        advice_atk = "耐心等待冰点恐慌释放后的低位新题材首板试错。"
        advice_def = "绝对防守！回避一切中高位连板的接力，防止A杀和核按钮。"
    elif (b.get('premium', 0) > 0 and b.get('limit_down', 0) < 15 and max_lb >= 4
          and (breadth >= 0.9 or temp >= 55)):
        # ADR-0035：新增广度确认——跌多涨少(广度<0.9)且温度<55 时，即便有高度龙头
        # 也不给 50-70% 仓位定性，落到混沌震荡(30-50%)，与跨日追踪口径对齐。
        emotion = "📈 【修复/主升期】"
        advice_pos = "50%-70%"
        advice_atk = "聚焦主线板块，寻找核心龙头的首阴低吸，或做板块内高低切的补涨二板。"
        advice_def = "远离被资金抛弃的老旧题材，只做资金介入深的核心线。"
    elif b.get('limit_down', 0) >= 15 and b.get('broken_ratio', 0) > 30:
        emotion = "🫥 【强分歧/退潮初期】"
        advice_pos = "20%-40%"
        advice_atk = "最高空间龙的深水区低吸博弈，或日内最强逆势板块的先锋股。"
        advice_def = "炸板率畸高，绝对禁止盘中半路追高或打跟风板，极易被闷杀。"
    else:
        emotion = "📊 【混沌震荡期】 (电风扇轮动)"
        advice_pos = "30%-50%"
        advice_atk = "有资金持续运作的低位独立逻辑个股，适合做网格T+0或低吸。"
        advice_def = "主线不清晰，坚决不做接力，缩短持股周期，见好就收。"

    return emotion, advice_pos, advice_atk, advice_def, breadth


def decide_exit(pct, mkt_temp=50, auction_ref=None):
    """10:40 持仓去弱留强的唯一决策矩阵（run_backtest 与报告共用，杜绝两套矩阵漂移）。
    入参 pct 为个股当日涨跌幅，mkt_temp 为大盘温度。返回 (action, advice)。

    【2026-10-21 新增第三入参 auction_ref=None · 集合竞价参考】
    为什么需要：原实现只看 10:40 那一瞬的涨幅，于是
      · 高开6%后一路阴跌到10:40只剩+0.5%  →  该跑
      · 平开后稳步走强到10:40涨+0.5%      →  该留
    这两只票在原逻辑眼里完全一样（pct 都是+0.5），但结论应该相反。
    集合竞价描述的正是「这只票今天有没有承接」，补上了这个缺失维度。

    调整范围（刻意保守，绝不越界）：
      · 只在 **-2% ≤ pct < 4%** 这一档生效（原本 HOLD/SELL_ALL 二选一，最需上下文）
      · pct ≥ 9.5%（锁仓晋级）与 pct ≤ -5%（止损）**完全不受影响** —— 强信号不该被竞价噪声干扰
      · 竞价 strong（真金白银进场）→ 原判 SELL_ALL/SELL_HALF 上调为 HOLD
      · 竞价 weak（虚高/无量/低开）→ 原判 HOLD 下调为 SELL_ALL
      · 无竞价参考（None / 文件缺失 / 字段异常）→ 行为与改造前**完全一致**

    向后兼容：第三入参有默认值，原有调用点（不传 auction_ref）行为不变。
    """
    if pct >= 9.5:
        return "HOLD", "👑 【锁仓晋级】资金做多合力极强，个股打出高度，坚决锁仓，封死涨停不动。"
    if pct >= 4.0:
        if mkt_temp >= 50:
            return "HOLD", "🟢 【持股锁仓】个股突破且承接力极佳，大盘温度优秀，持股待涨。"
        return "SELL_ALL", "🟡 【冲高全清】市场温度低，个股孤军难支，利用日内高点全清兑现。"
    if -2.0 <= pct < 4.0:
        if mkt_temp >= 55:
            _act, _adv = "SELL_HALF", "🟡 【减仓一半】承接良好但未封板，冲高抛一半锁定主动权，余仓观察。"
        else:
            _act, _adv = "SELL_ALL", "🔴 【全清出局】个股承接平庸且大盘寒冷，反弹冲高即全清保护筹码。"
        # 【2026-10-21】集合竞价参考微调（仅本档生效；无参考则原样返回）
        if _auction_ref is not None and auction_ref:
            try:
                _n_act, _note = _auction_ref.adjust_exit_action(_act, pct, auction_ref, mkt_temp)
                if _note:
                    _act = _n_act
                    # 【关键】动作变了，**文案必须同步重写** ——
                    # 否则会出现 action=HOLD 但 advice 开头还写「全清出局」的自相矛盾，
                    # 这正是复盘 P1-5「展示层文案必须与通道结论同源」记录的老毛病，不能重犯。
                    _adv = _AUCTION_ADVICE.get(_n_act, _adv) + "｜" + _note
            except Exception as _e:
                print("[auction] 卖出参考调整异常(保持原判定): %s" % _e)
        return _act, _adv
    if pct <= -5.0 or mkt_temp < EXIT_TEMP_GATE:
        return "SELL_ALL", "🚨 【果断清仓止损】深水区弱势阴跌，遭遇强烈负反馈，无条件清仓！"
    return "SELL_HALF", "🔴 【减仓一半】走势弱于预期缺乏护盘，反抽砍一半，尾盘不红则全清。"


def _sentiment_pos_factor(mkt_temp):
    """按大盘温度给本时段【总买入仓位】系数（占可用现金比例）。
    与报告 advice_pos 情绪分档同向：高潮重兵、主升适度、混沌轻仓、退潮极轻、冰点空仓。
    单票仍受 validate_order_plan_cash 的 25% 上限硬约束（2026-09-21 用户需求：
    手数随当天情绪浮动，替代原固定 15% 单票仓位）。"""
    t = mkt_temp or 50
    if t >= 75:
        return 0.60   # 高潮逼空期：总仓 60%
    if t >= 60:
        return 0.45   # 修复/主升期：总仓 45%
    if t >= 50:
        return 0.30   # 混沌震荡期：总仓 30%
    if t >= 40:
        return 0.15   # 退潮初期：总仓 15%
    return 0.0        # 冰点/系统性风险：不打


def _board_buy_items(stage, candidates, avail, mkt_temp, board):
    """打板通道（仅 09:25 早盘集合竞价）：多因子过线 + 个股今日仍强 → 生成挂涨停排单指令。"""
    items = []
    score, detail = board_score(board, mkt_temp)
    if score < BOARD_SCORE_GATE:
        return [{"type": "INFO", "name": "", "symbol": "",
                 "note": "打板综合分 %d < %d，梯队共振不足不打板｜%s" % (score, BOARD_SCORE_GATE, detail)}]
    if not candidates:
        return [{"type": "INFO", "name": "", "symbol": "",
                 "note": "打板综合分 %d 达标，但无候选标的" % score}]

    min_pct = BOARD_MIN_PCT.get(stage, 3.0)
    # 情绪仓位：单时段总买入仓 = 可用现金 × 温度系数；打板单时段最多 1 只 → 单票即总仓，仍受 25% 硬上限
    pos_factor = _sentiment_pos_factor(mkt_temp)
    if pos_factor <= 0:
        return [{"type": "INFO", "name": "", "symbol": "",
                 "note": "大盘温度 %.0f 冰点(<40)，情绪不支持打板，今日空手｜%s" % (mkt_temp, detail)}]
    per = min(avail * pos_factor, avail * 0.25)
    picked = 0
    weak = []
    for c in candidates:
        if picked >= BOARD_MAX_PICKS:
            break
        cn = c.get("name") or c.get("stock_chi_name") or ""
        cs = c.get("symbol") or c.get("code") or ""
        if not cs or is_suspended(cn):
            continue
        info = get_stock_detailed_sina(cn)
        price = info.get("current_price", 0.0) if info else 0.0
        cpct = info.get("pct", 0.0) if info else 0.0
        if price <= 0:
            continue
        # 一字板流动性过滤（2026-09-28 ADR-0035 补齐；09-14/15/21/22 连续复现）：
        # 开盘即封死涨停（open==涨停价）时买单排在巨量封单之后根本无法成交
        # （09-21 华瓷股份：封单 2.79 亿 / 成交 0.51 亿，报告判「一字买不进」却仍派单）。
        # 注意：盘中封板（非一字开盘）仍保留排队打板 —— 这正是本通道的打法。
        try:
            _lu_px, _ld_px = get_stock_limits(cs, cn, info.get("yesterday_close", price))
            if info.get("open_price", 0) >= _lu_px - 1e-6 and price >= _lu_px - 1e-6:
                items.append({"type": "INFO", "name": cn, "symbol": cs, "price": round(price, 2),
                              "buy_shares": 0, "buy_lots": 0, "amount": 0.0, "board_score": score,
                              "note": "一字涨停开盘(open=%.2f=涨停价%.2f)，流动性锁死跳过派单" % (
                                  info.get("open_price", 0.0), _lu_px)})
                continue
        except Exception as e:
            print(f"[WARN] 静默异常已显式化 @board_yizi_filter: {e}")
        # 「今天依然强势」硬要求：达不到实时涨幅门槛的不打
        if cpct < min_pct:
            weak.append("%s(%+.1f%%)" % (cn, cpct))
            continue
        buy_lots = _afford_lots(per, price, cs)
        if buy_lots * price * _lot_for(cs) > avail:
            buy_lots = 0
        if buy_lots <= 0:
            continue
        buy_shares = _to_shares(buy_lots, cs)
        items.append({"type": "BUY_LIMIT", "name": cn, "symbol": cs, "price": round(price, 2),
                      "buy_shares": buy_shares, "buy_lots": buy_lots,
                      "amount": round(buy_shares * price, 2),
                      "board_score": score,
                      "note": "%s 挂涨停排单打板｜综合分%d 实时%+.1f%%｜封单>1亿续排，流失30%%撤单" % (
                          stage, score, cpct)})
        picked += 1

    if not items:
        tail = ("；候选今日不够强：" + "、".join(weak[:4])) if weak else ""
        items = [{"type": "INFO", "name": "", "symbol": "",
                  "note": "打板综合分 %d 达标，但无可执行标的%s" % (score, tail)}]
    return items


# ============================================================================
# ADR-0040：复盘闸门接入（2026-10-06，用户需求驱动的闭环改造）
# ----------------------------------------------------------------------------
# 【问题】ADR-0039 的复盘模式解决了「看清盘面」，但结论只渲染在看板上，对第二天
#   的买入通道零约束 —— 用户原话：「复盘决定了第二天啥可以买……而不是还和以前一样
#   简单的功能堆砌」。择时闸门从未真正约束过买入，是「追高亏损」的结构性根因。
#
# 【做法】复盘侧（market-monitor/review_gate.py）盘后把六引擎结论翻译成
#   `reports/trade_gate.json`（写明 asof = 次日）；本调度器在**候选定稿处**
#   （与上方「系统性风险闸门」同一挂载点）读取并执行：
#     硬阻断：退潮/冰点/亏钱效应≥4 → 清空买入候选（方法论：退潮期空仓）
#     冷却名单：追高/超配违规标的 → N 个交易日内不再派单
#     风格闸门：board_days > max_board_days → 剔除（不接力高位连板）
#     只数上限：分歧段最多 2 只、冰点 1 只
#     仓位上限：可用额度 = cap%×总权益 − 持仓市值，缺口为负则禁止加仓
#     风向标打标：命中复盘 8 只风向标 → 候选置顶标记「复盘重点」
#
# 【为什么是内联镜像而不是 import】
#   本文件在盘中热路径上，subprocess/import 会带来秒级延迟与 akshare 导入开销；
#   且两个仓库不同目录。因此两边共用**同一份数据契约**（字段名 + 规则语义），
#   规则改动必须同时改两处 —— review_gate.py --selftest 会打印需同步的规则清单。
#
# 【安全设计（铁律）】
#   1) fail-open：文件缺失 / asof≠今日 / 格式错 / 任何异常 → 一律原样放行，
#      行为与接入前完全一致。最坏情况等于没有这个功能。
#   2) 逃生阀：复盘判防守而次日实时温度 ≥ REVIEW_GATE_ESCAPE_TEMP → 硬阻断降级为
#      警告（防踏空整段反弹），降级事实写入留痕，绝不静默。
#   3) 只读：不修改回测库；唯一写入对象是 trade_gate_applied.json（应用留痕）。
# ============================================================================
# ============================================================================
# 【2026-10-21】集合竞价参考因子（auction_ref.py）
# ---------------------------------------------------------------------------
# 背景：decide_exit(pct, mkt_temp) 只看 10:40 那一瞬的涨幅，
#       完全看不见 09:25 集合竞价到 10:40 之间这只票发生了什么。
#       而 09:25 的竞价数据（高开幅度/竞价量能）算出来了却没人消费。
# 做法：09:25 落盘竞价画像 → 10:40 卖出判定 / 13:40·14:40 买入打分 消费它。
# 铁律：① 只做参考因子，不改卖出时点（10:40 仍是唯一卖点）
#       ② 全程 fail-open：数据缺失 → 行为与改造前完全一致
# ============================================================================
try:
    import auction_ref as _auction_ref
except Exception:            # 模块不可用时退化为「永远无参考」
    _auction_ref = None

# 【2026-10-21】竞价参考调整后的动作 → 对应文案（保证 advice 与 action 永远同源）
_AUCTION_ADVICE = {
    "HOLD": "🟢 【竞价参考·持股锁仓】竞价承接真实，虽未封板但有资金托底，持股观察。",
    "SELL_ALL": "🔴 【竞价参考·全清出局】竞价虚高/承接已弱，反弹冲高即全清保护筹码。",
    "SELL_HALF": "🟡 【竞价参考·减仓一半】承接转弱，冲高抛一半锁定主动权，余仓观察。",
}

REVIEW_GATE_ENABLED     = True
REVIEW_GATE_FILE        = os.path.join(REPORTS_DIR, "trade_gate.json")
# 【2026-10-21 新增】今日生效闸门副本。
# 背景：闸门文件只有一份，而"约束今日"与"约束明日"两份需并存 ——
# 10-21 盘前跑 --auto 生成 asof=10-22 的闸门，会覆盖 10-20 盘后生成的 asof=10-21 闸门，
# 导致当天盘中闸门直接 fail-open（实测 10-21 早盘即出现此情形）。
# 现在 review_gate.auto_build 会把"约束今日"的那份另存到此文件，调度器优先读它。
REVIEW_GATE_CURRENT     = os.path.join(REPORTS_DIR, "trade_gate_current.json")
REVIEW_GATE_APPLIED     = os.path.join(REPORTS_DIR, "trade_gate_applied.json")
REVIEW_GATE_ESCAPE_TEMP = 60      # 逃生阀阈值：次日实时情绪温度 ≥ 此值 → 硬阻断降级

_ACTIVE_REVIEW_GATE = None        # 本进程当前生效闸门（每个时段进程只跑一次，无并发）
_REVIEW_GATE_META   = {}
_REVIEW_GATE_DROPS  = []


def _rg_norm(s):
    """6 位纯数字代码（与 review_gate._norm_code 同口径）。
    卖出单用 000066、操作池候选用 000066.SZ，不归一必然匹配失败。"""
    return "".join(ch for ch in str(s or "") if ch.isdigit())


def _rg_int(v, d=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return d


def _rg_float(v, d=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


# ============================================================================
# GJD 国家队 ETF 闸门修正（ADR-0049，2026-11-06）
#
# 用户需求：「把 GJD 跟踪器（看 ETF 是否放量买入）加入投资推票条件」。
#
# 为什么只改闸门、不改排序分：
#   GJD 是**池级**信号（20 只宽基 ETF 的合计放量倍数 + 主动净买入）。池级信号若做成
#   个股排序分的加分项，等于给池内每只票同加一个常数 —— 排序结果一个都不变，
#   代码改了、报告里也能写「已考虑国家队」，但实际决策零影响，是最典型的假改造。
#   它对决策真正有意义的落点只有一处：**本时段总仓上限 / 最多几只**。所以这里把
#   梯队翻译成 position_cap_pct 的 pp 修正和 max_picks 的只数修正。
#
# 方向（宁可不加，也不因「国家队买了」就放开）：
#   强力买入 +6pp/+1 只 · 明显买入 +4pp · 常态/温和 0 · 缩量 -2pp · 放量出货 -8pp/-1 只
#   ——「放量出货」罚得比「缩量」重，因为放量却在净卖出是高位派发，比国家队缺席更危险。
#
# 安全设计（三条红线）：
#   ① 取数失败/无基准/盘前 → effect=unknown → **不加不减**（fail-open）。绝不让网络
#      抖动决定实盘仓位。
#   ② 一次运行只打一次腾讯（4 分钟缓存），调度跑在派单关键路径上，不能被 24 只 ETF 拖住。
#   ③ 原始闸门值另存 gate["gjd"]，报告里同时显示「原值 → 调整后」，可追溯、可回滚。
# ============================================================================
GJD_GATE_ENABLED = os.environ.get("MA_GJD_GATE", "1") != "0"
GJD_DIR = MARKET_MONITOR_DIR
_GJD_TTL = 240.0
_GJD_CACHE = {"at": 0.0, "snap": None, "err": ""}
_GJD_LOGGED = set()      # 同一进程内同样的修正只打印一次（load_review_gate 每次运行会调 3~4 次）


def _gjd_snapshot(force=False):
    """取 GJD 快照（带进程内缓存）。永不抛异常，失败返回 ok=False 的快照。"""
    now = time.time()
    if (not force) and _GJD_CACHE["snap"] and (now - _GJD_CACHE["at"]) < _GJD_TTL:
        return _GJD_CACHE["snap"]
    snap = {"ok": False, "reason": "GJD 未接入", "effect": {"pos_delta": 0.0, "pick_delta": 0,
                                                           "dir": "hold", "why": "未接入"}}
    try:
        if GJD_DIR not in sys.path:
            sys.path.insert(0, GJD_DIR)
        import gjd_etf as _gjd          # 延迟导入：GJD 模块异常不该影响调度器启动
        snap = _gjd.quick_snapshot(timeout=6)
    except Exception as e:              # noqa: BLE001
        _GJD_CACHE["err"] = "%s: %s" % (type(e).__name__, e)
        print("[gjd] 取数失败(闸门不加不减): %s" % _GJD_CACHE["err"])
    _GJD_CACHE["snap"] = snap
    _GJD_CACHE["at"] = now
    return snap


def _gjd_apply_to_gate(gate):
    """把 GJD 梯队修正量并进闸门 dict（原地 + 留痕）。返回 gate；任何异常原样返回。"""
    if not GJD_GATE_ENABLED or not isinstance(gate, dict):
        return gate
    if gate.get("gjd"):                 # 已应用过（同一 dict 反复传入）→ 幂等
        return gate
    try:
        snap = _gjd_snapshot()
        eff = (snap or {}).get("effect") or {}
        core = (snap or {}).get("core") or {}
        raw_cap = _rg_float(gate.get("position_cap_pct"))
        raw_picks = _rg_int(gate.get("max_picks"))
        rec = {
            "ok": bool((snap or {}).get("ok")),
            "reason": (snap or {}).get("reason") or "",
            "session": (snap or {}).get("session"),
            "ratio": core.get("ratio"),
            "net_buy_yi": core.get("net_buy_yi"),
            "level": core.get("level"),
            "level_code": core.get("level_code"),
            "dir": eff.get("dir") or "hold",
            "why": eff.get("why") or "",
            "pos_delta": eff.get("pos_delta") or 0.0,
            "pick_delta": eff.get("pick_delta") or 0,
            "raw_position_cap_pct": raw_cap,
            "raw_max_picks": raw_picks,
            "applied": False,
        }
        if eff.get("dir") in ("loosen", "tighten"):
            if raw_cap is not None:
                new_cap = max(0.0, min(100.0, raw_cap + float(eff.get("pos_delta") or 0.0)))
                gate["position_cap_pct"] = round(new_cap, 1)
                rec["new_position_cap_pct"] = gate["position_cap_pct"]
                rec["applied"] = True
            if raw_picks:
                # 上限保护：单次 GJD 修正最多 ±2 只。当前映射最大 ±1，这里是防将来改映射
                # 时不小心把只数放大到离谱（如 +9 → 12 只），也防外部 JSON 被改坏。
                _d = max(-2, min(2, int(eff.get("pick_delta") or 0)))
                new_picks = max(1, int(raw_picks) + _d)
                if new_picks != raw_picks:
                    gate["max_picks"] = new_picks
                    rec["new_max_picks"] = new_picks
                    rec["applied"] = True
        gate["gjd"] = rec
        _logkey = (rec["dir"], raw_cap, rec.get("new_position_cap_pct"),
                   raw_picks, rec.get("new_max_picks"))
        if rec["applied"] and _logkey not in _GJD_LOGGED:
            _GJD_LOGGED.add(_logkey)
            print("[gjd] 闸门修正 %s：仓位 %s%% → %s%% · 只数 %s → %s（core %.2f× / 净买 %s 亿 / %s）"
                  % (rec["dir"], raw_cap, rec.get("new_position_cap_pct", raw_cap),
                     raw_picks, rec.get("new_max_picks", raw_picks),
                     rec["ratio"] if rec["ratio"] is not None else -1,
                     rec["net_buy_yi"], rec["level"]))
        elif not rec["ok"] and ("noop", rec["reason"]) not in _GJD_LOGGED:
            _GJD_LOGGED.add(("noop", rec["reason"]))
            print("[gjd] 闸门不加不减：%s" % (rec["reason"] or "无数据"))
    except Exception as e:              # noqa: BLE001 —— 闸门路径必须 fail-open
        print("[gjd] 闸门修正异常(不干预): %s: %s" % (type(e).__name__, e))
        gate.setdefault("gjd", {"ok": False, "applied": False,
                                "reason": "%s: %s" % (type(e).__name__, e)})
    return gate


def load_review_gate(today_str=None):
    """读取当日生效的复盘闸门。过期 / 缺失 / 关闭 / 任何异常 → None（调用方必须 fail-open）。"""
    if not REVIEW_GATE_ENABLED:

        return None
    today = today_str or datetime.now().strftime("%Y-%m-%d")
    # 【2026-10-21 修】优先读"今日生效闸门"副本。
    # 闸门文件只有一份，"约束今日"与"约束明日"两份需并存 ——
    # 盘前跑 --auto 会把明日闸门写进 trade_gate.json，覆盖掉约束今日的那份，
    # 导致当天盘中闸门直接 fail-open（实测 10-21 早盘即如此）。
    # 现在 auto_build 会把约束今日的闸门另存到 trade_gate_current.json，本函数优先读它。
    for _path in (REVIEW_GATE_CURRENT, REVIEW_GATE_FILE):
        try:
            with open(_path, encoding="utf-8") as f:
                g = json.load(f)
        except Exception:
            continue
        if not isinstance(g, dict) or not g.get("asof"):
            continue
        if str(g.get("asof"))[:10] == today:
            # ADR-0049：所有调用方都从这里拿闸门 → GJD 修正在此统一生效，
            # 保证「报告显示的仓位」与「实际派单用的仓位」永远是同一个数。
            return _gjd_apply_to_gate(g)
    # 两个文件都没有约束今日的闸门 → 报出实际读到的是哪个（不静默）
    try:
        with open(REVIEW_GATE_FILE, encoding="utf-8") as f:
            _g = json.load(f)
        _a = str((_g or {}).get("asof"))[:10]
    except Exception:
        _a = "无"
    print("[review_gate] 闸门 asof=%s ≠ 今日 %s → 作废（fail-open，不干预）" % (_a, today))
    return None


def _gate_avail_cap(avail):
    """ADR-0040：复盘仓位上限 → 本次买入可用额度上限。无闸门/异常 → 原样返回。

    【2026-10-21 修 P1-4 · 修双重乘算】
    原实现返回「闸门额度金额」（= 段上限% × 权益 − 已持仓），调用方直接拿它
    乘以情绪单笔系数 → **段上限被乘了两次**：
      段上限 40%×权益 = 197,116，再 × 情绪系数 15% = 29,574 = 权益的 6%，
      而闸门单票上限是 15% —— 额度利用率仅 12.5%，本可以下的单被腰斩。
    现在区分两种语义，用 `_gate_cap_is_total()` 判定：
      · 闸门额度是「本时段总额」→ 调用方应**除以只数**得到单笔额度
      · 闸门额度是「单票上限」→ 调用方沿用乘以情绪系数的旧路径
    本函数保持返回金额不变（其它调用点不受影响），仅新增 `_gate_avail_cap_total()` 供
    13:40/14:40 的单笔额度计算使用。
    """
    try:
        ap = (_REVIEW_GATE_META or {}).get("avail_cap")
        if ap is None:
            return avail
        return max(0.0, min(float(avail), float(ap)))
    except Exception:
        return avail


def _gate_cap_is_total():
    """判断当前闸门的 position_cap_pct 语义是「本时段总仓」还是「单票上限」。

    闸门 schema（review_gate.py）：
      position_cap_pct   = 本时段**总仓**上限（%）
      single_pos_cap_pct = **单票**上限（%）
      max_picks          = 本时段最多只数
    因此额度 = position_cap_pct% × 权益 − 已持仓，是**总额**，
    单笔额度应 = 总额 ÷ max_picks（并再受 single_pos_cap_pct 约束）。
    无闸门时返回 False（沿用旧路径，由情绪系数决定仓位）。
    """
    try:
        g = _ACTIVE_REVIEW_GATE
        if not g:
            return False
        if _rg_float(g.get("position_cap_pct")) is None:
            return False
        return True
    except Exception:
        return False


def _gate_single_budget(avail, n_picks):
    """【2026-10-21 修 P1-4】算出「单笔可下金额」。

    语义修正（对照复盘 P1-4：额度利用率仅 12.5%，单笔 5% 与段上限 40% 严重脱节）：
      旧：per = 闸门额度 × 情绪系数  → 段上限被乘两次，只用上 6% 权益
      新：per = 闸门总额 ÷ 只数       → 分歧段 40% ÷ 3 只 ≈ 13.3%/只（再受单票上限约束）
    无闸门时退回旧路径（情绪系数 × 可用现金），保持历史行为不变。
    """
    try:
        if not _gate_cap_is_total():
            return None
        g = _ACTIVE_REVIEW_GATE or {}
        picks = max(1, _rg_int(g.get("max_picks")) or 1)
        n = max(1, int(n_picks or picks))
        # 只数取「实际候选数」与「闸门允许只数」的较小者，避免额度被摊薄到不存在的票上
        eff_n = max(1, min(n, picks))
        per = float(avail) / eff_n
        # 单票上限硬约束（闸门 single_pos_cap_pct 优先于段上限÷只数）
        eq = _rg_float((_GATE_EQ_CACHE or {}).get("total_equity"))
        single_pct = _rg_float(g.get("single_pos_cap_pct"))
        if eq and single_pct:
            per = min(per, single_pct / 100.0 * eq)
        return per
    except Exception as e:
        print("[gate_budget] 单笔额度计算异常(退回旧路径): %s" % e)
        return None


# 权益缓存（供 _gate_single_budget 计算单票上限；由 build_action_items 写入）
_GATE_EQ_CACHE = {}


def _gate_plan_block():
    """订单计划里的 review_gate 段（留痕，供工作台/报告展示）。"""
    g = _ACTIVE_REVIEW_GATE
    return {
        "active": bool(g),
        "stage": (g or {}).get("stage"),
        "asof": (g or {}).get("asof"),
        "mode": (g or {}).get("mode"),
        "hard_block": bool((g or {}).get("hard_block")),
        "position_cap_pct": (g or {}).get("position_cap_pct"),
        "summary": (("放行 %d 只 / 剔除 %d 只" % (
            len(_REVIEW_GATE_META.get("kept_names") or []), len(_REVIEW_GATE_DROPS)))
            + (("；" + "；".join(_REVIEW_GATE_META.get("notes") or []))
               if _REVIEW_GATE_META.get("notes") else "")) if g else "",
        "kept": _REVIEW_GATE_META.get("kept_names") or [],
        "drops": _REVIEW_GATE_DROPS,
        "meta": {k: v for k, v in (_REVIEW_GATE_META or {}).items() if k != "kept_names"},
    }


def _gate_info_items():
    """把闸门留痕转成 INFO 操作项 → 报告/闪卡与订单计划口径一致（用户看得见为什么没买）。"""
    if not _ACTIVE_REVIEW_GATE:
        return []
    items = []
    m = _REVIEW_GATE_META or {}
    # 未执行卖出提醒置顶：复盘纪律检查器判「漏执行」时，次日买入时段先提醒处理旧单，
    # 避免"旧仓该卖没卖、又开新仓"（历史累计代价已超 3,000 元）。
    for ps in (_ACTIVE_REVIEW_GATE.get("pending_sell") or []):
        items.append({"type": "INFO", "name": ps.get("name", ""), "symbol": ps.get("symbol", ""),
                      "note": "⚠️ 昨日卖出建议未执行：%s %s —— %s"
                              % (ps.get("name"), ps.get("action") or "",
                                 (ps.get("text") or "")[:80])})
    if m.get("only_note"):
        items.append({"type": "INFO", "name": "", "symbol": "", "note": m["only_note"]})
    for d in _REVIEW_GATE_DROPS:
        items.append({"type": "INFO", "name": d.get("name", ""), "symbol": d.get("symbol", ""),
                      "note": "跳过：%s" % d.get("reason", "")})
    if _REVIEW_GATE_DROPS and not m.get("only_note"):
        items.insert(0, {"type": "INFO", "name": "", "symbol": "",
                         "note": "🔒 复盘闸门（%s段）适用：%d 只候选被约束，详见工作台「复盘模式」"
                                 % ((_ACTIVE_REVIEW_GATE or {}).get("stage"), len(_REVIEW_GATE_DROPS))})
    return items


def _write_review_gate_applied(stage, kept, drops, meta):
    """应用留痕（供 workbench 展示 + 盘后 effectiveness 度量）。合并当日各时段。"""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            with open(REVIEW_GATE_APPLIED, encoding="utf-8") as f:
                ap = json.load(f)
            if not isinstance(ap, dict) or ap.get("date") != today:
                ap = {}
        except Exception:
            ap = {}
        ap.setdefault("date", today)
        ap.setdefault("stages", [])
        g = _ACTIVE_REVIEW_GATE or {}
        ap["asof"] = g.get("asof")
        ap["stage"] = g.get("stage")
        ap["applied_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rec = {"stage": stage, "at": datetime.now().strftime("%H:%M:%S"),
               "kept": [{"name": c.get("name"), "symbol": c.get("symbol"),
                         "board_days": c.get("board_days")} for c in (kept or [])],
               "drops": drops or [], "meta": meta or {}}
        ap["stages"] = [s for s in ap["stages"] if s.get("stage") != stage] + [rec]
        tmp = REVIEW_GATE_APPLIED + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(ap, f, ensure_ascii=False, indent=2)
        os.replace(tmp, REVIEW_GATE_APPLIED)
    except Exception as e:
        print("[review_gate] 留痕写入失败(不影响主流程): %s" % e)


# ============================================================================
# 【2026-10-21 修 P0-2】派单距高硬判据（连续 4 次建议 · 伪强势 n=10）
# ---------------------------------------------------------------------------
# 背景（10-16~10-20 连续实证）：
#   荣丰控股 10-16 派单距高 −9.05%（历史最大偏离）→ 次日跌停
#   万丰奥威 10-20 13:40 距高 −6.26%、东方嘉盛 −8.40% → 均靠「均价闸门碰巧」拦下
#   也就是说：现有规则**运气替代了规则** —— 拦对了，但没拦对原因。
# 判据：派单价 ≤ 当日最高 × 0.995 才放行，违反者降级为「观察」（不派单）。
#
# 【为什么先 shadow 而不直接 enforce】
#   复盘原建议：「先 shadow 记录 5 个交易日再转 enforce」。
#   本实现遵循该路径：默认 SHADOW（只记录不拦截），满足下列任一条件自动转 ENFORCE：
#     · 影子期已满 N 个交易日（默认 5）且影子命中数 ≥ MIN_SHADOW_HITS
#     · 影子期已满 N 个交易日且影子命中数为 0（说明该判据无区分度，继续 shadow 无意义）
#   强制 enforce 可通过 REVIEW_DIST_HIGH_MODE = "enforce" 打开。
#   影子判定结果写入 trade_gate_applied.json（shadow_would_drop），可在工作台查看。
# ============================================================================
REVIEW_DIST_HIGH_ENABLED    = True
REVIEW_DIST_HIGH_RATIO      = 0.995   # 派单价必须 ≥ 当日最高 × 0.995
REVIEW_DIST_HIGH_MODE       = "shadow"  # "shadow" | "enforce"
REVIEW_DIST_HIGH_SHADOW_DAYS = 5      # 影子期交易日数
REVIEW_DIST_HIGH_MIN_HITS   = 2        # 影子期至少命中多少次才值得 enforce
REVIEW_DIST_HIGH_FILE       = os.path.join(REPORTS_DIR, "dist_high_shadow.json")

_DIST_HIGH_STATE = {"days": 0, "hits": 0, "misses": 0, "samples": [], "last_date": ""}


def _dist_high_load_state():
    global _DIST_HIGH_STATE
    try:
        if os.path.exists(REVIEW_DIST_HIGH_FILE):
            with open(REVIEW_DIST_HIGH_FILE, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                _DIST_HIGH_STATE = {
                    "days": int(d.get("days") or 0),
                    "hits": int(d.get("hits") or 0),
                    "misses": int(d.get("misses") or 0),
                    "samples": list(d.get("samples") or [])[-30:],
                    "last_date": str(d.get("last_date") or ""),
                }
    except Exception:
        pass
    return _DIST_HIGH_STATE


def _dist_high_save_state(st):
    global _DIST_HIGH_STATE
    _DIST_HIGH_STATE = st
    try:
        with open(REVIEW_DIST_HIGH_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("[dist_high] 影子状态落盘失败(不影响主流程): %s" % e)


def _dist_high_price_and_peak(c):
    """取候选的「派单价」与「当日最高」→ (price, high, ok)。

    派单价口径：优先用候选自带 price（订单计划最终采用的那个价），
    退化到 quote 快照的 current_price；当日最高取 quote 快照的 high。
    取不到 → ok=False（fail-open，不做判定）。
    """
    try:
        name = c.get("name") or c.get("stock_chi_name") or ""
        sym = c.get("symbol") or c.get("code") or ""
        price = c.get("price") or c.get("suggest_price") or 0
        high = c.get("high") or c.get("day_high") or 0
        if not price or not high:
            info = get_stock_detailed_sina(name) if name else None
            if not info and sym:
                info = get_stock_detailed_tencent(sym) if _rg_int(1) else None
            if info:
                price = price or info.get("current_price") or 0
                high = high or info.get("high") or 0
        price = float(price or 0)
        high = float(high or 0)
        if price <= 0 or high <= 0:
            return 0.0, 0.0, False
        return price, high, True
    except Exception:
        return 0.0, 0.0, False


def _apply_dist_high_guard(stage, candidates):
    """【P0-2】派单距高判据：派单价距当日最高太远 → 伪强势，降级不派单。

    返回 (kept, would_drop, mode_used)。
    - shadow 模式：kept 原样返回，would_drop 记录命中的票（不拦截）
    - enforce 模式：命中的票从 kept 中剔除
    任一异常 → fail-open（原样放行），绝不阻断主流程。
    """
    cands = list(candidates or [])
    if not REVIEW_DIST_HIGH_ENABLED or not cands:
        return cands, [], REVIEW_DIST_HIGH_MODE
    try:
        st = _dist_high_load_state()
        today = datetime.now().strftime("%Y-%m-%d")
        would = []
        for c in cands:
            price, high, ok = _dist_high_price_and_peak(c)
            if not ok:
                continue
            floor = high * REVIEW_DIST_HIGH_RATIO
            if price < floor:
                dist = (price / high - 1.0) * 100 if high > 0 else 0.0
                would.append({
                    "name": c.get("name", ""), "symbol": c.get("symbol", ""),
                    "reason": "伪强势（距当日最高 %.2f%%，超过 %.1f%% 阈值）：派单价 %.2f < 当日最高 %.2f×%.3f"
                              % (dist, (1 - REVIEW_DIST_HIGH_RATIO) * 100,
                                 price, high, REVIEW_DIST_HIGH_RATIO),
                    "dist_pct": round(dist, 2),
                })

        # ---- 影子状态推进 ----
        if st.get("last_date") != today:
            st["days"] = int(st.get("days") or 0) + 1
            st["last_date"] = today
        st["hits"] = int(st.get("hits") or 0) + len(would)
        st["misses"] = int(st.get("misses") or 0) + max(0, len(cands) - len(would))
        for w in would:
            st["samples"].append({"date": today, "stage": stage,
                                   "name": w["name"], "dist_pct": w["dist_pct"]})
        st["samples"] = st["samples"][-30:]
        _dist_high_save_state(st)

        # ---- 模式判定：影子期满后自动决定是否 enforce ----
        mode = REVIEW_DIST_HIGH_MODE
        days = int(st.get("days") or 0)
        hits = int(st.get("hits") or 0)
        if mode == "shadow" and days >= REVIEW_DIST_HIGH_SHADOW_DAYS:
            if hits >= REVIEW_DIST_HIGH_MIN_HITS:
                mode = "enforce"
                print("[dist_high] 影子期 %d 个交易日已满，命中 %d 次 → 自动转 enforce" % (days, hits))
            else:
                print("[dist_high] 影子期 %d 个交易日已满，命中 %d 次（<%d）→ 该判据无区分度，保持 shadow"
                      % (days, hits, REVIEW_DIST_HIGH_MIN_HITS))
        if mode == "enforce" and would:
            drop_keys = {w["symbol"] or w["name"] for w in would}
            kept = [c for c in cands if (c.get("symbol") or c.get("name")) not in drop_keys]
            print("[dist_high] enforce 模式剔除 %d 只伪强势：%s"
                  % (len(would), ", ".join(w["name"] for w in would)))
            return kept, would, mode
        if would:
            print("[dist_high] shadow 模式记录：%d 只距高超阈值（不拦截）%s"
                  % (len(would), ", ".join("%s %.2f%%" % (w["name"], w["dist_pct"]) for w in would)))
        return cands, would, mode
    except Exception as e:
        print("[dist_high] 接入点异常(放行): %s" % e)
        return cands, [], "fail-open"


def _apply_review_gate(stage, candidates, xgb=None, db=None):
    """把复盘闸门套到买入候选上 → 过滤后的候选列表（fail-open：异常原样返回）。"""
    global _ACTIVE_REVIEW_GATE, _REVIEW_GATE_META, _REVIEW_GATE_DROPS
    cands = list(candidates or [])
    gate = load_review_gate()
    if not gate:
        return cands
    try:
        if str(gate.get("mode") or "enforce") != "enforce":
            _ACTIVE_REVIEW_GATE = gate
            _REVIEW_GATE_META = {"applied": False, "notes": ["shadow 模式：仅记录判定，不拦截"],
                                 "only_note": "🔒 复盘闸门处于 shadow 模式（只记录不拦截），"
                                              "本次判定已留痕待校准"}
            kept_n = cands
            _REVIEW_GATE_DROPS = []
            try:
                _, would = [], []
                cooling = {_rg_norm(x.get("symbol") or x.get("code") or x.get("name")): x
                           for x in (gate.get("cooling") or [])}
                max_bd = _rg_int(gate.get("max_board_days"))
                for c in cands:
                    k = _rg_norm(c.get("symbol") or c.get("code") or c.get("name"))
                    bd = _rg_int(c.get("board_days") or c.get("lianban"))
                    if k in cooling:
                        would.append({"name": c.get("name", ""), "symbol": c.get("symbol", ""),
                                      "reason": "shadow：冷却名单"})
                    elif max_bd and bd > max_bd:
                        would.append({"name": c.get("name", ""), "symbol": c.get("symbol", ""),
                                      "reason": "shadow：%d 板 > 上限 %d 板" % (bd, max_bd)})
                _REVIEW_GATE_META["shadow_would_drop"] = would
                print("[review_gate] shadow 模式下本会剔除 %d 只" % len(would))
            except Exception:
                pass
            _write_review_gate_applied(stage, kept_n, [], _REVIEW_GATE_META)
            return cands

        drops = []
        hard = bool(gate.get("hard_block"))
        temp = _rg_float(xgb.get("temperature")) if isinstance(xgb, dict) else None
        notes = []
        if hard and temp is not None and temp >= REVIEW_GATE_ESCAPE_TEMP:
            hard = False
            notes.append("逃生阀：今日实时情绪温度 %.1f ≥ %d，与复盘判定的「%s」背离 → 硬阻断降级为警告"
                         % (temp, REVIEW_GATE_ESCAPE_TEMP, gate.get("stage")))
            print("[review_gate] " + notes[-1])

        if hard:
            why = gate.get("hard_block_reason") or gate.get("reason") or "复盘判定禁止开新仓"
            for c in cands:
                drops.append({"name": c.get("name", ""), "symbol": c.get("symbol", ""),
                              "reason": "复盘硬阻断：%s" % why})
            kept = []
        else:
            cooling = {_rg_norm(x.get("symbol") or x.get("code") or x.get("name")): x
                       for x in (gate.get("cooling") or [])}
            focus = {_rg_norm(x.get("symbol") or x.get("code") or x.get("name")): x
                     for x in (gate.get("focus") or [])}
            max_bd = _rg_int(gate.get("max_board_days"))
            kept = []
            for c in cands:
                key = _rg_norm(c.get("symbol") or c.get("code") or c.get("name"))
                cd = cooling.get(key)
                if cd:
                    drops.append({"name": c.get("name", ""), "symbol": c.get("symbol", ""),
                                  "reason": "复盘冷却名单（至 %s）：%s"
                                            % (cd.get("until"), cd.get("reason"))})
                    continue
                bd = _rg_int(c.get("board_days") or c.get("lianban"))
                if max_bd and bd > max_bd:
                    drops.append({"name": c.get("name", ""), "symbol": c.get("symbol", ""),
                                  "reason": "复盘风格闸门：%d 连板 > 「%s」段允许的最高 %d 板（不接力高位连板）"
                                            % (bd, gate.get("stage"), max_bd)})
                    continue
                f = focus.get(key)
                if f:
                    c["_review_focus"] = True
                    c["_review_focus_label"] = "复盘重点·%s" % (f.get("kind_label") or "风向标")
                    notes.append("风向标命中：%s（%s）" % (c.get("name"), f.get("kind_label") or ""))
                kept.append(c)
            picks = _rg_int(gate.get("max_picks"))
            if picks and len(kept) > picks:
                for c in kept[picks:]:
                    drops.append({"name": c.get("name", ""), "symbol": c.get("symbol", ""),
                                  "reason": "复盘只数上限：「%s」段本时段最多 %d 只"
                                            % (gate.get("stage"), picks)})
                kept = kept[:picks]

        # ---- 总仓位上限：可用额度 = cap% × 总权益 − 持仓市值 ----
        meta = {"applied": True, "notes": notes, "avail_cap": None}
        cap = _rg_float(gate.get("position_cap_pct"))
        eq = _rg_float((db or {}).get("total_equity")) if isinstance(db, dict) else None
        if cap is not None and eq:
            cash = _rg_float((db or {}).get("available_cash"), 0.0) or 0.0
            held = max(eq - cash, 0.0)
            allowance = cap / 100.0 * eq - held
            meta["position_now_pct"] = round(held / eq * 100, 2) if eq else None
            meta["allowance"] = round(allowance, 2)
            if allowance <= 0 and kept:
                for c in kept:
                    drops.append({"name": c.get("name", ""), "symbol": c.get("symbol", ""),
                                  "reason": "复盘仓位上限：当前仓位 %.1f%% 已达/超 %s 段上限 %g%%，"
                                            "买入额度缺口 ¥%.0f，需先减仓再加仓"
                                            % (meta["position_now_pct"] or 0.0,
                                               gate.get("stage"), cap, abs(allowance))})
                kept = []
            else:
                meta["avail_cap"] = round(min(allowance, cash), 2)

        _ACTIVE_REVIEW_GATE = gate
        _REVIEW_GATE_DROPS = drops
        _REVIEW_GATE_META = meta
        _REVIEW_GATE_META["kept_names"] = [c.get("name") for c in kept]
        if drops:
            _REVIEW_GATE_META["only_note"] = (
                "🔒 复盘闸门（%s段 · 上限 %g%%）约束了 %d 只候选：%s"
                % (gate.get("stage"), cap or 0, len(drops),
                   "；".join("%s→%s" % (d["name"] or "?", (d.get("reason") or "")[:34]) for d in drops[:3])))
        print("[review_gate] %s段 放行 %d 只 / 剔除 %d 只 %s"
              % (gate.get("stage"), len(kept), len(drops),
                 ("| " + "；".join(notes)) if notes else ""))
        _write_review_gate_applied(stage, kept, drops, meta)
        return kept
    except Exception as e:
        import traceback
        print("[review_gate] 异常(放行，不干预): %s" % e)
        traceback.print_exc()
        return cands


def _fill_gate(info):
    """ADR-0036 L1 可成交性/承接闸门 → (ok, reason)。

    用户反馈（2026-09-28）："买进的很大程度是开板了" —— 说明旧口径会推"正在走坏"的票。
    两道判据（均只做减法，绝不新增候选）：
      ① 现价须站上当日均价（均价＝成交额/成交量）：均价下方＝卖方占优，低吸＝接飞刀
      ② 成交额下限（亿）：流动性不足无法足量成交

    安全设计：任何取数异常（info 缺失/volume<=0/amount<=0）→ **放行**并打 WARN，
    宁放过不错杀，防止单只票数据源抖动导致整时段零推荐。
    """
    if not FILL_GATE_ENABLED:
        return True, ""
    price = float(info.get("current_price", 0) or 0)
    vol = float(info.get("volume", 0) or 0)
    amt = float(info.get("amount", 0) or 0)
    if price <= 0 or vol <= 0 or amt <= 0:
        print("[WARN] fill_gate 取数不足(放行): price=%s vol=%s amt=%s" % (price, vol, amt))
        return True, ""
    vwap = amt / vol
    if vwap > 0 and price < vwap - 1e-6:
        return False, "现价%.2f低于当日均价%.2f(卖方占优)" % (price, vwap)
    amount_yi = amt / 1e8
    if amount_yi < FILL_MIN_AMOUNT_YI:
        return False, "成交额%.2f亿<%.2f亿(流动性不足)" % (amount_yi, FILL_MIN_AMOUNT_YI)
    return True, ""


def _open_board_guard(info, limit_up_price):
    """ADR-0037 触板后开板闸门 → (blocked, reason)。

    判据（仅一条，只做减法）：**日内最高价已触及涨停价，但现价仍在涨停价下方**
      → 该股今天封板失败/炸板回落，属"冲高回落"形态，实证次日均值 -3.60%、胜率 33%。
    这是用户「买进的很大程度是开板了」的正面修正，也是唯一在历史样本上**有选股选择性**
    的闸门（拦截组 -3.60% vs 放行组 +2.68%）。

    安全设计：`info` 缺 high / limit_up_price<=0 / high<=0 / 现价<=0 任一成立 → **放行**
    并打 WARN（宁放过不错杀，避免数据源抖动导致整时段零推荐）。已封板(price>=limit)
    的情况不会走到这里 —— 上游 `is_lu` 已跳过；此处再判一次仅为幂等安全。
    """
    if not OPEN_BOARD_GUARD_ENABLED:
        return False, ""
    try:
        price = float(info.get("current_price", 0) or 0)
        high = float(info.get("high", 0) or 0)
        lim = float(limit_up_price or 0)
    except Exception:
        return False, ""
    if price <= 0 or high <= 0 or lim <= 0:
        print("[WARN] open_board_guard 取数不足(放行): price=%s high=%s lim=%s" % (price, high, lim))
        return False, ""
    if price >= lim - 1e-6:
        return False, ""                      # 仍封在板上：不属本闸门管辖
    if high >= lim * (1.0 - OPEN_BOARD_GUARD_TOL) - 1e-6:
        return True, "盘中触板%.2f后开板(现价%.2f，涨停价%.2f)" % (high, price, lim)
    return False, ""


def _plain_buy_items(stage, candidates, avail, pos_pct, max_picks, note, empty_note,
                      per_amount=None):
    """未涨停买入通道(13:40 午后强势 / 14:40 龙首阴共用)：按现价买入，不挂涨停排单。
    已封涨停板则买不进 → 直接跳过(午后/尾盘布局不推涨停票，用户明确反馈)。
    两道减法闸门（顺序固定）：
      ADR-0037 触板后开板 → 跳过（冲高回落形态，实证 -3.60%/胜率33%，且 5 笔真实成交占 3 笔）
      ADR-0036 可成交性   → 跳过（现价低于均价 / 成交额不足，仅纪律约束、无选股选择性）

    【2026-10-21 修 P1-4】新增 per_amount 参数：
      per_amount 非 None → 单笔金额直接用该值（复盘闸门总额 ÷ 只数的口径，
      避免段上限被情绪系数二次乘算）；None → 沿用旧的 avail × pos_pct 路径。
    """
    items = []
    per = per_amount if per_amount is not None else (avail * pos_pct)
    for c in (candidates or [])[:max_picks]:
        cn = c.get("name") or c.get("stock_chi_name") or ""
        cs = c.get("symbol") or c.get("code") or ""
        if not cs or is_suspended(cn):
            continue
        info = get_stock_detailed_sina(cn)
        price = info.get("current_price", 0.0) if info else 0.0
        if price <= 0:
            items.append({"type": "BUY", "name": cn, "symbol": cs, "price": 0.0,
                          "buy_shares": 0, "buy_lots": 0, "amount": 0.0,
                          "note": "无法获取现价，约买入15%仓，请手工确认手数"})
            continue
        # 已封涨停板则买不进 → 直接跳过
        is_lu = False
        lu = 0.0          # ADR-0037：预先绑定，异常路径下 _open_board_guard 会自动放行
        try:
            lu, _ld = get_stock_limits(cs, cn, info.get("yesterday_close", price))
            is_lu = price >= lu - 1e-6
        except Exception as e:
            print(f"[WARN] 静默异常已显式化 @plain_buy: {e}")
        if is_lu:
            continue
        # ADR-0037：触板后开板闸门（最高价触板但未封住 → 冲高回落形态，历史 -3.60%/胜率33%）
        _ob, _obwhy = _open_board_guard(info, lu)
        if _ob:
            items.append({"type": "INFO", "name": cn, "symbol": cs, "price": round(price, 2),
                          "buy_shares": 0, "buy_lots": 0, "amount": 0.0,
                          "note": "触板后开板闸门跳过：%s" % _obwhy})
            continue
        # ADR-0036：可成交性/承接闸门（均价下方 + 流动性不足 → 不派单，只记 INFO）
        _ok, _why = _fill_gate(info)
        if not _ok:
            items.append({"type": "INFO", "name": cn, "symbol": cs, "price": round(price, 2),
                          "buy_shares": 0, "buy_lots": 0, "amount": 0.0,
                          "note": "可成交性闸门跳过：%s" % _why})
            continue
        buy_lots = _afford_lots(per, price, cs)
        if buy_lots * price * _lot_for(cs) > avail:
            buy_lots = 0
        buy_shares = _to_shares(buy_lots, cs)
        # ADR-0041：把"这只票是靠哪几个模块选出来的"直接写进操作项 —— 宗旨要求
        # 每个模块都必须能解释自己在推票里的作用，人这一侧才看得见闭环。
        _src_tag = "＋".join(c.get("sources") or []) or "—"
        items.append({"type": "BUY", "name": cn, "symbol": cs, "price": round(price, 2),
                      "buy_shares": buy_shares, "buy_lots": buy_lots,
                      "amount": round(buy_shares * price, 2),
                      "sources": list(c.get("sources") or []),
                      "note": "%s｜选股源：%s" % (note, _src_tag), "is_limit_up": False})
    if not items:
        items = [{"type": "INFO", "name": "", "symbol": "", "note": empty_note}]
    return items


def build_action_items(stage, candidates, mkt_temp=50, board=None):
    """【买入指令唯一生成器】只读派生本时段买单（含手数，不修改回测库）。

    职责边界（与三时段独立池框架严格对齐）：
      · 09:25 → 早盘集合竞价涨停池，挂涨停排单（BUY_LIMIT）
      · 13:40 → 午后强势池，未涨停票现价买入（BUY）
      · 14:40 → 尾盘龙首阴池，低吸布局买入（BUY）
      · 其他时段 → 不派单
    卖出指令一律不在此产生，唯一出口是 10:40 的 run_backtest；
    账户/持仓/余额展示见 build_account_review。
    """
    items = []
    try:
        db = load_backtest_db()
        avail = float(db.get("available_cash", 0.0))
        # ADR-0040：复盘仓位上限 → 压缩本次买入可用额度（无闸门时原样返回）
        avail = _gate_avail_cap(avail)
        # 【2026-10-21 修 P1-4】权益缓存（供单票上限换算用）
        global _GATE_EQ_CACHE
        _GATE_EQ_CACHE = {"total_equity": float(db.get("total_equity",
                                                      db.get("initial_capital", 0.0)) or 0.0)}

        if stage in BOARD_STAGES:
            items = _board_buy_items(stage, candidates, avail, mkt_temp, board)

        elif stage == "13:40":
            total_pct = _sentiment_pos_factor(mkt_temp)
            if total_pct <= 0:
                items = [{"type": "INFO", "name": "", "symbol": "",
                          "note": "大盘温度 %.0f 冰点(<40)，情绪不支持开新仓，午后强势池今日不派买单" % mkt_temp}]
            else:
                n = max(1, min(len(candidates or []), LATE_MAX_PICKS))
                # 【2026-10-21 修 P1-4】有闸门时单笔额度 = 闸门总额 ÷ 只数（不再乘情绪系数，
                # 否则段上限被乘两次 → 额度利用率仅 12.5%，单笔 5% vs 闸门上限 15% 严重脱节）
                _per_amt = _gate_single_budget(avail, n)
                if _per_amt is not None:
                    _pos_pct = (total_pct / n)   # 仅用于 note 展示
                    _note = ("13:40-13:55 午后强势未涨停票现价买入，布局冲板；次日10:40按强弱出清或减半"
                             "｜单笔额度按复盘闸门总额÷%d 只计算（¥%.0f/只）" % (n, _per_amt))
                else:
                    _pos_pct = min(total_pct / n, 0.25)
                    _note = "13:40-13:55 午后强势未涨停票现价买入，布局冲板；次日10:40按强弱出清或减半"
                items = _plain_buy_items(
                    stage, candidates, avail, _pos_pct, LATE_MAX_PICKS,
                    _note, "午后无明确未涨停强势标的，不操作", per_amount=_per_amt)

        elif stage == "14:40":
            total_pct = _sentiment_pos_factor(mkt_temp)
            if total_pct <= 0:
                items = [{"type": "INFO", "name": "", "symbol": "",
                          "note": "大盘温度 %.0f 冰点(<40)，情绪不支持开新仓，尾盘龙首阴今日不派买单" % mkt_temp}]
            else:
                n = max(1, min(len(candidates or []), LATE_MAX_PICKS))
                _per_amt = _gate_single_budget(avail, n)
                if _per_amt is not None:
                    _pos_pct = (total_pct / n)
                    _note = ("14:55-15:00 尾盘龙首阴低吸买入，布局次日；次日10:40按强弱出清或减半"
                             "｜单笔额度按复盘闸门总额÷%d 只计算（¥%.0f/只）" % (n, _per_amt))
                else:
                    _pos_pct = min(total_pct / n, 0.25)
                    _note = "14:55-15:00 尾盘龙首阴低吸买入，布局次日；次日10:40按强弱出清或减半"
                items = _plain_buy_items(
                    stage, candidates, avail, _pos_pct, LATE_MAX_PICKS,
                    _note, "尾盘无明确龙首阴低吸标的，不操作", per_amount=_per_amt)
        else:
            items = [{"type": "INFO", "name": "", "symbol": "",
                      "note": "%s 非买入时段，不派买单" % stage}]
    except Exception as e:
        items = [{"type": "INFO", "name": "", "symbol": "", "note": "买入指令生成异常: %s" % e}]
    # ADR-0040：复盘闸门留痕附进操作项 —— 让报告 / 操作闪卡与订单计划口径一致，
    # 用户能直接看到「为什么这次没推/推得少」，而不是只有一个空空的"无标的"。
    try:
        items = items + _gate_info_items()
    except Exception as e:
        print(f"[WARN] 静默异常已显式化 @gate_info: {e}")
    # 仓位闭环校验：单票/总额超买兜底（只读派生，不修改回测库）
    try:
        items, _cw = validate_order_plan_cash(items, avail)
        if _cw:
            print("[order] 仓位校验: " + "; ".join(_cw))
    except Exception as e:
        print(f"[WARN] 静默异常已显式化 @2383: {e}")
    return items


def validate_order_plan_cash(items, avail, max_pos_pct=0.25):
    """仓位闭环校验：防止 _board_buy_items / _buy_candidates 口径偏差导致超买。
    单票金额上限 = avail × max_pos_pct；总买入额不得超过可用现金(按金额从大到小累计扣除)。
    返回 (cleaned_items, warnings)。属于"最后一道闸"，不修改回测库。"""
    warnings = []
    buys = [i for i in items if i.get("type") in ("BUY", "BUY_LIMIT")]
    others = [i for i in items if i.get("type") not in ("BUY", "BUY_LIMIT")]
    cap = avail * max_pos_pct
    for b in buys:
        if b.get("amount", 0) > cap + 1e-6:
            warnings.append(f"单票超额 {b.get('name')}: ¥{b['amount']:.0f} > 上限 ¥{cap:.0f}, 已截断手数")
            price = b.get("price") or 0
            if price > 0:
                lots = _afford_lots(cap, price, b.get("symbol"))
                b["buy_lots"] = lots
                b["buy_shares"] = _to_shares(lots, b.get("symbol"))
                b["amount"] = round(b["buy_shares"] * price, 2)
    rem = avail
    kept = []
    for b in sorted(buys, key=lambda x: x.get("amount", 0), reverse=True):
        if b.get("amount", 0) <= rem + 1e-6:
            kept.append(b)
            rem -= b.get("amount", 0)
        else:
            warnings.append(f"现金不足跳过 {b.get('name')}: 需¥{b['amount']:.0f} 余¥{rem:.0f}")
    return others + kept, warnings


def get_buy_intents(stage, candidates, mkt_temp=50, board=None):
    """统一决策：返回本时段应买入的标的意图列表（与弹窗完全一致）。
    供报告买入段与回测账户 record_buys_to_db 复用，杜绝三套逻辑不一致。"""
    items = build_action_items(stage, candidates, mkt_temp, board)
    return [i for i in items if i.get("type") in ("BUY", "BUY_LIMIT")]


def _fmt_action(it):
    """极简操作指令：买/卖 X(代码) N手。持仓/余额不在此显示。"""
    t = it.get("type")
    name = it.get("name", "")
    sym = _disp_symbol(it.get("symbol", ""))
    sym_tag = ("(%s)" % sym) if sym else ""
    if t == "SELL_ALL":
        return "卖 %s%s %s (全清)" % (name, sym_tag, _fmt_lots(it.get("sell_shares", 0), it.get("symbol")))
    if t == "SELL_HALF":
        return "卖 %s%s %s (半仓)" % (name, sym_tag, _fmt_lots(it.get("sell_shares", 0), it.get("symbol")))
    if t == "BUY":
        if it.get("buy_shares", 0) > 0:
            return "买 %s%s %s" % (name, sym_tag, _fmt_lots(it.get("buy_shares", 0), it.get("symbol")))
        return "买 %s%s — %s" % (name, sym_tag, it.get("note", ""))
    if t == "BUY_LIMIT":
        if it.get("buy_shares", 0) > 0:
            return "排单 %s%s %s" % (name, sym_tag, _fmt_lots(it.get("buy_shares", 0), it.get("symbol")))
        return "排单 %s%s — %s" % (name, sym_tag, it.get("note", ""))
    if t == "INFO":
        return it.get("note", "")
    return it.get("note", "")


def build_order_plan(stage, candidates, advice_pos, mkt_temp=50, board=None):
    items = build_action_items(stage, candidates, mkt_temp, board)
    db = load_backtest_db()
    avail = float(db.get("available_cash", 0.0))
    equity = float(db.get("total_equity", db.get("initial_capital", 0.0)) or 0.0)
    # ===== 【2026-10-21 修 P1-4】额度利用率诊断 =====
    # 痛点：10-20 额度 197,116 元、实投仅 24,740 元（5.02%），
    # 而闸门单票上限是 15% —— 单笔额度与段上限严重脱节，且**无人看得见**。
    # 这里打印「理论额度 / 实际下单额 / 偏差」三个数，把隐性的额度浪费显性化，
    # 供复盘核对（不改实际下单金额，只暴露偏差 —— 真正的调整需人拍板）。
    _buys = [i for i in items if i["type"] in ("BUY", "BUY_LIMIT")]
    _actual = sum(float(i.get("amount") or 0) for i in _buys)
    _gate = load_review_gate() or {}
    _cap_pct = _rg_float(_gate.get("position_cap_pct"))
    _picks = _rg_int(_gate.get("max_picks")) or 0
    _single_pct = _rg_float(_gate.get("single_pos_cap_pct"))
    _theo_pct = None
    if _cap_pct and _picks:
        _theo_pct = round(_cap_pct / _picks, 2)
    _theo_amount = round(_theo_pct / 100.0 * equity, 2) if (_theo_pct and equity) else None
    _util_pct = round(_actual / equity * 100, 2) if equity else None
    _quota_block = {
        "equity": round(equity, 2),
        "available_cash": round(avail, 2),
        "gate_position_cap_pct": _cap_pct,
        "gate_max_picks": _picks,
        "gate_single_cap_pct": _single_pct,
        "theoretical_single_pct": _theo_pct,
        "theoretical_single_amount": _theo_amount,
        "actual_single_amount": round(_actual / len(_buys), 2) if _buys else 0.0,
        "actual_total_amount": round(_actual, 2),
        "actual_total_pct": _util_pct,
        "utilization_pct": round(_actual / _theo_amount * 100, 1)
                          if (_theo_amount and _actual) else None,
        # 【2026-10-21 修】无闸门时 _cap_pct/_picks/_theo_pct 可能为 None，
        # 旧写法用 %g 格式化 None 会抛 "must be real number, not NoneType"
        # → 整个 build_order_plan 抛异常 → 订单计划不落盘（派发被跳过）。
        "note": ("理论单笔 = 段上限 %s ÷ max_picks(%s) = %s；实际单笔 %s 元。"
                 "偏差过大说明单笔额度未吃满段上限，需人拍板是否上调。"
                 % ("%g%%" % _cap_pct if _cap_pct is not None else "无闸门",
                    _picks if _picks else "-",
                    ("%.1f%%" % _theo_pct) if _theo_pct is not None else "-",
                    round(_actual / len(_buys), 2) if _buys else 0)),
    }
    return {
        "stage": stage,
        "generated_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "account": {
            "available_cash": round(avail, 2),
            "total_equity": round(equity, 2),
        },
        "recommended_position": advice_pos,
        "market_temp": mkt_temp,
        "actions": items,
        # 【2026-10-21 修 P1-4】额度诊断块（只暴露偏差，不改下单金额）
        "quota_diag": _quota_block,
        "buy_candidates": [{"name": i["name"], "symbol": i["symbol"],
                            "price": i.get("price"), "buy_shares": i.get("buy_shares"),
                            "amount": i.get("amount")}
                           for i in items if i["type"] in ("BUY", "BUY_LIMIT")],
        "sell_positions": [{"name": i["name"], "symbol": i["symbol"], "action": i["type"],
                            "held_shares": i.get("shares"), "sell_shares": i.get("sell_shares"),
                            "amount": i.get("amount")}
                           for i in items if i["type"] in ("SELL_ALL", "SELL_HALF")],
        # ADR-0040：复盘闸门留痕（哪个阶段闸门、放行了谁、剔除了谁及原因、额度缺口）
        "review_gate": _gate_plan_block(),
        # ADR-0041：推票参会源（每只候选由哪些模块选中）—— 供工作台/复盘核对"谁在选股"
        "push_sources": [{"name": c.get("name"), "symbol": c.get("symbol"),
                          "sources": list(c.get("sources") or []),
                          "score": c.get("score"),
                          # 【2026-10-21】竞价参考加权留痕（供报告/复盘核对这一分怎么来的）
                          "auction_bonus": c.get("auction_bonus"),
                          "auction_why": c.get("auction_why", "")}
                         for c in (candidates or [])[:POOL_TOP_N]],
    }


def dispatch_orders(stage, order_plan):
    """将订单计划落盘，并按配置决定是否真正下单（默认 DRY_RUN）。"""
    try:
        import sys as _sys
        _here = os.path.dirname(os.path.abspath(__file__))
        if _here not in _sys.path:
            _sys.path.insert(0, _here)
        op_path = os.path.join(REPORTS_DIR, "order_plan_%s.json" % stage.replace(":", ""))
        with open(op_path, "w", encoding="utf-8") as f:
            json.dump(order_plan, f, ensure_ascii=False, indent=2)
        print(f"[broker] 订单计划已写出: {op_path}")
        try:
            import broker_easytrader as be
        except Exception as e:
            print(f"[broker] 执行器不可用(Mac/未安装easytrader)，仅保留订单JSON: {e}")
            return
        be.run_order_plan(order_plan)
    except Exception as e:
        print(f"[broker] 派发异常: {e}")


STAGE_LABEL = {
    "09:25": "早盘打板",
    "10:40": "持仓处理",
    "11:40": "午盘复盘",
    "13:40": "午后强势",
    "14:40": "尾盘龙首阴",
}


def _gate_flash_line():
    """闪卡末行的复盘闸门摘要（一行）。无闸门 → 空串。

    ADR-0040：闪卡是用户最先看到的推票载体，闸门必须**始终**出现在上面。
    否则闸门悄悄删掉候选后，用户看到的还是"买 X"，无从判断这次推票是否经过
    复盘过滤、也无从积累「闸门到底帮了还是害了」的体感 —— 人这一侧的闭环就断了。
    """
    g = _ACTIVE_REVIEW_GATE
    if not g:
        return ""
    m = _REVIEW_GATE_META or {}
    seg = ["🔒 复盘闸门 %s段" % (g.get("stage") or "")]
    if g.get("hard_block"):
        seg.append("⛔ 硬阻断：不开新仓")
    else:
        seg.append("仓位上限 %g%%" % (_rg_float(g.get("position_cap_pct"), 0) or 0))
        seg.append("不碰 %d 板以上" % _rg_int(g.get("max_board_days")))
    n = len(_REVIEW_GATE_DROPS)
    if n:
        seg.append("已剔除 %d 只（%s）" % (
            n, "、".join((d.get("name") or d.get("symbol") or "?") for d in _REVIEW_GATE_DROPS[:3])))
    if m.get("escape"):
        seg.append("⚠ 逃生阀已降级")
    if str(g.get("mode") or "enforce") != "enforce":
        seg.append("shadow: 只记录不拦截")
    for ps in (g.get("pending_sell") or []):
        seg.append("⚠️ 昨日卖出未执行 %s" % (ps.get("name") or ""))
    gj = _gjd_flash_seg(g)
    if gj:
        seg.append(gj)
    return " ｜ ".join(seg)


def _gjd_flash_seg(g=None, snap=None):
    """GJD 一行摘要（供闸门摘要 / 无闸门时兜底）。无数据且未接入 → 空串。

    ADR-0049：GJD 会改「仓位上限」这个用户最关心的数字，所以改没改、为什么改，
    必须在闪卡上直接说清，而不是只写在日志里。
    """
    rec = (g or {}).get("gjd") if isinstance(g, dict) else None
    if not rec:
        if snap is None:
            return ""
        if not snap.get("ok"):
            return "🏛️ GJD 未生效：%s（闸门不加不减）" % ((snap.get("reason") or "无数据")[:40])
        eff = snap.get("effect") or {}
        core = snap.get("core") or {}
        return ("🏛️ GJD core %s · 净买 %s 亿 · %s（闸门不加不减：%s）"
                % (_fmt_gjd_ratio(core.get("ratio")), _fmt_gjd_net(core.get("net_buy_yi")),
                   core.get("level") or "--", eff.get("why") or ""))
    head = "🏛️ GJD core %s · 净买 %s 亿 · %s" % (
        _fmt_gjd_ratio(rec.get("ratio")), _fmt_gjd_net(rec.get("net_buy_yi")),
        rec.get("level") or "--")
    if not rec.get("ok"):
        return head + " ｜ 未生效：%s（闸门不加不减）" % ((rec.get("reason") or "无数据")[:40])
    if not rec.get("applied"):
        return head + " ｜ 闸门不加不减（%s）" % (rec.get("why") or "")
    adj = []
    if rec.get("new_position_cap_pct") is not None and \
            rec.get("new_position_cap_pct") != rec.get("raw_position_cap_pct"):
        adj.append("仓位 %g%%→%g%%" % (rec.get("raw_position_cap_pct") or 0,
                                     rec["new_position_cap_pct"]))
    if rec.get("new_max_picks") is not None and rec.get("new_max_picks") != rec.get("raw_max_picks"):
        adj.append("只数 %s→%s" % (rec.get("raw_max_picks"), rec["new_max_picks"]))
    return head + " ｜ 闸门%s：%s" % ("放宽" if rec.get("dir") == "loosen" else "收紧",
                                   " · ".join(adj) or "无变化")


def _fmt_gjd_ratio(v):
    return "--" if v is None else "%.2f×" % float(v)


def _fmt_gjd_net(v):
    if v is None:
        return "无数据"
    return "%+.2f" % float(v)


def _source_flash_line(candidates):
    """ADR-0041：本次推票的「参会模块」一行摘要。

    宗旨落地：模块不许只展览 —— 推出来的每只票都要能说清是哪几个源选中的。
    与 _gate_flash_line() 一起构成"这次为什么推它/为什么没推"的完整解释。
    """
    rows = []
    for c in (candidates or [])[:POOL_TOP_N]:
        srcs = c.get("sources") or []
        if not srcs:
            continue
        rows.append("%s[%s]" % (c.get("name") or c.get("symbol"), "＋".join(srcs)))
    if not rows:
        return ""
    return "🧩 推票参会源：" + "；".join(rows)


def build_ops_flashcard(stage, candidates, mkt_temp=50, board=None):
    """操作闪卡正文：纯操作指令，越精简越好（需求：只提示操作）。"""
    items = build_action_items(stage, candidates, mkt_temp, board)
    label = STAGE_LABEL.get(stage, stage)
    lines = ["⚡ %s %s" % (stage, label)]
    ops = [i for i in items if i.get("type") in ("SELL_ALL", "SELL_HALF", "BUY", "BUY_LIMIT")]
    if ops:
        for i in ops:
            lines.append(_fmt_action(i))
            # 碰撞参考：操作闪卡命中真实持仓时附真实仓位提示(不改变指令)
            _rh = real_holding_reference(i.get("symbol", ""), i.get("name", ""),
                                         "SELL" if i.get("type") in ("SELL_ALL", "SELL_HALF") else "BUY")
            if _rh:
                lines.append("  " + _rh.strip())
        # ADR-0040：有推票时闸门摘要必须出现在末尾（无推票时 INFO 段已含闸门说明，不重复）
        try:
            _gl = _gate_flash_line()
            if _gl:
                lines.append(_gl)
        except Exception as e:
            print(f"[WARN] 静默异常已显式化 @gate_flash: {e}")
        # ADR-0041：参会源摘要（放最后一行，不干扰操作指令阅读）
        try:
            _sl = _source_flash_line(candidates)
            if _sl:
                lines.append(_sl)
        except Exception as e:
            print(f"[WARN] 静默异常已显式化 @source_flash: {e}")
    else:
        lines += [i.get("note", "") for i in items if i.get("type") == "INFO"]
        # ADR-0049：无复盘闸门时 GJD 无处落地，但也不能让它消失 —— 明确说「为什么没生效」
        try:
            if not _ACTIVE_REVIEW_GATE:
                _gj = _gjd_flash_seg(None, _gjd_snapshot())
                if _gj:
                    lines.append(_gj + "（无复盘闸门，GJD 修正无落点）")
        except Exception as e:
            print(f"[WARN] 静默异常已显式化 @gjd_flash: {e}")
    return "\n".join(lines) + "\n"


def build_account_review():
    """后台做账：午盘(11:40)梳理 今日卖出 / 当前持仓 / 收益。与操作提示剥离。"""
    db = load_backtest_db()
    today_str = datetime.now().strftime('%Y-%m-%d')
    avail = float(db.get("available_cash", 0.0))
    # 今日卖出（已实现）
    today_sells = [t for t in db.get("completed_trades", []) if t.get("sell_date") == today_str]
    realized = sum(float(t.get("profit_loss_amount", 0.0)) for t in today_sells)
    # 当前持仓（未实现）
    held_lines = []
    unreal = 0.0
    held_value = 0.0
    for p in db.get("active_positions", []):
        name = p.get("name", "")
        sym = p.get("symbol", "")
        shares = int(float(p.get("shares", 0)))
        cost = float(p.get("buy_price", 0.0))
        buy_fee = float(p.get("buy_fee", 0.0))
        info = get_stock_detailed_sina(name)
        cur = info.get("current_price", cost) if info else cost
        pct = info.get("pct", 0.0) if info else 0.0
        mv = shares * cur
        held_value += mv
        upnl = (cur - cost) * shares - buy_fee
        unreal += upnl
        tag = "[T+1]" if p.get("buy_date", "") >= today_str else ""
        held_lines.append("  · %s(%s) %s 现价%.2f(%+.1f%%) 市值¥%s 浮盈¥%s %s" % (
            name, _disp_symbol(sym), _fmt_lots(shares, sym), cur, pct, _fmt_money(mv), _fmt_money(upnl), tag))
    equity = avail + held_value
    sep = "━" * 52
    lines = [
        sep,
        "【午盘总结 · 账户梳理】 %s" % today_str,
        sep,
        "可用资金 ¥%s ｜ 持仓市值 ¥%s ｜ 总权益 ¥%s" % (_fmt_money(avail), _fmt_money(held_value), _fmt_money(equity)),
        "今日已实现盈亏 ¥%s ｜ 当前浮盈 ¥%s" % (_fmt_money(realized), _fmt_money(unreal)),
        "",
        "一、今日卖出（早盘执行）:",
    ]
    if today_sells:
        for t in today_sells:
            lines.append("  · 卖 %s(%s) %s 价%.2f 盈亏¥%s(%+.1f%%)" % (
                t.get("name", ""), _disp_symbol(t.get("symbol", "")),
                _fmt_lots(t.get("shares", 0), t.get("symbol", "")), float(t.get("sell_price", 0.0)),
                _fmt_money(float(t.get("profit_loss_amount", 0.0))), float(t.get("profit_loss_pct", 0.0))))
    else:
        lines.append("  · 无（今日早盘未卖出）")
    lines += ["", "二、当前持仓:"]
    if held_lines:
        lines += held_lines
    else:
        lines.append("  · 空仓")

    # 三、持仓异动兜底提醒（2026-09-23，只读巡检，不派单不改库）
    # 10:40 是框架内唯一卖出决策点；若该时段自动化未触发，当日会"只买不卖"静默。
    # 11:40 复盘时主动校验卖出通道是否产出，把风险显式化。
    # （2026-09-23 起另有 M9 实时持仓盯盘：工作台每 15s 跟踪，信号解除会主动撤回
    #   pending_sell_confirm —— 所以"无卖出单"也可能是正常解除，需在文案里说明。）
    _sell_file = os.path.join(REPORTS_DIR, "pending_sell_confirm_%s.json" % today_str)
    _has_sell = os.path.exists(_sell_file)
    _held_n = len(db.get("active_positions", []))
    lines += ["", "三、持仓异动兜底提醒 (卖出通道巡检):"]
    if _held_n == 0:
        lines.append("  · 当前无持仓，无需卖出处理。")
    elif _has_sell:
        try:
            with open(_sell_file, encoding="utf-8") as _f:
                _n_items = len(json.load(_f).get("items", []))
        except Exception as _e:
            print(f"[WARN] 静默异常已显式化 @account_review_sell_file: {_e}")
            _n_items = 0
        lines.append("  · ✅ 今日已产出待确认卖出单（%d 项），当前持仓 %d 只。" % (_n_items, _held_n))
        lines.append("  · 请到【工作台 · 持仓盯盘 / 操作确认台】查看实时建议并逐笔确认。")
    else:
        lines.append("  · ⚠️ 今日卖出通道未产出待确认卖出单，但当前持仓 %d 只。" % _held_n)
        lines.append("  · 可能原因: ① 10:40 自动化未触发；② 实时盯盘评估为继续持有（资金流入，卖出信号已解除并撤回）。")
        lines.append("  · 兜底操作: 打开工作台「🔭 持仓盯盘」查看实时建议 —— M9 每 15s 跟踪，"
                     "破位/大资金流出会即时弹窗并写入待确认卖出单，无需等到下个时段。")

    lines += [sep]
    return "\n".join(lines) + "\n"


def generate_report(stage):
    today_str = datetime.now().strftime('%Y-%m-%d')
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    global _PENDING_FILE_NOTES
    _PENDING_FILE_NOTES = []   # 每次生成报告重置，汇总本时段产生的待确认单提示

    # 若真实持仓已清空，归档失效的 pending_sell，防止外部 UI 继续展示过期卖出单
    _cleanup_orphan_pending_sells()

    # 集合竞价时段特殊延迟：开盘集合竞价最终匹配时间为09:25:00，但交易所/行情服务商的数据发布有延迟。
    # 如果脚本在 09:25:00 ~ 09:25:40 之间触发，主动等待到 09:25:40 以确保获取到真实完整的开盘竞价数据，不返回 0。
    if stage == "09:25":
        now = datetime.now()
        if now.hour == 9 and now.minute == 25 and now.second < 40:
            sleep_time = 40 - now.second
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 集合竞价刚结束，等待 {sleep_time} 秒让交易所和选股宝数据完全发布...")
            time.sleep(sleep_time)
            # 重新生成时间戳
            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
    # 带重试的网络数据拉取
    xgb = None
    sh_pct, sz_pct, cy_pct, total_vol = 0.0, 0.0, 0.0, 0.0
    for attempt in range(5):
        sh_pct, sz_pct, cy_pct, total_vol = get_akshare_macro()
        xgb = fetch_xuangutong_data()
        
        # 验证数据的有效性：在交易日 09:25 之后，温度或指数或限售池不应该全部为0。如果为0说明数据尚未准备好，重试。
        # 特别是检查 limit_up_pool 是否为空（09:25 必定有开盘涨停个股/ST股）
        if stage == "09:25" and (not xgb.get('limit_up_pool') or xgb.get('temperature') == 0):
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 警告：竞价数据不完整或为空，第 {attempt+1}/5 次重试，等待 3 秒...")
            time.sleep(3)
        else:
            break
            
    if not xgb:
        xgb = fetch_xuangutong_data()  # 兜底
    
    # ==== 涨停池与逻辑 ====
    pool = xgb['limit_up_pool']
    super_stock_pool = xgb['super_stock_pool']
    max_lb = 0
    dragon_str = "无"
    dragons = []
    top_reasons = []
    tier_breakdown = {'5板及以上': 0, '4板': 0, '3板': 0, '2板': 0, '首板': 0}
    # 必须在 pool 分支外预置：空涨停池(如节后首日开盘)时下方第 4528 行会无条件引用 sorted_reasons，
    # 原实现仅在 if pool: 内赋值 → UnboundLocalError 直接阻断整份报告。空池应为"无题材"而非崩溃。
    sorted_reasons = []
    
    # 建立股票到各个板块的映射
    plate_limit_up_stocks = defaultdict(list)
    plate_all_active_stocks = defaultdict(list)
    stock_seen = set()
    
    if pool:
        lb_list = [x.get('limit_up_days', 1) for x in pool]
        max_lb = max(lb_list) if lb_list else 0
        
        dragons = [x['stock_chi_name'] for x in pool if x.get('limit_up_days', 1) == max_lb and 'ST' not in x['stock_chi_name']]
        if not dragons:
            dragons = [x['stock_chi_name'] for x in pool if x.get('limit_up_days', 1) == max_lb]
            
        dragon_str = f"【{max_lb}连板】 {', '.join(dragons)}"
        
        for lb in lb_list:
            if lb >= 5: 
                tier_breakdown['5板及以上'] += 1
            elif lb == 4: 
                tier_breakdown['4板'] += 1
            elif lb == 3: 
                tier_breakdown['3板'] += 1
            elif lb == 2: 
                tier_breakdown['2板'] += 1
            elif lb == 1: 
                tier_breakdown['首板'] += 1
            
        tier_str = f"高位(≥4板): {tier_breakdown['5板及以上']+tier_breakdown['4板']}只 | 中位(3板): {tier_breakdown['3板']}只 | 低接(2板): {tier_breakdown['2板']}只 | 首板: {tier_breakdown['首板']}只"
        
        # Plate structure construction
        for stock in pool:
            symbol = stock.get("symbol")
            if not symbol: continue
            stock_seen.add(symbol)
            plates = _extract_plates(stock)
            for p in plates:
                plate_limit_up_stocks[p].append(stock)
                plate_all_active_stocks[p].append(stock)
                
        for stock in super_stock_pool:
            symbol = stock.get("symbol")
            if not symbol or symbol in stock_seen: continue
            plates = _extract_plates(stock)
            for p in plates:
                plate_all_active_stocks[p].append(stock)

        # Basic sector rank (for top reasons)
        reasons = {}
        for p, stocks in plate_limit_up_stocks.items():
            reasons[p] = len(stocks)
        sorted_reasons = sorted(reasons.items(), key=lambda k: k[1], reverse=True)
        top_reasons_list = [r for r in sorted_reasons if r[0] not in ['ST股', '业绩增长']][:3]
        top_reasons = [r[0] for r in top_reasons_list]
        top_reasons_str = " | ".join([f"{k}({v}家)" for k, v in top_reasons_list])
    else:
        tier_str = "连板梯队断层"
        top_reasons_str = "无"

    # ==== 锁单强度 Top Ranking (Anchor points) ====
    lock_rows = []
    for it in pool:
        bl = it.get("buy_lock_volume_ratio")
        if bl is None: continue
        lock_rows.append({
            "name": it.get("stock_chi_name"),
            "symbol": it.get("symbol"),
            "lock": float(bl),
            "pct": it.get("change_percent", 0),
            "limit_up_days": it.get("limit_up_days", 1),
            "plates": _extract_plates(it)[:3]
        })
    lock_rows.sort(key=lambda x: x["lock"], reverse=True)
    
    # ==== 👑 底部盘整放量突破 - 首板新金龙潜力股监控 (寻找新龙头启动点) ====
    first_board_candidates = []
    if pool:
        for s in pool:
            if s.get("limit_up_days", 1) == 1: # 仅筛选今日首板突破股
                name = s.get("stock_chi_name", "")
                if "ST" in name or "st" in name: continue
                
                # 突围今日资金净流入量(即今日总成交额 亿)
                cap = s.get('non_restricted_capital', 0)
                tr = s.get('turnover_ratio', 0)
                turnover_亿 = (cap * tr) / 100000000
                
                first_board_candidates.append({
                    "name": name,
                    "symbol": s.get("symbol"),
                    "turnover_亿": turnover_亿,
                    "tr_pct": tr * 100,
                    "plates": _extract_plates(s)[:2],
                    "raw": s
                })
        # 按照今日大额资金流入量(突围成交额)倒序排列
        first_board_candidates.sort(key=lambda x: x["turnover_亿"], reverse=True)

    hot_themes = [t[0] for t in (sorted_reasons or []) if t[0] not in ['ST股', '业绩增长']][:8]

    # 系统性风险闸门：弱市/恐慌结构下统一不开新仓(对所有买入通道生效)。
    risk_gate_triggered = (
        stage in ["10:40", "11:40", "13:40", "14:40", "盘后复盘"]
        and (
            xgb.get('temperature', 50) < 35
            or cy_pct <= -2.5
            or (sh_pct <= -1.5 and sz_pct <= -2.0)
            or (xgb.get('broken_ratio', 0) >= 35 and xgb.get('limit_down', 0) >= 10)
        )
    )

    # ===== 统一操作池：汇总共线所有模块候选，排序取 Top N 作为买入候选(闭环) =====
    _intel_cands = get_intel_candidates()
    _bottom_rows = scan_bottom_surge(stage=stage)   # ADR-0033：底部放量通道（非接入时段返回 []）
    # ADR-0041：三个新供票源（复盘重点 / 自选 / 题材套利），均 fail-open，非接入时段返回 []
    _rv_rows   = load_review_focus_rows(stage)
    _self_rows = load_self_watchlist_rows() if stage in ("13:40", "14:40") else []
    _ta_rows   = fetch_theme_arb_rows(stage)
    if _rv_rows or _self_rows or _ta_rows:
        print("[adr0041] 新供票源：复盘重点 %d 只 %s｜自选 %d 只｜题材套利 %d 只"
              % (len(_rv_rows), [x.get("name") for x in _rv_rows],
                 len(_self_rows), len(_ta_rows)))
    _contribs = _gather_pool_contributions(
        xgb, first_board_candidates, pool,
        plate_limit_up_stocks, plate_all_active_stocks,
        sorted_reasons, hot_themes, _intel_cands,
        bottom_surge_rows=_bottom_rows,
        review_focus_rows=_rv_rows, self_rows=_self_rows, theme_arb_rows=_ta_rows)
    _op_pool = build_operation_pool(_contribs, load_operation_pool(), today_str)
    _mt0 = xgb.get('temperature', 50) if isinstance(xgb, dict) else 50
    # 【2026-10-21】传入集合竞价参考（13:40/14:40 买入侧打分加权用；无参考则 0，排序不变）
    _auction_map = {}
    if _auction_ref is not None:
        try:
            _auction_map = _auction_ref.load_auction_ref()
        except Exception:
            _auction_map = {}
    _ranked_buys = rank_operation_pool(_op_pool, xgb, load_backtest_db(), stage, hot_themes,
                                       _mt0, None, auction_ref_map=_auction_map)
    _buy_candidates = _ranked_buys  # 统一买入候选：弹窗/报告/订单计划/回测账户共用

    # 风险闸门一致性：闸门触发时强制清空买入候选，使模块七(尾盘推荐)、模块九(统一操作池)、
    # 弹窗待确认买单、订单计划四者一致地"不开新仓"。
    # 否则闸门只挡住模块七的文案，却挡不住模块九列候选 + 弹窗真实下单，造成自相矛盾。
    if risk_gate_triggered:
        _ranked_buys = []
        _buy_candidates = []

    # ===== ADR-0040：复盘闸门（把 T 日复盘结论落到 T+1 的买入候选上）=====
    # 挂载点选在这里的原因：与上方「系统性风险闸门」同一处「候选定稿点」——
    # 下游四个消费方（弹窗闪卡 / 报告 Top N / 订单计划 / 回测账户）全部读
    # _buy_candidates / _ranked_buys，一改全改，不会出现"闸门挡了弹窗却挡不住下单"的
    # 自相矛盾。任何异常 fail-open（原样放行），绝不阻断主流程。
    try:
        _gated = _apply_review_gate(stage, _buy_candidates, xgb, load_backtest_db())
        _buy_candidates = _gated
        _ranked_buys = _gated
    except Exception as _e:
        print("[review_gate] 接入点异常(放行): %s" % _e)

    # ===== 【2026-10-21 修 P0-2】派单距高硬判据（伪强势 n=10）=====
    # 挂在复盘闸门之后、报告组装之前：与「候选定稿点」同一位置，
    # 下游弹窗/报告/订单计划/回测账户共用 _buy_candidates，一改全改。
    _dist_high_would = []
    try:
        _kept_dh, _dist_high_would, _dist_high_mode = _apply_dist_high_guard(stage, _buy_candidates)
        if _dist_high_mode == "enforce" and _kept_dh is not _buy_candidates:
            _buy_candidates = _kept_dh
            _ranked_buys = _kept_dh
    except Exception as _e:
        print("[dist_high] 接入点异常(放行): %s" % _e)

    # 日内交叉验证日志：记录各时段真实买入候选(=弹窗/报告/订单同源的 _buy_candidates)，
    # 使模块十「日内题材热度共振交叉验证」验证的是真实下单依据，而非孤立的 orphan 候选。
    # 此前日志写入的是独立 candidates 变量(锁单/非涨停选取)，与真实买单脱节，模块十形同虚设。
    if _buy_candidates:
        append_intraday_log(stage, _buy_candidates)

    # ==== 昨日策略自我验证与追踪 ====
    prev_state = load_state()
    verify_lines = []
    dragon_health = "中性"   # 必须在分支外初始化：无昨日状态(如首日/数据清空后)时下方 _board 仍会引用

    if prev_state and prev_state.get('date') != today_str:
        prev_date = prev_state['date']
        prev_dragons = prev_state.get('dragons', [])
        prev_sectors = prev_state.get('sectors', [])
        
        verify_lines.append(f"【跨日策略连续性追踪 (自 {prev_date} 验证)】")
        
        # 验证龙头
        if prev_dragons:
            dragon_statuses = []
            for d in prev_dragons:
                sym, pct = get_stock_price_sina(d)
                if pct > 9.0:
                    dragon_statuses.append(f"🟢 {d}({pct:+.2f}%, 连板晋级)")
                    dragon_health = "极强"
                elif pct > 0:
                    dragon_statuses.append(f"🟡 {d}({pct:+.2f}%, 断板红盘)")
                elif pct < -5.0:
                    dragon_statuses.append(f"🔴 {d}({pct:+.2f}%, 严重负反馈)")
                    if dragon_health != "极强": dragon_health = "极弱"
                else:
                    dragon_statuses.append(f"⚪️ {d}({pct:+.2f}%, 震荡)")
            verify_lines.append(f"  • 旧龙追踪: {', '.join(dragon_statuses)}")
        
        # 验证板块
        if prev_sectors:
            sector_statuses = []
            c_pcts = get_concept_pct(prev_sectors)
            for s in prev_sectors:
                pct = c_pcts.get(s, 0.0)
                if pct > 1.5: sector_statuses.append(f"🟢 {s}({pct:+.2f}%, 持续爆发)")
                elif pct > 0: sector_statuses.append(f"🟡 {s}({pct:+.2f}%, 震荡维持)")
                else: sector_statuses.append(f"🔴 {s}({pct:+.2f}%, 退潮走弱)")
            verify_lines.append(f"  • 旧主线追踪: {', '.join(sector_statuses)}")
            
        # 给出连续性操作建议
        if dragon_health == "极强":
            verify_lines.append("  ➡️ 【执行指令】: 龙头打出高度，主线情绪延续！持仓者锁仓，空仓者可积极尝试主线内的1进2或首板挖掘。")
        elif dragon_health == "极弱":
            verify_lines.append("  ➡️ 【执行指令】: 龙头惨遭核按钮，情绪退潮期确认！【立刻停止接力】，持仓逢高/冲高止损离场，进入空仓防守状态。")
        else:
            if xgb['rise_count'] > xgb['fall_count'] and xgb['temperature'] > 50:
                verify_lines.append("  ➡️ 【执行指令】: 高低切换中，旧龙断板但盘面承接尚可。关注新冒头资金的【一板/二板】试错。")
            else:
                verify_lines.append("  ➡️ 【执行指令】: 情绪混沌且旧逻辑证伪。管住手，切忌买入后排跟风股，等待新空间龙确立。")
        verify_lines.append("")

    # ==== 盘面定性（2026-09-28 重构为纯函数 classify_market_phase，补广度权重，ADR-0035）====
    emotion, advice_pos, advice_atk, advice_def, _breadth_ratio = classify_market_phase(
        xgb, cy_pct, sh_pct, sz_pct, max_lb)

    # 提取今日各板块首个涨停板(即板块龙头)用于状态存档
    top_leaders = []
    if pool:
        hot_plates_list = [t[0] for t in sorted_reasons if t[0] not in ['ST股', '业绩增长']][:5]
        for p_name in hot_plates_list:
            limit_ups = plate_limit_up_stocks[p_name]
            limit_ups.sort(key=lambda x: (
                x.get('limit_up_days', 0), 
                -x.get('first_limit_up', float('inf')), 
                x.get('buy_lock_volume_ratio', 0)
            ), reverse=True)
            if limit_ups:
                top_leaders.append(limit_ups[0]['stock_chi_name'])

    # 14:40保存状态给第二天用（含首板新星名单，供次日 1进2 晋级追踪）
    first_board_stars_slim = [
        {
            "name": x["name"],
            "symbol": x["symbol"],
            "plates": x.get("plates") or [],
            "turnover_亿": round(x.get("turnover_亿", 0.0), 2),
            "tr_pct": round(x.get("tr_pct", 0.0), 1)
        }
        for x in first_board_candidates[:5]
    ]
    if "14:40" in stage or "盘后复盘" in stage:
        save_state(dragons, top_leaders, top_reasons, today_str, first_board_stars_slim)

    # ==== 统一决策参数（供弹窗 / 报告买入段 / 回测账户三处复用，彻底消除三套逻辑不一致）====
    _mt = xgb.get('temperature', 50) if isinstance(xgb, dict) else 50
    # 【2026-10-21 修 P0-1】炸板率 data_ready 判定。
    # 09:25 集合竞价时 xuangubao 的 limit_up_broken_ratio 是**除零产物**：
    #   涨停池刚建（limit_up≈8）但尚无任何炸板（broken_count=0）→ ratio=0.0。
    # 这个 0 是"没有分母"而不是"真的 0% 炸板"，若按 0% 满分处理会**高估**打板质量，
    # 若按旧代码的 or 100 兜底又会按 100% **惩罚**（恒扣 18 分，连续 8 日）。
    # 正确做法：显式标记 data_ready=False，board_score 给中性半档 9/18。
    _broken_raw = xgb.get('broken_ratio', None) if isinstance(xgb, dict) else None
    _brk_count = xgb.get('broken_count', None) if isinstance(xgb, dict) else None
    _lu_now = xgb.get('limit_up', 0) if isinstance(xgb, dict) else 0
    _board_data_ready = True
    if _broken_raw is None:
        _board_data_ready = False          # 字段完全缺失
    else:
        try:
            if _brk_count is not None and int(_brk_count) == 0:
                # 炸板数为 0：可能是真的一个都没炸（盘中成熟期），也可能是除零（早盘）
                # 用涨停家数做区分：早盘涨停池尚未成型（<15 家）→ 无数据
                if int(_lu_now) < 15:
                    _board_data_ready = False
        except (TypeError, ValueError):
            _board_data_ready = False
    _board = {
        "max_lb": max_lb,
        # 【关键】0 是合法值（真的 0% 炸板），不能用 `or 100` 把它变成 100
        "broken_ratio": (100.0 if _broken_raw is None else float(_broken_raw)),
        "broken_count": _brk_count,
        "data_ready": _board_data_ready,
        "limit_up": _lu_now,
        "dragon_health": dragon_health,
        "premium": xgb.get('premium', 0) if isinstance(xgb, dict) else 0,
    }

    # ==== 组装报告 ====
    lines = []
    
    # 👑 09:25 集合竞价节点：主力抢筹监控 + 打板排单指令置于报告绝对顶端，供开盘两分钟极限决策。
    #    严格按五时段框架：09:25 只打板买入，不出任何卖出指令（卖出唯一出口是 10:40）。
    _board_buy_intents = []
    if stage in BOARD_STAGES:
        _board_buy_intents = get_buy_intents(stage, _buy_candidates, _mt, _board)
        if stage == "09:25":
            lines.extend(get_opening_auction_signals(xgb, stage,
                                                     decision_buy_candidates=_board_buy_intents))
            # 【2026-10-21】把集合竞价画像落盘，供 10:40 卖出判定与 13:40/14:40 买入打分消费。
            # 之前竞价数据只写进报告文本，10:40 的 decide_exit 完全看不见「今天有没有承接」。
            if _auction_ref is not None:
                try:
                    _n = _auction_ref.build_auction_ref(
                        _AUCTION_SCORED_STOCKS or [],
                        date_str=datetime.now().strftime("%Y%m%d"))
                    # 【2026-10-21 补覆盖缺口】涨停池只含竞价就封板的票；
                    # 持仓股若竞价没涨停就拿不到参考 —— 而这类票最需要参考
                    # （不会涨停，10:40 判定全看 pct）。用行情快照(open=竞价价)补齐。
                    try:
                        _db_pos = load_backtest_db()
                        _poss = _db_pos.get("active_positions") or []
                        _n2 = _auction_ref.supplement_with_quotes(
                            _poss, get_stock_detailed_sina,
                            date_str=datetime.now().strftime("%Y%m%d"))
                    except Exception as _e2:
                        print("[auction] 持仓补充调用失败(不影响主流程): %s" % _e2)
                        _n2 = 0
                    if _n or _n2:
                        lines.append("  📇 【集合竞价参考已落盘】%d 只竞价画像（其中持仓股补充 %d 只）"
                                     " → auction_ref.json"
                                     "（供 10:40 卖出判定 + 13:40/14:40 买入打分参考）" % (_n, _n2))
                        lines.append("")
                except Exception as _e:
                    print("[auction] 09:25 落盘失败(不影响报告): %s" % _e)
            lines.append("===========================================================\n")
        # 打板同样难成交 → 一律走待确认买入单，用户在确认台填实际成交手数后才写库
        if _board_buy_intents:
            _pb = write_pending_buy_confirm(stage, _board_buy_intents)
            if _pb:
                _PENDING_FILE_NOTES.append(f"{stage}待确认打板单: {os.path.basename(_pb)}")

    lines.append(f"===========================================================")
    lines.append(f"    【机构级游资五合一超级终端】 A股决策内参 - {stage} 策略报告")
    lines.append(f"     生成时间: {now_str}")
    lines.append(f"===========================================================\n")

    # 时段语义保护（2026-10-08 新增）：补跑/延迟触发时，若当前时钟尚未到达该时段的
    # 首个数据窗口（早于时段点，如 09:10 跑 "11:40" 档），行情源只有昨收快照、无当日成交，
    # 报告会被下游误读为"跌停/冰点"。此处显式标注数据快照口径，不改报告正文。
    try:
        _hh, _mm = (int(x) for x in str(stage).split(":")[:2])
        _slot_min = _hh * 60 + _mm
        _now_dt = datetime.now()
        _now_min = _now_dt.hour * 60 + _now_dt.minute
        if _now_min < _slot_min:
            lines.append(f"⚠️ 【数据快照口径】当前 {now_str[11:16]} 早于 {stage} 时段窗口，"
                         f"本次为提前/补跑生成：行情源尚未产生当日成交，现价、温度、涨停家数等"
                         f"均为盘前空值(0)，不代表跌停或冰点。时段语义请以盘中正式触发的那份报告为准。")
            lines.append("")
    except Exception as _e:
        print(f"[slot_label] 时段标注跳过: {_e}")
    
    # 1. 昨日尾盘实盘个股战绩回测（持仓看板 + 收益统计）
    #    10:40 是框架内唯一的卖出决策点 → emit_sell=True 才会生成待确认卖出单；
    #    11:40/13:40/14:40/盘后只做复盘看板，emit_sell=False 不重复派卖单。
    if stage in ["10:40", "11:40", "13:40", "14:40", "盘后复盘"]:
        backtest_lines = run_backtest(xgb, emit_sell=(stage == "10:40"))
        lines.extend(backtest_lines)
        lines.append("")
        lines.append("===========================================================\n")

    # 2. 跨日连续性追踪
    if verify_lines:
        lines.extend(verify_lines)

    # 3. 旁系前瞻研报情报
    intel_lines = get_intel_brief_lines()
    lines.extend(intel_lines)
    lines.append("===========================================================\n")

    # 3.5 中线产业趋势研究池
    trend_lines = get_trend_research_lines()
    lines.extend(trend_lines)
    lines.append("===========================================================\n")

    # ===== 【2026-10-21 修 P2-8】10:40 报告首行固定打印持仓状态 =====
    # 痛点：10:40 报告存在但首行不说明「为何无卖出动作」→ 后续复盘把它误判为
    # 「通道缺失」（10-15 就出现过 10:40 报告缺失的误判）。
    # 现在持仓为 0 时首行明写「本报告仅作规则留痕」，让归因不再靠猜。
    if stage == "10:40":
        try:
            _db10 = load_backtest_db()
            _pos10 = _db10.get("active_positions") or []
            if not _pos10:
                lines.append("【10:40 持仓处理 · 规则留痕】")
                lines.append("  • 当前持仓 0 只 → 本报告仅作规则留痕（无持仓可评估，本时段不产生卖出指令）")
                lines.append("  • 说明：这是正常空转，非通道缺失；卖出通道仅在有持仓时才产生待确认卖出单。")
                lines.append("")
        except Exception as _e:
            print("[report_1040] 持仓状态读取失败(跳过首行提示): %s" % _e)

    # 4. 盘面全景与资金底座
    lines.append("【一、 盘面全景与资金底座】")
    lines.append(f"  • 指数表现: 上证 {sh_pct}% | 深证 {sz_pct}% | 创业板 {cy_pct}%")
    lines.append(f"  • 两市成交量能: {total_vol:.0f} 亿元")
    # ADR-0049：GJD 宽基 ETF 资金面 —— 「资金底座」这一节本来就该回答「长钱在买还是在卖」
    try:
        _gline = _gjd_flash_seg(_ACTIVE_REVIEW_GATE or load_review_gate(), _gjd_snapshot())
        if _gline:
            lines.append(f"  • 国家队资金: {_gline}")
    except Exception as _e:
        lines.append(f"  • 国家队资金: 读取失败（{type(_e).__name__}: {_e}）")
        print(f"[WARN] 静默异常已显式化 @report_gjd: {_e}")
    lines.append(f"  • 市场做多真实温度: {xgb['temperature']:.1f}%")
    lines.append(f"  • 真实赚钱效应: 上涨 {xgb['rise_count']} 家 | 下跌 {xgb['fall_count']} 家")
    lines.append("")

    # 4. 游资情绪阵眼
    lines.append("【二、 游资情绪阵眼 (打板生态)】")
    lines.append(f"  • 涨跌停分布: 涨停 {xgb['limit_up']} 家 | 跌停 {xgb['limit_down']} 家")
    lines.append(f"  • 昨日涨停今日溢价: {xgb['premium']:.2f}% (接力热度指标)")
    lines.append(f"  • 封板炸板率: {xgb['broken_ratio']:.1f}% (资金做多合力)")
    lines.append(f"  • 市场空间龙: {dragon_str}")
    lines.append(f"  • 连板梯队树: {tier_str}")
    lines.append(f"  • 最强板块主线: {top_reasons_str}")
    lines.append("")

    # 5. 锁单强度 Top 10 Ranks
    lines.append("【三、 开盘/日内打板强度 Top 10 (筹码抱团中枢)】")
    if not lock_rows:
        lines.append("  (暂无明显锁单资金数据)")
    else:
        for idx, r in enumerate(lock_rows[:10]):
            lines.append(f"  [{idx+1}] {r['name']} ({r['symbol']}) - 锁单强度: {r['lock']:.3f} | 连板数: {r['limit_up_days']} | 主线概念: {','.join(r['plates'])}")
    lines.append("")

    # 6. 👑 底部盘整突破 - 首板新金龙潜力股监控 (突破放量、主力资金一下子涌入新主线)
    lines.append("【四、 底部突围 - 资金巨量净流入首板新星 (成龙潜质监控)】")
    lines.append("  🔗 以上首板新星已汇入【九、统一操作池】，参与次日 1进2 晋级追踪与买入决策闭环。")
    if not first_board_candidates:
        lines.append("  • 今日暂无明显底部放量突围的首板个股。")
    else:
        lines.append("  📢 监控到以下个股今日刚启动“首板突破”，且主力大额资金猛烈流入：")
        for idx, x in enumerate(first_board_candidates[:5]): # 取前 5 只资金流入最大首板
            lines.append(f"  • {x['name']} ({x['symbol']}) | 今日突破成交额: {x['turnover_亿']:.2f} 亿 | 换手率: {x['tr_pct']:.1f}% | 概念: {','.join(x['plates'])}")
            lines.append(f"    ➡️ 【新龙潜力深度剖析】: 底部在箱体盘整多日，今日主力资金猛然大幅流入，吃掉上方所有抛盘死死封板。筹码换手极度充分。建议明日 10:40 重点观察其能否打出高溢价晋级二板，有极强连板成龙概率！")
    lines.append("")

    # 6.5 首板新星次日晋级追踪（昨日启动股闭环，仅监控提示不自动下单）
    followup_lines = build_first_board_followup(prev_state, stage)
    if followup_lines:
        lines.extend(followup_lines)

    # 7. 核心题材 [龙头 + 中军] 拆解
    lines.append("【五、 核心题材板块“双雄”拆解 [龙头+中军]】")
    lines.append("  🔗 领涨龙与中军已汇入【九、统一操作池】，参与共识加权与买入决策。")
    if not pool:
        lines.append("  (当前题材由于缺乏封板效应，连板生态匮乏，暂未产生核心龙头与中军对立结构)")
    else:
        hot_plates_list = [t[0] for t in sorted_reasons if t[0] not in ['ST股', '业绩增长']][:5]
        for p_name in hot_plates_list:
            limit_ups = plate_limit_up_stocks[p_name]
            limit_ups.sort(key=lambda x: (
                x.get('limit_up_days', 0), 
                -x.get('first_limit_up', float('inf')), 
                x.get('buy_lock_volume_ratio', 0)
            ), reverse=True)
            leader = limit_ups[0] if limit_ups else None
            
            all_actives = plate_all_active_stocks[p_name]
            all_actives.sort(key=lambda x: x.get('non_restricted_capital', 0), reverse=True)
            top5_capitals = all_actives[:5]
            top5_capitals.sort(key=lambda x: (x.get('non_restricted_capital', 0) * x.get('turnover_ratio', 0) * x.get('price', 0)), reverse=True)
            core = top5_capitals[0] if top5_capitals else None
            
            lines.append(f"  ⚡️ 【{p_name}】 (板块涨停: {len(limit_ups)}家)")
            if leader:
                ld_info = format_stock_info(leader)
                tm_val = leader.get('first_limit_up')
                tm_str = datetime.fromtimestamp(tm_val).strftime('%H:%M:%S') if tm_val else '-'
                lines.append(f"    - [龙头] {ld_info['name']} ({ld_info['symbol']}) | {ld_info['limit_days']}连板 | 首封: {tm_str} | 强度: {ld_info['lock_vol']:.3f} | 涨幅: {ld_info['change_pct']*100:.2f}%")
            if core:
                cr_info = format_stock_info(core)
                is_same = (leader and leader.get('symbol') == core.get('symbol'))
                prefix = "身兼中军" if is_same else "中军"
                lines.append(f"    - [{prefix}] {cr_info['name']} ({cr_info['symbol']}) | 涨幅: {cr_info['change_pct']*100:.2f}% | 流通盘: {cr_info['capital_亿']:.1f}亿 | 估算成交额: {cr_info['turnover_amt_亿']:.1f}亿")
        if not hot_plates_list:
            lines.append("  (暂无聚焦题材)")
    lines.append("")

    # 8. 资金异动分钟级涨速监控
    lines.append("【六、 资金异动分钟级拉升先锋】")
    if xgb['hot_sectors_raw']:
        for i, sector in enumerate(xgb['hot_sectors_raw']):
            stocks = []
            for s in sector.get('stocks', [])[:2]:
                stock_name = s.get('name') or s.get('stock_chi_name') or s.get('symbol') or '未知'
                mtm = s.get('mtm', s.get('change_percent', s.get('pcp', 0)))
                stocks.append(f"{stock_name}({mtm*100:+.1f}%)")
            pcp = sector.get('pcp', sector.get('change_percent', 0))
            plate_name = sector.get('plate_name') or sector.get('name') or '未知板块'
            pioneer_text = ', '.join(stocks) if stocks else '暂无成分股明细'
            lines.append(f"  [{i+1}] {plate_name} (整体涨速: {pcp*100:+.2f}%) - 异动先锋: {pioneer_text}")
    else:
        lines.append("  (暂无明显资金异速拉升迹象)")
    lines.append("")

    # 9. 候选跟踪与具体买入操作建议（仅尾盘时段生成；早盘/盘中报告不显示尾盘建议，避免无效占位）
    if stage in ["14:40", "盘后复盘"]:
        lines.append("【七、 今日尾盘个推荐与实战布兵策略】")
        if risk_gate_triggered:
            lines.append("  • 🧊 系统性风险闸门已触发：指数/市场温度/炸板跌停结构不支持开新仓。")
            lines.append("  • 今日候选只保留为观察池，不生成买入指令；优先处理持仓风险和等待次日确认。")
        elif not _buy_candidates:
            lines.append("  • 当前时段无符合短线高做多概率筛选标准的个股，建议空仓或锁仓观望。")
        else:
            # 基于当前大盘建议的仓位 advice_pos 动态调整单股分配仓位
            pos_per_stock = "5%-10% (轻仓试错)"
            if "70%" in advice_pos or "80%" in advice_pos:
                pos_per_stock = "20%-25% (重兵突击)"
            elif "50%" in advice_pos:
                pos_per_stock = "15%-20% (适度建仓)"
            elif "20%" in advice_pos or "30%" in advice_pos:
                pos_per_stock = "10% (轻仓博弈)"
            elif "0%" in advice_pos:
                pos_per_stock = "绝对空仓 (建议观望)"

            lines.append(f"  • 建议单股配置仓位: {pos_per_stock} (大盘推荐总仓位: {advice_pos})")
            lines.append("")
            for idx, c in enumerate(_buy_candidates):
                lines.append(f"  • [闭环候选 {idx+1}] {c['name']} ({c['symbol']}) | 连板: {c.get('board_days',1)}板 | 共识源: {','.join(c.get('sources',[]))} | 打分: {c.get('score',0)}")
                lines.append(f"    - 汇总共线: {' / '.join(c.get('sources',[]))} 模块共同指向（已汇入统一操作池）")
                lines.append(f"    - 实战逻辑: {c.get('thesis','')}")
                if "绝对空仓" in pos_per_stock:
                    lines.append(f"    - 操作指令: 🚨 当前大盘大情绪极度退潮，今日尾盘【无条件放弃买入】，保持空仓。")
                else:
                    lines.append(f"    - 操作指令: 🛒 建议在尾盘 14:55-15:00 分批吸入 【{pos_per_stock.split(' ')[0]}】 仓位，次日 10:40 执行闭环决策判定。")
                _rh_ref = real_holding_reference(c.get('symbol',''), c.get('name',''), 'BUY')
                if _rh_ref:
                    lines.append(_rh_ref)
                lines.append("")
        lines.append("")
    else:
        lines.append("【七、 今日尾盘个推荐与实战布兵策略】（将于 14:40 尾盘时段结合当日盘中数据生成，早盘/盘中不提前给出）")
        lines.append("")

    # 10. 交叉验证
    if stage in ["14:40", "盘后复盘"]:
        cross_lines = get_intraday_cross_validation()
        lines.extend(cross_lines)
        lines.append("")

    # 11. 本时段推演与次日建议
    lines.append("【八、 推演与次日指导建议】")
    lines.append(f"  • 盘口定性: {emotion} (涨跌广度比 {_breadth_ratio:.2f})")
    # 时段定调文案与五时段框架一一对应（原 11:30/13:50 分支的 stage 名从不存在，是死文案）
    if stage == "09:25":
        lines.append("  • 早盘打板: 回溯昨日涨停群，取今日竞价仍强的标的；多因子共振过线才挂涨停排单，否则空手不打。")
    elif stage == "10:40":
        lines.append("  • 持仓处理: 全天唯一卖出决策点。不强的出（全清/减半），强的留（锁仓）；此点之后当日不再派卖单。")
    elif stage == "11:40":
        lines.append("  • 午盘复盘: 只做早盘行情分析与账户梳理，不派任何单。判断早盘拉升虚实，为午后强势选股定调。")
    elif stage == "13:40":
        lines.append("  • 午后强势: 在当日资金净流入最多的趋势板块里，选有涨停趋势但尚未封板的优质票，现价买入布局冲板；次日10:40按强弱出清或减半。")
    elif stage == "14:40" or stage == "盘后复盘":
        lines.append("  • 尾盘龙首阴: 昨日涨停今日0轴横盘(无大资金流出无放量跌)的热点票，尾盘低吸布局次日；次日10:40按强弱出清或减半。选股数据已存档供明日闭环验证。")

    lines.append("\n  >>> [全新战术执行指令] <<<")
    lines.append(f"    1. 仓位控制: 建议保持在 {advice_pos}")
    lines.append(f"    2. 进攻方向: {advice_atk}")
    lines.append(f"    3. 防守警示: {advice_def}")
    if "14:40" in stage or "盘后复盘" in stage:
        lines.append(f"    4. 明日验证: 重点关注空间龙【{dragons[0] if dragons else '暂无'}】表现，以及主线板块【{top_reasons[0] if top_reasons else '无'}】的承接力。")

    # ===== 九、统一操作池总览(全模块共识 → 操作指令闭环) =====
    lines.append("【九、 统一操作池总览 (全模块共识 → 操作指令闭环)】")
    # ===== 【2026-10-21 修 P1-4】额度利用率诊断（暴露偏差，避免"额度算了却没用"隐形流失）=====
    try:
        _dbq = load_backtest_db()
        _eqq = float(_dbq.get("total_equity", _dbq.get("initial_capital", 0.0)) or 0.0)
        _bq = [i for i in (build_action_items(stage, _buy_candidates, _mt, _board) or [])
               if i.get("type") in ("BUY", "BUY_LIMIT")]
        _actq = sum(float(i.get("amount") or 0) for i in _bq)
        _gq = load_review_gate() or {}
        _capq = _rg_float(_gq.get("position_cap_pct"))
        _picksq = _rg_int(_gq.get("max_picks")) or 0
        _singleq = _rg_float(_gq.get("single_pos_cap_pct"))
        if _capq and _picksq and _eqq:
            _theo_pct = _capq / _picksq
            _theo_amt = _theo_pct / 100.0 * _eqq
            _act_single = (_actq / len(_bq)) if _bq else 0.0
            _util = (_actq / _theo_amt * 100) if (_theo_amt and _actq) else 0.0
            lines.append(f"  • 额度诊断: 闸门段上限 {_capq:g}% ÷ max_picks {_picksq} = 理论单笔 {_theo_pct:.1f}%"
                         f"（¥{_theo_amt:,.0f}）｜实际单笔 ¥{_act_single:,.0f}"
                         f"｜额度利用率 {_util:.1f}%"
                         f"｜闸门单票上限 {_singleq if _singleq else '-'}")
            if _bq and _actq < _theo_amt * 0.5:
                lines.append("    ⚠️ 额度利用率偏低：本时段实际下单额不足理论单笔的一半，"
                             "差额来自可成交性/触板开板等减法闸门（属正常纪律约束），"
                             "非额度计算错误。")
    except Exception as _e:
        print("[quota_diag] 额度诊断异常(跳过): %s" % _e)
    if _op_pool:
        held_n = sum(1 for e in _op_pool.values() if e.get("status") == "held")
        track_n = sum(1 for e in _op_pool.values() if e.get("status") == "tracking")
        lines.append(f"  • 池中标的: 共 {len(_op_pool)} 只（跟踪中 {track_n} / 持仓 {held_n}）。多源汇总: 底部突围·连板主线·题材双雄·情报·龙头资金·午后强势·龙首阴·补涨池。")
        if _ranked_buys:
            _bs, _bd = board_score(_board, _mt)
            # ===== 【2026-10-21 修 P2-7】阶段标签按 stage 渲染，勿硬编码 =====
            # 痛点：10:42 的 10:40 报告标题写「14:40 尾盘龙首阴池低吸布局」——
            # 因为旧代码 else 分支兜底成了 14:40 文案，会误导复盘归因
            # （复盘看到 14:40 字样以为读了尾盘报告）。
            if stage == "09:25":
                _dispatch_note = f"{stage} 早盘集合竞价涨停池挂涨停排单(多因子综合分≥{BOARD_SCORE_GATE})"
            elif stage == "13:40":
                _dispatch_note = "13:40 午后强势池未涨停现价买入(冲板趋势)"
            elif stage == "14:40":
                _dispatch_note = "14:40 尾盘龙首阴池低吸布局"
            else:
                # 其余时段（10:40 持仓处理 / 11:40 午盘研判）**不派买单**，
                # 明确写清本时段性质，避免出现别的时段的文案造成归因错乱。
                _dispatch_note = {
                    "10:40": "10:40 持仓处理通道（本时段仅卖出决策，不派买单）",
                    "11:40": "11:40 午盘研判通道（本时段只做分析，不派买单）",
                }.get(stage, f"{stage} 时段（不派买单）")
            lines.append(f"  📢 今日排序买入候选 Top {len(_ranked_buys)}（{_dispatch_note}；当前打板综合分 {_bs} 分）：")
            lines.append(f"      打板因子明细: {_bd}")
            for i, c in enumerate(_ranked_buys):
                lines.append(f"    [{i+1}] {c['name']} ({c['symbol']}) | 连板 {c.get('board_days',1)}板 | 共识源: {','.join(c.get('sources',[]))} | 打分 {c.get('score',0)} | 信号 {c.get('signal',0)}")
                _rh_ref = real_holding_reference(c.get('symbol',''), c.get('name',''), 'BUY')
                if _rh_ref:
                    lines.append(_rh_ref)
            # ===== 【2026-10-21 修 P1-5】剔除原因显式化 =====
            # 痛点：此前 Top 段只写「排序买入候选 Top N」，不写「剔除了谁、为什么」，
            # 而 order_plan.buy_candidates 可能为 0 → 用户看报告以为"漏单"，
            # 实为通道按规则主动剔除（10-20 万丰/东方嘉盛即被均价闸门剔除）。
            # 现在把剔除名单与「今日是否生成确认单」与 buy_candidates 同源打印。
            # 【措辞区分】shadow 命中的票**并未被剔除**（仍在候选里），必须标注清楚，
            # 否则会出现"既在 Top 候选里、又在剔除名单里"的困惑。
            # 【2026-10-21】竞价参考展示（买入侧）：让「这一分怎么来的」可核对
            try:
                _arows = [c for c in _ranked_buys if c.get("auction_bonus")]
                if _arows:
                    lines.append("      📇 集合竞价参考（09:25 竞价画像 → 今日买卖两侧共用）：")
                    for _c in _arows[:6]:
                        lines.append("         - %s %s | %s" % (
                            _c.get("name", ""), _c.get("symbol", ""),
                            _c.get("auction_why") or "竞价参考"))
            except Exception:
                pass
            _drop_rows = list(_REVIEW_GATE_DROPS or [])
            _shadow_rows = []
            for _w in (_dist_high_would or []):
                if _dist_high_mode == "enforce":
                    _drop_rows.append({"name": _w.get("name", ""), "symbol": _w.get("symbol", ""),
                                       "reason": _w.get("reason", "")})
                else:
                    _shadow_rows.append(_w)
            if _drop_rows:
                lines.append(f"      ⛔ 已剔除 {len(_drop_rows)} 只（下方为剔除原因，与派单结论同源）：")
                for _d in _drop_rows[:8]:
                    lines.append(f"         - {_d.get('name','')}（{_d.get('symbol','')}）— {_d.get('reason','')}")
                if len(_drop_rows) > 8:
                    lines.append(f"         - …另有 {len(_drop_rows) - 8} 只，详见 order_plan_{stage.replace(':','')}.json")
            if _shadow_rows:
                # 阈值说明用绝对值：派单价须 ≥ 当日最高 × 0.995（即距高不超过 0.5%）
                _thr_pct = (1 - REVIEW_DIST_HIGH_RATIO) * 100
                lines.append(f"      ⚠️ 伪强势观察 {len(_shadow_rows)} 只（**未剔除**，仍在候选中，"
                             f"影子期记录中；判据：派单价须 ≥ 当日最高×{REVIEW_DIST_HIGH_RATIO}"
                             f"，即距高不超过 {_thr_pct:.1f}%）：")
                for _w in _shadow_rows[:8]:
                    lines.append(f"         - {_w.get('name','')}（{_w.get('symbol','')}）— 距当日最高 {_w.get('dist_pct',0):.2f}%"
                                 f"（影子记录，暂不拦截）")
            # 「今日是否生成确认单」必须与 order_plan.buy_candidates 同源，不能靠猜
            _will_confirm = "是" if _buy_candidates else "否"
            lines.append(f"      📌 本时段是否生成买入确认单：{_will_confirm}"
                         f"（判据：order_plan.buy_candidates {'非空' if _buy_candidates else '为空'}）")
        else:
            lines.append("  • 今日无达买入阈值的候选（弱市或共识不足），空仓观望。")
            if _REVIEW_GATE_DROPS:
                lines.append(f"      ⛔ 已剔除 {len(_REVIEW_GATE_DROPS)} 只（下方为剔除原因，与派单结论同源）：")
                for _d in list(_REVIEW_GATE_DROPS)[:8]:
                    lines.append(f"         - {_d.get('name','')}（{_d.get('symbol','')}）— {_d.get('reason','')}")
            lines.append("      📌 本时段是否生成买入确认单：否（buy_candidates 为空 → 不派单）")        # 今日真实共振核心股(≥2 个买入时段共同推荐的 real _buy_candidates)
        _res = get_resonance_core()
        if _res:
            _res_txt = ", ".join("%s(%s)×%d" % (n, s, len(ss)) for s, n, ss in _res)
            lines.append(f"  🔥 今日真实共振核心股(≥2买入时段重合): {_res_txt}")
        lines.append("  • 池中全量标的(共识源 / 连板进度 / 状态)：")
        for sym, e in sorted(_op_pool.items(), key=lambda kv: kv[1].get('signal', 0), reverse=True):
            lines.append(f"    - {e.get('name','?')} ({sym}) | 板数 {e.get('board_days',1)} | 源: {','.join(e.get('sources',[]))} | {e.get('status','tracking')}")
    else:
        lines.append("  • 操作池为空，暂无候选。")

    full_report = "\n".join(lines)
    print(full_report)

    # 保存报告至 WilliamLi 的新指定报告目录 Work/reports
    date_str = datetime.now().strftime('%Y%m%d')
    time_str = datetime.now().strftime('%H%M')
    filename = os.path.join(REPORTS_DIR, f"pro_report_{date_str}_{time_str}_{stage.replace(':', '')}.txt")
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(full_report)
    except Exception as e:
        print(f"[WARN] 静默异常已显式化 @3130: {e}")

    # ===== 弹窗派发：严格一一对应三时段独立池框架 =====
    #  09:25 早盘集合竞价涨停池 → 操作闪卡 + 待确认打板单(挂涨停排单) → 确认台
    #  10:40 持仓处理(唯一卖点) → 待确认卖出单 → 确认台
    #  11:40 纯复盘 → 账户梳理弹窗，绝不派单
    #  13:40 午后强势池 → 待确认买入单(未涨停现价) → 确认台
    #  14:40 尾盘龙首阴池 → 待确认买入单(低吸) → 确认台
    try:
        # _mt / _board 已在上方「统一决策参数」处计算，弹窗与回测账户共用同一决策结果
        if stage in BUY_STAGES:
            # 09:25 打板单已在报告顶端生成；13:40(午后强势)/14:40(龙首阴) 的未涨停买入单在此生成
            if stage in ("13:40", "14:40"):
                _buy_intents_plain = get_buy_intents(stage, _buy_candidates, _mt, _board)
                if _buy_intents_plain:
                    _pending_path = write_pending_buy_confirm(stage, _buy_intents_plain)
                    if _pending_path:
                        _PENDING_FILE_NOTES.append(f"{stage}待确认买入单: {os.path.basename(_pending_path)}")
            # 任何派单时段都挂一个 15:00 兜底确认子进程（当日只挂一次，避免重复处理）
            if _PENDING_FILE_NOTES:
                # 13:40/14:40 时，早间的打板排队单可能已严重失效（如跌停），先标记
                if stage in ("13:40", "14:40"):
                    _mark_stale_board_pending_buys()
                _spawn_deadline_confirm()
            flashcard_text = build_ops_flashcard(stage, _buy_candidates, _mt, _board)
            # 弹窗里提示本时段产生的所有待确认单，引导用户收盘前确认实际成交/卖出
            if _PENDING_FILE_NOTES:
                flashcard_text += "\n\n📝 已生成【待确认单】(均不自动成交/平仓)：\n"
                for _n in _PENDING_FILE_NOTES:
                    flashcard_text += f"   • {_n}\n"
                flashcard_text += ("   请在【工作台 · 操作确认台】逐笔确认买入/放弃（收盘前完成，可填实际手数）；"
                                   "15:00 自动按确认结果写库，未确认视为未成交/未卖出。")
            flashcard_path = os.path.join(REPORTS_DIR, f"intraday_alert_{stage.replace(':', '')}.txt")
            with open(flashcard_path, 'w', encoding='utf-8') as f:
                f.write(flashcard_text)
            # 优先级①（最高）：本机操作弹窗 —— 秒级、同步即时
            # 【2026-09-22 起】桌面版【交互式确认台 GUI(confirm_gui.py)】已停用（ADR-0031）：
            #   工作台(market-monitor /workbench)自带的操作确认台已稳定运行，且会直接回写本
            #   时段生成的 pending_buy_confirm_*.json（status→confirmed + actual_lots），
            #   15:00 兜底子进程照旧写库；买入改为单一入口，避免同一批单两处重复确认。
            #   ⚠【2026-09-23 更正】下面两条旧结论已过期：工作台【支持卖出确认】——
            #   app.py 提供 POST /api/confirm/sell（买卖双区、手数可改、三项校验、
            #   处理完立即写库）。10:40 与 M9 实时盯盘产出的 pending_sell_confirm_*
            #   均可在工作台确认，不存在"没有界面可确认"的问题。
            #   本机现在只弹【只读操作指令闪卡】，作用是提示"去工作台确认"，不再承载确认输入。
            open_report_in_textedit(flashcard_path, f"操作指令 {stage}（请到工作台操作确认台确认）")
            # 优先级②：微信推送 —— 网络 IO，独立线程异步，不阻塞弹窗与主流程时效
            _t = threading.Thread(target=send_wechat_pushplus,
                                  args=(f"操作指令 - {stage}", flashcard_text),
                                  kwargs={"template": "txt"}, daemon=False)
            _t.start()
            _WECHAT_THREADS.append(_t)
        elif stage == "11:40":
            review_text = build_account_review()
            flashcard_path = os.path.join(REPORTS_DIR, f"intraday_alert_{stage.replace(':', '')}.txt")
            with open(flashcard_path, 'w', encoding='utf-8') as f:
                f.write(review_text)
            # 优先级①：Mac 弹窗同步（即时）
            open_report_in_textedit(flashcard_path, f"午盘账户梳理 {stage}")
            # 桌面弹窗已足够，不再发系统通知（避免误导）
            # 优先级②：微信异步推送
            _t = threading.Thread(target=send_wechat_pushplus,
                                  args=("午盘账户梳理 - " + today_str, review_text),
                                  kwargs={"template": "txt"}, daemon=False)
            _t.start()
            _WECHAT_THREADS.append(_t)
        elif stage == "10:40":
            # 10:40 = 框架内唯一卖出决策点：持仓去弱留强。
            # run_backtest 产出卖出建议 → 待确认单交给工作台确认台；无卖出单则静默弹完整市场报告。
            if _PENDING_FILE_NOTES:
                _spawn_deadline_confirm()
            # 桌面确认台已停用（ADR-0031），统一回到只读报告弹窗
            _t1040 = "市场报告 10:40" if _PENDING_FILE_NOTES else "市场报告 10:40 (持仓全部留强，无需操作)"
            if _PENDING_FILE_NOTES:
                _t1040 += "（请到工作台操作确认台确认）"
            open_report_in_textedit(filename, _t1040)
        elif stage == "盘后复盘":
            # 盘后复盘：弹窗只读报告 + 异步推送到微信（与买入时段同源开关 WECHAT_PUSH_ENABLED）
            open_report_in_textedit(filename, f"盘后复盘 {today_str}")
            _t = threading.Thread(target=send_wechat_pushplus,
                                  args=("盘后复盘 - " + today_str, full_report),
                                  kwargs={"template": "txt"}, daemon=False)
            _t.start()
            _WECHAT_THREADS.append(_t)
    except Exception as e:
        # 曾经这里是 `pass`，弹窗环节任何异常都被静默吞掉，
        # 表现为"任务跑完、报告落盘、但屏幕上什么都没有且日志无一行报错"。
        import traceback
        print(f"[Popup] ❌ 弹窗环节异常: {e}")
        traceback.print_exc()

    # 需求②：产出订单计划 JSON + 接管账户派发（默认 DRY_RUN，不真实下单）
    # 仅三个买入时段(09:25 早盘打板 / 13:40 午后强势 / 14:40 尾盘龙首阴)产出订单
    try:
        if stage in BUY_STAGES:
            op = build_order_plan(stage, _buy_candidates, advice_pos,
                                  xgb.get('temperature', 50) if isinstance(xgb, dict) else 50,
                                  _board)
            dispatch_orders(stage, op)
    except Exception as e:
        print(f"[broker] 订单派发跳过: {e}")

    # ===== 统一操作池：决策后与实际持仓对账并持久化(跨日闭环) =====
    try:
        _reconcile_pool_status(_op_pool, load_backtest_db())
        save_operation_pool(_op_pool)
    except Exception as e:
        print(f"[operation_pool] 持久化失败: {e}")

    # 等待所有微信异步推送线程完成（最多 10s），确保推送不丢；Mac 弹窗已先行弹出
    for _t in _WECHAT_THREADS:
        _t.join(timeout=10)
    _WECHAT_THREADS.clear()

if __name__ == "__main__":
    # 清理代理环境变量，防止本地代理影响国内金融API（新浪/选股宝）的直接请求
    for var in ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy']:
        os.environ.pop(var, None)
        
    # 待确认写库命令（必须在交易日拦截【之前】处理：用户点了确认就必须写库，
    # 哪怕已收盘/周末补确认；且 GUI 提交走 --now 立即写库，不等 15:00）。
    #   confirm        → 等待至 15:00 截止后写库（14:40 spawned 子进程 / plist 15:00 备份触发）
    #   confirm --now  → 立即写库（confirm_gui.py 用户主动提交时调用，避免盘中提交被
    #                    _wait_until_deadline 阻塞到 15:00 导致 GUI 侧 120s 超时假失败）
    if len(sys.argv) > 1 and sys.argv[1] == "confirm":
        # 定向模式（2026-09-22 新增，服务工作台操作确认台 ADR-0032）：
        #   confirm --now --session 13:40  → 只处理该买入时段的待确认单
        #   confirm --now --sell           → 只处理待确认卖出单
        #   两个都不带                     → 全量扫描（15:00 作业/兜底，行为完全不变）
        # 定向模式跳过 wait_for_network：交互式确认时价格与手数都已随单带入，
        # 不再需要为"卖价留0"去拉现价；而 wait_for_network 最坏会阻塞 75s，
        # 放在 Web 请求里不可接受。
        _cargv = sys.argv[2:]
        _only_session = None
        if "--session" in _cargv:
            _ci = _cargv.index("--session")
            _only_session = _cargv[_ci + 1] if _ci + 1 < len(_cargv) else None
        _only_sell = "--sell" in _cargv
        _targeted = bool(_only_sell or _only_session)
        if "--now" not in _cargv:
            _wait_until_deadline("15:00")
        if not _targeted:
            wait_for_network()   # 卖价留0时需拉现价
        if _only_sell:
            confirm_pending_sell()
        elif _only_session:
            confirm_pending_buy(_only_session)
        else:
            run_pending_confirmations()
        sys.exit(0)

    # 等待网络连通（防止 macOS 休眠唤醒后 LaunchAgent 执行过快，网络设备尚未就绪）
    wait_for_network()

    if not is_trading_day():
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 非交易日 (周末/节假日)，跳过报告生成。")
        sys.exit(0)

    # 预热模式：仅强制同步刷新情报，不生成完整报告。
    # 由 LaunchAgent 在每个弹窗前5分钟调用，保证弹窗拿到的是当时最新最热的情报。
    # 注意：此处在 argparse 解析之前，必须用 sys.argv 判断，不能引用尚未定义的 args。
    if len(sys.argv) > 1 and sys.argv[1] == "refresh_intel":
        ok, msg = refresh_intel_brief_if_stale(max_age_minutes=0, blocking=True)
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [refresh_intel] 情报刷新结果: {msg}")
        sys.exit(0)

    current_stage = "盘后复盘"
    send_email = False

    # 解析参数
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("stage_arg", nargs="?", default="auto")
    parser.add_argument("--session", choices=["opening", "midday", "late", "postmarket"])
    parser.add_argument("--send", action="store_true")
    # 人工强制执行：绕过「时段窗口」与「行情就绪」两道闸（数据源故障现场排查用）
    parser.add_argument("--force", action="store_true")
    # 兼容其它脚本的废弃参数
    parser.add_argument("--schedule", action="store_true")
    parser.add_argument("--log-path", default="")
    parser.add_argument("--report-dir", default="")
    args = parser.parse_args()

    # 确定时段 (兼容 session 传入)
    sess_map = {"opening": "09:25", "midday": "11:40", "late": "14:40", "postmarket": "盘后复盘"}
    
    if args.session:
        current_stage = sess_map.get(args.session, "盘后复盘")
    elif args.stage_arg != "auto":
        current_stage = args.stage_arg
    else:
        # 自动探测
        now_hm = datetime.now().strftime("%H:%M")
        if "09:15" <= now_hm <= "09:35": current_stage = "09:25"
        elif "10:30" <= now_hm <= "10:50": current_stage = "10:40"
        elif "11:25" <= now_hm <= "11:55": current_stage = "11:40"
        elif "13:25" <= now_hm <= "13:55": current_stage = "13:40"
        elif "14:25" <= now_hm <= "14:55": current_stage = "14:40"

    send_email = args.send

    # ==== 闸门 1：时段窗口守卫（2026-10-08 新增，ADR-0042）====
    # 桌面端 automation 在 app 唤醒后会补跑错过的日程（如 09:10 跑 "11:40" 档），
    # 盘前空快照会被写成"当日报告"并覆盖跨日基准。不在窗口内直接退出：
    # 不生成报告、不写任何状态文件、不弹窗。
    _ok_win, _why_win = slot_window_ok(current_stage)
    if not _ok_win and not args.force:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 不在 {current_stage} 时段窗口"
              f"（{_why_win}），跳过报告生成（零副作用）。如需强制执行请加 --force。")
        sys.exit(0)
    if not _ok_win:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ⚠️ --force 覆盖时段窗口拦截：{_why_win}")

    # ==== 闸门 2：行情就绪守卫（同上）====
    # 窗口内但行情源只有昨收快照（成交额 0）→ 判定盘前/数据源异常，同样零副作用退出。
    _ok_rd, _why_rd = market_data_ready(current_stage)
    if not _ok_rd and not args.force:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 行情未就绪（{_why_rd}），"
              f"跳过报告生成（零副作用）。若确认数据源恢复，请加 --force 强制生成。")
        sys.exit(0)
    if not _ok_rd:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ⚠️ --force 覆盖行情就绪拦截：{_why_rd}")

    devnull = open(os.devnull, 'w')
    old_stderr = sys.stderr
    sys.stderr = devnull
    try:
        generate_report(current_stage)
    finally:
        sys.stderr = old_stderr

# -*- coding: utf-8 -*-
"""市场数据抓取器：后台线程 + 内存缓存 + 板块涨速跟踪

数据源（均已实测可用）：
  指数   : 腾讯批量接口 qt.gtimg.cn（~0.1s）
  板块   : 同花顺行业板块 ak.stock_board_industry_summary_ths()
  涨停池 : ak.stock_zt_pool_em / stock_zt_pool_dtgc_em / stock_zt_pool_zbgc_em
  全A    : 新浪 ak.stock_zh_a_spot()（慢，用于宽度与异动筛选）
"""
import os
import json
from pathlib import Path

# 必须在导入 akshare/requests 前清掉代理环境变量（本地代理会断连行情源）
for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
           "all_proxy", "ALL_PROXY"]:
    os.environ.pop(_k, None)

import threading
import time
import traceback
from collections import deque
from datetime import datetime

import requests
import akshare as ak

import config


# ---------------------------------------------------------------------------
# 交易时段判断
# ---------------------------------------------------------------------------
def market_status(now=None):
    """返回 (状态码, 描述)
    状态码: pre / trading / lunch_break / closed / closed_all_day
    """
    now = now or datetime.now()
    if now.weekday() >= 5:
        return "closed_all_day", "周末休市"
    t = now.hour * 100 + now.minute
    if t < 915:
        return "pre", "开盘前"
    if t <= 1130:
        return "trading", "交易中"
    if t < 1300:
        return "lunch_break", "午间休市"
    if t <= 1500:
        return "trading", "交易中"
    return "closed", "已收盘"


# ---------------------------------------------------------------------------
# 板块涨速跟踪器
# ---------------------------------------------------------------------------

# 一级行业 → 热门 ths 概念名精选映射（ths 概念无盘中数据，硬编码更准）
INDUSTRY_CONCEPT_MAP = {
    "通信设备": ["光纤概念", "CPO", "铜缆高速连接"],
    "通信服务": ["5G", "华为概念", "算力租赁"],
    "光学光电子": ["光刻机", "光刻胶", "光学元件", "VR"],
    "消费电子": ["苹果产业链", "华为概念", "折叠屏"],
    "元件": ["PCB", "连接器", "被动元件"],
    "半导体": ["光刻机", "存储芯片", "先进封装"],
    "电池": ["固态电池", "锂电池", "钠离子电池", "动力电池"],
    "光伏设备": ["TOPCON电池", "BC电池", "HJT电池", "钙钛矿电池"],
    "风电设备": ["海上风电", "风机"],
    "电网设备": ["特高压", "智能电网"],
    "工业金属": ["黄金", "白银", "铜", "小金属"],
    "贵金属": ["黄金", "白银", "黄金ETF"],
    "小金属": ["稀土", "钨", "锂"],
    "钢铁": ["钢铁ETF", "特钢"],
    "煤炭": ["焦煤", "动力煤"],
    "石油": ["油气开采", "炼化"],
    "化学制药": ["创新药", "CRO", "仿制药"],
    "生物制品": ["疫苗", "抗体"],
    "中药": ["中药配方颗粒", "中药ETF"],
    "医疗服务": ["CRO", "医美", "辅助生殖"],
    "医疗器械": ["医疗器械ETF", "IVD", "骨科"],
    "汽车整车": ["新能源车", "无人驾驶", "华为汽车"],
    "汽车零部件": ["一体化压铸", "汽车热管理", "无人驾驶"],
    "工程机械": ["挖掘机", "装载机"],
    "家用电器": ["白电", "扫地机", "智能家居"],
    "纺织服饰": ["户外服装", "运动鞋"],
    "食品饮料": ["白酒", "乳业", "预制菜"],
    "白酒": ["高端白酒", "酱酒"],
    "传媒": ["短剧", "游戏", "AIGC"],
    "互联网电商": ["跨境电商", "直播电商"],
    "软件开发": ["AI智能体", "鸿蒙", "信创"],
    "互联网服务": ["AIGC", "多模态AI", "数据要素"],
    "教育": ["在线教育", "职业教育"],
    "旅游及酒店": ["旅游概念", "酒店"],
    "零售": ["新零售", "免税店"],
    "房地产": ["物业管理", "房地产ETF"],
    "银行": ["国有大行", "股份制银行"],
    "证券": ["券商ETF", "互联网券商"],
    "保险": ["保险ETF"],
    "物流": ["快递", "航运"],
    "农林牧渔": ["转基因", "生猪", "宠物经济"],
    "种植业与林业": ["转基因", "粮食概念"],
}

class SpeedTracker:
    """记录每个板块的 (时间戳, 涨跌幅) 序列，计算滚动窗口涨速"""

    def __init__(self, window_seconds, keep_seconds):
        self.window = window_seconds
        self.keep = keep_seconds
        self.hist = {}          # name -> deque[(ts, pct)]
        self.lock = threading.Lock()

    def update(self, ts, name_pct_map):
        with self.lock:
            for name, pct in name_pct_map.items():
                q = self.hist.setdefault(name, deque())
                q.append((ts, pct))
                # 只保留足够计算最长窗口的历史
                while q and ts - q[0][0] > self.keep:
                    q.popleft()
            # 清理已消失的板块
            dead = [n for n, q in self.hist.items()
                    if not q or ts - q[-1][0] > self.keep * 2]
            for n in dead:
                del self.hist[n]

    def speed(self, name_pct_map, ts=None):
        """返回 {name: {window: speed}}，speed = 当前涨跌幅 - N秒前涨跌幅"""
        ts = ts or time.time()
        out = {}
        with self.lock:
            for name, pct in name_pct_map.items():
                q = self.hist.get(name)
                entry = {w: None for w in self.window}
                if q:
                    for w in self.window:
                        target = ts - w
                        # 找到时间上最接近 target 的历史点
                        best = None
                        for pt, ppct in q:
                            if pt <= target + 5:
                                best = (pt, ppct)
                            else:
                                break
                        if best and ts - best[0] <= w * 1.5:
                            entry[w] = round(pct - best[1], 2)
                out[name] = entry
        return out


# ---------------------------------------------------------------------------
# 行情查询工具（独立模块函数，被 MarketData.dragon_radar 等调用）
# ---------------------------------------------------------------------------
def _num(parts, idx):
    """安全取腾讯 qt 字段为 float（字段缺失/为空/'-' 返回 None）。"""
    try:
        v = parts[idx]
    except IndexError:
        return None
    if v in ("", "-", None):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _qt_symbol(code: str) -> str:
    """2026-09-29：剥离 .SZ/.SH/sz 等前后缀，兼容 000066 / 000066.SZ / sz000066。纯数字行为不变。"""
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if not code:
        return ""
    if code.startswith(("6", "9", "5")):
        return "sh" + code
    if code.startswith(("0", "3", "2")):
        return "sz" + code
    return ""


def quote_stocks(codes):
    """腾讯批量个股行情，返回 {code: {name, price, prev_close, open, pct, high, low, turnover, float_mv, zt_price, vol_ratio}}"""
    syms = [_qt_symbol(c) for c in codes]
    syms = [s for s in syms if s]
    if not syms:
        return {}
    try:
        r = requests.get("http://qt.gtimg.cn/q=" + ",".join(syms),
                         timeout=4, headers={"User-Agent": "Mozilla/5.0"})
        r.encoding = "gbk"
    except Exception:
        return {}
    out = {}
    for line in r.text.split(";"):
        if "~" not in line:
            continue
        p = line.split("~")
        if len(p) < 50 or not p[3]:
            continue
        try:
            out[p[2]] = {
                "name": p[1],
                "price": float(p[3]),
                "prev_close": float(p[4]),
                "open": float(p[5]),
                "pct": float(p[32]),
                "high": float(p[33]),
                "low": float(p[34]),
                "amount_yi": round(float(p[37]) / 1e4, 2) if p[37] else 0.0,
                "turnover": float(p[38]) if p[38] else 0.0,
                "float_mv": float(p[44]) if p[44] else 0.0,
                "zt_price": float(p[47]) if p[47] else 0.0,
                "vol_ratio": float(p[49]) if p[49] else 0.0,
            }
        except (ValueError, IndexError):
            continue
    return out


# ---------------------------------------------------------------------------
# 主数据抓取器
# ---------------------------------------------------------------------------
class MarketData:
    def __init__(self):
        self.lock = threading.RLock()
        self.last_update = {
            "indices": None, "sectors": None,
            "pools": None, "full": None,
        }
        self.errors = []

        self.indices = []            # 指数列表
        self.sectors = []            # 板块列表（含涨速字段）
        self.concepts = []           # 概念板块（题材粒度，光模块/CPO 等）
        self.zt_pool = []            # 涨停池
        self.dt_pool = []            # 跌停池
        self.zb_pool = []            # 炸板池
        self.breadth = {}            # 市场宽度
        self.screen = {}             # 异动榜单
        self.stock_speed = {}        # code -> (ts, pct) 上一帧全A涨跌幅
        self._index_ma = {}          # code -> {ma20, ma30}
        self._index_ma_at = 0.0      # 指数均线拉取时间戳（1h 缓存）
        self._price_hist = deque(maxlen=20)   # [(ts, {code: pct})] 全A快照历史（5分钟涨幅计算）
        self.five_min_top = []       # 5分钟涨幅榜 top15
        self.five_min_at = None      # 榜单时间
        self._daily_amount = {}      # {date: 全A总成交额(亿)} 用于均量
        self._amount_file = Path(__file__).parent / "data" / "daily_amount.json"
        try:
            self._daily_amount = json.loads(self._amount_file.read_text(encoding="utf-8"))
        except Exception:
            self._daily_amount = {}

        self._tracker = SpeedTracker(
            config.SPEED_WINDOWS, config.SPEED_HISTORY_SECONDS)
        self._stop = threading.Event()
        self._threads = []
        self._closed_done = {}   # 收盘定版标记：工作日收盘后每个线程只拉一次

    # ---------------- 工具 ----------------
    def _err(self, tag, exc):
        msg = f"[{datetime.now():%H:%M:%S}] {tag}: {type(exc).__name__} {exc}"
        print(msg)
        with self.lock:
            self.errors.append(msg)
            if len(self.errors) > 20:
                self.errors.pop(0)

    @staticmethod
    def _today():
        return datetime.now().strftime("%Y%m%d")

    # ---------------- 指数（腾讯，快） ----------------
    def fetch_indices(self):
        codes = ",".join(c for c, _ in config.WATCH_INDICES)
        r = requests.get(f"http://qt.gtimg.cn/q={codes}", timeout=5)
        r.encoding = "gbk"
        out = []
        for line in r.text.split(";"):
            if "~" not in line:
                continue
            p = line.split("~")
            if len(p) < 38:
                continue
            try:
                amount_wan = float(p[37])  # 成交额(万)
            except (ValueError, IndexError):
                amount_wan = 0.0
            out.append({
                "code": p[2], "name": p[1],
                "price": float(p[3]),
                "change": float(p[31]),
                "pct": float(p[32]),
                "open": float(p[5]), "prev_close": float(p[4]),
                "high": float(p[33]), "low": float(p[34]),
                "amount_yi": round(amount_wan / 1e4, 1),  # 亿
            })
        # 指数均线 MA20/MA30（腾讯日K，1 小时缓存）
        try:
            if time.time() - self._index_ma_at > 3600:
                self._refresh_index_ma()
        except Exception:
            pass
        ma_map = self._index_ma
        for it in out:
            it["ma20"] = ma_map.get("sh" + it["code"], {}).get("ma20")
            it["ma30"] = ma_map.get("sh" + it["code"], {}).get("ma30")
        if out:
            with self.lock:
                self.indices = out

    def _refresh_index_ma(self):
        """拉上证指数日K算 MA20/MA30（防守维度1：破30日线进防守）。"""
        try:
            url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                   "?param=sh000001,day,,,40,qfq")
            d = requests.get(url, timeout=8).json()
            days = (d.get("data", {}).get("sh000001", {}).get("day")
                    or d.get("data", {}).get("sh000001", {}).get("qfqday") or [])
            closes = [float(x[2]) for x in days]
            if len(closes) >= 30:
                self._index_ma["sh000001"] = {
                    "ma20": round(sum(closes[-20:]) / 20, 2),
                    "ma30": round(sum(closes[-30:]) / 30, 2),
                }
                self._index_ma_at = time.time()
                print(f"[MA] 指数均线 OK: "
                      f"MA20={self._index_ma['sh000001']['ma20']} "
                      f"MA30={self._index_ma['sh000001']['ma30']}")
        except Exception as e:
            print(f"[MA] 均线拉取失败: {type(e).__name__}: {str(e)[:60]}")

    # ---------------- 板块（同花顺） ----------------
    def fetch_concepts(self):
        """概念板块（题材粒度）。
        akshare stock_fund_flow_concept 已挂（页面结构失效），降级方案：
        1. 同花顺概念名清单（stock_board_concept_name_ths）= 全量题材名
        2. 涨停池 industry 统计（self.zt_pool）= 今日该概念涨停家数
        3. 排序：先按"今日涨停数 desc"，再按"名称"
        盘中变化：涨停池每 12s 刷新，概念板块热度随之变化。
        """
        try:
            df = ak.stock_board_concept_name_ths()
            names = [str(n).strip() for n in df["name"].tolist() if str(n).strip()]
        except Exception as e:
            self._err("概念名", e)
            return self.concepts or []
        # 涨停池按 industry 字段统计（workbench  涨停池 stock_zt_pool_em 包含 industry 列）
        zt_by_industry = {}
        for s in self.zt_pool:
            ind = s.get("industry") or s.get("板块") or ""
            if ind:
                zt_by_industry[ind] = zt_by_industry.get(ind, 0) + 1
        # 涨停池按 industry + 股票名做关键词命中（ths 概念名 vs 涨停股名/行业）
        zt_names = [str(s.get("name", "")) for s in self.zt_pool]
        zt_industries = list({s.get("industry") or "" for s in self.zt_pool if s.get("industry")})
        rows = []
        for name in names:
            # 概念名关键 token（如"光纤""固态电池""华为"等），去掉"概念""板块"等后缀
            key = name.replace("概念", "").replace("板块", "").strip()
            # 命中规则：涨停股名称含 key，或涨停股 industry 含 key
            hit = 0
            if key and len(key) >= 2:
                hit = sum(1 for n in zt_names if key in n)
                hit += sum(1 for i in zt_industries if key in i)
            rows.append({
                "name": name,
                "pct": 0.0,              # ths 盘中冻结，此处不显示涨速避免误导
                "leader": "--",
                "leader_pct": 0.0,
                "net_inflow": 0.0,
                "zt_count": hit,
                "company_count": 0,
            })
        rows.sort(key=lambda x: (-x["zt_count"], x["name"]))
        with self.lock:
            self.concepts = rows
            self.last_update["concepts"] = datetime.now()
        return rows

    # ---------------- 板块（同花顺） ----------------
    def fetch_sectors(self):
        df = ak.stock_board_industry_summary_ths()
        now = time.time()
        # 非交易时段（午休/收盘/盘前）：同花顺返回的涨跌幅是上次收盘的冻结快照，
        # 相邻两帧差分恒为 0，毫无参考价值，直接把涨速置空让前端显示「--」
        mkt_status, _ = market_status()
        in_trading = (mkt_status == "trading")
        rows = []
        for _, r in df.iterrows():
            rows.append({
                "name": str(r["板块"]),
                "pct": float(r["涨跌幅"]),
                "amount": float(r["总成交额"]),      # 亿
                "net_inflow": float(r["净流入"]),     # 亿
                "up_count": int(r["上涨家数"]),
                "down_count": int(r["下跌家数"]),
                "leader": str(r["领涨股"]),
                "leader_pct": float(r["领涨股-涨跌幅"]) if r["领涨股-涨跌幅"] != "-" else 0.0,
            })
        pct_map = {r["name"]: r["pct"] for r in rows}
        # 先取上一帧涨速（用更新前的历史），再写入新快照
        speeds = self._tracker.speed(pct_map, ts=now)
        self._tracker.update(now, pct_map)
        for r in rows:
            sp = speeds.get(r["name"], {})
            sp1 = sp.get(60)
            sp5 = sp.get(300)
            # 同花顺 stock_board_industry_summary_ths() 在盘中 pct 不更新（数据源限制），
            # 计算出来的涨速恒为 0 无参考价值，统一置空让前端显示「--」并提示
            if not in_trading or sp1 == 0:
                sp1 = None
            if not in_trading or sp5 == 0:
                sp5 = None
            r["speed_1m"] = sp1
            r["speed_5m"] = sp5
        rows.sort(key=lambda x: -x["pct"])
        with self.lock:
            self.sectors = rows
            self.last_update["sectors"] = datetime.now()
            # 给每个一级行业加"代表题材"列（精选硬编码：ths 概念没盘中数据，关键词匹配不准）
            for s in rows:
                s["representative_concepts"] = INDUSTRY_CONCEPT_MAP.get(s["name"], [])




    # ---------------- 涨停/跌停/炸板池 ----------------
    def fetch_pools(self):
        d = self._today()
        zt, dt, zb = [], [], []
        try:
            df = ak.stock_zt_pool_em(date=d)
            for _, r in df.iterrows():
                zt.append({
                    "code": str(r["代码"]), "name": str(r["名称"]),
                    "pct": round(float(r["涨跌幅"]), 2),
                    "lianban": int(r["连板数"]),
                    "stat": str(r["涨停统计"]),
                    "first_time": str(r["首次封板时间"]),
                    "break_count": int(r["炸板次数"]),
                    "amount_yi": round(float(r["成交额"]) / 1e8, 2),
                    "industry": str(r["所属行业"]),
                    "fund_yi": round(float(r["封板资金"]) / 1e8, 2),
                    "mv_yi": round(float(r["流通市值"]) / 1e8, 2),
                    "turnover": round(float(r["换手率"]), 2),
                })
        except Exception as e:
            self._err("涨停池", e)
        try:
            df = ak.stock_zt_pool_dtgc_em(date=d)
            for _, r in df.iterrows():
                dt.append({
                    "code": str(r["代码"]), "name": str(r["名称"]),
                    "pct": round(float(r["涨跌幅"]), 2),
                    "amount_yi": round(float(r["成交额"]) / 1e8, 2),
                    "industry": str(r["所属行业"]),
                })
        except Exception as e:
            self._err("跌停池", e)
        try:
            df = ak.stock_zt_pool_zbgc_em(date=d)
            for _, r in df.iterrows():
                zb.append({
                    "code": str(r["代码"]), "name": str(r["名称"]),
                    "pct": round(float(r["涨跌幅"]), 2),
                    "speed": r.get("涨速", None),
                    "break_count": int(r["炸板次数"]),
                    "amount_yi": round(float(r["成交额"]) / 1e8, 2),
                    "industry": str(r["所属行业"]),
                })
        except Exception as e:
            self._err("炸板池", e)
        with self.lock:
            self.zt_pool, self.dt_pool, self.zb_pool = zt, dt, zb
            self.last_update["pools"] = datetime.now()
        # M7 短线精灵：封涨停板 / 打开涨停事件（涨停池 diff）
        try:
            import sprite
            st, _ = market_status()
            sprite.instance().feed_pools(zt, zb, ts=time.time(), in_trading=(st == "trading"))
        except Exception as e:
            self._err("sprite-pools", e)

    # ---------------- 全A：宽度 + 异动筛选 ----------------
    def fetch_full_market(self):
        """全A行情（腾讯 qt.gtimg.cn 批量，比新浪稳定）+ 宽度 + 异动 + 涨速。
        列表源 ak.stock_info_a_code_name（东财列表接口可用，行情接口被WAF挡）。"""
        try:
            rows = self._fetch_tencent_full_market()
        except Exception as e:
            self._err("全A", e)
            return  # 失败保留 prev，下次成功帧仍能算涨速
        now = time.time()
        prev = self.stock_speed
        cur = {}
        mkt_status, _ = market_status()
        in_trading = (mkt_status == "trading")

        up = down = flat = l_up = l_up20 = l_down = 0
        movers = []
        for rec in rows:
            code, pct = rec["code"], rec["pct"]
            if pct > 0: up += 1
            elif pct < 0: down += 1
            else: flat += 1
            if pct >= 9.9: l_up += 1
            if pct >= 19.9: l_up20 += 1
            if pct <= -9.9: l_down += 1
            p = prev.get(code)
            if in_trading and p and now - p[0] < 180:
                rec["speed_1m"] = round(pct - p[1], 2)
            cur[code] = (now, pct)
            if rec["amount_yi"] >= config.SCREEN_MIN_AMOUNT / 1e8:
                movers.append(rec)
        breadth = {
            "total": len(rows),
            "up": up, "down": down, "flat": flat,
            "limit_up": l_up, "limit_up_20": l_up20, "limit_down": l_down,
        }
        # 涨速榜（异动核心）
        fast_up = [r for r in movers if r["speed_1m"] is not None]
        fast_up.sort(key=lambda x: -(x["speed_1m"] or 0))
        speed_up = [r for r in fast_up if (r["speed_1m"] or 0) > 0][:config.SCREEN_TOP_N]
        speed_down = [r for r in fast_up if (r["speed_1m"] or 0) < 0][:config.SCREEN_TOP_N]
        # 涨幅榜 / 成交额榜
        movers.sort(key=lambda x: -x["pct"])
        top_gainers = movers[:config.SCREEN_TOP_N]
        movers.sort(key=lambda x: -x["amount_yi"])
        top_amount = movers[:config.SCREEN_TOP_N]

        self.stock_speed = cur
        # 5 分钟涨幅榜：全A快照历史（deque 20 帧 ≈ 25 分钟），取 5 分钟前的帧对比
        try:
            # 存 {code: pct} 而不是 (ts, pct) 元组：后续 p5 = b_snap[code] 直接是 pct
            self._price_hist.append((now, {c: p for c, (t, p) in cur.items()}))
            base = None
            for ts, snap in self._price_hist:
                if now - ts >= 300:          # ≥5分钟的最旧帧
                    base = (ts, snap)
                    break
            five = []
            if base is not None and in_trading:
                b_ts, b_snap = base
                for code, (t, pct) in cur.items():
                    p5 = b_snap.get(code)
                    if p5 is not None and now - t < 10:
                        five.append((code, pct - p5))
                five.sort(key=lambda x: -x[1])
                top = five[:config.SCREEN_TOP_N]
                name_map = {r["code"]: (r["name"], r["pct"], r["price"], r["amount_yi"])
                            for r in rows}
                self.five_min_top = [{
                    "code": c, "chg5": round(d, 2),
                    "name": name_map.get(c, ("--", 0, 0, 0))[0],
                    "pct": name_map.get(c, ("--", 0, 0, 0))[1],
                    "price": name_map.get(c, ("--", 0, 0, 0))[2],
                    "amount_yi": name_map.get(c, ("--", 0, 0, 0))[3],
                } for c, d in top]
                self.five_min_at = datetime.now()
            else:
                # 非交易时段（午休/收盘）：快照冻结，5分钟涨幅无意义，清空
                self.five_min_top = []
                self.five_min_at = None
        except Exception:
            pass
        # 全A总成交额（亿）→ 落盘日度 → 5日均量（防守维度2）
        try:
            total_amount = sum(r["amount_yi"] for r in rows)
            today_s = datetime.now().strftime("%Y-%m-%d")
            self._daily_amount[today_s] = round(total_amount, 1)
            self._daily_amount = {k: v for k, v in
                                  list(self._daily_amount.items())[-10:]}
            self._amount_file.parent.mkdir(parents=True, exist_ok=True)
            self._amount_file.write_text(
                json.dumps(self._daily_amount, ensure_ascii=False), encoding="utf-8")
            amts = [v for k, v in sorted(self._daily_amount.items()) if v]
            avg5 = round(sum(amts[-5:]) / min(len(amts), 5), 1) if amts else 0.0
        except Exception:
            total_amount, avg5 = 0.0, 0.0
        with self.lock:
            self.breadth = breadth
            self.breadth["amount_yi"] = round(total_amount, 1)
            self.breadth["avg5_yi"] = avg5
            self.screen = {
                "speed_up": speed_up, "speed_down": speed_down,
                "top_gainers": top_gainers, "top_amount": top_amount,
            }
            self.last_update["full"] = datetime.now()
        # M7 短线精灵：全A帧 → 火箭发射 / 极速拉升 / 高台跳水 / 量比异动 / 涨速榜
        try:
            import sprite
            sprite.instance().feed_full(rows, ts=now, in_trading=in_trading)
        except Exception as e:
            self._err("sprite-full", e)

    # 腾讯批量行情：全A代码分批请求 qt.gtimg.cn
    def _fetch_tencent_full_market(self, batch=200):
        import requests as _req
        if not getattr(self, "_a_codes", None):
            # 代码列表落盘缓存 7 天（stock_info_a_code_name 走深交所接口，盘中易超时）
            cache_file = Path(__file__).parent / "data" / "a_codes.json"
            codes = None
            if cache_file.exists():
                try:
                    cached = json.loads(cache_file.read_text(encoding="utf-8"))
                    age = (time.time() - cached.get("ts", 0)) / 86400
                    if age < 7:
                        codes = cached["codes"]
                except Exception:
                    codes = None
            if not codes:
                df = ak.stock_info_a_code_name()
                codes = df["code"].astype(str).tolist()
                try:
                    cache_file.parent.mkdir(parents=True, exist_ok=True)
                    cache_file.write_text(json.dumps({"ts": time.time(), "codes": codes}),
                                          encoding="utf-8")
                except Exception:
                    pass
            self._a_codes = codes
        codes = self._a_codes
        rows = []
        for i in range(0, len(codes), batch):
            chunk = codes[i:i + batch]
            syms = []
            for c in chunk:
                if c.startswith(("6", "9")):
                    syms.append("sh" + c)
                elif c.startswith(("0", "3", "2")):
                    syms.append("sz" + c)
                else:
                    syms.append("bj" + c)
            r = _req.get("https://qt.gtimg.cn/q=" + ",".join(syms), timeout=8)
            r.encoding = "gbk"
            for line in r.text.split(";"):
                line = line.strip()
                if not line.startswith("v_"):
                    continue
                f = line.split('="', 1)
                if len(f) < 2:
                    continue
                parts = f[1].rstrip('"').split("~")
                if len(parts) < 40:
                    continue
                code = parts[2]
                try:
                    price = float(parts[3])
                    pct = float(parts[32])
                    prev_close = float(parts[4])
                    high = float(parts[33])
                    low = float(parts[34])
                    amount_yi = round(float(parts[37]) / 1e4, 2)  # 万元 → 亿
                except (ValueError, IndexError):
                    continue
                rows.append({
                    "code": code, "name": parts[1], "pct": pct,
                    "price": price, "amount_yi": amount_yi,
                    "amplitude": round((high - low) / prev_close * 100, 2) if prev_close else 0.0,
                    "speed_1m": None,
                    # ---- M7 短线精灵扩展字段（腾讯 qt 字段位实测 2026-09-14）----
                    "open": _num(parts, 5),
                    "prev_close": prev_close,
                    "high": high,
                    "low": low,
                    "vol_ratio": _num(parts, 49),      # 量比
                    "turnover": _num(parts, 38),       # 换手率 %
                    "zt_price": _num(parts, 47),       # 涨停价
                    "avg_price": _num(parts, 51),      # 均价
                    "outer": _num(parts, 7),           # 外盘(手)
                    "inner": _num(parts, 8),           # 内盘(手)
                    "mv_yi": _num(parts, 44),          # 流通市值(亿)
                })
        if len(rows) < 3000:
            raise RuntimeError(f"腾讯批量返回异常: {len(rows)} 行")
        return rows

    # ---------------- 后台线程 ----------------
    def _loop(self, name, fn, interval, need_trading=True):
        while not self._stop.is_set():
            status, _ = market_status()
            if need_trading:
                if status in ("closed", "closed_all_day"):
                    # 工作日收盘（closed）：首次进入拉一次「定版数据」（涨停池/全A收盘后是全天最终版），
                    # 之后长休，每 10 分钟检查一次是否开盘；周末（closed_all_day）纯休眠不拉
                    if status == "closed" and not self._closed_done.get(name):
                        try:
                            fn()
                            self._closed_done[name] = True
                            print(f"[{datetime.now():%H:%M:%S}] {name} 收盘定版完成")
                        except Exception as e:
                            self._err(name, e)
                            self._closed_done[name] = True  # 失败也标记，避免反复尝试
                    self._stop.wait(600)
                    continue
                if status == "pre" and name != "指数":
                    # 开盘前：指数可拉（有竞价参考），其余线程降频等待
                    self._stop.wait(120)
                    continue
            t0 = time.time()
            try:
                fn()
                print(f"[{datetime.now():%H:%M:%S}] {name} ok ({time.time() - t0:.1f}s)")
            except Exception as e:
                self._err(name, e)
                traceback.print_exc()
            # 按执行耗时自适应等待，保证节流
            wait = max(interval - (time.time() - t0), 2)
            self._stop.wait(wait)

    def start(self):
        jobs = [
            ("指数", self.fetch_indices, config.INDEX_INTERVAL),
            ("板块", self.fetch_sectors, config.SECTOR_INTERVAL),
            ("概念", self.fetch_concepts, config.SECTOR_INTERVAL),
            ("涨停池", self.fetch_pools, config.POOL_INTERVAL),
            ("全A", self.fetch_full_market, config.FULLMarket_INTERVAL),
        ]
        for name, fn, itv in jobs:
            t = threading.Thread(target=self._loop, args=(name, fn, itv),
                                 daemon=True, name=f"fetch-{name}")
            t.start()
            self._threads.append(t)
            time.sleep(0.3)  # 错开启动

    def stop(self):
        self._stop.set()

    # ---------------- 龙头候选雷达（Dragon Radar） ----------------
    # 思路：market-advisor board_score 的实时个股化版本。
    # 从当前涨停池里筛「最有龙头相」的票：连板高度 + 封板质量 + 板块共振
    # + 辨识度（市值/换手），再乘以全局情绪系数。
    def dragon_radar(self, top_n=10):
        with self.lock:
            zt = [dict(s) for s in self.zt_pool]
            zb, dt = list(self.zb_pool), list(self.dt_pool)
        if not zt:
            return {"rows": [], "mood": None, "note": "涨停池为空"}

        # ---- 全局情绪系数（0.75 ~ 1.12） ----
        zt_n, zb_n, dt_n = len(zt), len(zb), len(dt)
        zha_rate = zb_n / (zt_n + zb_n) if (zt_n + zb_n) else 0.0
        max_lb = max((s["lianban"] for s in zt), default=0)
        s1 = min(zt_n / 80.0, 1.0)          # 涨停家数（80家满）
        s2 = 1.0 - min(zha_rate / 0.40, 1.0)  # 炸板率（40%归零）
        s3 = min(max_lb / 4.0, 1.0)         # 空间高度（4板满）
        s4 = 1.0 - min(dt_n / 12.0, 1.0)    # 跌停压力
        mood_raw = (s1 * 0.35 + s2 * 0.30 + s3 * 0.20 + s4 * 0.15)
        mood = round(0.75 + mood_raw * 0.37, 3)

        # ---- 板块共振：行业涨停家数 + 行业内最高板 ----
        ind_cnt, ind_max = {}, {}
        for s in zt:
            ind = s["industry"]
            ind_cnt[ind] = ind_cnt.get(ind, 0) + 1
            ind_max[ind] = max(ind_max.get(ind, 0), s["lianban"])

        rows = []
        for s in zt:
            lb = s["lianban"]
            # 1) 连板高度 (0-26)
            h = {1: 10, 2: 18, 3: 24}.get(lb, 26 if lb == 4 else 24)
            # 2) 封板质量 (0-24)
            mv = s.get("mv_yi") or 0
            seal_ratio = (s["fund_yi"] / mv * 100) if mv else 0.0  # 封成比%
            seal = min(seal_ratio / 2.5, 1.0) * 12
            seal += max(0, 10 - s["break_count"] * 5)               # 炸板扣分
            ft = s["first_time"]
            try:
                ftv = int(ft)
            except (TypeError, ValueError):
                ftv = 150000
            seal += 10 if ftv <= 93000 else (7 if ftv <= 100000 else
                     (4 if ftv <= 110000 else (2 if ftv <= 140000 else 0)))
            # 3) 板块共振 (0-30)
            ind = s["industry"]
            n = ind_cnt.get(ind, 1)
            sec = min(n / 8.0, 1.0) * 18
            is_ldr = ind_max.get(ind, 0) == lb
            if lb >= 2 and is_ldr:
                sec += 8            # 行业最高板（≥2板）= 板块龙头地位
            elif lb == 1 and n >= 3:
                sec += 3            # 首板题材卡位（不给龙头地位）
            if lb >= 3 and lb == max_lb:
                sec += 4            # 全场空间板
            # 4) 辨识度 (0-14)：流通市值 20-150亿 满分，换手 3-15% 满分
            ident = 8 if 20 <= mv <= 150 else (6 if 150 < mv <= 300 else
                    (5 if 10 <= mv < 20 else 3))
            to = s.get("turnover") or 0
            ident += 6 if 3 <= to <= 15 else (4 if 1 <= to < 3 or 15 < to <= 25 else 2)

            base = h + seal + sec + ident
            score = round(min(base * mood, 100), 1)

            # 一字板判定：开盘即封 + 极低换手
            yizi = ftv <= 92501 and to < 1.0
            tags = []
            if lb >= 2 and ind_max.get(ind, 0) == lb:
                tags.append("板块龙头")
            if lb >= 3 and lb == max_lb:
                tags.append(f"空间板{lb}")
            if lb >= 3:
                tags.append(f"{lb}板高度")
            if seal_ratio >= 2.5:
                tags.append("封单硬")
            if s["break_count"] == 0:
                tags.append("零炸板")
            if n >= 8:
                tags.append(f"{ind}共振{n}家")
            if yizi:
                tags.append("一字(难买)")
            if s["break_count"] >= 2:
                tags.append("反复开板")

            if lb >= 3 and lb == max_lb and score >= 70 and not yizi:
                tip = "👑 空间板龙头，梯队辨识度最高"
            elif score >= 78 and lb >= 2 and not yizi:
                tip = "🔥 主升龙头候选，重点跟踪"
            elif score >= 72 and lb == 1 and n >= 5 and not yizi:
                tip = "⚡ 首板龙头胚子，观察能否带队晋级"
            elif yizi:
                tip = "一字封死，排队博弈，注意流动性"
            elif s["break_count"] >= 2:
                tip = "分歧大，回封确认再看"
            else:
                tip = "储备观察"

            rows.append({
                "code": s["code"], "name": s["name"], "lianban": lb,
                "industry": ind, "score": score,
                "seal_ratio": round(seal_ratio, 2),
                "fund_yi": s["fund_yi"], "mv_yi": mv,
                "turnover": to, "break_count": s["break_count"],
                "first_time": ft, "sector_zt": n,
                "is_sector_leader": ind_max.get(ind, 0) == lb,
                "yizi": yizi, "tags": tags, "tip": tip,
            })

        rows.sort(key=lambda x: -x["score"])
        # 叠加实时行情：让"盘中% / 封板状态"有真实变化（之前只有快照属性，分数全天不变）
        from_advisor = quote_stocks([s["code"] for s in rows[:top_n]])
        for s in rows[:top_n]:
            q = from_advisor.get(s["code"])
            if q:
                s["pct"] = q.get("pct")
                s["price"] = q.get("price")
                s["zt_price"] = q.get("zt_price")
                # 封板状态：现价 >= 涨停价 99.8% 视为仍在封板
                sealed_now = q.get("zt_price", 0) > 0 and q.get("price", 0) >= q.get("zt_price", 0) * 0.998
                s["sealed_now"] = bool(sealed_now)
            else:
                s["pct"] = None
                s["price"] = None
                s["zt_price"] = None
                s["sealed_now"] = None
        return {
            "rows": rows[:top_n],
            "mood": mood,
            "mood_detail": {"zt": zt_n, "zb": zb_n, "dt": dt_n,
                            "zha_rate": round(zha_rate * 100, 1),
                            "max_lb": max_lb},
            "note": "情绪系数×个股四因子(高度/封板/板块/辨识度) · 盘中实时价叠加",
        }

    # ---------------- 快照（供 API 返回） ----------------
    def snapshot(self):
        with self.lock:
            status, status_text = market_status()
            zt = sorted(self.zt_pool, key=lambda x: (-x["lianban"], x["first_time"]))
            # 连板梯队: {连板数: [股票]}
            ladder = {}
            for s in zt:
                if s["lianban"] >= 2:
                    ladder.setdefault(s["lianban"], []).append(s)
            ladder = {k: v for k, v in sorted(ladder.items(), reverse=True)}
            return {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "market_status": status,
                "status_text": status_text,
                "indices": self.indices,
                "sectors": self.sectors,
                "concepts": self.concepts,
                "zt_pool": zt[:60],
                "zt_pool_all": zt,   # 全量（涨停池板块分组聚合用）
                "zt_count": len(self.zt_pool),
                "dt_pool": self.dt_pool,
                "dt_count": len(self.dt_pool),
                "zb_pool": self.zb_pool[:40],
                "zb_count": len(self.zb_pool),
                "ladder": {str(k): [s["name"] for s in v][:8]
                           for k, v in ladder.items()},
                "breadth": self.breadth,
                "screen": self.screen,
                "five_min": self.five_min_top,
                "five_min_at": (self.five_min_at.strftime("%H:%M:%S")
                                if self.five_min_at else None),
                "last_update": {k: (v.strftime("%H:%M:%S") if v else None)
                                for k, v in self.last_update.items()},
                "errors": self.errors[-5:],
            }

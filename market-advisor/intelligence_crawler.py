import os
import sys
import json
import re
import socket
import time
from collections import defaultdict
from datetime import datetime
import akshare as ak
from google import genai
from google.genai import types
from dotenv import load_dotenv

# Load dotenv from my-chatbot/.env where GEMINI_API_KEY resides
load_dotenv("/Users/williamli/Work/Python_Project/my-chatbot/.env")

REPORTS_DIR = os.path.expanduser('/Users/williamli/Work/reports')
INTEL_FILE = os.path.join(REPORTS_DIR, 'market_intel_briefs.json')

# Gemini 候选模型：增强模式可用时自动回退
GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash", "gemini-flash-latest"]

api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    print("[Intel Crawler] Error: GEMINI_API_KEY not found in .env")
    sys.exit(0)


def _detect_proxy():
    """自动探测可用本地代理，优先使用环境变量中已配置的代理，否则扫描常见代理端口。"""
    env_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    if env_proxy:
        return env_proxy
    common_ports = [52633, 7890, 7891, 7892, 1087, 10808, 8118, 8888, 1080]
    for port in common_ports:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.4)
        try:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return f"http://127.0.0.1:{port}"
        except Exception:
            pass
        finally:
            s.close()
    return None


_proxy = _detect_proxy()
if _proxy:
    os.environ["HTTP_PROXY"] = _proxy
    os.environ["HTTPS_PROXY"] = _proxy
    print(f"[Intel Crawler] 使用代理: {_proxy}")
else:
    print("[Intel Crawler] 未检测到本地代理，尝试直连（Gemini 在境内可能失败）")

# 监测龙头股篮子：用于拉取机构最新研报，聚合出机构最看好的行业/题材
REPRESENTATIVE_BASKET = {
    "sh600519": "贵州茅台", "sz300750": "宁德时代", "sh601127": "赛力斯",
    "sz002594": "比亚迪", "sh601138": "工业富联", "sz002230": "科大讯飞",
    "sz000063": "中兴通讯", "sz002156": "通富微电", "sh600900": "长江电力",
    "sh601012": "隆基绿能", "sz000001": "平安银行", "sz300122": "智飞生物",
    "sh600031": "三一重工",
}


def _fetch_research_reports():
    """拉取核心龙头股的最新机构研报列表（该 eastmoney 接口在本网络可走代理成功）。"""
    report_list = []
    for symbol, name in REPRESENTATIVE_BASKET.items():
        code = "".join(filter(str.isdigit, symbol))
        try:
            df = ak.stock_research_report_em(symbol=code)
            if not df.empty:
                for _, row in df.head(2).iterrows():
                    report_list.append({
                        "symbol": code,
                        "name": name,
                        "title": str(row.get("报告名称", "")),
                        "rating": str(row.get("东财评级", "")),
                        "institution": str(row.get("机构", "")),
                        "sector": str(row.get("行业", "")),
                        "date": str(row.get("日期", "")),
                    })
        except Exception as e:
            print(f"[Intel Crawler] 研报拉取失败 {name}({code}): {e}")
    return report_list


def _build_from_research(report_list):
    """从研报列表聚合出机构近24h最覆盖的行业/题材（无需 Gemini，可靠）。"""
    sec_map = defaultdict(lambda: {"count": 0, "stocks": {}, "titles": [], "ratings": set()})
    for r in report_list:
        sec = (r.get("sector") or "").strip() or "其他"
        d = sec_map[sec]
        d["count"] += 1
        d["stocks"][r["symbol"]] = r["name"]
        if r.get("title"):
            d["titles"].append(r["title"])
        if r.get("rating"):
            d["ratings"].add(r["rating"])

    ranked = sorted(sec_map.items(), key=lambda kv: kv[1]["count"], reverse=True)
    sectors = []
    for sec, d in ranked[:3]:
        stocks = [f'{code} {name}' for code, name in d["stocks"].items()]
        rating = "/".join(list(d["ratings"])[:3]) or "暂无"
        top_title = d["titles"][0] if d["titles"] else ""
        logic = f"机构近24h覆盖{d['count']}篇研报（评级：{rating}）。核心线索：{top_title[:50]}"
        sectors.append({"name": sec, "logic": logic, "stocks": stocks})
    return {"sectors": sectors, "source": "research_reports"}


def _try_gemini_once(prompt, timeout_ms=15000):
    """单次 Gemini 增强尝试（短超时），成功返回 (text, model)，失败返回 (None, err)。锦上添花，不阻塞主路径。"""
    last_err = None
    for model in GEMINI_MODELS[:2]:
        try:
            client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=timeout_ms))
            resp = client.models.generate_content(model=model, contents=prompt)
            text = (resp.text or "").strip()
            if text:
                return text, model
        except Exception as e:
            last_err = e
            print(f"[Intel Crawler] Gemini[{model}] 增强失败: {e}")
    return None, last_err


def fetch_and_analyze_intel():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting 24h AI Intelligence Catalyst Scan...")

    # 主路径：机构研报聚合（可靠，给出“机构最新最覆盖的行业/题材”）
    report_list = _fetch_research_reports()
    print(f"[Intel Crawler] 拉取研报 {len(report_list)} 篇。")
    data = None
    if report_list:
        data = _build_from_research(report_list)
        print(f"[Intel Crawler] 研报聚合完成，行业数: {len(data['sectors'])}")

    # 增强：尝试用 Gemini 基于研报做更深度的 AI 前瞻叙事（单次短超时，失败则沿用研报聚合）
    if data is not None and report_list:
        try:
            prompt = f"""你是一个顶级的A股短线量化策略和题材研究专家。
以下是我们今天监测的A股各核心行业领头羊最新的机构研究报告列表：
{json.dumps(report_list, ensure_ascii=False, indent=2)}
请识别并归纳出当前在机构研报中最被看好、最适合“龙头/强者恒强”接力风格的前3个【核心催化题材板块】，
精炼其炒作动因（100字内），并列出每板块1-2只核心观察个股（代码+名字）。
请必须用纯JSON格式回复（不要Markdown），结构：
{{"sectors":[{{"name":"题材名","logic":"催化逻辑(100字内)","stocks":["代码 简称"]}}]}}
"""
            text, model = _try_gemini_once(prompt)
            if text:
                if text.startswith("```"):
                    text = re.sub(r"^```[a-zA-Z]*\n", "", text)
                    text = re.sub(r"\n```$", "", text)
                gem = json.loads(text)
                if gem.get("sectors"):
                    gem["source"] = "gemini"
                    data = gem
                    print(f"[Intel Crawler] Gemini 增强成功 (model={model})，采用 AI 前瞻叙事。")
        except Exception as e:
            print(f"[Intel Crawler] Gemini 增强失败，沿用研报聚合: {e}")

    if data is None:
        print("[Intel Crawler] 情报生成最终失败，保留原缓存。")
        return

    data["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(INTEL_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Intelligence brief updated (source={data.get('source')}).")
    except Exception as e:
        print(f"[Intel Crawler] Failed to save intelligence: {e}")


if __name__ == "__main__":
    fetch_and_analyze_intel()

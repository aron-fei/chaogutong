#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pro_report_*.txt → HTML 可视化卡片转换器（供 automation 调用）。

用法: /opt/homebrew/anaconda3/bin/python3 txt_to_html.py <input.txt> [output.html]
默认输出到输入同目录下的 <同名>.html
纯标准库实现，无外部依赖。
"""
import html as _html
import re
import sys
from pathlib import Path


def _esc(s):
    return _html.escape(s, quote=False)


def _colorize(line):
    """对文本行做语义着色（A股习惯：红涨绿跌）。返回 HTML。"""
    t = _esc(line)
    # 红色系（涨停/晋级/买入/🔥/👑）
    for kw in ("涨停", "晋级", "一字涨停", "买入", "打板", "主升", "封板", "抢筹",
               "加仓", "锁仓", "冲高", "龙头", "排单", "🔥", "👑", "🟢", "✅", "强"):
        t = re.sub(rf"({kw})", r'<span class="r">\1</span>', t)
    # 绿色系（跌停/退潮/止损/清仓/🔴/📉）
    for kw in ("跌停", "退潮", "止损", "清仓", "卖出", "离场", "减仓", "杀跌",
               "核按钮", "亏", "弱", "🔴", "📉", "❌", "空仓"):
        t = re.sub(rf"({kw})", r'<span class="g">\1</span>', t)
    # 黄色系（提示/警示/分歧/观察）
    for kw in ("分歧", "观察", "排队", "博弈", "死锁", "轻仓", "备用", "🟡", "⚠️"):
        t = re.sub(rf"({kw})", r'<span class="y">\1</span>', t)
    # 数字百分比（涨跌幅）着色
    t = re.sub(r"([+-]?\d+\.?\d*%)", r'<span class="num">\1</span>', t)
    return t


def _block_to_html(block):
    """把一个 ==== 分隔的文本块转成 HTML 片段。"""
    lines = [l.rstrip() for l in block.splitlines()]
    lines = [l for l in lines if l.strip()]  # 去空行
    if not lines:
        return ""
    # 块标题（==== 之间第一行非空）
    out = []
    para = []

    def flush():
        if para:
            out.append('<p class="p">' + '<br>'.join(_colorize(p) for p in para) + "</p>")
            para.clear()

    for ln in lines:
        if ln.startswith("【"):
            flush()
            out.append(f'<h4 class="h">{_colorize(ln)}</h4>')
        elif ln.startswith("  •") or ln.startswith("•"):
            flush()
            out.append(f'<div class="li">{_colorize(ln.strip())}</div>')
        elif ln.startswith("  [") or re.match(r"^\s*\[?\d+\]", ln):
            flush()
            out.append(f'<div class="li">{_colorize(ln.strip())}</div>')
        elif ln.startswith("➡️") or ln.startswith("  ➡"):
            flush()
            out.append(f'<div class="act">{_colorize(ln.strip())}</div>')
        else:
            para.append(ln)
    flush()
    return "\n".join(out)


def convert(txt: str, title: str) -> str:
    # 分离头部标题（前几行 ==== ... ==== 内）
    blocks = re.split(r"={5,}", txt)
    header = ""
    body_blocks = []
    for b in blocks:
        b = b.strip("\n")
        if not b.strip():
            continue
        if not header and ("决策内参" in b or "策略报告" in b or "监控" in b):
            header = b
        else:
            body_blocks.append(b)
    html_parts = []
    for b in body_blocks:
        html_parts.append(_block_to_html(b))
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_esc(title)}</title>
<style>
  body {{ background:#eef1f5; margin:0; padding:20px 12px;
        font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif; }}
  .card {{ max-width:720px; margin:0 auto; background:#fff; border-radius:12px;
          box-shadow:0 2px 10px rgba(0,0,0,.10); overflow:hidden; }}
  .head {{ background:linear-gradient(135deg,#1a237e,#283593); color:#fff;
          padding:16px 20px; }}
  .head h1 {{ margin:0; font-size:17px; letter-spacing:.5px; }}
  .head .sub {{ margin:6px 0 0; font-size:12px; opacity:.8; }}
  .body {{ padding:16px 20px; }}
  .h {{ margin:14px 0 8px; font-size:14px; color:#1f2328;
        border-left:3px solid #1a237e; padding-left:8px; }}
  .p {{ margin:8px 0; font-size:12.5px; line-height:1.8; color:#3d4148; white-space:pre-wrap; }}
  .li {{ margin:5px 0; padding:6px 10px; background:#f6f8fa; border-radius:6px;
        font-size:12px; line-height:1.7; color:#3d4148; }}
  .act {{ margin:8px 0; padding:10px 12px; background:#fff3e0; border-left:4px solid #e65100;
         border-radius:6px; font-size:12.5px; line-height:1.7; color:#4e342e; }}
  .r {{ color:#cf222e; font-weight:600; }}
  .g {{ color:#1a7f37; font-weight:600; }}
  .y {{ color:#9a6700; font-weight:600; }}
  .num {{ font-family:"SF Mono",Menlo,monospace; color:#0550ae; font-weight:600; }}
  .foot {{ text-align:center; padding:12px; font-size:11px; color:#999;
           background:#fafbfc; }}
</style>
</head>
<body>
<div class="card">
  <div class="head"><h1>📝 {_esc(title)}</h1>
    <div class="sub">{_esc(header[:120] if header else "")}</div></div>
  <div class="body">{''.join(html_parts)}</div>
  <div class="foot">market-advisor 决策内参 · 仅供参考，不构成投资建议</div>
</div>
</body>
</html>"""


def main():
    if len(sys.argv) < 2:
        print("用法: txt_to_html.py <input.txt> [output.html]")
        sys.exit(1)
    src = Path(sys.argv[1])
    txt = src.read_text(encoding="utf-8", errors="replace")
    title = src.stem.replace("_", " ").replace("pro report", "决策内参")
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_suffix(".html")
    dst.write_text(convert(txt, title), encoding="utf-8")
    print(dst)


if __name__ == "__main__":
    main()

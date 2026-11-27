#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""极简报告/指令弹窗 (Tkinter, 零依赖)。

为什么不用 TextEdit / osascript：
  - LaunchAgent 后台进程调 `open -a TextEdit file` 只会拉起空 TextEdit，文档打不开；
  - osascript 需要「辅助访问 / 自动化」权限，权限被重置后直接报 -1728 或超时 10s。
  Tkinter 在同一用户会话内直接建窗口，不需要任何 macOS 隐私权限，最稳。

用法:
    python3 popup_gui.py <文件路径> [窗口标题]
"""
import os
import sys
import tkinter as tk
from tkinter import font as tkfont


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return "读取失败: %s\n%s" % (path, e)


def main():
    if len(sys.argv) < 2:
        print("usage: popup_gui.py <file> [title]")
        return 1
    path = sys.argv[1]
    title = sys.argv[2] if len(sys.argv) > 2 else os.path.basename(path)
    text = _read(path)

    # 短文本(操作闪卡)用小窗大字；长文本(完整报告)用大窗常规字
    is_card = len(text) < 800

    root = tk.Tk()
    root.title(title)
    root.configure(bg="#ffffff")

    mono = tkfont.Font(family="Menlo", size=16 if is_card else 12)

    wrap = tk.Frame(root, bg="#ffffff", padx=14, pady=10)
    wrap.pack(fill="both", expand=True)

    head = tk.Label(wrap, text=title, bg="#ffffff", fg="#111111",
                    font=tkfont.Font(family="PingFang SC", size=14, weight="bold"),
                    anchor="w")
    head.pack(fill="x", pady=(0, 8))

    body = tk.Frame(wrap, bg="#ffffff")
    body.pack(fill="both", expand=True)

    sb = tk.Scrollbar(body)
    sb.pack(side="right", fill="y")

    txt = tk.Text(body, wrap="word", font=mono, bg="#ffffff", fg="#111111",
                  relief="flat", highlightthickness=1,
                  highlightbackground="#d0d0d0", yscrollcommand=sb.set)
    txt.pack(side="left", fill="both", expand=True)
    sb.config(command=txt.yview)

    # 买红卖绿（A股习惯）：整行着色，其余黑白
    txt.tag_config("buy", foreground="#c62828")
    txt.tag_config("sell", foreground="#2e7d32")
    for idx, line in enumerate(text.split("\n"), start=1):
        txt.insert("end", line + "\n")
        s = line.strip()
        if s.startswith("买") or s.startswith("排单"):
            txt.tag_add("buy", "%d.0" % idx, "%d.end" % idx)
        elif s.startswith("卖"):
            txt.tag_add("sell", "%d.0" % idx, "%d.end" % idx)
    txt.config(state="disabled")

    btn = tk.Button(wrap, text="关闭 (Esc)", command=root.destroy,
                    font=tkfont.Font(family="PingFang SC", size=13),
                    highlightbackground="#ffffff")
    btn.pack(pady=(10, 0))

    w, h = (620, 320) if is_card else (900, 700)
    root.update_idletasks()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry("%dx%d+%d+%d" % (w, h, (sw - w) // 2, max(60, (sh - h) // 3)))

    root.bind("<Escape>", lambda e: root.destroy())
    # 强制抢到前台：LaunchAgent 拉起的窗口默认不抢焦点，会被飞书/微信盖住
    root.lift()
    root.attributes("-topmost", True)
    root.focus_force()
    root.after(1200, lambda: root.attributes("-topmost", False))
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

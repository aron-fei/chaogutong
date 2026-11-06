#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
尾盘操作确认台 (极简版)
=========================================
一屏一个清单：每笔操作一行，默认勾选=按系统建议执行。
  · 想改：直接改行里的手数/价格（1手=100股，全系统统一以手为单位）
  · 不执行：取消勾选
  · 涨停股：默认不勾选（买不进）
底部一个按钮【确认执行 · 写入账户】→ 写回 actual_* → 调主脚本 confirm 写库 → 回执。

用法:
  python3 confirm_gui.py                 # 自动加载当日待确认单
  python3 confirm_gui.py <买单> <卖单>    # 指定文件
"""
import os
import sys
import json
import glob
import subprocess
from datetime import datetime

import tkinter as tk
from tkinter import messagebox

# ---------- 路径 ----------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = os.path.expanduser("~/Work/reports")
DB_FILE = os.path.join(REPORTS_DIR, "backtest_database.json")
MAIN_SCRIPT = os.path.join(SCRIPT_DIR, "market_scheduler.py")
PYTHON_BIN = sys.executable or "/opt/homebrew/anaconda3/bin/python3"

# ---------- 配色 (极简 · 白底黑字 · 红买绿卖) ----------
C_BG   = "#ffffff"
C_LINE = "#ececec"   # 分隔线
C_TEXT = "#1a1a1a"
C_SUB  = "#999999"
C_BUY  = "#d92b2b"   # 买/涨=红
C_SELL = "#0f9d58"   # 卖/跌=绿
C_BOARD = "#e67e22"  # 09:25/13:40 涨停排队单：橙/警告，区别于实时买入
C_BTN  = "#1a1a1a"   # 主按钮=中性深色
C_BTN_DK = "#000000"

FONT = "PingFang SC"

# A股交易单位：1手 = 100股。界面与输入一律用【手】。
LOT_SIZE = 100


def _font(size, bold=False):
    return (FONT, size, "bold") if bold else (FONT, size)


def _to_lots(shares):
    try:
        return int(int(float(shares)) // LOT_SIZE)
    except Exception:
        return 0


def _to_shares(lots):
    try:
        return int(float(lots)) * LOT_SIZE
    except Exception:
        return 0


# ---------- 数据加载 ----------
def _find_pending():
    """只加载【当天】的待确认单。隔夜残留的 pending 文件一律忽略：
    主脚本 confirm 只处理当日文件，若把旧单显示出来会造成"确认了却写不进库"的假象。"""
    today = datetime.now().strftime("%Y-%m-%d")
    buys = sorted(
        p for p in glob.glob(os.path.join(REPORTS_DIR, "pending_buy_confirm_*.json"))
        if not p.endswith(".confirmed.json") and today in os.path.basename(p))
    sells = sorted(
        p for p in glob.glob(os.path.join(REPORTS_DIR, "pending_sell_confirm_*.json"))
        if not p.endswith(".confirmed.json") and today in os.path.basename(p))
    return buys, (sells[-1] if sells else None)


def _load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _load_account():
    d = _load_json(DB_FILE) or {}
    cash = float(d.get("available_cash", 0) or 0)
    equity = float(d.get("total_equity", 0) or 0)
    init = float(d.get("initial_capital", 500000) or 500000)
    pos = len(d.get("active_positions", []) or [])
    pnl = (equity - init) / init * 100 if init else 0
    return cash, equity, pos, pnl


# ---------- 主界面 ----------
class ConfirmApp:
    def __init__(self, root, buy_paths, sell_path):
        self.root = root
        self.buy_paths = buy_paths
        self.sell_path = sell_path
        self.rows = []

        root.title("尾盘操作确认台")
        root.configure(bg=C_BG)
        root.geometry("560x720")
        root.minsize(520, 480)

        self._submitting = False  # 防止快速双击导致重复提交

        self._build_header()
        self._build_list()
        self._build_footer()

    # ---- 顶部：标题 + 账户一行 ----
    def _build_header(self):
        top = tk.Frame(self.root, bg=C_BG)
        top.pack(fill="x", padx=26, pady=(22, 6))
        tk.Label(top, text="尾盘操作确认", bg=C_BG, fg=C_TEXT,
                 font=_font(20, True)).pack(side="left")
        tk.Label(top, text=datetime.now().strftime("%m-%d %H:%M"),
                 bg=C_BG, fg=C_SUB, font=_font(12)).pack(side="right", pady=(8, 0))

        cash, equity, pos, pnl = _load_account()
        tk.Label(self.root,
                 text=f"净值 ¥{equity:,.0f}  ·  可用 ¥{cash:,.0f}  ·  持仓 {pos} 笔  ·  {pnl:+.2f}%",
                 bg=C_BG, fg=C_SUB, font=_font(12)).pack(anchor="w", padx=26)
        tk.Frame(self.root, bg=C_LINE, height=1).pack(fill="x", padx=26, pady=(12, 0))

        tk.Label(self.root, text="默认已勾选 = 按建议执行；数量单位为【手】(1手=100股)；不做就取消勾选",
                 bg=C_BG, fg=C_SUB, font=_font(11)).pack(anchor="w", padx=26, pady=(10, 2))

    # ---- 中部：操作清单 ----
    def _build_list(self):
        self.list_frame = tk.Frame(self.root, bg=C_BG)
        self.list_frame.pack(fill="both", expand=True, padx=26, pady=(4, 0))

        # 卖出在前
        if self.sell_path:
            for it in (_load_json(self.sell_path) or {}).get("items", []):
                self._add_row("sell", it)
        # 买入（同一股票可能出现在 09:25 和 14:40 两张单里，只显示一次；
        # 写回时 _match 按 symbol 匹配同一行，两张单同步写同一确认值，
        # 写库侧 record_buys_to_db 有 active_symbols 去重兜底不会重复买入）
        seen_buy = set()
        for bp in self.buy_paths:
            data = _load_json(bp) or {}
            sess = data.get("session", "")
            for it in data.get("items", []):
                sym = it.get("symbol")
                if sym in seen_buy:
                    continue
                seen_buy.add(sym)
                # 兼容旧文件：优先用 item 自带的 session，否则用文件级 session
                it["_file_session"] = it.get("session") or sess
                self._add_row("buy", it)

        if not self.rows:
            tk.Label(self.list_frame, text="当前没有待确认的操作",
                     bg=C_BG, fg=C_SUB, font=_font(14)).pack(pady=60)

    def _add_row(self, kind, it):
        row = tk.Frame(self.list_frame, bg=C_BG)
        row.pack(fill="x", pady=6)

        if kind == "buy":
            is_limit = bool(it.get("is_limit_up"))
            px = float(it.get("suggest_price") or 0)
            lots = int(it.get("suggest_lots") or 0) or _to_lots(it.get("suggest_shares") or 0)
            file_session = it.get("_file_session", "")
            is_board_order = file_session in ("09:25", "13:40")
            if is_board_order:
                # 09:25/13:40 是涨停排队单，不是实时买入建议；默认不勾选，由用户显式确认实际成交
                color = C_BOARD
                verb = "排单"
                held_lots = 0
                default_on = False
                default_lots = 0
            else:
                color = C_BUY
                verb = "买入"
                held_lots = 0
                default_on = not is_limit
                default_lots = 0 if is_limit else lots
        else:
            is_limit = False
            px = float(it.get("price") or 0)
            held_lots = int(it.get("held_lots") or 0) or _to_lots(it.get("held_shares") or 0)
            rec = int(it.get("recommend_sell_lots") or 0) or _to_lots(it.get("recommend_sell_shares") or 0)
            lots = rec if rec > 0 else held_lots
            color = C_SELL
            verb = "卖出"
            default_on = True
            default_lots = lots

        chk = tk.BooleanVar(value=default_on)
        share_var = tk.StringVar(value=str(default_lots))
        price_var = tk.StringVar(value=f"{px:.2f}")

        # 排单/涨停(默认不勾选)联动：手数>0 自动勾选、归0 自动取消，
        # 避免"填了手数却忘勾选"导致确认台把它当未成交漏掉(2026-09-01 芒果超媒教训)
        if kind == "buy" and not default_on:
            def _sync_chk_from_lots(*_):
                try:
                    v = float(share_var.get() or 0)
                except ValueError:
                    v = 0
                chk.set(v > 0)
            share_var.trace_add("write", _sync_chk_from_lots)

        # 勾选框
        cb = tk.Checkbutton(row, variable=chk, bg=C_BG, activebackground=C_BG,
                            highlightthickness=0, bd=0)
        cb.pack(side="left")

        # 操作动词 (红/绿)
        tk.Label(row, text=verb, bg=C_BG, fg=color, font=_font(15, True),
                 width=4).pack(side="left", padx=(2, 6))
        # 名称
        tk.Label(row, text=it.get("name", "?"), bg=C_BG, fg=C_TEXT,
                 font=_font(15, True), width=7, anchor="w").pack(side="left")

        # 手数框
        e_share = tk.Entry(row, textvariable=share_var, width=7, font=_font(13),
                           justify="center", relief="solid", bd=1)
        e_share.pack(side="left", padx=(6, 2))
        tk.Label(row, text="手 @", bg=C_BG, fg=C_SUB, font=_font(12)).pack(side="left")
        # 价格框
        e_price = tk.Entry(row, textvariable=price_var, width=7, font=_font(13),
                           justify="center", relief="solid", bd=1)
        e_price.pack(side="left", padx=(2, 6))

        # 右侧注释
        if kind == "buy" and is_board_order:
            stale_tag = "【委托价已失效】" if it.get("is_stale") else ""
            sug = int(it.get("suggest_lots") or 0) or _to_lots(it.get("suggest_shares") or 0)
            sug_txt = f"建议 {sug}手≈¥{px * _to_shares(sug) / 10000:.1f}万 ｜ " if sug > 0 else ""
            hint = f"{stale_tag}{sug_txt}{file_session} 涨停排队单，委托价 ¥{px:.2f}；实际成交填左侧(0=未成交)。当前价可能已变，请勿按委托价新买入"
            hint_fg = C_BOARD
        elif kind == "buy" and is_limit:
            hint = "涨停·买不进"
            hint_fg = C_SUB
        elif kind == "sell":
            hint = f"持仓 {held_lots}手"
            hint_fg = C_SUB
        else:
            _amt = px * _to_shares(lots)
            hint = f"≈¥{_amt/10000:.1f}万" if _amt else ""
            hint_fg = C_SUB
        tk.Label(row, text=hint, bg=C_BG, fg=hint_fg, font=_font(11)).pack(side="left")

        self.rows.append({
            "kind": kind, "item": it, "chk": chk,
            "share_var": share_var, "price_var": price_var,
            "held_lots": held_lots, "held_shares": int(it.get("held_shares") or 0),
            "is_limit": is_limit,
        })

    # ---- 底部：一个按钮 ----
    def _build_footer(self):
        tk.Frame(self.root, bg=C_LINE, height=1).pack(fill="x", padx=26, pady=(8, 0))
        foot = tk.Frame(self.root, bg=C_BG)
        foot.pack(fill="x", padx=26, pady=16)

        if not self.rows:
            self._plain_button(foot, "关闭", self.root.destroy, font=_font(14))
            return

        self._plain_button(foot, "确认执行 · 写入账户", self.on_submit, font=_font(17, True))

    def _plain_button(self, parent, text, command, font=None):
        """最朴素按钮：白底黑字黑边框。macOS 下 tk.Button 的 bg/fg 会被系统主题覆盖，
        用 Frame+Label 组合由 Tkinter 自己绘制，保证任何系统主题下都看得见。"""
        if font is None:
            font = _font(14)

        border = tk.Frame(parent, bg="black", cursor="hand2")
        border.pack(fill="x")

        label = tk.Label(border, text=text, font=font,
                         bg="white", fg="black",
                         padx=20, pady=14)
        label.pack(fill="x", padx=2, pady=2)

        def on_click(event=None):
            command()

        # 简单的按下反馈（变浅灰）
        def on_press(event=None):
            label.config(bg="#eeeeee")

        def on_release(event=None):
            label.config(bg="white")
            on_click()

        def on_enter(event=None):
            label.config(bg="#f7f7f7")

        def on_leave(event=None):
            label.config(bg="white")

        border.bind("<Button-1>", on_press)
        label.bind("<Button-1>", on_press)
        border.bind("<ButtonRelease-1>", on_release)
        label.bind("<ButtonRelease-1>", on_release)
        border.bind("<Enter>", on_enter)
        label.bind("<Enter>", on_enter)
        border.bind("<Leave>", on_leave)
        label.bind("<Leave>", on_leave)
        return border

    # ---- 提交 ----
    def on_submit(self):
        if self._submitting:
            return
        self._submitting = True

        try:
            self._do_submit()
        finally:
            self._submitting = False

    def _do_submit(self):
        exec_list, skip_list = [], []
        for r in self.rows:
            if not r["chk"].get():
                skip_list.append(r)
                continue
            raw = (r["share_var"].get() or "").strip()
            try:
                lots_f = float(raw)
                price = float(r["price_var"].get())
            except ValueError:
                messagebox.showerror("输入有误", f"{r['item'].get('name')} 的手数/价格必须是数字。")
                return
            if lots_f != int(lots_f):
                messagebox.showwarning("手数不合法",
                                       f"{r['item'].get('name')} 的手数必须是整数（1手=100股，不支持半手）。")
                return
            lots = int(lots_f)
            if lots <= 0:
                skip_list.append(r)
                continue
            if price <= 0:
                messagebox.showwarning("价格不合法", f"{r['item'].get('name')} 的成交价必须大于 0。")
                return
            if r["kind"] == "sell" and lots > r["held_lots"]:
                messagebox.showwarning("超出持仓",
                                       f"{r['item'].get('name')} 最多可卖 {r['held_lots']} 手。")
                return
            # 卖满全部手数 = 全清，连带清掉历史零股
            if r["kind"] == "sell" and lots >= r["held_lots"] and r["held_shares"] > 0:
                shares = r["held_shares"]
            else:
                shares = _to_shares(lots)
            r["_lots"], r["_shares"], r["_price"] = lots, shares, price
            exec_list.append(r)

        # 现金预检：买入总额(含费率)不能超过可用现金——否则写库侧会静默跳过，
        # 造成"以为买上了其实没写库"的失真
        cash_now, _, _, _ = _load_account()
        sell_back = sum(r["_shares"] * r["_price"] * 0.9982
                        for r in exec_list if r["kind"] == "sell")
        buy_need = sum(r["_shares"] * r["_price"] * 1.0013
                       for r in exec_list if r["kind"] == "buy")
        if buy_need > cash_now + sell_back:
            messagebox.showwarning(
                "现金不足",
                f"买入合计约 ¥{buy_need:,.0f}(含费)，超过可用现金 ¥{cash_now:,.0f}"
                f"{f' + 卖出回笼 ¥{sell_back:,.0f}' if sell_back else ''}。\n"
                "请减少买入手数，否则超额部分不会写入账户。")
            return

        if not exec_list:
            if not messagebox.askyesno("没有要执行的操作", "所有单都未勾选，将全部视为不成交。仍要提交吗？"):
                return

        # 汇总确认
        lines = []
        for r in exec_list:
            verb = "买入" if r["kind"] == "buy" else "卖出"
            _odd = r["_shares"] - _to_shares(r["_lots"])
            _qty = f"{r['_lots']}手" + (f"+{_odd}股" if _odd > 0 else "")
            lines.append(f"  {verb} {r['item'].get('name')}  {_qty} @ ¥{r['_price']:.2f}")
        for r in skip_list:
            lines.append(f"  跳过 {r['item'].get('name')}")
        if not messagebox.askyesno("确认执行", "\n".join(lines) + "\n\n确认按此写入账户？"):
            return

        # 写回 buy
        for bp in self.buy_paths:
            data = _load_json(bp)
            if not data:
                continue
            for it in data.get("items", []):
                r = self._match(it, "buy")
                if r and r in exec_list:
                    it["actual_lots"] = r["_lots"]
                    it["actual_shares"] = r["_shares"]
                    it["suggest_price"] = r["_price"]
                else:
                    it["actual_lots"] = 0
                    it["actual_shares"] = 0
            self._save_json(bp, data)

        # 写回 sell
        if self.sell_path:
            data = _load_json(self.sell_path)
            if data:
                for it in data.get("items", []):
                    r = self._match(it, "sell")
                    if r and r in exec_list:
                        it["actual_sell"] = True
                        it["actual_lots"] = r["_lots"]
                        it["actual_shares"] = r["_shares"]
                        it["actual_sell_price"] = r["_price"]
                    else:
                        it["actual_sell"] = False
                        it["actual_lots"] = 0
                        it["actual_shares"] = 0
                self._save_json(self.sell_path, data)

        # 写库（--now: 立即执行，不等 15:00）
        try:
            proc = subprocess.run([PYTHON_BIN, MAIN_SCRIPT, "confirm", "--now"],
                                  capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            messagebox.showerror("写库超时", "写库进程超过 120 秒未完成，请稍后检查账户，勿重复提交。")
            return
        except Exception as e:
            messagebox.showerror("写库失败", str(e))
            return

        # 真实校验：pending 文件应已被主脚本改名为 .confirmed.json
        leftover = [p for p in (self.buy_paths + ([self.sell_path] if self.sell_path else []))
                    if os.path.exists(p)]
        if proc.returncode != 0 or leftover:
            detail = (proc.stderr or proc.stdout or "").strip()[-500:]
            messagebox.showerror(
                "写库未完成",
                "确认单未全部写入账户，请勿视为已成交。\n\n"
                + ("未处理: " + ", ".join(os.path.basename(p) for p in leftover) + "\n" if leftover else "")
                + (detail if detail else ""))
            return

        cash, equity, pos, pnl = _load_account()
        messagebox.showinfo("已写入账户",
                            f"最新净值 ¥{equity:,.0f}  ({pnl:+.2f}%)\n"
                            f"可用 ¥{cash:,.0f}  ·  持仓 {pos} 笔")
        self.root.destroy()

    def _match(self, it, kind):
        for r in self.rows:
            if r["kind"] == kind and r["item"].get("symbol") == it.get("symbol"):
                return r
        return None

    def _save_json(self, path, data):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            messagebox.showerror("保存失败", f"{path}\n{e}")


def main():
    args = [a for a in sys.argv[1:] if a]
    if args:
        buy_paths = [a for a in args if "buy" in a and os.path.exists(a)]
        sell_paths = [a for a in args if "sell" in a and os.path.exists(a)]
        sell_path = sell_paths[-1] if sell_paths else None
        if not buy_paths and not sell_path:
            buy_paths, sell_path = _find_pending()
    else:
        buy_paths, sell_path = _find_pending()

    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", 1.4)
    except Exception:
        pass
    ConfirmApp(root, buy_paths, sell_path)
    root.lift()
    root.attributes("-topmost", True)
    root.after(500, lambda: root.attributes("-topmost", False))
    root.mainloop()


if __name__ == "__main__":
    main()

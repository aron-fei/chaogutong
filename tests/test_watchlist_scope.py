# -*- coding: utf-8 -*-
"""watchlist scope 分账回归测试（ADR-0050 附录：题材/权重两套逻辑不可混淆）

铁律（用户 2026-11-06 原话）：「题材 和 权重 是两套逻辑 不能混淆」
  · scope="short" → 短线/题材票，进 13:40/14:40 自选供票源；
  · scope="trend" → 权重/趋势核心票，**绝不**进短线供票源，只走中线趋势池。

全部用临时 WATCHLIST_FILE，不碰 data/watchlist.json 真实数据。
运行：/opt/homebrew/anaconda3/bin/python3 tests/test_watchlist_scope.py
"""
import json
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import watchlist  # noqa: E402


def _fresh(tmpdir):
    """每次测试前：清空模块状态 + 指向临时文件。"""
    watchlist.WATCHLIST_FILE = Path(tmpdir) / "watchlist.json"
    watchlist.WATCHLIST_FILE.write_text("[]", encoding="utf-8")


def main():
    fails = []

    def check(name, cond, detail=""):
        status = "PASS" if cond else "FAIL"
        print(f"[{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
        if not cond:
            fails.append(name)

    with tempfile.TemporaryDirectory() as td:
        # ---- 1. add()：scope 写入 + 默认 short ----
        _fresh(td)
        watchlist.add("600519", "", "short")
        watchlist.add("688981", "中芯国际", "trend")
        watchlist.add("300274", "阳光电源")          # 缺省 → short
        watchlist.add("688347", "华虹公司", "TREND ")  # 大小写/空格容错
        items = watchlist.list_items()
        check("add 写入 scope=short", items[0].get("scope") == "short")
        check("add 写入 scope=trend（中芯国际）", items[1].get("scope") == "trend")
        check("add 缺省 scope=short（阳光电源）", items[2].get("scope") == "short")
        check("add scope 容错归一（TREND→trend）", items[3].get("scope") == "trend")

        # ---- 2. add 非法参数 ----
        try:
            watchlist.add("600519")
            check("add 重复代码抛 ValueError", False)
        except ValueError:
            check("add 重复代码抛 ValueError", True)
        try:
            watchlist.add("abc123", "", "trend")
            check("add 非法代码抛 ValueError", False)
        except ValueError:
            check("add 非法代码抛 ValueError", True)

        # ---- 3. set_scope：命中 / 未命中 ----
        got = watchlist.set_scope("600519", "trend")
        check("set_scope 命中返回列表", isinstance(got, list))
        check("set_scope 生效（600519→trend）",
              any(i["code"] == "600519" and i["scope"] == "trend" for i in (got or [])))
        none = watchlist.set_scope("999999", "trend")
        check("set_scope 未命中返回 None", none is None)
        # 切回 short
        watchlist.set_scope("600519", "short")
        check("set_scope 可切回 short",
              any(i["code"] == "600519" and i["scope"] == "short"
                  for i in watchlist.list_items()))

        # ---- 4. 旧数据兼容：无 scope 字段 → 缺省 short ----
        _fresh(td)
        watchlist.WATCHLIST_FILE.write_text(json.dumps([
            {"code": "600000", "name": "浦发银行", "added_at": "2026-11-01 10:00"}
        ], ensure_ascii=False), encoding="utf-8")
        items = watchlist.list_items()
        check("旧数据无 scope 字段可读", len(items) == 1)
        # evaluate() 行带 scope（空行情容错：网络失败不影响结构断言）
        rows = watchlist.evaluate()
        check("evaluate() 返回 items 列表", isinstance(rows, dict) and "items" in rows)
        r0 = (rows.get("items") or [{}])[0]
        check("evaluate() 行含 scope=short（旧数据缺省）",
              r0.get("scope") == "short", f"got={r0.get('scope')!r}")

        # ---- 5. 关键回归：evaluate() 必须透传 trend（曾因 _items_for_dk 重建丢字段恒读 short）----
        _fresh(td)
        watchlist.WATCHLIST_FILE.write_text(json.dumps([
            {"code": "688981", "name": "中芯国际", "scope": "trend",
             "added_at": "2026-11-06 12:54"}
        ], ensure_ascii=False), encoding="utf-8")
        rows = watchlist.evaluate()
        r_t = (rows.get("items") or [{}])[0]
        check("evaluate() 透传 trend（688981）",
              r_t.get("scope") == "trend", f"got={r_t.get('scope')!r}")

        # ---- 6. 文件落盘持久 ----
        _fresh(td)
        watchlist.add("300750", "宁德时代", "trend")
        disk = json.loads(watchlist.WATCHLIST_FILE.read_text(encoding="utf-8"))
        check("scope 持久化到 watchlist.json",
              disk and disk[0].get("scope") == "trend")

    print("-" * 56)
    total = fails
    print(f"结果：{'全绿 ✔' if not total else 'FAIL × %d → %s' % (len(total), total)}")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())

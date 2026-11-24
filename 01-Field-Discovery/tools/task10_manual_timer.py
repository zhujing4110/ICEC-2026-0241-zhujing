#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
课题10 · 人工耗时实测助手（给「本人实测·待补」那一列用）

审计表 raw_data/task10-batch-audit.csv 里有两列人工耗时：
  · 人工总耗时(分钟,估算)          —— 按动作拆解推算，已在表内
  · 人工耗时(分钟,本人实测·待补)   —— 本工具要填的那 5 个数字

为什么允许「掐子样本再换算」：5 件活按全量做完要 2 小时以上，而单件成本的稳定性
恰恰是本课题的结论之一。所以按子样本计时，再乘回全量，并在备注里写明「样本数 × 倍数」，
读者能一眼看出哪个是实测、哪个是推算。不做静默换算。

用法：
    python tools/task10_manual_timer.py --samples 100   # 先造一批乱名样张（任务1/2 用）
    python tools/task10_manual_timer.py                 # 逐项计时，只打印不写表
    python tools/task10_manual_timer.py --write         # 计时完写回 CSV 并重算倍数
"""

from __future__ import annotations

import argparse
import csv
import random
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

FIELD_DIR = Path(__file__).resolve().parents[1]
CSV_PATH = FIELD_DIR / "raw_data" / "task10-batch-audit.csv"
SAMPLE_DIR = FIELD_DIR / ".manual"
LOG_FILE = FIELD_DIR.parent / "02-20Days-Challenge" / "process_logs" / "runtime-2026-11-21.log"

# 列序号（0-based），与 CSV 表头对应
COL_MANUAL_EST = 4      # 人工总耗时(分钟,估算)
COL_SCRIPT = 5          # 脚本耗时(秒,实测)
COL_RATIO = 6           # 提速倍数
COL_MEASURED = 8        # 人工耗时(分钟,本人实测·待补)
COL_NOTE = 9            # 备注

TASKS = [
    {
        "row": 1, "scale": 100, "sub": 20, "est": 13.3,
        "name": "批量重命名 100 个乱名截图 → 日期_序号",
        "how": "打开 .manual/rename-sample-100/，逐个改名（选中 → F2 → 输入 → 回车）。\n"
               "        不许用资源管理器的「批量重命名」功能，那等于自动化，测出来的不是人工成本。\n"
               "        掐 20 个即可，乘 5 换算。",
    },
    {
        "row": 2, "scale": 500, "sub": 100, "est": 66.7,
        "name": "批量重命名 500 个（扩展性验证）",
        "how": "不用真做 500 个：用第 1 项的秒数 ×5 换算即可（单件成本恒定正是要验证的点）。\n"
               "        若坚持实测，掐 100 个再 ×5。",
        "derive_from": 1,
    },
    {
        "row": 3, "scale": 439, "sub": 100, "est": 14.6,
        "name": "通读 439 行运行日志，挑出 ERROR 并按类型计数",
        "how": f"用编辑器打开 {LOG_FILE.name}，从第一行往下读 100 行，一边读一边记下\n"
               "        你认为是报错的行（读到 ERROR / 超时 / COM 订户 这类）。掐完这 100 行即可，乘 4.39。",
    },
    {
        "row": 4, "scale": 32, "sub": 8, "est": 8.0,
        "name": "统计 32 个文件的行数与字节数",
        "how": "打开 8 个文件：编辑器看行数、右键属性看字节数，记到纸上。掐 8 个，乘 4。\n"
               "        要模拟「人肉做」的真实速度：不许用 Ctrl+Shift+F 全局统计。",
    },
    {
        "row": 5, "scale": 100, "sub": 10, "est": 25.0,
        "name": "给 100 张图按内容打标签（评估后未自动化）",
        "how": "随便找 10 张图（.manual/ 下可自建），逐张看内容 → 判断类别 → 重命名成该类别。\n"
               "        掐 10 张，乘 10。这一项是「不该自动化」的例证，测得准比测得快重要。",
    },
]


def make_samples(n: int) -> None:
    """造 n 个命名混乱的截图文件，供人工重命名计时用。"""
    folder = SAMPLE_DIR / f"rename-sample-{n}"
    if folder.exists():
        for p in folder.iterdir():
            p.unlink()
    folder.mkdir(parents=True, exist_ok=True)
    kinds = ["微信图片", "Screenshot", "IMG", "屏幕截图", "QQ图片"]
    rnd = random.Random(20261124)          # 固定种子，可复现
    for _ in range(n):
        kind = rnd.choice(kinds)
        stamp = f"202611{rnd.randint(10, 23):02d}{rnd.randint(0, 23):02d}{rnd.randint(0, 59):02d}"
        suffix = rnd.choice(["", f"({rnd.randint(1, 9)})", f"_{rnd.randint(10, 999)}"])
        (folder / f"{kind}_{stamp}{suffix}.png").write_bytes(b"x" * 16)
    print(f"已生成 {n} 个乱名样张：{folder}")
    print(f"（该目录已被 .gitignore 忽略，不会进仓库）")
    print(f"做完记得手动清掉，或直接留着，下次重测还能用。")


def ask_int(prompt: str, default: int) -> int:
    raw = input(prompt).strip()
    if not raw:
        return default
    try:
        v = int(raw)
        return v if v > 0 else default
    except ValueError:
        return default


def timed(task: dict) -> dict | None:
    """对一个任务掐一次表，返回结果；跳过返回 None。"""
    print("\n" + "=" * 66)
    print(f"第 {task['row']} 项 · {task['name']}")
    print(f"  估算值 {task['est']} 分钟（全量 {task['scale']} 单位）")
    print(f"  怎么做：{task['how']}")
    print("=" * 66)

    cmd = input("回车开始计时（输入 s 跳过，输入 d 用第 1 项推算）> ").strip().lower()
    if cmd == "s":
        print("  已跳过。")
        return None
    if cmd == "d":
        return {"row": task["row"], "skipped_derive": True}

    t0 = time.perf_counter()
    input("  计时中…做完按回车停表 > ")
    elapsed = time.perf_counter() - t0

    count = ask_int(f"  这 {elapsed:.1f} 秒里实际做了多少单位？（回车 = {task['sub']}）> ", task["sub"])
    per = elapsed / count
    total_min = per * task["scale"] / 60
    print(f"  实测 {elapsed:.1f} s / {count} 单位 → 单件 {per:.2f} s")
    print(f"  换算全量 {task['scale']}：**{total_min:.1f} 分钟**（估算 {task['est']}，"
          f"偏差 {(total_min - task['est']) / task['est'] * 100:+.0f}%）")
    return {"row": task["row"], "count": count, "scale": task["scale"],
            "seconds": round(elapsed, 1), "per": round(per, 2),
            "total_min": round(total_min, 1)}


def write_back(results: dict[int, dict]) -> None:
    """把实测值写回 CSV，并重算倍数；备注里标明样本数与换算倍数。"""
    rows = list(csv.reader(CSV_PATH.open(encoding="utf-8-sig")))
    header, body = rows[0], rows[1:]

    for row_no, res in results.items():
        r = body[row_no - 1]
        if res.get("skipped_derive"):
            # 用第 1 项单件成本推算，不当作独立实测
            src = results.get(1)
            if not src or "per" not in src:
                print(f"  第 {row_no} 项需要先测第 1 项，已跳过。")
                continue
            res = dict(src)
            res["total_min"] = round(src["per"] * TASKS[row_no - 1]["scale"] / 60, 1)
            note = (f"由第1项单件 {src['per']:.2f} s 推算（非独立实测）；"
                    f"实测样本 {src['count']} 件 {src['seconds']:.0f} s")
        else:
            k = res["scale"] / res["count"]
            note = (f"实测样本 {res['count']} 件 {res['seconds']:.0f} s，×{k:g} 推算至全量"
                    if abs(k - 1) > 1e-9 else f"实测全量 {res['count']} 件 {res['seconds']:.0f} s")

        r[COL_MEASURED] = f"{res['total_min']:.1f}"
        script = r[COL_SCRIPT].strip()
        try:
            sec = float(script)
            r[COL_RATIO] = f"{res['total_min'] * 60 / sec:.0f}"
        except ValueError:
            pass                    # 「未自动化」这类非数值，倍数列保持空
        old_note = r[COL_NOTE].strip()
        r[COL_NOTE] = f"{old_note}｜{note}" if old_note else note
        print(f"  第 {row_no} 项 → {r[COL_MEASURED]} 分钟，倍数 {r[COL_RATIO] or '—'}")

    # 四行数值任务都填了，就把表头的「按估算」改成「按实测重算」
    numeric_rows = [i for i in range(1, len(body) + 1)
                    if body[i - 1][COL_SCRIPT].strip().replace(".", "").isdigit()]
    if numeric_rows and all(body[i - 1][COL_MEASURED].strip() for i in numeric_rows):
        header[COL_RATIO] = header[COL_RATIO].replace("按估算", "按实测重算")
        header[COL_MEASURED] = header[COL_MEASURED].replace("·待补", "")

    with CSV_PATH.open("w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerows([header] + body)
    print(f"\n已写回 {CSV_PATH.relative_to(FIELD_DIR.parent)}")


def main() -> int:
    ap = argparse.ArgumentParser(description="课题10 人工耗时实测助手")
    ap.add_argument("--samples", type=int, metavar="N", help="生成 N 个乱名样张后退出")
    ap.add_argument("--write", action="store_true", help="计时结束后写回 CSV（默认只打印）")
    ap.add_argument("--only", type=int, metavar="ROW", help="只测第 ROW 项（1-5）")
    args = ap.parse_args()

    if args.samples:
        make_samples(args.samples)
        return 0

    if not CSV_PATH.exists():
        print(f"找不到审计表：{CSV_PATH}")
        return 1

    todo = [t for t in TASKS if args.only is None or t["row"] == args.only]
    print("课题10 人工耗时实测：每项只掐一小段，工具负责换算到全量。")
    print("提示：全程别用批处理功能，要模拟「人一个个做」的真实速度。")

    results: dict[int, dict] = {}
    for t in todo:
        try:
            res = timed(t)
        except EOFError:
            print("\n（输入流结束，提前停止）")
            break
        if res:
            results[t["row"]] = res

    if not results:
        print("\n没有采集任何数据。")
        return 0

    print("\n" + "-" * 66)
    print(f"{'项':<4}{'实测分钟':>10}{'估算分钟':>10}{'偏差':>8}   备注")
    for row_no, res in sorted(results.items()):
        if res.get("skipped_derive"):
            print(f"{row_no:<4}{'（由第1项推算）':>12}")
            continue
        est = TASKS[row_no - 1]["est"]
        dev = (res["total_min"] - est) / est * 100
        print(f"{row_no:<4}{res['total_min']:>10.1f}{est:>10.1f}{dev:>7.0f}%   样本 {res['count']} 件 / {res['seconds']:.0f} s")

    if args.write:
        write_back(results)
        print("记得同步 report.md：去掉「按估算」标注，局限第一条改写为实测值。")
    else:
        print("\n未写表。确认无误后加 --write 重跑一次（或把上面 5 个数发我，我代填）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

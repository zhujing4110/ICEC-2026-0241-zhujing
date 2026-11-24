#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
课题04 · 食堂窗口／电梯口高峰排队吞吐 —— 每 2 分钟记录助手

连续 30 分钟、每 2 分钟记一次，共 15 组；读数**直接写入**
raw_data/task04-queue-throughput.csv，不用记行号、不用手算时间点。

用法：
    python tools/task04_queue_timer.py --kind 食堂 --place "二食堂 3 号窗口" --counters 2
    python tools/task04_queue_timer.py --kind 电梯 --place "教学楼 A 座 1 层电梯口" --counters 3

可选参数：
    --duration 30        总时长（分钟，默认 30，与大纲一致）
    --interval 2         记录间隔（分钟，默认 2，与大纲一致）
    --start-queue 5      开测瞬间的排队人数（不填则开测时问一次）
    --append             接着上次的进度继续记（中断过后用）
    --fast               自测用：把等待压到 0.5 秒，不用于真实测量

四项读数怎么取：
    排队人数        这一时刻队伍里有几个人（正在接受服务的不算，站着等饭/等梯的算）
    间隔内到达      过去 2 分钟新排进队伍的人数
    间隔内完成      过去 2 分钟拿到饭／上电梯离开的人数
    单次服务耗时    挑这 2 分钟里一个人，从他开始点单/进轿厢计时到结束（秒）；
                    懒得掐就给空，脚本按「间隔 ÷ 完成人数」自动折算并标注

一个容易漏的点：**开测那一刻的排队人数要单独记**（写在 meta 表里），
否则时序曲线缺起点，后面算平均等待时间会偏差。
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = Path(__file__).resolve().parents[1]
CSV_PATH = BASE / "raw_data" / "task04-queue-throughput.csv"
META_PATH = BASE / "raw_data" / "task04-queue-meta.csv"

DATA_COLS = ["序号", "时点", "间隔内到达(人)", "间隔内完成(人)", "排队人数(人)",
             "单次服务耗时(s)", "开放窗口数", "备注"]
META_COLS = ["场景类型", "地点", "日期", "开始时间", "计划时长(分钟)", "记录间隔(分钟)",
             "窗口或电梯数", "起始排队人数", "备注"]


# ---------------------------------------------------------------- CSV 读写

def load_data() -> list[dict]:
    if not CSV_PATH.exists():
        with CSV_PATH.open("w", newline="", encoding="utf-8-sig") as f:
            csv.DictWriter(f, fieldnames=DATA_COLS).writeheader()
    with CSV_PATH.open(encoding="utf-8-sig", newline="") as f:
        return [dict(r) for r in csv.DictReader(f)]


def save_data(rows: list[dict]) -> None:
    with CSV_PATH.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=DATA_COLS)
        w.writeheader()
        w.writerows(rows)


def save_meta(meta: dict) -> None:
    with META_PATH.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=META_COLS)
        w.writeheader()
        w.writerow({k: meta.get(k, "") for k in META_COLS})


def load_meta() -> dict:
    if not META_PATH.exists():
        return {}
    with META_PATH.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    return rows[0] if rows else {}


def filled(rows: list[dict]) -> list[dict]:
    """返回已有读数的行（排队人数或到达人数非空即算已填）。"""
    return [r for r in rows if str(r.get("排队人数(人)", "")).strip()
            or str(r.get("间隔内到达(人)", "")).strip()]


# ---------------------------------------------------------------- 交互

def ask_number(prompt: str, allow_blank: bool = False) -> float | None:
    while True:
        try:
            raw = input(prompt).strip().replace("，", ",")
        except EOFError:
            print("\n输入被中断，已记录的数据都保存了。")
            raise SystemExit(130)
        if not raw:
            if allow_blank:
                return None
            print("  这一项必须填，重来。")
            continue
        try:
            return float(raw)
        except ValueError:
            print("  需要填数字，重来。")


def fmt_num(v: float | None) -> str:
    if v is None:
        return ""
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


# ---------------------------------------------------------------- 统计

def summarize(rows: list[dict], interval: float, counters: float) -> dict:
    """按排队论口径算到达率、服务率、强度、平均队长与平均等待。"""
    at = [float(r["间隔内到达(人)"]) for r in rows if str(r.get("间隔内到达(人)", "")).strip()]
    dn = [float(r["间隔内完成(人)"]) for r in rows if str(r.get("间隔内完成(人)", "")).strip()]
    qs = [float(r["排队人数(人)"]) for r in rows if str(r.get("排队人数(人)", "")).strip()]
    sv = [float(r["单次服务耗时(s)"]) for r in rows
          if str(r.get("单次服务耗时(s)", "")).strip() and float(r.get("单次服务耗时(s)") or 0) > 0]

    n = len(rows)
    lam = (sum(at) / n / interval) if (at and n) else 0.0        # 到达率 人/分钟
    sv_mean = (sum(sv) / len(sv)) if sv else 0.0                  # 单次服务耗时 秒
    mu = 60.0 / sv_mean if sv_mean > 0 else 0.0                   # 单窗口服务率 人/分钟
    rho = lam / (counters * mu) if (mu > 0 and counters > 0) else 0.0
    lq = sum(qs) / len(qs) if qs else 0.0
    wq = (lq / lam) if lam > 0 else 0.0                           # Little 定律：W = L / λ
    return {
        "n": n, "lam": lam, "sv_mean": sv_mean, "mu": mu, "rho": rho,
        "lq": lq, "wq": wq,
        "arr_total": sum(at), "done_total": sum(dn), "backlog": sum(at) - sum(dn),
    }


def print_summary(st: dict, counters: float) -> None:
    print()
    print("=" * 60)
    print("  本次读数小结（可直接抄进 report.md 后核对）")
    print("=" * 60)
    print(f"  有效记录        {st['n']} 组")
    print(f"  累计到达/完成   {st['arr_total']:g} 人 / {st['done_total']:g} 人"
          f"   净积压 {st['backlog']:+.0f} 人")
    print(f"  到达率 λ        {st['lam']:.2f} 人/分钟")
    print(f"  单次服务耗时    {st['sv_mean']:.1f} 秒  → 单窗口服务率 μ {st['mu']:.2f} 人/分钟")
    print(f"  窗口数 c        {counters:g}")
    print(f"  服务强度 ρ      {st['rho']:.2f}   " + (
        "（<1 表示窗口能兜住，队列不该无限长）" if st["rho"] < 1
        else "（≥1 表示供给不足，队列必然发散——这就是瓶颈）"))
    print(f"  平均排队人数    {st['lq']:.1f} 人")
    print(f"  平均等待时间    {st['wq']:.2f} 分钟（Little 定律 L = λ·W 反解）")
    if st["rho"] < 1 and st["backlog"] > 0:
        print()
        print("  ⚠ ρ<1 但净积压在涨：多半是抽测的服务耗时偏短（挑了点得快的），")
        print("    或中途有窗口停开没记。回看备注列，必要时补测 3~5 次服务耗时。")
    print()
    print("  接下来跑一次分析，拿到可粘贴的表格：")
    print("    python tools/task04_analyze.py")
    print("  别忘了同框照：队伍 + 窗口招牌 + 你的手/身影进同一个画面。")


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="课题04 排队吞吐记录助手")
    ap.add_argument("--kind", default="食堂", choices=["食堂", "电梯"], help="场景类型")
    ap.add_argument("--place", default="", help="具体地点，例如“二食堂 3 号窗口”")
    ap.add_argument("--counters", type=float, default=1, help="开放窗口数／电梯台数")
    ap.add_argument("--duration", type=float, default=30, help="总时长（分钟）")
    ap.add_argument("--interval", type=float, default=2, help="记录间隔（分钟）")
    ap.add_argument("--start-queue", type=float, default=None, help="开测瞬间排队人数")
    ap.add_argument("--append", action="store_true", help="接着已有进度继续记")
    ap.add_argument("--fast", action="store_true", help="自测用：等待压到 0.5 秒")
    args = ap.parse_args()

    rows = load_data()
    done = len(filled(rows))
    if done and not args.append:
        print(f"表里已经有 {done} 组读数。")
        print("  想接着记加 --append；想重测就先把 raw_data/task04-queue-throughput.csv 清空。")
        return 1

    need = int(round(args.duration / args.interval))
    first = (done + 1) if args.append else 1
    if first > need:
        print(f"已经记满 {need} 组，不需要再记。要重测请先清空数据表。")
        return 0
    print("=" * 60)
    print(f"  课题04 · {args.kind}排队吞吐   {args.place or '(地点未填)'}")
    print(f"  {args.duration:g} 分钟 / 每 {args.interval:g} 分钟记一次 = {need} 组")
    print("=" * 60)
    print("开测前 30 秒确认：")
    print("  1. 站在能同时看到队尾和窗口的位置，别堵住通道")
    print("  2. 手机计时开着，纸笔或本脚本，二选一")
    print("  3. 记下当前窗口开了几个（中途增减要写进备注）")
    print()

    start_q = args.start_queue
    meta_old = load_meta() if args.append else {}
    if meta_old:
        # 续记：沿用上一次的日期/开始时间/起始排队，不要把 meta 覆盖成第二次的时间
        if start_q is None:
            try:
                start_q = float(meta_old.get("起始排队人数") or 0)
            except ValueError:
                start_q = 0.0
        if args.counters == 1:
            try:
                args.counters = float(meta_old.get("窗口或电梯数") or 1)
            except ValueError:
                pass
        meta = dict(meta_old)
        print(f"续记：沿用 {meta.get('日期','')} {meta.get('开始时间','')} 起的那次测量")
    else:
        if start_q is None:
            start_q = ask_number("开测瞬间排队人数（人）：")
        meta = {
            "场景类型": args.kind,
            "地点": args.place,
            "日期": datetime.now().strftime("%Y-%m-%d"),
            "开始时间": datetime.now().strftime("%H:%M"),
            "计划时长(分钟)": fmt_num(args.duration),
            "记录间隔(分钟)": fmt_num(args.interval),
            "窗口或电梯数": fmt_num(args.counters),
            "起始排队人数": fmt_num(start_q),
            "备注": "",
        }
    save_meta(meta)

    print()
    if not args.append:
        input(f"按回车开测（每 {args.interval:g} 分钟提醒一次，Ctrl+C 可中断）...")
    if args.fast:
        print("  [--fast 自测模式：等待被压缩，不要用于真实测量]")

    start = time.time()
    last_sv: float | None = None

    try:
        for k in range(first, need + 1):
            wait = 0.5 if args.fast else args.interval * 60
            target = start + (k - first) * wait + wait
            remain = target - time.time()
            if remain > 0:
                time.sleep(remain)
            print("\a", end="")
            now = datetime.now().strftime("%H:%M")
            print()
            print(f"—— 第 {k}/{need} 组   此刻 {now}（已过 {k * args.interval:g} 分钟）——")
            q = ask_number("  当前排队人数：")
            arrive = ask_number("  过去 %g 分钟到达人数：" % args.interval, allow_blank=True)
            done_n = ask_number("  过去 %g 分钟完成人数：" % args.interval, allow_blank=True)
            sv = ask_number("  单次服务耗时（秒，回车=自动折算）：", allow_blank=True)

            note = ""
            if sv is None:
                if done_n:
                    sv = args.interval * 60 / done_n
                    note = "服务耗时按间隔÷完成数折算"
                elif last_sv:
                    sv = last_sv
                    note = "沿用上一次服务耗时"
                else:
                    note = "服务耗时缺，分析时会跳过"
            else:
                last_sv = sv

            row = rows[k - 1] if k - 1 < len(rows) else {c: "" for c in DATA_COLS}
            row.update({
                "序号": k,
                "时点": now,
                "间隔内到达(人)": fmt_num(arrive if arrive is not None else 0),
                "间隔内完成(人)": fmt_num(done_n if done_n is not None else 0),
                "排队人数(人)": fmt_num(q),
                "单次服务耗时(s)": fmt_num(sv),
                "开放窗口数": fmt_num(args.counters),
                "备注": note,
            })
            if k - 1 >= len(rows):
                rows.append(row)
            save_data(rows)
            print(f"  已存：排队 {fmt_num(q)} 人 / 到达 {fmt_num(arrive)} / 完成 {fmt_num(done_n)}")
    except KeyboardInterrupt:
        print("\n已中断，已记录的数据都保存了。接着记用 --append。")
        return 130

    st = summarize(filled(rows), args.interval, args.counters)
    print_summary(st, args.counters)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
课题02 · 手机多工况发热与掉电 —— 定时记录助手（四工况连跑版）

大纲原文只要求「4 种工况各 30 分钟内每 5 分钟记录电量与发热」，**没有规定工况之间必须冷却**。
所以这里给两种跑法，自己按剩余时间选：

  1) 严谨版（默认）：每段前静置 10 分钟回温 → 4×30 + 4×10 ≈ 2 h 40 min
     四段起点温度一致，绝对温度与掉电曲线可以横向比较。

  2) 紧凑版：--cooldown 0 → 4×30 = 2 h
     段间余温叠加，后段起点更热；只能比较「段内升温速率」，不能比较绝对温度。
     （报告里必须写明这一点，否则算改口径不说明。）

用法：
    python tools/task02_timer.py --all                  # 一次跑完四段（推荐）
    python tools/task02_timer.py --all --cooldown 0     # 2 小时紧凑版
    python tools/task02_timer.py --mode 待机            # 只跑单段（中断后续测）

跑起来之后中途不用管：每 5 分钟响一声，你只需要看一眼手机，报两个数（电量和电池温度）。
每段开头额外问一次「起点读数」，写进 raw_data/task02-phase-start.csv，用来算段内 ΔT。

读数从哪来：
    电量   系统设置 → 电池，或下拉控制中心
    温度   装 AccuBattery / DevCheck，读「电池温度」（不是电池健康度）
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = Path(__file__).resolve().parents[1]
CSV_PATH = BASE / "raw_data" / "task02-phone-thermal.csv"
START_PATH = BASE / "raw_data" / "task02-phase-start.csv"

MODES = ["待机", "短视频", "导航/拍照", "高帧率游戏"]
DATA_COLS = ["序号", "工况", "时间点(分钟)", "电量(%)", "电池温度(℃)", "环境室温(℃)", "备注"]
START_COLS = ["工况", "段前静置(分钟)", "起点时刻", "起点电量(%)", "起点温度(℃)", "环境室温(℃)", "备注"]


# ---------------------------------------------------------------- IO

def load_rows() -> tuple[list[str], list[dict]]:
    with CSV_PATH.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return reader.fieldnames or DATA_COLS, [dict(r) for r in reader]


def save_rows(fieldnames: list[str], rows: list[dict]) -> None:
    with CSV_PATH.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def load_starts() -> dict[str, dict]:
    if not START_PATH.exists():
        return {}
    with START_PATH.open(encoding="utf-8-sig", newline="") as f:
        return {r["工况"]: r for r in csv.DictReader(f) if r.get("工况")}


def save_start(rec: dict) -> None:
    data = load_starts()
    data[rec["工况"]] = rec
    with START_PATH.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=START_COLS)
        w.writeheader()
        for m in MODES:
            if m in data:
                w.writerow({k: data[m].get(k, "") for k in START_COLS})


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


def fmt(v: float | None) -> str:
    if v is None:
        return ""
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


# ---------------------------------------------------------------- 节奏

class Clock:
    """真实时间里 1 分钟 = 60 秒；--fast 自测时 1 分钟 = 0.05 秒。"""

    def __init__(self, fast: bool = False):
        self.scale = 0.05 / 60 if fast else 1.0

    def sleep_minutes(self, minutes: float) -> None:
        target = time.time() + minutes * 60 * self.scale
        remain = target - time.time()
        if remain > 0:
            time.sleep(remain)


def wait_with_marks(total_min: float, clock: Clock, label: str) -> None:
    """静置倒计时：只在 一半 / 1 分钟 / 结束时提示，不刷屏。"""
    print(f"  {label} 开始（{total_min:g} 分钟）")
    marks = sorted({m for m in (total_min / 2, 1.0) if 0 < m < total_min}, reverse=True)
    left = total_min
    for m in marks:
        clock.sleep_minutes(left - m)
        left = m
        print(f"    还剩 {m:g} 分钟")
    clock.sleep_minutes(left)
    print(f"    {label} 结束")


# ---------------------------------------------------------------- 单段

def run_phase(mode: str, fieldnames: list[str], rows: list[dict],
              duration: float, interval: float, room: float | None,
              clock: Clock, cooldown: float) -> None:
    print()
    print("-" * 56)
    print(f"  工况：{mode}")
    print("-" * 56)

    if cooldown > 0:
        wait_with_marks(cooldown, clock, "静置回温")

    print("  这段开始前，读一次起点读数（用来算段内 ΔT）：")
    b0 = ask_number("  起点电量（%）：")
    t0 = ask_number("  起点温度（℃）：")
    save_start({
        "工况": mode, "段前静置(分钟)": fmt(cooldown),
        "起点时刻": datetime.now().strftime("%H:%M"),
        "起点电量(%)": fmt(b0), "起点温度(℃)": fmt(t0),
        "环境室温(℃)": fmt(room), "备注": "",
    })

    points = list(range(1, int(duration / interval) + 1))
    for i in points:
        mark = i * interval
        clock.sleep_minutes(interval)
        print("\a", end="")
        print(f"  —— {mode} 第 {mark:g} 分钟（{datetime.now():%H:%M}）——")
        battery = ask_number("    电量（%）：")
        temp = ask_number("    电池温度（℃）：")

        hit = False
        for r in rows:
            if r.get("工况") == mode and r.get("时间点(分钟)") == fmt(mark):
                r["电量(%)"] = fmt(battery)
                r["电池温度(℃)"] = fmt(temp)
                if room is not None:
                    r["环境室温(℃)"] = fmt(room)
                hit = True
                break
        if not hit:
            print(f"    ⚠ 表里没有「{mode} / {mark:g} 分钟」这一行，本次未保存")
        save_rows(fieldnames, rows)


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="课题02 定时记录助手（四工况连跑）")
    ap.add_argument("--all", action="store_true", help="一次跑完四个工况（推荐）")
    ap.add_argument("--mode", choices=MODES, help="只跑某一个工况")
    ap.add_argument("--duration", type=float, default=30, help="每工况时长（分钟，默认 30）")
    ap.add_argument("--interval", type=float, default=5, help="记录间隔（分钟，默认 5）")
    ap.add_argument("--cooldown", type=float, default=10, help="每段前静置分钟数，0 = 不静置")
    ap.add_argument("--room", type=float, default=None, help="环境室温（℃，不填则问一次）")
    ap.add_argument("--fast", action="store_true", help="自测用：1 分钟压成 0.05 秒")
    args = ap.parse_args()

    if not args.all and not args.mode:
        ap.error("要么 --all，要么 --mode 指定单个工况")

    if not CSV_PATH.exists():
        print(f"找不到数据表: {CSV_PATH}")
        return 1

    fieldnames, rows = load_rows()
    phases = MODES if args.all else [args.mode]

    total = len(phases) * args.duration + len(phases) * args.cooldown
    end_at = datetime.now() + timedelta(minutes=total)

    print("=" * 60)
    print(f"  课题02 · {'四工况连跑' if args.all else args.mode}")
    print(f"  每段 {args.duration:g} 分钟 × 每 {args.interval:g} 分钟记一次"
          f" + 段前静置 {args.cooldown:g} 分钟")
    print(f"  合计约 {total:g} 分钟，预计 {end_at:%H:%M} 结束")
    print("=" * 60)
    print("开始前 60 秒确认：")
    print("  1. 全程不充电，屏幕亮度固定，连同一个 Wi-Fi")
    print("  2. 手机放在桌上通风处，别压枕头/被子上测（散热条件会变）")
    print("  3. 装好 AccuBattery / DevCheck，能读「电池温度」")
    print("  4. 中途要拍同框照：手机屏幕读数 + 桌面/环境 + 你的手")
    print()

    room = args.room
    if room is None:
        room = ask_number("当前环境室温（℃，可回车跳过）：", allow_blank=True)

    if not args.fast:
        input("按回车开始（之后不用管，响一声就报两个数；Ctrl+C 可中断）...")

    clock = Clock(args.fast)
    try:
        for mode in phases:
            run_phase(mode, fieldnames, rows, args.duration, args.interval,
                      room, clock, args.cooldown)
    except KeyboardInterrupt:
        print("\n已中断，已记录的数据都保存了。接着跑用 --mode 指定没跑完的那段。")
        return 130

    filled = sum(1 for r in rows if str(r.get("电量(%)", "")).strip())
    print()
    print("=" * 60)
    print(f"  完成：{filled} / {len(rows)} 行有数据")
    print(f"  时间序列  raw_data/{CSV_PATH.name}")
    print(f"  段起点    raw_data/{START_PATH.name}")
    print()
    print("  下一步：python tools/task02_analyze.py")
    print("  别忘了同框照（至少覆盖 2 种工况 + 1 张温度读数特写）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

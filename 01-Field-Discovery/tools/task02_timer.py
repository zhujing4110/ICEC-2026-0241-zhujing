#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
课题02 · 手机多工况发热与掉电 —— 定时记录助手

按 5 分钟间隔提醒你读一次电量与电池温度，并**直接写入** raw_data/task02-phone-thermal.csv，
避免手抄出错、也不用自己算时间点。

用法（每个工况单独跑一次，共 4 次）：
    python tools/task02_timer.py --mode 待机
    python tools/task02_timer.py --mode 短视频
    python tools/task02_timer.py --mode 导航/拍照
    python tools/task02_timer.py --mode 高帧率游戏

可选参数：
    --duration 30     总时长（分钟，默认 30）
    --interval 5      记录间隔（分钟，默认 5）
    --room 26         环境室温（℃，默认从命令行读一次）

读数从哪来：
    电量   系统设置 → 电池，或下拉控制中心
    温度   推荐装 AccuBattery / DevCheck，读「电池温度」（不是电池健康度）
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

CSV_PATH = Path(__file__).resolve().parents[1] / "raw_data" / "task02-phone-thermal.csv"
MODES = ["待机", "短视频", "导航/拍照", "高帧率游戏"]


def load_rows() -> tuple[list[str], list[dict]]:
    with CSV_PATH.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return reader.fieldnames or [], [dict(r) for r in reader]


def save_rows(fieldnames: list[str], rows: list[dict]) -> None:
    with CSV_PATH.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def ask_number(prompt: str, allow_blank: bool = False) -> str:
    while True:
        raw = input(prompt).strip()
        if not raw and allow_blank:
            return ""
        try:
            float(raw)
            return raw
        except ValueError:
            print("  需要填数字，重来。")


def main() -> int:
    ap = argparse.ArgumentParser(description="课题02 定时记录助手")
    ap.add_argument("--mode", required=True, choices=MODES)
    ap.add_argument("--duration", type=int, default=30, help="总时长（分钟）")
    ap.add_argument("--interval", type=int, default=5, help="记录间隔（分钟）")
    args = ap.parse_args()

    if not CSV_PATH.exists():
        print(f"找不到数据表: {CSV_PATH}")
        return 1

    fieldnames, rows = load_rows()

    print("=" * 56)
    print(f"  课题02 · {args.mode} 工况")
    print("=" * 56)
    print("开测前请确认：")
    print("  1. 手机已静置 10 分钟，摸起来不温")
    print("  2. 屏幕亮度固定，不充电，连同一个 Wi-Fi")
    print("  3. 准备好同框照：手机屏幕读数 + 桌面/环境入镜")
    print()
    room = ask_number("当前环境室温（℃）：", allow_blank=True)
    print()
    input(f"按回车开始计时（之后每 {args.interval} 分钟记录一次，Ctrl+C 可中断）...")

    start = time.time()
    points = list(range(args.interval, args.duration + 1, args.interval))

    try:
        for minute in points:
            target = start + minute * 60
            remain = target - time.time()
            if remain > 0:
                time.sleep(remain)
            print()
            print(f"—— 第 {minute} 分钟 ——")
            battery = ask_number("  电量（%）：")
            temp = ask_number("  电池温度（℃）：")

            hit = False
            for r in rows:
                if r.get("工况") == args.mode and r.get("时间点(分钟)") == str(minute):
                    r["电量(%)"] = battery
                    r["机身温度(℃)"] = temp
                    if room:
                        r["环境室温(℃)"] = room
                    hit = True
                    break
            if not hit:
                print(f"  ⚠ 表里没有「{args.mode} / {minute} 分钟」这一行，本次未保存")
            save_rows(fieldnames, rows)
    except KeyboardInterrupt:
        print("\n已中断，已记录的数据已保存。")
        return 130

    use_min = (time.time() - start) / 60
    print()
    print(f"完成：{args.mode}，计划 {args.duration} 分钟，实际 {use_min:.1f} 分钟")
    print(f"数据已写入 {CSV_PATH.name}，建议此时补拍同框照。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

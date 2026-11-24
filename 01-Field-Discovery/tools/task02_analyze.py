#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
课题02 · 数据回填后的一键核算

读 raw_data/task02-phone-thermal.csv（24 行时序）与 task02-phase-start.csv（每段起点），
算出每段：起点温度 → 终点温度、段内升温速率、掉电速率、末段是否收敛，
并打出可直接粘进 report.md 的 Markdown 表格。**只算不做结论**。

用法：
    python tools/task02_analyze.py
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = Path(__file__).resolve().parents[1]
CSV_PATH = BASE / "raw_data" / "task02-phone-thermal.csv"
START_PATH = BASE / "raw_data" / "task02-phase-start.csv"
MODES = ["待机", "短视频", "导航/拍照", "高帧率游戏"]


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return [dict(r) for r in csv.DictReader(f)]


def num(row: dict, key: str) -> float | None:
    raw = str(row.get(key, "")).strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def slope(xs: list[float], ys: list[float]) -> float:
    """最小二乘斜率。"""
    n = len(xs)
    if n < 2:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


def main() -> int:
    ap = argparse.ArgumentParser(description="课题02 发热与掉电核算")
    ap.add_argument("--duration", type=float, default=30, help="每工况时长（分钟）")
    args = ap.parse_args()

    rows = [r for r in read_csv(CSV_PATH)
            if num(r, "电量(%)") is not None or num(r, "电池温度(℃)") is not None]
    starts = {r.get("工况"): r for r in read_csv(START_PATH)}

    if not rows:
        print(f"{CSV_PATH.name} 里还没有读数——先跑 tools/task02_timer.py --all 再回来。")
        return 1

    print("## 原始时序（可直接粘进 report.md）")
    print()
    print("| 序号 | 工况 | 时间(min) | 电量(%) | 电池温度(℃) | 环境室温(℃) |")
    print("|---|---|---|---|---|---|")
    for r in rows:
        def c(k: str) -> str:
            v = num(r, k)
            return "" if v is None else (str(int(v)) if v.is_integer() else f"{v:g}")
        print(f"| {r.get('序号','')} | {r.get('工况','')} | {r.get('时间点(分钟)','')} | "
              f"{c('电量(%)')} | {c('电池温度(℃)')} | {c('环境室温(℃)')} |")
    print()

    print("## 分段指标")
    print()
    print("| 工况 | 起点温度 | 终点温度 | 段内 ΔT | 升温速率(℃/min) | 起点电量 | 掉电 | 掉电速率(%/min) | 末两点温差 |")
    print("|---|---|---|---|---|---|---|---|---|")
    summary = []
    for mode in MODES:
        seg = [r for r in rows if r.get("工况") == mode]
        ts = [num(r, "电池温度(℃)") for r in seg]
        bs = [num(r, "电量(%)") for r in seg]
        mins = [num(r, "时间点(分钟)") for r in seg]
        if not ts or any(v is None for v in ts):
            continue
        st = starts.get(mode, {})
        t_start = num(st, "起点温度(℃)") if st else None
        b_start = num(st, "起点电量(%)") if st else None
        t_end = ts[-1]
        t0 = t_start if t_start is not None else ts[0]
        span = (mins[0] if t_start is None else 0.0)
        dt = t_end - t0
        dtemp_rate = slope([m for m in mins], ts) if all(v is not None for v in mins) else 0.0
        drop = (b_start - bs[-1]) if (b_start is not None and bs[-1] is not None) else None
        span_full = max(mins) if mins else args.duration
        drop_rate = (drop / span_full) if drop is not None else None
        tail = ts[-1] - ts[-2] if len(ts) >= 2 else 0.0
        summary.append({
            "mode": mode, "t0": t0, "t_end": t_end, "dt": dt,
            "rate": dtemp_rate, "b0": b_start, "b_end": bs[-1],
            "drop": drop, "drop_rate": drop_rate, "tail": tail, "span": span_full,
            "t_start_from_csv": t_start is None,
        })
        def f(v, nd=1, sign=False):
            if v is None:
                return ""
            return f"{v:+.{nd}f}" if sign else f"{v:.{nd}f}"
        print(f"| {mode} | {f(t0)} | {f(t_end)} | {f(dt, 1, True)} | {f(dtemp_rate, 2, True)} | "
              f"{f(b_start, 0)} | {f(drop, 0) if drop is not None else ''} | "
              f"{f(drop_rate, 2) if drop_rate is not None else ''} | {f(tail, 1, True)} |")
    print()
    print("> 升温速率用段内全部记录点做最小二乘拟合，比「ΔT ÷ 时长」更抗单点读数抖动。")
    print("> 起点温度取自 `task02-phase-start.csv`；缺起点时会退回第一个记录点，并扣掉那 5 分钟跨度。")
    print()

    if summary:
        hottest = max(summary, key=lambda s: s["rate"])
        coolest = min(summary, key=lambda s: s["rate"])
        print("## 脚本自动指出的三处（归因仍要人写）")
        print()
        print(f"1. 升温最陡的是 **{hottest['mode']}**（{hottest['rate']:+.2f} ℃/min），"
              f"最平缓的是 **{coolest['mode']}**（{coolest['rate']:+.2f} ℃/min），"
              f"相差约 {abs(hottest['rate'] - coolest['rate']):.2f} ℃/min。")
        conv = [s for s in summary if abs(s["tail"]) <= 0.3]
        if conv:
            print(f"2. {'、'.join(s['mode'] for s in conv)} 在最后 5 分钟内温差 ≤0.3 ℃，"
                  f"说明已接近散热平衡（曲线走平）；其余工况仍在爬升，说明还没触顶。")
        else:
            sp = max(s["span"] for s in summary)
            print(f"2. 四段最后 5 分钟内温度都还在上升（温差 >0.3 ℃），"
                  f"{sp:g} 分钟内没跑到热平衡——这是「观测时长短于热时间常数」的证据，要写进局限。")
        drains = [s for s in summary if s["drop_rate"] is not None]
        if drains:
            worst = max(drains, key=lambda s: s["drop_rate"])
            print(f"3. 掉电最快的是 **{worst['mode']}**（{worst['drop_rate']:.2f} %/min，"
                  f"{worst['span']:g} 分钟掉 {worst['drop']:.0f}%）。把四段的掉电速率排序，"
                  f"与升温速率排序对照，就能回答「是升温拖垮续航，还是功耗本身高」。")
        if all(s["t_start_from_csv"] for s in summary):
            print()
            print("⚠ 缺 `task02-phase-start.csv` 的起点读数，ΔT 已退回用 5 分钟点计算，"
                  "会系统性低估前 5 分钟的温升。补上起点再跑一次更准。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

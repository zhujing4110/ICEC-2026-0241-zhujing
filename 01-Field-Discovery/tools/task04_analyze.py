#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
课题04 · 数据回填后的一键核算

读 raw_data/task04-queue-throughput.csv 与 task04-queue-meta.csv，
算出到达率、服务率、服务强度、平均队长与平均等待，并打出可直接粘进
report.md 的 Markdown 表格。**只算不做结论**——瓶颈归因要人看现场，
脚本算不出现场的“窗口开了几个但只有一个在打菜”这种事。

用法：
    python tools/task04_analyze.py
    python tools/task04_analyze.py --interval 2 --counters 2
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
CSV_PATH = BASE / "raw_data" / "task04-queue-throughput.csv"
META_PATH = BASE / "raw_data" / "task04-queue-meta.csv"


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


def col(rows: list[dict], key: str) -> list[float]:
    return [v for v in (num(r, key) for r in rows) if v is not None]


def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def main() -> int:
    ap = argparse.ArgumentParser(description="课题04 排队数据核算")
    ap.add_argument("--interval", type=float, default=None, help="记录间隔（分钟），默认读 meta")
    ap.add_argument("--counters", type=float, default=None, help="窗口/电梯数，默认读 meta")
    args = ap.parse_args()

    rows = [r for r in read_csv(CSV_PATH)
            if num(r, "排队人数(人)") is not None or num(r, "间隔内到达(人)") is not None]
    meta = (read_csv(META_PATH) or [{}])[0]

    if not rows:
        print(f"{CSV_PATH.name} 里还没有读数——先按要求测完再跑。")
        return 1

    interval = args.interval or num(meta, "记录间隔(分钟)") or 2.0
    counters = args.counters or num(meta, "窗口或电梯数") or 1.0

    arrive = col(rows, "间隔内到达(人)")
    served = col(rows, "间隔内完成(人)")
    queue = col(rows, "排队人数(人)")
    sv = [v for v in col(rows, "单次服务耗时(s)") if v > 0]

    lam = mean(arrive) / interval if arrive else 0.0
    sv_mean = mean(sv)
    mu = 60.0 / sv_mean if sv_mean > 0 else 0.0
    rho = lam / (counters * mu) if (mu > 0 and counters > 0) else 0.0
    lq = mean(queue)
    wq = lq / lam if lam > 0 else 0.0
    start_q = num(meta, "起始排队人数") or 0.0

    print("=" * 64)
    print(f"  {meta.get('日期','?')} {meta.get('开始时间','?')} 起  "
          f"{meta.get('场景类型','?')} · {meta.get('地点','?')} · {counters:g} 个窗口")
    print(f"  记录 {len(rows)} 组 / 间隔 {interval:g} 分钟 / 起始排队 {start_q:g} 人")
    print("=" * 64)
    print()

    print("## 排队时序（可直接粘进 report.md）")
    print()
    print("| 序号 | 时点 | 排队人数 | 2min 到达 | 2min 完成 | 单次服务(s) |")
    print("|---|---|---|---|---|---|")
    for r in rows:
        def cell(k: str) -> str:
            v = num(r, k)
            return "" if v is None else (str(int(v)) if v.is_integer() else f"{v:g}")
        print(f"| {r.get('序号','')} | {r.get('时点','')} | {cell('排队人数(人)')} | "
              f"{cell('间隔内到达(人)')} | {cell('间隔内完成(人)')} | {cell('单次服务耗时(s)')} |")
    print()

    print("## 关键指标")
    print()
    print(f"- 到达率 λ = {lam:.2f} 人/分钟（每 {interval:g} 分钟平均来 {mean(arrive):.1f} 人）")
    print(f"- 单次服务耗时 = {sv_mean:.1f} 秒 → 单窗口服务率 μ = {mu:.2f} 人/分钟")
    print(f"- 服务强度 ρ = λ / (c·μ) = {rho:.2f}")
    print(f"- 平均队长 Lq = {lq:.1f} 人；平均等待 Wq = Lq/λ = {wq:.2f} 分钟")
    print(f"- 累计到达 {sum(arrive):g} 人 / 完成 {sum(served):g} 人 → 净积压 "
          f"{sum(arrive) - sum(served):+.0f} 人")
    print()

    # 自动能看出来的三件事
    if queue:
        peak_i = max(range(len(queue)), key=lambda i: queue[i])
        peak_row = rows[peak_i] if peak_i < len(rows) else {}
        print("## 脚本自动指出的三处（剩下的归因要人写）")
        print()
        print(f"1. 队列峰值出现在第 {peak_row.get('序号','?')} 组（{peak_row.get('时点','?')}），"
              f"{queue[peak_i]:g} 人；该窗口的到达 {mean(arrive):.1f} 人/{interval:g}min，"
              f"完成 {mean(served):.1f} 人/{interval:g}min。")
        if rho >= 1:
            print(f"2. ρ = {rho:.2f} ≥ 1：现有 {counters:g} 个窗口的极限服务能力"
                  f"（{counters * mu:.1f} 人/分钟）低于到达率（{lam:.2f} 人/分钟），"
                  f"队列在高峰段必然单调变长——这是供给侧的瓶颈，不是排队秩序问题。")
        else:
            print(f"2. ρ = {rho:.2f} < 1：窗口理论产能"
                  f"（{counters * mu:.1f} 人/分钟）高于到达率（{lam:.2f} 人/分钟），"
                  f"队列理论上能被打平。此时若实测队列仍在变长，瓶颈更可能在"
                  f"**瞬时聚集**（某几分钟集中涌入）或**单笔耗时波动**（有人点了十串）。")
        net = (lam - counters * mu) * interval
        if net > 0:
            print(f"3. 队伍每 {interval:g} 分钟净增 {net:+.1f} 人，30 分钟后预计到 "
                  f"{start_q + net * (30 / interval):.0f} 人量级；和实测峰值对比，"
                  f"能验证“均匀到达”这个假设是否成立。")
        else:
            print(f"3. 队伍每 {interval:g} 分钟净减 {abs(net):.1f} 人（窗口在消化积压）。"
                  f"若 30 分钟内队伍没被消化下去，说明到达不是均匀的而是成簇来的——"
                  f"这正是“明明开了足够的窗口却还是要排”的常见成因。")
        if rho < 1 and (sum(arrive) - sum(served)) > 0:
            print()
            print("⚠ 一致性提醒：ρ < 1 表示产能够，但累计净积压仍为正。"
                  "这两件事同时成立，通常意味着**抽测的单次服务耗时偏短**"
                  "（你挑的是点得快的），或**有窗口中途停开**没记进备注。"
                  "面试很可能会追这一点，报告里要么补测、要么主动写清。")
        print()
        print("已知局限（要写进 report）：单点单次、无重复试验；到达按 2 分钟聚合，"
              "看不到 10 秒级的瞬时爆发；服务耗时只抽测，不是全量。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

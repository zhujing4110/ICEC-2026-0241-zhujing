#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
课题10 · 日常数字任务耗时审计 —— 批处理基准脚本

在**受控样本**上复现三个日常数字任务，测量脚本侧耗时（秒级精度，重复取中位数）。
手工侧耗时无法由程序代测，需本人实测，方法见 01-Field-Discovery/README.md。

三个任务：
  A  批量重命名      N 个命名混乱的截图文件 → 统一"日期_序号"格式
  B  日志报错统计    从 439 行真实运行日志中提取报错行并按类型计数
  C  仓库规模清单    统计全部源码/文档的行数与字节数，生成清单

用法：
    python tools/task10_benchmark.py
    python tools/task10_benchmark.py --n 100 --repeat 3
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import shutil
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

# Windows 控制台默认 GBK，强制 UTF-8 避免中文报错
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

FIELD_DIR = Path(__file__).resolve().parents[1]          # 01-Field-Discovery
REPO_ROOT = Path(__file__).resolve().parents[2]          # 仓库根
LOG_FILE = REPO_ROOT / "02-20Days-Challenge" / "process_logs" / "runtime-2026-11-21.log"
SANDBOX = FIELD_DIR / ".bench"
OUT_JSON = FIELD_DIR / "raw_data" / "task10-benchmark-result.json"

# 跳过的目录，避免统计时把虚拟环境/依赖算进去
SKIP_DIRS = {".git", ".venv", ".bench", "__pycache__", "node_modules"}


# ---------------------------------------------------------------- 任务 A
def _make_messy_files(folder: Path, n: int) -> None:
    """造一批命名混乱的截图文件，模拟日常文件夹真实状态。"""
    folder.mkdir(parents=True, exist_ok=True)
    kinds = ["微信图片", "Screenshot", "IMG", "屏幕截图", "QQ图片"]
    for i in range(n):
        kind = random.choice(kinds)
        stamp = f"202611{random.randint(10, 23):02d}{random.randint(0, 23):02d}{random.randint(0, 59):02d}"
        suffix = random.choice(["", f"({random.randint(1, 9)})", f"_{random.randint(10, 999)}"])
        (folder / f"{kind}_{stamp}{suffix}.png").write_bytes(b"x" * 16)


def task_a_rename(n: int) -> dict:
    """批量重命名：乱名 → 20261123_001.png 形式。"""
    folder = SANDBOX / "rename"
    if folder.exists():
        shutil.rmtree(folder)
    _make_messy_files(folder, n)

    t0 = time.perf_counter()
    files = sorted(folder.iterdir(), key=lambda p: p.name)
    for idx, f in enumerate(files, 1):
        f.rename(folder / f"20261123_{idx:03d}.png")
    elapsed = time.perf_counter() - t0

    done = len(list(folder.iterdir()))
    return {"task": "A 批量重命名", "n": n, "seconds": elapsed, "verified": done == n}


# ---------------------------------------------------------------- 任务 B
def task_b_log_scan() -> dict:
    """从真实运行日志中提取报错行并分类计数。"""
    if not LOG_FILE.exists():
        return {"task": "B 日志报错统计", "n": 0, "seconds": 0.0, "verified": False,
                "note": f"日志不存在: {LOG_FILE}"}

    t0 = time.perf_counter()
    total = 0
    buckets: Counter[str] = Counter()
    for line in LOG_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
        total += 1
        up = line.upper()
        if "ERROR" in up:
            buckets["ERROR"] += 1
        elif "WARNING" in up or "WARN" in up:
            buckets["WARN"] += 1
        if "超时" in line or "timeout" in up:
            buckets["超时"] += 1
        if "COM" in up or "订户" in line:
            buckets["COM 错误"] += 1
        if "已回复" in line:
            buckets["成功回复"] += 1
        if "收到" in line or "Processing request" in line:
            buckets["收到消息"] += 1
    elapsed = time.perf_counter() - t0

    return {"task": "B 日志报错统计", "n": total, "seconds": elapsed,
            "verified": total > 0, "detail": dict(buckets)}


# ---------------------------------------------------------------- 任务 C
def task_c_inventory() -> dict:
    """统计仓库内全部文件的规模，生成清单。"""
    t0 = time.perf_counter()
    rows = []
    for p in REPO_ROOT.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.relative_to(REPO_ROOT).parts):
            continue
        data = p.read_bytes()
        lines = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
        rows.append((str(p.relative_to(REPO_ROOT)), len(data), lines))
    elapsed = time.perf_counter() - t0

    out = FIELD_DIR / "raw_data" / "task10-repo-inventory.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["文件", "字节数", "行数"])
        w.writerows(sorted(rows, key=lambda r: -r[1]))

    return {"task": "C 仓库规模清单", "n": len(rows), "seconds": elapsed,
            "verified": len(rows) > 0, "output": str(out.relative_to(REPO_ROOT))}


# ---------------------------------------------------------------- 主流程
def run(n: int, repeat: int) -> None:
    SANDBOX.mkdir(parents=True, exist_ok=True)
    runners = [lambda: task_a_rename(n), task_b_log_scan, task_c_inventory]
    results = []

    for fn in runners:
        times = []
        last = None
        for _ in range(max(1, repeat)):
            last = fn()
            times.append(last["seconds"])
        last = dict(last)
        last["seconds_median"] = round(statistics.median(times), 4)
        last["seconds_min"] = round(min(times), 4)
        last["repeat"] = len(times)
        last["seconds"] = round(last["seconds"], 4)
        results.append(last)

    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "sample_size": n,
        "results": results,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{'任务':<16}{'规模':>6}{'中位耗时(s)':>14}{'最快(s)':>12}  校验")
    print("-" * 60)
    for r in results:
        print(f"{r['task']:<16}{r['n']:>6}{r['seconds_median']:>14.3f}{r['seconds_min']:>12.3f}  "
              f"{'OK' if r.get('verified') else 'FAIL'}")
    print(f"\n结果已写入: {OUT_JSON.relative_to(REPO_ROOT)}")

    # 清理沙箱，不把测试文件留在仓库里
    if SANDBOX.exists():
        shutil.rmtree(SANDBOX)


def main() -> int:
    ap = argparse.ArgumentParser(description="课题10 批处理基准")
    ap.add_argument("--n", type=int, default=100, help="任务A 的文件数量（默认 100）")
    ap.add_argument("--repeat", type=int, default=3, help="每任务重复次数，取中位数（默认 3）")
    args = ap.parse_args()
    run(args.n, args.repeat)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

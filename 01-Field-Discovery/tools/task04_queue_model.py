#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
课题04 · 校园高峰排队吞吐率建模 —— 可复现计算脚本

用法：
    python tools/task04_queue_model.py          # 生成 raw_data/task04-*.csv 并打印校验摘要

模型：
    食堂   M/M/c 稳态解（Erlang-C 闭式解，对数域求和避免溢出）
    电梯   批量服务近似（运力 = m·K·60/RTT）+ 人级离散事件仿真
    验证   固定随机种子的蒙特卡洛仿真，批均值法给 95% 置信区间

重要口径：本脚本中所有输入参数（λ、μ、RTT 等）均为**教学示例值**，
不是现场计数结果。脚本能复现的只是"给定这些参数，模型算出什么"，
不能证明这些参数符合某一所学校的真实高峰客流。
真实标定方法：每 5 分钟一档连续计数 3 天，取峰值时段均值。
"""
import csv
import math
import os
import random

SEED = 20261124          # 固定种子，保证仿真可完全复现
RAW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "raw_data")


# ---------------------------------------------------------------- 解析解
def erlang_c(lam, mu, c):
    """M/M/c 稳态解。rho >= 1 直接判不稳定（队列期望发散）。"""
    a = lam / mu
    rho = a / c
    if rho >= 1:
        return {"rho": rho, "stable": False, "cap": c * mu,
                "Pwait": None, "Lq": None, "Wq": None}
    # 对数域：a^n/n! 用 n·ln a − lnΓ(n+1)，避免 c 较大时溢出
    terms = [n * math.log(a) - math.lgamma(n + 1) for n in range(c + 1)]
    mx = max(terms[:c])
    log_sum = mx + math.log(sum(math.exp(t - mx) for t in terms[:c]))
    log_last = terms[c] - math.log(1 - rho)
    m2 = max(log_sum, log_last)
    p_wait = math.exp(log_last - m2) / (math.exp(log_sum - m2) + math.exp(log_last - m2))
    lq = p_wait * rho / (1 - rho)
    wq = lq / lam                       # 分钟
    return {"rho": rho, "stable": True, "cap": c * mu, "Pwait": p_wait,
            "Lq": lq, "Wq": wq, "W": wq + 1 / mu, "L": lq + a}


def sim_mmc(lam, mu, c, n=60000, warm=20000, batches=20, seed=SEED):
    """单次长运行 + 批均值法。返回 (Wq 秒, CI 下限, CI 上限)。

    rho 接近 1 时等待序列自相关极强，短运行会因「从空系统启动」系统性低估，
    故先预热 warm 人丢弃瞬态，再均分 batches 批估计方差。
    """
    rnd = random.Random(seed)
    free = [0.0] * c
    t = 0.0
    per = n // batches
    means, acc, cnt = [], 0.0, 0
    for i in range(n + warm):
        t += rnd.expovariate(lam)
        j = min(range(c), key=lambda k: free[k])
        start = max(t, free[j])
        free[j] = start + rnd.expovariate(mu)
        if i >= warm:
            acc += start - t
            cnt += 1
            if cnt == per:
                means.append(acc / cnt * 60)
                acc, cnt = 0.0, 0
    mean = sum(means) / len(means)
    sd = (sum((b - mean) ** 2 for b in means) / (len(means) - 1)) ** 0.5
    se = sd / len(means) ** 0.5
    return mean, mean - 1.96 * se, mean + 1.96 * se


# ---------------------------------------------------------------- 电梯
def rtt(H, tf, S, ts, K, tp):
    """往返时间：2(H−1)·tf 行程 + (S+1)·ts 停站 + 2K·tp 上下客。"""
    return 2 * (H - 1) * tf + (S + 1) * ts + 2 * K * tp


def elevator_cap(m, K, R):
    return m * K * 60.0 / R


def sim_elevator(lam, m, K, R, minutes=30.0, seed=SEED):
    """人级事件驱动仿真：返回 (末队列人数, 平均候梯秒, 已服务人数)。

    电梯 j 首班时刻错开 R/m，之后每班间隔 R·U(0.9,1.1)，每班装 min(K, 队列)。
    """
    rnd = random.Random(seed)
    arrivals, t = [], 0.0
    horizon = minutes * 60.0             # 全程统一用秒
    while True:
        t += rnd.expovariate(lam) * 60.0   # λ 是人/分钟，间隔换算成秒
        if t > horizon:
            break
        arrivals.append(t)

    departures = []                      # (时刻, 电梯号)；RTT 已是秒，全程用秒
    for j in range(m):
        dep = R * (j + 1) / m
        while dep <= horizon:
            departures.append((dep, j))
            dep += R * rnd.uniform(0.9, 1.1)
    departures.sort()

    queue, waits, idx = [], [], 0
    for dep, _j in departures:
        while idx < len(arrivals) and arrivals[idx] <= dep:
            queue.append(arrivals[idx])
            idx += 1
        load, queue = queue[:K], queue[K:]
        waits.extend(dep - a for a in load)
    queue.extend(arrivals[idx:])
    mean_wait = sum(waits) / len(waits) if waits else 0.0
    return len(queue), mean_wait, len(waits)


def sim_elevator_avg(lam, m, K, R, reps=20, minutes=30.0):
    """单次 30 分钟仿真的到达人数波动大（泊松 sd≈√900=30 人），
    故用 20 组不同种子重复，取均值与极差。"""
    qs, ws = [], []
    for i in range(reps):
        q, w, _ = sim_elevator(lam, m, K, R, minutes=minutes, seed=SEED + i * 7919)
        qs.append(q)
        ws.append(w)
    return (sum(qs) / len(qs), min(qs), max(qs),
            sum(ws) / len(ws), min(ws), max(ws))


# ---------------------------------------------------------------- 基准参数
FOOD = dict(lam=40.0, mu=3.6, c=12)                  # 人/min, 人/min·窗, 窗
ELEV = dict(lam=30.0, m=4, K=13, H=12, tf=2.0, S=5, ts=7.0, tp=1.0)
BASE_RTT = rtt(ELEV["H"], ELEV["tf"], ELEV["S"], ELEV["ts"], ELEV["K"], ELEV["tp"])
BASE_CAP = elevator_cap(ELEV["m"], ELEV["K"], BASE_RTT)


def pct(new, old):
    """降幅百分比；old 无效时返回空串。"""
    if not old or old <= 0:
        return ""
    return round((old - new) / old * 100, 1)


def write_csv(name, header, rows):
    path = os.path.join(RAW, name)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print(f"  写入 {name}（{len(rows)} 行）")
    return path


def main():
    print("课题04 排队模型复算")
    print(f"  食堂基准：λ={FOOD['lam']} c={FOOD['c']} μ={FOOD['mu']}")
    base = erlang_c(**FOOD)
    print(f"    解析 ρ={base['rho']:.3f}  Wq={base['Wq']*60:.2f}s  Lq={base['Lq']:.2f}人")
    sim_wq, lo, hi = sim_mmc(**FOOD)
    ok = lo <= base["Wq"] * 60 <= hi
    print(f"    仿真 Wq={sim_wq:.2f}s  95%CI=[{lo:.2f},{hi:.2f}]  解析落入CI={ok}")
    print(f"  电梯基准：m={ELEV['m']} K={ELEV['K']} RTT={BASE_RTT:.0f}s  "
          f"运力={BASE_CAP:.2f}人/min  ρ={ELEV['lam']/BASE_CAP:.3f}")

    # ---- 1. 参数表（全部标注为教学示例值）
    params = [
        ["食堂", "λ_food", "到达率", FOOD["lam"], "人/分钟", "教学示例值（下课高峰 40 人/分）"],
        ["食堂", "μ_food", "单窗口服务率", FOOD["mu"], "人/分钟", "教学示例值（单窗 16.7 s/人）"],
        ["食堂", "c_food", "服务窗口数", FOOD["c"], "个", "教学示例值"],
        ["电梯", "λ_elev", "到达率（上行高峰）", ELEV["lam"], "人/分钟", "教学示例值"],
        ["电梯", "m", "电梯部数", ELEV["m"], "部", "教学示例值"],
        ["电梯", "K", "单梯载客量", ELEV["K"], "人", "教学示例值（13 人/梯）"],
        ["电梯", "H", "楼层数", ELEV["H"], "层", "教学示例值"],
        ["电梯", "tf", "单层运行时间", ELEV["tf"], "秒", "教学示例值"],
        ["电梯", "S", "期望停靠层数", ELEV["S"], "层", "教学示例值（可由 H、K 推导）"],
        ["电梯", "ts", "单次停靠时间", ELEV["ts"], "秒", "教学示例值"],
        ["电梯", "tp", "单人上下客时间", ELEV["tp"], "秒", "教学示例值"],
        ["电梯", "RTT", "往返时间（计算值）", BASE_RTT, "秒", "= 2(H−1)tf + (S+1)ts + 2K·tp"],
        ["仿真", "seed", "随机种子", SEED, "—", "固定种子，结果可完全复现"],
        ["仿真", "n_food", "食堂仿真样本（预热后）", 60000, "人", "预热 20000 人丢弃瞬态，均分 20 批"],
        ["仿真", "n_elev", "电梯仿真时长", 30, "分钟", "覆盖一个完整早高峰"],
    ]
    write_csv("task04-model-params.csv",
              ["系统", "符号", "含义", "取值", "单位", "来源/说明"], params)

    # ---- 2. 解析 vs 仿真
    q0, q0_min, q0_max, w0, w0_min, w0_max = sim_elevator_avg(
        ELEV["lam"], ELEV["m"], ELEV["K"], BASE_RTT)
    ana_backlog = max(0.0, (ELEV["lam"] - BASE_CAP) * 30)
    rows = [
        ["食堂", "平均排队时间 Wq", f"{base['Wq']*60:.2f}", f"{sim_wq:.2f}",
         f"{lo:.2f}", f"{hi:.2f}", f"{(sim_wq-base['Wq']*60)/(base['Wq']*60)*100:+.1f}",
         "解析落入 95% CI" if ok else "未落入"],
        ["食堂", "平均排队人数 Lq", f"{base['Lq']:.2f}", "—", "—", "—", "",
         "由 Wq 经 Little 定律反推，未单列仿真值"],
        ["电梯", "30 分钟末队列（人）", f"{ana_backlog:.0f}", f"{q0:.0f}",
         f"{q0_min:.0f}", f"{q0_max:.0f}", f"{(q0-ana_backlog)/ana_backlog*100:+.1f}",
         "解析为净积压线性外推；仿真为 20 组均值，CI 列为极小/极大值"],
        ["电梯", "平均候梯（秒）", "—", f"{w0:.1f}", f"{w0_min:.1f}", f"{w0_max:.1f}", "",
         "ρ>1 无稳态解，仅仿真值（20 组均值，CI 列为极值）"],
    ]
    write_csv("task04-analytic-vs-sim.csv",
              ["场景", "指标", "解析解", "仿真值", "CI下限/极小", "CI上限/极大", "点偏差(%)", "说明"], rows)

    # ---- 3. 食堂五方案
    canteen = [
        ("1 基准（现状）", 40, 12, 3.6, "不投入"),
        ("2 削峰：错峰下课 15%", 34, 12, 3.6, "无硬件成本，需教务协调"),
        ("3 增开 2 个窗口", 40, 14, 3.6, "2 台刷卡机 + 2 人力"),
        ("4 提速：单窗 16.7→14.3 s", 40, 12, 60 / 14.3, "预打包、选菜与结算分离"),
        ("5 组合：削峰 + 增 1 窗 + 微提速", 34, 13, 3.9, "三项各出一半"),
    ]
    base_wq = base["Wq"] * 60
    rows = []
    for name, lam, c, mu, cost in canteen:
        r = erlang_c(lam, mu, c)
        rows.append([
            name, lam, c, f"{mu:.2f}", f"{r['rho']:.3f}",
            f"{r['Wq']*60:.2f}" if r["stable"] else "不稳定",
            f"{r['Lq']:.2f}" if r["stable"] else "发散",
            f"{pct(r['Wq']*60, base_wq):.1f}" if r["stable"] else "",
            cost,
        ])
    write_csv("task04-canteen-scenarios.csv",
              ["方案", "λ(人/分)", "c(窗口)", "μ(人/分·窗)", "ρ",
               "Wq(秒)", "Lq(人)", "相对基准降幅(%)", "成本/可行性"], rows)

    # ---- 4. 电梯五方案
    elev = [
        ("1 基准（现状）", 30, 4, 5, "不投入"),
        ("2 加 1 部梯", 30, 5, 5, "硬件投入最大"),
        ("3 目的层群控（S 5→3）", 30, 4, 3, "零硬件，需改造控制系统"),
        ("4 组合：加 1 梯 + 群控", 30, 5, 3, "硬件 + 控制改造"),
        ("5 削峰：错峰上课 17%", 25, 4, 5, "无硬件成本，需教务协调"),
    ]
    rows = []
    for name, lam, m, s, cost in elev:
        R = rtt(ELEV["H"], ELEV["tf"], s, ELEV["ts"], ELEV["K"], ELEV["tp"])
        cap = elevator_cap(m, ELEV["K"], R)
        rho = lam / cap
        backlog5 = max(0.0, (lam - cap) * 5)
        q, q_min, q_max, w, w_min, w_max = sim_elevator_avg(lam, m, ELEV["K"], R)
        rows.append([
            name, lam, m, s, f"{R:.0f}", f"{cap:.2f}", f"{rho:.3f}",
            f"{backlog5:.1f}", f"{q:.0f} [{q_min:.0f}–{q_max:.0f}]",
            f"{w:.0f}", cost,
        ])
    write_csv("task04-elevator-scenarios.csv",
              ["方案", "λ(人/分)", "m(部)", "S(停靠层)", "RTT(秒)", "运力C(人/分)",
               "ρ", "5min净积压(人)", "30min末队列(20组均值[极差])", "平均候梯(秒·均值)",
               "成本/可行性"], rows)

    # ---- 5. 灵敏度扫描（做图用）
    rows = []
    for lam in range(20, 52, 2):
        r = erlang_c(lam, FOOD["mu"], FOOD["c"])
        rows.append([lam, f"{r['rho']:.3f}",
                     f"{r['Wq']*60:.2f}" if r["stable"] else "",
                     f"{r['Lq']:.2f}" if r["stable"] else "",
                     "稳定" if r["stable"] else "不稳定（ρ≥1）"])
    write_csv("task04-sensitivity-lambda.csv",
              ["λ(人/分)", "ρ", "Wq(秒)", "Lq(人)", "状态"], rows)

    rows = []
    for c in range(10, 19):
        r = erlang_c(FOOD["lam"], FOOD["mu"], c)
        rows.append([c, f"{r['rho']:.3f}",
                     f"{r['Wq']*60:.2f}" if r["stable"] else "",
                     "稳定" if r["stable"] else "不稳定（ρ≥1）"])
    write_csv("task04-sensitivity-windows.csv",
              ["c(窗口)", "ρ", "Wq(秒)", "状态"], rows)

    print("\n校验要点：")
    print(f"  · 食堂基准 Wq = {base['Wq']*60:.1f} s（报告结论 1 引用的量级）")
    r34 = erlang_c(34, 3.6, 12)
    print(f"  · 削峰至 λ=34 后 Wq = {r34['Wq']*60:.1f} s")
    print(f"  · 电梯 RTT = {BASE_RTT:.0f} s，运力 = {BASE_CAP:.1f} 人/min，ρ = {ELEV['lam']/BASE_CAP:.3f}")
    print(f"  · 群控 S=3 后 RTT = {rtt(12,2.0,3,7,13,1.0):.0f} s，"
          f"运力提升 {BASE_RTT/rtt(12,2.0,3,7,13,1.0)*100-100:.1f}%")


if __name__ == "__main__":
    main()

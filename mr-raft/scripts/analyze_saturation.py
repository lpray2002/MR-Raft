#!/usr/bin/env python3
"""量化 perf-aware penalty 在高压下的饱和/压扁程度，以及各 region 选举时刻 rho 排序。

目的：用真实 metrics 数据回答「N2 给了高超时，但相对其他节点会不会还是低的 → 最后还是当选」。
关键看两件事：
  1. 选举时刻（leader_count 跳变前后）各候选节点的 rho/penalty 排序，min 是否落在强节点。
  2. 高压稳态下，同一 region 候选节点之间 penalty 的极差 Δpenalty，
     换算成 ticks (penaltyScale*Δp) 是否仍 > tie-break(0~2)。若 <2 则排序被随机淹没。
"""
import json
import math
import glob
import os
from collections import defaultdict

ROOT = os.path.join(os.path.dirname(__file__), "..", "output", "priority_election", "configA")

# 参数（与 election.go / 实验一致）
ALPHA = 0.3
KAPPA = 5.0
GAMMA = 2.0
PENALTY_SCALE = 20
ELECTION_RTT = 10

# region 成员
REGIONS = {1: [0, 1, 2], 2: [1, 2, 3], 3: [2, 3, 4]}
# 静态性能缺额 s：0.75 核 -> 0.5，1.5 核 -> 0
STATIC = {0: 0.5, 1: 0.0, 2: 0.5, 3: 0.0, 4: 0.5}


def penalty_from_rho(rho):
    return 1 - math.pow(1 / (1 + ALPHA), rho)


def rho_perf(L, load, s):
    return L + KAPPA * load + GAMMA * s


def load_metrics(strategy, run):
    """返回 {node_id: [rows]}，rows 按 ts 排序。"""
    d = os.path.join(ROOT, f"comp_{strategy}", f"run_{run}")
    out = {}
    for nid in range(5):
        path = os.path.join(d, f"node_{nid}_metrics.jsonl")
        if not os.path.exists(path):
            continue
        rows = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        rows.sort(key=lambda r: r["ts"])
        out[nid] = rows
    return out


def steady_avg(rows, frac=0.5):
    """取后 frac 段（稳态）的平均 load。"""
    if not rows:
        return 0.0
    tail = rows[int(len(rows) * (1 - frac)):]
    return sum(r["load"] for r in tail) / len(tail)


def analyze_steady_resolution(strategy):
    """稳态下，每个 region 内候选节点 perf-aware penalty 的极差，换算成 ticks。"""
    print(f"\n{'='*70}\n策略={strategy}：稳态 region 内 penalty 分辨率\n{'='*70}")
    for run in (1, 2, 3):
        m = load_metrics(strategy, run)
        if not m:
            continue
        # 稳态平均 load + 末尾 leader_count
        avg_load = {nid: steady_avg(rows) for nid, rows in m.items()}
        last_L = {nid: (rows[-1]["leader_count"] if rows else 0) for nid, rows in m.items()}
        print(f"\n run_{run}:")
        for rid, members in REGIONS.items():
            rhos = {}
            for nid in members:
                L = last_L.get(nid, 0)
                load = avg_load.get(nid, 0.0)
                s = STATIC[nid]
                rho = rho_perf(L, load, s)
                rhos[nid] = (rho, penalty_from_rho(rho))
            ps = {nid: p for nid, (_, p) in rhos.items()}
            pmin = min(ps.values())
            pmax = max(ps.values())
            dticks = PENALTY_SCALE * (pmax - pmin)
            winner_rho = min(members, key=lambda n: rhos[n][0])
            detail = "  ".join(
                f"N{n}(L={last_L.get(n,0)},load={avg_load.get(n,0):.2f},p={ps[n]:.3f})"
                for n in members
            )
            flag = "  <-- Δticks<2 排序被tie-break淹没!" if dticks < 2 else ""
            print(f"  R{rid}: {detail}")
            print(f"       argmin(rho)=N{winner_rho}  Δpenalty={pmax-pmin:.3f} -> Δticks={dticks:.2f}{flag}")


if __name__ == "__main__":
    for strat in ("perf_aware", "leader_count"):
        analyze_steady_resolution(strat)

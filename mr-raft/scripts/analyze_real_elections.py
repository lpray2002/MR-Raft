#!/usr/bin/env python3
"""用真实采集数据分析选举时刻的区分度。

数据：output/priority_election/configA/comp_perf_aware/run_{1,2,3}/node_{0..4}_metrics.jsonl
每行每秒采集一次，包含 load/cpu/leader_count/leader_of/penalty 等。

步骤：
1. 读取 5 个节点的 metrics，按 ts 对齐
2. 定位各 region 选举完成时刻（leader_of 首次包含该 region）
3. 取选举时刻前一秒（选举发生时）各候选的真实 L/load/cpu
4. 用不同公式计算 ρ/penalty/超时，比较区分度
"""
import json
import os
import math
from collections import defaultdict

ROOT = os.path.join(os.path.dirname(__file__), "..",
                    "output", "priority_election", "configA", "comp_perf_aware")

REGIONS = {1: [0, 1, 2], 2: [1, 2, 3], 3: [2, 3, 4]}
STATIC = {0: 0.5, 1: 0.0, 2: 0.5, 3: 0.0, 4: 0.5}

ALPHA = 0.3
KAPPA = 5.0
GAMMA = 2.0


def load_node_metrics(run_dir, nid):
    path = os.path.join(run_dir, f"node_{nid}_metrics.jsonl")
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    rows.sort(key=lambda r: r["ts"])
    return rows


def find_election_moments(all_metrics):
    """找到各 region 首次出现 leader 的时刻（选举完成）。
    返回 {region_id: (ts, winner_node)}
    """
    elections = {}
    for nid, rows in all_metrics.items():
        seen_regions = set()
        for r in rows:
            for rid in r.get("leader_of", []):
                if rid not in seen_regions:
                    seen_regions.add(rid)
                    if rid not in elections or r["ts"] < elections[rid][0]:
                        elections[rid] = (r["ts"], nid)
    return elections


def get_snapshot_at(all_metrics, ts, tolerance=2.0):
    """取各节点在 ts 附近（±tolerance 秒）最近的快照。"""
    snap = {}
    for nid, rows in all_metrics.items():
        best = None
        for r in rows:
            if abs(r["ts"] - ts) < tolerance:
                if best is None or abs(r["ts"] - ts) < abs(best["ts"] - ts):
                    best = r
        snap[nid] = best
    return snap


def penalty_fn(rho, alpha=ALPHA):
    return 1 - math.pow(1 / (1 + alpha), rho)


def analyze_run(run_id):
    run_dir = os.path.join(ROOT, f"run_{run_id}")
    print(f"\n{'#'*70}\n# Run {run_id}: 真实采集数据\n{'#'*70}")

    all_metrics = {}
    for nid in range(5):
        all_metrics[nid] = load_node_metrics(run_dir, nid)

    elections = find_election_moments(all_metrics)
    print(f"  选举时刻: {', '.join(f'R{rid}@t={ts:.1f}→N{win}' for rid,(ts,win) in sorted(elections.items()))}")

    for rid in sorted(elections.keys()):
        ts_elect, actual_winner = elections[rid]
        members = REGIONS[rid]

        # 取选举前 1~2 秒的快照（选举决策发生在选举完成前）
        snap = get_snapshot_at(all_metrics, ts_elect - 1.0)

        print(f"\n  ── R{rid} 选举时刻 (t≈{ts_elect:.1f}, 实际赢家=N{actual_winner}) ──")
        print(f"  {'节点':<5}{'L':<4}{'load':<8}{'cpu':<8}{'s':<5}"
              f"{'ρ(当前)':<10}{'pen':<8}{'超时ms':<10}"
              f"{'ρ(去L)':<10}{'超时(去L)':<10}")

        for n in members:
            s = snap.get(n)
            if s is None:
                print(f"  N{n:<4} 数据缺失")
                continue
            L = s["leader_count"]
            load = s["load"]
            cpu = s["cpu_pct"]
            st = STATIC[n]

            # 当前公式
            rho_cur = L + KAPPA * load + GAMMA * st
            pen_cur = penalty_fn(rho_cur)
            to_cur = (10 + 20 * pen_cur) * 100

            # 去掉 L
            rho_noL = KAPPA * load + GAMMA * st
            pen_noL = penalty_fn(rho_noL)
            to_noL = (10 + 20 * pen_noL) * 100

            print(f"  N{n:<4}{L:<4}{load:<8.3f}{cpu:<8.3f}{st:<5.1f}"
                  f"{rho_cur:<10.2f}{pen_cur:<8.3f}{to_cur:<10.0f}"
                  f"{rho_noL:<10.2f}{to_noL:<10.0f}")

        # 区分度
        snap_valid = {n: snap[n] for n in members if snap.get(n)}
        if len(snap_valid) == len(members):
            pens_cur = []
            pens_noL = []
            for n in members:
                s = snap[n]
                L = s["leader_count"]; load = s["load"]; st = STATIC[n]
                pens_cur.append(penalty_fn(L + KAPPA * load + GAMMA * st))
                pens_noL.append(penalty_fn(KAPPA * load + GAMMA * st))
            dp_cur = (max(pens_cur) - min(pens_cur)) * 2000
            dp_noL = (max(pens_noL) - min(pens_noL)) * 2000
            print(f"  区分度(当前公式): {dp_cur:.0f}ms   区分度(去L): {dp_noL:.0f}ms")


if __name__ == "__main__":
    for run in (1, 2, 3):
        analyze_run(run)

#!/usr/bin/env python3
"""统计 perf_aware 下各节点 cpu/net/mem/load 的分布，看是哪个分量决定了 load 的动态范围。

回答：load 区分度差，到底是公式形式问题，还是分量信号失衡（net/mem 太小被浪费）。
"""
import json
import os
import statistics

ROOT = os.path.join(os.path.dirname(__file__), "..", "output", "priority_election", "configA")
STATIC = {0: 0.5, 1: 0.0, 2: 0.5, 3: 0.0, 4: 0.5}


def load_rows(strategy, run, nid):
    path = os.path.join(ROOT, f"comp_{strategy}", f"run_{run}", f"node_{nid}_metrics.jsonl")
    rows = []
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return rows


def pct(vals, p):
    if not vals:
        return 0.0
    s = sorted(vals)
    k = int(len(s) * p)
    return s[min(k, len(s) - 1)]


def summarize(strategy):
    print(f"\n{'='*78}\n策略={strategy}：各分量分布（合并 3 run 稳态后半段）\n{'='*78}")
    print(f"{'节点':<6}{'核数':<6}{'cpu(med/max)':<20}{'net(med/max)':<22}{'mem(med/max)':<20}{'load(med/max)':<18}")
    for nid in range(5):
        cpu, net, mem, load = [], [], [], []
        for run in (1, 2, 3):
            rows = load_rows(strategy, run, nid)
            tail = rows[len(rows)//2:]  # 稳态后半段
            for r in tail:
                cpu.append(r["cpu_pct"]); net.append(r["net_util"])
                mem.append(r["mem_pct"]); load.append(r["load"])
        cores = "0.75" if STATIC[nid] == 0.5 else "1.5"
        print(f"N{nid:<5}{cores:<6}"
              f"{pct(cpu,.5):.3f}/{max(cpu) if cpu else 0:.3f}      "
              f"{pct(net,.5):.4f}/{max(net) if net else 0:.4f}        "
              f"{pct(mem,.5):.3f}/{max(mem) if mem else 0:.3f}      "
              f"{pct(load,.5):.3f}/{max(load) if load else 0:.3f}")

    # 全体 load 跨节点的中位数极差，衡量「区分度」
    med_by_node = {}
    for nid in range(5):
        load = []
        for run in (1, 2, 3):
            rows = load_rows(strategy, run, nid)
            tail = rows[len(rows)//2:]
            load += [r["load"] for r in tail]
        med_by_node[nid] = statistics.median(load) if load else 0
    spread = max(med_by_node.values()) - min(med_by_node.values())
    print(f"  跨节点 load 中位数极差(区分度) = {spread:.3f}   "
          f"(强={[f'N{n}:{med_by_node[n]:.2f}' for n in (1,3)]}, 弱={[f'N{n}:{med_by_node[n]:.2f}' for n in (0,2,4)]})")


if __name__ == "__main__":
    for strat in ("perf_aware", "leader_count", "random"):
        summarize(strat)

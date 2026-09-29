#!/usr/bin/env python3
"""删掉 L 项后的公式验证。

候选：ρ = κ·load + γ·s   (只有实时负载+静态算力)
对比：看排序对不对、区分度够不够。
"""
import math

ALPHA = 0.5
REGIONS = {1: [0, 1, 2], 2: [1, 2, 3], 3: [2, 3, 4]}
STATIC = {0: 0.5, 1: 0.0, 2: 0.5, 3: 0.0, 4: 0.5}
LOAD = {0: 0.48, 1: 0.40, 2: 0.49, 3: 0.34, 4: 0.36}
LCUR = {0: 0, 1: 1, 2: 0, 3: 0, 4: 1}


def pen(rho, alpha=ALPHA):
    return 1 - math.pow(1 / (1 + alpha), rho)


def run(name, kappa, gamma, alpha=ALPHA):
    print(f"\n{'='*64}\n{name}\n  ρ = {kappa}·load + {gamma}·s   α={alpha}   (无 L 项)\n{'='*64}")
    for rid, members in REGIONS.items():
        rhos = {}
        for n in members:
            r = kappa * LOAD[n] + gamma * STATIC[n]
            rhos[n] = r
        pens = {n: pen(rhos[n], alpha) for n in members}
        dp = max(pens.values()) - min(pens.values())
        win = min(members, key=lambda n: rhos[n])
        det = "  ".join(f"N{n}(ρ={rhos[n]:.2f},p={pens[n]:.3f})" for n in members)
        print(f"  R{rid}: {det}")
        print(f"        Δpenalty={dp:.4f} 赢家=N{win} (span1000→{dp*1000:.0f}ms)")


def run_election_seq(name, kappa, gamma, alpha=ALPHA):
    """模拟逐 region 启动选举（R1→R2→R3），看最终 leader 分配。"""
    print(f"\n{'─'*64}\n{name} — 逐 region 选举模拟\n{'─'*64}")
    leaders = {}  # rid -> winner
    for rid, members in REGIONS.items():
        rhos = {}
        for n in members:
            r = kappa * LOAD[n] + gamma * STATIC[n]
            rhos[n] = r
        win = min(members, key=lambda n: rhos[n])
        leaders[rid] = win
        print(f"  R{rid}: 赢家=N{win} (ρ={rhos[win]:.2f})")
    # 最终分配
    from collections import Counter
    lc = Counter(leaders.values())
    print(f"  最终 leader 分配: {dict(leaders)}")
    print(f"  各节点 leader 数: {dict(lc)}")
    if lc.get(2, 0) > 0:  # N2 当了 leader
        print(f"  ⚠️ 瓶颈节点 N2 当了 leader!")


if __name__ == "__main__":
    run("去掉L: κ=5, γ=2", 5.0, 2.0)
    run("去掉L: κ=3, γ=2", 3.0, 2.0)
    run("去掉L: κ=3, γ=3", 3.0, 3.0)

    print("\n\n" + "="*64)
    print("逐 region 选举模拟（看最终 leader 分配是否合理）")
    print("="*64)
    run_election_seq("去掉L: κ=5, γ=2", 5.0, 2.0)
    run_election_seq("去掉L: κ=3, γ=3", 3.0, 3.0)

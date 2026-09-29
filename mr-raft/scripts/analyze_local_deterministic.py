#!/usr/bin/env python3
"""验证：用本地已知的确定量(L, R=参与region数, s=算力缺额)主导排序，
实时 load 仅作微调，能否在不跨节点通信下达到足够区分度。

候选公式：ρ = L + κ_R·(R-L) + γ·s + η·load
  R-L = 当前作为 follower 的 region 数（本地已知，零通信）
  load = 实时负载（本地，小权重 η）
全部本地可算，无需 load_ref 跨节点信息。
"""
import math

ALPHA = 0.5
REGIONS = {1: [0, 1, 2], 2: [1, 2, 3], 3: [2, 3, 4]}
# R_j = 参与 region 数
RPART = {0: 1, 1: 2, 2: 3, 3: 2, 4: 1}
STATIC = {0: 0.5, 1: 0.0, 2: 0.5, 3: 0.0, 4: 0.5}
# 真实稳态 load
LOAD = {0: 0.48, 1: 0.40, 2: 0.49, 3: 0.34, 4: 0.36}
# 当前 L（稳态，取真实数据中位）
LCUR = {0: 0, 1: 1, 2: 0, 3: 0, 4: 1}


def pen(rho, alpha=ALPHA):
    return 1 - math.pow(1 / (1 + alpha), rho)


def run(name, kR, gamma, eta, alpha=ALPHA):
    print(f"\n{'='*64}\n{name}\n  ρ = L + {kR}·(R-L) + {gamma}·s + {eta}·load   α={alpha}\n{'='*64}")
    tot = 0
    for rid, members in REGIONS.items():
        rhos = {}
        for n in members:
            L = LCUR[n]
            r = L + kR * (RPART[n] - L) + gamma * STATIC[n] + eta * LOAD[n]
            rhos[n] = r
        pens = {n: pen(rhos[n], alpha) for n in members}
        dp = max(pens.values()) - min(pens.values())
        win = min(members, key=lambda n: rhos[n])
        tot += dp
        det = "  ".join(f"N{n}(ρ={rhos[n]:.2f})" for n in members)
        print(f"  R{rid}: {det}")
        print(f"        ρ_min={min(rhos.values()):.2f} Δpenalty={dp:.4f} "
              f"赢家=N{win} (span1000→{dp*1000:.0f}ms)")
    print(f"  ΣΔpenalty={tot:.4f}")


if __name__ == "__main__":
    # 纯结构+静态，零 load
    run("① 纯确定量(follower权重0.4,静态2,无load)", 0.4, 2.0, 0.0)
    run("② 确定量+小load微调(η=2)", 0.4, 2.0, 2.0)
    run("③ 加重follower权重(0.6)", 0.6, 2.0, 0.0)
    run("④ 对比:纯leader-count(只有L,看失明)", 0.0, 0.0, 0.0)

#!/usr/bin/env python3
"""分解选举时间公式，定位区分度损失在哪一段。

完整公式：T = (ELECTION_RTT + rand(0,2) + PENALTY_SCALE*penalty) * rttMs
区分度 = PENALTY_SCALE * (p_max - p_min) * rttMs （region 内候选之间）

扫不同参数 (alpha, penaltyScale) 看区分度怎么变。
"""
import math

ALPHA = 0.3
KAPPA = 5.0
GAMMA = 2.0
PENALTY_SCALE = 20
RTT_MS = 100

REGIONS = {1: [0, 1, 2], 2: [1, 2, 3], 3: [2, 3, 4]}
STATIC = {0: 0.5, 1: 0.0, 2: 0.5, 3: 0.0, 4: 0.5}


def penalty(rho, alpha=ALPHA):
    return 1 - math.pow(1 / (1 + alpha), rho)


def rho(L, load, s):
    return L + KAPPA * load + GAMMA * s


# 典型稳态场景（来自真实数据中位）：弱节点 load~0.45，强节点 load~0.34
SCENARIO = {
    # node: (L, load, s)
    0: (0, 0.48, 0.5),
    1: (1, 0.40, 0.0),
    2: (0, 0.49, 0.5),
    3: (0, 0.34, 0.0),
    4: (1, 0.36, 0.5),
}


def timeout_ms(L, load, s, alpha, pscale):
    return (10 + pscale * penalty(rho(L, load, s), alpha)) * RTT_MS


def analyze(alpha, pscale):
    print(f"\n{'='*64}\nα={alpha}  penaltyScale={pscale}\n{'='*64}")
    for rid, members in REGIONS.items():
        tos = {}
        for n in members:
            L, load, s = SCENARIO[n]
            tos[n] = timeout_ms(L, load, s, alpha, pscale)
        lo, hi = min(tos.values()), max(tos.values())
        winner = min(members, key=lambda n: tos[n])
        detail = "  ".join(f"N{n}={tos[n]:.0f}ms" for n in members)
        print(f"  R{rid}: {detail}")
        print(f"       区分度(max-min)={hi-lo:.0f}ms  赢家=N{winner}  "
              f"{'⚠️ <300ms tie-break淹没' if hi-lo < 300 else ''}")


if __name__ == "__main__":
    print("当前参数：")
    analyze(0.3, 20)
    print("\n\n候选调参：")
    analyze(0.5, 20)   # 更陡的惩罚曲线
    analyze(0.3, 40)   # 加倍 scale
    analyze(0.5, 40)   # 两者都加

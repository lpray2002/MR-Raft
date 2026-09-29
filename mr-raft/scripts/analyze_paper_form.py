#!/usr/bin/env python3
"""把 perf-aware 公式写成论文形式 T = T_base + (T_max-T_base)*penalty + δ，
量化论文参数 (T_base=150, T_max=300) 下的区分度，并扫 T_max/α/κ。

论文形式： T_j = T_base + (T_max - T_base)*[1 - (1/(1+α))^ρ_j] + δ
区分度(region 内) = (T_max - T_base) * (penalty_max - penalty_min)
"""
import math

KAPPA = 5.0
GAMMA = 2.0
REGIONS = {1: [0, 1, 2], 2: [1, 2, 3], 3: [2, 3, 4]}

# 真实数据稳态中位场景 (L, load, s)
SCEN = {0: (0, 0.48, 0.5), 1: (1, 0.40, 0.0), 2: (0, 0.49, 0.5),
        3: (0, 0.34, 0.0), 4: (1, 0.36, 0.5)}


def penalty(rho, alpha):
    return 1 - math.pow(1 / (1 + alpha), rho)


def rho(L, load, s, kappa):
    return L + kappa * load + GAMMA * s


def disc(t_base, t_max, alpha, kappa, delta=30):
    span = t_max - t_base
    print(f"\n{'='*66}\nT_base={t_base} T_max={t_max} (span={span}) α={alpha} κ={kappa} δ=0~{delta}\n{'='*66}")
    worst = 1e9
    for rid, members in REGIONS.items():
        tos, pens = {}, {}
        for n in members:
            L, load, s = SCEN[n]
            p = penalty(rho(L, load, s, kappa), alpha)
            pens[n] = p
            tos[n] = t_base + span * p
        lo, hi = min(tos.values()), max(tos.values())
        d = hi - lo
        worst = min(worst, d)
        win = min(members, key=lambda n: tos[n])
        det = "  ".join(f"N{n}={tos[n]:.0f}" for n in members)
        flag = f"  ⚠️<{delta}ms 被δ淹没" if d < delta else ""
        print(f"  R{rid}: {det}ms   区分度={d:.0f}ms 赢家=N{win}{flag}")
    print(f"  → 最差 region 区分度 = {worst:.0f}ms  (需 > δ={delta} 才稳)")


if __name__ == "__main__":
    print("【论文原参数】span 太小，区分度被 δ 淹没：")
    disc(150, 300, 0.3, 5.0)

    print("\n\n【放大 span】保持论文形式，仅加大 T_max：")
    disc(150, 600, 0.3, 5.0)
    disc(150, 1200, 0.3, 5.0)

    print("\n\n【放大 span + 降 κ 拉出饱和区】：")
    disc(150, 1200, 0.3, 3.0)
    disc(150, 1200, 0.5, 3.0)

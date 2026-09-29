#!/usr/bin/env python3
"""对比三种提升 Δpenalty(负载区分度)的方案，用真实稳态场景。

区分度 = span * Δpenalty。固定 span，只看 Δpenalty 谁大。
方案：
  A 当前      ρ = L + κ·load + γs            (κ=5)
  B 降κ       ρ = L + κ·load + γs            (κ=3)
  C 相对负载  ρ = L + κ·(load - load_minRegion) + γs   (κ=5)
  D 相对+降κ无关：相对负载本身已脱离基线
"""
import math

GAMMA = 2.0
ALPHA = 0.3
REGIONS = {1: [0, 1, 2], 2: [1, 2, 3], 3: [2, 3, 4]}
SCEN = {0: (0, 0.48, 0.5), 1: (1, 0.40, 0.0), 2: (0, 0.49, 0.5),
        3: (0, 0.34, 0.0), 4: (1, 0.36, 0.5)}


def pen(rho, alpha=ALPHA):
    return 1 - math.pow(1 / (1 + alpha), rho)


def run(name, kappa, relative, alpha=ALPHA):
    print(f"\n{'='*60}\n{name}  (κ={kappa}, relative={relative}, α={alpha})\n{'='*60}")
    tot = 0
    for rid, members in REGIONS.items():
        loads = {n: SCEN[n][1] for n in members}
        lref = min(loads.values()) if relative else 0.0
        rhos, pens = {}, {}
        for n in members:
            L, load, s = SCEN[n]
            r = L + kappa * (load - lref) + GAMMA * s
            rhos[n] = r
            pens[n] = pen(r, alpha)
        dp = max(pens.values()) - min(pens.values())
        rmin = min(rhos.values())
        win = min(members, key=lambda n: rhos[n])
        tot += dp
        print(f"  R{rid}: ρ_min={rmin:.2f}  Δpenalty={dp:.4f}  "
              f"赢家=N{win}  (span=1000ms→区分度≈{dp*1000:.0f}ms)")
    print(f"  Σ Δpenalty = {tot:.4f}")


if __name__ == "__main__":
    run("A 当前(绝对load,κ=5)", 5.0, False)
    run("B 降κ(绝对load,κ=3)", 3.0, False)
    run("C 相对load(κ=5)", 5.0, True)
    run("C+ 相对load(κ=8 放大差异)", 8.0, True)
    run("C++ 相对load(κ=8,α=0.5)", 8.0, True, alpha=0.5)

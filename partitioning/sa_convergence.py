"""
SA 收敛曲线：记录每轮 best_cost，绘制收敛图
"""
import sys
import os
import numpy as np
import matplotlib.pyplot as plt

sys.path.insert(0, os.path.dirname(__file__))
from src.topology.spatial_generator import generate_ppp_topology
from src.topology.mec_builder import MECTopologyBuilder
from src.partition.strategies import PartitionStrategies
import networkx as nx


def sa_with_history(graph, tau, T0=5.0, alpha=0.995, max_iter=5000, seed=42):
    """运行 SA 并返回每轮的 best_cost 历史"""
    import random
    from collections import defaultdict

    rng = random.Random(seed)
    all_edges = set((min(u, v), max(u, v)) for u, v in graph.edges())

    # 初始解
    raw_regions = []
    for u, v in all_edges:
        R = {u, v}
        cands = list((set(graph.neighbors(u)) | set(graph.neighbors(v))) - R)
        rng.shuffle(cands)
        for w in cands:
            if len(R) >= tau:
                break
            R.add(w)
        raw_regions.append(R)

    unique = list(set(frozenset(r) for r in raw_regions))
    unique.sort(key=lambda x: -len(x))
    node_to_kept = defaultdict(set)
    regions = []
    for r in unique:
        r_nodes = list(r)
        cand = set(node_to_kept[r_nodes[0]])
        for n in r_nodes[1:]:
            cand &= node_to_kept[n]
            if not cand:
                break
        if any(r < regions[idx] for idx in cand):
            continue
        new_idx = len(regions)
        regions.append(set(r))
        for n in r:
            node_to_kept[n].add(new_idx)

    ps = PartitionStrategies(graph)

    node_to_regions = defaultdict(set)
    for idx, R in enumerate(regions):
        for n in R:
            node_to_regions[n].add(idx)

    edge_cov = {e: 0 for e in all_edges}
    for R in regions:
        for e in ps._sa_edges_in_region(R, all_edges):
            edge_cov[e] += 1

    state = {
        'regions': regions,
        'node_to_regions': node_to_regions,
        'edge_cov': edge_cov,
        'cost': sum(len(R) for R in regions),
    }

    best_cost = state['cost']
    best_regions = [R.copy() for R in regions]
    history = [best_cost]

    T = T0
    for iteration in range(max_iter):
        if T < 0.01:
            break
        for _ in range(50):
            if not state['regions']:
                break
            move_type = rng.random()
            if move_type < 0.45:
                ps._sa_node_remove(state, all_edges, graph, rng, T)
            elif move_type < 0.75:
                ps._sa_node_transfer(state, all_edges, graph, rng, T, tau)
            elif move_type < 0.90:
                ps._sa_region_merge(state, all_edges, graph, rng, T, tau)
            else:
                ps._sa_region_split(state, all_edges, graph, rng, T)

        T *= alpha
        if state['cost'] < best_cost:
            best_cost = state['cost']
            best_regions = [R.copy() for R in state['regions']]
        history.append(best_cost)

    return history


def generate_graph(num_nodes, seed):
    """生成测试图"""
    area_size = 2000
    coverage_radius = 300
    density = num_nodes / (area_size * area_size)
    mec_graph, spatial = generate_ppp_topology(
        area_size=area_size,
        lambda_bs=density,
        coverage_radius_mean=coverage_radius,
        coverage_radius_std=50,
        plot_voronoi=False,
        plot_graph=False,
        random_seed=seed,
        return_spatial=True
    )
    if not nx.is_connected(mec_graph):
        builder = MECTopologyBuilder()
        mec_graph = builder.extract_largest_component(mec_graph, verbose=False)
    return mec_graph


def main():
    os.makedirs('output/sa_convergence', exist_ok=True)

    configs = [
        (50, 5, 'n=50'),
        (100, 6, 'n=100'),
        (150, 7, 'n=150'),
        (200, 8, 'n=200'),
    ]

    max_iter = 3000

    plt.rcParams.update({
        'font.size': 13,
        'axes.labelsize': 14,
        'axes.labelweight': 'bold',
        'legend.fontsize': 12,
        'grid.alpha': 0.3,
        'grid.linestyle': '--',
    })

    fig, ax = plt.subplots(1, 1, figsize=(7, 5))

    COLORS = [
        '#1f4e79', '#2e75b6', '#c0504d', '#e36c42',
        '#4e8542', '#7f7f7f', '#9b59b6', '#f39c12',
        '#1abc9c', '#c94c4c'
    ]

    for i, (n, tau, label) in enumerate(configs):
        print(f"Running SA for {label}, tau={tau}, max_iter={max_iter}...")
        graph = generate_graph(n, seed=42)
        actual_n = graph.number_of_nodes()
        actual_m = graph.number_of_edges()
        print(f"  Actual nodes={actual_n}, edges={actual_m}")

        history = sa_with_history(graph, tau=tau, max_iter=max_iter, seed=42)
        iterations = list(range(len(history)))

        ax.plot(iterations, history, color=COLORS[i],
                linewidth=2, label=f'{label} (τ={tau})')

        converge_iter = len(history) - 1
        for j in range(len(history) - 1, 0, -1):
            if history[j] < history[j - 1]:
                converge_iter = j
                break
        print(f"  Initial cost={history[0]}, Final cost={history[-1]}, "
              f"Last improvement at iter={converge_iter}")

    ax.set_xlabel('Iteration')
    ax.set_ylabel('Best Cost (Σ|R|)')
    ax.grid(True, alpha=0.3, linestyle='--')
    ax.legend(framealpha=0.95)
    ax.set_xlim(left=0)

    plt.tight_layout()
    plt.savefig('output/sa_convergence/sa_convergence.png',
                bbox_inches='tight', dpi=300)
    plt.savefig('output/sa_convergence/sa_convergence.pdf',
                bbox_inches='tight')
    print("\n已保存: output/sa_convergence/sa_convergence.png")
    print("已保存: output/sa_convergence/sa_convergence.pdf")
    plt.close()


if __name__ == '__main__':
    main()

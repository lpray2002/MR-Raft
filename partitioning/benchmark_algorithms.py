"""
分区算法与增量更新算法执行时间 Benchmark
测量不同网络规模下各算法的执行时间，输出 LaTeX 表格和折线图
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ['LOKY_MAX_CPU_COUNT'] = '4'

import builtins
import functools
import time
import numpy as np
import networkx as nx
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from src.topology.spatial_generator import generate_ppp_topology
from src.topology.mec_builder import MECTopologyBuilder
from src.partition.strategies import PartitionStrategies
from src.partition.dynamic_region import DynamicRegionManager, DynamicRegionManagerOptimized

print = functools.partial(builtins.print, flush=True)

# 论文风格调色板（与 run.py / plotter.py 统一）
PAPER_COLORS = [
    '#0273bc', '#598dca', '#87a8d7', '#b1c4e5', '#d8e1f2',
    '#fbd7d3', '#f2aea8', '#e6867f', '#d65c58', '#c32934'
]

# ============================================
# 配置参数
# ============================================

AREA_SIZE = 2000
COVERAGE_RADIUS = 300
USE_OPTIMAL_TAU = False
FIXED_TAU = 5
NUM_TRIALS = 10  # 每个规模重复次数取平均
THRESHOLD_RANGE = range(2, 16)
THRESHOLD_ALPHA = 0.4
THRESHOLD_BETA = 0.6
NORMALIZATION = 'max'

# 通过密度控制节点数（area_size=2000 → 面积=4e6 m²）
# 节点数 ≈ density × area_size²
# TARGET_NODES = [10, 15, 20, 30, 50, 75, 100, 120, 140, 160, 180, 200, 220, 240, 260, 280, 300, 320, 340, 360, 380, 400]
TARGET_NODES = [10, 15, 20, 30, 50, 75, 100, 120, 140, 160, 180, 200, 220, 240, 260, 280, 300, 320, 340, 360, 380, 400]
DENSITIES = [n / (AREA_SIZE ** 2) for n in TARGET_NODES]

OUTPUT_DIR = 'output/benchmark'

# ============================================
# Benchmark 函数
# ============================================


def generate_graph(density, seed):
    """生成图并提取最大连通分量"""
    mec_graph, spatial = generate_ppp_topology(
        area_size=AREA_SIZE,
        lambda_bs=density,
        coverage_radius_mean=COVERAGE_RADIUS,
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


def _normalize_metric(value, min_value, max_value, normalization):
    """按论文配置对单个指标归一化。"""
    if normalization == 'minmax':
        denom = (max_value - min_value) or 1
        return (value - min_value) / denom
    denom = max_value or 1
    return value / denom


def find_optimal_tau_single_scan(graph, threshold_range=None,
                                 alpha=THRESHOLD_ALPHA,
                                 beta=THRESHOLD_BETA,
                                 normalization=NORMALIZATION):
    """为 benchmark 选择 τ*，并复用一次 maximal-clique 枚举结果。

    该函数只用于选择各拓扑的 τ*。后续静态运行时间仍对每个算法
    进行一次完整调用，因此 runtime 图表示给定 τ* 后的单次运行时间，
    不混入阈值扫描开销。
    """
    threshold_range = list(threshold_range or THRESHOLD_RANGE)
    ps = PartitionStrategies(graph)
    cliques = [set(c) for c in nx.find_cliques(graph)]

    rows = []
    t0 = time.perf_counter()
    for tau in threshold_range:
        partitions = ps._merge_small_partitions(cliques, tau)
        stats = ps.get_partition_statistics(partitions)
        rows.append((tau, partitions, stats))
    scan_ms = (time.perf_counter() - t0) * 1000

    avg_instances = [stats['avg_instances'] for _, _, stats in rows]
    avg_sizes = [stats['avg_size'] for _, _, stats in rows]
    min_instances, max_instances = min(avg_instances), max(avg_instances)
    min_size, max_size = min(avg_sizes), max(avg_sizes)

    best_tau = rows[0][0]
    best_score = float('inf')
    for tau, _partitions, stats in rows:
        norm_instances = _normalize_metric(
            stats['avg_instances'], min_instances, max_instances, normalization)
        norm_size = _normalize_metric(
            stats['avg_size'], min_size, max_size, normalization)
        score = alpha * norm_instances + beta * norm_size
        if score < best_score:
            best_tau = tau
            best_score = score

    return best_tau, best_score, scan_ms


def benchmark_static_partitions(graph, tau):
    """测量各静态分区算法的执行时间"""
    ps = PartitionStrategies(graph)
    results = {}

    # 1. Maximal Clique
    t0 = time.perf_counter()
    partitions_clique = ps.maximal_clique()
    results['maximal_clique'] = {
        'time_ms': (time.perf_counter() - t0) * 1000,
        'num_regions': len(partitions_clique)
    }

    # 2. Threshold-based
    t0 = time.perf_counter()
    partitions_threshold = ps.threshold_based(tau)
    results['threshold_based'] = {
        'time_ms': (time.perf_counter() - t0) * 1000,
        'num_regions': len(partitions_threshold)
    }

    # 3. Neighbor-based
    t0 = time.perf_counter()
    partitions_neighbor = ps.neighbor_based()
    results['neighbor_based'] = {
        'time_ms': (time.perf_counter() - t0) * 1000,
        'num_regions': len(partitions_neighbor)
    }

    # 4. Global (baseline)
    t0 = time.perf_counter()
    partitions_global = ps.global_partition()
    results['global'] = {
        'time_ms': (time.perf_counter() - t0) * 1000,
        'num_regions': len(partitions_global)
    }

    # 5. Greedy Node-Centric
    t0 = time.perf_counter()
    partitions_greedy = ps.greedy_node_centric(tau)
    results['greedy'] = {
        'time_ms': (time.perf_counter() - t0) * 1000,
        'num_regions': len(partitions_greedy)
    }

    # 6. Simulated Annealing (skip for very large graphs due to high cost)
    if graph.number_of_nodes() <= 250:
        t0 = time.perf_counter()
        partitions_sa = ps.simulated_annealing(tau, max_iter=1300)
        results['sa'] = {
            'time_ms': (time.perf_counter() - t0) * 1000,
            'num_regions': len(partitions_sa)
        }
    else:
        results['sa'] = {'time_ms': -1, 'num_regions': -1}

    return results


def benchmark_incremental_update(graph, tau, num_ops=10, seed=42):
    """测量增量更新算法的执行时间"""
    ps = PartitionStrategies(graph)
    initial = ps.threshold_based(tau)

    results = {}
    nodes = list(graph.nodes())
    non_edges = list(nx.non_edges(graph))
    edges = list(graph.edges())

    if not non_edges or not edges:
        return {'edge_addition': {'time_ms': 0, 'avg_ms': 0, 'num_ops': 0},
                'edge_removal': {'time_ms': 0, 'avg_ms': 0, 'num_ops': 0}}

    rng = np.random.default_rng(seed)
    num_add = min(num_ops, len(non_edges))
    num_remove = min(num_ops, len(edges))

    # Edge Addition (with step breakdown)
    add_edges = [non_edges[i] for i in rng.choice(len(non_edges), num_add, replace=False)]
    # manager = DynamicRegionManager(graph.copy(), initial, tau=tau)
    # t0 = time.perf_counter()
    # add_timings = []
    # for u, v in add_edges:
    #     manager.edge_addition(u, v)
    #     add_timings.append(manager._last_timing)
    # total_add = (time.perf_counter() - t0) * 1000
    # results['edge_addition'] = {
    #     'time_ms': total_add,
    #     'avg_ms': total_add / num_add,
    #     'num_ops': num_add,
    #     'breakdown': {
    #         'step1_add_edge_ms': np.mean([t['step1_add_edge_ms'] for t in add_timings]),
    #         'step2_affected_nodes_ms': np.mean([t['step2_affected_nodes_ms'] for t in add_timings]),
    #         'step3_find_cliques_ms': np.mean([t['step3_find_cliques_ms'] for t in add_timings]),
    #         'step4_merge_ms': np.mean([t['step4_merge_ms'] for t in add_timings]),
    #         'avg_affected_size': np.mean([t['affected_size'] for t in add_timings]),
    #         'avg_num_cliques': np.mean([t['num_cliques'] for t in add_timings]),
    #     }
    # }

    # Edge Removal (with step breakdown)
    remove_edges = [edges[i] for i in rng.choice(len(edges), num_remove, replace=False)]
    # manager = DynamicRegionManager(graph.copy(), initial, tau=tau)
    # t0 = time.perf_counter()
    # rem_timings = []
    # for u, v in remove_edges:
    #     manager.edge_removal(u, v)
    #     rem_timings.append(manager._last_timing)
    # total_remove = (time.perf_counter() - t0) * 1000
    # results['edge_removal'] = {
    #     'time_ms': total_remove,
    #     'avg_ms': total_remove / num_remove,
    #     'num_ops': num_remove,
    #     'breakdown': {
    #         'step1_remove_edge_ms': np.mean([t['step1_remove_edge_ms'] for t in rem_timings]),
    #         'step2_check_regions_ms': np.mean([t['step2_check_regions_ms'] for t in rem_timings]),
    #         'step3_cleanup_ms': np.mean([t['step3_cleanup_ms'] for t in rem_timings]),
    #     }
    # }

    # Batch (sequential internally)
    changes = [('+', u, v) for u, v in add_edges[:num_add//2]]
    changes += [('-', u, v) for u, v in remove_edges[:num_remove//2]]
    # manager = DynamicRegionManager(graph.copy(), initial, tau=tau)
    # t0 = time.perf_counter()
    # for op, u, v in changes:
    #     if op == '+':
    #         manager.edge_addition(u, v)
    #     else:
    #         manager.edge_removal(u, v)
    # total_batch = (time.perf_counter() - t0) * 1000
    # results['batch_sequential'] = {
    #     'time_ms': total_batch,
    #     'avg_ms': total_batch / len(changes) if changes else 0,
    #     'num_ops': len(changes)
    # }

    # Batch (local rebuild)
    # manager = DynamicRegionManager(graph.copy(), initial, tau=tau)
    # t0 = time.perf_counter()
    # manager.batch_update(changes)
    # total_batch_rebuild = (time.perf_counter() - t0) * 1000
    # results['batch_rebuild'] = {
    #     'time_ms': total_batch_rebuild,
    #     'avg_ms': total_batch_rebuild / len(changes) if changes else 0,
    #     'num_ops': len(changes),
    #     'breakdown': manager._last_timing if hasattr(manager, '_last_timing') else {}
    # }

    # Edge Addition
    manager_opt = DynamicRegionManagerOptimized(graph.copy(), initial, tau=tau)
    t0 = time.perf_counter()
    opt_add_timings = []
    for u, v in add_edges:
        manager_opt.edge_addition(u, v)
        opt_add_timings.append(manager_opt._last_timing)
    total_add_opt = (time.perf_counter() - t0) * 1000
    results['edge_addition'] = {
        'time_ms': total_add_opt,
        'avg_ms': total_add_opt / num_add,
        'num_ops': num_add,
        'breakdown': {
            'step1_add_edge_ms': np.mean([t['step1_add_edge_ms'] for t in opt_add_timings]),
            'step2_affected_nodes_ms': np.mean([t['step2_affected_nodes_ms'] for t in opt_add_timings]),
            'step3_find_cliques_ms': np.mean([t['step3_find_cliques_ms'] for t in opt_add_timings]),
            'step4_merge_ms': np.mean([t['step4_merge_ms'] for t in opt_add_timings]),
            'avg_affected_size': np.mean([t['affected_size'] for t in opt_add_timings]),
            'avg_num_cliques': np.mean([t['num_cliques'] for t in opt_add_timings]),
        }
    }

    # Edge Removal
    manager_opt = DynamicRegionManagerOptimized(graph.copy(), initial, tau=tau)
    t0 = time.perf_counter()
    opt_rem_timings = []
    for u, v in remove_edges:
        manager_opt.edge_removal(u, v)
        opt_rem_timings.append(manager_opt._last_timing)
    total_remove_opt = (time.perf_counter() - t0) * 1000
    results['edge_removal'] = {
        'time_ms': total_remove_opt,
        'avg_ms': total_remove_opt / num_remove,
        'num_ops': num_remove,
    }

    # Batch sequential（暂不测，保留代码）
    # manager_opt = DynamicRegionManagerOptimized(graph.copy(), initial, tau=tau)
    # t0 = time.perf_counter()
    # for op, u, v in changes:
    #     if op == '+':
    #         manager_opt.edge_addition(u, v)
    #     else:
    #         manager_opt.edge_removal(u, v)
    # total_batch_opt = (time.perf_counter() - t0) * 1000
    # results['batch_sequential'] = {
    #     'time_ms': total_batch_opt,
    #     'avg_ms': total_batch_opt / len(changes) if changes else 0,
    #     'num_ops': len(changes)
    # }

    # Batch rebuild（暂不测，保留代码）
    # manager_opt = DynamicRegionManagerOptimized(graph.copy(), initial, tau=tau)
    # t0 = time.perf_counter()
    # manager_opt.batch_update(changes)
    # total_batch_rebuild_opt = (time.perf_counter() - t0) * 1000
    # results['batch_rebuild'] = {
    #     'time_ms': total_batch_rebuild_opt,
    #     'avg_ms': total_batch_rebuild_opt / len(changes) if changes else 0,
    #     'num_ops': len(changes),
    #     'breakdown': manager_opt._last_timing if hasattr(manager_opt, '_last_timing') else {}
    # }

    return results


def run_benchmark():
    """运行完整 benchmark"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    all_results = []

    print(f"{'='*70}")
    print(f"  Algorithm Execution Time Benchmark")
    print(f"  Area={AREA_SIZE}m, Coverage={COVERAGE_RADIUS}m, Trials={NUM_TRIALS}")
    if USE_OPTIMAL_TAU:
        print(f"  Static and incremental runtimes use each topology's selected τ* "
              f"(scan range={list(THRESHOLD_RANGE)}, "
              f"α={THRESHOLD_ALPHA}, β={THRESHOLD_BETA}, norm={NORMALIZATION})")
    else:
        print(f"  Static and incremental runtimes use fixed τ={FIXED_TAU}")
    print(f"{'='*70}")

    for idx, (target_n, density) in enumerate(zip(TARGET_NODES, DENSITIES)):
        print(f"\n  [{idx+1}/{len(TARGET_NODES)}] Target nodes ≈ {target_n} (density={density:.2e})")

        trial_results = []
        for trial in range(NUM_TRIALS):
            seed = trial * 100 + idx * 7 + 42
            graph = generate_graph(density, seed)
            n_nodes = graph.number_of_nodes()
            n_edges = graph.number_of_edges()

            if USE_OPTIMAL_TAU:
                tau_used, tau_score, tau_scan_ms = find_optimal_tau_single_scan(graph)
            else:
                tau_used, tau_score, tau_scan_ms = FIXED_TAU, None, 0.0

            static = benchmark_static_partitions(graph, tau_used)
            incremental = benchmark_incremental_update(graph, tau_used, num_ops=10, seed=seed)

            trial_results.append({
                'n_nodes': n_nodes,
                'n_edges': n_edges,
                'tau': tau_used,
                'tau_score': tau_score,
                'tau_scan_ms': tau_scan_ms,
                'static': static,
                'incremental': incremental
            })

        # 取平均
        avg_nodes = np.mean([r['n_nodes'] for r in trial_results])
        avg_edges = np.mean([r['n_edges'] for r in trial_results])
        avg_tau = np.mean([r['tau'] for r in trial_results])
        avg_tau_scan_ms = np.mean([r['tau_scan_ms'] for r in trial_results])

        avg_static = {}
        for algo in ['maximal_clique', 'threshold_based', 'neighbor_based', 'global', 'greedy', 'sa']:
            times = [r['static'][algo]['time_ms'] for r in trial_results]
            if all(t >= 0 for t in times):
                avg_static[algo] = {
                    'time_ms': np.mean(times),
                    'std_ms': np.std(times),
                    'num_regions': np.mean([r['static'][algo]['num_regions'] for r in trial_results]),
                }
            else:
                avg_static[algo] = {'time_ms': -1, 'std_ms': 0, 'num_regions': -1}

        avg_incr = {}
        for algo in ['edge_addition', 'edge_removal']:
            avg_incr[algo] = {
                'time_ms': np.mean([r['incremental'][algo]['time_ms'] for r in trial_results]),
                'std_ms': np.std([r['incremental'][algo]['time_ms'] for r in trial_results]),
                'avg_ms': np.mean([r['incremental'][algo].get('avg_ms', 0) for r in trial_results]),
            }
        
        # 聚合 breakdown
        add_breakdowns = [r['incremental']['edge_addition'].get('breakdown', {}) for r in trial_results]
        if add_breakdowns and add_breakdowns[0]:
            avg_incr['edge_addition']['breakdown'] = {
                k: np.mean([bd[k] for bd in add_breakdowns if k in bd])
                for k in add_breakdowns[0].keys()
            }
        
        # batch breakdown（暂不测，保留代码）
        # batch_breakdowns = [r['incremental']['batch_rebuild'].get('breakdown', {}) for r in trial_results]
        # if batch_breakdowns and batch_breakdowns[0]:
        #     avg_incr['batch_rebuild']['breakdown'] = {
        #         k: np.mean([bd[k] for bd in batch_breakdowns if k in bd])
        #         for k in batch_breakdowns[0].keys()
        #     }

        result = {
            'target_n': target_n,
            'avg_nodes': avg_nodes,
            'avg_edges': avg_edges,
            'avg_tau': avg_tau,
            'avg_tau_scan_ms': avg_tau_scan_ms,
            'static': avg_static,
            'incremental': avg_incr
        }
        all_results.append(result)

        print(f"    Actual: {avg_nodes:.0f} nodes, {avg_edges:.0f} edges")
        if USE_OPTIMAL_TAU:
            print(f"    τ*: avg={avg_tau:.2f}, "
                  f"threshold-scan overhead={avg_tau_scan_ms:.3f}ms "
                  f"(not included in static runtimes)")
        else:
            print(f"    τ={avg_tau:.0f}")
        greedy_str = f"{avg_static['greedy']['time_ms']:.3f}"
        sa_str = f"{avg_static['sa']['time_ms']:.3f}" if avg_static['sa']['time_ms'] >= 0 else "skipped"
        print(f"    Static:  clique={avg_static['maximal_clique']['time_ms']:.3f}ms, "
              f"threshold={avg_static['threshold_based']['time_ms']:.3f}ms, "
              f"neighbor={avg_static['neighbor_based']['time_ms']:.3f}ms, "
              f"greedy={greedy_str}ms, sa={sa_str}ms")
        print(f"    Incremental: add={avg_incr['edge_addition']['avg_ms']:.3f}ms/op, "
              f"remove={avg_incr['edge_removal']['avg_ms']:.3f}ms/op")
        
        # 打印步骤分解（取最后一个 trial 的 breakdown）
        last_trial = trial_results[-1]['incremental']
        if 'breakdown' in last_trial['edge_addition']:
            bd = last_trial['edge_addition']['breakdown']
            print(f"      Add breakdown: deepcopy={bd['step1_add_edge_ms']:.3f} | "
                  f"affected={bd['step2_affected_nodes_ms']:.3f} | "
                  f"cliques={bd['step3_find_cliques_ms']:.3f} | "
                  f"merge={bd['step4_merge_ms']:.3f} | "
                  f"|C|={bd['avg_affected_size']:.1f}")
        # batch breakdown 打印（暂不测，保留代码）
        # if 'breakdown' in last_trial['batch_rebuild']:
        #     bd = last_trial['batch_rebuild']['breakdown']
        #     if bd:
        #         print(f"      Batch breakdown: collect={bd.get('step1_collect_affected_ms',0):.3f} | "
        #               f"apply={bd.get('step2_apply_changes_ms',0):.3f} | "
        #               f"find_regions={bd.get('step3_find_regions_ms',0):.3f} | "
        #               f"repartition={bd.get('step4_repartition_ms',0):.3f} | "
        #               f"id_match={bd.get('step5_id_match_cleanup_ms',0):.3f} | "
        #               f"|A|={bd.get('affected_size',0)}")

    # 生成输出
    import json
    with open(f'{OUTPUT_DIR}/benchmark_results.json', 'w') as f:
        json.dump(all_results, f)
    generate_plot(all_results)
    generate_latex_table(all_results)

    print(f"\n{'='*70}")
    print(f"  Benchmark complete!")
    print(f"  Output: {OUTPUT_DIR}/")
    print(f"{'='*70}")


def generate_plot(results):
    """生成执行时间折线图（拆分为单图，论文风格）"""
    nodes = [r['avg_nodes'] for r in results]

    # 论文风格参数：轴标签 20 / 刻度 18 / 图例 12（框内 best）
    plt.rcParams['font.size'] = 18
    plt.rcParams['axes.labelsize'] = 20
    plt.rcParams['legend.fontsize'] = 12
    plt.rcParams['xtick.labelsize'] = 18
    plt.rcParams['ytick.labelsize'] = 18
    # 保证导出的 PDF/SVG 中文字为可编辑文字对象
    plt.rcParams['pdf.fonttype'] = 42
    plt.rcParams['svg.fonttype'] = 'none'
    plt.rcParams['ps.fonttype'] = 42
    # 保持线条/点为完整矢量路径（不简化顶点、不栅格化），便于逐个编辑
    plt.rcParams['path.simplify'] = False
    plt.rcParams['pdf.compression'] = 0
    plt.rcParams['agg.path.chunksize'] = 0
    FIG_SIZE = (7, 5)        # 适合双栏论文单图比例
    GRID_KW = dict(alpha=0.7, linestyle='--', linewidth=0.8)

    def plot_editable_polyline(ax, xs, ys, label, color,
                               marker='o', markersize=10, linewidth=3.5):
        """逐段绘制折线，并将点单独绘制，便于在矢量 PDF 中逐段编辑。"""
        if not xs:
            return
        if len(xs) == 1:
            ax.scatter(xs, ys, label=label, marker=marker, s=markersize ** 2,
                       color=color, zorder=3)
            return
        for i in range(len(xs) - 1):
            ax.plot(xs[i:i + 2], ys[i:i + 2],
                    label=label if i == 0 else '_nolegend_',
                    color=color, linewidth=linewidth,
                    solid_capstyle='round', solid_joinstyle='round')
        ax.scatter(xs, ys, marker=marker, s=markersize ** 2,
                   color=color, zorder=3)

    # ============ 图1：静态分区算法（单图）============
    fig, ax = plt.subplots(figsize=FIG_SIZE)
    algos = ['maximal_clique', 'threshold_based', 'neighbor_based', 'greedy', 'sa']
    if USE_OPTIMAL_TAU:
        labels = ['Maximal Clique', r'Threshold ($\tau^*$)', 'Neighbor-based',
                  r'Greedy ($\tau^*$)', r'SA ($\tau^*$)']
    else:
        labels = ['Maximal Clique', rf'Threshold ($\tau={FIXED_TAU}$)', 'Neighbor-based',
                  rf'Greedy ($\tau={FIXED_TAU}$)', rf'SA ($\tau={FIXED_TAU}$)']
    # 统一圆形 marker，用论文调色板较深的颜色区分
    static_colors = [PAPER_COLORS[0], PAPER_COLORS[1], PAPER_COLORS[7],
                     PAPER_COLORS[8], PAPER_COLORS[9]]
    # maximal_clique 与 threshold 耗时几乎重合，给一个轻微的显示偏移以错开（仅视觉）
    display_offset = {'maximal_clique': 0.96, 'threshold_based': 1.04}

    for algo, label, color in zip(algos, labels, static_colors):
        times = [r['static'][algo]['time_ms'] for r in results]
        valid = [(n, t) for n, t in zip(nodes, times) if t >= 0]
        if valid:
            vn, vt = zip(*valid)
            off = display_offset.get(algo, 1.0)
            vt = [t * off for t in vt]
            plot_editable_polyline(ax, list(vn), vt, label=label,
                                   color=color, marker='o')

    ax.set_xlabel('Number of Nodes', fontweight='bold')
    ax.set_ylabel('Execution Time (ms)', fontweight='bold')
    ax.set_yscale('log')
    ax.minorticks_off()  # 关闭 log 轴次刻度
    ax.legend(fontsize=12, framealpha=0.95, loc='best')
    ax.grid(True, **GRID_KW)
    plt.tight_layout()
    plt.savefig(f'{OUTPUT_DIR}/algorithm_time_static.png', dpi=300, bbox_inches='tight')
    plt.savefig(f'{OUTPUT_DIR}/algorithm_time_static.pdf', bbox_inches='tight')
    plt.close()
    print(f"  Plot saved: {OUTPUT_DIR}/algorithm_time_static.png")

    # ============ 图2：增量更新算法（单图）============
    fig, ax = plt.subplots(figsize=FIG_SIZE)
    add_times = [r['incremental']['edge_addition']['avg_ms'] for r in results]
    rem_times = [r['incremental']['edge_removal']['avg_ms'] for r in results]

    plot_editable_polyline(ax, nodes, add_times, label='Edge Addition',
                           color=PAPER_COLORS[0], marker='o')
    plot_editable_polyline(ax, nodes, rem_times, label='Edge Removal',
                           color=PAPER_COLORS[9], marker='o')

    ax.set_xlabel('Number of Nodes', fontweight='bold')
    ax.set_ylabel('Execution Time (ms/op)', fontweight='bold')
    ax.legend(fontsize=12, framealpha=0.95, loc='best')
    ax.grid(True, **GRID_KW)
    plt.tight_layout()
    plt.savefig(f'{OUTPUT_DIR}/algorithm_time_incremental.png', dpi=300, bbox_inches='tight')
    plt.savefig(f'{OUTPUT_DIR}/algorithm_time_incremental.pdf', bbox_inches='tight')
    plt.close()
    print(f"  Plot saved: {OUTPUT_DIR}/algorithm_time_incremental.png")

    # 步骤分解图
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))

    # Edge Addition breakdown (stacked area)
    add_steps = ['step1_add_edge_ms', 'step2_affected_nodes_ms',
                 'step3_find_cliques_ms', 'step4_merge_ms']
    add_labels = ['deepcopy + add_edge', 'Compute affected set',
                  'Enumerate cliques', 'Merge + subset removal']
    add_colors = ['#a6cee3', '#b2df8a', '#fb9a99', '#fdbf6f']

    add_data = {s: [] for s in add_steps}
    valid_nodes_add = []
    for r in results:
        bd = r['incremental'].get('edge_addition', {}).get('breakdown', {})
        if bd and all(s in bd for s in add_steps):
            valid_nodes_add.append(r['avg_nodes'])
            for s in add_steps:
                add_data[s].append(bd[s])

    if valid_nodes_add:
        bottom = np.zeros(len(valid_nodes_add))
        for step, label, color in zip(add_steps, add_labels, add_colors):
            vals = np.array(add_data[step])
            ax1.bar(range(len(valid_nodes_add)), vals, bottom=bottom,
                    label=label, color=color, width=0.8)
            bottom += vals
        ax1.set_xticks(range(len(valid_nodes_add)))
        ax1.set_xticklabels([f'{int(n)}' for n in valid_nodes_add], rotation=45)
        ax1.set_xlabel('Number of Nodes', fontsize=20, fontweight='bold')
        ax1.set_ylabel('Avg Time per Operation (ms)', fontsize=20, fontweight='bold')
        ax1.legend(loc='best', fontsize=12)
        ax1.grid(True, axis='y', **GRID_KW)

    # Batch rebuild breakdown (stacked area)
    batch_steps = ['step1_collect_affected_ms', 'step2_apply_changes_ms',
                   'step3_find_regions_ms', 'step4_repartition_ms',
                   'step5_id_match_cleanup_ms']
    batch_labels = ['Collect affected', 'Apply changes',
                    'Find regions', 'Repartition (cliques)',
                    'ID match + cleanup']
    batch_colors = ['#a6cee3', '#b2df8a', '#fb9a99', '#fdbf6f', '#cab2d6']

    batch_data = {s: [] for s in batch_steps}
    valid_nodes_batch = []
    for r in results:
        bd = r['incremental'].get('batch_rebuild', {}).get('breakdown', {})
        if bd and all(s in bd for s in batch_steps):
            valid_nodes_batch.append(r['avg_nodes'])
            for s in batch_steps:
                batch_data[s].append(bd[s])

    if valid_nodes_batch:
        bottom = np.zeros(len(valid_nodes_batch))
        for step, label, color in zip(batch_steps, batch_labels, batch_colors):
            vals = np.array(batch_data[step])
            ax2.bar(range(len(valid_nodes_batch)), vals, bottom=bottom,
                    label=label, color=color, width=0.8)
            bottom += vals
        ax2.set_xticks(range(len(valid_nodes_batch)))
        ax2.set_xticklabels([f'{int(n)}' for n in valid_nodes_batch], rotation=45)
        ax2.set_xlabel('Number of Nodes', fontsize=20, fontweight='bold')
        ax2.set_ylabel('Total Time (ms)', fontsize=20, fontweight='bold')
        ax2.legend(loc='best', fontsize=12)
        ax2.grid(True, axis='y', **GRID_KW)

    plt.tight_layout()
    plt.savefig(f'{OUTPUT_DIR}/algorithm_step_breakdown.png', dpi=300, bbox_inches='tight')
    plt.savefig(f'{OUTPUT_DIR}/algorithm_step_breakdown.pdf', bbox_inches='tight')
    plt.close()
    print(f"  Plot saved: {OUTPUT_DIR}/algorithm_step_breakdown.png")


def generate_latex_table(results):
    """生成 LaTeX 表格"""
    lines = []
    lines.append(r'\begin{table}[t]')
    lines.append(r'\centering')
    lines.append(r'\caption{Algorithm Execution Time under Different Network Scales}')
    lines.append(r'\label{tab:algorithm_time}')
    lines.append(r'\begin{tabular}{rrrrrrrrrr}')
    lines.append(r'\toprule')
    tau_header = r'$\tau^*$' if USE_OPTIMAL_TAU else r'$\tau$'
    lines.append(rf'$|V|$ & $|E|$ & {tau_header} & Clique & Threshold & Neighbor & Greedy & SA & Add & Remove \\')
    lines.append(r' & & & (ms) & (ms) & (ms) & (ms) & (ms) & (ms/op) & (ms/op) \\')
    lines.append(r'\midrule')

    for r in results:
        sa_str = f"{r['static']['sa']['time_ms']:.3f}" if r['static']['sa']['time_ms'] >= 0 else "---"
        line = (f"{r['avg_nodes']:.0f} & {r['avg_edges']:.0f} & "
                f"{r.get('avg_tau', FIXED_TAU):.1f} & "
                f"{r['static']['maximal_clique']['time_ms']:.3f} & "
                f"{r['static']['threshold_based']['time_ms']:.3f} & "
                f"{r['static']['neighbor_based']['time_ms']:.3f} & "
                f"{r['static']['greedy']['time_ms']:.3f} & "
                f"{sa_str} & "
                f"{r['incremental']['edge_addition']['avg_ms']:.3f} & "
                f"{r['incremental']['edge_removal']['avg_ms']:.3f} \\\\")
        lines.append(line)

    lines.append(r'\bottomrule')
    lines.append(r'\end{tabular}')
    lines.append(r'\end{table}')

    table_str = '\n'.join(lines)
    with open(f'{OUTPUT_DIR}/algorithm_time_table.tex', 'w') as f:
        f.write(table_str)

    print(f"  LaTeX table saved: {OUTPUT_DIR}/algorithm_time_table.tex")
    print(f"\n  LaTeX Table Preview:")
    print(table_str)


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == '--replot':
        import json
        with open(f'{OUTPUT_DIR}/benchmark_results.json') as f:
            cached = json.load(f)
        generate_plot(cached)
        generate_latex_table(cached)
        print("Replotted from cached results.")
    else:
        run_benchmark()

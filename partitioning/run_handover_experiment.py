"""
UAV Handover 端到端可靠性实验
完整实验矩阵：移动模型 × UAV速度 × Leader分布策略
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '.'))

import json
import time
import numpy as np
import networkx as nx
from itertools import product

from src.topology.spatial_generator import SpatialTopologyGenerator
from src.topology.mec_builder import MECTopologyBuilder
from src.partition.strategies import PartitionStrategies
from src.simulation.handover_benchmark import (
    HandoverBenchmark, ExperimentConfig, ExperimentResult,
    save_results_csv, save_summary,
)


# =============================================================================
# 实验配置
# =============================================================================

# 拓扑参数
AREA_SIZE = 2000
DENSITY = 4e-6
COVERAGE_RADIUS = 300
COVERAGE_STD = 50
NUM_NODES = 15
TAU = 4
TOPO_SEED = 42

# 实验矩阵
MOVEMENT_MODELS = ["linear", "rwp", "gauss_markov", "predefined"]
UAV_SPEEDS = [10, 20, 40]           # m/s
LEADER_STRATEGIES = ["balanced", "moderate", "skewed"]

# 实验参数
DURATION_S = 60.0                    # 每组实验时长
REQUEST_INTERVAL_MS = 50             # 请求间隔
MODE = "simulated"                   # simulated 或 real
SEEDS = [42, 123, 456]               # 多次重复取平均

# 输出目录
OUTPUT_DIR = "output/handover_experiment"


# =============================================================================
# 辅助函数
# =============================================================================

def setup_topology():
    """生成并返回拓扑"""
    gen = SpatialTopologyGenerator(
        area_size=AREA_SIZE, density=DENSITY,
        coverage_radius=COVERAGE_RADIUS, coverage_std=COVERAGE_STD,
        random_seed=TOPO_SEED
    )
    spatial = gen.generate_with_fixed_count(NUM_NODES)
    builder = MECTopologyBuilder()
    G = builder.build_from_spatial(spatial)
    
    if not nx.is_connected(G):
        G = builder.extract_largest_component(G, verbose=False)
        nodes_in_graph = set(G.nodes())
        spatial.base_stations = [
            bs for bs in spatial.base_stations if bs.id in nodes_in_graph
        ]
        spatial.num_stations = len(spatial.base_stations)
    
    ps = PartitionStrategies(G)
    regions = {i: set(r) for i, r in enumerate(ps.threshold_based(TAU))}
    
    return spatial, G, regions


def run_single_experiment(spatial, G, regions, model, speed, strategy, seed):
    """运行单次实验"""
    config = ExperimentConfig(
        movement_model=model,
        uav_speed=speed,
        leader_strategy=strategy,
        duration_s=DURATION_S,
        request_interval_ms=REQUEST_INTERVAL_MS,
        mode=MODE,
        seed=seed,
    )
    
    benchmark = HandoverBenchmark(config, spatial, G, regions)
    result = benchmark.run_sync()
    return result


def aggregate_results(results: list) -> dict:
    """聚合多次重复实验的结果"""
    if not results:
        return {}
    
    agg = {
        "num_runs": len(results),
        "total_requests": np.mean([r.total_requests for r in results]),
        "success_rate": np.mean([r.success_rate for r in results]),
        "success_rate_std": np.std([r.success_rate for r in results]),
        "avg_latency_ms": np.mean([r.avg_latency_ms for r in results]),
        "avg_latency_std": np.std([r.avg_latency_ms for r in results]),
        "p50_latency_ms": np.mean([r.p50_latency_ms for r in results]),
        "p95_latency_ms": np.mean([r.p95_latency_ms for r in results]),
        "p99_latency_ms": np.mean([r.p99_latency_ms for r in results]),
        "num_handovers": np.mean([len(r.handover_events) for r in results]),
        "handover_window_requests": np.mean([r.handover_window_requests for r in results]),
        "handover_window_success_rate": np.mean([
            r.handover_window_success_rate for r in results 
            if r.handover_window_requests > 0
        ]) if any(r.handover_window_requests > 0 for r in results) else 0,
        "handover_window_avg_latency_ms": np.mean([
            r.handover_window_avg_latency_ms for r in results
            if r.handover_window_avg_latency_ms > 0
        ]) if any(r.handover_window_avg_latency_ms > 0 for r in results) else 0,
        "stable_avg_latency_ms": np.mean([r.stable_avg_latency_ms for r in results]),
    }
    
    # 延迟增加量
    if agg["stable_avg_latency_ms"] > 0 and agg["handover_window_avg_latency_ms"] > 0:
        agg["latency_increase_ms"] = (
            agg["handover_window_avg_latency_ms"] - agg["stable_avg_latency_ms"])
        agg["latency_increase_ratio"] = (
            agg["handover_window_avg_latency_ms"] / agg["stable_avg_latency_ms"])
    else:
        agg["latency_increase_ms"] = 0
        agg["latency_increase_ratio"] = 1.0
    
    return agg


# =============================================================================
# 主函数
# =============================================================================

def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    print("=" * 80)
    print("  UAV Handover End-to-End Reliability Experiment")
    print("=" * 80)
    
    # 生成拓扑
    spatial, G, regions = setup_topology()
    print(f"\nTopology: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges, "
          f"{len(regions)} regions")
    print(f"Experiment matrix: {len(MOVEMENT_MODELS)} models × "
          f"{len(UAV_SPEEDS)} speeds × {len(LEADER_STRATEGIES)} strategies "
          f"× {len(SEEDS)} seeds")
    total_runs = len(MOVEMENT_MODELS) * len(UAV_SPEEDS) * len(LEADER_STRATEGIES) * len(SEEDS)
    print(f"Total runs: {total_runs}")
    print(f"Duration per run: {DURATION_S}s (simulated)")
    print()
    
    # 运行实验矩阵
    all_results = {}  # (model, speed, strategy) -> aggregated result
    all_raw_results = {}  # (model, speed, strategy) -> [ExperimentResult, ...]
    
    run_count = 0
    start_time = time.time()
    
    # 打印表头
    header = (f"{'#':>3} {'Model':<14} {'Strategy':<10} {'Speed':>5} "
              f"{'HOs':>4} {'Succ%':>7} {'AvgLat':>7} {'P99':>7} "
              f"{'HO_Lat':>7} {'Stable':>7} {'ΔLat':>6}")
    print(header)
    print("-" * len(header))
    
    for model, speed, strategy in product(MOVEMENT_MODELS, UAV_SPEEDS, LEADER_STRATEGIES):
        results_for_config = []
        
        for seed in SEEDS:
            run_count += 1
            result = run_single_experiment(
                spatial, G, regions, model, speed, strategy, seed)
            results_for_config.append(result)
            
            # 保存单次结果 CSV
            csv_path = (f"{OUTPUT_DIR}/csv/"
                       f"{model}_s{speed}_{strategy}_seed{seed}.csv")
            os.makedirs(os.path.dirname(csv_path), exist_ok=True)
            save_results_csv(result, csv_path)
        
        # 聚合
        agg = aggregate_results(results_for_config)
        key = (model, speed, strategy)
        all_results[key] = agg
        all_raw_results[key] = results_for_config
        
        # 打印进度
        print(f"{run_count:>3} {model:<14} {strategy:<10} {speed:>5} "
              f"{agg['num_handovers']:>4.0f} "
              f"{agg['success_rate']*100:>6.2f}% "
              f"{agg['avg_latency_ms']:>6.2f} "
              f"{agg['p99_latency_ms']:>6.2f} "
              f"{agg['handover_window_avg_latency_ms']:>6.2f} "
              f"{agg['stable_avg_latency_ms']:>6.2f} "
              f"{agg['latency_increase_ms']:>+5.1f}")
    
    elapsed = time.time() - start_time
    print(f"\nCompleted {run_count} runs in {elapsed:.1f}s")
    
    # 保存聚合结果 JSON
    json_results = {}
    for (model, speed, strategy), agg in all_results.items():
        key_str = f"{model}_s{speed}_{strategy}"
        json_results[key_str] = agg
    
    json_path = f"{OUTPUT_DIR}/aggregated_results.json"
    with open(json_path, 'w') as f:
        json.dump(json_results, f, indent=2, default=float)
    print(f"Saved aggregated results: {json_path}")
    
    # 保存实验配置
    config_info = {
        "topology": {
            "area_size": AREA_SIZE,
            "density": DENSITY,
            "coverage_radius": COVERAGE_RADIUS,
            "num_nodes": NUM_NODES,
            "tau": TAU,
            "actual_nodes": G.number_of_nodes(),
            "actual_edges": G.number_of_edges(),
            "num_regions": len(regions),
        },
        "experiment": {
            "movement_models": MOVEMENT_MODELS,
            "uav_speeds": UAV_SPEEDS,
            "leader_strategies": LEADER_STRATEGIES,
            "duration_s": DURATION_S,
            "request_interval_ms": REQUEST_INTERVAL_MS,
            "seeds": SEEDS,
            "mode": MODE,
        },
    }
    config_path = f"{OUTPUT_DIR}/experiment_config.json"
    with open(config_path, 'w') as f:
        json.dump(config_info, f, indent=2)
    print(f"Saved experiment config: {config_path}")
    
    print(f"\n{'='*80}")
    print(f"  Experiment complete! Run analyze_handover_results.py to generate figures.")
    print(f"{'='*80}")


if __name__ == "__main__":
    main()

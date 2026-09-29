"""
UAV Handover 仿真可视化验证
展示 UAV 轨迹、基站覆盖、handover 事件和 leader 分布
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '.'))

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import Circle, FancyArrowPatch
import networkx as nx

from src.topology.spatial_generator import SpatialTopologyGenerator
from src.topology.mec_builder import MECTopologyBuilder
from src.partition.strategies import PartitionStrategies
from src.simulation.uav_handover import (
    generate_handover_scenario, save_scenario
)
from src.visualization.plotter import COLORS


def setup_topology(num_nodes=15, seed=42):
    """生成测试拓扑"""
    gen = SpatialTopologyGenerator(area_size=2000, density=4e-6,
                                    coverage_radius=300, coverage_std=50,
                                    random_seed=seed)
    spatial = gen.generate_with_fixed_count(num_nodes)
    builder = MECTopologyBuilder()
    G = builder.build_from_spatial(spatial)
    
    if not nx.is_connected(G):
        G = builder.extract_largest_component(G, verbose=False)
        nodes_in_graph = set(G.nodes())
        spatial.base_stations = [bs for bs in spatial.base_stations if bs.id in nodes_in_graph]
        spatial.num_stations = len(spatial.base_stations)
    
    ps = PartitionStrategies(G)
    regions_list = ps.threshold_based(4)
    regions = {i: set(r) for i, r in enumerate(regions_list)}
    
    return spatial, G, regions


def plot_trajectory_with_handovers(scenario, spatial, regions, ax=None, title=""):
    """
    绘制 UAV 轨迹 + 基站覆盖 + handover 事件
    """
    if ax is None:
        fig, ax = plt.subplots(1, 1, figsize=(10, 10))
    
    area = spatial.area_size
    
    # 绘制基站覆盖范围（半透明圆）
    for bs in spatial.base_stations:
        circle = Circle((bs.x, bs.y), bs.coverage_radius,
                        fill=True, alpha=0.08, color='skyblue',
                        linewidth=1, edgecolor='steelblue', linestyle='--')
        ax.add_patch(circle)
    
    # 绘制基站位置
    leader_dist = scenario["leader_distribution"]
    for bs in spatial.base_stations:
        node_info = leader_dist.get(str(bs.id), {})
        num_leaders = node_info.get("num_leaders", 0)
        
        # leader 数量决定标记大小
        marker_size = 80 + num_leaders * 60
        color = 'red' if num_leaders > 0 else 'gray'
        
        ax.scatter(bs.x, bs.y, s=marker_size, c=color, 
                   marker='^', zorder=5, edgecolors='k', linewidths=1.5)
        ax.annotate(f"m{bs.id}\nL={num_leaders}", (bs.x, bs.y),
                    textcoords="offset points", xytext=(0, 12),
                    ha='center', fontsize=8, fontweight='bold')
    
    # 绘制 UAV 轨迹
    traj = scenario["trajectory"]
    xs = [p["x"] for p in traj]
    ys = [p["y"] for p in traj]
    ax.plot(xs, ys, 'b-', alpha=0.4, linewidth=1.5, label='UAV trajectory')
    
    # 起点和终点
    ax.scatter(xs[0], ys[0], s=200, c='green', marker='o', zorder=10, 
               edgecolors='k', linewidths=2, label='Start')
    ax.scatter(xs[-1], ys[-1], s=200, c='black', marker='s', zorder=10,
               edgecolors='k', linewidths=2, label='End')
    
    # 绘制 handover 事件
    events = scenario["handover_events"]
    for i, e in enumerate(events):
        px, py = e["position"]
        ax.scatter(px, py, s=150, c='orange', marker='*', zorder=8,
                   edgecolors='red', linewidths=1.5)
        ax.annotate(
            f"HO{i+1}\n{e['from_node']}→{e['to_node']}\nt={e['time_ms']/1000:.1f}s",
            (px, py), textcoords="offset points", xytext=(10, -15),
            fontsize=7, color='red', fontweight='bold',
            bbox=dict(boxstyle='round,pad=0.3', facecolor='yellow', alpha=0.7)
        )
    
    ax.set_xlim(-50, area + 50)
    ax.set_ylim(-50, area + 50)
    ax.set_aspect('equal')
    ax.legend(loc='upper right', fontsize=9)
    ax.set_title(title, fontsize=11, fontweight='bold')
    ax.set_xlabel('X (m)')
    ax.set_ylabel('Y (m)')
    
    return ax


def plot_handover_timeline(scenario, ax=None, title=""):
    """绘制 handover 时间线：展示连接节点随时间的变化"""
    if ax is None:
        fig, ax = plt.subplots(1, 1, figsize=(12, 4))
    
    traj = scenario["trajectory"]
    times = [p["time_ms"] / 1000 for p in traj]
    nodes = [p["node"] for p in traj]
    
    ax.step(times, nodes, where='post', linewidth=2, color='steelblue')
    
    # 标记 handover 事件
    events = scenario["handover_events"]
    for e in events:
        t = e["time_ms"] / 1000
        ax.axvline(x=t, color='red', linestyle='--', alpha=0.6, linewidth=1)
        ax.annotate(f"{e['from_node']}→{e['to_node']}\nΔL={e['to_leaders']-e['from_leaders']}",
                    (t, e['to_node']), textcoords="offset points", xytext=(5, 10),
                    fontsize=8, color='red', fontweight='bold')
    
    ax.set_xlabel('Time (s)', fontsize=10)
    ax.set_ylabel('Connected Node', fontsize=10)
    ax.set_title(title, fontsize=11, fontweight='bold')
    ax.grid(True, alpha=0.3)
    
    return ax


def plot_leader_comparison(scenarios, ax=None):
    """对比不同 leader 分布策略下的 handover 特征"""
    if ax is None:
        fig, ax = plt.subplots(1, 1, figsize=(10, 5))
    
    strategies = []
    leader_diffs = []
    
    for s in scenarios:
        strategy = s["leader_strategy"]
        events = s["handover_events"]
        diffs = [abs(e["to_leaders"] - e["from_leaders"]) for e in events]
        strategies.append(strategy)
        leader_diffs.append(diffs)
    
    # 箱线图
    bp = ax.boxplot(leader_diffs, labels=strategies, patch_artist=True)
    colors_box = ['#2196F3', '#FF9800', '#F44336']
    for patch, color in zip(bp['boxes'], colors_box):
        patch.set_facecolor(color)
        patch.set_alpha(0.6)
    
    ax.set_xlabel('Leader Distribution Strategy', fontsize=10)
    ax.set_ylabel('|ΔLeaders| per Handover', fontsize=10)
    ax.set_title('Leader Count Difference at Handover Events', fontsize=11, fontweight='bold')
    ax.grid(True, alpha=0.3, axis='y')
    
    return ax


def main():
    os.makedirs("output/handover", exist_ok=True)
    
    # 生成拓扑
    spatial, G, regions = setup_topology(num_nodes=15, seed=42)
    print(f"Topology: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges, "
          f"{len(regions)} regions")
    
    # =========================================================================
    # 图1：三种移动模型对比（固定速度=20m/s, balanced leader）
    # =========================================================================
    print("\n--- Figure 1: Movement Models Comparison ---")
    fig, axes = plt.subplots(1, 3, figsize=(18, 6))
    
    for i, model in enumerate(["linear", "rwp", "predefined"]):
        scenario = generate_handover_scenario(
            spatial, G, regions,
            uav_speed=20, duration_s=60,
            movement_model=model,
            leader_strategy="balanced",
            random_seed=42
        )
        plot_trajectory_with_handovers(
            scenario, spatial, regions, ax=axes[i],
            title=f"{model.upper()} (v=20m/s, {scenario['num_handovers']} HOs)"
        )
        print(f"  {model}: {scenario['num_handovers']} handovers "
              f"({scenario['handover_frequency_per_min']:.1f}/min)")
    
    plt.tight_layout()
    plt.savefig("output/handover/fig1_movement_models.png", dpi=150, bbox_inches='tight')
    plt.close()
    print("  Saved: output/handover/fig1_movement_models.png")
    
    # =========================================================================
    # 图2：不同速度下的 handover 频率
    # =========================================================================
    print("\n--- Figure 2: Speed vs Handover Frequency ---")
    speeds = [5, 10, 15, 20, 30, 40, 60]
    models = ["linear", "rwp", "predefined"]
    
    fig, ax = plt.subplots(1, 1, figsize=(8, 5))
    for model in models:
        freqs = []
        for speed in speeds:
            s = generate_handover_scenario(
                spatial, G, regions,
                uav_speed=speed, duration_s=60,
                movement_model=model,
                leader_strategy="balanced",
                random_seed=42
            )
            freqs.append(s["handover_frequency_per_min"])
        ax.plot(speeds, freqs, 'o-', linewidth=2, markersize=6, label=model)
    
    ax.set_xlabel('UAV Speed (m/s)', fontsize=11)
    ax.set_ylabel('Handover Frequency (per min)', fontsize=11)
    ax.set_title('Handover Frequency vs UAV Speed', fontsize=12, fontweight='bold')
    ax.legend(fontsize=10)
    ax.grid(True, alpha=0.3)
    
    plt.tight_layout()
    plt.savefig("output/handover/fig2_speed_vs_frequency.png", dpi=150, bbox_inches='tight')
    plt.close()
    print("  Saved: output/handover/fig2_speed_vs_frequency.png")
    
    # =========================================================================
    # 图3：Leader 分布策略对比
    # =========================================================================
    print("\n--- Figure 3: Leader Distribution Comparison ---")
    fig, axes = plt.subplots(2, 3, figsize=(18, 10))
    
    scenarios_for_comparison = []
    for i, strategy in enumerate(["balanced", "moderate", "skewed"]):
        scenario = generate_handover_scenario(
            spatial, G, regions,
            uav_speed=30, duration_s=60,
            movement_model="rwp",
            leader_strategy=strategy,
            random_seed=42
        )
        scenarios_for_comparison.append(scenario)
        
        # 上排：轨迹图
        plot_trajectory_with_handovers(
            scenario, spatial, regions, ax=axes[0, i],
            title=f"Leader: {strategy} ({scenario['num_handovers']} HOs)"
        )
        
        # 下排：时间线
        plot_handover_timeline(scenario, ax=axes[1, i],
                               title=f"Timeline ({strategy})")
        
        # 打印 leader 分布统计
        leaders_per_node = [info["num_leaders"] 
                           for info in scenario["leader_distribution"].values()]
        print(f"  {strategy}: leaders/node = {leaders_per_node}, "
              f"max={max(leaders_per_node)}, "
              f"std={np.std(leaders_per_node):.2f}")
    
    plt.tight_layout()
    plt.savefig("output/handover/fig3_leader_distribution.png", dpi=150, bbox_inches='tight')
    plt.close()
    print("  Saved: output/handover/fig3_leader_distribution.png")
    
    # =========================================================================
    # 图4：Leader 差异箱线图
    # =========================================================================
    print("\n--- Figure 4: Leader Difference at Handover ---")
    fig, ax = plt.subplots(1, 1, figsize=(8, 5))
    
    # 用更多 handover 事件（更高速度 + 更长时间）
    scenarios_high_freq = []
    for strategy in ["balanced", "moderate", "skewed"]:
        s = generate_handover_scenario(
            spatial, G, regions,
            uav_speed=40, duration_s=120,
            movement_model="rwp",
            leader_strategy=strategy,
            random_seed=42
        )
        scenarios_high_freq.append(s)
        print(f"  {strategy}: {s['num_handovers']} handovers in 120s")
    
    plot_leader_comparison(scenarios_high_freq, ax=ax)
    
    plt.tight_layout()
    plt.savefig("output/handover/fig4_leader_diff_boxplot.png", dpi=150, bbox_inches='tight')
    plt.close()
    print("  Saved: output/handover/fig4_leader_diff_boxplot.png")
    
    # =========================================================================
    # 保存一个完整场景的 JSON（供 Go 客户端使用）
    # =========================================================================
    print("\n--- Saving scenario JSON for Go client ---")
    scenario_for_go = generate_handover_scenario(
        spatial, G, regions,
        uav_speed=20, duration_s=60,
        movement_model="rwp",
        leader_strategy="balanced",
        random_seed=42
    )
    save_scenario(scenario_for_go, "output/handover/scenario_rwp_20_balanced.json")
    print("  Saved: output/handover/scenario_rwp_20_balanced.json")
    
    print(f"\n{'='*60}")
    print(f"  All handover visualizations completed!")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()

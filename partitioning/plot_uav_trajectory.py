"""
UAV 轨迹可视化
- 静态图：四种移动模型对比（论文级 PDF）
- 动画 GIF：UAV 运动过程 + 连接 MEC 节点标识

风格与 run.py / TopologyPlotter 一致
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '.'))

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.animation as animation
from matplotlib.collections import LineCollection
import networkx as nx

from src.topology.spatial_generator import SpatialTopologyGenerator
from src.topology.mec_builder import MECTopologyBuilder
from src.partition.strategies import PartitionStrategies
from src.simulation.uav_handover import generate_handover_scenario
from src.visualization.plotter import COLORS


# =============================================================================
# 风格参数（与 plotter.py 一致）
# =============================================================================

NODE_COLOR = '#87a8d7'
COVERAGE_ALPHA = 0.15
NODE_SIZE = 120          # 比 run.py 的 700 小（空间图中节点更小）
NODE_EDGE_COLOR = 'white'
NODE_LINEWIDTH = 1.5
LABEL_FONTSIZE = 12
AXIS_FONTSIZE = 14
GRID_ALPHA = 0.25


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


def draw_base_topology(ax, spatial, highlight_node=None):
    """
    绘制底图：基站覆盖 + 基站位置 + 标签
    风格与 TopologyPlotter.plot_spatial_topology 一致
    """
    area = spatial.area_size
    
    # 覆盖范围（半透明圆，无边框）
    for bs in spatial.base_stations:
        circle = mpatches.Circle(
            (bs.x, bs.y), bs.coverage_radius,
            fill=True, facecolor=NODE_COLOR, edgecolor='none',
            alpha=COVERAGE_ALPHA, linewidth=0
        )
        ax.add_patch(circle)
    
    # 基站位置
    for bs in spatial.base_stations:
        is_highlight = (highlight_node is not None and bs.id == highlight_node)
        size = NODE_SIZE * 2.5 if is_highlight else NODE_SIZE
        edge_color = 'red' if is_highlight else NODE_EDGE_COLOR
        lw = 3 if is_highlight else NODE_LINEWIDTH
        zorder = 6 if is_highlight else 4
        
        ax.scatter(bs.x, bs.y, s=size, c=NODE_COLOR, marker='o',
                   edgecolors=edge_color, linewidths=lw, zorder=zorder)
    
    # 标签
    for bs in spatial.base_stations:
        ax.text(bs.x, bs.y + bs.coverage_radius * 0.15,
                f"m{bs.id}", ha='center', va='bottom',
                fontsize=LABEL_FONTSIZE, fontweight='bold')
    
    # 坐标轴
    ax.set_xlim(-50, area + 50)
    ax.set_ylim(-50, area + 50)
    ax.set_xlabel('X (m)', fontweight='bold', fontsize=AXIS_FONTSIZE)
    ax.set_ylabel('Y (m)', fontweight='bold', fontsize=AXIS_FONTSIZE)
    ax.set_aspect('equal')
    ax.grid(True, alpha=GRID_ALPHA, linestyle='--', color='gray')


def draw_trajectory(ax, scenario, spatial, show_connection_line=True):
    """
    在底图上绘制 UAV 轨迹
    
    - 轨迹线段按连接节点着色
    - Handover 事件标记
    - 起点/终点标记
    """
    traj = scenario["trajectory"]
    events = scenario["handover_events"]
    
    if not traj:
        return
    
    # 为每个节点分配颜色
    all_nodes = sorted(set(p["node"] for p in traj))
    node_color_map = {n: COLORS[i % len(COLORS)] for i, n in enumerate(all_nodes)}
    
    # 绘制轨迹（按连接节点分段着色）
    xs = [p["x"] for p in traj]
    ys = [p["y"] for p in traj]
    nodes = [p["node"] for p in traj]
    
    # 分段绘制
    seg_start = 0
    for i in range(1, len(traj)):
        if nodes[i] != nodes[seg_start] or i == len(traj) - 1:
            end = i if nodes[i] != nodes[seg_start] else i + 1
            seg_xs = xs[seg_start:end]
            seg_ys = ys[seg_start:end]
            color = node_color_map[nodes[seg_start]]
            ax.plot(seg_xs, seg_ys, '-', color=color, linewidth=2.5,
                    alpha=0.7, zorder=3)
            seg_start = i
    
    # 连接虚线（UAV → 当前 MEC 节点，仅在 handover 时刻画）
    if show_connection_line and events:
        for e in events:
            px, py = e["position"]
            to_node = e["to_node"]
            # 找到目标节点位置
            for bs in spatial.base_stations:
                if bs.id == to_node:
                    ax.plot([px, bs.x], [py, bs.y], '--', color='red',
                            linewidth=1.5, alpha=0.6, zorder=2)
                    break
    
    # Handover 事件标记
    for i, e in enumerate(events):
        px, py = e["position"]
        ax.scatter(px, py, s=200, c='red', marker='*', zorder=8,
                   edgecolors='darkred', linewidths=1)
        ax.annotate(f"HO{i+1}", (px, py),
                    textcoords="offset points", xytext=(8, 8),
                    fontsize=9, color='red', fontweight='bold',
                    bbox=dict(boxstyle='round,pad=0.2', facecolor='yellow',
                              alpha=0.7, edgecolor='none'))
    
    # 起点（绿色圆）
    ax.scatter(xs[0], ys[0], s=250, c='#4CAF50', marker='o', zorder=9,
               edgecolors='darkgreen', linewidths=2, label='Start')
    
    # 终点（黑色方块）
    ax.scatter(xs[-1], ys[-1], s=200, c='black', marker='s', zorder=9,
               edgecolors='black', linewidths=2, label='End')
    
    # 图例（轨迹颜色 → 节点映射）
    legend_patches = [
        mpatches.Patch(color=node_color_map[n], label=f'm{n}')
        for n in all_nodes
    ]
    legend_patches.append(plt.Line2D([0], [0], marker='*', color='w',
                                      markerfacecolor='red', markersize=12,
                                      label='Handover'))
    legend_patches.append(plt.Line2D([0], [0], marker='o', color='w',
                                      markerfacecolor='#4CAF50', markersize=10,
                                      label='Start'))
    legend_patches.append(plt.Line2D([0], [0], marker='s', color='w',
                                      markerfacecolor='black', markersize=8,
                                      label='End'))
    ax.legend(handles=legend_patches, loc='upper right', fontsize=8,
              ncol=2, framealpha=0.8)


# =============================================================================
# 静态图：四种移动模型对比
# =============================================================================

def plot_trajectory_comparison(spatial, G, regions, output_dir="output/handover_figures"):
    """生成四种移动模型的轨迹对比图"""
    os.makedirs(output_dir, exist_ok=True)
    
    models = ["linear", "rwp", "gauss_markov", "predefined"]
    titles = ["(a) Linear", "(b) Random Waypoint", 
              "(c) Gauss-Markov", "(d) Predefined Path"]
    
    fig, axes = plt.subplots(2, 2, figsize=(14, 14))
    axes = axes.flatten()
    
    for i, (model, title) in enumerate(zip(models, titles)):
        ax = axes[i]
        
        scenario = generate_handover_scenario(
            spatial, G, regions,
            uav_speed=30, duration_s=60,
            movement_model=model,
            leader_strategy="balanced",
            random_seed=42,
        )
        
        draw_base_topology(ax, spatial)
        draw_trajectory(ax, scenario, spatial)
        
        # 子图标题（论文风格：小字，左下角）
        num_ho = len(scenario["handover_events"])
        ax.text(0.02, 0.98, f"{title}\nv=30 m/s, {num_ho} handovers",
                transform=ax.transAxes, fontsize=11, fontweight='bold',
                va='top', ha='left',
                bbox=dict(boxstyle='round', facecolor='white', alpha=0.8))
    
    plt.tight_layout(pad=2.0)
    plt.savefig(f"{output_dir}/trajectory_comparison.png", dpi=150, bbox_inches='tight')
    plt.savefig(f"{output_dir}/trajectory_comparison.pdf", dpi=300, bbox_inches='tight')
    plt.close()
    print(f"  Saved: {output_dir}/trajectory_comparison.png/pdf")


# =============================================================================
# 动画 GIF：UAV 运动过程
# =============================================================================

def create_trajectory_animation(spatial, G, regions, 
                                 model="gauss_markov", speed=30,
                                 output_dir="output/handover_figures",
                                 fps=10, duration_s=60,
                                 speedup=10):
    """
    生成 UAV 轨迹动画 GIF
    
    Parameters
    ----------
    speedup : int
        加速倍率（10 = 60s 仿真压缩为 6s 动画）
    """
    os.makedirs(output_dir, exist_ok=True)
    
    scenario = generate_handover_scenario(
        spatial, G, regions,
        uav_speed=speed, duration_s=duration_s,
        movement_model=model,
        leader_strategy="balanced",
        random_seed=42,
    )
    
    traj = scenario["trajectory"]
    events = scenario["handover_events"]
    sample_interval = scenario["sample_interval_ms"]
    
    if not traj:
        print("  No trajectory data")
        return
    
    # 为每个节点分配颜色
    all_nodes = sorted(set(p["node"] for p in traj))
    node_color_map = {n: COLORS[i % len(COLORS)] for i, n in enumerate(all_nodes)}
    
    # 计算帧数和采样
    total_frames = int(duration_s * fps / speedup)
    points_per_frame = len(traj) // total_frames
    
    # 创建图
    fig, ax = plt.subplots(1, 1, figsize=(10, 10))
    
    # Handover 事件时间
    ho_times = set(e["time_ms"] for e in events)
    
    def init():
        ax.clear()
        draw_base_topology(ax, spatial)
        return []
    
    def animate(frame):
        ax.clear()
        
        # 当前时间点
        end_idx = min((frame + 1) * points_per_frame, len(traj))
        current_traj = traj[:end_idx]
        
        if not current_traj:
            draw_base_topology(ax, spatial)
            return []
        
        current_point = current_traj[-1]
        current_node = current_point["node"]
        current_time = current_point["time_ms"]
        
        # 绘制底图（高亮当前连接节点）
        draw_base_topology(ax, spatial, highlight_node=current_node)
        
        # 绘制已走过的轨迹（按节点着色）
        if len(current_traj) > 1:
            for seg_start_idx in range(len(current_traj) - 1):
                p1 = current_traj[seg_start_idx]
                p2 = current_traj[seg_start_idx + 1]
                color = node_color_map.get(p1["node"], 'gray')
                alpha = 0.3 + 0.5 * (seg_start_idx / len(current_traj))
                ax.plot([p1["x"], p2["x"]], [p1["y"], p2["y"]],
                        '-', color=color, linewidth=2, alpha=alpha, zorder=3)
        
        # 绘制 UAV 当前位置
        ax.scatter(current_point["x"], current_point["y"],
                   s=200, c='red', marker='D', zorder=10,
                   edgecolors='darkred', linewidths=2)
        
        # 连接虚线（UAV → 当前 MEC 节点）
        for bs in spatial.base_stations:
            if bs.id == current_node:
                ax.plot([current_point["x"], bs.x],
                        [current_point["y"], bs.y],
                        '--', color='red', linewidth=2, alpha=0.8, zorder=5)
                break
        
        # 标记已发生的 handover 事件
        for i, e in enumerate(events):
            if e["time_ms"] <= current_time:
                px, py = e["position"]
                ax.scatter(px, py, s=150, c='orange', marker='*', zorder=7,
                           edgecolors='red', linewidths=1)
                ax.annotate(f"HO{i+1}", (px, py),
                            textcoords="offset points", xytext=(6, 6),
                            fontsize=8, color='red', fontweight='bold')
        
        # 状态信息
        info_text = (f"t = {current_time/1000:.1f}s\n"
                     f"Connected: m{current_node}\n"
                     f"Handovers: {sum(1 for e in events if e['time_ms'] <= current_time)}")
        ax.text(0.02, 0.98, info_text, transform=ax.transAxes,
                fontsize=11, fontweight='bold', va='top', ha='left',
                bbox=dict(boxstyle='round', facecolor='white', alpha=0.9))
        
        # 进度条
        progress = current_time / (duration_s * 1000)
        bar_y = -30
        ax.plot([0, spatial.area_size * progress], [bar_y, bar_y],
                '-', color='steelblue', linewidth=4, solid_capstyle='round')
        ax.plot([0, spatial.area_size], [bar_y, bar_y],
                '-', color='lightgray', linewidth=4, solid_capstyle='round', zorder=1)
        
        return []
    
    print(f"  Generating animation: {total_frames} frames @ {fps} fps...")
    anim = animation.FuncAnimation(
        fig, animate, init_func=init,
        frames=total_frames, interval=1000//fps, blit=False
    )
    
    # 保存 HTML5 交互式动画
    html_path = f"{output_dir}/trajectory_{model}_v{speed}.html"
    html_content = anim.to_jshtml(fps=fps)
    with open(html_path, 'w') as f:
        f.write(html_content)
    plt.close()
    print(f"  Saved: {html_path}")
    
    return html_path


# =============================================================================
# 自定义途径点轨迹
# =============================================================================

def plot_custom_waypoints(spatial, G, regions, waypoints,
                          speed=20, duration_s=60,
                          leader_strategy="balanced", seed=42,
                          output_dir="output/handover_figures",
                          output_name="trajectory_custom",
                          make_gif=True, fps=10, speedup=10,
                          switch_prob=0.0, mec_nodes_info=None):
    """
    根据用户指定的途径点绘制 UAV 轨迹
    
    Parameters
    ----------
    waypoints : List
        途径点列表，支持两种格式：
        - 坐标元组: [(x1, y1), (x2, y2), ...]
        - 基站 ID: [3, 7, 12, 0]（自动转换为基站坐标）
        - 混合: [(100, 200), 3, (500, 800), 7]
    speed : float
        UAV 飞行速度 (m/s)
    duration_s : float
        飞行时长 (秒)
    leader_strategy : str
        Leader 分布策略
    output_name : str
        输出文件名前缀
    make_gif : bool
        是否生成动画 GIF
    switch_prob : float
        重叠覆盖区域随机切换概率 (0=不切换, 0.3=30%概率切换)
    """
    os.makedirs(output_dir, exist_ok=True)
    
    # 解析 waypoints：将基站 ID 转换为坐标
    bs_map = {bs.id: (bs.x, bs.y) for bs in spatial.base_stations}
    resolved_waypoints = []
    waypoint_labels = []
    
    for wp in waypoints:
        if isinstance(wp, (int, np.integer)):
            # 基站 ID
            if wp in bs_map:
                resolved_waypoints.append(bs_map[wp])
                waypoint_labels.append(f"m{wp}")
            else:
                print(f"  Warning: node {wp} not found, skipping")
                continue
        elif isinstance(wp, (tuple, list)) and len(wp) == 2:
            # 坐标
            resolved_waypoints.append((float(wp[0]), float(wp[1])))
            waypoint_labels.append(f"({wp[0]:.0f},{wp[1]:.0f})")
        else:
            print(f"  Warning: invalid waypoint {wp}, skipping")
            continue
    
    if len(resolved_waypoints) < 2:
        print("  Error: need at least 2 waypoints")
        return None
    
    print(f"  Waypoints ({len(resolved_waypoints)}):")
    for i, (wp, label) in enumerate(zip(resolved_waypoints, waypoint_labels)):
        print(f"    WP{i+1}: {label} → ({wp[0]:.0f}, {wp[1]:.0f})")
    
    # 计算总路程，自动调整 duration_s 确保能飞完全程
    total_dist = sum(
        np.sqrt((resolved_waypoints[i+1][0] - resolved_waypoints[i][0])**2 +
                (resolved_waypoints[i+1][1] - resolved_waypoints[i][1])**2)
        for i in range(len(resolved_waypoints) - 1)
    )
    # 需要的时间 = 路程/速度 + 每个 waypoint 的到达误差补偿
    min_duration = total_dist / speed + len(resolved_waypoints) * 0.5
    if duration_s <= 0 or duration_s < min_duration:
        print(f"  Auto-adjusting duration: {duration_s:.0f}s → {min_duration:.0f}s "
              f"(distance={total_dist:.0f}m, speed={speed}m/s)")
        duration_s = min_duration
    
    # 生成轨迹
    from src.simulation.uav_handover import (
        UAVTrajectoryGenerator, HandoverDecider, TrajectoryPoint
    )
    
    # --- 坐标转换器（真实拓扑时将 spatial 米坐标转为 Mercator） ---
    _coord_converter = None
    if mec_nodes_info is not None:
        try:
            from pyproj import Transformer
            _transformer_to_merc = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
            _center_lon = np.mean([m['lon'] for m in mec_nodes_info])
            _center_lat = np.mean([m['lat'] for m in mec_nodes_info])
            # 用第一个节点校准 spatial→Mercator 的偏移
            _mec0 = mec_nodes_info[0]
            _bs0 = next(bs for bs in spatial.base_stations if bs.id == _mec0['id'])
            _merc0_x, _merc0_y = _transformer_to_merc.transform(_mec0['lon'], _mec0['lat'])
            # spatial 坐标 → 经纬度 → Mercator
            _x0_from_lonlat = (_mec0['lon'] - _center_lon) * 111320 * np.cos(np.radians(_center_lat))
            _y0_from_lonlat = (_mec0['lat'] - _center_lat) * 110540
            _offset_x = _bs0.x - _x0_from_lonlat
            _offset_y = _bs0.y - _y0_from_lonlat

            def _coord_converter(sx, sy):
                """spatial 米坐标 → Mercator 坐标"""
                rel_x = sx - _offset_x
                rel_y = sy - _offset_y
                lon = rel_x / (111320 * np.cos(np.radians(_center_lat))) + _center_lon
                lat = rel_y / 110540 + _center_lat
                mx, my = _transformer_to_merc.transform(lon, lat)
                return mx, my
        except ImportError:
            pass

    def _to_plot_coords(x, y):
        """将 spatial 坐标转为绘图坐标（真实拓扑时转 Mercator）"""
        if _coord_converter:
            return _coord_converter(x, y)
        return x, y

    gen = UAVTrajectoryGenerator(spatial, speed_mps=speed, random_seed=seed)
    trajectory = gen.predefined_path(resolved_waypoints, duration_s=duration_s)
    
    # 真实拓扑：用基站级别覆盖判定（UAV 在 MEC 下属任一基站覆盖圆内即视为在该 MEC 覆盖范围）
    if mec_nodes_info is not None and _coord_converter is not None:
        try:
            from pyproj import Transformer
            _t = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
            
            # 构建基站级别的 Mercator 坐标和覆盖半径，映射到所属 MEC
            bs_merc_positions = []  # 每个基站的 Mercator 坐标
            bs_merc_radii = []      # 每个基站的覆盖半径
            bs_to_mec = []          # 每个基站所属的 MEC ID
            for mec in mec_nodes_info:
                for bs in mec.get('base_stations', []):
                    bx, by = _t.transform(bs['lon'], bs['lat'])
                    bs_merc_positions.append([bx, by])
                    bs_merc_radii.append(bs.get('coverage_radius', 300))
                    bs_to_mec.append(mec['id'])
            
            # 如果没有基站信息，回退到 MEC 中心点
            if not bs_merc_positions:
                for mec in mec_nodes_info:
                    mx, my = _t.transform(mec['lon'], mec['lat'])
                    bs_merc_positions.append([mx, my])
                    bs_merc_radii.append(mec.get('coverage_radius', 300))
                    bs_to_mec.append(mec['id'])
            
            bs_merc_positions = np.array(bs_merc_positions)
            bs_merc_radii = np.array(bs_merc_radii)
            
            # 将轨迹点转为 Mercator 并用基站级别覆盖重新判定
            for i, pt in enumerate(trajectory):
                mx, my = _coord_converter(pt.x, pt.y)
                # 计算到每个基站的距离
                dists = np.sqrt((bs_merc_positions[:, 0] - mx)**2 + (bs_merc_positions[:, 1] - my)**2)
                in_coverage = np.where(dists <= bs_merc_radii)[0]
                if len(in_coverage) > 0:
                    # 找到覆盖该点的所有 MEC，选最近基站所属的 MEC
                    best_bs_idx = in_coverage[np.argmin(dists[in_coverage])]
                    new_node = bs_to_mec[best_bs_idx]
                else:
                    new_node = -1
                trajectory[i] = TrajectoryPoint(pt.time_ms, pt.x, pt.y, new_node)
            
            print(f"  Base-station-level Mercator coverage re-evaluation done")
        except ImportError:
            pass
    
    # 提取 handover 事件
    regions_dict = regions if isinstance(regions, dict) else {i: set(r) for i, r in enumerate(regions)}
    decider = HandoverDecider(G, regions_dict, hysteresis_ms=300)
    events = decider.extract_handovers(trajectory)
    leader_map = decider.generate_leader_distribution(leader_strategy)
    events = decider.enrich_with_leader_info(events, leader_map)
    
    # 构建 scenario 格式（兼容现有绘图函数）
    scenario = {
        "scenario": f"custom_v{speed}_{leader_strategy}",
        "movement_model": "custom",
        "uav_speed_mps": speed,
        "total_duration_s": duration_s,
        "sample_interval_ms": gen.sample_interval_ms,
        "num_handovers": len(events),
        "handover_events": [
            {
                "time_ms": e.time_ms,
                "from_node": e.from_node,
                "to_node": e.to_node,
                "position": e.position,
                "from_leaders": e.from_leaders,
                "to_leaders": e.to_leaders,
                "to_regions": e.to_regions,
                "to_is_leader_of": e.to_is_leader_of,
            }
            for e in events
        ],
        "leader_distribution": {
            str(node): {
                "leader_of": leader_map.get(node, []),
                "num_leaders": len(leader_map.get(node, [])),
            }
            for node in G.nodes()
        },
        "trajectory": [
            {"time_ms": p.time_ms, "x": p.x, "y": p.y, "node": p.connected_node}
            for p in trajectory
        ],
    }
    
    # 在重叠覆盖区域注入随机切换 (jitter)
    if switch_prob > 0:
        rng = np.random.RandomState(seed)
        
        # 真实拓扑用基站级别 Mercator 坐标判定覆盖
        if mec_nodes_info is not None and _coord_converter is not None:
            try:
                from pyproj import Transformer
                _t = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
                _bs_merc_pos = []
                _bs_merc_rad = []
                _bs_to_mec_id = []
                for mec in mec_nodes_info:
                    for bs in mec.get('base_stations', []):
                        bx, by = _t.transform(bs['lon'], bs['lat'])
                        _bs_merc_pos.append([bx, by])
                        _bs_merc_rad.append(bs.get('coverage_radius', 300))
                        _bs_to_mec_id.append(mec['id'])
                if not _bs_merc_pos:
                    for mec in mec_nodes_info:
                        mx, my = _t.transform(mec['lon'], mec['lat'])
                        _bs_merc_pos.append([mx, my])
                        _bs_merc_rad.append(mec.get('coverage_radius', 300))
                        _bs_to_mec_id.append(mec['id'])
                _bs_merc_pos = np.array(_bs_merc_pos)
                _bs_merc_rad = np.array(_bs_merc_rad)
                use_bs_level_jitter = True
            except ImportError:
                use_bs_level_jitter = False
        else:
            use_bs_level_jitter = False
        
        if not use_bs_level_jitter:
            bs_positions = np.array([[bs.x, bs.y] for bs in spatial.base_stations])
            bs_radii = np.array([bs.coverage_radius for bs in spatial.base_stations])
            bs_ids = [bs.id for bs in spatial.base_stations]
        
        current_node = scenario["trajectory"][0]["node"]
        jitter_switches = 0
        for pt in scenario["trajectory"]:
            x, y = pt["x"], pt["y"]
            
            if use_bs_level_jitter:
                mx, my = _coord_converter(x, y)
                dists = np.sqrt((_bs_merc_pos[:, 0] - mx)**2 + (_bs_merc_pos[:, 1] - my)**2)
                in_cov_idx = np.where(dists <= _bs_merc_rad)[0]
                # 找到覆盖该点的所有 MEC（去重）
                covering_mecs = []
                seen = set()
                for idx in in_cov_idx[np.argsort(dists[in_cov_idx])]:
                    mec_id = _bs_to_mec_id[idx]
                    if mec_id not in seen:
                        covering_mecs.append(mec_id)
                        seen.add(mec_id)
                covering = covering_mecs
            else:
                dists = np.sqrt((bs_positions[:, 0] - x)**2 + (bs_positions[:, 1] - y)**2)
                in_coverage = np.where(dists <= bs_radii)[0]
                covering = [bs_ids[idx] for idx in in_coverage[np.argsort(dists[in_coverage])]]
            
            if not covering:
                current_node = pt["node"]
            elif len(covering) > 1 and current_node in covering:
                if rng.random() < switch_prob:
                    others = [n for n in covering if n != current_node]
                    current_node = rng.choice(others)
            elif current_node not in covering:
                current_node = covering[0]
            
            pt["node"] = current_node
            if pt["node"] != scenario["trajectory"][0]["node"]:
                jitter_switches += 1
        
        # 重新提取 handover 事件
        from src.simulation.uav_handover import TrajectoryPoint
        jittered_trajectory = [
            TrajectoryPoint(pt["time_ms"], pt["x"], pt["y"], pt["node"])
            for pt in scenario["trajectory"]
        ]
        events = decider.extract_handovers(jittered_trajectory)
        events = decider.enrich_with_leader_info(events, leader_map)
        scenario["handover_events"] = [
            {
                "time_ms": e.time_ms,
                "from_node": e.from_node,
                "to_node": e.to_node,
                "position": e.position,
                "from_leaders": e.from_leaders,
                "to_leaders": e.to_leaders,
                "to_regions": e.to_regions,
                "to_is_leader_of": e.to_is_leader_of,
            }
            for e in events
        ]
        scenario["num_handovers"] = len(events)
        print(f"  Jitter applied: switch_prob={switch_prob:.0%}, {len(events)} handovers")
    
    # --- 底图绘制函数（真实拓扑用 plot_real_spatial_topology） ---
    def _draw_base(ax, highlight_node=None):
        if mec_nodes_info is not None:
            from src.visualization.plotter import TopologyPlotter, COLORS
            plotter = TopologyPlotter(figsize=(10, 10))
            plotter.plot_real_spatial_topology(
                spatial, mec_nodes_info=mec_nodes_info,
                mec_graph=G, mec_color=COLORS[0],
                show=False, ax=ax
            )
            legend = ax.get_legend()
            if legend:
                legend.remove()
            # 高亮节点
            if highlight_node is not None:
                try:
                    from pyproj import Transformer
                    transformer = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
                    mec = next(m for m in mec_nodes_info if m['id'] == highlight_node)
                    hx, hy = transformer.transform(mec['lon'], mec['lat'])
                    ax.scatter(hx, hy, s=500, c='red', marker='o',
                               edgecolors='darkred', linewidths=3, zorder=10)
                except (ImportError, StopIteration):
                    pass
        else:
            draw_base_topology(ax, spatial, highlight_node=highlight_node)

    # --- 静态图 ---
    fig, ax = plt.subplots(1, 1, figsize=(10, 10))
    _draw_base(ax)
    if _coord_converter is None:
        draw_trajectory(ax, scenario, spatial)
    else:
        # 真实拓扑：手动绘制轨迹（坐标需要转换）
        traj = scenario["trajectory"]
        if len(traj) > 1:
            for i in range(len(traj) - 1):
                p1x, p1y = _to_plot_coords(traj[i]["x"], traj[i]["y"])
                p2x, p2y = _to_plot_coords(traj[i+1]["x"], traj[i+1]["y"])
                node = traj[i]["node"]
                color = COLORS[node % len(COLORS)] if node != -1 else 'lightgray'
                ax.plot([p1x, p2x], [p1y, p2y], '-', color=color, linewidth=2, alpha=0.7, zorder=3)
        # handover 标记
        for e in scenario.get("handover_events", []):
            hx, hy = _to_plot_coords(*e["position"])
            ax.scatter(hx, hy, s=150, c='orange', marker='*', zorder=7,
                       edgecolors='red', linewidths=1)
        # 起点/终点
        if traj:
            sx, sy = _to_plot_coords(traj[0]["x"], traj[0]["y"])
            ex, ey = _to_plot_coords(traj[-1]["x"], traj[-1]["y"])
            ax.scatter(sx, sy, s=200, c='green', marker='^', zorder=8, edgecolors='darkgreen', linewidths=2)
            ax.scatter(ex, ey, s=200, c='red', marker='v', zorder=8, edgecolors='darkred', linewidths=2)
    
    # 标记 waypoints（蓝色菱形，不标文字）
    # for i, (wp, label) in enumerate(zip(resolved_waypoints, waypoint_labels)):
    #     px, py = _to_plot_coords(wp[0], wp[1])
    #     ax.scatter(px, py, s=150, c='blue', marker='D', zorder=11,
    #                edgecolors='darkblue', linewidths=1.5, alpha=0.8)
    
    # 绘制 waypoint 之间的规划路线（灰色虚线）
    for i in range(len(resolved_waypoints) - 1):
        wp1 = resolved_waypoints[i]
        wp2 = resolved_waypoints[i + 1]
        p1x, p1y = _to_plot_coords(wp1[0], wp1[1])
        p2x, p2y = _to_plot_coords(wp2[0], wp2[1])
        ax.plot([p1x, p2x], [p1y, p2y], 
                ':', color='gray', linewidth=1.5, alpha=0.5, zorder=2)
    
    # 信息标注
    info = f"Custom Path | v={speed} m/s | {len(events)} handovers"
    ax.text(0.02, 0.98, info, transform=ax.transAxes,
            fontsize=10, fontweight='bold', va='top', ha='left',
            bbox=dict(boxstyle='round', facecolor='white', alpha=0.85))
    
    plt.tight_layout()
    png_path = f"{output_dir}/{output_name}.png"
    pdf_path = f"{output_dir}/{output_name}.pdf"
    plt.savefig(png_path, dpi=150, bbox_inches='tight')
    plt.savefig(pdf_path, dpi=300, bbox_inches='tight')
    plt.close()
    print(f"  Saved: {png_path}")
    
    # --- 动画 GIF ---
    if make_gif:
        traj_data = scenario["trajectory"]
        
        # 找到 UAV 到达最后一个 waypoint 的时间点（之后裁剪）
        # 从末尾往前找第一个远离终点的点，但至少保留 80% 的轨迹
        last_wp = resolved_waypoints[-1]
        min_keep = int(len(traj_data) * 0.8)  # 至少保留 80%
        arrival_idx = len(traj_data)
        for idx in range(len(traj_data) - 1, min_keep, -1):
            p = traj_data[idx]
            dist = np.sqrt((p["x"] - last_wp[0])**2 + (p["y"] - last_wp[1])**2)
            if dist > 5.0:
                arrival_idx = min(idx + 10, len(traj_data))  # 到达后多留几帧
                break
        traj_data = traj_data[:arrival_idx]
        
        actual_duration = traj_data[-1]["time_ms"] / 1000 if traj_data else duration_s
        total_frames = max(1, int(actual_duration * fps / speedup))
        points_per_frame = max(1, len(traj_data) // total_frames)
        
        all_nodes = sorted(set(p["node"] for p in traj_data if p["node"] != -1))
        node_color_map = {n: COLORS[i % len(COLORS)] for i, n in enumerate(all_nodes)}
        
        fig, ax = plt.subplots(1, 1, figsize=(10, 10))
        
        def init():
            ax.clear()
            _draw_base(ax)
            # 保存 axes 范围供后续帧使用
            init._xlim = ax.get_xlim()
            init._ylim = ax.get_ylim()
            return []
        
        def animate(frame):
            ax.clear()
            end_idx = min((frame + 1) * points_per_frame, len(traj_data))
            current_traj = traj_data[:end_idx]
            
            if not current_traj:
                _draw_base(ax)
                ax.set_xlim(init._xlim)
                ax.set_ylim(init._ylim)
                return []
            
            current_point = current_traj[-1]
            current_node = current_point["node"]
            current_time = current_point["time_ms"]
            
            # 底图（高亮当前节点）
            highlight = current_node if current_node != -1 else None
            _draw_base(ax, highlight_node=highlight)
            ax.set_xlim(init._xlim)
            ax.set_ylim(init._ylim)
            
            # Waypoints 标记（已禁用）
            # for i, (wp, label) in enumerate(zip(resolved_waypoints, waypoint_labels)):
            #     px, py = _to_plot_coords(wp[0], wp[1])
            #     ax.scatter(px, py, s=100, c='blue', marker='D', zorder=11,
            #                edgecolors='darkblue', linewidths=1, alpha=0.6)
            
            # 规划路线
            for i in range(len(resolved_waypoints) - 1):
                wp1 = resolved_waypoints[i]
                wp2 = resolved_waypoints[i + 1]
                p1x, p1y = _to_plot_coords(wp1[0], wp1[1])
                p2x, p2y = _to_plot_coords(wp2[0], wp2[1])
                ax.plot([p1x, p2x], [p1y, p2y],
                        ':', color='gray', linewidth=1.5, alpha=0.4, zorder=2)
            
            # 已走轨迹
            if len(current_traj) > 1:
                for seg_idx in range(len(current_traj) - 1):
                    p1 = current_traj[seg_idx]
                    p2 = current_traj[seg_idx + 1]
                    node = p1["node"]
                    color = node_color_map.get(node, 'gray') if node != -1 else 'lightgray'
                    alpha = 0.3 + 0.5 * (seg_idx / len(current_traj))
                    p1x, p1y = _to_plot_coords(p1["x"], p1["y"])
                    p2x, p2y = _to_plot_coords(p2["x"], p2["y"])
                    ax.plot([p1x, p2x], [p1y, p2y],
                            '-', color=color, linewidth=2, alpha=alpha, zorder=3)
            
            # UAV 当前位置
            uav_color = 'red' if current_node != -1 else 'gray'
            uav_px, uav_py = _to_plot_coords(current_point["x"], current_point["y"])
            ax.scatter(uav_px, uav_py,
                       s=200, c=uav_color, marker='D', zorder=10,
                       edgecolors='darkred', linewidths=2)
            
            # 连接虚线
            if current_node != -1:
                for bs in spatial.base_stations:
                    if bs.id == current_node:
                        bs_px, bs_py = _to_plot_coords(bs.x, bs.y)
                        ax.plot([uav_px, bs_px],
                                [uav_py, bs_py],
                                '--', color='red', linewidth=2, alpha=0.8, zorder=5)
                        break
            
            # 已发生的 handover
            ho_events = scenario["handover_events"]
            for i, e in enumerate(ho_events):
                if e["time_ms"] <= current_time:
                    px, py = e["position"]
                    hpx, hpy = _to_plot_coords(px, py)
                    ax.scatter(hpx, hpy, s=150, c='orange', marker='*', zorder=7,
                               edgecolors='red', linewidths=1)
            
            # 状态信息
            conn_str = f"m{current_node}" if current_node != -1 else "DISCONNECTED"
            info_text = (f"t = {current_time/1000:.1f}s\n"
                         f"Connected: {conn_str}\n"
                         f"Handovers: {sum(1 for e in ho_events if e['time_ms'] <= current_time)}")
            ax.text(0.02, 0.98, info_text, transform=ax.transAxes,
                    fontsize=11, fontweight='bold', va='top', ha='left',
                    bbox=dict(boxstyle='round', facecolor='white', alpha=0.9))
            
            # 进度条（使用 axes 坐标范围）
            # actual_total_ms = traj_data[-1]["time_ms"] if traj_data else duration_s * 1000
            progress = current_time / (duration_s * 2000)
            xlim = ax.get_xlim()
            ylim = ax.get_ylim()
            bar_x_start = xlim[0]
            bar_x_end = xlim[1]
            bar_y = ylim[0] - (ylim[1] - ylim[0]) * 0.02
            ax.plot([bar_x_start, bar_x_start + (bar_x_end - bar_x_start) * progress], [bar_y, bar_y],
                    '-', color='steelblue', linewidth=4, solid_capstyle='round')
            ax.plot([bar_x_start, bar_x_end], [bar_y, bar_y],
                    '-', color='lightgray', linewidth=4, solid_capstyle='round', zorder=1)
            
            return []
        
        print(f"  Generating animation: {total_frames} frames...")
        anim = animation.FuncAnimation(
            fig, animate, init_func=init,
            frames=total_frames, interval=1000//fps, blit=False
        )
        html_path = f"{output_dir}/{output_name}.html"
        html_content = anim.to_jshtml(fps=fps)
        with open(html_path, 'w') as f:
            f.write(html_content)
        plt.close()
        print(f"  Saved: {html_path}")
    
    return scenario


# =============================================================================
# 主函数
# =============================================================================

def main():
    output_dir = "output/handover_figures"
    os.makedirs(output_dir, exist_ok=True)
    
    print("=" * 60)
    print("  UAV Trajectory Visualization")
    print("=" * 60)
    
    # 生成拓扑
    spatial, G, regions = setup_topology(num_nodes=15, seed=42)
    print(f"\nTopology: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")
    print(f"Base stations:")
    for bs in spatial.base_stations:
        print(f"  m{bs.id}: ({bs.x:.0f}, {bs.y:.0f}), r={bs.coverage_radius:.0f}m")
    
    # 1. 静态图：四种模型对比
    print("\n[1/4] Static trajectory comparison...")
    plot_trajectory_comparison(spatial, G, regions, output_dir)
    
    # 2. 动画 GIF：Gauss-Markov 模型
    print("\n[2/4] Animation: Gauss-Markov (v=30 m/s)...")
    create_trajectory_animation(
        spatial, G, regions,
        model="gauss_markov", speed=30,
        output_dir=output_dir, fps=10, speedup=10
    )
    
    # 3. 动画 GIF：RWP 模型（handover 更频繁）
    print("\n[3/4] Animation: RWP (v=40 m/s)...")
    create_trajectory_animation(
        spatial, G, regions,
        model="rwp", speed=40,
        output_dir=output_dir, fps=10, speedup=10
    )
    
    # 4. 自定义途径点示例：用基站 ID 指定路径
    print("\n[4/4] Custom waypoints (by node ID)...")
    # 选择几个分散的基站作为途径点
    node_ids = sorted(G.nodes())
    # 取第 1、4、8、11 号节点（或可用的）
    sample_nodes = [node_ids[i] for i in range(0, len(node_ids), 3)][:4]
    sample_nodes.append(sample_nodes[0])  # 回到起点
    print(f"  Selected waypoints: {sample_nodes}")
    
    plot_custom_waypoints(
        spatial, G, regions,
        waypoints=sample_nodes,
        speed=25, duration_s=60,
        output_dir=output_dir,
        output_name="trajectory_custom_nodes",
        make_gif=True, fps=10, speedup=10
    )
    
    print(f"\n{'='*60}")
    print(f"  All visualizations saved to {output_dir}/")
    print(f"{'='*60}")
    print(f"\n  Usage: plot_custom_waypoints(spatial, G, regions,")
    print(f"           waypoints=[3, 7, 12, 0],  # by node ID")
    print(f"           waypoints=[(100,200), (800,600), (1500,400)],  # by coords")
    print(f"           speed=20, duration_s=60)")


if __name__ == "__main__":
    main()

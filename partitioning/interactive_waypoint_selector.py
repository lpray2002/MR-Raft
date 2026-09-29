"""
交互式 UAV 途径点选择器

使用真实空间坐标布局（与 run.py plot_spatial_topology 一致）展示 MEC 拓扑，
用户在图上任意位置点击定义 UAV 飞行路径，程序自动推算途经节点序列。

端到端流程：
    interactive_waypoint_selector.py (自由画路径)
    → export_cluster_config.py (生成配置)
    → run-handover-experiment.sh (Docker 集群 + handover-client)
    → analyze_consistency_results.py (出图)

使用方式：
    python interactive_waypoint_selector.py

依赖：matplotlib（使用 TkAgg 后端的交互模式）
"""

import sys
import os
import subprocess
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '.'))

import numpy as np
import matplotlib
if __name__ == '__main__':
    matplotlib.use('TkAgg')
else:
    matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.widgets import Button, Slider
import networkx as nx

from src.topology.spatial_generator import SpatialTopologyGenerator
from src.topology.mec_builder import MECTopologyBuilder
from src.partition.strategies import PartitionStrategies
from src.visualization.plotter import TopologyPlotter, COLORS


def setup_topology(num_nodes=15, seed=42):
    """生成随机拓扑（Region ID 从 1 开始，与 Dragonboat 对齐）"""
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
    regions = {i + 1: set(r) for i, r in enumerate(ps.threshold_based(4))}

    return spatial, G, regions


def setup_real_topology(
    csv_path='data/real_topology/460.csv',
    center_lat=39.98, center_lon=116.39,
    radius_km=2.0, n_mec_nodes=15,
    min_range=300, max_range=500,
    partition_method='threshold', threshold_tau=5
):
    """
    加载真实北京基站拓扑并分区

    Parameters
    ----------
    csv_path : str
        基站 CSV 数据路径
    center_lat, center_lon : float
        中心坐标
    radius_km : float
        筛选半径
    n_mec_nodes : int
        MEC 节点数
    min_range, max_range : int
        覆盖半径范围
    partition_method : str
        分区方法: 'threshold', 'neighbor', 'clique', 'global'
    threshold_tau : int
        Threshold 方法的阈值参数

    Returns
    -------
    spatial : SpatialTopology
    G : nx.Graph
    regions : dict[int, set]  (region_id -> node_ids, ID 从 1 开始)
    """
    from src.topology.real_data_loader import load_beijing_data
    from src.topology.spatial_generator import SpatialTopology, BaseStation

    real_spatial, df_filtered, real_mec_nodes = load_beijing_data(
        csv_path=csv_path,
        center_lat=center_lat, center_lon=center_lon,
        radius_km=radius_km, n_mec_nodes=n_mec_nodes,
        min_range=min_range, max_range=max_range
    )

    builder = MECTopologyBuilder()
    G = builder.build_from_mec_nodes(real_mec_nodes)

    # 提取最大连通分量并重新编号
    if not nx.is_connected(G):
        largest_cc = max(nx.connected_components(G), key=len)
        old_nodes = sorted(largest_cc)
        node_mapping = {old_id: new_id for new_id, old_id in enumerate(old_nodes)}

        new_graph = nx.Graph()
        for old_id, new_id in node_mapping.items():
            new_graph.add_node(new_id, **G.nodes[old_id])
        for u, v in G.edges():
            if u in node_mapping and v in node_mapping:
                new_graph.add_edge(node_mapping[u], node_mapping[v], **G[u][v])
        G = new_graph

        # 更新空间拓扑
        filtered_bs = []
        for bs in real_spatial.base_stations:
            if bs.id in node_mapping:
                new_bs = BaseStation(
                    station_id=node_mapping[bs.id],
                    x=bs.x, y=bs.y,
                    coverage_radius=bs.coverage_radius
                )
                filtered_bs.append(new_bs)
        real_spatial = SpatialTopology(real_spatial.area_size, filtered_bs)

    # 分区
    ps = PartitionStrategies(G)
    if partition_method == 'threshold':
        partitions = ps.threshold_based(merge_threshold=threshold_tau)
    elif partition_method == 'neighbor':
        partitions = ps.neighbor_based()
    elif partition_method == 'clique':
        partitions = ps.maximal_clique()
    elif partition_method == 'global':
        partitions = ps.global_partition()
    else:
        raise ValueError(f"Unknown partition method: {partition_method}")

    regions = {i + 1: set(r) for i, r in enumerate(partitions)}
    return real_spatial, G, regions, real_mec_nodes


class WaypointSelector:
    """
    交互式途径点选择器（真实空间坐标布局）

    操作方式：
    - 左键点击：在图上任意位置添加路径点
    - 右键点击：撤销上一个路径点
    - Enter / "Compute" 按钮：计算途经节点序列
    - E / "Export & Run" 按钮：导出配置并显示实验命令
    - C：清除所有路径点
    - Q：退出
    """

    def __init__(self, spatial, G, regions, speed=25, dwell_s=0, mec_nodes_info=None):
        self.spatial = spatial
        self.G = G
        self.regions = regions
        self.speed = speed
        self.dwell_s = dwell_s
        self.mec_nodes_info = mec_nodes_info  # 真实拓扑时有值

        # 路径点（用户点击的原始位置，真实坐标系）
        self.path_points = []

        # 推算出的节点序列
        self.inferred_nodes = []

        # 节点 → Region 映射
        self.node_regions = {}
        for rid, members in regions.items():
            for nid in members:
                self.node_regions.setdefault(nid, []).append(rid)
        for nid in self.node_regions:
            self.node_regions[nid] = sorted(self.node_regions[nid])

        # 真实空间坐标布局
        if mec_nodes_info is not None:
            # 真实拓扑：使用 Web Mercator 坐标（与 plot_real_spatial_topology 一致）
            try:
                from pyproj import Transformer
                transformer = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
                self.pos = {}
                self._merc_radii = {}
                for mec in mec_nodes_info:
                    x, y = transformer.transform(mec['lon'], mec['lat'])
                    self.pos[mec['id']] = (x, y)
                    self._merc_radii[mec['id']] = mec.get('coverage_radius', 300)
                
                # 构建基站级别坐标（用于覆盖判定）
                self._bs_coords_per_node = {}
                for mec in mec_nodes_info:
                    bs_list = []
                    for bs in mec.get('base_stations', []):
                        bx, by = transformer.transform(bs['lon'], bs['lat'])
                        br = bs.get('coverage_radius', 300)
                        bs_list.append((bx, by, br))
                    self._bs_coords_per_node[mec['id']] = bs_list
                    
            except ImportError:
                # fallback: 简单经纬度→米转换
                center_lon = np.mean([m['lon'] for m in mec_nodes_info])
                center_lat = np.mean([m['lat'] for m in mec_nodes_info])
                self.pos = {}
                self._merc_radii = {}
                self._bs_coords_per_node = {}
                for mec in mec_nodes_info:
                    x = (mec['lon'] - center_lon) * 111320 * np.cos(np.radians(center_lat))
                    y = (mec['lat'] - center_lat) * 110540
                    self.pos[mec['id']] = (x, y)
                    self._merc_radii[mec['id']] = mec.get('coverage_radius', 300)
                    bs_list = []
                    for bs in mec.get('base_stations', []):
                        bx = (bs['lon'] - center_lon) * 111320 * np.cos(np.radians(center_lat))
                        by = (bs['lat'] - center_lat) * 110540
                        br = bs.get('coverage_radius', 300)
                        bs_list.append((bx, by, br))
                    self._bs_coords_per_node[mec['id']] = bs_list
        else:
            self.pos = {bs.id: (bs.x, bs.y) for bs in spatial.base_stations}
            self._merc_radii = None
            self._bs_coords_per_node = None

        # 节点坐标数组 + 覆盖半径（用于最近邻和覆盖判定）
        self.node_ids = sorted(G.nodes())
        self.node_coords = np.array([self.pos[n] for n in self.node_ids])
        bs_map = {bs.id: bs for bs in spatial.base_stations}
        if self._merc_radii is not None:
            self.node_radii = np.array([self._merc_radii.get(n, 300) for n in self.node_ids])
        else:
            self.node_radii = np.array([bs_map[n].coverage_radius for n in self.node_ids])
        self.switch_prob = 0.3  # 重叠区域切换概率

        # 分区列表（用于着色）
        self.partitions = []
        for rid in sorted(regions.keys()):
            self.partitions.append(regions[rid])

        # 节点→颜色
        self.node_color_map = {}
        for idx, part in enumerate(self.partitions):
            for nid in part:
                self.node_color_map[nid] = COLORS[idx % len(COLORS)]

        # === 创建图形 ===
        plt.rcParams['font.size'] = 13
        plt.rcParams['axes.titlesize'] = 16
        plt.rcParams['axes.labelsize'] = 15

        self.fig, self.ax = plt.subplots(figsize=(12, 10), dpi=100)
        self.fig.subplots_adjust(bottom=0.18)

        # 绘制底图
        self._draw_topology()

        # 动态元素
        self.path_line = None
        self.path_scatter = None
        self.info_text = None
        self.inferred_annotations = []

        # 按钮
        ax_compute = self.fig.add_axes([0.08, 0.02, 0.12, 0.04])
        ax_html = self.fig.add_axes([0.22, 0.02, 0.14, 0.04])
        ax_export = self.fig.add_axes([0.38, 0.02, 0.16, 0.04])
        ax_clear = self.fig.add_axes([0.56, 0.02, 0.10, 0.04])
        ax_analyze = self.fig.add_axes([0.68, 0.02, 0.12, 0.04])

        self.btn_compute = Button(ax_compute, 'Compute ↻',
                                   color='#4CAF50', hovercolor='#66BB6A')
        self.btn_html = Button(ax_html, 'HTML ▶',
                                color='#2196F3', hovercolor='#64B5F6')
        self.btn_export = Button(ax_export, 'Export & Run ⚡',
                                  color='#9C27B0', hovercolor='#BA68C8')
        self.btn_clear = Button(ax_clear, 'Clear ✕',
                                 color='#FF9800', hovercolor='#FFB74D')
        self.btn_analyze = Button(ax_analyze, 'Analyze 📊',
                                   color='#607D8B', hovercolor='#90A4AE')

        self.btn_compute.on_clicked(self._on_compute)
        self.btn_html.on_clicked(self._on_generate_html)
        self.btn_export.on_clicked(self._on_export_and_run)
        self.btn_clear.on_clicked(self._on_clear)
        self.btn_analyze.on_clicked(self._on_analyze)

        # 滑块
        ax_speed = self.fig.add_axes([0.15, 0.12, 0.35, 0.025])
        ax_dwell = self.fig.add_axes([0.15, 0.09, 0.35, 0.025])
        ax_jitter = self.fig.add_axes([0.15, 0.06, 0.35, 0.025])
        self.slider_speed = Slider(ax_speed, 'Speed (m/s)', 5, 60,
                                    valinit=speed, valstep=5)
        self.slider_dwell = Slider(ax_dwell, 'Dwell (s)', 0, 30,
                                    valinit=dwell_s, valstep=1)
        self.slider_jitter = Slider(ax_jitter, 'Jitter P', 0.0, 1.0,
                                     valinit=self.switch_prob, valstep=0.05)
        self.slider_jitter.on_changed(lambda v: setattr(self, 'switch_prob', v))

        # 事件绑定
        self.fig.canvas.mpl_connect('button_press_event', self._on_click)
        self.fig.canvas.mpl_connect('key_press_event', self._on_key)

        self._update_display()

        self.fig.suptitle(
            'Click anywhere to define UAV path | Right-click undo | Enter=Compute',
            fontsize=12, y=0.98)

    def _draw_topology(self):
        """绘制拓扑底图：真实拓扑用 plot_real_spatial_topology，随机拓扑用 plot_spatial_topology"""
        ax = self.ax

        plotter = TopologyPlotter(figsize=(10, 10))

        if self.mec_nodes_info is not None:
            # 真实拓扑：使用 plot_real_spatial_topology（经纬度 + Web Mercator）
            plotter.plot_real_spatial_topology(
                self.spatial,
                mec_nodes_info=self.mec_nodes_info,
                mec_graph=self.G,
                mec_color=COLORS[0],
                show=False,
                ax=ax
            )
            # 移除默认图例，后面加分区着色的
            legend = ax.get_legend()
            if legend:
                legend.remove()

            # 获取当前坐标范围（Web Mercator 坐标系），用于路径点坐标转换
            self._xlim = ax.get_xlim()
            self._ylim = ax.get_ylim()
        else:
            # 随机拓扑：使用 plot_spatial_topology（x/y 米坐标）
            plotter.plot_spatial_topology(
                self.spatial,
                mec_graph=self.G,
                show_coverage=True,
                show_labels=False,
                show=False,
                ax=ax
            )
            # 清除默认颜色，用分区着色重绘
            for coll in ax.collections[:]:
                coll.remove()
            for patch in ax.patches[:]:
                patch.remove()
            legend = ax.get_legend()
            if legend:
                legend.remove()

            # 重新绘制覆盖范围（按 Region 着色）
            for bs in self.spatial.base_stations:
                nid = bs.id
                color = self.node_color_map.get(nid, COLORS[0])
                circle = mpatches.Circle(
                    (bs.x, bs.y), bs.coverage_radius,
                    fill=True, facecolor=color, edgecolor='none',
                    alpha=0.15, linewidth=0
                )
                ax.add_patch(circle)

            # 重新绘制节点（按分区着色）
            for bs in self.spatial.base_stations:
                nid = bs.id
                color = self.node_color_map.get(nid, COLORS[0])
                ax.scatter(bs.x, bs.y, s=200, c=color, marker='o',
                           edgecolors='white', linewidths=3, zorder=5)

        # 标签：node_id [R1,R2]
        for bs in self.spatial.base_stations:
            nid = bs.id
            groups = self.node_regions.get(nid, [])
            region_str = ','.join(str(r) for r in groups)
            pos_x, pos_y = self.pos[nid]
            offset_y = self.node_radii[self.node_ids.index(nid)] * 0.18 if nid in self.node_ids else 50
            ax.text(pos_x, pos_y + offset_y,
                    f"{nid} [R{region_str}]",
                    fontsize=11, ha='center', va='bottom', fontweight='bold')

    def _covering_nodes(self, x, y):
        """返回覆盖 (x,y) 位置的所有节点 ID 列表（按距离排序）
        
        真实拓扑模式：检查 UAV 是否在该 MEC 节点的任意一个基站覆盖范围内
        随机拓扑模式：检查 UAV 到 MEC 节点中心的距离是否小于覆盖半径
        """
        if self.mec_nodes_info is not None and hasattr(self, '_bs_coords_per_node'):
            # 真实拓扑：基站级别覆盖判定
            covering = []
            min_dist_per_node = {}
            for nid in self.node_ids:
                bs_coords = self._bs_coords_per_node[nid]  # [(x, y, radius), ...]
                if not bs_coords:
                    continue
                for bx, by, br in bs_coords:
                    dist = np.sqrt((bx - x)**2 + (by - y)**2)
                    if dist <= br:
                        # 该节点的某个基站覆盖了 UAV
                        if nid not in min_dist_per_node or dist < min_dist_per_node[nid]:
                            min_dist_per_node[nid] = dist
                        break
            
            if not min_dist_per_node:
                # 不在任何基站覆盖范围内，不连接任何节点
                # 取最近基站对应的节点
                best_nid = None
                best_dist = float('inf')
                for nid in self.node_ids:
                    for bx, by, br in self._bs_coords_per_node[nid]:
                        dist = np.sqrt((bx - x)**2 + (by - y)**2)
                        if dist < best_dist:
                            best_dist = dist
                            best_nid = nid
                return [best_nid] if best_nid is not None else []
            
            # 按最近基站距离排序
            sorted_nodes = sorted(min_dist_per_node.keys(), key=lambda n: min_dist_per_node[n])
            return sorted_nodes
        else:
            # 随机拓扑：MEC 节点中心 + 覆盖半径
            dists = np.sqrt((self.node_coords[:, 0] - x)**2 +
                            (self.node_coords[:, 1] - y)**2)
            in_coverage = dists <= self.node_radii
            if not np.any(in_coverage):
                return [self.node_ids[np.argmin(dists)]]
            indices = np.where(in_coverage)[0]
            sorted_indices = indices[np.argsort(dists[indices])]
            return [self.node_ids[i] for i in sorted_indices]

    def _compute_inferred_sequence(self):
        """从路径点推算途经节点序列

        按请求间隔（request_interval_ms）的时间步长采样 UAV 位置，
        每个时刻独立判定连接哪个节点：
          - 非重叠区域：连最近覆盖节点
          - 重叠区域：以 switch_prob 概率切换到其他覆盖节点

        同时生成 self.timed_ticks 供导出时生成变长 dwell schedule。
        """
        if len(self.path_points) < 2:
            self.inferred_nodes = []
            self.timed_ticks = []
            return

        speed = int(self.slider_speed.val)
        from export_cluster_config import REQUEST_INTERVAL_MS
        interval_ms = REQUEST_INTERVAL_MS if REQUEST_INTERVAL_MS > 0 else 10

        # 计算路径总长度和各段累积距离
        segments = []
        cumulative_dist = [0.0]
        for i in range(len(self.path_points) - 1):
            p1 = np.array(self.path_points[i])
            p2 = np.array(self.path_points[i + 1])
            seg_len = np.linalg.norm(p2 - p1)
            segments.append((p1, p2, seg_len))
            cumulative_dist.append(cumulative_dist[-1] + seg_len)

        total_dist = cumulative_dist[-1]
        if total_dist < 1e-6:
            self.inferred_nodes = []
            self.timed_ticks = []
            return

        total_time_ms = int((total_dist / speed) * 1000)
        num_ticks = max(total_time_ms // interval_ms, 1)

        rng = np.random.RandomState(42)
        timed_ticks = []  # [{time_ms, node_id}, ...]
        current_node = None

        for tick in range(num_ticks + 1):
            time_ms = tick * interval_ms
            d = (tick / num_ticks) * total_dist

            seg_idx = 0
            for j in range(len(segments)):
                if cumulative_dist[j + 1] >= d:
                    seg_idx = j
                    break
            else:
                seg_idx = len(segments) - 1

            p1, p2, seg_len = segments[seg_idx]
            local_d = d - cumulative_dist[seg_idx]
            t = local_d / seg_len if seg_len > 1e-6 else 0
            pt = p1 + t * (p2 - p1)

            covering = self._covering_nodes(pt[0], pt[1])

            if current_node is None:
                current_node = covering[0]
            elif len(covering) > 1 and current_node in covering:
                if rng.random() < self.switch_prob:
                    others = [n for n in covering if n != current_node]
                    current_node = rng.choice(others)
            elif current_node not in covering:
                current_node = covering[0]

            timed_ticks.append({"time_ms": int(time_ms), "node_id": int(current_node)})

        self.timed_ticks = timed_ticks

        # 去连续重复得到节点序列
        deduped = [timed_ticks[0]["node_id"]]
        for tt in timed_ticks[1:]:
            if tt["node_id"] != deduped[-1]:
                deduped.append(tt["node_id"])

        self.inferred_nodes = deduped

    def _on_click(self, event):
        """鼠标点击"""
        if event.inaxes != self.ax:
            return

        if event.button == 1:  # 左键：添加路径点
            self.path_points.append((event.xdata, event.ydata))
            self._update_display()

        elif event.button == 3:  # 右键：撤销
            if self.path_points:
                self.path_points.pop()
                self._update_display()

    def _on_key(self, event):
        if event.key == 'enter':
            self._on_compute(None)
        elif event.key == 'h':
            self._on_generate_html(None)
        elif event.key == 'e':
            self._on_export_and_run(None)
        elif event.key == 'c':
            self._on_clear(None)
        elif event.key == 'q':
            plt.close(self.fig)

    def _on_compute(self, event):
        """计算推断的节点序列"""
        if len(self.path_points) < 2:
            print("  Need at least 2 path points!")
            return

        self._compute_inferred_sequence()

        n_ho = len(self.inferred_nodes) - 1
        # 识别 jitter handovers（来回切换同一对节点）
        jitter_count = 0
        for i in range(1, n_ho):
            if self.inferred_nodes[i + 1] == self.inferred_nodes[i - 1]:
                jitter_count += 1

        print(f"\n  {'='*55}")
        print(f"  Inferred sequence ({len(self.inferred_nodes)} nodes, {n_ho} HOs, "
              f"{jitter_count} jitter, P={self.switch_prob:.0%})")
        print(f"  Path: {' -> '.join(f'm{n}' for n in self.inferred_nodes)}")
        print(f"  {'─'*55}")

        for i in range(n_ho):
            from_n = self.inferred_nodes[i]
            to_n = self.inferred_nodes[i + 1]
            from_r = set(self.node_regions.get(from_n, []))
            to_r = set(self.node_regions.get(to_n, []))
            shared = from_r & to_r
            is_jitter = (i >= 1 and self.inferred_nodes[i + 1] == self.inferred_nodes[i - 1])
            tag = "JIT" if is_jitter else ("  ✓" if shared else "  ✗")
            print(f"    HO{i+1:>2}: m{from_n} -> m{to_n}  "
                  f"shared=R{sorted(shared)}  {tag}")
        print(f"  {'='*55}")

        self._update_display()

    def _on_generate_html(self, event):
        """生成轨迹动画 HTML（使用路径点坐标 + jitter）"""
        if len(self.path_points) < 2:
            print("  Need at least 2 path points!")
            return

        self.speed = int(self.slider_speed.val)
        print(f"\n  Generating HTML trajectory with {len(self.path_points)} waypoints, "
              f"speed={self.speed} m/s, jitter={self.switch_prob:.0%}...")

        from plot_uav_trajectory import plot_custom_waypoints

        # 真实拓扑模式下，路径点是 Mercator 坐标，需要转换为 spatial 的米坐标
        if self.mec_nodes_info is not None:
            waypoints_for_plot = self._mercator_to_spatial_coords(self.path_points)
        else:
            waypoints_for_plot = self.path_points

        plot_custom_waypoints(
            self.spatial, self.G, self.regions,
            waypoints=waypoints_for_plot,
            speed=self.speed,
            duration_s=0,  # 0 = 自动根据路程计算
            output_dir="output/handover_figures",
            output_name="trajectory_interactive",
            make_gif=True, fps=10, speedup=30,
            switch_prob=self.switch_prob,
            mec_nodes_info=self.mec_nodes_info,
        )

        html_path = os.path.abspath("output/handover_figures/trajectory_interactive.html")
        print(f"  Opening: {html_path}")
        os.system(f'open "{html_path}"')

    def _mercator_to_spatial_coords(self, mercator_points):
        """将 Web Mercator 坐标转换为 spatial 的米坐标系"""
        if not self.mec_nodes_info:
            return mercator_points

        try:
            from pyproj import Transformer
            transformer = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
        except ImportError:
            transformer = None

        # 获取 spatial 坐标系的参考点（用 mec_nodes_info 的中心经纬度）
        center_lon = np.mean([m['lon'] for m in self.mec_nodes_info])
        center_lat = np.mean([m['lat'] for m in self.mec_nodes_info])

        # spatial 中的坐标是相对于某个原点的米坐标
        # 找到 Mercator→经纬度→米 的映射关系
        # 用已知节点建立对应关系
        bs_map = {bs.id: (bs.x, bs.y) for bs in self.spatial.base_stations}

        converted = []
        for mx, my in mercator_points:
            if transformer:
                lon, lat = transformer.transform(mx, my)
            else:
                # fallback
                lon = mx / (111320 * np.cos(np.radians(center_lat))) + center_lon
                lat = my / 110540 + center_lat

            # 经纬度→米坐标（与 real_data_loader.convert_to_meters 一致）
            x = (lon - center_lon) * 111320 * np.cos(np.radians(center_lat))
            y = (lat - center_lat) * 110540

            # 平移到 spatial 坐标系（spatial 的原点可能有偏移）
            # 用第一个节点校准
            if not hasattr(self, '_coord_offset'):
                mec0 = self.mec_nodes_info[0]
                x0_calc = (mec0['lon'] - center_lon) * 111320 * np.cos(np.radians(center_lat))
                y0_calc = (mec0['lat'] - center_lat) * 110540
                x0_spatial, y0_spatial = bs_map[mec0['id']]
                self._coord_offset = (x0_spatial - x0_calc, y0_spatial - y0_calc)

            x += self._coord_offset[0]
            y += self._coord_offset[1]
            converted.append((x, y))

        return converted

    def _on_export_and_run(self, event):
        """导出配置 + 显示运行命令"""
        if not self.inferred_nodes or len(self.inferred_nodes) < 2:
            self._on_compute(None)
        if len(self.inferred_nodes) < 2:
            print("  Cannot export: need at least 2 nodes in path")
            return

        self.speed = int(self.slider_speed.val)
        self.dwell_s = int(self.slider_dwell.val)

        print(f"\n{'='*60}")
        print(f"  End-to-End Handover Experiment")
        print(f"{'='*60}")
        print(f"  Inferred waypoints: {self.inferred_nodes}")
        print(f"  Speed: {self.speed} m/s, Dwell: {self.dwell_s}s, Jitter: {self.switch_prob:.0%}")

        script_dir = os.path.dirname(os.path.abspath(__file__))
        output_dir = os.path.join(script_dir, "output", "handover_cluster")
        os.makedirs(output_dir, exist_ok=True)

        # Step 1a: 导出 timed_ticks（如果有 jitter 数据）
        if hasattr(self, 'timed_ticks') and self.timed_ticks:
            import json
            ticks_path = os.path.join(output_dir, "timed_ticks.json")
            with open(ticks_path, 'w') as f:
                json.dump(self.timed_ticks, f)
            print(f"  Saved timed_ticks.json ({len(self.timed_ticks)} ticks)")

        # Step 1b: export_cluster_config.py（使用 --ticks 模式）
        print(f"\n  [Step 1] Exporting cluster config...")
        waypoints_str = ','.join(str(nid) for nid in self.inferred_nodes)
        cmd = [
            sys.executable, os.path.join(script_dir, "export_cluster_config.py"),
            "--waypoints", waypoints_str,
            "--speed", str(self.speed),
            "--dwell", str(self.dwell_s),
            "--seed", "42",
            "--output-dir", output_dir,
        ]
        if self.mec_nodes_info is not None:
            cmd.extend(["--real"])
        if hasattr(self, 'timed_ticks') and self.timed_ticks:
            cmd.extend(["--ticks", os.path.join(output_dir, "timed_ticks.json")])

        result = subprocess.run(cmd, capture_output=True, text=True, cwd=script_dir)
        if result.returncode != 0:
            print(f"  ✗ export_cluster_config.py failed:\n{result.stderr}")
            return
        print(f"  ✓ Config exported to {output_dir}/")

        # Step 2: 提示运行
        go_project_dir = "/Users/lpray/workspace/work/2025_10_23_multi-region-raft"
        print(f"\n  [Step 2] Ready to run Docker experiment.")
        print(f"  Command:")
        print(f"    cd {go_project_dir}")
        print(f"    bash scripts/run-handover-experiment.sh --single --config {output_dir}")
        print(f"\n  Or analyze existing results:")
        print(f"    python analyze_consistency_results.py")

        # 保存
        waypoints_file = os.path.join(output_dir, "selected_waypoints.txt")
        with open(waypoints_file, 'w') as f:
            f.write(f"waypoints={waypoints_str}\n")
            f.write(f"speed={self.speed}\n")
            f.write(f"dwell={self.dwell_s}\n")
            f.write(f"node_ids={self.inferred_nodes}\n")
        print(f"\n  Saved: {waypoints_file}")
        print(f"{'='*60}")

    def _on_analyze(self, event):
        """运行结果分析"""
        script_dir = os.path.dirname(os.path.abspath(__file__))
        results_dir = "/Users/lpray/workspace/work/2025_10_23_multi-region-raft/output/handover_results"

        if not os.path.isdir(results_dir):
            print(f"  ⚠ No results at {results_dir}, run experiment first")
            return

        cmd = [sys.executable, os.path.join(script_dir, "analyze_consistency_results.py"),
               "--results-dir", results_dir]
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=script_dir)
        print(result.stdout)
        if result.returncode != 0:
            print(f"  ⚠ {result.stderr}")

    def _on_clear(self, event):
        self.path_points.clear()
        self.inferred_nodes.clear()
        self._update_display()

    def _update_display(self):
        """更新动态元素"""
        ax = self.ax

        # 清除旧路径
        if self.path_line:
            for l in self.path_line:
                l.remove()
            self.path_line = None
        if self.path_scatter:
            self.path_scatter.remove()
            self.path_scatter = None
        if self.info_text:
            self.info_text.remove()
            self.info_text = None
        for ann in self.inferred_annotations:
            try:
                ann.remove()
            except Exception:
                pass
        self.inferred_annotations.clear()

        if self.path_points:
            px = [p[0] for p in self.path_points]
            py = [p[1] for p in self.path_points]

            # 路径线
            self.path_line = ax.plot(px, py, '-', color='#D32F2F',
                                     linewidth=2.5, alpha=0.7, zorder=8)

            # 路径点
            self.path_scatter = ax.scatter(px, py, s=60, c='#D32F2F',
                                            marker='x', linewidths=2,
                                            alpha=0.9, zorder=9)

        # 推断序列标注
        if self.inferred_nodes and len(self.inferred_nodes) >= 2:
            # 统计每对节点间的箭头数量（用于 jitter 时偏移避免重叠）
            edge_count = {}
            n_ho = len(self.inferred_nodes) - 1

            for i in range(n_ho):
                from_n = self.inferred_nodes[i]
                to_n = self.inferred_nodes[i + 1]
                is_jitter = (i >= 1 and self.inferred_nodes[i + 1] == self.inferred_nodes[i - 1])

                from_r = set(self.node_regions.get(from_n, []))
                to_r = set(self.node_regions.get(to_n, []))
                shared = from_r & to_r

                if is_jitter:
                    color = '#FF9800'  # 橙色 = jitter
                    lw = 1.5
                elif shared:
                    color = '#2E7D32'  # 绿色 = 有共享 Region
                    lw = 2.5
                else:
                    color = '#D32F2F'  # 红色 = 无共享 Region
                    lw = 2.5

                pair = (min(from_n, to_n), max(from_n, to_n))
                edge_count[pair] = edge_count.get(pair, 0) + 1
                rad = 0.15 * edge_count[pair]  # 多次箭头递增弧度

                fx, fy = self.pos[from_n]
                tx, ty = self.pos[to_n]

                ann = ax.annotate('',
                    xy=(tx, ty), xytext=(fx, fy),
                    arrowprops=dict(arrowstyle='->', color=color,
                                    lw=lw, mutation_scale=15,
                                    connectionstyle=f'arc3,rad={rad}'),
                    zorder=7)
                self.inferred_annotations.append(ann)

        # 信息文本
        n_pts = len(self.path_points)
        info = f"Path points: {n_pts} | Jitter P: {self.switch_prob:.0%}"
        if self.inferred_nodes:
            n_nodes = len(self.inferred_nodes)
            n_ho = n_nodes - 1
            verifiable = sum(
                1 for i in range(n_ho)
                if set(self.node_regions.get(self.inferred_nodes[i], [])) &
                   set(self.node_regions.get(self.inferred_nodes[i+1], []))
            )
            jitter_count = sum(
                1 for i in range(1, n_ho)
                if self.inferred_nodes[i + 1] == self.inferred_nodes[i - 1]
            )
            info += f" | HOs: {n_ho} ({verifiable} verif, {jitter_count} jitter)"
            seq = ' -> '.join(f'm{n}' for n in self.inferred_nodes)
            if len(seq) > 80:
                seq = seq[:77] + '...'
            info += f"\n{seq}"

        self.info_text = ax.text(
            0.02, 0.02, info, transform=ax.transAxes,
            fontsize=11, fontweight='bold', va='bottom', ha='left',
            bbox=dict(boxstyle='round', facecolor='white', alpha=0.9,
                      edgecolor='gray'))

        self.fig.canvas.draw_idle()

    def show(self):
        plt.show()


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Interactive UAV Waypoint Selector')
    parser.add_argument('--real', action='store_true',
                        help='Use real Beijing base station topology')
    parser.add_argument('--partition', default='threshold',
                        choices=['threshold', 'neighbor', 'clique', 'global'],
                        help='Partition method (default: threshold)')
    parser.add_argument('--tau', type=int, default=5,
                        help='Threshold tau value (default: 5)')
    parser.add_argument('--seed', type=int, default=42,
                        help='Random seed for synthetic topology (default: 42)')
    parser.add_argument('--nodes', type=int, default=15,
                        help='Number of nodes for synthetic topology (default: 15)')
    args = parser.parse_args()

    print("=" * 60)
    print("  Interactive UAV Waypoint Selector")
    if args.real:
        print("  (Real Beijing Base Station Topology)")
    else:
        print("  (Synthetic topology, seed={})".format(args.seed))
    print("=" * 60)
    print()
    print("  Controls:")
    print("    Left click  - Add path point (anywhere on canvas)")
    print("    Right click - Undo last point")
    print("    Enter       - Compute inferred node sequence")
    print("    H           - Generate HTML trajectory animation")
    print("    E           - Export config & show run command")
    print("    C           - Clear all")
    print("    Q           - Quit")
    print()

    if args.real:
        print(f"  Loading real topology (partition={args.partition}, tau={args.tau})...")
        spatial, G, regions, mec_nodes_info = setup_real_topology(
            partition_method=args.partition,
            threshold_tau=args.tau
        )
        print(f"  Real topology: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")
    else:
        spatial, G, regions = setup_topology(num_nodes=args.nodes, seed=args.seed)
        mec_nodes_info = None
        print(f"  Synthetic topology: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")

    print(f"  Regions: {len(regions)} ({args.partition})")
    for rid, members in sorted(regions.items()):
        print(f"    R{rid}: nodes {sorted(members)}")
    print()

    selector = WaypointSelector(spatial, G, regions, speed=25, dwell_s=0,
                                mec_nodes_info=mec_nodes_info)
    selector.show()


if __name__ == "__main__":
    main()

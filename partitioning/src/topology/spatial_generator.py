"""
空间拓扑生成器
基于泊松点过程(PPP)生成基站空间分布
支持面向对象和函数式两种接口
"""

import numpy as np
import networkx as nx
import matplotlib.pyplot as plt
from typing import List, Dict, Tuple, Optional
from scipy.spatial import distance_matrix, Voronoi, voronoi_plot_2d
from matplotlib.patches import Circle


class BaseStation:
    """基站类"""
    
    def __init__(self, station_id: int, x: float, y: float, coverage_radius: float):
        """
        初始化基站
        
        Parameters:
        -----------
        station_id : int
            基站ID
        x : float
            X坐标
        y : float
            Y坐标
        coverage_radius : float
            覆盖半径
        """
        self.id = station_id
        self.x = x
        self.y = y
        self.coverage_radius = coverage_radius
    
    def distance_to(self, other: 'BaseStation') -> float:
        """计算到另一个基站的距离"""
        return np.sqrt((self.x - other.x)**2 + (self.y - other.y)**2)
    
    def has_overlap_with(self, other: 'BaseStation') -> bool:
        """判断是否与另一个基站的覆盖范围重叠"""
        distance = self.distance_to(other)
        return distance < (self.coverage_radius + other.coverage_radius)
    
    def __repr__(self):
        return f"BS({self.id}, pos=({self.x:.1f}, {self.y:.1f}), r={self.coverage_radius})"


class SpatialTopology:
    """空间拓扑类"""
    
    def __init__(self, area_size: float, base_stations: List[BaseStation]):
        """
        初始化空间拓扑
        
        Parameters:
        -----------
        area_size : float
            区域边长（假设为正方形区域）
        base_stations : List[BaseStation]
            基站列表
        """
        self.area_size = area_size
        self.base_stations = base_stations
        self.num_stations = len(base_stations)
    
    def get_station(self, station_id: int) -> Optional[BaseStation]:
        """根据ID获取基站"""
        for bs in self.base_stations:
            if bs.id == station_id:
                return bs
        return None
    
    def get_all_positions(self) -> np.ndarray:
        """获取所有基站位置"""
        return np.array([[bs.x, bs.y] for bs in self.base_stations])
    
    def __repr__(self):
        return f"SpatialTopology(area={self.area_size}x{self.area_size}, stations={self.num_stations})"


class SpatialTopologyGenerator:
    """空间拓扑生成器 - 基于泊松点过程(PPP)
    
    使用原始 PPP 生成方法
    """
    
    def __init__(self, 
                 area_size: float = 1000.0,
                 density: float = 0.0001,
                 coverage_radius: float = 150.0,
                 coverage_std: float = 0.0,
                 random_seed: Optional[int] = None):
        """
        Parameters:
        -----------
        area_size : float
            区域边长（米）
        density : float
            基站密度（个/m²）
        coverage_radius : float
            覆盖半径均值（米）
        coverage_std : float
            覆盖半径标准差（米），0表示固定半径
        random_seed : int, optional
            随机种子
        """
        self.area_size = area_size
        self.density = density
        self.coverage_radius = coverage_radius
        self.coverage_std = coverage_std if coverage_std > 0 else 0
        self.random_seed = random_seed
        
        if random_seed is not None:
            np.random.seed(random_seed)
    
    def generate(self) -> SpatialTopology:
        """生成空间拓扑（PPP）- 使用原始方法"""
        # 使用原始 PPP 生成方法
        area = self.area_size * self.area_size
        expected_num = area * self.density
        num_stations = np.random.poisson(expected_num)
        
        if num_stations == 0:
            num_stations = 1
        
        # 生成基站位置
        bs_x = np.random.uniform(0, self.area_size, num_stations)
        bs_y = np.random.uniform(0, self.area_size, num_stations)
        bs_positions = np.vstack((bs_x, bs_y)).T
        
        # 生成覆盖半径
        if self.coverage_std > 0:
            coverage_radii = np.clip(
                np.random.normal(self.coverage_radius, self.coverage_std, num_stations),
                50, 2 * self.coverage_radius
            )
        else:
            coverage_radii = np.full(num_stations, self.coverage_radius)
        
        # 创建基站对象
        base_stations = [
            BaseStation(i, x, y, r)
            for i, (x, y, r) in enumerate(zip(bs_x, bs_y, coverage_radii))
        ]
        
        return SpatialTopology(self.area_size, base_stations)
    
    def generate_with_fixed_count(self, num_stations: int) -> SpatialTopology:
        """
        生成指定数量基站的空间拓扑
        
        Parameters:
        -----------
        num_stations : int
            基站数量
        
        Returns:
        --------
        SpatialTopology : 生成的空间拓扑对象
        """
        # 在区域内随机生成基站位置
        x_positions = np.random.uniform(0, self.area_size, num_stations)
        y_positions = np.random.uniform(0, self.area_size, num_stations)
        
        # 创建基站对象
        base_stations = [
            BaseStation(i, x, y, self.coverage_radius)
            for i, (x, y) in enumerate(zip(x_positions, y_positions))
        ]
        
        return SpatialTopology(self.area_size, base_stations)
    
    def generate_grid(self, grid_size: int) -> SpatialTopology:
        """
        生成网格状基站分布（用于测试）
        
        Parameters:
        -----------
        grid_size : int
            网格大小（每边基站数）
        
        Returns:
        --------
        SpatialTopology : 生成的空间拓扑对象
        """
        spacing = self.area_size / (grid_size + 1)
        base_stations = []
        
        station_id = 0
        for i in range(1, grid_size + 1):
            for j in range(1, grid_size + 1):
                x = i * spacing
                y = j * spacing
                base_stations.append(
                    BaseStation(station_id, x, y, self.coverage_radius)
                )
                station_id += 1
        
        return SpatialTopology(self.area_size, base_stations)


def generate_ppp_topology_direct(area_size=2000,
                                  lambda_bs=4e-6,
                                  coverage_radius_mean=300,
                                  coverage_radius_std=50,
                                  random_seed=5) -> nx.Graph:
    """
    直接生成 PPP 拓扑并返回 NetworkX 图（原始方法）
    
    Parameters:
    -----------
    area_size : float
        区域边长（米）
    lambda_bs : float
        基站密度（个/平方米）
    coverage_radius_mean : float
        覆盖半径均值（米）
    coverage_radius_std : float
        覆盖半径标准差（米）
    random_seed : int
        随机种子
    
    Returns:
    --------
    G : networkx.Graph
        生成的拓扑图，节点包含 'pos' 和 'coverage' 属性
    """
    np.random.seed(random_seed)
    
    # 1. 生成 PPP 基站
    num_bs = np.random.poisson(lambda_bs * area_size * area_size)
    if num_bs == 0:
        num_bs = 1
    
    bs_x = np.random.uniform(0, area_size, num_bs)
    bs_y = np.random.uniform(0, area_size, num_bs)
    bs_positions = np.vstack((bs_x, bs_y)).T
    coverage_radii = np.clip(np.random.normal(coverage_radius_mean, coverage_radius_std, num_bs),
                             50, 2 * coverage_radius_mean)
    
    # 2. 构建 NetworkX 图
    G = nx.Graph()
    for i, (x, y, r) in enumerate(zip(bs_x, bs_y, coverage_radii)):
        G.add_node(i, pos=(x, y), coverage=r)
    
    # 添加边：两基站距离 <= 覆盖半径之和
    dist_mat = distance_matrix(bs_positions, bs_positions)
    for i in range(num_bs):
        for j in range(i + 1, num_bs):
            if dist_mat[i, j] <= coverage_radii[i] + coverage_radii[j]:
                G.add_edge(i, j, distance=dist_mat[i, j])
    
    return G


def generate_ppp_topology(area_size=2000,
                          lambda_bs=4e-6,
                          coverage_radius_mean=300,
                          coverage_radius_std=50,
                          plot_voronoi=False,
                          plot_graph=False,
                          random_seed=5,
                          node_color='#0273bc',
                          coverage_alpha=0.2,
                          edge_color='dimgray',
                          return_spatial=False):
    """
    原始 PPP 拓扑生成函数（带可视化）
    
    Parameters:
    -----------
    area_size : float
        区域边长（米）
    lambda_bs : float
        基站密度（个/平方米）
    coverage_radius_mean : float
        覆盖半径均值（米）
    coverage_radius_std : float
        覆盖半径标准差（米）
    plot_voronoi : bool
        是否绘制 Voronoi 小区图
    plot_graph : bool
        是否绘制网络拓扑图
    random_seed : int
        随机种子
    node_color : str
        节点颜色
    coverage_alpha : float
        覆盖圆透明度
    edge_color : str
        边颜色
    return_spatial : bool
        是否同时返回 SpatialTopology 对象
    
    Returns:
    --------
    G : networkx.Graph 或 Tuple[networkx.Graph, SpatialTopology]
        如果 return_spatial=False，返回生成的拓扑图
        如果 return_spatial=True，返回 (图, SpatialTopology对象) 元组
    """
    np.random.seed(random_seed)
    
    # 生成基站
    num_bs = np.random.poisson(lambda_bs * area_size * area_size)
    if num_bs == 0:
        num_bs = 1
    
    bs_x = np.random.uniform(0, area_size, num_bs)
    bs_y = np.random.uniform(0, area_size, num_bs)
    bs_positions = np.vstack((bs_x, bs_y)).T
    coverage_radii = np.clip(
        np.random.normal(coverage_radius_mean, coverage_radius_std, num_bs),
        50, 2 * coverage_radius_mean
    )
    
    # 构建图
    G = nx.Graph()
    for i, (x, y, r) in enumerate(zip(bs_x, bs_y, coverage_radii)):
        G.add_node(i, pos=(x, y), coverage=r)
    
    # 添加边
    dist_mat = distance_matrix(bs_positions, bs_positions)
    for i in range(num_bs):
        for j in range(i + 1, num_bs):
            if dist_mat[i, j] <= coverage_radii[i] + coverage_radii[j]:
                G.add_edge(i, j, distance=dist_mat[i, j])
    
    # Voronoi 图
    if plot_voronoi and num_bs >= 4:
        try:
            vor = Voronoi(bs_positions)
            fig, ax = plt.subplots(figsize=(8, 8))
            voronoi_plot_2d(vor, ax=ax, show_vertices=False, 
                           line_colors='gray', line_width=1, line_alpha=0.6, point_size=0)
            for x, y, r in zip(bs_x, bs_y, coverage_radii):
                ax.plot(x, y, 'o', color=node_color, markersize=6)
                circle = Circle((x, y), radius=r, color=node_color, alpha=coverage_alpha)
                ax.add_patch(circle)
            ax.set_xlim(0, area_size)
            ax.set_ylim(0, area_size)
            ax.set_xlabel("X (m)", fontsize=12, fontweight='bold')
            ax.set_ylabel("Y (m)", fontsize=12, fontweight='bold')
            ax.set_title("PPP Base Stations with Voronoi Cells", fontsize=14, fontweight='bold')
            ax.set_aspect('equal')
            plt.grid(True, alpha=0.3)
            plt.tight_layout()
            plt.show()
        except:
            print("无法生成 Voronoi 图")
    
    # 拓扑图
    if plot_graph:
        fig, ax = plt.subplots(figsize=(8, 8))
        for i in G.nodes:
            x, y = G.nodes[i]['pos']
            r = G.nodes[i]['coverage']
            circle = Circle((x, y), radius=r, color=node_color, alpha=coverage_alpha, zorder=1)
            ax.add_patch(circle)
        for u, v in G.edges:
            x1, y1 = G.nodes[u]['pos']
            x2, y2 = G.nodes[v]['pos']
            ax.plot([x1, x2], [y1, y2], color=edge_color, linewidth=2, alpha=0.8, zorder=2)
        for i in G.nodes:
            x, y = G.nodes[i]['pos']
            ax.plot(x, y, 'o', color=node_color, markersize=8,
                   markeredgecolor='white', markeredgewidth=1.5, zorder=3)
        ax.set_xlim(0, area_size)
        ax.set_ylim(0, area_size)
        ax.set_xlabel("X (m)", fontsize=12, fontweight='bold')
        ax.set_ylabel("Y (m)", fontsize=12, fontweight='bold')
        ax.set_title(f"PPP Topology: N={G.number_of_nodes()}, E={G.number_of_edges()}", 
                    fontsize=14, fontweight='bold')
        ax.set_aspect('equal')
        plt.grid(True, alpha=0.3)
        plt.tight_layout()
        plt.show()
    
    # 如果需要返回 SpatialTopology 对象
    if return_spatial:
        # 创建 BaseStation 对象
        base_stations = [
            BaseStation(i, bs_x[i], bs_y[i], coverage_radii[i])
            for i in range(num_bs)
        ]
        spatial_topology = SpatialTopology(area_size, base_stations)
        return G, spatial_topology
    
    return G


if __name__ == '__main__':
    # 测试
    print("测试 PPP 生成...")
    G = generate_ppp_topology(
        area_size=2000,
        lambda_bs=4e-6,
        coverage_radius_mean=300,
        coverage_radius_std=50,
        plot_graph=True,
        random_seed=42
    )
    print(f"生成图: {G.number_of_nodes()} 节点，{G.number_of_edges()} 边")


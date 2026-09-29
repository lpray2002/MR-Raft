"""
MEC拓扑图构建器
基于空间拓扑中基站的覆盖范围重叠关系构建图拓扑
"""

import networkx as nx
from typing import Optional
from .spatial_generator import SpatialTopology


class MECTopologyBuilder:
    """MEC拓扑图构建器"""
    
    def __init__(self):
        """初始化构建器"""
        pass
    
    def build_from_spatial(self, spatial_topology: SpatialTopology) -> nx.Graph:
        """
        从空间拓扑构建MEC拓扑图
        
        如果两个基站的覆盖范围重叠，则在它们之间建立边
        
        Parameters:
        -----------
        spatial_topology : SpatialTopology
            空间拓扑对象
        
        Returns:
        --------
        nx.Graph : MEC拓扑图
        """
        # 创建空图
        G = nx.Graph()
        
        # 添加节点，节点属性包括位置和覆盖半径
        for bs in spatial_topology.base_stations:
            G.add_node(
                bs.id,
                x=bs.x,
                y=bs.y,
                coverage_radius=bs.coverage_radius
            )
        
        # 检查所有基站对，如果覆盖范围重叠则添加边
        stations = spatial_topology.base_stations
        num_stations = len(stations)
        
        for i in range(num_stations):
            for j in range(i + 1, num_stations):
                bs_i = stations[i]
                bs_j = stations[j]
                
                # 检查是否重叠
                if bs_i.has_overlap_with(bs_j):
                    distance = bs_i.distance_to(bs_j)
                    G.add_edge(bs_i.id, bs_j.id, weight=distance)
        
        return G
    
    def add_edge_attributes(self, G: nx.Graph) -> nx.Graph:
        """
        为图的边添加额外属性
        
        Parameters:
        -----------
        G : nx.Graph
            MEC拓扑图
        
        Returns:
        --------
        nx.Graph : 添加了属性的图
        """
        for u, v in G.edges():
            if 'weight' not in G[u][v]:
                # 如果边没有权重，根据节点位置计算距离
                x1, y1 = G.nodes[u]['x'], G.nodes[u]['y']
                x2, y2 = G.nodes[v]['x'], G.nodes[v]['y']
                distance = ((x2 - x1)**2 + (y2 - y1)**2)**0.5
                G[u][v]['weight'] = distance
        
        return G
    
    def get_graph_statistics(self, G: nx.Graph) -> dict:
        """
        获取图的统计信息
        
        Parameters:
        -----------
        G : nx.Graph
            MEC拓扑图
        
        Returns:
        --------
        dict : 统计信息字典
        """
        stats = {
            'num_nodes': G.number_of_nodes(),
            'num_edges': G.number_of_edges(),
            'is_connected': nx.is_connected(G),
            'num_components': nx.number_connected_components(G),
            'density': nx.density(G),
        }
        
        if stats['num_nodes'] > 0:
            degrees = [d for n, d in G.degree()]
            stats['avg_degree'] = sum(degrees) / len(degrees)
            stats['max_degree'] = max(degrees)
            stats['min_degree'] = min(degrees)
        
        if stats['is_connected']:
            stats['diameter'] = nx.diameter(G)
            stats['avg_shortest_path_length'] = nx.average_shortest_path_length(G)
        
        return stats
    
    def build_from_mec_nodes(self, mec_nodes_info: list) -> nx.Graph:
        """
        从MEC节点信息构建MEC拓扑图（用于真实数据）
        
        如果两个MEC节点的融合覆盖范围重叠，则在这两个MEC节点之间建立边
        使用shapely进行几何运算，与空间图的绘制逻辑保持一致
        
        Parameters:
        -----------
        mec_nodes_info : list
            MEC节点信息列表，每个元素为字典，包含:
            - 'id': MEC节点ID
            - 'lon', 'lat': MEC节点位置
            - 'base_stations': 该MEC服务的基站列表，每个基站包含:
                - 'lon', 'lat': 基站位置
                - 'coverage_radius': 覆盖半径（米）
        
        Returns:
        --------
        nx.Graph : MEC拓扑图
        """
        import numpy as np
        from shapely.geometry import Point
        from shapely.ops import unary_union
        
        # 创建空图
        G = nx.Graph()
        
        # 添加MEC节点
        for mec in mec_nodes_info:
            G.add_node(
                mec['id'],
                lon=mec['lon'],
                lat=mec['lat'],
                num_base_stations=mec.get('num_base_stations', len(mec.get('base_stations', [])))
            )
        
        # 为每个MEC节点创建融合覆盖区域（使用Web Mercator投影以保持准确性）
        try:
            from pyproj import Transformer
            has_pyproj = True
            transformer = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
        except ImportError:
            has_pyproj = False
        
        # 为每个MEC构建融合覆盖区域
        mec_coverage_polygons = {}
        
        for mec in mec_nodes_info:
            bs_list = mec.get('base_stations', [])
            
            if len(bs_list) == 0:
                continue
            elif len(bs_list) == 1:
                # 单个基站：直接创建圆形
                bs = bs_list[0]
                if has_pyproj:
                    x, y = transformer.transform(bs['lon'], bs['lat'])
                    coverage = bs['coverage_radius']
                else:
                    # 简化转换：使用米单位
                    center_lat = mec['lat']
                    y = (bs['lat'] - center_lat) * 111320
                    x = (bs['lon'] - mec['lon']) * 111320 * np.cos(np.radians(center_lat))
                    coverage = bs['coverage_radius']
                
                point = Point(x, y)
                mec_coverage_polygons[mec['id']] = point.buffer(coverage, resolution=32)
            else:
                # 多个基站：融合覆盖区域
                circles = []
                for bs in bs_list:
                    if has_pyproj:
                        x, y = transformer.transform(bs['lon'], bs['lat'])
                        coverage = bs['coverage_radius']
                    else:
                        center_lat = mec['lat']
                        y = (bs['lat'] - center_lat) * 111320
                        x = (bs['lon'] - mec['lon']) * 111320 * np.cos(np.radians(center_lat))
                        coverage = bs['coverage_radius']
                    
                    point = Point(x, y)
                    circle = point.buffer(coverage, resolution=32)
                    circles.append(circle)
                
                # 融合所有圆形
                if circles:
                    try:
                        merged = unary_union(circles)
                        mec_coverage_polygons[mec['id']] = merged
                    except Exception as e:
                        print(f"    警告: MEC {mec['id']} 融合失败: {e}")
                        # 回退到使用第一个圆形
                        mec_coverage_polygons[mec['id']] = circles[0]
        
        # 检查每对MEC节点的融合覆盖区域是否重叠
        num_mecs = len(mec_nodes_info)
        
        for i in range(num_mecs):
            for j in range(i + 1, num_mecs):
                mec_i_id = mec_nodes_info[i]['id']
                mec_j_id = mec_nodes_info[j]['id']
                
                # 获取两个MEC的融合覆盖区域
                if mec_i_id not in mec_coverage_polygons or mec_j_id not in mec_coverage_polygons:
                    continue
                
                poly_i = mec_coverage_polygons[mec_i_id]
                poly_j = mec_coverage_polygons[mec_j_id]
                
                # 检查两个多边形是否相交（重叠）
                if poly_i.intersects(poly_j):
                    G.add_edge(mec_i_id, mec_j_id)
        
        return G
    
    def extract_largest_component(self, graph: nx.Graph, verbose: bool = False) -> nx.Graph:
        """
        提取最大连通分量
        
        Parameters:
        -----------
        graph : nx.Graph
            输入图
        verbose : bool
            是否打印信息
        
        Returns:
        --------
        nx.Graph : 最大连通分量子图
        """
        if nx.is_connected(graph):
            return graph
        
        largest_cc = max(nx.connected_components(graph), key=len)
        subgraph = graph.subgraph(largest_cc).copy()
        
        if verbose:
            original_nodes = graph.number_of_nodes()
            original_edges = graph.number_of_edges()
            new_nodes = subgraph.number_of_nodes()
            new_edges = subgraph.number_of_edges()
            
            print(f"提取最大连通分量: {original_nodes} -> {new_nodes} 节点, "
                  f"{original_edges} -> {new_edges} 边")
        
        return subgraph


if __name__ == '__main__':
    from .spatial_generator import SpatialTopologyGenerator
    generator = SpatialTopologyGenerator(area_size=1000, density=0.0001, coverage_radius=150, random_seed=42)
    spatial_topology = generator.generate()
    builder = MECTopologyBuilder()
    mec_graph = builder.build_from_spatial(spatial_topology)
    print(f"MEC图: {mec_graph.number_of_nodes()} 节点, {mec_graph.number_of_edges()} 边")


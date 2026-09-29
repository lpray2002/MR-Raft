"""真实基站数据加载和处理"""
import pandas as pd
import numpy as np
from typing import List, Tuple, Dict
from sklearn.cluster import KMeans
from .spatial_generator import BaseStation, SpatialTopology


class RealDataLoader:
    """加载和处理真实基站数据"""
    
    # 北京市中心坐标（天安门广场）
    BEIJING_CENTER = (116.3974, 39.9087)
    
    def __init__(self, csv_path: str):
        """
        初始化数据加载器
        
        Parameters:
        -----------
        csv_path : str
            CSV文件路径
        """
        self.csv_path = csv_path
        self.df = None
        
    def load_data(self) -> pd.DataFrame:
        """加载CSV数据"""
        column_names = [
            'radio', 'mcc', 'net', 'area', 'cell', 'unit',
            'lon', 'lat', 'range', 'samples', 'changeable',
            'created', 'updated', 'averageSignal'
        ]
        
        self.df = pd.read_csv(self.csv_path, names=column_names)
        print(f"加载了 {len(self.df)} 条基站数据")
        return self.df
    
    def filter_beijing_center(self, radius_km: float = 2.0,
                             center_lat: float = None,
                             center_lon: float = None) -> pd.DataFrame:
        """
        筛选北京市中心附近的基站
        
        Parameters:
        -----------
        radius_km : float
            半径范围（公里）
        center_lat : float, optional
            中心纬度（默认使用BEIJING_CENTER）
        center_lon : float, optional
            中心经度（默认使用BEIJING_CENTER）
        
        Returns:
        --------
        pd.DataFrame : 筛选后的数据
        """
        if self.df is None:
            self.load_data()
        
        # 使用提供的中心坐标，或默认的北京中心
        if center_lat is None or center_lon is None:
            center_lon, center_lat = self.BEIJING_CENTER
        
        # 将经纬度转换为弧度
        lat1 = np.radians(center_lat)
        lat2 = np.radians(self.df['lat'])
        lon1 = np.radians(center_lon)
        lon2 = np.radians(self.df['lon'])
        
        # Haversine公式（精确版本）
        dlon = lon2 - lon1
        dlat = lat2 - lat1
        
        a = np.sin(dlat/2)**2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon/2)**2
        c = 2 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))  # clip防止数值误差
        distance_km = 6371.0 * c  # 地球半径约6371km
        
        # 筛选范围内的基站，并只保留精确位置的基站
        mask = (distance_km <= radius_km)
        filtered_df = self.df[mask].copy()
        
        # 统计精确vs近似位置
        exact_count = (filtered_df['changeable'] == 0).sum()
        approx_count = (filtered_df['changeable'] == 1).sum()
        
        print(f"北京市中心 {radius_km}km 范围内找到 {len(filtered_df)} 个基站")
        print(f"  精确位置: {exact_count} 个, 近似位置: {approx_count} 个")
        
        return filtered_df
    
    def scale_coverage(self, df: pd.DataFrame, 
                      min_range: float = 300, 
                      max_range: float = 500) -> pd.DataFrame:
        """
        将基站覆盖范围缩放到指定范围
        
        Parameters:
        -----------
        df : pd.DataFrame
            基站数据
        min_range : float
            最小覆盖范围（米）
        max_range : float
            最大覆盖范围（米）
        
        Returns:
        --------
        pd.DataFrame : 带有缩放后覆盖范围的数据
        """
        df = df.copy()
        
        # 原始范围
        original_ranges = df['range'].values
        
        # 缩放到300-500m
        # 使用min-max归一化然后映射到目标范围
        min_orig = original_ranges.min()
        max_orig = original_ranges.max()
        
        if max_orig > min_orig:
            normalized = (original_ranges - min_orig) / (max_orig - min_orig)
            df['scaled_range'] = normalized * (max_range - min_range) + min_range
        else:
            df['scaled_range'] = (min_range + max_range) / 2
        
        print(f"覆盖范围从 [{min_orig:.0f}, {max_orig:.0f}]m 缩放到 [{min_range:.0f}, {max_range:.0f}]m")
        return df
    
    def cluster_mec_nodes(self, df: pd.DataFrame, n_clusters: int = None) -> Tuple[np.ndarray, np.ndarray]:
        """
        使用KMeans聚类选择MEC节点位置
        
        Parameters:
        -----------
        df : pd.DataFrame
            基站数据
        n_clusters : int, optional
            MEC节点数量，默认为基站数量的1/10
        
        Returns:
        --------
        Tuple[np.ndarray, np.ndarray] : (聚类标签, MEC节点位置)
        """
        if n_clusters is None:
            n_clusters = max(5, len(df) // 10)  # 默认为基站数的1/10，至少5个
        
        # 提取基站位置
        positions = df[['lon', 'lat']].values
        
        # KMeans聚类
        kmeans = KMeans(n_clusters=n_clusters, random_state=42, n_init=10)
        labels = kmeans.fit_predict(positions)
        mec_centers = kmeans.cluster_centers_
        
        print(f"使用KMeans将 {len(df)} 个基站聚类为 {n_clusters} 个MEC节点")
        return labels, mec_centers
    
    def build_mec_topology(self, df: pd.DataFrame, labels: np.ndarray, 
                          mec_centers: np.ndarray) -> Tuple[List[Dict], List[Dict]]:
        """
        构建MEC拓扑
        
        Parameters:
        -----------
        df : pd.DataFrame
            基站数据
        labels : np.ndarray
            聚类标签
        mec_centers : np.ndarray
            MEC节点位置（聚类中心）
        
        Returns:
        --------
        Tuple[List[Dict], List[Dict]] : (MEC节点列表, 基站列表)
        """
        mec_nodes = []
        base_stations = []
        
        # 为每个MEC节点计算覆盖范围
        for mec_id in range(len(mec_centers)):
            # 找到属于该MEC的所有基站
            cluster_mask = labels == mec_id
            cluster_bs = df[cluster_mask]
            
            if len(cluster_bs) == 0:
                continue
            
            # 聚类中心位置
            center_lon, center_lat = mec_centers[mec_id]
            
            # 找到距离聚类中心最近的实际基站作为MEC节点位置（使用精确的Haversine公式）
            min_distance = float('inf')
            nearest_bs_idx = None
            nearest_bs_lon = center_lon
            nearest_bs_lat = center_lat
            
            for idx, bs in cluster_bs.iterrows():
                # 使用Haversine公式计算精确距离
                lat1, lon1 = np.radians(center_lat), np.radians(center_lon)
                lat2, lon2 = np.radians(bs['lat']), np.radians(bs['lon'])
                
                dlat = lat2 - lat1
                dlon = lon2 - lon1
                
                a = np.sin(dlat/2)**2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon/2)**2
                c = 2 * np.arcsin(np.sqrt(a))
                distance = 6371000 * c  # 转换为米
                
                if distance < min_distance:
                    min_distance = distance
                    nearest_bs_idx = idx
                    nearest_bs_lon = bs['lon']
                    nearest_bs_lat = bs['lat']
            
            # MEC节点位于最近的基站位置
            mec_lon, mec_lat = nearest_bs_lon, nearest_bs_lat
            
            # 收集该MEC服务的所有基站信息（用于绘制合并覆盖范围）
            cluster_bs_info = []
            max_distance = 0
            
            for _, bs in cluster_bs.iterrows():
                # 使用Haversine公式计算基站到MEC节点的精确距离
                lat1, lon1 = np.radians(mec_lat), np.radians(mec_lon)
                lat2, lon2 = np.radians(bs['lat']), np.radians(bs['lon'])
                
                dlat = lat2 - lat1
                dlon = lon2 - lon1
                
                a = np.sin(dlat/2)**2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon/2)**2
                c = 2 * np.arcsin(np.sqrt(a))
                distance = 6371000 * c  # 转换为米
                
                # 加上基站自身的覆盖范围
                total_distance = distance + bs['scaled_range']
                max_distance = max(max_distance, total_distance)
                
                # 保存基站信息
                cluster_bs_info.append({
                    'lon': bs['lon'],
                    'lat': bs['lat'],
                    'coverage_radius': bs['scaled_range']
                })
            
            mec_nodes.append({
                'id': mec_id,
                'lon': mec_lon,
                'lat': mec_lat,
                'coverage_radius': max_distance,
                'num_base_stations': len(cluster_bs),
                'base_stations': cluster_bs_info  # 添加基站信息
            })
            
            # 记录属于该MEC的基站
            for idx, bs in cluster_bs.iterrows():
                base_stations.append({
                    'id': len(base_stations),
                    'mec_id': mec_id,
                    'lon': bs['lon'],
                    'lat': bs['lat'],
                    'coverage_radius': bs['scaled_range'],
                    'radio': bs['radio']
                })
        
        print(f"构建了 {len(mec_nodes)} 个MEC节点，覆盖 {len(base_stations)} 个基站")
        return mec_nodes, base_stations
    
    def convert_to_meters(self, lon: float, lat: float, 
                         center_lon: float = None, 
                         center_lat: float = None) -> Tuple[float, float]:
        """
        将经纬度转换为以中心为原点的米制坐标（使用更精确的Web Mercator投影）
        
        Parameters:
        -----------
        lon, lat : float
            经纬度
        center_lon, center_lat : float, optional
            中心点经纬度，默认为北京中心
        
        Returns:
        --------
        Tuple[float, float] : (x, y) 米制坐标
        """
        if center_lon is None or center_lat is None:
            center_lon, center_lat = self.BEIJING_CENTER
        
        # 使用更精确的方法：在小范围内使用局部平面近似
        # 在纬度center_lat处，1度经度对应的距离
        meters_per_degree_lon = 111320 * np.cos(np.radians(center_lat))
        # 1度纬度始终约为111320米
        meters_per_degree_lat = 111320
        
        # 转换为相对于中心的米制坐标
        x = (lon - center_lon) * meters_per_degree_lon
        y = (lat - center_lat) * meters_per_degree_lat
        
        return x, y
    
    def create_spatial_topology(self, mec_nodes: List[Dict], 
                               base_stations: List[Dict]) -> SpatialTopology:
        """
        创建SpatialTopology对象
        
        Parameters:
        -----------
        mec_nodes : List[Dict]
            MEC节点列表
        base_stations : List[Dict]
            基站列表
        
        Returns:
        --------
        SpatialTopology : 空间拓扑对象
        """
        # 计算数据范围（以第一个MEC节点为中心）
        if len(mec_nodes) == 0:
            raise ValueError("没有MEC节点")
        
        center_lon = mec_nodes[0]['lon']
        center_lat = mec_nodes[0]['lat']
        
        # 转换MEC节点坐标
        bs_objects = []
        for mec in mec_nodes:
            x, y = self.convert_to_meters(mec['lon'], mec['lat'], center_lon, center_lat)
            bs = BaseStation(
                station_id=mec['id'],
                x=x,
                y=y,
                coverage_radius=mec['coverage_radius']
            )
            bs_objects.append(bs)
        
        # 计算区域大小（包含所有MEC节点的覆盖范围）
        all_x = [bs.x for bs in bs_objects]
        all_y = [bs.y for bs in bs_objects]
        all_ranges = [bs.coverage_radius for bs in bs_objects]
        
        min_x = min(x - r for x, r in zip(all_x, all_ranges))
        max_x = max(x + r for x, r in zip(all_x, all_ranges))
        min_y = min(y - r for y, r in zip(all_y, all_ranges))
        max_y = max(y + r for y, r in zip(all_y, all_ranges))
        
        area_size = max(max_x - min_x, max_y - min_y)
        
        # 平移坐标使其都为正数
        offset_x = -min_x
        offset_y = -min_y
        
        for bs in bs_objects:
            bs.x += offset_x
            bs.y += offset_y
        
        # 创建SpatialTopology对象
        spatial = SpatialTopology(
            area_size=area_size,
            base_stations=bs_objects
        )
        
        return spatial


def load_beijing_data(csv_path: str = 'data/real_topology/460.csv',
                     center_lat: float = 39.9087,
                     center_lon: float = 116.3974,
                     radius_km: float = 2.0,
                     min_range: float = 300,
                     max_range: float = 500,
                     n_mec_nodes: int = None,
                     use_exact_only: bool = False) -> Tuple[SpatialTopology, pd.DataFrame, List[Dict]]:
    """
    加载北京真实基站数据并构建MEC拓扑
    
    Parameters:
    -----------
    csv_path : str
        CSV文件路径
    center_lat : float
        中心纬度
    center_lon : float
        中心经度
    radius_km : float
        筛选半径（公里）
    min_range, max_range : float
        覆盖范围缩放区间（米）
    n_mec_nodes : int, optional
        MEC节点数量
    use_exact_only : bool
        是否只使用精确位置的基站（changeable=0）
    
    Returns:
    --------
    Tuple[SpatialTopology, pd.DataFrame, List[Dict]] : 
        (空间拓扑, 筛选后的基站数据, MEC节点信息)
    """
    loader = RealDataLoader(csv_path)
    
    # 1. 加载并筛选数据
    df_filtered = loader.filter_beijing_center(
        radius_km,
        center_lat=center_lat,
        center_lon=center_lon
    )
    
    # 可选：只保留精确位置的基站
    if use_exact_only:
        before_count = len(df_filtered)
        df_filtered = df_filtered[df_filtered['changeable'] == 0].copy()
        print(f"只使用精确位置基站: {len(df_filtered)}/{before_count} 个")
    
    # 2. 缩放覆盖范围
    df_scaled = loader.scale_coverage(df_filtered, min_range, max_range)
    
    # 3. KMeans聚类选择MEC节点
    labels, mec_centers = loader.cluster_mec_nodes(df_scaled, n_mec_nodes)
    
    # 4. 构建MEC拓扑
    mec_nodes, base_stations = loader.build_mec_topology(df_scaled, labels, mec_centers)
    
    # 5. 创建SpatialTopology对象
    spatial = loader.create_spatial_topology(mec_nodes, base_stations)
    
    return spatial, df_scaled, mec_nodes


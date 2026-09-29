"""
拓扑可视化工具
提供空间拓扑和图拓扑的可视化功能
"""

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import networkx as nx
import numpy as np
from typing import List, Set, Dict, Optional, Tuple
import os

try:
    import contextily as ctx
    HAS_CONTEXTILY = True
except ImportError:
    HAS_CONTEXTILY = False


# 定义颜色方案（统一使用）
COLORS = [
    '#0273bc', '#598dca', '#87a8d7', '#b1c4e5', '#d8e1f2',
    '#fbd7d3', '#f2aea8', '#e6867f', '#d65c58', '#c32934'
]

# 空间拓扑图的固定页面边距。仿真与真实拓扑共用该布局，保证输出 PDF 的
# 页面边界和正方形坐标框一致，便于在 LaTeX 中按等宽并列对齐。
SPATIAL_TOPOLOGY_LAYOUT = {
    'left': 0.18,
    'bottom': 0.17,
    'right': 0.95,
    'top': 0.85,
}


class TopologyPlotter:
    """拓扑可视化器"""
    
    def __init__(self, figsize: Tuple[int, int] = (6, 5), dpi: int = 100):
        """
        初始化可视化器
        
        Parameters:
        -----------
        figsize : Tuple[int, int]
            图像大小
        dpi : int
            图像分辨率
        """
        self.figsize = figsize
        self.dpi = dpi
        
        # 设置matplotlib参数
        plt.rcParams['font.size'] = 13
        plt.rcParams['axes.titlesize'] = 16
        plt.rcParams['axes.labelsize'] = 15
        # 保证导出的矢量图中文字为可编辑文字对象（便于手动移动标签）
        plt.rcParams['svg.fonttype'] = 'none'   # SVG 文字保留为 <text>，不转路径
        plt.rcParams['pdf.fonttype'] = 42       # PDF 使用 TrueType，文字可选可编辑
    
    def plot_spatial_topology(self, 
                             spatial_topology,
                             mec_graph=None,
                             density: float = None,
                             show_coverage: bool = True,
                             show_labels: bool = False,
                             station_marker: str = 'o',
                             node_color: str = '#87a8d7',
                             output_file: Optional[str] = None,
                             show: bool = True,
                             ax=None):
        """
        绘制空间拓扑
        
        Parameters:
        -----------
        spatial_topology : SpatialTopology
            空间拓扑对象
        mec_graph : nx.Graph, optional
            MEC拓扑图（用于计算统计信息）
        density : float, optional
            基站密度λ_bs
        show_coverage : bool
            是否显示覆盖范围
        show_labels : bool
            是否显示基站标签
        station_marker : str
            基站标记样式 ('o'=圆形, 's'=正方形, '^'=三角形等)
        output_file : str, optional
            输出文件路径
        show : bool
            是否显示图像
        ax : matplotlib.axes.Axes, optional
            外部传入的 axes（用于嵌入到交互式界面）
        """
        if ax is None:
            fig, ax = plt.subplots(figsize=self.figsize, dpi=self.dpi)
            own_fig = True
        else:
            fig = ax.get_figure()
            own_fig = False
        
        # 绘制覆盖范围
        if show_coverage:
            for bs in spatial_topology.base_stations:
                circle = mpatches.Circle(
                    (bs.x, bs.y), 
                    bs.coverage_radius,
                    fill=True,
                    facecolor=node_color,
                    edgecolor='none',
                    alpha=0.15,
                    linewidth=0
                )
                ax.add_patch(circle)
        
        # 绘制基站位置
        x_coords = [bs.x for bs in spatial_topology.base_stations]
        y_coords = [bs.y for bs in spatial_topology.base_stations]
        
        ax.scatter(x_coords, y_coords, 
                  c=node_color, s=200, marker=station_marker,  # 增大节点
                  edgecolors='white', linewidths=3,  # 增粗边缘
                  zorder=5, label='Base Station')
        
        # 添加标签
        if show_labels:
            for bs in spatial_topology.base_stations:
                ax.text(bs.x, bs.y + bs.coverage_radius * 0.18, 
                       str(bs.id),
                       ha='center', va='bottom',
                       fontsize=16, fontweight='bold')  # 增大字体
        
        # 计算统计信息并构建标题
        num_nodes = spatial_topology.num_stations
        title_lines = []
        
        if density:
            # 使用科学计数法显示密度
            # 将 2e-06 转换为 2 × 10^-6
            exp_str = f'{density:.0e}'
            mantissa, exponent = exp_str.split('e')
            exponent = int(exponent)
            title_lines.append(f'$\\lambda_{{bs}} = {mantissa} \\times 10^{{{exponent}}}$')
        
        if mec_graph:
            num_edges = mec_graph.number_of_edges()
            avg_degree = sum(dict(mec_graph.degree()).values()) / num_nodes if num_nodes > 0 else 0
            graph_density = nx.density(mec_graph)
            clustering = nx.average_clustering(mec_graph)
            
            title_lines.append(f'N={num_nodes}, E={num_edges}')
            title_lines.append(f'Avg Degree={avg_degree:.2f}, Density={graph_density:.3f}')
            title_lines.append(f'Clustering={clustering:.3f}')
        else:
            title_lines.append(f'N={num_nodes}')
        
        title = '\n'.join(title_lines)
        
        # 设置坐标轴（删除title用于论文展示）
        ax.set_xlim(0, spatial_topology.area_size)
        ax.set_ylim(0, spatial_topology.area_size)
        ax.set_xlabel('X (m)', fontweight='bold', fontsize=18)  # 增大字体
        ax.set_ylabel('Y (m)', fontweight='bold', fontsize=18)  # 增大字体
        # ax.set_title(title, fontweight='bold', fontsize=16, pad=20)  # 论文图不需要title
        ax.set_aspect('equal')
        ax.grid(True, alpha=0.25, linestyle='-', linewidth=0.5, color='gray')
        ax.tick_params(axis='both', labelsize=14)  # 增大刻度字体
        
        # 添加图例（自动选择最佳位置避免遮挡）
        legend_elements = [
            plt.scatter([], [], c=node_color, s=120, marker=station_marker,  # 增大图例标记
                       edgecolors='white', linewidths=2.5, label='MEC',),
            mpatches.Patch(facecolor=node_color, alpha=0.15, 
                          edgecolor='none', label='Coverage')
        ]
        ax.legend(handles=legend_elements,
                 loc='lower center', bbox_to_anchor=(0.5, 1.01),
                 ncol=len(legend_elements),  # 单行排列
                 framealpha=0.95, fontsize=16,  # 增大字体
                 edgecolor='gray', fancybox=False,
                 columnspacing=1.0, handletextpad=0.4)
        
        if own_fig:
            fig.subplots_adjust(**SPATIAL_TOPOLOGY_LAYOUT)
            
            if output_file:
                os.makedirs(os.path.dirname(output_file) if os.path.dirname(output_file) else '.', 
                           exist_ok=True)
                plt.savefig(output_file, dpi=self.dpi)
                pdf_file = os.path.splitext(output_file)[0] + '.pdf'
                plt.savefig(pdf_file)
                print(f"已保存: {output_file}")
                print(f"已保存: {pdf_file}")
            
            if show:
                plt.show()
            else:
                plt.close()
        
        return ax
    
    def plot_real_spatial_topology(self,
                                   spatial_topology,
                                   mec_nodes_info: List[Dict],
                                   mec_graph=None,
                                   mec_color: str = '#87a8d7',
                                   output_file: Optional[str] = None,
                                   show: bool = True,
                                   ax=None):
        """
        绘制真实地理位置的空间拓扑（带地图背景）
        
        Parameters:
        -----------
        spatial_topology : SpatialTopology
            空间拓扑对象（包含真实经纬度的基站）
        mec_nodes_info : List[Dict]
            MEC节点信息列表，每个包含 {id, lon, lat, base_stations, ...}
        mec_graph : nx.Graph, optional
            MEC拓扑图（用于计算统计信息，只绘制其中的节点）
        mec_color : str
            MEC节点和覆盖区域的统一颜色
        output_file : str, optional
            输出文件路径
        show : bool
            是否显示图像
        ax : matplotlib.axes.Axes, optional
            外部传入的 axes（用于嵌入到交互式界面）
        """
        from shapely.geometry import Point
        from shapely.ops import unary_union
        
        if ax is None:
            fig, ax = plt.subplots(figsize=self.figsize, dpi=self.dpi)
            own_fig = True
        else:
            fig = ax.get_figure()
            own_fig = False
        
        # 从MEC节点信息中收集所有基站的经纬度
        all_base_stations = []
        for mec in mec_nodes_info:
            for bs in mec.get('base_stations', []):
                all_base_stations.append(bs)
        
        if len(all_base_stations) == 0:
            print("  警告: 没有基站数据")
            return
        
        # 计算中心点
        lons = [bs['lon'] for bs in all_base_stations]
        lats = [bs['lat'] for bs in all_base_stations]
        center_lon = np.mean(lons)
        center_lat = np.mean(lats)
        
        # 使用Web Mercator投影（EPSG:3857）以便添加地图背景
        try:
            from pyproj import Transformer
            has_pyproj = True
            transformer = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
        except ImportError:
            has_pyproj = False
            print("  警告: pyproj未安装，将使用简化坐标转换")
            print("  建议运行: pip install pyproj")
        
        if has_pyproj:
            # 转换所有基站坐标为Web Mercator
            xs_merc = []
            ys_merc = []
            for bs in all_base_stations:
                x, y = transformer.transform(bs['lon'], bs['lat'])
                xs_merc.append(x)
                ys_merc.append(y)
        else:
            # 简化转换（km单位）
            def lonlat_to_km(lon, lat, center_lon, center_lat):
                y_km = (lat - center_lat) * 111320  # 米
                x_km = (lon - center_lon) * 111320 * np.cos(np.radians(center_lat))
                return x_km, y_km
            
            xs_merc = []
            ys_merc = []
            for bs in all_base_stations:
                x, y = lonlat_to_km(bs['lon'], bs['lat'], center_lon, center_lat)
                xs_merc.append(x)
                ys_merc.append(y)
        
        # 确定要绘制的MEC节点（如果提供了mec_graph，只绘制其中的节点）
        if mec_graph:
            valid_mec_ids = set(mec_graph.nodes())
            mec_nodes_to_plot = [mec for mec in mec_nodes_info if mec['id'] in valid_mec_ids]
        else:
            mec_nodes_to_plot = mec_nodes_info
        
        # 1. 绘制MEC覆盖范围（融合显示，统一颜色）
        print("  绘制MEC融合覆盖范围...")
        for mec in mec_nodes_to_plot:
            color = mec_color  # 使用统一颜色
            
            if len(mec.get('base_stations', [])) == 1:
                # 单个基站：绘制圆形
                bs = mec['base_stations'][0]
                if has_pyproj:
                    x, y = transformer.transform(bs['lon'], bs['lat'])
                    coverage = bs['coverage_radius']  # 米
                else:
                    x, y = lonlat_to_km(bs['lon'], bs['lat'], center_lon, center_lat)
                    coverage = bs['coverage_radius']  # 米
                
                circle = mpatches.Circle(
                    (x, y),
                    coverage,
                    fill=True,
                    facecolor=color,
                    edgecolor='none',
                    alpha=0.2,
                    linewidth=0
                )
                ax.add_patch(circle)
            else:
                # 多个基站：融合显示
                circles = []
                for bs in mec.get('base_stations', []):
                    if has_pyproj:
                        x, y = transformer.transform(bs['lon'], bs['lat'])
                    else:
                        x, y = lonlat_to_km(bs['lon'], bs['lat'], center_lon, center_lat)
                    
                    coverage = bs['coverage_radius']  # 米
                    point = Point(x, y)
                    circle = point.buffer(coverage, resolution=32)
                    circles.append(circle)
                
                if circles:
                    try:
                        merged = unary_union(circles)
                        
                        if merged.geom_type == 'Polygon':
                            coords = list(merged.exterior.coords)
                            polygon = mpatches.Polygon(
                                coords,
                                fill=True,
                                facecolor=color,
                                edgecolor=color,
                                alpha=0.2,
                                linewidth=2.5
                            )
                            ax.add_patch(polygon)
                        elif merged.geom_type == 'MultiPolygon':
                            for poly in merged.geoms:
                                coords = list(poly.exterior.coords)
                                polygon = mpatches.Polygon(
                                    coords,
                                    fill=True,
                                    facecolor=color,
                                    edgecolor=color,
                                    alpha=0.2,
                                    linewidth=2.5
                                )
                                ax.add_patch(polygon)
                    except Exception as e:
                        print(f"    警告: MEC {mec['id']} 融合失败: {e}")
                        # 回退到单独绘制
                        for bs in mec.get('base_stations', []):
                            if has_pyproj:
                                x, y = transformer.transform(bs['lon'], bs['lat'])
                            else:
                                x, y = lonlat_to_km(bs['lon'], bs['lat'], center_lon, center_lat)
                            
                            coverage = bs['coverage_radius']
                            circle = mpatches.Circle(
                                (x, y),
                                coverage,
                                fill=True,
                                facecolor=color,
                                edgecolor='none',
                                alpha=0.15,
                                linewidth=0
                            )
                            ax.add_patch(circle)
        
        # 2. 绘制基站（小正方形）
        ax.scatter(xs_merc, ys_merc,
                  c='#808080', s=10, marker='s',
                  edgecolors='#555', linewidths=1.0,
                  zorder=5, label='Base Station', alpha=0.9)
        
        # 3. 绘制MEC节点（大圆点，统一颜色）
        mec_xs = []
        mec_ys = []
        for mec in mec_nodes_to_plot:
            if has_pyproj:
                x, y = transformer.transform(mec['lon'], mec['lat'])
            else:
                x, y = lonlat_to_km(mec['lon'], mec['lat'], center_lon, center_lat)
            mec_xs.append(x)
            mec_ys.append(y)
        
        ax.scatter(mec_xs, mec_ys,
                  c=mec_color, s=260, marker='o',
                  edgecolors='white', linewidths=1,
                  zorder=10, label='MEC Node', alpha=1.0)
        
        # 添加MEC节点编号标签（在节点中心）
        for i, (mec, x, y) in enumerate(zip(mec_nodes_to_plot, mec_xs, mec_ys)):
            ax.text(x, y, str(mec['id']),
                   fontsize=9, fontweight='bold',
                   ha='center', va='center',
                   color='black',
                   zorder=11)  # 确保标签在最上层

        # 真实拓扑的地理范围通常并非正方形。先将较短方向补齐为相同跨度，
        # 再请求底图瓦片，避免扩展坐标范围后出现无底图的空白区域。
        if has_pyproj:
            xlim = ax.get_xlim()
            ylim = ax.get_ylim()
            span = max(xlim[1] - xlim[0], ylim[1] - ylim[0])
            center_x = (xlim[0] + xlim[1]) / 2
            center_y = (ylim[0] + ylim[1]) / 2
            ax.set_xlim(center_x - span / 2, center_x + span / 2)
            ax.set_ylim(center_y - span / 2, center_y + span / 2)
        
        # 添加地图背景
        if has_pyproj and HAS_CONTEXTILY:
            print("  添加地图背景...")
            try:
                ctx.add_basemap(
                    ax,
                    source=ctx.providers.CartoDB.Positron,  # 简洁学术风格
                    zoom='auto',
                    alpha=0.9,
                    reset_extent=True,
                )
            except Exception as e:
                print(f"    警告: 添加地图背景失败: {e}")
        
        # 设置坐标轴为km单位（即使使用了Web Mercator投影）
        if has_pyproj:
            # 创建坐标转换函数（将Web Mercator转换为km）
            from matplotlib.ticker import FuncFormatter
            
            def merc_to_km_x(x, pos):
                # 将Web Mercator的x坐标转换为相对于中心的km
                return f'{(x - center_x) / 1000:.1f}'
            
            def merc_to_km_y(y, pos):
                # 将Web Mercator的y坐标转换为相对于中心的km
                return f'{(y - center_y) / 1000:.1f}'
            
            # 应用坐标格式化器
            ax.xaxis.set_major_formatter(FuncFormatter(merc_to_km_x))
            ax.yaxis.set_major_formatter(FuncFormatter(merc_to_km_y))
            
            ax.set_xlabel('X (km)', fontweight='bold', fontsize=18)
            ax.set_ylabel('Y (km)', fontweight='bold', fontsize=18)
        else:
            ax.set_xlabel('X (m)', fontweight='bold', fontsize=18)
            ax.set_ylabel('Y (m)', fontweight='bold', fontsize=18)
        
        ax.set_aspect('equal')
        ax.grid(True, alpha=0.25, linestyle='-', linewidth=0.5, color='gray')
        ax.tick_params(axis='both', labelsize=14)
        
        # 添加图例（自动选择最佳位置避免遮挡）
        legend_elements = [
            plt.scatter([], [], c='#808080', s=50, marker='s',  # 与实际大小一致
                       edgecolors='#555', linewidths=1.5, label='BS'),
            plt.scatter([], [], c=mec_color, s=150, marker='o',
                       edgecolors='white', linewidths=3, label='MEC'),
            mpatches.Patch(facecolor=mec_color, alpha=0.2,
                          edgecolor=mec_color, linewidth=2.5, label='Coverage')
        ]
        ax.legend(handles=legend_elements,
                 loc='lower center', bbox_to_anchor=(0.5, 1.01),
                 ncol=len(legend_elements),  # 单行排列
                 framealpha=0.95, fontsize=16,
                 edgecolor='gray', fancybox=False,
                 columnspacing=1.0, handletextpad=0.4)
        
        # # 统计信息（如果有MEC图）
        # if mec_graph:
        #     num_nodes = mec_graph.number_of_nodes()
        #     num_edges = mec_graph.number_of_edges()
        #     avg_degree = sum(dict(mec_graph.degree()).values()) / num_nodes if num_nodes > 0 else 0
        #
        #     # 添加文本框显示统计信息
        #     stats_text = f'Nodes={num_nodes}, Edges={num_edges}\nAvg Degree={avg_degree:.2f}'
        #     ax.text(0.02, 0.98, stats_text,
        #            transform=ax.transAxes,
        #            fontsize=13,
        #            verticalalignment='top',
        #            bbox=dict(boxstyle='round', facecolor='white', alpha=0.9, edgecolor='gray'))
        
        if own_fig:
            fig.subplots_adjust(**SPATIAL_TOPOLOGY_LAYOUT)
            
            if output_file:
                os.makedirs(os.path.dirname(output_file) if os.path.dirname(output_file) else '.',
                           exist_ok=True)
                plt.savefig(output_file, bbox_inches='tight', dpi=self.dpi)
                pdf_file = os.path.splitext(output_file)[0] + '.pdf'
                plt.savefig(pdf_file, bbox_inches='tight')
                print(f"已保存: {output_file}")
                print(f"已保存: {pdf_file}")
            
            if show:
                plt.show()
            else:
                plt.close()
        
        return ax
    
    def plot_mec_graph(self,
                      graph: nx.Graph,
                      spatial_topology=None,
                      density: float = None,
                      layout: str = 'kamada_kawai',
                      node_size: int = 400,
                      node_color: str = '#87a8d7',
                      output_file: Optional[str] = None,
                      show: bool = True,
                      show_labels: bool = True):
        """
        绘制MEC拓扑图（未分区）
        
        Parameters:
        -----------
        graph : nx.Graph
            MEC拓扑图
        spatial_topology : SpatialTopology, optional
            空间拓扑对象，用于获取节点位置
        density : float, optional
            基站密度λ_bs
        layout : str
            图布局算法，默认 'kamada_kawai'
        node_size : int
            节点大小，默认 600
        node_color : str
            节点颜色，默认浅蓝色 '#87a8d7'
        output_file : str, optional
            输出文件路径
        show : bool
            是否显示图像
        """
        fig, ax = plt.subplots(figsize=self.figsize, dpi=self.dpi)
        
        # 确定节点位置
        if layout == 'spatial' and spatial_topology:
            pos = {bs.id: (bs.x, bs.y) for bs in spatial_topology.base_stations}
        elif layout == 'kamada_kawai':
            pos = nx.kamada_kawai_layout(graph)
        elif layout == 'spring':
            pos = nx.spring_layout(graph, seed=42, k=2, iterations=50)
        elif layout == 'circular':
            pos = nx.circular_layout(graph)
        else:
            pos = nx.kamada_kawai_layout(graph)
        
        # 绘制边 - 深色、加粗
        nx.draw_networkx_edges(graph, pos, ax=ax,
                              edge_color='dimgray', alpha=0.8, width=3)
        
        # 绘制节点（无透明度）
        nx.draw_networkx_nodes(graph, pos, ax=ax,
                              node_color=node_color,
                              node_size=node_size,
                              edgecolors='k',
                              linewidths=2)
        
        # 标注节点（无背景框，自适应偏移）
        if show_labels:
            y_coords = [y for x, y in pos.values()]
            y_range = max(y_coords) - min(y_coords) if y_coords else 1.0
            label_offset = y_range * 0.04  # 标签偏移为y轴范围

            for node, (x, y) in pos.items():
                ax.text(x, y + label_offset, str(node),
                       fontsize=17, ha='center', va='bottom', fontweight='bold')
        
        # 计算统计信息并构建标题
        num_nodes = graph.number_of_nodes()
        num_edges = graph.number_of_edges()
        avg_degree = sum(dict(graph.degree()).values()) / num_nodes if num_nodes > 0 else 0
        
        title_lines = []
        if density:
            # 使用科学计数法显示密度
            exp_str = f'{density:.0e}'
            mantissa, exponent = exp_str.split('e')
            exponent = int(exponent)
            title_lines.append(f'$\\lambda_{{bs}} = {mantissa} \\times 10^{{{exponent}}}$')
        
        title_lines.append(f'Nodes={num_nodes}, Edges={num_edges}')
        title_lines.append(f'Avg Degree={avg_degree:.2f}')
        
        title = '\n'.join(title_lines)
        
        # ax.set_title(title, fontweight='bold', fontsize=16, pad=20)  # 论文图不需要title
        ax.axis('off')
        
        # 如果使用spatial布局，设置合适的坐标范围
        if layout == 'spatial' and spatial_topology:
            ax.set_xlim(-50, spatial_topology.area_size + 50)
            ax.set_ylim(-50, spatial_topology.area_size + 50)
            ax.set_aspect('equal')
        
        plt.tight_layout()
        
        if output_file:
            os.makedirs(os.path.dirname(output_file) if os.path.dirname(output_file) else '.', 
                       exist_ok=True)
            plt.savefig(output_file, bbox_inches='tight', dpi=self.dpi)
            pdf_file = os.path.splitext(output_file)[0] + '.pdf'
            plt.savefig(pdf_file, bbox_inches='tight')
            print(f"已保存: {output_file}")
            print(f"已保存: {pdf_file}")
        
        if show:
            plt.show()
        else:
            plt.close()
    
    def plot_partitions(self,
                       graph: nx.Graph,
                       partitions: List[Set[int]],
                       spatial_topology=None,
                       density: float = None,
                       layout: str = 'spring',
                       title: str = 'Partition Result',
                       output_file: Optional[str] = None,
                       show: bool = True,
                       style: str = 'default',
                       label_fontsize: int = 21):
        """
        绘制分区结果
        
        Parameters:
        -----------
        graph : nx.Graph
            MEC拓扑图
        partitions : List[Set[int]]
            分区列表
        spatial_topology : SpatialTopology, optional
            空间拓扑对象
        density : float, optional
            基站密度
        layout : str
            图布局算法
        title : str
            图标题
        output_file : str, optional
            输出文件路径
        show : bool
            是否显示图像
        """
        fig, ax = plt.subplots(figsize=self.figsize, dpi=self.dpi)
        
        # 确定节点位置
        if layout == 'spatial' and spatial_topology:
            pos = {bs.id: (bs.x, bs.y) for bs in spatial_topology.base_stations}
        elif layout == 'kamada_kawai':
            pos = nx.kamada_kawai_layout(graph)
        elif layout == 'spring':
            pos = nx.spring_layout(graph, seed=42, k=2, iterations=50)
        elif layout == 'circular':
            pos = nx.circular_layout(graph)
        else:
            pos = nx.kamada_kawai_layout(graph)  # 默认使用 kamada_kawai
        
        # 绘制边 - 深色、加粗
        nx.draw_networkx_edges(graph, pos, ax=ax,
                              edge_color='dimgray', alpha=0.8, width=3)
        
        # 按分区绘制节点（使用全局配色方案，无透明度）
        for idx, part in enumerate(partitions):
            nx.draw_networkx_nodes(
                graph, pos, ax=ax,
                nodelist=list(part),
                node_color=COLORS[idx % len(COLORS)],
                node_size=400,
                edgecolors='k',
                linewidths=2
            )
        
        # 为每个节点创建标签（节点ID [所属分区编号列表]）
        node_to_partitions = {node: [] for node in graph.nodes()}
        for partition_id, partition in enumerate(partitions):
            for node in partition:
                node_to_partitions[node].append(partition_id + 1)  # 1-based 编号
        
        # 使用 text 添加标签（无背景框，自适应偏移）
        y_coords = [y for x, y in pos.values()]
        y_range = max(y_coords) - min(y_coords) if y_coords else 1.0
        label_offset = y_range * 0.04  # 标签偏移为y轴范围的6.5%，避免与节点重叠

        for node, (x, y) in pos.items():
            groups = node_to_partitions[node]
            label_text = ','.join(map(str, groups))
            ax.text(x, y + label_offset, f"{node} [{label_text}]",
                   fontsize=label_fontsize,
                   ha='center',
                   va='bottom',
                   fontweight='bold')
        
        # 构建标题
        node_to_partition_count = {node: len(node_to_partitions[node]) for node in graph.nodes()}
        max_instances = max(node_to_partition_count.values()) if node_to_partition_count else 0
        
        title_lines = []
        if density:
            exp_str = f'{density:.0e}'
            mantissa, exponent = exp_str.split('e')
            exponent = int(exponent)
            title_lines.append(f'$\\lambda_{{bs}} = {mantissa} \\times 10^{{{exponent}}}$')
        
        title_lines.append(f'{title}')
        title_lines.append(f'Partitions={len(partitions)}, Max Instances={max_instances}')
        
        # ax.set_title('\n'.join(title_lines), fontweight='bold', fontsize=16, pad=20)  # 论文图不需要title
        ax.axis('off')
        
        plt.tight_layout()
        
        if output_file:
            os.makedirs(os.path.dirname(output_file) if os.path.dirname(output_file) else '.', 
                       exist_ok=True)
            plt.savefig(output_file, bbox_inches='tight', dpi=self.dpi)
            pdf_file = os.path.splitext(output_file)[0] + '.pdf'
            plt.savefig(pdf_file, bbox_inches='tight')
            # SVG：每个标签为独立可移动文字对象，便于在 Illustrator/Inkscape 手动微调
            svg_file = os.path.splitext(output_file)[0] + '.svg'
            plt.savefig(svg_file, bbox_inches='tight')
            print(f"已保存: {output_file}")
            print(f"已保存: {pdf_file}")
            print(f"已保存: {svg_file}")
        
        if show:
            plt.show()
        else:
            plt.close()
    
    def plot_comparison(self,
                       graph: nx.Graph,
                       partition_dict: Dict[str, List[Set[int]]],
                       spatial_topology=None,
                       layout: str = 'spring',
                       output_file: Optional[str] = None,
                       show: bool = True):
        """
        绘制多个策略的对比图
        
        Parameters:
        -----------
        graph : nx.Graph
            MEC拓扑图
        partition_dict : Dict[str, List[Set[int]]]
            策略名称到分区的映射
        spatial_topology : SpatialTopology, optional
            空间拓扑对象
        layout : str
            图布局算法
        output_file : str, optional
            输出文件路径
        show : bool
            是否显示图像
        """
        num_strategies = len(partition_dict)
        ncols = 2
        nrows = (num_strategies + 1) // 2
        
        fig, axes = plt.subplots(nrows, ncols, 
                                figsize=(self.figsize[0], self.figsize[1] * nrows / 2),
                                dpi=self.dpi)
        
        if num_strategies == 1:
            axes = np.array([[axes]])
        elif nrows == 1:
            axes = axes.reshape(1, -1)
        
        # 确定节点位置（所有子图使用相同位置）
        if layout == 'spatial' and spatial_topology:
            pos = {bs.id: (bs.x, bs.y) for bs in spatial_topology.base_stations}
        elif layout == 'spring':
            pos = nx.spring_layout(graph, seed=42)
        else:
            pos = nx.spring_layout(graph, seed=42)
        
        # 绘制每个策略
        for idx, (strategy_name, partitions) in enumerate(partition_dict.items()):
            row = idx // ncols
            col = idx % ncols
            ax = axes[row, col]
            
            # 节点实例数
            node_to_partition_count = {node: 0 for node in graph.nodes()}
            for partition in partitions:
                for node in partition:
                    node_to_partition_count[node] += 1
            
            # 绘制边
            nx.draw_networkx_edges(graph, pos, ax=ax,
                                  edge_color='gray', alpha=0.3, width=0.5)
            
            # 绘制节点
            node_colors = [node_to_partition_count[node] for node in graph.nodes()]
            nx.draw_networkx_nodes(graph, pos, ax=ax,
                                  node_color=node_colors,
                                  cmap=plt.cm.YlOrRd,
                                  node_size=400,
                                  edgecolors='black',
                                  linewidths=1.5)
            
            # 绘制标签
            nx.draw_networkx_labels(graph, pos, ax=ax,
                                   font_size=7,
                                   font_weight='bold')
            
            max_instances = max(node_colors) if node_colors else 0
            # ax.set_title(f'{strategy_name}\n({len(partitions)} partitions, '
            #             f'max instances: {max_instances})',
            #             fontsize=14, fontweight='bold')  # 论文图不需要title
            ax.axis('off')
        
        # 隐藏多余的子图
        for idx in range(num_strategies, nrows * ncols):
            row = idx // ncols
            col = idx % ncols
            axes[row, col].axis('off')
        
        fig.suptitle('Partition Strategy Comparison', 
                    fontsize=18, fontweight='bold')
        plt.tight_layout()
        
        if output_file:
            os.makedirs(os.path.dirname(output_file) if os.path.dirname(output_file) else '.', 
                       exist_ok=True)
            plt.savefig(output_file, bbox_inches='tight', dpi=self.dpi)
            pdf_file = os.path.splitext(output_file)[0] + '.pdf'
            plt.savefig(pdf_file, bbox_inches='tight')
            print(f"已保存: {output_file}")
            print(f"已保存: {pdf_file}")
        
        if show:
            plt.show()
        else:
            plt.close()


if __name__ == '__main__':
    print("请运行 examples/ 目录下的示例脚本")


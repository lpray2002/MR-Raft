"""
简单运行脚本 - 直接修改参数运行
无需命令行参数，修改下面的配置即可
"""

import os
# 设置环境变量以消除joblib/sklearn的警告
os.environ['LOKY_MAX_CPU_COUNT'] = '4'

from src.topology.spatial_generator import generate_ppp_topology, SpatialTopology
from src.topology.mec_builder import MECTopologyBuilder
from src.topology.real_data_loader import load_beijing_data
from src.partition.strategies import PartitionStrategies
from src.partition.optimizer import PartitionOptimizer
from src.visualization.plotter import TopologyPlotter, COLORS
from src.analysis.threshold_analyzer import ThresholdAnalyzer
from src.utils import (
    collect_topology_stats,
    generate_topology_features_table,
    generate_partition_comparison_table,
    generate_overlap_sensitivity_table,
)
import networkx as nx

# ============================================
# 配置参数（直接修改这里）
# ============================================

# 拓扑生成参数
AREA_SIZE = 2000           # 区域大小 (m)
COVERAGE_RADIUS = 300      # 覆盖半径 (m)

# 三密度对比配置
DENSITIES = [2e-6, 4e-6, 6e-6]  # 要对比的密度列表
SEEDS = [49, 56, 13]           # 对应的随机种子
# SEEDS = [38, 180, 13]           # 对应的随机种子
# SEEDS = [49, 23, 13]           # 对应的随机种子

# 真实数据配置
USE_REAL_DATA = True           # 是否包含真实基站数据分析
REAL_DATA_CSV = 'data/real_topology/460.csv'
# REAL_DATA_CENTER_LAT = 39.98
# REAL_DATA_CENTER_LON = 116.39
REAL_DATA_CENTER_LAT = 39.98
REAL_DATA_CENTER_LON = 116.39

REAL_DATA_RADIUS_KM = 2.0
# REAL_DATA_N_MEC = 13      # 相应减少MEC节点数
REAL_DATA_N_MEC = 15     # 相应减少MEC节点数
REAL_DATA_COVERAGE_MIN = 300
REAL_DATA_COVERAGE_MAX = 500

# 可视化选项
PLOT_SPATIAL = True        # 绘制空间拓扑图
PLOT_MEC_GRAPH = True      # 绘制MEC拓扑图
PLOT_PARTITION = True      # 绘制分区结果

# 图表尺寸配置（适用于论文）
# 推荐尺寸：
#   - 单栏图 (3.5, 3): 适合IEEE单栏
#   - 单栏图 (4, 3.5): 适合ACM单栏
#   - 双栏图 (7, 5): 适合IEEE双栏
#   - 双栏图 (10, 8): 适合高清展示
# 画布尺寸接近 IEEE 双栏 subfigure(0.30\textwidth) 纸面显示宽(~2.15in)，避免大画布+小显示导致文字过小
FIGURE_SIZE_TOPOLOGY = (6, 5)       # 仿真密度拓扑图和分区图尺寸（原 (10, 7)）
FIGURE_SIZE_REAL_TOPOLOGY = (6, 5)  # 真实拓扑图和分区图尺寸（原 (9, 8)）
FIGURE_SIZE_ANALYSIS = (7, 5)      # 阈值分析曲线尺寸
DPI = 300                           # 图片分辨率（300适合论文）

# 分析选项
PLOT_THRESHOLD_ANALYSIS = True  # 绘制阈值分析曲线（7张图）
THRESHOLD_RANGE = range(2, 16)  # 阈值分析范围
GENERATE_LATEX_TABLES = True    # 生成LaTeX表格

# 综合评分 S(τ) 的归一化方法：'max'（最大值归一化，原始方法）或 'minmax'（min-max 缩放）
NORMALIZATION = 'max'

# α/β 敏感性分析选项（回应审稿人关于超参数 α/β 缺乏敏感性分析的意见）
ANALYZE_ALPHA_SENSITIVITY = True  # 是否进行 α/β 敏感性扫描
ALPHA_STEP = 0.05                 # α 扫描步长（β = 1 - α）
ALPHA_HIGHLIGHT = 0.4             # 论文采用的 α 取值，用于标注其所处稳定区间

# 空间重叠率敏感性分析选项（回应审稿人 1.1：不同 MEC 空间重叠率对
# 一致性区域形成与稳定性的影响）。在固定节点集合上按比例缩放各节点覆盖
# 半径，将覆盖图的重叠率 ρ = 2|E| / (N(N-1)) 从 ~0.1 扫到 ~1.0，
# 每个 ρ 点仅用 Threshold-based 方法（自动求最优阈值 τ*）划分。
# 论文主文仅展示高密度拓扑（审稿人强调 high-density deployments）。
ANALYZE_OVERLAP_SENSITIVITY = True
OVERLAP_DENSITY = 6e-6              # 仅高密度拓扑 λ_bs
OVERLAP_RATIO_TARGETS = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
OVERLAP_METRIC = 'avg_size'        # 折线图 Y 轴指标（CSV 中保存全部指标）
OVERLAP_TABLE_WIDTH = r'\columnwidth'  # LaTeX 表格宽度，同 \includegraphics[width=...]
LATEX_TABLE_WIDTH = OVERLAP_TABLE_WIDTH  # 密度/分区等表格共用
OVERLAP_FIXED_TAU = 2                # 固定阈值；设为 None 则每个 ρ 自动求最优 τ*
OVERLAP_ALPHA = 0.4                 # 综合评分 S(τ)=α·Ī+β·R̄ 的 α（β=1-α）

# 输出选项
SAVE_FILES = True          # 是否保存文件
OUTPUT_DIR = 'output'      # 输出目录

# ============================================
# 主程序
# ============================================

def load_real_topology(
    csv_path=None, center_lat=None, center_lon=None,
    radius_km=None, n_mec_nodes=None,
    min_range=None, max_range=None
):
    """
    加载真实北京基站拓扑（可复用的封装函数）

    使用模块顶部的默认配置，也可通过参数覆盖。
    返回 (spatial, G, mec_nodes) 三元组，图已连通且节点从 0 编号。
    """
    _csv = csv_path or REAL_DATA_CSV
    _lat = center_lat or REAL_DATA_CENTER_LAT
    _lon = center_lon or REAL_DATA_CENTER_LON
    _radius = radius_km or REAL_DATA_RADIUS_KM
    _n_mec = n_mec_nodes or REAL_DATA_N_MEC
    _min_r = min_range or REAL_DATA_COVERAGE_MIN
    _max_r = max_range or REAL_DATA_COVERAGE_MAX

    real_spatial, df_filtered, real_mec_nodes = load_beijing_data(
        csv_path=_csv, center_lat=_lat, center_lon=_lon,
        radius_km=_radius, n_mec_nodes=_n_mec,
        min_range=_min_r, max_range=_max_r
    )

    builder = MECTopologyBuilder()
    G = builder.build_from_mec_nodes(real_mec_nodes)

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

        from src.topology.spatial_generator import BaseStation
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

        filtered_mec_nodes = []
        for mec in real_mec_nodes:
            if mec['id'] in node_mapping:
                new_mec = mec.copy()
                new_mec['id'] = node_mapping[mec['id']]
                filtered_mec_nodes.append(new_mec)
        real_mec_nodes = filtered_mec_nodes

    return real_spatial, G, real_mec_nodes


def process_density(density, seed):
    """
    处理单个密度的拓扑生成和分区
    
    Parameters:
    -----------
    density : float
        基站密度
    seed : int
        随机种子
    
    Returns:
    --------
    tuple : (temp_mec, temp_spatial) - MEC拓扑图和空间拓扑对象
    """
    # 生成拓扑
    temp_mec, temp_spatial = generate_ppp_topology(
        area_size=AREA_SIZE,
        lambda_bs=density,
        coverage_radius_mean=COVERAGE_RADIUS,
        coverage_radius_std=50,
        plot_voronoi=False,
        plot_graph=False,
        random_seed=seed,
        return_spatial=True
    )
    
    # 提取最大连通分量
    if not nx.is_connected(temp_mec):
        temp_builder = MECTopologyBuilder()
        temp_mec = temp_builder.extract_largest_component(temp_mec, verbose=False)
        
        # 同步更新空间拓扑对象
        largest_component_nodes = set(temp_mec.nodes())
        filtered_base_stations = [
            bs for bs in temp_spatial.base_stations 
            if bs.id in largest_component_nodes
        ]
        temp_spatial = SpatialTopology(temp_spatial.area_size, filtered_base_stations)
    
    return temp_mec, temp_spatial


def _summarize_stable_intervals(alpha_values, tau_values):
    """将 τ*(α) 序列归纳为若干 (τ*, α_lo, α_hi) 稳定区间。"""
    intervals = []
    start = 0
    n = len(tau_values)
    for i in range(1, n + 1):
        if i == n or tau_values[i] != tau_values[start]:
            intervals.append((tau_values[start], alpha_values[start], alpha_values[i - 1]))
            start = i
    return intervals


def run_alpha_sensitivity(topologies, threshold_range, alpha_step=0.05,
                          highlight_alpha=0.4, output_dir=None, save_files=True,
                          normalization='minmax'):
    """
    α/β 敏感性扫描。

    对每个拓扑，在 α∈[0,1]（β=1-α）上扫描综合评分
    S(τ)=α·Ī(τ)+β·R̄(τ) 的最优阈值 τ*，用于回应审稿人关于
    超参数 α/β 缺乏敏感性分析的意见。由于 α/β 仅通过 τ* 影响
    最终分区拓扑，本扫描给出 τ*(α) 及其稳定区间，并标注论文取值
    所处的稳定区间。

    Parameters
    ----------
    topologies : dict[str, nx.Graph]
        拓扑标签到 MEC 图的映射。
    threshold_range : iterable[int]
        候选阈值范围。
    alpha_step : float
        α 扫描步长。
    highlight_alpha : float
        论文采用的 α 取值。
    output_dir : str or None
        输出目录（保存 CSV 与图）。
    save_files : bool
        是否保存文件。

    Returns
    -------
    dict : {label: {'alpha': [...], 'tau_star': [...], 'score': [...],
                    'intervals': [(tau, a_lo, a_hi), ...]}}
    """
    import csv as _csv
    import numpy as np

    alpha_values = [round(float(a), 4)
                    for a in np.arange(0.0, 1.0 + 1e-9, alpha_step)]

    results = {}
    print("\n" + "=" * 80)
    print("α/β 敏感性扫描：S(τ)=α·Ī(τ)+β·R̄(τ) 下的最优阈值 τ*")
    print("=" * 80)

    for label, G in topologies.items():
        optimizer = PartitionOptimizer(G)
        taus, scores = [], []
        for alpha in alpha_values:
            beta = 1.0 - alpha
            tau_star, score, _ = optimizer.find_optimal_threshold(
                threshold_range=list(threshold_range),
                alpha=alpha, beta=beta,
                normalization=normalization, verbose=False
            )
            taus.append(tau_star)
            scores.append(score)

        intervals = _summarize_stable_intervals(alpha_values, taus)
        results[label] = {
            'alpha': alpha_values, 'tau_star': taus,
            'score': scores, 'intervals': intervals
        }

        # 定位论文取值所处的稳定区间
        hi_tau = hi_lo = hi_hi = None
        for tau, a_lo, a_hi in intervals:
            if a_lo - 1e-9 <= highlight_alpha <= a_hi + 1e-9:
                hi_tau, hi_lo, hi_hi = tau, a_lo, a_hi
                break

        distinct = sorted(set(taus))
        print(f"\n  拓扑 [{label}]:")
        print(f"    τ* 取值个数: {len(distinct)}, 取值集合: {distinct}")
        print(f"    稳定区间 (τ*, α_lo, α_hi):")
        for tau, a_lo, a_hi in intervals:
            print(f"      τ*={tau}: α∈[{a_lo:.2f}, {a_hi:.2f}]")
        if hi_tau is not None:
            print(f"    论文取值 α={highlight_alpha} (β={1 - highlight_alpha:.2f}) "
                  f"→ τ*={hi_tau}, 落在稳定区间 α∈[{hi_lo:.2f}, {hi_hi:.2f}]")

    if save_files and output_dir is not None:
        os.makedirs(output_dir, exist_ok=True)

        # CSV：完整扫描数据
        csv_path = os.path.join(output_dir, 'alpha_sensitivity.csv')
        with open(csv_path, 'w', newline='', encoding='utf-8') as f:
            writer = _csv.writer(f)
            writer.writerow(['topology', 'alpha', 'beta', 'tau_star', 'score'])
            for label, data in results.items():
                for a, t, s in zip(data['alpha'], data['tau_star'], data['score']):
                    writer.writerow([label, f'{a:.2f}', f'{1 - a:.2f}', t, f'{s:.6f}'])
        print(f"\n  已保存: {csv_path}")

        # 图：τ*(α) 阶梯曲线
        try:
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt

            fig, ax = plt.subplots(figsize=(7, 5), dpi=300)
            markers = ['o', 's', '^', 'd']
            for label, data in results.items():
                marker = markers[len(ax.lines) % len(markers)]
                ax.step(data['alpha'], data['tau_star'], where='post',
                        marker=marker, linewidth=2, markersize=3,
                        label=str(label))
            ax.axvline(highlight_alpha, color='gray', linestyle='--',
                       linewidth=2.5, label=f'alpha={highlight_alpha}')
            ax.set_xlabel(r'$\alpha$  ($\beta = 1-\alpha$)',
                          fontsize=20, fontweight='bold')
            ax.set_ylabel(r'Optimal threshold $\tau^*$',
                          fontsize=20, fontweight='bold')
            ax.grid(True, alpha=0.3)
            ax.tick_params(axis='both', labelsize=18)
            for tick in ax.get_xticklabels() + ax.get_yticklabels():
                tick.set_fontweight('bold')
            ax.legend(fontsize=12)
            fig.tight_layout()
            fig_path = os.path.join(output_dir, 'alpha_sensitivity_tau.png')
            fig.savefig(fig_path, bbox_inches='tight')
            pdf_path = os.path.join(output_dir, 'alpha_sensitivity_tau.pdf')
            fig.savefig(pdf_path, bbox_inches='tight')
            plt.close(fig)
            print(f"  已保存: {fig_path}")
            print(f"  已保存: {pdf_path}")
        except Exception as e:
            print(f"  [警告] 绘图失败: {e}")

    return results


# ---------------------------------------------------------------------------
# 空间重叠率敏感性分析（回应审稿人 1.1）
# ---------------------------------------------------------------------------

def _overlap_ratio(G):
    """覆盖图重叠率 ρ = 2|E| / (N(N-1))（即无向图边密度）。"""
    n = G.number_of_nodes()
    if n < 2:
        return 0.0
    return 2.0 * G.number_of_edges() / (n * (n - 1))


def _scale_synthetic_graph(base_graph, scale):
    """在固定节点集合上，将各节点覆盖半径按 scale 缩放后重建边。

    合成拓扑节点带有 'pos'(x, y) 与 'coverage'(r) 属性，边判定条件为
    dist(i, j) <= scale * (r_i + r_j)，节点集合与位置保持不变。
    """
    import itertools
    import math

    G = nx.Graph()
    for n, d in base_graph.nodes(data=True):
        G.add_node(n, **d)
    nodes = list(base_graph.nodes(data=True))
    for (i, di), (j, dj) in itertools.combinations(nodes, 2):
        xi, yi = di['pos']
        xj, yj = dj['pos']
        ri = di['coverage'] * scale
        rj = dj['coverage'] * scale
        if math.hypot(xi - xj, yi - yj) <= ri + rj:
            G.add_edge(i, j)
    return G


def _scale_real_graph(real_mec_nodes, scale, builder):
    """将真实 MEC 各基站覆盖半径按 scale 缩放后重建融合覆盖图。

    真实拓扑的边由多基站融合覆盖区域的几何重叠决定，因此按比例缩放每个
    基站的 coverage_radius 后调用 build_from_mec_nodes 重建，节点集合不变。
    """
    import copy

    scaled = copy.deepcopy(real_mec_nodes)
    for mec in scaled:
        for bs in mec.get('base_stations', []):
            if 'coverage_radius' in bs:
                bs['coverage_radius'] = bs['coverage_radius'] * scale
    return builder.build_from_mec_nodes(scaled)


def _find_scale_for_ratio(rebuild_fn, target_rho, lo=0.05, hi=20.0, iters=40):
    """二分搜索缩放系数 scale，使覆盖图重叠率尽量接近 target_rho。

    ρ(scale) 随 scale 单调不减，采用二分逼近。返回 (scale, graph, rho)。
    """
    best = None
    for _ in range(iters):
        mid = (lo + hi) / 2.0
        G = rebuild_fn(mid)
        rho = _overlap_ratio(G)
        if best is None or abs(rho - target_rho) < abs(best[2] - target_rho):
            best = (mid, G, rho)
        if rho < target_rho:
            lo = mid
        else:
            hi = mid
    return best


def run_overlap_sensitivity(topologies, ratio_targets, metric='avg_size',
                            alpha=0.4, beta=0.6, normalization='max',
                            fixed_tau=None,
                            figsize=(7, 5), dpi=300,
                            output_dir=None, save_files=True,
                            single_column_table=True,
                            table_width=r'\columnwidth'):
    """空间重叠率敏感性扫描（回应审稿人 1.1）。

    在固定节点集合上按比例缩放覆盖半径，将覆盖图重叠率
    ρ = 2|E| / (N(N-1)) 从约 0.1 扫到约 1.0。每个 ρ 点使用
    Threshold-based 划分；fixed_tau 为 None 时自动求最优 τ*，
    否则全程使用固定阈值 τ。

    Parameters
    ----------
    topologies : list[tuple]
        [(label, rebuild_fn), ...]，rebuild_fn(scale) -> nx.Graph。
    ratio_targets : iterable[float]
        目标重叠率序列（如 0.1..1.0）。
    metric : str
        折线图 Y 轴指标（'avg_size' / 'avg_instances' / 'max_size' /
        'max_instances' / 'num_partitions'），CSV 保存全部指标。
    """
    import csv as _csv

    metric_labels = {
        'avg_size': 'Average Region Size',
        'max_size': 'Max Region Size',
        'avg_instances': 'Average Instances per Node',
        'max_instances': 'Max Instances per Node',
        'num_partitions': 'Number of Regions',
    }

    # 与阈值分析图统一的颜色/标记：λ=2e-6→COLORS[1]/o，4e-6→COLORS[6]/s，
    # 6e-6→COLORS[8]/^，Real→COLORS[3]/d。
    style_map = {
        '2e-06': (COLORS[1], 'o'), '2e-6': (COLORS[1], 'o'),
        '4e-06': (COLORS[6], 's'), '4e-6': (COLORS[6], 's'),
        '6e-06': (COLORS[8], '^'), '6e-6': (COLORS[8], '^'),
        'Real': (COLORS[3], 'd'),
    }
    fallback_styles = [(COLORS[1], 'o'), (COLORS[6], 's'),
                       (COLORS[8], '^'), (COLORS[3], 'd')]

    tau_mode = f"固定 τ={fixed_tau}" if fixed_tau is not None else "最优 τ*"
    print("\n" + "=" * 80)
    if len(topologies) == 1:
        print(f"空间重叠率敏感性扫描 [{topologies[0][0]}]："
              f"ρ 从 0.1→1.0，Threshold-based({tau_mode}) 划分")
    else:
        print(f"空间重叠率敏感性扫描：ρ 从 0.1→1.0，Threshold-based({tau_mode}) 划分")
    print("=" * 80)

    results = {}
    for label, rebuild_fn in topologies:
        rows = []
        print(f"\n  拓扑 [{label}]:")
        for target in ratio_targets:
            scale, G, rho = _find_scale_for_ratio(rebuild_fn, target)
            strategies = PartitionStrategies(G)
            if fixed_tau is not None:
                tau_used = fixed_tau
                partitions = strategies.threshold_based(merge_threshold=fixed_tau)
            else:
                n_nodes = G.number_of_nodes()
                threshold_range = list(range(2, max(3, n_nodes + 1)))
                optimizer = PartitionOptimizer(G)
                tau_used, _score, partitions = optimizer.find_optimal_threshold(
                    threshold_range=threshold_range,
                    alpha=alpha, beta=beta,
                    normalization=normalization, verbose=False
                )
            stats = strategies.get_partition_statistics(partitions)
            rows.append({
                'target_rho': target,
                'rho': rho,
                'scale': scale,
                'tau_star': tau_used,
                'num_partitions': stats['num_partitions'],
                'avg_size': stats['avg_size'],
                'max_size': stats['max_size'],
                'avg_instances': stats['avg_instances'],
                'max_instances': stats['max_instances'],
            })
            tau_label = f"τ={tau_used}" if fixed_tau is not None else f"τ*={tau_used}"
            print(f"    ρ≈{target:.1f} (实际 {rho:.3f}, scale={scale:.2f}) "
                  f"→ {tau_label}, 区域数={stats['num_partitions']}, "
                  f"平均区域大小={stats['avg_size']:.2f}, "
                  f"平均实例数={stats['avg_instances']:.2f}")
        results[label] = rows

    if save_files and output_dir is not None:
        os.makedirs(output_dir, exist_ok=True)

        # CSV：完整扫描数据
        csv_path = os.path.join(output_dir, 'overlap_sensitivity.csv')
        with open(csv_path, 'w', newline='', encoding='utf-8') as f:
            writer = _csv.writer(f)
            writer.writerow(['topology', 'rho', 'target_rho', 'scale',
                             'tau_star', 'num_partitions', 'avg_size',
                             'max_size', 'avg_instances', 'max_instances'])
            for label, rows in results.items():
                for r in rows:
                    writer.writerow([
                        label, f"{r['rho']:.2f}", f"{r['target_rho']:.2f}",
                        f"{r['scale']:.4f}", r['tau_star'], r['num_partitions'],
                        f"{r['avg_size']:.4f}", f"{r['max_size']:.4f}",
                        f"{r['avg_instances']:.4f}", f"{r['max_instances']:.4f}",
                    ])
        print(f"\n  已保存: {csv_path}")

        # LaTeX 表格
        table_tex = generate_overlap_sensitivity_table(
            results,
            single_column=single_column_table,
            table_width=table_width,
            fixed_tau=fixed_tau,
            alpha=alpha,
        )
        tex_path = os.path.join(output_dir, 'overlap_sensitivity.tex')
        with open(tex_path, 'w', encoding='utf-8') as f:
            f.write(table_tex)
        print(f"  已保存: {tex_path}")

        # 折线图：各指标 vs 重叠率 ρ，每个指标单独一张图（风格对齐阈值分析图）
        try:
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt

            # 导出可编辑的 PDF/SVG 文字对象
            plt.rcParams['pdf.fonttype'] = 42
            plt.rcParams['svg.fonttype'] = 'none'
            plt.rcParams['ps.fonttype'] = 42

            def _plot_metric(metric_key, out_stem):
                fig, ax = plt.subplots(figsize=figsize, dpi=dpi)
                for i, (label, rows) in enumerate(results.items()):
                    xs = [r['rho'] for r in rows]
                    ys = [r[metric_key] for r in rows]
                    color, marker = style_map.get(
                        label, fallback_styles[i % len(fallback_styles)])
                    if label == 'Real':
                        legend_label = 'Real'
                    else:
                        try:
                            legend_label = f'λ={float(label):.0e}'.replace('-0', '-')
                        except (TypeError, ValueError):
                            legend_label = str(label)
                    ax.plot(xs, ys, marker=marker, color=color,
                            linewidth=3.5, markersize=10, label=legend_label)

                ax.set_xlabel(r'Overlap Ratio $\rho$',
                              fontsize=20, fontweight='bold')
                ax.set_ylabel(metric_labels.get(metric_key, metric_key),
                              fontsize=20, fontweight='bold')
                if len(results) > 1:
                    ax.legend(fontsize=16, loc='best')
                ax.grid(True, alpha=0.7, linestyle='--', linewidth=0.8)
                ax.tick_params(axis='both', labelsize=18)
                fig.tight_layout()

                png_path = os.path.join(output_dir, f'{out_stem}.png')
                pdf_path = os.path.join(output_dir, f'{out_stem}.pdf')
                fig.savefig(png_path, dpi=dpi, bbox_inches='tight')
                fig.savefig(pdf_path, bbox_inches='tight')
                plt.close(fig)
                print(f"  已保存: {png_path}")
                print(f"  已保存: {pdf_path}")

            # 每个指标单独出图
            for m in ['avg_size', 'avg_instances', 'max_instances', 'max_size']:
                _plot_metric(m, f'overlap_sensitivity_{m}')
            # 兼容旧文件名：主指标另存一份 overlap_sensitivity.{png,pdf}
            _plot_metric(metric, 'overlap_sensitivity')
        except Exception as e:
            print(f"  [警告] 绘图失败: {e}")

    return results


def main():
    print("=" * 80)
    if USE_REAL_DATA:
        print("MEC区域划分 - 三密度 + 真实数据对比分析")
    else:
        print("MEC区域划分 - 三密度对比分析")
    print("=" * 80)
    print(f"\n配置:")
    print(f"  区域大小: {AREA_SIZE}m × {AREA_SIZE}m")
    print(f"  覆盖半径: {COVERAGE_RADIUS}m")
    print(f"  密度列表: {[f'{d:.0e}' for d in DENSITIES]}")
    print(f"  随机种子: {SEEDS}")
    if USE_REAL_DATA:
        print(f"  真实数据: 北京基站 (中心: {REAL_DATA_CENTER_LAT}, {REAL_DATA_CENTER_LON}, 半径: {REAL_DATA_RADIUS_KM}km)")
    
    # 三密度对比分析（完整流程）
    print(f"\n[1/{'4' if USE_REAL_DATA else '3'}] 三密度对比分析...")
    densities = DENSITIES
    seeds = SEEDS
    
    # 用于收集LaTeX表格数据
    latex_data = {}
    
    for idx, (density, seed) in enumerate(zip(densities, seeds)):
        print(f"\n  [{idx+1}/3] 密度 {density:.0e} (seed={seed})...")
        
        # 4.1 生成拓扑（使用辅助函数）
        temp_mec, temp_spatial = process_density(density, seed)
        
        print(f"    生成 {temp_mec.number_of_nodes()} 个节点")
        print(f"    边数: {temp_mec.number_of_edges()}")
        
        # 4.2 应用分区策略
        temp_strategies = PartitionStrategies(temp_mec)
        temp_optimizer = PartitionOptimizer(temp_mec)
        
        # 极大团
        temp_clique = temp_strategies.maximal_clique()
        
        # 最优阈值分区（使用与阈值分析相同的范围）
        temp_threshold, _, temp_optimal = temp_optimizer.find_optimal_threshold(
            threshold_range=list(THRESHOLD_RANGE),
            normalization=NORMALIZATION,
            verbose=False
        )
        print(f"    极大团: {len(temp_clique)} 个分区")
        print(f"    最优阈值: τ={temp_threshold}, 分区数: {len(temp_optimal)}")
        
        # 其他策略
        temp_neighbor = temp_strategies.neighbor_based()
        temp_global = temp_strategies.global_partition()
        temp_greedy = temp_strategies.greedy_node_centric(temp_threshold)
        temp_sa = temp_strategies.simulated_annealing(temp_threshold)
        
        temp_partition_dict = {
            'Maximal Clique': temp_clique,
            f'Threshold (τ={temp_threshold})': temp_optimal,
            'Neighbor-based': temp_neighbor,
            f'Greedy-NC (τ={temp_threshold})': temp_greedy,
            f'SA (τ={temp_threshold})': temp_sa,
            'Global': temp_global
        }
        
        # 4.3 可视化（论文展示尺寸）
        temp_plotter = TopologyPlotter(figsize=FIGURE_SIZE_TOPOLOGY)
        density_str = f'{density:.0e}'.replace('-0', '-').replace('+0', '')
        
        # 1) 绘制空间拓扑图
        if PLOT_SPATIAL:
            print(f"    绘制空间拓扑图...")
            if SAVE_FILES:
                spatial_output = f'{OUTPUT_DIR}/density_comparison/density_{density_str}_spatial.png'
                os.makedirs(os.path.dirname(spatial_output), exist_ok=True)
            else:
                spatial_output = None
            if density == 2e-6:
                node_color = COLORS[1]  # 蓝色
            elif density == 4e-6:
                node_color = COLORS[6]  # 浅红
            else:  # 6e-6
                node_color = COLORS[8]  # 深红
            temp_plotter.plot_spatial_topology(
                temp_spatial,
                mec_graph=temp_mec,
                density=density,
                output_file=spatial_output,
                node_color=node_color,
                show=True
            )
        
        # 2) 绘制MEC拓扑图
        if PLOT_MEC_GRAPH:
            print(f"    绘制MEC拓扑图...")
            if SAVE_FILES:
                mec_output = f'{OUTPUT_DIR}/density_comparison/density_{density_str}_mec_graph.png'
            else:
                mec_output = None
            
            # 根据密度选择节点颜色（与plot_three_densities.py保持一致）
            if density == 2e-6:
                node_color = COLORS[1]  # 蓝色
            elif density == 4e-6:
                node_color = COLORS[6]  # 浅红
            else:  # 6e-6
                node_color = COLORS[8]  # 深红
            
            temp_plotter.plot_mec_graph(
                temp_mec,
                spatial_topology=temp_spatial,
                density=density,
                layout='kamada_kawai',
                node_color=node_color,
                output_file=mec_output,
                show=True,
                show_labels=False  # 仿真拓扑图不显示节点编号
            )
        
        # 3) 绘制每个策略的分区结果图（单独绘制）
        if PLOT_PARTITION:
            print(f"    绘制各策略分区结果...")
            
            # 策略名称映射（用于文件名）
            strategy_file_names = {
                'Maximal Clique': 'maximal_clique',
                f'Threshold (τ={temp_threshold})': f'threshold_tau{temp_threshold}',
                'Neighbor-based': 'neighbor_based',
                f'Greedy (τ={temp_threshold})': f'greedy_tau{temp_threshold}',
                'Global': 'global'
            }
            
            for strategy_name, partitions in temp_partition_dict.items():
                # 获取文件名友好的策略名
                file_strategy_name = strategy_file_names.get(strategy_name, 
                                                             strategy_name.lower().replace(' ', '_').replace('(', '').replace(')', ''))
                
                if SAVE_FILES:
                    partition_output = f'{OUTPUT_DIR}/density_comparison/density_{density_str}_{file_strategy_name}.png'
                else:
                    partition_output = None
                
                temp_plotter.plot_partitions(
                    temp_mec,
                    partitions,
                    spatial_topology=temp_spatial,
                    density=density,
                    layout='kamada_kawai',
                    title=f'{strategy_name}',
                    output_file=partition_output,
                    show=True
                )
        
        # 4) 打印详细统计信息
        print(f"    统计信息:")
        print(f"    {'-' * 60}")
        for strategy_name, partitions in temp_partition_dict.items():
            stats = temp_strategies.get_partition_statistics(partitions)
            print(f"    {strategy_name}:")
            print(f"      分区数: {stats['num_partitions']}, "
                  f"平均大小: {stats['avg_size']:.2f}, "
                  f"最大大小: {stats['max_size']}")
            print(f"      平均实例数: {stats['avg_instances']:.2f}, "
                  f"最大实例数: {stats['max_instances']}")
        print(f"    {'-' * 60}")
        
        # 5) 收集LaTeX表格数据
        if GENERATE_LATEX_TABLES:
            # 收集拓扑统计
            topology_stats = collect_topology_stats(temp_mec)
            
            # 收集分区统计
            partition_stats = {}
            partition_stats['Global'] = temp_strategies.get_partition_statistics(temp_global)
            partition_stats['Neighbor-based'] = temp_strategies.get_partition_statistics(temp_neighbor)
            partition_stats['Maximal Clique'] = temp_strategies.get_partition_statistics(temp_clique)
            partition_stats[f'Greedy ($\\tau={temp_threshold}$)'] = temp_strategies.get_partition_statistics(temp_greedy)
            partition_stats[f'Simulated Annealing ($\\tau={temp_threshold}$)'] = temp_strategies.get_partition_statistics(temp_sa)
            partition_stats[f'Threshold-based ($\\tau={temp_threshold}^*$)'] = temp_strategies.get_partition_statistics(temp_optimal)
            
            latex_data[density] = {
                'topology': topology_stats,
                'partitions': partition_stats
            }
    
    # 1.5 真实数据分析
    real_mec_graph = None
    real_spatial = None
    real_mec_nodes = None
    
    if USE_REAL_DATA:
        print(f"\n[2/{'4' if USE_REAL_DATA else '3'}] 真实基站数据分析...")
        
        # 加载真实数据
        print(f"  加载真实基站数据...")
        real_spatial, df_filtered, real_mec_nodes = load_beijing_data(
            csv_path=REAL_DATA_CSV,
            center_lat=REAL_DATA_CENTER_LAT,
            center_lon=REAL_DATA_CENTER_LON,
            radius_km=REAL_DATA_RADIUS_KM,
            n_mec_nodes=REAL_DATA_N_MEC,
            min_range=REAL_DATA_COVERAGE_MIN,
            max_range=REAL_DATA_COVERAGE_MAX
        )
        
        # 构建MEC拓扑图
        builder_real = MECTopologyBuilder()
        real_mec_graph = builder_real.build_from_mec_nodes(real_mec_nodes)
        
        print(f"  加载了 {len(real_spatial.base_stations)} 个MEC节点")
        print(f"  边数: {real_mec_graph.number_of_edges()}")
        
        # 提取最大连通分量
        if not nx.is_connected(real_mec_graph):
            print(f"  提取最大连通分量...")
            # 获取最大连通分量的节点
            largest_cc = max(nx.connected_components(real_mec_graph), key=len)
            old_nodes = sorted(largest_cc)  # 保持顺序一致
            
            # 创建节点映射：旧ID -> 新ID（从0开始连续编号）
            node_mapping = {old_id: new_id for new_id, old_id in enumerate(old_nodes)}
            
            # 重新构建图并重新编号节点
            new_graph = nx.Graph()
            for old_id, new_id in node_mapping.items():
                # 复制节点属性
                node_attrs = real_mec_graph.nodes[old_id]
                new_graph.add_node(new_id, **node_attrs)
            
            # 添加边（使用新编号）
            for u, v in real_mec_graph.edges():
                if u in node_mapping and v in node_mapping:
                    new_u = node_mapping[u]
                    new_v = node_mapping[v]
                    edge_attrs = real_mec_graph[u][v]
                    new_graph.add_edge(new_u, new_v, **edge_attrs)
            
            real_mec_graph = new_graph
            
            # 更新MEC节点列表（过滤并重新编号）
            filtered_mec_nodes = []
            for mec in real_mec_nodes:
                if mec['id'] in node_mapping:
                    new_mec = mec.copy()
                    new_mec['id'] = node_mapping[mec['id']]
                    filtered_mec_nodes.append(new_mec)
            real_mec_nodes = filtered_mec_nodes
            
            # 更新空间拓扑（过滤并重新编号基站）
            filtered_base_stations = []
            for bs in real_spatial.base_stations:
                if bs.id in node_mapping:
                    # 创建新的BaseStation对象，更新ID
                    from src.topology.spatial_generator import BaseStation
                    new_bs = BaseStation(
                        station_id=node_mapping[bs.id],
                        x=bs.x,
                        y=bs.y,
                        coverage_radius=bs.coverage_radius
                    )
                    filtered_base_stations.append(new_bs)
            real_spatial = SpatialTopology(real_spatial.area_size, filtered_base_stations)
            
            print(f"  最大连通分量: {real_mec_graph.number_of_nodes()} 个MEC节点（重新编号为0-{real_mec_graph.number_of_nodes()-1}）")
        
        # 应用分区策略
        print(f"  应用分区策略...")
        real_strategies = PartitionStrategies(real_mec_graph)
        real_optimizer = PartitionOptimizer(real_mec_graph)
        
        real_clique = real_strategies.maximal_clique()
        real_threshold, _, real_optimal = real_optimizer.find_optimal_threshold(
            threshold_range=list(THRESHOLD_RANGE),
            normalization=NORMALIZATION,
            verbose=False
        )
        real_neighbor = real_strategies.neighbor_based()
        real_global = real_strategies.global_partition()
        real_greedy = real_strategies.greedy_node_centric(real_threshold)
        real_sa = real_strategies.simulated_annealing(real_threshold)
        
        real_partition_dict = {
            'Maximal Clique': real_clique,
            f'Threshold (τ={real_threshold})': real_optimal,
            'Neighbor-based': real_neighbor,
            f'Greedy-NC (τ={real_threshold})': real_greedy,
            f'SA (τ={real_threshold})': real_sa,
            'Global': real_global
        }
        
        print(f"  极大团: {len(real_clique)} 个分区")
        print(f"  最优阈值: τ={real_threshold}, 分区数: {len(real_optimal)}")
        
        # 可视化
        if PLOT_SPATIAL or PLOT_MEC_GRAPH or PLOT_PARTITION:
            print(f"  绘制拓扑图...")
            temp_plotter = TopologyPlotter(figsize=FIGURE_SIZE_REAL_TOPOLOGY)
            
            # 空间拓扑图
            if PLOT_SPATIAL and SAVE_FILES:
                spatial_output = f'{OUTPUT_DIR}/real_data/spatial_topology_map.png'
                os.makedirs(os.path.dirname(spatial_output), exist_ok=True)
                temp_plotter.plot_real_spatial_topology(
                    real_spatial,
                    mec_nodes_info=real_mec_nodes,
                    mec_graph=real_mec_graph,
                    mec_color=COLORS[3],
                    output_file=spatial_output,
                    show=True
                )
            
            # MEC拓扑图
            if PLOT_MEC_GRAPH and SAVE_FILES:
                mec_output = f'{OUTPUT_DIR}/real_data/mec_graph.png'
                temp_plotter.plot_mec_graph(
                    real_mec_graph,
                    spatial_topology=real_spatial,
                    density=None,
                    layout='kamada_kawai',
                    node_color=COLORS[3],
                    node_size=400,
                    output_file=mec_output,
                    show=True
                )
            
            # 分区结果
            if PLOT_PARTITION and SAVE_FILES:
                strategy_file_names = {
                    'Maximal Clique': 'maximal_clique',
                    f'Threshold (τ={real_threshold})': f'threshold_tau{real_threshold}',
                    'Neighbor-based': 'neighbor_based',
                    f'Greedy (τ={real_threshold})': f'greedy_tau{real_threshold}',
                    'Global': 'global'
                }
                
                for strategy_name, partitions in real_partition_dict.items():
                    file_strategy_name = strategy_file_names.get(strategy_name, 
                                                                 strategy_name.lower().replace(' ', '_').replace('(', '').replace(')', ''))
                    partition_output = f'{OUTPUT_DIR}/real_data/{file_strategy_name}.png'
                    
                    temp_plotter.plot_partitions(
                        real_mec_graph,
                        partitions,
                        spatial_topology=real_spatial,
                        density=None,
                        layout='kamada_kawai',
                        title=f'{strategy_name}',
                        output_file=partition_output,
                        show=True,
                        label_fontsize=16  # 真实分区图标签更小，缓解密集区拥挤
                    )
        
        # 统计信息
        print(f"  统计信息:")
        print(f"  {'-' * 60}")
        for strategy_name, partitions in real_partition_dict.items():
            stats = real_strategies.get_partition_statistics(partitions)
            print(f"  {strategy_name}:")
            print(f"    分区数: {stats['num_partitions']}, "
                  f"平均大小: {stats['avg_size']:.2f}, "
                  f"最大大小: {stats['max_size']}")
            print(f"    平均实例数: {stats['avg_instances']:.2f}, "
                  f"最大实例数: {stats['max_instances']}")
        print(f"  {'-' * 60}")
        
        # 收集LaTeX表格数据
        if GENERATE_LATEX_TABLES:
            topology_stats = collect_topology_stats(real_mec_graph)
            partition_stats = {}
            partition_stats['Global'] = real_strategies.get_partition_statistics(real_global)
            partition_stats['Neighbor-based'] = real_strategies.get_partition_statistics(real_neighbor)
            partition_stats['Maximal Clique'] = real_strategies.get_partition_statistics(real_clique)
            partition_stats[f'Greedy ($\\tau={real_threshold}$)'] = real_strategies.get_partition_statistics(real_greedy)
            partition_stats[f'Simulated Annealing ($\\tau={real_threshold}$)'] = real_strategies.get_partition_statistics(real_sa)
            partition_stats[f'Threshold-based ($\\tau={real_threshold}^*$)'] = real_strategies.get_partition_statistics(real_optimal)
            
            latex_data['Real'] = {
                'topology': topology_stats,
                'partitions': partition_stats
            }
    
    # 2. 阈值分析曲线
    if PLOT_THRESHOLD_ANALYSIS:
        step_num = 3 if USE_REAL_DATA else 2
        print(f"\n[{step_num}/{'4' if USE_REAL_DATA else '3'}] 阈值分析曲线...")
        
        # 准备密度列表（包含真实数据）
        analysis_densities = densities.copy()
        if USE_REAL_DATA:
            analysis_densities.append('Real')
        
        # 收集数据
        threshold_data = {d: {
            'thresholds': [],
            'avg_size': [],
            'max_size': [],
            'avg_instances': [],
            'max_instances': [],
            'num_partitions': []
        } for d in analysis_densities}
        
        for density, seed in zip(densities, seeds):
            print(f"\n  分析密度 {density:.0e}...")
            
            # 生成拓扑
            G, _ = process_density(density, seed)
            
            # 对每个阈值计算统计数据
            strategies_temp = PartitionStrategies(G)
            for threshold in THRESHOLD_RANGE:
                partitions = strategies_temp.threshold_based(merge_threshold=threshold)
                stats = strategies_temp.get_partition_statistics(partitions)
                
                threshold_data[density]['thresholds'].append(threshold)
                threshold_data[density]['avg_size'].append(stats['avg_size'])
                threshold_data[density]['max_size'].append(stats['max_size'])
                threshold_data[density]['avg_instances'].append(stats['avg_instances'])
                threshold_data[density]['max_instances'].append(stats['max_instances'])
                threshold_data[density]['num_partitions'].append(stats['num_partitions'])
        
        # 真实数据的阈值分析
        if USE_REAL_DATA and real_mec_graph is not None:
            print(f"\n  分析真实数据...")
            
            real_strategies_temp = PartitionStrategies(real_mec_graph)
            for threshold in THRESHOLD_RANGE:
                partitions = real_strategies_temp.threshold_based(merge_threshold=threshold)
                stats = real_strategies_temp.get_partition_statistics(partitions)
                
                threshold_data['Real']['thresholds'].append(threshold)
                threshold_data['Real']['avg_size'].append(stats['avg_size'])
                threshold_data['Real']['max_size'].append(stats['max_size'])
                threshold_data['Real']['avg_instances'].append(stats['avg_instances'])
                threshold_data['Real']['max_instances'].append(stats['max_instances'])
                threshold_data['Real']['num_partitions'].append(stats['num_partitions'])
        
        # 绘制曲线
        print("\n  绘制阈值分析曲线...")
        analyzer = ThresholdAnalyzer(
            densities=analysis_densities,
            figsize=FIGURE_SIZE_ANALYSIS,
            dpi=DPI
        )
        output_dir = f'{OUTPUT_DIR}/threshold_analysis'
        analyzer.plot_all_metrics(threshold_data, output_dir)
        
        # 获取最优阈值用于LaTeX代码生成
        optimal_threshold_dict = analyzer.find_optimal_thresholds(threshold_data)
    
    # 2.5 α/β 敏感性扫描
    if ANALYZE_ALPHA_SENSITIVITY:
        print(f"\n[α/β 敏感性扫描]")
        alpha_topologies = {}
        for density, seed in zip(densities, seeds):
            G_alpha, _ = process_density(density, seed)
            alpha_topologies[f'{density:.0e}'] = G_alpha
        if USE_REAL_DATA and real_mec_graph is not None:
            alpha_topologies['Real'] = real_mec_graph
        run_alpha_sensitivity(
            alpha_topologies,
            threshold_range=THRESHOLD_RANGE,
            alpha_step=ALPHA_STEP,
            highlight_alpha=ALPHA_HIGHLIGHT,
            output_dir=f'{OUTPUT_DIR}/alpha_sensitivity',
            save_files=SAVE_FILES,
            normalization=NORMALIZATION
        )

    # 2.6 空间重叠率敏感性扫描（回应审稿人 1.1，仅高密度拓扑）
    overlap_results = None
    if ANALYZE_OVERLAP_SENSITIVITY:
        print(f"\n[空间重叠率敏感性扫描 - 高密度拓扑 λ={OVERLAP_DENSITY:.0e}]")
        try:
            overlap_idx = DENSITIES.index(OVERLAP_DENSITY)
            overlap_seed = SEEDS[overlap_idx]
        except ValueError:
            overlap_idx = len(DENSITIES) - 1
            overlap_seed = SEEDS[overlap_idx]
            print(f"  [警告] OVERLAP_DENSITY 不在 DENSITIES 中，"
                  f"改用 {DENSITIES[overlap_idx]:.0e}")
        G_base, _ = process_density(DENSITIES[overlap_idx], overlap_seed)
        overlap_topologies = [
            (f'{DENSITIES[overlap_idx]:.0e}',
             (lambda g: (lambda s: _scale_synthetic_graph(g, s)))(G_base))
        ]
        overlap_results = run_overlap_sensitivity(
            overlap_topologies,
            ratio_targets=OVERLAP_RATIO_TARGETS,
            metric=OVERLAP_METRIC,
            normalization=NORMALIZATION,
            figsize=FIGURE_SIZE_ANALYSIS,
            dpi=DPI,
            output_dir=f'{OUTPUT_DIR}/overlap_sensitivity',
            save_files=SAVE_FILES,
            table_width=OVERLAP_TABLE_WIDTH,
            fixed_tau=OVERLAP_FIXED_TAU,
            alpha=OVERLAP_ALPHA,
        )
    
    # 3. 生成LaTeX表格
    if GENERATE_LATEX_TABLES and latex_data:
        step_num = 4 if USE_REAL_DATA else 3
        print(f"\n[{step_num}/{'4' if USE_REAL_DATA else '3'}] 生成LaTeX表格...")
        
        # 准备密度列表（包含真实数据）
        table_densities = densities.copy()
        if USE_REAL_DATA and 'Real' in latex_data:
            table_densities.append('Real')
        
        # 生成表格1：拓扑特征统计
        table1 = generate_topology_features_table(
            latex_data, table_densities,
            single_column=True, table_width=LATEX_TABLE_WIDTH,
        )

        # 生成表格2：分区策略对比（双栏 table*）
        table2 = generate_partition_comparison_table(
            latex_data, table_densities,
            single_column=False, table_width=r'\textwidth',
        )

        # 生成表格3：空间重叠率敏感性（若已运行）
        table3 = None
        if overlap_results:
            table3 = generate_overlap_sensitivity_table(
                overlap_results,
                single_column=True,
                table_width=OVERLAP_TABLE_WIDTH,
                fixed_tau=OVERLAP_FIXED_TAU,
                alpha=OVERLAP_ALPHA,
            )
        
        # 保存到文件
        if SAVE_FILES:
            latex_output_dir = f'{OUTPUT_DIR}/latex_tables'
            os.makedirs(latex_output_dir, exist_ok=True)
            
            # 保存拓扑特征表格
            with open(f'{latex_output_dir}/topology_features.tex', 'w', encoding='utf-8') as f:
                f.write(table1)
            print(f"  已保存: topology_features.tex")
            
            # 保存分区对比表格
            with open(f'{latex_output_dir}/partition_comparison.tex', 'w', encoding='utf-8') as f:
                f.write(table2)
            print(f"  已保存: partition_comparison.tex")

            if table3:
                with open(f'{latex_output_dir}/overlap_sensitivity.tex', 'w', encoding='utf-8') as f:
                    f.write(table3)
                print(f"  已保存: overlap_sensitivity.tex")
            
            # 保存合并版本
            with open(f'{latex_output_dir}/all_tables.tex', 'w', encoding='utf-8') as f:
                f.write(table1)
                f.write('\n\n')
                f.write(table2)
                if table3:
                    f.write('\n\n')
                    f.write(table3)
            print(f"  已保存: all_tables.tex")
            
            print(f"  LaTeX表格已保存到: {latex_output_dir}/")
    
    # ========================================
    # 8. 生成图片的LaTeX代码
    # ========================================
    if SAVE_FILES:
        print("\n" + "=" * 80)
        print("Step 8: 生成图片的LaTeX代码")
        print("=" * 80)
        
        from src.utils import generate_all_figures_latex
        
        # 构建最优阈值字典
        optimal_thresholds = {}
        if PLOT_THRESHOLD_ANALYSIS and 'optimal_threshold_dict' in locals():
            # 使用阈值分析得到的最优阈值
            optimal_thresholds = optimal_threshold_dict.copy()
        else:
            # 使用默认阈值或从单独运行中获取
            for density in densities:
                optimal_thresholds[density] = 4  # 默认阈值
        
        latex_code = generate_all_figures_latex(
            densities=densities,
            seeds=SEEDS,
            threshold_strategies=optimal_thresholds,
            output_dir_base=OUTPUT_DIR
        )
        
        # 保存LaTeX代码
        latex_output_dir = os.path.join(OUTPUT_DIR, 'latex_figures')
        os.makedirs(latex_output_dir, exist_ok=True)
        latex_file = os.path.join(latex_output_dir, 'figures.tex')
        
        with open(latex_file, 'w', encoding='utf-8') as f:
            f.write(latex_code)
        
        print(f"LaTeX图片代码已保存至: {latex_file}")
    
    print("\n" + "=" * 80)
    print("完成！")
    if SAVE_FILES:
        print(f"\n输出文件:")
        print(f"  - 拓扑和分区图: {OUTPUT_DIR}/density_comparison/")
        if USE_REAL_DATA:
            print(f"  - 真实数据图: {OUTPUT_DIR}/real_data/")
        if PLOT_THRESHOLD_ANALYSIS:
            print(f"  - 阈值分析曲线: {OUTPUT_DIR}/threshold_analysis/")
        if GENERATE_LATEX_TABLES:
            print(f"  - LaTeX表格: {OUTPUT_DIR}/latex_tables/")
        print(f"  - LaTeX图片代码: {OUTPUT_DIR}/latex_figures/figures.tex")
    print("=" * 80)


if __name__ == '__main__':
    main()


"""
TPS阈值分析脚本 - 分析不同阈值下的TPS性能
"""

import matplotlib.pyplot as plt
import numpy as np
from tps_simulation import TPSSimulator
import os
from src.partition.strategies import PartitionStrategies
from src.partition.optimizer import PartitionOptimizer
import networkx as nx
from src.topology.real_data_loader import load_beijing_data
from src.topology.mec_builder import MECTopologyBuilder
from src.topology.spatial_generator import SpatialTopology, BaseStation


def load_real_topology():
    """
    加载真实的MEC拓扑
    
    Returns:
    --------
    nx.Graph : MEC拓扑图
    """
    # 配置参数（与run.py保持一致）
    REAL_DATA_CSV = 'data/real_topology/460.csv'
    REAL_DATA_CENTER_LAT = 39.98
    REAL_DATA_CENTER_LON = 116.39
    REAL_DATA_RADIUS_KM = 2.0
    REAL_DATA_N_MEC = 15
    REAL_DATA_COVERAGE_MIN = 300
    REAL_DATA_COVERAGE_MAX = 500
    
    print("加载真实MEC拓扑...")
    
    # 加载真实数据
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
    
    # 提取最大连通分量并重新编号
    if not nx.is_connected(real_mec_graph):
        print("提取最大连通分量并重新编号...")
        largest_cc = max(nx.connected_components(real_mec_graph), key=len)
        old_nodes = sorted(largest_cc)
        node_mapping = {old_id: new_id for new_id, old_id in enumerate(old_nodes)}
        
        new_graph = nx.Graph()
        for old_id, new_id in node_mapping.items():
            node_attrs = real_mec_graph.nodes[old_id]
            new_graph.add_node(new_id, **node_attrs)
        
        for u, v in real_mec_graph.edges():
            if u in node_mapping and v in node_mapping:
                new_u = node_mapping[u]
                new_v = node_mapping[v]
                edge_attrs = real_mec_graph[u][v]
                new_graph.add_edge(new_u, new_v, **edge_attrs)
        
        real_mec_graph = new_graph
    
    print(f"  节点数: {real_mec_graph.number_of_nodes()}")
    print(f"  边数: {real_mec_graph.number_of_edges()}")
    
    return real_mec_graph


def partition_to_string(partitions):
    """
    将分区结果转换为字符串格式
    
    Parameters:
    -----------
    partitions : List[Set[int]]
        分区列表
    
    Returns:
    --------
    str : 分区字符串，格式如 "0:[1,2,3] 1:[4,5,6,7]"
    """
    # 构建节点到分区的映射
    node_to_partitions = {}
    
    for partition_id, partition in enumerate(partitions, start=1):
        for node_id in partition:
            if node_id not in node_to_partitions:
                node_to_partitions[node_id] = []
            node_to_partitions[node_id].append(partition_id)
    
    # 转换为字符串
    parts = []
    for node_id in sorted(node_to_partitions.keys()):
        partition_list = node_to_partitions[node_id]
        parts.append(f"{node_id}:[{','.join(map(str, partition_list))}]")
    
    return ' '.join(parts)


def analyze_threshold_tps(mec_graph, threshold_range, leader_max_tps=15000):
    """
    分析不同阈值下的TPS性能
    
    Parameters:
    -----------
    mec_graph : nx.Graph
        MEC拓扑图
    threshold_range : range or list
        阈值范围
    leader_max_tps : int
        每个分区leader的最大TPS
    
    Returns:
    --------
    Dict : 包含各阈值下的TPS统计信息
    """
    print(f"\n{'='*80}")
    print("阈值TPS分析")
    print(f"{'='*80}\n")
    
    simulator = TPSSimulator(leader_max_tps=leader_max_tps)
    strategies = PartitionStrategies(mec_graph)
    
    results = {
        'thresholds': [],
        'num_partitions': [],
        'effective_tps': [],
        'invalid_tps': [],
        'utilization': [],
        'avg_node_tps': [],
        'max_node_tps': [],
        'min_node_tps': [],
        'num_multi_partition_nodes': []
    }
    
    for threshold in threshold_range:
        print(f"  分析阈值 τ={threshold}...")
        
        # 应用阈值分区
        partitions = strategies.threshold_based(merge_threshold=threshold)
        
        # 转换为字符串格式
        partition_str = partition_to_string(partitions)
        
        # 计算TPS
        node_to_partitions = simulator.parse_partitions(partition_str)
        partition_to_nodes = simulator.build_partition_to_nodes(node_to_partitions)
        stats = simulator.calculate_system_tps(node_to_partitions, partition_to_nodes, verbose=False)
        
        # 统计多分区节点
        num_multi_partition = sum(1 for p in node_to_partitions.values() if len(p) > 1)
        
        # 记录结果
        results['thresholds'].append(threshold)
        results['num_partitions'].append(stats['num_partitions'])
        results['effective_tps'].append(stats['effective_tps'])
        results['invalid_tps'].append(stats['invalid_tps'])
        results['utilization'].append((stats['effective_tps'] / stats['total_partition_capacity']) * 100)
        results['avg_node_tps'].append(stats['avg_node_tps'])
        results['max_node_tps'].append(stats['max_node_tps'])
        results['min_node_tps'].append(stats['min_node_tps'])
        results['num_multi_partition_nodes'].append(num_multi_partition)
        
        print(f"    分区数: {stats['num_partitions']}, "
              f"有效TPS: {stats['effective_tps']:,.0f}, "
              f"利用率: {results['utilization'][-1]:.2f}%")
    
    return results


def plot_threshold_tps_analysis(results, output_dir='output/tps_threshold_analysis'):
    """
    绘制阈值TPS分析图表
    
    Parameters:
    -----------
    results : Dict
        阈值分析结果
    output_dir : str
        输出目录
    """
    os.makedirs(output_dir, exist_ok=True)
    
    thresholds = results['thresholds']
    
    # 1. 有效TPS vs 阈值
    fig, ax = plt.subplots(figsize=(10, 6))
    
    ax.plot(thresholds, results['effective_tps'], 'o-', linewidth=2.5, 
            markersize=8, color='#2ecc71', label='Effective TPS')
    ax.plot(thresholds, results['invalid_tps'], 's--', linewidth=2.5, 
            markersize=8, color='#e74c3c', label='Invalid TPS')
    
    ax.set_xlabel('Threshold (τ)', fontsize=12, fontweight='bold')
    ax.set_ylabel('TPS', fontsize=12, fontweight='bold')
    ax.set_title('TPS vs Threshold', fontsize=14, fontweight='bold')
    ax.legend(fontsize=11)
    ax.grid(True, alpha=0.3)
    
    # 标记最优点
    max_idx = results['effective_tps'].index(max(results['effective_tps']))
    ax.axvline(x=thresholds[max_idx], color='red', linestyle=':', linewidth=2, alpha=0.7)
    ax.text(thresholds[max_idx], max(results['effective_tps']), 
            f'  Optimal τ={thresholds[max_idx]}', 
            fontsize=10, fontweight='bold', color='red')
    
    plt.tight_layout()
    plt.savefig(f'{output_dir}/tps_vs_threshold.png', dpi=300, bbox_inches='tight')
    print(f"  已保存: {output_dir}/tps_vs_threshold.png")
    plt.close()
    
    # 2. 利用率 vs 阈值
    fig, ax = plt.subplots(figsize=(10, 6))
    
    ax.plot(thresholds, results['utilization'], 'o-', linewidth=2.5, 
            markersize=8, color='#3498db')
    
    ax.set_xlabel('Threshold (τ)', fontsize=12, fontweight='bold')
    ax.set_ylabel('Utilization (%)', fontsize=12, fontweight='bold')
    ax.set_title('System Utilization vs Threshold', fontsize=14, fontweight='bold')
    ax.grid(True, alpha=0.3)
    ax.set_ylim(0, 105)
    
    # 标记最优点
    ax.axvline(x=thresholds[max_idx], color='red', linestyle=':', linewidth=2, alpha=0.7)
    ax.text(thresholds[max_idx], results['utilization'][max_idx], 
            f'  {results["utilization"][max_idx]:.1f}%', 
            fontsize=10, fontweight='bold', color='red')
    
    plt.tight_layout()
    plt.savefig(f'{output_dir}/utilization_vs_threshold.png', dpi=300, bbox_inches='tight')
    print(f"  已保存: {output_dir}/utilization_vs_threshold.png")
    plt.close()
    
    # 3. 分区数 vs 阈值
    fig, ax = plt.subplots(figsize=(10, 6))
    
    ax.plot(thresholds, results['num_partitions'], 'o-', linewidth=2.5, 
            markersize=8, color='#9b59b6')
    
    ax.set_xlabel('Threshold (τ)', fontsize=12, fontweight='bold')
    ax.set_ylabel('Number of Partitions', fontsize=12, fontweight='bold')
    ax.set_title('Number of Partitions vs Threshold', fontsize=14, fontweight='bold')
    ax.grid(True, alpha=0.3)
    
    plt.tight_layout()
    plt.savefig(f'{output_dir}/partitions_vs_threshold.png', dpi=300, bbox_inches='tight')
    print(f"  已保存: {output_dir}/partitions_vs_threshold.png")
    plt.close()
    
    # 4. 多分区节点数 vs 阈值
    fig, ax = plt.subplots(figsize=(10, 6))
    
    ax.plot(thresholds, results['num_multi_partition_nodes'], 'o-', linewidth=2.5, 
            markersize=8, color='#e67e22')
    
    ax.set_xlabel('Threshold (τ)', fontsize=12, fontweight='bold')
    ax.set_ylabel('Number of Multi-Partition Nodes', fontsize=12, fontweight='bold')
    ax.set_title('Multi-Partition Nodes vs Threshold', fontsize=14, fontweight='bold')
    ax.grid(True, alpha=0.3)
    
    plt.tight_layout()
    plt.savefig(f'{output_dir}/multi_partition_nodes_vs_threshold.png', dpi=300, bbox_inches='tight')
    print(f"  已保存: {output_dir}/multi_partition_nodes_vs_threshold.png")
    plt.close()
    
    # 5. 综合对比图（2x2子图）
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    
    # 子图1: 有效TPS vs 阈值
    ax = axes[0, 0]
    ax.plot(thresholds, results['effective_tps'], 'o-', linewidth=2.5, 
            markersize=8, color='#2ecc71')
    ax.set_xlabel('Threshold (τ)', fontsize=11, fontweight='bold')
    ax.set_ylabel('Effective TPS', fontsize=11, fontweight='bold')
    ax.set_title('(a) Effective TPS vs Threshold', fontsize=12, fontweight='bold')
    ax.grid(True, alpha=0.3)
    ax.axvline(x=thresholds[max_idx], color='red', linestyle=':', linewidth=2, alpha=0.7)
    
    # 子图2: 利用率 vs 阈值
    ax = axes[0, 1]
    ax.plot(thresholds, results['utilization'], 'o-', linewidth=2.5, 
            markersize=8, color='#3498db')
    ax.set_xlabel('Threshold (τ)', fontsize=11, fontweight='bold')
    ax.set_ylabel('Utilization (%)', fontsize=11, fontweight='bold')
    ax.set_title('(b) Utilization vs Threshold', fontsize=12, fontweight='bold')
    ax.grid(True, alpha=0.3)
    ax.axvline(x=thresholds[max_idx], color='red', linestyle=':', linewidth=2, alpha=0.7)
    
    # 子图3: 分区数 vs 阈值
    ax = axes[1, 0]
    ax.plot(thresholds, results['num_partitions'], 'o-', linewidth=2.5, 
            markersize=8, color='#9b59b6')
    ax.set_xlabel('Threshold (τ)', fontsize=11, fontweight='bold')
    ax.set_ylabel('Number of Partitions', fontsize=11, fontweight='bold')
    ax.set_title('(c) Partitions vs Threshold', fontsize=12, fontweight='bold')
    ax.grid(True, alpha=0.3)
    ax.axvline(x=thresholds[max_idx], color='red', linestyle=':', linewidth=2, alpha=0.7)
    
    # 子图4: 多分区节点数 vs 阈值
    ax = axes[1, 1]
    ax.plot(thresholds, results['num_multi_partition_nodes'], 'o-', linewidth=2.5, 
            markersize=8, color='#e67e22')
    ax.set_xlabel('Threshold (τ)', fontsize=11, fontweight='bold')
    ax.set_ylabel('Multi-Partition Nodes', fontsize=11, fontweight='bold')
    ax.set_title('(d) Multi-Partition Nodes vs Threshold', fontsize=12, fontweight='bold')
    ax.grid(True, alpha=0.3)
    ax.axvline(x=thresholds[max_idx], color='red', linestyle=':', linewidth=2, alpha=0.7)
    
    plt.tight_layout()
    plt.savefig(f'{output_dir}/comprehensive_analysis.png', dpi=300, bbox_inches='tight')
    print(f"  已保存: {output_dir}/comprehensive_analysis.png")
    plt.close()
    
    # 6. 节点TPS范围 vs 阈值
    fig, ax = plt.subplots(figsize=(10, 6))
    
    ax.plot(thresholds, results['avg_node_tps'], 'o-', linewidth=2.5, 
            markersize=8, color='#1abc9c', label='Average Node TPS')
    ax.fill_between(thresholds, results['min_node_tps'], results['max_node_tps'], 
                     alpha=0.3, color='#1abc9c', label='TPS Range')
    
    ax.set_xlabel('Threshold (τ)', fontsize=12, fontweight='bold')
    ax.set_ylabel('Node TPS', fontsize=12, fontweight='bold')
    ax.set_title('Node TPS Distribution vs Threshold', fontsize=14, fontweight='bold')
    ax.legend(fontsize=11)
    ax.grid(True, alpha=0.3)
    
    plt.tight_layout()
    plt.savefig(f'{output_dir}/node_tps_range_vs_threshold.png', dpi=300, bbox_inches='tight')
    print(f"  已保存: {output_dir}/node_tps_range_vs_threshold.png")
    plt.close()


def generate_threshold_comparison_table(results, output_dir='output/tps_threshold_analysis'):
    """
    生成阈值对比的LaTeX表格
    
    Parameters:
    -----------
    results : Dict
        阈值分析结果
    output_dir : str
        输出目录
    """
    os.makedirs(output_dir, exist_ok=True)
    
    latex_code = r"""\begin{table}[htbp]
\centering
\caption{不同阈值下的TPS性能对比}
\label{tab:threshold_tps_comparison}
\resizebox{\textwidth}{!}{
\begin{tabular}{ccccccccc}
\toprule
\textbf{阈值} & \textbf{分区数} & \textbf{有效TPS} & \textbf{无效TPS} & \textbf{利用率} & \textbf{平均节点TPS} & \textbf{最大节点TPS} & \textbf{最小节点TPS} & \textbf{多分区节点数} \\
\midrule
"""
    
    # 找出最优阈值
    max_idx = results['effective_tps'].index(max(results['effective_tps']))
    
    for i, threshold in enumerate(results['thresholds']):
        # 如果是最优阈值，加粗显示
        if i == max_idx:
            latex_code += r"\rowcolor{yellow!20} "
            latex_code += f"\\textbf{{{threshold}}} & "
        else:
            latex_code += f"{threshold} & "
        
        latex_code += f"{results['num_partitions'][i]} & "
        latex_code += f"{int(results['effective_tps'][i]):,} & "
        latex_code += f"{int(results['invalid_tps'][i]):,} & "
        latex_code += f"{results['utilization'][i]:.2f}\\% & "
        latex_code += f"{results['avg_node_tps'][i]:,.0f} & "
        latex_code += f"{results['max_node_tps'][i]:,.0f} & "
        latex_code += f"{results['min_node_tps'][i]:,.0f} & "
        latex_code += f"{results['num_multi_partition_nodes'][i]} \\\\\n"
    
    latex_code += r"""\bottomrule
\end{tabular}
}
\end{table}
"""
    
    # 保存到文件
    with open(f'{output_dir}/threshold_tps_comparison.tex', 'w', encoding='utf-8') as f:
        f.write(latex_code)
    
    print(f"  已保存LaTeX表格: {output_dir}/threshold_tps_comparison.tex")


def main():
    """主函数"""
    print("=" * 80)
    print("MEC拓扑系统 - 阈值TPS分析")
    print("=" * 80)
    
    # 配置参数
    THRESHOLD_RANGE = range(2, 16)  # 阈值范围 2-15
    LEADER_MAX_TPS = 15000
    
    print(f"\n配置:")
    print(f"  阈值范围: {list(THRESHOLD_RANGE)}")
    print(f"  每个分区Leader最大TPS: {LEADER_MAX_TPS:,}")
    
    # 加载真实拓扑
    mec_graph = load_real_topology()
    
    # 分析不同阈值下的TPS
    results = analyze_threshold_tps(mec_graph, THRESHOLD_RANGE, LEADER_MAX_TPS)
    
    # 找出最优阈值
    max_idx = results['effective_tps'].index(max(results['effective_tps']))
    optimal_threshold = results['thresholds'][max_idx]
    
    # 打印总结
    print(f"\n{'='*80}")
    print("阈值分析总结")
    print(f"{'='*80}\n")
    
    print(f"{'阈值':<8} {'分区数':<10} {'有效TPS':<15} {'无效TPS':<15} {'利用率':<12} {'多分区节点':<12}")
    print("-" * 80)
    
    for i, threshold in enumerate(results['thresholds']):
        marker = " ★" if i == max_idx else ""
        print(f"{threshold:<8} {results['num_partitions'][i]:<10} "
              f"{results['effective_tps'][i]:>12,.0f}   "
              f"{results['invalid_tps'][i]:>12,.0f}   "
              f"{results['utilization'][i]:>10.2f}%   "
              f"{results['num_multi_partition_nodes'][i]:<12}{marker}")
    
    print(f"\n最优阈值: τ={optimal_threshold}")
    print(f"  分区数: {results['num_partitions'][max_idx]}")
    print(f"  有效TPS: {results['effective_tps'][max_idx]:,.0f}")
    print(f"  利用率: {results['utilization'][max_idx]:.2f}%")
    
    # 生成可视化
    print(f"\n{'='*80}")
    print("生成可视化图表")
    print(f"{'='*80}\n")
    plot_threshold_tps_analysis(results)
    
    # 生成LaTeX表格
    print(f"\n{'='*80}")
    print("生成LaTeX表格")
    print(f"{'='*80}\n")
    generate_threshold_comparison_table(results)
    
    print("\n" + "=" * 80)
    print("阈值TPS分析完成！")
    print("=" * 80)


if __name__ == '__main__':
    main()

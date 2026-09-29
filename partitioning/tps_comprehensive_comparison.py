"""
TPS综合对比脚本 - 对比所有分区方法（包括不同阈值）的TPS性能
"""

import matplotlib.pyplot as plt
import numpy as np
from tps_simulation import TPSSimulator
from tps_threshold_analysis import load_real_topology, partition_to_string
import os
from src.partition.strategies import PartitionStrategies
from src.partition.optimizer import PartitionOptimizer


def analyze_all_methods(mec_graph, threshold_range, leader_max_tps=15000):
    """
    分析所有分区方法的TPS性能
    
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
    Dict : 所有方法的TPS统计信息
    """
    print(f"\n{'='*80}")
    print("综合TPS分析 - 所有分区方法")
    print(f"{'='*80}\n")
    
    simulator = TPSSimulator(leader_max_tps=leader_max_tps)
    strategies = PartitionStrategies(mec_graph)
    optimizer = PartitionOptimizer(mec_graph)
    
    all_results = {}
    
    # 1. Global方法
    print("  [1/4] 分析Global方法...")
    global_partitions = strategies.global_partition()
    partition_str = partition_to_string(global_partitions)
    node_to_partitions = simulator.parse_partitions(partition_str)
    partition_to_nodes = simulator.build_partition_to_nodes(node_to_partitions)
    stats = simulator.calculate_system_tps(node_to_partitions, partition_to_nodes, verbose=False)
    all_results['Global'] = {
        'method': 'Global',
        'threshold': None,
        **stats
    }
    print(f"    分区数: {stats['num_partitions']}, 有效TPS: {stats['effective_tps']:,.0f}")
    
    # 2. Neighbor-based方法
    print("  [2/4] 分析Neighbor-based方法...")
    neighbor_partitions = strategies.neighbor_based()
    partition_str = partition_to_string(neighbor_partitions)
    node_to_partitions = simulator.parse_partitions(partition_str)
    partition_to_nodes = simulator.build_partition_to_nodes(node_to_partitions)
    stats = simulator.calculate_system_tps(node_to_partitions, partition_to_nodes, verbose=False)
    all_results['Neighbor-based'] = {
        'method': 'Neighbor-based',
        'threshold': None,
        **stats
    }
    print(f"    分区数: {stats['num_partitions']}, 有效TPS: {stats['effective_tps']:,.0f}")
    
    # 3. Maximal Clique方法
    print("  [3/4] 分析Maximal Clique方法...")
    clique_partitions = strategies.maximal_clique()
    partition_str = partition_to_string(clique_partitions)
    node_to_partitions = simulator.parse_partitions(partition_str)
    partition_to_nodes = simulator.build_partition_to_nodes(node_to_partitions)
    stats = simulator.calculate_system_tps(node_to_partitions, partition_to_nodes, verbose=False)
    all_results['Maximal Clique'] = {
        'method': 'Maximal Clique',
        'threshold': None,
        **stats
    }
    print(f"    分区数: {stats['num_partitions']}, 有效TPS: {stats['effective_tps']:,.0f}")
    
    # 4. Threshold方法（不同阈值）
    print("  [4/4] 分析Threshold方法（不同阈值）...")
    for threshold in threshold_range:
        threshold_partitions = strategies.threshold_based(merge_threshold=threshold)
        partition_str = partition_to_string(threshold_partitions)
        node_to_partitions = simulator.parse_partitions(partition_str)
        partition_to_nodes = simulator.build_partition_to_nodes(node_to_partitions)
        stats = simulator.calculate_system_tps(node_to_partitions, partition_to_nodes, verbose=False)
        
        method_name = f'Threshold (τ={threshold})'
        all_results[method_name] = {
            'method': 'Threshold',
            'threshold': threshold,
            **stats
        }
        print(f"    τ={threshold}: 分区数={stats['num_partitions']}, 有效TPS={stats['effective_tps']:,.0f}")
    
    return all_results


def plot_comprehensive_comparison(all_results, output_dir='output/tps_comprehensive'):
    """
    绘制综合对比图表
    
    Parameters:
    -----------
    all_results : Dict
        所有方法的结果
    output_dir : str
        输出目录
    """
    os.makedirs(output_dir, exist_ok=True)
    
    # 分离基础方法和阈值方法
    base_methods = ['Global', 'Neighbor-based', 'Maximal Clique']
    threshold_methods = [k for k in all_results.keys() if k.startswith('Threshold')]
    
    # 提取阈值方法的数据
    thresholds = [all_results[m]['threshold'] for m in threshold_methods]
    threshold_effective_tps = [all_results[m]['effective_tps'] for m in threshold_methods]
    threshold_utilization = [(all_results[m]['effective_tps'] / all_results[m]['total_partition_capacity']) * 100 
                             for m in threshold_methods]
    
    # 1. 有效TPS对比（基础方法 + 阈值曲线）
    fig, ax = plt.subplots(figsize=(12, 7))
    
    # 绘制基础方法的柱状图
    base_tps = [all_results[m]['effective_tps'] for m in base_methods]
    x_base = np.arange(len(base_methods))
    colors_base = ['#3498db', '#e67e22', '#9b59b6']
    bars = ax.bar(x_base, base_tps, width=0.6, color=colors_base, alpha=0.8, 
                   edgecolor='black', linewidth=1.5, label='Base Methods')
    
    # 添加数值标签
    for bar in bars:
        height = bar.get_height()
        ax.text(bar.get_x() + bar.get_width()/2., height,
                f'{int(height):,}',
                ha='center', va='bottom', fontsize=10, fontweight='bold')
    
    # 在右侧绘制阈值方法的曲线
    ax2 = ax.twinx()
    ax2.plot(np.array(thresholds) + len(base_methods), threshold_effective_tps, 
             'o-', linewidth=2.5, markersize=8, color='#e74c3c', label='Threshold Methods')
    
    # 标记最优阈值
    max_idx = threshold_effective_tps.index(max(threshold_effective_tps))
    ax2.plot(thresholds[max_idx] + len(base_methods), threshold_effective_tps[max_idx], 
             '*', markersize=20, color='gold', markeredgecolor='black', markeredgewidth=2,
             label=f'Optimal (τ={thresholds[max_idx]})')
    
    # 设置x轴
    all_labels = base_methods + [f'τ={t}' for t in thresholds]
    ax.set_xticks(range(len(all_labels)))
    ax.set_xticklabels(all_labels, rotation=45, ha='right')
    
    ax.set_xlabel('Partitioning Method', fontsize=12, fontweight='bold')
    ax.set_ylabel('Effective TPS (Base Methods)', fontsize=12, fontweight='bold', color='#3498db')
    ax2.set_ylabel('Effective TPS (Threshold Methods)', fontsize=12, fontweight='bold', color='#e74c3c')
    ax.set_title('Comprehensive TPS Comparison: All Methods', fontsize=14, fontweight='bold')
    
    ax.tick_params(axis='y', labelcolor='#3498db')
    ax2.tick_params(axis='y', labelcolor='#e74c3c')
    
    # 合并图例
    lines1, labels1 = ax.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax.legend(lines1 + lines2, labels1 + labels2, loc='upper left', fontsize=10)
    
    ax.grid(axis='y', alpha=0.3)
    
    plt.tight_layout()
    plt.savefig(f'{output_dir}/comprehensive_tps_comparison.png', dpi=300, bbox_inches='tight')
    print(f"  已保存: {output_dir}/comprehensive_tps_comparison.png")
    plt.close()
    
    # 2. 利用率对比
    fig, ax = plt.subplots(figsize=(12, 7))
    
    # 基础方法
    base_utilization = [(all_results[m]['effective_tps'] / all_results[m]['total_partition_capacity']) * 100 
                        for m in base_methods]
    bars = ax.bar(x_base, base_utilization, width=0.6, color=colors_base, alpha=0.8, 
                   edgecolor='black', linewidth=1.5)
    
    # 添加数值标签
    for bar in bars:
        height = bar.get_height()
        ax.text(bar.get_x() + bar.get_width()/2., height,
                f'{height:.1f}%',
                ha='center', va='bottom', fontsize=10, fontweight='bold')
    
    # 阈值方法曲线
    ax.plot(np.array(thresholds) + len(base_methods) - 0.5, threshold_utilization, 
            'o-', linewidth=2.5, markersize=8, color='#e74c3c', label='Threshold Methods')
    
    # 标记最优阈值
    max_util_idx = threshold_utilization.index(max(threshold_utilization))
    ax.plot(thresholds[max_util_idx] + len(base_methods) - 0.5, threshold_utilization[max_util_idx], 
            '*', markersize=20, color='gold', markeredgecolor='black', markeredgewidth=2)
    
    ax.set_xticks(range(len(all_labels)))
    ax.set_xticklabels(all_labels, rotation=45, ha='right')
    ax.set_xlabel('Partitioning Method', fontsize=12, fontweight='bold')
    ax.set_ylabel('Utilization (%)', fontsize=12, fontweight='bold')
    ax.set_title('System Utilization Comparison: All Methods', fontsize=14, fontweight='bold')
    ax.set_ylim(0, 110)
    ax.grid(axis='y', alpha=0.3)
    ax.legend(fontsize=10)
    
    plt.tight_layout()
    plt.savefig(f'{output_dir}/comprehensive_utilization_comparison.png', dpi=300, bbox_inches='tight')
    print(f"  已保存: {output_dir}/comprehensive_utilization_comparison.png")
    plt.close()
    
    # 3. 散点图：分区数 vs 有效TPS（所有方法）
    fig, ax = plt.subplots(figsize=(12, 8))
    
    # 基础方法
    base_partitions = [all_results[m]['num_partitions'] for m in base_methods]
    ax.scatter(base_partitions, base_tps, s=400, c=colors_base, alpha=0.7, 
              edgecolors='black', linewidth=2, marker='s', label='Base Methods')
    
    # 标注基础方法
    for i, method in enumerate(base_methods):
        ax.annotate(method, (base_partitions[i], base_tps[i]), 
                   xytext=(10, 10), textcoords='offset points',
                   fontsize=10, fontweight='bold',
                   bbox=dict(boxstyle='round,pad=0.5', facecolor='yellow', alpha=0.3))
    
    # 阈值方法
    threshold_partitions = [all_results[m]['num_partitions'] for m in threshold_methods]
    scatter = ax.scatter(threshold_partitions, threshold_effective_tps, s=300, 
                        c=thresholds, cmap='coolwarm', alpha=0.7, 
                        edgecolors='black', linewidth=2, marker='o')
    
    # 添加颜色条
    cbar = plt.colorbar(scatter, ax=ax)
    cbar.set_label('Threshold (τ)', fontsize=11, fontweight='bold')
    
    # 标注最优阈值
    ax.scatter([threshold_partitions[max_idx]], [threshold_effective_tps[max_idx]], 
              s=600, marker='*', color='gold', edgecolors='black', linewidth=2,
              label=f'Optimal Threshold (τ={thresholds[max_idx]})', zorder=5)
    
    ax.set_xlabel('Number of Partitions', fontsize=12, fontweight='bold')
    ax.set_ylabel('Effective TPS', fontsize=12, fontweight='bold')
    ax.set_title('Partitions vs Effective TPS: All Methods', fontsize=14, fontweight='bold')
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=10, loc='best')
    
    plt.tight_layout()
    plt.savefig(f'{output_dir}/partitions_vs_tps_all_methods.png', dpi=300, bbox_inches='tight')
    print(f"  已保存: {output_dir}/partitions_vs_tps_all_methods.png")
    plt.close()
    
    # 4. 帕累托前沿分析（利用率 vs 有效TPS）
    fig, ax = plt.subplots(figsize=(12, 8))
    
    # 基础方法
    ax.scatter(base_utilization, base_tps, s=400, c=colors_base, alpha=0.7, 
              edgecolors='black', linewidth=2, marker='s', label='Base Methods')
    
    for i, method in enumerate(base_methods):
        ax.annotate(method, (base_utilization[i], base_tps[i]), 
                   xytext=(10, 10), textcoords='offset points',
                   fontsize=10, fontweight='bold',
                   bbox=dict(boxstyle='round,pad=0.5', facecolor='yellow', alpha=0.3))
    
    # 阈值方法
    scatter = ax.scatter(threshold_utilization, threshold_effective_tps, s=300, 
                        c=thresholds, cmap='coolwarm', alpha=0.7, 
                        edgecolors='black', linewidth=2, marker='o')
    
    cbar = plt.colorbar(scatter, ax=ax)
    cbar.set_label('Threshold (τ)', fontsize=11, fontweight='bold')
    
    # 标注最优点（最大有效TPS）
    ax.scatter([threshold_utilization[max_idx]], [threshold_effective_tps[max_idx]], 
              s=600, marker='*', color='gold', edgecolors='black', linewidth=2,
              label=f'Max TPS (τ={thresholds[max_idx]})', zorder=5)
    
    # 标注最高利用率点
    ax.scatter([threshold_utilization[max_util_idx]], [threshold_effective_tps[max_util_idx]], 
              s=600, marker='P', color='lime', edgecolors='black', linewidth=2,
              label=f'Max Utilization (τ={thresholds[max_util_idx]})', zorder=5)
    
    ax.set_xlabel('Utilization (%)', fontsize=12, fontweight='bold')
    ax.set_ylabel('Effective TPS', fontsize=12, fontweight='bold')
    ax.set_title('Pareto Frontier: Utilization vs Effective TPS', fontsize=14, fontweight='bold')
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=10, loc='best')
    
    plt.tight_layout()
    plt.savefig(f'{output_dir}/pareto_frontier.png', dpi=300, bbox_inches='tight')
    print(f"  已保存: {output_dir}/pareto_frontier.png")
    plt.close()


def generate_comprehensive_table(all_results, output_dir='output/tps_comprehensive'):
    """
    生成综合对比的LaTeX表格
    
    Parameters:
    -----------
    all_results : Dict
        所有方法的结果
    output_dir : str
        输出目录
    """
    os.makedirs(output_dir, exist_ok=True)
    
    # 找出最优方法（最大有效TPS）
    max_tps_method = max(all_results.items(), key=lambda x: x[1]['effective_tps'])
    max_util_method = max(all_results.items(), 
                         key=lambda x: x[1]['effective_tps'] / x[1]['total_partition_capacity'])
    
    latex_code = r"""\begin{table}[htbp]
\centering
\caption{所有分区方法的TPS性能综合对比}
\label{tab:comprehensive_tps_comparison}
\resizebox{\textwidth}{!}{
\begin{tabular}{lccccc}
\toprule
\textbf{方法} & \textbf{分区数} & \textbf{有效TPS} & \textbf{无效TPS} & \textbf{利用率} & \textbf{备注} \\
\midrule
"""
    
    # 按方法类型分组
    base_methods = ['Global', 'Neighbor-based', 'Maximal Clique']
    threshold_methods = sorted([k for k in all_results.keys() if k.startswith('Threshold')],
                               key=lambda x: all_results[x]['threshold'])
    
    # 基础方法
    for method in base_methods:
        stats = all_results[method]
        utilization = (stats['effective_tps'] / stats['total_partition_capacity']) * 100
        
        note = ""
        if method == max_tps_method[0]:
            note = "最大TPS"
        if method == max_util_method[0]:
            note = "最高利用率" if not note else note + ", 最高利用率"
        
        latex_code += f"{method} & {stats['num_partitions']} & "
        latex_code += f"{int(stats['effective_tps']):,} & "
        latex_code += f"{int(stats['invalid_tps']):,} & "
        latex_code += f"{utilization:.2f}\\% & "
        latex_code += f"{note} \\\\\n"
    
    latex_code += r"\midrule" + "\n"
    
    # 阈值方法
    for method in threshold_methods:
        stats = all_results[method]
        utilization = (stats['effective_tps'] / stats['total_partition_capacity']) * 100
        threshold = stats['threshold']
        
        note = ""
        if method == max_tps_method[0]:
            note = "\\textbf{最大TPS}"
            latex_code += r"\rowcolor{yellow!20} "
        if method == max_util_method[0]:
            note = "\\textbf{最高利用率}" if not note else note + ", \\textbf{最高利用率}"
            if method != max_tps_method[0]:
                latex_code += r"\rowcolor{green!20} "
        
        latex_code += f"Threshold ($\\tau={threshold}$) & {stats['num_partitions']} & "
        latex_code += f"{int(stats['effective_tps']):,} & "
        latex_code += f"{int(stats['invalid_tps']):,} & "
        latex_code += f"{utilization:.2f}\\% & "
        latex_code += f"{note} \\\\\n"
    
    latex_code += r"""\bottomrule
\end{tabular}
}
\end{table}
"""
    
    # 保存到文件
    with open(f'{output_dir}/comprehensive_comparison.tex', 'w', encoding='utf-8') as f:
        f.write(latex_code)
    
    print(f"  已保存LaTeX表格: {output_dir}/comprehensive_comparison.tex")


def main():
    """主函数"""
    print("=" * 80)
    print("MEC拓扑系统 - 综合TPS对比分析")
    print("=" * 80)
    
    # 配置参数
    THRESHOLD_RANGE = range(2, 16)
    LEADER_MAX_TPS = 15000
    
    print(f"\n配置:")
    print(f"  阈值范围: {list(THRESHOLD_RANGE)}")
    print(f"  每个分区Leader最大TPS: {LEADER_MAX_TPS:,}")
    
    # 加载真实拓扑
    mec_graph = load_real_topology()
    
    # 分析所有方法
    all_results = analyze_all_methods(mec_graph, THRESHOLD_RANGE, LEADER_MAX_TPS)
    
    # 找出最优方法
    max_tps_method = max(all_results.items(), key=lambda x: x[1]['effective_tps'])
    max_util_method = max(all_results.items(), 
                         key=lambda x: x[1]['effective_tps'] / x[1]['total_partition_capacity'])
    
    # 打印总结
    print(f"\n{'='*80}")
    print("综合分析总结")
    print(f"{'='*80}\n")
    
    print(f"最大有效TPS方法: {max_tps_method[0]}")
    print(f"  有效TPS: {max_tps_method[1]['effective_tps']:,.0f}")
    print(f"  分区数: {max_tps_method[1]['num_partitions']}")
    print(f"  利用率: {(max_tps_method[1]['effective_tps'] / max_tps_method[1]['total_partition_capacity']) * 100:.2f}%")
    
    print(f"\n最高利用率方法: {max_util_method[0]}")
    print(f"  利用率: {(max_util_method[1]['effective_tps'] / max_util_method[1]['total_partition_capacity']) * 100:.2f}%")
    print(f"  有效TPS: {max_util_method[1]['effective_tps']:,.0f}")
    print(f"  分区数: {max_util_method[1]['num_partitions']}")
    
    # 生成可视化
    print(f"\n{'='*80}")
    print("生成可视化图表")
    print(f"{'='*80}\n")
    plot_comprehensive_comparison(all_results)
    
    # 生成LaTeX表格
    print(f"\n{'='*80}")
    print("生成LaTeX表格")
    print(f"{'='*80}\n")
    generate_comprehensive_table(all_results)
    
    print("\n" + "=" * 80)
    print("综合TPS对比分析完成！")
    print("=" * 80)


if __name__ == '__main__':
    main()

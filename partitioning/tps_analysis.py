"""
TPS分析脚本 - 包含可视化和详细分析
"""

import matplotlib.pyplot as plt
import numpy as np
from tps_simulation import TPSSimulator
import os


def plot_tps_comparison(all_results, output_dir='output/tps_analysis'):
    """
    绘制TPS对比图
    
    Parameters:
    -----------
    all_results : Dict
        所有方法的结果
    output_dir : str
        输出目录
    """
    os.makedirs(output_dir, exist_ok=True)
    
    methods = list(all_results.keys())
    effective_tps = [all_results[m]['effective_tps'] for m in methods]
    invalid_tps = [all_results[m]['invalid_tps'] for m in methods]
    num_partitions = [all_results[m]['num_partitions'] for m in methods]
    
    # 1. 有效TPS vs 无效TPS对比
    fig, ax = plt.subplots(figsize=(10, 6))
    x = np.arange(len(methods))
    width = 0.35
    
    bars1 = ax.bar(x - width/2, effective_tps, width, label='有效TPS', color='#2ecc71')
    bars2 = ax.bar(x + width/2, invalid_tps, width, label='无效TPS', color='#e74c3c')
    
    ax.set_xlabel('分区方法', fontsize=12, fontweight='bold')
    ax.set_ylabel('TPS', fontsize=12, fontweight='bold')
    ax.set_title('不同分区方法的TPS对比', fontsize=14, fontweight='bold')
    ax.set_xticks(x)
    ax.set_xticklabels(methods, rotation=15, ha='right')
    ax.legend(fontsize=11)
    ax.grid(axis='y', alpha=0.3)
    
    # 添加数值标签
    for bar in bars1:
        height = bar.get_height()
        ax.text(bar.get_x() + bar.get_width()/2., height,
                f'{int(height):,}',
                ha='center', va='bottom', fontsize=9)
    
    for bar in bars2:
        height = bar.get_height()
        ax.text(bar.get_x() + bar.get_width()/2., height,
                f'{int(height):,}',
                ha='center', va='bottom', fontsize=9)
    
    plt.tight_layout()
    plt.savefig(f'{output_dir}/tps_comparison.png', dpi=300, bbox_inches='tight')
    print(f"  已保存: {output_dir}/tps_comparison.png")
    plt.close()
    
    # 2. 利用率对比
    fig, ax = plt.subplots(figsize=(10, 6))
    utilization = [(all_results[m]['effective_tps'] / all_results[m]['total_partition_capacity']) * 100 
                   for m in methods]
    
    colors = ['#3498db', '#e67e22', '#9b59b6', '#1abc9c']
    bars = ax.bar(methods, utilization, color=colors, alpha=0.8, edgecolor='black', linewidth=1.5)
    
    ax.set_xlabel('分区方法', fontsize=12, fontweight='bold')
    ax.set_ylabel('利用率 (%)', fontsize=12, fontweight='bold')
    ax.set_title('不同分区方法的系统利用率', fontsize=14, fontweight='bold')
    ax.set_ylim(0, 110)
    ax.grid(axis='y', alpha=0.3)
    
    # 添加数值标签
    for bar in bars:
        height = bar.get_height()
        ax.text(bar.get_x() + bar.get_width()/2., height,
                f'{height:.1f}%',
                ha='center', va='bottom', fontsize=11, fontweight='bold')
    
    plt.xticks(rotation=15, ha='right')
    plt.tight_layout()
    plt.savefig(f'{output_dir}/utilization_comparison.png', dpi=300, bbox_inches='tight')
    print(f"  已保存: {output_dir}/utilization_comparison.png")
    plt.close()
    
    # 3. 分区数 vs 有效TPS散点图
    fig, ax = plt.subplots(figsize=(10, 6))
    
    scatter = ax.scatter(num_partitions, effective_tps, s=300, c=colors, alpha=0.7, 
                        edgecolors='black', linewidth=2)
    
    # 添加方法标签
    for i, method in enumerate(methods):
        ax.annotate(method, (num_partitions[i], effective_tps[i]), 
                   xytext=(10, 10), textcoords='offset points',
                   fontsize=10, fontweight='bold',
                   bbox=dict(boxstyle='round,pad=0.5', facecolor='yellow', alpha=0.3))
    
    ax.set_xlabel('分区数', fontsize=12, fontweight='bold')
    ax.set_ylabel('有效TPS', fontsize=12, fontweight='bold')
    ax.set_title('分区数与有效TPS的关系', fontsize=14, fontweight='bold')
    ax.grid(True, alpha=0.3)
    
    plt.tight_layout()
    plt.savefig(f'{output_dir}/partitions_vs_tps.png', dpi=300, bbox_inches='tight')
    print(f"  已保存: {output_dir}/partitions_vs_tps.png")
    plt.close()
    
    # 4. 节点TPS分布热力图
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    axes = axes.flatten()
    
    for idx, method in enumerate(methods):
        ax = axes[idx]
        
        # 获取节点TPS数据
        simulator = TPSSimulator()
        node_to_partitions = simulator.parse_partitions(
            get_partition_string(method)
        )
        partition_to_nodes = simulator.build_partition_to_nodes(node_to_partitions)
        node_tps = simulator.calculate_node_tps(node_to_partitions, partition_to_nodes)
        
        nodes = sorted(node_tps.keys())
        tps_values = [node_tps[n] for n in nodes]
        
        # 绘制条形图
        bars = ax.bar(nodes, tps_values, color='#3498db', alpha=0.7, edgecolor='black')
        
        # 为多分区节点标记不同颜色
        for i, node in enumerate(nodes):
            if len(node_to_partitions[node]) > 1:
                bars[i].set_color('#e74c3c')
        
        ax.set_xlabel('节点ID', fontsize=10, fontweight='bold')
        ax.set_ylabel('TPS', fontsize=10, fontweight='bold')
        ax.set_title(f'{method} - 节点TPS分布', fontsize=11, fontweight='bold')
        ax.grid(axis='y', alpha=0.3)
        ax.set_xticks(nodes)
        
        # 添加图例
        from matplotlib.patches import Patch
        legend_elements = [
            Patch(facecolor='#3498db', label='单分区节点'),
            Patch(facecolor='#e74c3c', label='多分区节点')
        ]
        ax.legend(handles=legend_elements, fontsize=8, loc='upper right')
    
    plt.tight_layout()
    plt.savefig(f'{output_dir}/node_tps_distribution.png', dpi=300, bbox_inches='tight')
    print(f"  已保存: {output_dir}/node_tps_distribution.png")
    plt.close()


def get_partition_string(method_name):
    """获取分区字符串"""
    methods = {
        'Global': '0:[1] 1:[1] 2:[1] 3:[1] 4:[1] 5:[1] 6:[1] 7:[1] 8:[1] 9:[1] 10:[1] 11:[1] 12:[1] 13:[1] 14:[1]',
        'Neighbor-based': '0:[1,2,3] 1:[4,5,6,7] 2:[1,2,4,6] 3:[5,6] 4:[8,9] 5:[5,7] 6:[2,3,9] 7:[1,2,4] 8:[7] 9:[1,4,5,6] 10:[8] 11:[3,8,9] 12:[1,4,5,6] 13:[4,5,6] 14:[1,4]',
        'Maximal Clique': '0:[1,2] 1:[3,4,5] 2:[1,6,7] 3:[3] 4:[8,9] 5:[5,10] 6:[2,11] 7:[1,6] 8:[10] 9:[3,4,7] 10:[8] 11:[9,11] 12:[4,6,7] 13:[3,4] 14:[6]',
        'Threshold': '0:[1] 1:[2,3] 2:[1,4] 3:[2] 4:[5] 5:[3] 6:[1] 7:[1,4] 8:[3] 9:[2,4] 10:[5] 11:[1,5] 12:[2,4] 13:[2] 14:[4]'
    }
    return methods.get(method_name, '')


def analyze_multi_partition_nodes(all_results):
    """
    分析多分区节点的影响
    
    Parameters:
    -----------
    all_results : Dict
        所有方法的结果
    """
    print(f"\n{'='*80}")
    print("多分区节点分析")
    print(f"{'='*80}\n")
    
    simulator = TPSSimulator()
    
    for method_name in all_results.keys():
        partition_str = get_partition_string(method_name)
        node_to_partitions = simulator.parse_partitions(partition_str)
        
        # 统计多分区节点
        multi_partition_nodes = {
            node: partitions for node, partitions in node_to_partitions.items()
            if len(partitions) > 1
        }
        
        print(f"{method_name}:")
        print(f"  多分区节点数: {len(multi_partition_nodes)} / {len(node_to_partitions)}")
        
        if multi_partition_nodes:
            print(f"  多分区节点详情:")
            for node, partitions in sorted(multi_partition_nodes.items()):
                print(f"    节点 {node}: {len(partitions)} 个分区 {partitions}")
            
            # 计算平均分区数
            avg_partitions = sum(len(p) for p in multi_partition_nodes.values()) / len(multi_partition_nodes)
            print(f"  平均每个多分区节点的分区数: {avg_partitions:.2f}")
        
        print()


def generate_latex_table(all_results, output_dir='output/tps_analysis'):
    """
    生成LaTeX表格
    
    Parameters:
    -----------
    all_results : Dict
        所有方法的结果
    output_dir : str
        输出目录
    """
    os.makedirs(output_dir, exist_ok=True)
    
    latex_code = r"""\begin{table}[htbp]
\centering
\caption{不同分区方法的TPS性能对比}
\label{tab:tps_comparison}
\begin{tabular}{lcccccc}
\toprule
\textbf{方法} & \textbf{分区数} & \textbf{节点数} & \textbf{总容量} & \textbf{有效TPS} & \textbf{无效TPS} & \textbf{利用率} \\
\midrule
"""
    
    for method_name, stats in all_results.items():
        utilization = (stats['effective_tps'] / stats['total_partition_capacity']) * 100
        latex_code += f"{method_name} & {stats['num_partitions']} & {stats['num_nodes']} & "
        latex_code += f"{int(stats['total_partition_capacity']):,} & "
        latex_code += f"{int(stats['effective_tps']):,} & "
        latex_code += f"{int(stats['invalid_tps']):,} & "
        latex_code += f"{utilization:.2f}\\% \\\\\n"
    
    latex_code += r"""\bottomrule
\end{tabular}
\end{table}
"""
    
    # 保存到文件
    with open(f'{output_dir}/tps_comparison_table.tex', 'w', encoding='utf-8') as f:
        f.write(latex_code)
    
    print(f"  已保存LaTeX表格: {output_dir}/tps_comparison_table.tex")


def main():
    """主函数"""
    print("=" * 80)
    print("MEC拓扑系统 TPS 详细分析")
    print("=" * 80)
    
    # 初始化模拟器
    simulator = TPSSimulator(leader_max_tps=15000)
    
    # 定义分区方法
    methods = {
        'Global': '0:[1] 1:[1] 2:[1] 3:[1] 4:[1] 5:[1] 6:[1] 7:[1] 8:[1] 9:[1] 10:[1] 11:[1] 12:[1] 13:[1] 14:[1]',
        'Neighbor-based': '0:[1,2,3] 1:[4,5,6,7] 2:[1,2,4,6] 3:[5,6] 4:[8,9] 5:[5,7] 6:[2,3,9] 7:[1,2,4] 8:[7] 9:[1,4,5,6] 10:[8] 11:[3,8,9] 12:[1,4,5,6] 13:[4,5,6] 14:[1,4]',
        'Maximal Clique': '0:[1,2] 1:[3,4,5] 2:[1,6,7] 3:[3] 4:[8,9] 5:[5,10] 6:[2,11] 7:[1,6] 8:[10] 9:[3,4,7] 10:[8] 11:[9,11] 12:[4,6,7] 13:[3,4] 14:[6]',
        'Threshold': '0:[1] 1:[2,3] 2:[1,4] 3:[2] 4:[5] 5:[3] 6:[1] 7:[1,4] 8:[3] 9:[2,4] 10:[5] 11:[1,5] 12:[2,4] 13:[2] 14:[4]'
    }
    
    # 模拟所有方法
    all_results = {}
    for method_name, partition_str in methods.items():
        stats = simulator.simulate_method(method_name, partition_str, verbose=False)
        all_results[method_name] = stats
    
    # 打印简要结果
    print(f"\n{'='*80}")
    print("方法对比总结")
    print(f"{'='*80}")
    print(f"\n{'方法':<20} {'分区数':<10} {'有效TPS':<15} {'无效TPS':<15} {'利用率':<10}")
    print("-" * 80)
    
    for method_name, stats in all_results.items():
        utilization = (stats['effective_tps'] / stats['total_partition_capacity']) * 100
        print(f"{method_name:<20} {stats['num_partitions']:<10} "
              f"{stats['effective_tps']:>12,.0f}   "
              f"{stats['invalid_tps']:>12,.0f}   "
              f"{utilization:>8.2f}%")
    
    # 多分区节点分析
    analyze_multi_partition_nodes(all_results)
    
    # 生成可视化
    print(f"\n{'='*80}")
    print("生成可视化图表")
    print(f"{'='*80}\n")
    plot_tps_comparison(all_results)
    
    # 生成LaTeX表格
    print(f"\n{'='*80}")
    print("生成LaTeX表格")
    print(f"{'='*80}\n")
    generate_latex_table(all_results)
    
    print("\n" + "=" * 80)
    print("分析完成！")
    print("=" * 80)


if __name__ == '__main__':
    main()

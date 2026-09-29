"""
阈值分析模块 - 分析不同阈值下的分区性能
"""

import os
import numpy as np
import matplotlib.pyplot as plt
from typing import Dict, List
from ..visualization.plotter import COLORS


def calculate_composite_score(avg_instances: List[float], 
                              avg_size: List[float], 
                              alpha: float = 0.4, 
                              beta: float = 0.6) -> List[float]:
    """
    计算综合评分
    
    Parameters:
    -----------
    avg_instances : List[float]
        平均实例数列表
    avg_size : List[float]
        平均分区大小列表
    alpha : float
        实例数权重
    beta : float
        分区大小权重
        
    Returns:
    --------
    List[float] : 综合评分列表（越小越好）
    """
    norm_instances = np.array(avg_instances) / max(avg_instances)
    norm_size = np.array(avg_size) / max(avg_size)
    score = alpha * norm_instances + beta * norm_size
    return score.tolist()


class ThresholdAnalyzer:
    """阈值分析器 - 分析和可视化不同阈值下的性能"""
    
    def __init__(self, densities: List = None, 
                 figsize: tuple = (10, 7), 
                 dpi: int = 300):
        """
        Parameters:
        -----------
        densities : List
            要分析的密度列表（可以包含浮点数或字符串），默认为 [2e-6, 4e-6, 6e-6]
        figsize : tuple
            图表尺寸 (宽, 高)，单位为英寸，默认 (10, 7)
        dpi : int
            图片分辨率，默认 300（适合论文）
        """
        self.densities = densities or [2e-6, 4e-6, 6e-6]
        
        # 为每个密度分配颜色和标记
        default_colors = [COLORS[1], COLORS[6], COLORS[8], COLORS[3]]  # 添加第4个颜色用于Real
        default_markers = ['o', 's', '^', 'd']  # 添加第4个标记用于Real
        
        self.colors = {}
        self.markers = {}
        
        for i, density in enumerate(self.densities):
            if density == 2e-6:
                self.colors[density] = COLORS[1]
                self.markers[density] = 'o'
            elif density == 4e-6:
                self.colors[density] = COLORS[6]
                self.markers[density] = 's'
            elif density == 6e-6:
                self.colors[density] = COLORS[8]
                self.markers[density] = '^'
            elif density == 'Real' or isinstance(density, str):
                self.colors[density] = COLORS[3]  # 用于Real的颜色
                self.markers[density] = 'd'  # 菱形标记
            else:
                # 为其他密度动态分配
                self.colors[density] = default_colors[i % len(default_colors)]
                self.markers[density] = default_markers[i % len(default_markers)]
        
        self.figsize = figsize
        self.dpi = dpi
    
    def find_optimal_thresholds(self, data: Dict) -> Dict[float, int]:
        """找到每个密度的最优阈值"""
        optimal_thresholds = {}
        for density in self.densities:
            scores = calculate_composite_score(
                data[density]['avg_instances'],
                data[density]['avg_size']
            )
            optimal_idx = np.argmin(scores)
            optimal_thresholds[density] = data[density]['thresholds'][optimal_idx]
        return optimal_thresholds
    
    def _plot_single_metric(self, data: Dict, metric_key: str, 
                           ylabel: str, filename: str, 
                           optimal_thresholds: Dict, output_dir: str):
        """绘制单个指标的曲线图"""
        plt.figure(figsize=self.figsize)
        
        # 找出哪些密度在相同阈值处的Y值接近（会发生视觉重叠）
        threshold_groups = {}  # {阈值: [(密度, Y值)]}
        for density in self.densities:
            opt_th = optimal_thresholds[density]
            opt_idx = data[density]['thresholds'].index(opt_th)
            y_val = data[density][metric_key][opt_idx]
            
            if opt_th not in threshold_groups:
                threshold_groups[opt_th] = []
            threshold_groups[opt_th].append((density, y_val))
        
        for density in self.densities:
            # 处理字符串类型的密度标签（如"Real"）
            if isinstance(density, str):
                label = density
            else:
                label = f'λ={density:.0e}'.replace('-0', '-')
            plt.plot(data[density]['thresholds'], data[density][metric_key],
                    marker=self.markers[density], color=self.colors[density], 
                    linewidth=3.5, markersize=10, label=label)  # 加粗曲线
            
            # 标记最优阈值（仅在Y值接近导致视觉重叠时加偏移）
            opt_th = optimal_thresholds[density]
            opt_idx = data[density]['thresholds'].index(opt_th)
            y_val = data[density][metric_key][opt_idx]
            
            # 检查是否需要偏移：同一阈值处有多个密度，且Y值接近
            y_offset = 0
            if opt_th in threshold_groups and len(threshold_groups[opt_th]) > 1:
                # 获取同一阈值处所有密度的Y值
                y_values = [y for d, y in threshold_groups[opt_th]]
                y_range = max(y_values) - min(y_values)
                
                # 只有当Y值范围小于整体数据范围的15%时才认为会重叠
                all_y_values = data[density][metric_key]
                data_range = max(all_y_values) - min(all_y_values)
                
                if data_range > 0 and y_range / data_range < 0.15:
                    # Y值确实很接近，会发生重叠，添加偏移
                    # 使用数据范围的百分比作为偏移量，保证视觉一致性
                    densities_at_threshold = [d for d, y in threshold_groups[opt_th]]
                    idx_in_group = densities_at_threshold.index(density)
                    # 偏移量为数据范围的2%
                    offset_percent = [0.02, 0, -0.02]
                    y_offset = offset_percent[idx_in_group] * data_range if idx_in_group < len(offset_percent) else 0
            
            plt.scatter(opt_th, y_val + y_offset,
                       s=600, marker='*', color=self.colors[density], 
                       edgecolors='black', linewidths=2.5, zorder=10)
        
        plt.xlabel('Merge Threshold', fontsize=20, fontweight='bold')  # 增大字体
        plt.ylabel(ylabel, fontsize=20, fontweight='bold')  # 增大字体
        plt.legend(fontsize=16, loc='best')  # 增大字体
        plt.grid(True, alpha=0.7, linestyle='--', linewidth=0.8)  # 提高网格可见度
        plt.xticks(fontsize=18)  # 增大字体
        plt.yticks(fontsize=18)  # 增大字体
        plt.tight_layout()
        plt.savefig(f'{output_dir}/{filename}', dpi=self.dpi, bbox_inches='tight')
        pdf_name = os.path.splitext(filename)[0] + '.pdf'
        plt.savefig(f'{output_dir}/{pdf_name}', bbox_inches='tight')
        print(f"  已保存: {filename}")
        print(f"  已保存: {pdf_name}")
        plt.show()
    
    def plot_all_metrics(self, data: Dict, output_dir: str):
        """绘制所有性能指标曲线（7张图）"""
        os.makedirs(output_dir, exist_ok=True)
        
        # 找到最优阈值
        optimal_thresholds = self.find_optimal_thresholds(data)
        
        # 1. 平均实例数
        self._plot_single_metric(data, 'avg_instances', 
                                'Average Instances per Node',
                                'threshold_avg_instances.png',
                                optimal_thresholds, output_dir)
        
        # 2. 最大实例数
        self._plot_single_metric(data, 'max_instances',
                                'Max Instances per Node',
                                'threshold_max_instances.png',
                                optimal_thresholds, output_dir)
        
        # 3. 平均分区大小
        self._plot_single_metric(data, 'avg_size',
                                'Average Partition Size',
                                'threshold_avg_size.png',
                                optimal_thresholds, output_dir)
        
        # 4. 最大分区大小
        self._plot_single_metric(data, 'max_size',
                                'Max Partition Size',
                                'threshold_max_size.png',
                                optimal_thresholds, output_dir)
        
        # 5. 分区数量
        self._plot_single_metric(data, 'num_partitions',
                                'Number of Regions',
                                'threshold_num_partitions.png',
                                optimal_thresholds, output_dir)
        
        # 6. 双轴图
        self._plot_dual_axis(data, optimal_thresholds, output_dir)
        
        # 7. 综合评分
        self._plot_composite_score(data, optimal_thresholds, output_dir)
        
        # 打印最优阈值
        print("\n  最优阈值 (基于综合评分):")
        for density in self.densities:
            if isinstance(density, str):
                print(f"    {density}: τ={optimal_thresholds[density]}")
            else:
                print(f"    λ={density:.0e}: τ={optimal_thresholds[density]}")
    
    def _plot_dual_axis(self, data: Dict, optimal_thresholds: Dict, output_dir: str):
        """绘制双轴图：分区大小 vs 实例数"""
        fig, ax1 = plt.subplots(figsize=self.figsize)
        ax2 = ax1.twinx()
        
        # 收集同一阈值处的Y值
        threshold_size_groups = {}  # {阈值: [(密度, size值)]}
        threshold_inst_groups = {}  # {阈值: [(密度, instance值)]}
        for density in self.densities:
            opt_th = optimal_thresholds[density]
            opt_idx = data[density]['thresholds'].index(opt_th)
            
            if opt_th not in threshold_size_groups:
                threshold_size_groups[opt_th] = []
                threshold_inst_groups[opt_th] = []
            
            threshold_size_groups[opt_th].append((density, data[density]['avg_size'][opt_idx]))
            threshold_inst_groups[opt_th].append((density, data[density]['avg_instances'][opt_idx]))
        
        for density in self.densities:
            # 处理字符串类型的密度标签（如"Real"）
            if isinstance(density, str):
                label = density
            else:
                label = f'λ={density:.0e}'.replace('-0', '-')
            
            # 左轴：平均分区大小
            ax1.plot(data[density]['thresholds'], data[density]['avg_size'],
                    marker=self.markers[density], color=self.colors[density],
                    linewidth=3.5, markersize=10, linestyle='-',  # 加粗曲线
                    label=f'{label} (Size)')
            
            # 右轴：平均实例数
            ax2.plot(data[density]['thresholds'], data[density]['avg_instances'],
                    marker=self.markers[density], color=self.colors[density],
                    linewidth=3.5, markersize=10, linestyle='--', alpha=0.4,  # 加粗曲线
                    label=f'{label} (Instances)')
            
            # 标记最优阈值（仅在Y值接近导致视觉重叠时加偏移）
            opt_th = optimal_thresholds[density]
            opt_idx = data[density]['thresholds'].index(opt_th)
            
            # 检查size是否需要偏移
            size_offset = 0
            if opt_th in threshold_size_groups and len(threshold_size_groups[opt_th]) > 1:
                y_values = [y for d, y in threshold_size_groups[opt_th]]
                y_range = max(y_values) - min(y_values)
                data_range = max(data[density]['avg_size']) - min(data[density]['avg_size'])
                
                if data_range > 0 and y_range / data_range < 0.15:
                    densities_at_threshold = [d for d, y in threshold_size_groups[opt_th]]
                    idx_in_group = densities_at_threshold.index(density)
                    # 使用数据范围的2%作为偏移量
                    offset_percent = [0.02, 0, -0.02]
                    size_offset = offset_percent[idx_in_group] * data_range if idx_in_group < len(offset_percent) else 0
            
            # 检查instances是否需要偏移
            inst_offset = 0
            if opt_th in threshold_inst_groups and len(threshold_inst_groups[opt_th]) > 1:
                y_values = [y for d, y in threshold_inst_groups[opt_th]]
                y_range = max(y_values) - min(y_values)
                data_range = max(data[density]['avg_instances']) - min(data[density]['avg_instances'])
                
                if data_range > 0 and y_range / data_range < 0.15:
                    densities_at_threshold = [d for d, y in threshold_inst_groups[opt_th]]
                    idx_in_group = densities_at_threshold.index(density)
                    # 使用数据范围的2%作为偏移量
                    offset_percent = [0.02, 0, -0.02]
                    inst_offset = offset_percent[idx_in_group] * data_range if idx_in_group < len(offset_percent) else 0
            
            ax1.scatter(opt_th, data[density]['avg_size'][opt_idx] + size_offset,
                       s=600, marker='*', color=self.colors[density],
                       edgecolors='black', linewidths=2.5, zorder=10)
            ax2.scatter(opt_th, data[density]['avg_instances'][opt_idx] + inst_offset,
                       s=600, marker='*', color=self.colors[density],
                       edgecolors='black', linewidths=2.5, zorder=10)
        
        ax1.set_xlabel('Merge Threshold', fontsize=20, fontweight='bold')  # 增大字体
        ax1.set_ylabel('Average Partition Size', fontsize=20, fontweight='bold')  # 增大字体
        ax2.set_ylabel('Average Instances per Node', fontsize=20, fontweight='bold')  # 增大字体
        
        lines1, labels1 = ax1.get_legend_handles_labels()
        lines2, labels2 = ax2.get_legend_handles_labels()
        ax1.legend(lines1 + lines2, labels1 + labels2, fontsize=18, loc='best')  # 增大字体
        
        ax1.grid(True, alpha=0.7, linestyle='--', linewidth=0.8)  # 提高网格可见度
        ax1.tick_params(axis='both', labelsize=18)  # 增大字体
        ax2.tick_params(axis='y', labelsize=18)  # 增大字体
        plt.tight_layout()
        plt.savefig(f'{output_dir}/threshold_dual_axis.png', dpi=self.dpi, bbox_inches='tight')
        plt.savefig(f'{output_dir}/threshold_dual_axis.pdf', bbox_inches='tight')
        print(f"  已保存: threshold_dual_axis.png")
        print(f"  已保存: threshold_dual_axis.pdf")
        plt.show()
    
    def _plot_composite_score(self, data: Dict, optimal_thresholds: Dict, output_dir: str):
        """绘制综合评分曲线"""
        plt.figure(figsize=self.figsize)
        
        # 收集同一阈值处的评分值
        threshold_score_groups = {}  # {阈值: [(密度, score值)]}
        all_scores_by_density = {}  # 保存每个密度的所有评分
        
        for density in self.densities:
            opt_th = optimal_thresholds[density]
            opt_idx = data[density]['thresholds'].index(opt_th)
            scores = calculate_composite_score(
                data[density]['avg_instances'],
                data[density]['avg_size']
            )
            all_scores_by_density[density] = scores
            
            if opt_th not in threshold_score_groups:
                threshold_score_groups[opt_th] = []
            threshold_score_groups[opt_th].append((density, scores[opt_idx]))
        
        for density in self.densities:
            # 处理字符串类型的密度标签（如"Real"）
            if isinstance(density, str):
                label = density
            else:
                label = f'λ={density:.0e}'.replace('-0', '-')
            scores = all_scores_by_density[density]
            
            plt.plot(data[density]['thresholds'], scores,
                    marker=self.markers[density], color=self.colors[density],
                    linewidth=3.5, markersize=10, label=label)  # 加粗曲线
            
            # 标记最优阈值（仅在Y值接近导致视觉重叠时加偏移）
            opt_th = optimal_thresholds[density]
            opt_idx = data[density]['thresholds'].index(opt_th)
            
            # 检查是否需要偏移
            y_offset = 0
            if opt_th in threshold_score_groups and len(threshold_score_groups[opt_th]) > 1:
                y_values = [y for d, y in threshold_score_groups[opt_th]]
                y_range = max(y_values) - min(y_values)
                data_range = max(scores) - min(scores)
                
                if data_range > 0 and y_range / data_range < 0.15:
                    densities_at_threshold = [d for d, y in threshold_score_groups[opt_th]]
                    idx_in_group = densities_at_threshold.index(density)
                    # 使用数据范围的2%作为偏移量
                    offset_percent = [0.02, 0, -0.02]
                    y_offset = offset_percent[idx_in_group] * data_range if idx_in_group < len(offset_percent) else 0
            
            plt.scatter(opt_th, scores[opt_idx] + y_offset,
                       s=600, marker='*', color=self.colors[density],
                       edgecolors='black', linewidths=2.5, zorder=10)
        
        plt.xlabel('Merge Threshold', fontsize=20, fontweight='bold')  # 增大字体
        plt.ylabel('Composite Score', fontsize=20, fontweight='bold')  # 增大字体
        # plt.title('Composite Score (α=0.4, β=0.6)', fontsize=20, fontweight='bold')  # 论文图不需要title
        plt.legend(fontsize=18, loc='best')  # 增大字体
        plt.grid(True, alpha=0.7, linestyle='--', linewidth=0.8)  # 提高网格可见度
        plt.xticks(fontsize=18)  # 增大字体
        plt.yticks(fontsize=18)  # 增大字体
        plt.tight_layout()
        plt.savefig(f'{output_dir}/threshold_composite_score.png', dpi=self.dpi, bbox_inches='tight')
        plt.savefig(f'{output_dir}/threshold_composite_score.pdf', bbox_inches='tight')
        print(f"  已保存: threshold_composite_score.png")
        print(f"  已保存: threshold_composite_score.pdf")
        plt.show()


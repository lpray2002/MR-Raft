"""
分区优化器
用于寻找最优的分区参数
"""

import networkx as nx
import numpy as np
from typing import List, Set, Dict, Tuple, Optional
from .strategies import PartitionStrategies


class PartitionOptimizer:
    """分区优化器"""
    
    def __init__(self, graph: nx.Graph):
        """
        初始化优化器
        
        Parameters:
        -----------
        graph : nx.Graph
            MEC拓扑图
        """
        self.graph = graph
        self.strategies = PartitionStrategies(graph)
    
    def find_optimal_threshold(self, 
                              threshold_range: List[int] = None,
                              alpha: float = 0.4,
                              beta: float = 0.6,
                              normalization: str = 'max',
                              verbose: bool = True) -> Tuple[int, float, List[Set[int]]]:
        """
        寻找最优的合并阈值
        
        Parameters:
        -----------
        threshold_range : List[int], optional
            要测试的阈值范围，默认 range(2, 16) = [2, 3, ..., 15]
        alpha : float
            实例数权重
        beta : float
            分区大小权重
        normalization : str
            归一化方法，可选 'max'（最大值归一化，原始方法）或
            'minmax'（min-max 缩放，将各指标映射到 [0, 1]）
        verbose : bool
            是否打印详细信息
        
        Returns:
        --------
        Tuple[int, float, List[Set[int]]] : (最优阈值, 最优分数, 最优分区)
        """
        if threshold_range is None:
            threshold_range = list(range(2, 16))  # 默认使用连续范围 [2, 3, 4, ..., 15]
        
        best_threshold = threshold_range[0]
        best_score = float('inf')
        best_partitions = []
        
        # 首先收集所有阈值下的统计数据，用于归一化
        all_stats = []
        all_partitions = []
        for threshold in threshold_range:
            partitions = self.strategies.threshold_based(merge_threshold=threshold)
            stats = self.strategies.get_partition_statistics(partitions)
            all_stats.append(stats)
            all_partitions.append(partitions)
        
        # 提取用于归一化的统计量
        avg_instances_list = [s['avg_instances'] for s in all_stats]
        avg_size_list = [s['avg_size'] for s in all_stats]
        # 最大值归一化（原始方法）所需的最大值
        max_instances = max(avg_instances_list) if max(avg_instances_list) > 0 else 1
        max_size = max(avg_size_list) if max(avg_size_list) > 0 else 1
        # min-max 缩放所需的最小值与极差
        min_instances = min(avg_instances_list)
        min_size = min(avg_size_list)
        range_instances = (max(avg_instances_list) - min_instances) or 1
        range_size = (max(avg_size_list) - min_size) or 1
        
        if verbose:
            print(f"{'阈值':<6} {'分区数':<8} {'平均大小':<10} {'最大大小':<10} "
                  f"{'平均实例':<10} {'最大实例':<10} {'评分':<10}")
            print("=" * 70)
        
        # 使用归一化后的评分
        for i, threshold in enumerate(threshold_range):
            stats = all_stats[i]
            partitions = all_partitions[i]
            
            # 根据所选归一化方法计算归一化指标
            if normalization == 'minmax':
                # min-max 缩放：将各指标分别映射到 [0, 1]
                norm_instances = (stats['avg_instances'] - min_instances) / range_instances
                norm_size = (stats['avg_size'] - min_size) / range_size
            else:
                # 最大值归一化（原始方法）
                norm_instances = stats['avg_instances'] / max_instances
                norm_size = stats['avg_size'] / max_size
            score = alpha * norm_instances + beta * norm_size
            
            if verbose:
                print(f"{threshold:<6} {stats['num_partitions']:<8} "
                      f"{stats['avg_size']:<10.2f} {stats['max_size']:<10} "
                      f"{stats['avg_instances']:<10.2f} {stats['max_instances']:<10} "
                      f"{score:<10.4f}")
            
            # 更新最优值
            if score < best_score:
                best_score = score
                best_threshold = threshold
                best_partitions = partitions
        
        if verbose:
            print("=" * 70)
            print(f"最优阈值: {best_threshold}, 评分: {best_score:.4f}")
        
        return best_threshold, best_score, best_partitions
    
    def compare_all_strategies(self, 
                              optimal_threshold: Optional[int] = None,
                              verbose: bool = True) -> Dict[str, Dict]:
        """
        比较所有策略的性能
        
        Parameters:
        -----------
        optimal_threshold : int, optional
            阈值策略使用的阈值，如果为None则自动寻找
        verbose : bool
            是否打印详细信息
        
        Returns:
        --------
        Dict[str, Dict] : 每个策略的统计信息
        """
        results = {}
        
        # 如果没有提供最优阈值，自动寻找
        if optimal_threshold is None:
            optimal_threshold, _, _ = self.find_optimal_threshold(verbose=False)
        
        # 定义策略
        strategy_configs = {
            'Maximal Clique': self.strategies.maximal_clique(),
            f'Threshold-based (τ={optimal_threshold})': 
                self.strategies.threshold_based(merge_threshold=optimal_threshold),
            'Neighbor-based': self.strategies.neighbor_based(),
            'Global': self.strategies.global_partition()
        }
        
        if verbose:
            print("\n" + "=" * 100)
            print("策略对比")
            print("=" * 100)
            print(f"{'策略':<30} {'分区数':<8} {'平均大小':<10} {'最大大小':<10} "
                  f"{'平均实例':<12} {'最大实例':<10}")
            print("-" * 100)
        
        for strategy_name, partitions in strategy_configs.items():
            stats = self.strategies.get_partition_statistics(partitions)
            results[strategy_name] = {
                'partitions': partitions,
                'stats': stats
            }
            
            if verbose:
                print(f"{strategy_name:<30} {stats['num_partitions']:<8} "
                      f"{stats['avg_size']:<10.2f} {stats['max_size']:<10} "
                      f"{stats['avg_instances']:<12.2f} {stats['max_instances']:<10}")
        
        if verbose:
            print("=" * 100)
        
        return results
    
    def analyze_threshold_sensitivity(self, threshold_range: List[int] = None) -> Dict:
        """分析阈值敏感性"""
        if threshold_range is None:
            threshold_range = list(range(2, 20))
        
        analysis = {
            'thresholds': [], 'num_partitions': [], 'avg_size': [], 
            'max_size': [], 'avg_instances': [], 'max_instances': [], 'scores': []
        }
        
        for threshold in threshold_range:
            partitions = self.strategies.threshold_based(merge_threshold=threshold)
            stats = self.strategies.get_partition_statistics(partitions)
            score = self.strategies.evaluate_partitions(partitions)
            
            analysis['thresholds'].append(threshold)
            analysis['num_partitions'].append(stats['num_partitions'])
            analysis['avg_size'].append(stats['avg_size'])
            analysis['max_size'].append(stats['max_size'])
            analysis['avg_instances'].append(stats['avg_instances'])
            analysis['max_instances'].append(stats['max_instances'])
            analysis['scores'].append(score)
        
        return analysis


if __name__ == '__main__':
    G = nx.karate_club_graph()
    optimizer = PartitionOptimizer(G)
    optimal_threshold, _, _ = optimizer.find_optimal_threshold(verbose=True)
    print(f"\n最优阈值: {optimal_threshold}")


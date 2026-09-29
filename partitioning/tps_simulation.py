"""
TPS模拟计算脚本
模拟计算整个拓扑系统的总TPS，考虑分区leader处理能力和节点多分区复制开销
"""

from typing import Dict, List, Set
from collections import defaultdict


class TPSSimulator:
    """TPS模拟器"""
    
    def __init__(self, leader_max_tps: int = 15000):
        """
        初始化TPS模拟器
        
        Parameters:
        -----------
        leader_max_tps : int
            每个分区leader的最大TPS处理能力
        """
        self.leader_max_tps = leader_max_tps
    
    def parse_partitions(self, partition_str: str) -> Dict[int, List[int]]:
        """
        解析分区字符串
        
        Parameters:
        -----------
        partition_str : str
            分区字符串，格式如 "0:[1,2,3] 1:[4,5,6,7]"
        
        Returns:
        --------
        Dict[int, List[int]] : 节点ID -> 所属分区列表
        """
        node_to_partitions = {}
        
        # 分割每个节点的分区信息
        parts = partition_str.split()
        for part in parts:
            if ':' not in part:
                continue
            node_str, partition_str = part.split(':')
            node_id = int(node_str)
            # 解析分区列表 [1,2,3]
            partition_list = partition_str.strip('[]').split(',')
            partitions = [int(p) for p in partition_list if p.strip()]
            node_to_partitions[node_id] = partitions
        
        return node_to_partitions
    
    def build_partition_to_nodes(self, node_to_partitions: Dict[int, List[int]]) -> Dict[int, Set[int]]:
        """
        构建分区到节点的映射
        
        Parameters:
        -----------
        node_to_partitions : Dict[int, List[int]]
            节点ID -> 所属分区列表
        
        Returns:
        --------
        Dict[int, Set[int]] : 分区ID -> 节点集合
        """
        partition_to_nodes = defaultdict(set)
        
        for node_id, partitions in node_to_partitions.items():
            for partition_id in partitions:
                partition_to_nodes[partition_id].add(node_id)
        
        return dict(partition_to_nodes)
    
    def calculate_node_tps(self, node_to_partitions: Dict[int, List[int]], 
                          partition_to_nodes: Dict[int, Set[int]]) -> Dict[int, float]:
        """
        计算每个节点的TPS处理能力
        
        考虑最优leader分布情况：
        - 每个分区的leader在不同节点上
        - 节点的TPS取决于其所属分区中最小的平均TPS
        
        Parameters:
        -----------
        node_to_partitions : Dict[int, List[int]]
            节点ID -> 所属分区列表
        partition_to_nodes : Dict[int, Set[int]]
            分区ID -> 节点集合
        
        Returns:
        --------
        Dict[int, float] : 节点ID -> 该节点的TPS处理能力
        """
        node_tps = {}
        
        for node_id, partitions in node_to_partitions.items():
            # 计算该节点在每个分区中的平均TPS
            partition_avg_tps = []
            for partition_id in partitions:
                partition_size = len(partition_to_nodes[partition_id])
                avg_tps = self.leader_max_tps / partition_size
                partition_avg_tps.append(avg_tps)
            
            # 节点的TPS取最小值（瓶颈）
            node_tps[node_id] = min(partition_avg_tps) if partition_avg_tps else 0
        
        return node_tps
    
    def calculate_system_tps(self, node_to_partitions: Dict[int, List[int]], 
                            partition_to_nodes: Dict[int, Set[int]],
                            verbose: bool = True) -> Dict[str, float]:
        """
        计算系统总TPS
        
        系统总TPS = Σ(每个分区的leader_max_tps) - 无效TPS
        无效TPS = 由于节点属于多个分区导致的重复计算
        
        Parameters:
        -----------
        node_to_partitions : Dict[int, List[int]]
            节点ID -> 所属分区列表
        partition_to_nodes : Dict[int, Set[int]]
            分区ID -> 节点集合
        verbose : bool
            是否打印详细信息
        
        Returns:
        --------
        Dict[str, float] : 包含各项TPS统计信息
        """
        # 计算每个节点的TPS
        node_tps = self.calculate_node_tps(node_to_partitions, partition_to_nodes)
        
        # 计算总的分区容量
        num_partitions = len(partition_to_nodes)
        total_partition_capacity = num_partitions * self.leader_max_tps
        
        # 计算无效TPS
        # 对于每个节点，如果它属于k个分区，那么它的请求会被复制k次
        # 但实际上只有1次是有效的，其余k-1次是无效的
        invalid_tps = 0
        for node_id, partitions in node_to_partitions.items():
            num_partitions_per_node = len(partitions)
            if num_partitions_per_node > 1:
                # 该节点的无效TPS = 节点TPS * (分区数 - 1)
                invalid_tps += node_tps[node_id] * (num_partitions_per_node - 1)
        
        # 系统有效TPS
        effective_tps = total_partition_capacity - invalid_tps
        
        # 统计信息
        stats = {
            'total_partition_capacity': total_partition_capacity,
            'invalid_tps': invalid_tps,
            'effective_tps': effective_tps,
            'num_partitions': num_partitions,
            'num_nodes': len(node_to_partitions),
            'avg_node_tps': sum(node_tps.values()) / len(node_tps) if node_tps else 0,
            'min_node_tps': min(node_tps.values()) if node_tps else 0,
            'max_node_tps': max(node_tps.values()) if node_tps else 0
        }
        
        if verbose:
            print(f"  分区数: {stats['num_partitions']}")
            print(f"  节点数: {stats['num_nodes']}")
            print(f"  总分区容量: {stats['total_partition_capacity']:,.0f} TPS")
            print(f"  无效TPS: {stats['invalid_tps']:,.2f} TPS")
            print(f"  系统有效TPS: {stats['effective_tps']:,.2f} TPS")
            print(f"  平均节点TPS: {stats['avg_node_tps']:,.2f} TPS")
            print(f"  节点TPS范围: [{stats['min_node_tps']:,.2f}, {stats['max_node_tps']:,.2f}]")
            
            # 打印每个节点的详细信息
            print(f"\n  节点详细信息:")
            for node_id in sorted(node_to_partitions.keys()):
                partitions = node_to_partitions[node_id]
                tps = node_tps[node_id]
                print(f"    节点 {node_id}: 分区{partitions} -> TPS={tps:,.2f}")
        
        return stats
    
    def generate_client_concurrency_configs(self, partition_str: str, 
                                           max_concurrency: int = 60,
                                           step: int = 2) -> List[str]:
        """
        根据节点有效流量比例生成客户端并发配置
        
        有效流量 = 节点TPS容量（只计算一次，不重复计算多分区的流量）
        
        Parameters:
        -----------
        partition_str : str
            分区字符串
        max_concurrency : int
            最大并发量
        step : int
            并发量增长步长
        
        Returns:
        --------
        List[str] : 客户端并发配置列表，格式如 "0:1,1:1,2:1,..."
        """
        # 解析分区
        node_to_partitions = self.parse_partitions(partition_str)
        partition_to_nodes = self.build_partition_to_nodes(node_to_partitions)
        
        # 计算每个节点的TPS容量
        node_tps = self.calculate_node_tps(node_to_partitions, partition_to_nodes)
        
        # 计算每个节点的有效流量（不重复计算多分区流量）
        # 有效流量 = 节点TPS容量（因为每个节点的请求只计算一次有效流量）
        node_effective_traffic = {}
        for node_id, tps in node_tps.items():
            # 节点的有效流量就是其TPS容量
            # 即使节点属于多个分区，客户端发送的流量也只计算一次
            node_effective_traffic[node_id] = tps
        
        # 找到最小有效流量作为基准
        min_traffic = min(node_effective_traffic.values())
        
        # 计算每个节点相对于最小有效流量的比例
        node_ratios = {node_id: traffic / min_traffic 
                      for node_id, traffic in node_effective_traffic.items()}
        
        # 生成不同并发量级别的配置
        configs = []
        for base_concurrency in range(1, max_concurrency + 1, step):
            # 按有效流量比例分配并发量
            node_concurrency = {}
            for node_id in sorted(node_to_partitions.keys()):
                # 根据比例计算并发量，向下取整
                concurrency = int(base_concurrency * node_ratios[node_id])
                # 至少为1
                node_concurrency[node_id] = max(1, concurrency)
            
            # 格式化为字符串
            config_str = ','.join([f"{node_id}:{conc}" 
                                   for node_id, conc in sorted(node_concurrency.items())])
            configs.append(config_str)
        
        return configs
    
    def simulate_method(self, method_name: str, partition_str: str, verbose: bool = True):
        """
        模拟某个分区方法的TPS
        
        Parameters:
        -----------
        method_name : str
            方法名称
        partition_str : str
            分区字符串
        verbose : bool
            是否打印详细信息
        
        Returns:
        --------
        Dict[str, float] : TPS统计信息
        """
        if verbose:
            print(f"\n{'='*80}")
            print(f"{method_name} 方法 TPS模拟")
            print(f"{'='*80}")
        
        # 解析分区
        node_to_partitions = self.parse_partitions(partition_str)
        partition_to_nodes = self.build_partition_to_nodes(node_to_partitions)
        
        # 计算TPS
        stats = self.calculate_system_tps(node_to_partitions, partition_to_nodes, verbose)
        
        return stats


def main():
    """主函数"""
    print("=" * 80)
    print("MEC拓扑系统 TPS 模拟")
    print("=" * 80)
    print(f"\n配置:")
    print(f"  每个分区Leader最大TPS: 8,000")
    print(f"  节点数: 15 (0-14)")
    
    # 初始化模拟器
    simulator = TPSSimulator(leader_max_tps=9000)
    
    # 定义三种分区方法
    methods = {
        'Global': '0:[1] 1:[1] 2:[1] 3:[1] 4:[1] 5:[1] 6:[1] 7:[1] 8:[1] 9:[1] 10:[1] 11:[1] 12:[1] 13:[1] 14:[1]',
        
        'Neighbor-based': '0:[1,2,3] 1:[4,5,6,7] 2:[1,2,4,6] 3:[5,6] 4:[8,9] 5:[5,7] 6:[2,3,9] 7:[1,2,4] 8:[7] 9:[1,4,5,6] 10:[8] 11:[3,8,9] 12:[1,4,5,6] 13:[4,5,6] 14:[1,4]',
        
        'Maximal Clique': '0:[1,2] 1:[3,4,5] 2:[1,6,7] 3:[3] 4:[8,9] 5:[5,10] 6:[2,11] 7:[1,6] 8:[10] 9:[3,4,7] 10:[8] 11:[9,11] 12:[4,6,7] 13:[3,4] 14:[6]',
        
        'Threshold': '0:[1] 1:[2,3] 2:[1,4] 3:[2] 4:[5] 5:[3] 6:[1] 7:[1,4] 8:[3] 9:[2,4] 10:[5] 11:[1,5] 12:[2,4] 13:[2] 14:[4]'
    }
    
    # 存储所有方法的结果
    all_results = {}
    
    # 模拟每种方法
    for method_name, partition_str in methods.items():
        stats = simulator.simulate_method(method_name, partition_str, verbose=True)
        all_results[method_name] = stats
    
    # 对比总结
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
    
    # 找出最优方法
    best_method = max(all_results.items(), key=lambda x: x[1]['effective_tps'])
    print(f"\n最优方法: {best_method[0]} (有效TPS: {best_method[1]['effective_tps']:,.0f})")
    
    # 生成客户端并发配置
    print("\n" + "=" * 80)
    print("客户端并发配置生成")
    print("=" * 80)
    print(f"\n配置说明:")
    print(f"  - 并发量范围: 1 到 60，步长为 2")
    print(f"  - 节点并发量按有效流量比例分配")
    print(f"  - 有效流量 = 节点TPS容量（不重复计算多分区流量）")
    print(f"  - 格式: 节点ID:并发量")
    
    for method_name, partition_str in methods.items():
        print(f"\n{method_name} 方法:")
        print("-" * 80)
        
        # 生成并发配置
        configs = simulator.generate_client_concurrency_configs(partition_str, max_concurrency=60, step=2)
        
        # 打印前5个和后5个配置作为示例
        print(f"\n  前5个配置:")
        for i, config in enumerate(configs[:5], 1):
            print(f"    [{i}] {config}")
        
        if len(configs) > 10:
            print(f"\n  ... (共 {len(configs)} 个配置) ...")
            
            print(f"\n  后5个配置:")
            for i, config in enumerate(configs[-5:], len(configs)-4):
                print(f"    [{i}] {config}")
        
        # 保存到文件
        output_file = f'output/client_configs/{method_name.lower().replace(" ", "_").replace("(", "").replace(")", "")}_client_configs.txt'
        import os
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        with open(output_file, 'w') as f:
            for config in configs:
                f.write(config + '\n')
        print(f"\n  已保存完整配置到: {output_file}")
    
    print("\n" + "=" * 80)
    print("模拟完成！")
    print("=" * 80)


if __name__ == '__main__':
    main()

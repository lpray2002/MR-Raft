"""
Raft 选举超时热力图对比
Random vs Priority-based Election Timeout Visualization

对比随机选举和基于优先级选举的超时时间分布
"""

import os
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns
from collections import defaultdict
import sys

# 导入项目配置
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.visualization.plotter import COLORS

# ============================================
# 配置参数（与run.py保持一致的风格）
# ============================================

# 仿真参数
NUM_NODES = 5              # 节点数量
NUM_ROUNDS = 10            # 选举轮数
NUM_REGIONS = 10            # Region数量

# Raft超时参数
T_BASE = 150               # 基础超时时间 (ms)
T_RANGE_RANDOM = 150       # Random选举的超时范围 (ms)
ALPHA = 0.3               # 优先级惩罚因子（指数增长）
DELTA_RANGE = 30           # 随机扰动范围 (ms)

# 可视化参数（与run.py保持一致）
FIGURE_SIZE_HEATMAP = (16, 8)  # 热力图尺寸
DPI = 300                      # 图片分辨率
OUTPUT_DIR = 'output/election'  # 输出目录

# 绘图风格配置（与run.py保持一致）
CMAP_RANDOM = 'Blues'          # Random选举热力图配色（蓝色系）
CMAP_PRIORITY = 'Reds'         # Priority选举热力图配色（红色系，与COLORS[9]一致）


class RaftElectionSimulator:
    """Raft选举模拟器"""
    
    def __init__(self, num_nodes=5, num_regions=3, 
                 T_base=150, alpha=0.3, delta_range=30):
        """
        初始化Raft选举模拟器
        
        Parameters:
        -----------
        num_nodes : int
            节点总数
        num_regions : int
            Region数量
        T_base : float
            基础超时时间 (ms)
        alpha : float
            优先级惩罚因子
        delta_range : float
            随机扰动范围 (ms)
        """
        self.num_nodes = num_nodes
        self.num_regions = num_regions
        self.T_base = T_base
        self.alpha = alpha
        self.delta_range = delta_range
        
        # 生成node-region membership
        self.node_regions = self._generate_membership()
        self.region_members = self._compute_region_members()
        
        print(f"[Config] Nodes={num_nodes}, Regions={num_regions}, "
              f"T_base={T_base}ms, α={alpha}")
        print(f"[Membership] Node-Region assignment:")
        for node_id, regions in self.node_regions.items():
            print(f"  Node {node_id}: Regions {regions}")
    
    def _generate_membership(self):
        """
        生成node-region membership
        每个节点都分配到所有 region（全局复制）
        """
        membership = {}
        all_regions = list(range(self.num_regions))
        
        # 每个节点都包含所有 region
        for node_id in range(self.num_nodes):
            membership[node_id] = all_regions.copy()
        
        return membership
    
    def _compute_region_members(self):
        """计算每个region的成员列表"""
        region_members = {r: [] for r in range(self.num_regions)}
        for node_id, regions in self.node_regions.items():
            for region_id in regions:
                region_members[region_id].append(node_id)
        return region_members
    
    def compute_timeout_random(self, node_id, round_idx):
        """
        计算Random选举的超时时间
        
        T_random = T_base + random(0, T_range)
        
        Parameters:
        -----------
        node_id : int
            节点ID
        round_idx : int
            选举轮次
        
        Returns:
        --------
        float : timeout (ms)
        """
        np.random.seed(node_id * 1000 + round_idx)  # 确保可重复性
        timeout = self.T_base + np.random.uniform(0, T_RANGE_RANDOM)
        return timeout
    
    def compute_timeout_priority(self, node_id, round_idx, leader_count):
        """
        计算Priority-based选举的超时时间（指数惩罚）
        
        T_j^(r) = T_base + (T_max - T_base) × [1 - (1/(1+α))^L_j] + δ
        
        其中:
        - L_j: 当前节点已担任的leader数量
        - α: 惩罚因子
        - T_max: 最大超时时间 = T_base × 3
        - δ: 随机扰动
        
        随着leader数量增加，超时时间从T_base逐渐增长到T_max
        
        Parameters:
        -----------
        node_id : int
            节点ID
        round_idx : int
            选举轮次
        leader_count : int
            当前节点已担任的leader数量
        
        Returns:
        --------
        float : timeout (ms)
        """
        np.random.seed(node_id * 1000 + round_idx)  # 确保可重复性
        
        T_max = self.T_base * 2  # 设置最大timeout为base的3倍
        decay_factor = (1.0 / (1 + self.alpha)) ** leader_count
        delta = np.random.uniform(0, self.delta_range)
        
        timeout = self.T_base + (T_max - self.T_base) * (1 - decay_factor) + delta
        
        return timeout
    
    def simulate_random_election(self, num_rounds):
        """
        模拟Random选举策略
        
        Parameters:
        -----------
        num_rounds : int
            选举轮数
        
        Returns:
        --------
        timeout_matrix : ndarray (num_rounds, num_nodes)
            每轮每个节点的超时时间
        leader_history : list
            每轮选出的leader列表
        """
        timeout_matrix = np.zeros((num_rounds, self.num_nodes))
        leader_history = []
        
        for round_idx in range(num_rounds):
            # 计算每个节点的超时时间
            for node_id in range(self.num_nodes):
                timeout_matrix[round_idx, node_id] = \
                    self.compute_timeout_random(node_id, round_idx)
            
            # 选择超时最短的节点作为leader
            leader = np.argmin(timeout_matrix[round_idx])
            leader_history.append(leader)
        
        return timeout_matrix, leader_history
    
    def simulate_priority_election(self, num_rounds):
        """
        模拟Priority-based选举策略
        
        Parameters:
        -----------
        num_rounds : int
            选举轮数
        
        Returns:
        --------
        timeout_matrix : ndarray (num_rounds, num_nodes)
            每轮每个节点的超时时间
        leader_history : list
            每轮选出的leader列表
        """
        timeout_matrix = np.zeros((num_rounds, self.num_nodes))
        leader_history = []
        leader_counts = np.zeros(self.num_nodes, dtype=int)
        
        for round_idx in range(num_rounds):
            # 计算每个节点的超时时间（基于当前leader数量）
            for node_id in range(self.num_nodes):
                timeout_matrix[round_idx, node_id] = \
                    self.compute_timeout_priority(
                        node_id, round_idx, leader_counts[node_id])
            
            # 选择超时最短的节点作为leader
            leader = np.argmin(timeout_matrix[round_idx])
            leader_history.append(leader)
            leader_counts[leader] += 1
        
        return timeout_matrix, leader_history


def plot_timeout_heatmaps(random_timeouts, priority_timeouts, 
                          random_leaders, priority_leaders,
                          output_dir='output/election'):
    """
    绘制超时时间热力图（分别输出单图）
    
    Parameters:
    -----------
    random_timeouts : ndarray
        Random选举的超时矩阵
    priority_timeouts : ndarray
        Priority选举的超时矩阵
    random_leaders : list
        Random选举的leader历史
    priority_leaders : list
        Priority选举的leader历史
    output_dir : str
        输出目录
    """
    os.makedirs(output_dir, exist_ok=True)
    
    num_rounds, num_nodes = random_timeouts.shape
    
    # === 单图1: Random Timeouts ===
    fig, ax = plt.subplots(figsize=(10, 8), dpi=DPI)
    
    im1 = ax.imshow(random_timeouts.T, aspect='auto', 
                    cmap=CMAP_RANDOM, interpolation='nearest',
                    vmin=T_BASE, vmax=T_BASE + T_RANGE_RANDOM)
    
    ax.set_xlabel('Election Round', fontsize=24, fontweight='bold')
    ax.set_ylabel('Node ID', fontsize=24, fontweight='bold')
    # 删除title以适应论文风格
    # ax.set_title('Random Timeouts per Node per Round', 
    #              fontsize=18, fontweight='bold', pad=15)
    
    # 设置刻度
    ax.set_yticks(range(num_nodes))
    ax.set_yticklabels(range(num_nodes), fontsize=24)
    ax.tick_params(axis='x', labelsize=24)
    ax.tick_params(axis='y', labelsize=24)
    
    # 添加colorbar
    cbar1 = plt.colorbar(im1, ax=ax, fraction=0.046, pad=0.04)
    cbar1.set_label('Timeout (ms)', fontsize=24, fontweight='bold')
    cbar1.ax.tick_params(labelsize=24)
    
    plt.tight_layout()
    output_path1 = os.path.join(output_dir, 'timeout_random.png')
    plt.savefig(output_path1, dpi=300, bbox_inches='tight')
    print(f"[OK] Random热力图已保存: {output_path1}")
    plt.close()
    
    # === 单图2: Priority-based Timeouts ===
    fig, ax = plt.subplots(figsize=(10, 8), dpi=DPI)
    
    # Priority-based的最大值为 T_base * 3 + delta_range
    priority_vmax = T_BASE * 2 + DELTA_RANGE
    im2 = ax.imshow(priority_timeouts.T, aspect='auto', 
                    cmap=CMAP_PRIORITY, interpolation='nearest',
                    vmin=T_BASE, vmax=priority_vmax)
    
    ax.set_xlabel('Election Round', fontsize=24, fontweight='bold')
    ax.set_ylabel('Node ID', fontsize=24, fontweight='bold')
    # 删除title以适应论文风格
    # ax.set_title('Priority-based Timeouts per Node per Round', 
    #              fontsize=18, fontweight='bold', pad=15)
    
    # 设置刻度
    ax.set_yticks(range(num_nodes))
    ax.set_yticklabels(range(num_nodes), fontsize=24)
    ax.tick_params(axis='x', labelsize=24)
    ax.tick_params(axis='y', labelsize=24)
    
    # 添加colorbar
    cbar2 = plt.colorbar(im2, ax=ax, fraction=0.046, pad=0.04)
    cbar2.set_label('Timeout (ms)', fontsize=24, fontweight='bold')
    cbar2.ax.tick_params(labelsize=24)
    
    plt.tight_layout()
    output_path2 = os.path.join(output_dir, 'timeout_priority.png')
    plt.savefig(output_path2, dpi=300, bbox_inches='tight')
    print(f"[OK] Priority热力图已保存: {output_path2}")
    plt.close()


def plot_leader_distribution(random_leaders, priority_leaders, 
                             num_nodes, output_dir='output/election'):
    """
    绘制leader分布对比图（单图，无标题，论文风格）
    
    Parameters:
    -----------
    random_leaders : list
        Random选举的leader历史
    priority_leaders : list
        Priority选举的leader历史
    num_nodes : int
        节点数量
    output_dir : str
        输出目录
    """
    os.makedirs(output_dir, exist_ok=True)
    
    # 统计每个节点当选leader的次数
    random_counts = np.bincount(random_leaders, minlength=num_nodes)
    priority_counts = np.bincount(priority_leaders, minlength=num_nodes)
    
    # 创建图形（与run.py风格一致）
    fig, ax = plt.subplots(figsize=(10, 8), dpi=DPI)
    
    x = np.arange(num_nodes)
    width = 0.35
    
    # 绘制柱状图（使用与run.py一致的颜色）
    # Random使用蓝色系 COLORS[1]，Priority使用红色系 COLORS[9]
    bars1 = ax.bar(x - width/2, random_counts, width, 
                   label='Random Election', 
                   alpha=0.8, color=COLORS[1], edgecolor='black', linewidth=2)
    bars2 = ax.bar(x + width/2, priority_counts, width, 
                   label='Priority-based Election', 
                   alpha=0.8, color=COLORS[9], edgecolor='black', linewidth=2)
    
    # 设置标签（删除title以适应论文风格）
    ax.set_xlabel('Node ID', fontsize=20, fontweight='bold')
    ax.set_ylabel('Number of Times as Leader', fontsize=20, fontweight='bold')
    ax.set_xticks(x)
    ax.set_xticklabels(range(num_nodes), fontsize=16)
    ax.tick_params(axis='y', labelsize=16)
    
    # 添加图例（调整大小）
    ax.legend(fontsize=16, loc='best', framealpha=0.95, edgecolor='gray')
    
    # 添加网格（提高可见度）
    ax.grid(axis='y', alpha=0.7, linestyle='--', linewidth=0.8)
    ax.set_axisbelow(True)
    
    # 在柱子上方添加数值标签
    for bars in [bars1, bars2]:
        for bar in bars:
            height = bar.get_height()
            if height > 0:  # 只显示非零值
                ax.text(bar.get_x() + bar.get_width()/2., height,
                       f'{int(height)}',
                       ha='center', va='bottom', fontsize=13, fontweight='bold')
    
    plt.tight_layout()
    
    # 保存图片
    output_path = os.path.join(output_dir, 'leader_distribution.png')
    plt.savefig(output_path, dpi=300, bbox_inches='tight')
    print(f"[OK] Leader分布图已保存: {output_path}")
    plt.close()


def plot_timeout_statistics(random_timeouts, priority_timeouts, 
                            output_dir='output/election'):
    """
    绘制超时时间统计对比（分别输出单图）
    
    Parameters:
    -----------
    random_timeouts : ndarray
        Random选举的超时矩阵
    priority_timeouts : ndarray
        Priority选举的超时矩阵
    output_dir : str
        输出目录
    """
    os.makedirs(output_dir, exist_ok=True)
    
    # 计算每个节点的平均超时时间
    random_avg = np.mean(random_timeouts, axis=0)
    priority_avg = np.mean(priority_timeouts, axis=0)
    
    # 计算标准差
    random_std = np.std(random_timeouts, axis=0)
    priority_std = np.std(random_timeouts, axis=0)
    
    num_nodes = random_timeouts.shape[1]
    x = np.arange(num_nodes)
    
    # === 单图1: 平均超时时间 ===
    fig, ax = plt.subplots(figsize=(10, 8), dpi=DPI)
    
    ax.errorbar(x, random_avg, yerr=random_std, 
                fmt='o-', linewidth=3.5, markersize=10, 
                label='Random', color=COLORS[1], 
                capsize=5, capthick=2, alpha=0.8)
    ax.errorbar(x, priority_avg, yerr=priority_std, 
                fmt='s-', linewidth=3.5, markersize=10, 
                label='Priority-based', color=COLORS[9], 
                capsize=5, capthick=2, alpha=0.8)
    
    ax.set_xlabel('Node ID', fontsize=20, fontweight='bold')
    ax.set_ylabel('Average Timeout (ms)', fontsize=20, fontweight='bold')
    # 删除title以适应论文风格
    ax.set_xticks(x)
    ax.tick_params(axis='both', labelsize=16)
    ax.legend(fontsize=16, loc='best', framealpha=0.95, edgecolor='gray')
    ax.grid(True, alpha=0.7, linestyle='--', linewidth=0.8)
    
    plt.tight_layout()
    output_path1 = os.path.join(output_dir, 'timeout_avg_comparison.png')
    plt.savefig(output_path1, dpi=300, bbox_inches='tight')
    print(f"[OK] 平均超时对比图已保存: {output_path1}")
    plt.close()
    
    # === 单图2: 超时时间变化（标准差） ===
    fig, ax = plt.subplots(figsize=(10, 8), dpi=DPI)
    
    width = 0.35
    bars1 = ax.bar(x - width/2, random_std, width, 
                   label='Random', alpha=0.8, color=COLORS[1], 
                   edgecolor='black', linewidth=2)
    bars2 = ax.bar(x + width/2, priority_std, width, 
                   label='Priority-based', alpha=0.8, color=COLORS[9], 
                   edgecolor='black', linewidth=2)
    
    ax.set_xlabel('Node ID', fontsize=20, fontweight='bold')
    ax.set_ylabel('Std Dev of Timeout (ms)', fontsize=20, fontweight='bold')
    # 删除title以适应论文风格
    ax.set_xticks(x)
    ax.tick_params(axis='both', labelsize=16)
    ax.legend(fontsize=16, loc='best', framealpha=0.95, edgecolor='gray')
    ax.grid(axis='y', alpha=0.7, linestyle='--', linewidth=0.8)
    
    plt.tight_layout()
    output_path2 = os.path.join(output_dir, 'timeout_std_comparison.png')
    plt.savefig(output_path2, dpi=300, bbox_inches='tight')
    print(f"[OK] 超时标准差对比图已保存: {output_path2}")
    plt.close()


def print_statistics(random_timeouts, priority_timeouts, 
                    random_leaders, priority_leaders):
    """
    打印统计信息
    
    Parameters:
    -----------
    random_timeouts : ndarray
        Random选举的超时矩阵
    priority_timeouts : ndarray
        Priority选举的超时矩阵
    random_leaders : list
        Random选举的leader历史
    priority_leaders : list
        Priority选举的leader历史
    """
    num_nodes = random_timeouts.shape[1]
    
    print("\n" + "=" * 80)
    print("RAFT ELECTION SIMULATION STATISTICS")
    print("=" * 80)
    
    # Random选举统计
    print("\n[Random Election]")
    print(f"  Average timeout: {np.mean(random_timeouts):.2f} ms")
    print(f"  Timeout range: [{np.min(random_timeouts):.2f}, {np.max(random_timeouts):.2f}] ms")
    print(f"  Timeout std dev: {np.std(random_timeouts):.2f} ms")
    
    random_counts = np.bincount(random_leaders, minlength=num_nodes)
    print(f"  Leader distribution: {random_counts}")
    print(f"  Max leader count: {np.max(random_counts)}")
    print(f"  Min leader count: {np.min(random_counts)}")
    print(f"  Std dev of leader count: {np.std(random_counts):.2f}")
    
    # Priority选举统计
    print("\n[Priority-based Election]")
    print(f"  Average timeout: {np.mean(priority_timeouts):.2f} ms")
    print(f"  Timeout range: [{np.min(priority_timeouts):.2f}, {np.max(priority_timeouts):.2f}] ms")
    print(f"  Timeout std dev: {np.std(priority_timeouts):.2f} ms")
    
    priority_counts = np.bincount(priority_leaders, minlength=num_nodes)
    print(f"  Leader distribution: {priority_counts}")
    print(f"  Max leader count: {np.max(priority_counts)}")
    print(f"  Min leader count: {np.min(priority_counts)}")
    print(f"  Std dev of leader count: {np.std(priority_counts):.2f}")
    
    # 对比
    print("\n[Comparison]")
    balance_improvement = (np.std(random_counts) - np.std(priority_counts)) / np.std(random_counts) * 100
    print(f"  Balance improvement (leader distribution): {balance_improvement:.1f}%")
    print(f"  Max leader count reduction: {np.max(random_counts) - np.max(priority_counts)}")
    
    print("=" * 80)


def main():
    """主函数"""
    print("=" * 80)
    print("Raft 选举超时热力图对比分析")
    print("Random vs Priority-based Election Timeout Comparison")
    print("=" * 80)
    
    print(f"\n[Configuration]")
    print(f"  Nodes: {NUM_NODES}")
    print(f"  Election Rounds: {NUM_ROUNDS}")
    print(f"  Regions: {NUM_REGIONS}")
    print(f"  Base Timeout: {T_BASE} ms")
    print(f"  Random Timeout Range: [0, {T_RANGE_RANDOM}] ms")
    print(f"  Priority α: {ALPHA}")
    print(f"  Delta Range: {DELTA_RANGE} ms")
    print(f"  Output Directory: {OUTPUT_DIR}")
    
    # 创建模拟器
    print(f"\n[Step 1/4] 初始化模拟器...")
    simulator = RaftElectionSimulator(
        num_nodes=NUM_NODES,
        num_regions=NUM_REGIONS,
        T_base=T_BASE,
        alpha=ALPHA,
        delta_range=DELTA_RANGE
    )
    
    # 模拟Random选举
    print(f"\n[Step 2/4] 模拟Random选举 ({NUM_ROUNDS} rounds)...")
    random_timeouts, random_leaders = simulator.simulate_random_election(NUM_ROUNDS)
    print(f"  完成 {NUM_ROUNDS} 轮Random选举")
    
    # 模拟Priority选举
    print(f"\n[Step 3/4] 模拟Priority-based选举 ({NUM_ROUNDS} rounds)...")
    priority_timeouts, priority_leaders = simulator.simulate_priority_election(NUM_ROUNDS)
    print(f"  完成 {NUM_ROUNDS} 轮Priority选举")
    
    # 生成可视化
    print(f"\n[Step 4/4] 生成可视化图表...")
    
    # 绘制热力图
    plot_timeout_heatmaps(
        random_timeouts, priority_timeouts,
        random_leaders, priority_leaders,
        output_dir=OUTPUT_DIR
    )
    
    # 绘制leader分布
    plot_leader_distribution(
        random_leaders, priority_leaders,
        NUM_NODES, output_dir=OUTPUT_DIR
    )
    
    # 绘制超时统计
    plot_timeout_statistics(
        random_timeouts, priority_timeouts,
        output_dir=OUTPUT_DIR
    )
    
    # 打印统计信息
    print_statistics(
        random_timeouts, priority_timeouts,
        random_leaders, priority_leaders
    )
    
    print("\n" + "=" * 80)
    print("[OK] 仿真完成！")
    print(f"\n生成的图表（单图模式）:")
    print(f"  热力图:")
    print(f"    - {OUTPUT_DIR}/timeout_random.png")
    print(f"    - {OUTPUT_DIR}/timeout_priority.png")
    print(f"  Leader分布:")
    print(f"    - {OUTPUT_DIR}/leader_distribution.png")
    print(f"  超时统计:")
    print(f"    - {OUTPUT_DIR}/timeout_avg_comparison.png")
    print(f"    - {OUTPUT_DIR}/timeout_std_comparison.png")
    print("=" * 80)


if __name__ == '__main__':
    main()


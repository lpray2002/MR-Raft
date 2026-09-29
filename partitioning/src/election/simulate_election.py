"""
指数惩罚优先级选举模拟器
Exponential Penalty-based Leader Election Simulator

公式: T_j^(r) = T_base × (1 + α)^L_j + δ
"""

import numpy as np
import matplotlib.pyplot as plt
from collections import defaultdict, Counter
import seaborn as sns

# 设置绘图风格
plt.style.use('seaborn-v0_8-paper')
sns.set_palette("husl")

class ExponentialElectionSimulator:
    """指数惩罚选举模拟器"""

    def __init__(self, num_nodes=15, num_regions=5,
                 T_base=100, T_max=300, alpha=0.5, delta_range=10):
        """
        初始化模拟器

        Args:
            num_nodes: 节点总数
            num_regions: region总数
            T_base: 基础timeout (ms)
            T_max: 最大timeout (ms)
            alpha: 指数惩罚因子
            delta_range: 随机扰动范围 (ms)
        """
        self.num_nodes = num_nodes
        self.num_regions = num_regions
        self.T_base = T_base
        self.T_max = T_max
        self.alpha = alpha
        self.delta_range = delta_range

        # 生成node-region membership (基于Threshold-based配置)
        self.node_regions = self._generate_membership()
        self.region_members = self._compute_region_members()

        # 计算L_max: 节点可能承担的最大leader数量
        self.L_max = max(len(regions) for regions in self.node_regions.values())

    def _generate_membership(self):
        """
        生成node-region membership
        模拟Threshold-based partitioning: avg 1.4 instances/node, max 2
        """
        membership = {i: [] for i in range(self.num_nodes)}

        # 每个region包含3-5个节点
        for region_id in range(self.num_regions):
            region_size = np.random.randint(3, 6)

            # 选择节点，但限制每个节点最多2个region
            candidates = [n for n in range(self.num_nodes)
                         if len(membership[n]) < 2]

            if len(candidates) >= region_size:
                members = np.random.choice(candidates, size=region_size,
                                          replace=False)
            else:
                members = candidates + list(np.random.choice(
                    [n for n in range(self.num_nodes) if n not in candidates],
                    size=region_size - len(candidates), replace=False
                ))

            for node_id in members:
                membership[node_id].append(region_id)

        return membership

    def _compute_region_members(self):
        """计算每个region的成员列表"""
        region_members = {r: [] for r in range(self.num_regions)}
        for node_id, regions in self.node_regions.items():
            for region_id in regions:
                region_members[region_id].append(node_id)
        return region_members

    def compute_timeout_exponential(self, L_j):
        """
        计算指数惩罚timeout（带归一化）

        T_j^(r) = T_base + (T_max - T_base) × [1 - (1/(1+α))^(L_j/L_max)] + δ

        Args:
            L_j: 节点当前的leader数量

        Returns:
            timeout (ms)
        """
        # 归一化的指数项
        normalized_L = L_j / self.L_max if self.L_max > 0 else 0
        decay_factor = (1.0 / (1 + self.alpha)) ** normalized_L
        delta = np.random.uniform(0, self.delta_range)

        timeout = self.T_base + (self.T_max - self.T_base) * (1 - decay_factor) + delta

        return timeout

    def compute_timeout_random(self):
        """Random baseline: 均匀随机timeout"""
        return self.T_base + np.random.uniform(0, 50)

    def simulate_election_exponential(self):
        """
        模拟一轮指数惩罚选举

        Returns:
            leader_counts: array of shape (num_nodes,)
        """
        leader_counts = np.zeros(self.num_nodes, dtype=int)

        for region_id in range(self.num_regions):
            candidates = self.region_members[region_id]

            if not candidates:
                continue

            # 计算每个候选节点的timeout
            timeouts = {}
            for node_id in candidates:
                L_j = leader_counts[node_id]
                timeouts[node_id] = self.compute_timeout_exponential(L_j)

            # Timeout最短的获胜
            winner = min(timeouts, key=timeouts.get)
            leader_counts[winner] += 1

        return leader_counts

    def simulate_election_random(self):
        """
        模拟一轮随机选举

        Returns:
            leader_counts: array of shape (num_nodes,)
        """
        leader_counts = np.zeros(self.num_nodes, dtype=int)

        for region_id in range(self.num_regions):
            candidates = self.region_members[region_id]

            if not candidates:
                continue

            # 随机选择
            winner = np.random.choice(candidates)
            leader_counts[winner] += 1

        return leader_counts

    def run_trials(self, num_trials=1000):
        """
        运行多次试验

        Args:
            num_trials: 试验次数

        Returns:
            random_results, exponential_results: 两个策略的结果列表
        """
        random_results = []
        exponential_results = []

        for _ in range(num_trials):
            random_results.append(self.simulate_election_random())
            exponential_results.append(self.simulate_election_exponential())

        return random_results, exponential_results

    @staticmethod
    def compute_gini(counts):
        """计算Gini系数"""
        counts = np.array(counts, dtype=float)
        if np.sum(counts) == 0:
            return 0.0
        n = len(counts)
        sorted_counts = np.sort(counts)
        index = np.arange(1, n + 1)
        return (2 * np.sum(index * sorted_counts)) / (n * np.sum(counts)) - (n + 1) / n

    @staticmethod
    def compute_statistics(results):
        """
        计算统计指标

        Args:
            results: list of leader_counts arrays

        Returns:
            dict with statistics
        """
        results_array = np.array(results)

        max_leaders_per_trial = np.max(results_array, axis=1)
        std_per_trial = np.std(results_array, axis=1)
        gini_per_trial = [ExponentialElectionSimulator.compute_gini(r)
                          for r in results]

        return {
            'mean_leaders_per_node': np.mean(results_array, axis=0),
            'avg_max_leaders': np.mean(max_leaders_per_trial),
            'median_max_leaders': np.median(max_leaders_per_trial),
            'p95_max_leaders': np.percentile(max_leaders_per_trial, 95),
            'avg_std': np.mean(std_per_trial),
            'avg_gini': np.mean(gini_per_trial),
            'max_leader_distribution': Counter(max_leaders_per_trial),
            'all_results': results_array
        }


def visualize_results(random_stats, exp_stats, simulator):
    """
    生成对比图表

    Args:
        random_stats: random策略的统计结果
        exp_stats: exponential策略的统计结果
        simulator: 模拟器实例
    """

    # === Figure 1: Leader Count Distribution (Histogram) ===
    fig, axes = plt.subplots(1, 2, figsize=(12, 4))

    # Random election
    all_counts_random = random_stats['all_results'].flatten()
    axes[0].hist(all_counts_random, bins=range(0, simulator.num_regions+2),
                 alpha=0.7, edgecolor='black', color='steelblue')
    axes[0].set_xlabel('Number of Leader Roles per Node', fontsize=11)
    axes[0].set_ylabel('Frequency', fontsize=11)
    axes[0].set_title('Random Election', fontsize=12, fontweight='bold')
    axes[0].axvline(random_stats['avg_max_leaders'],
                    color='red', linestyle='--', linewidth=2,
                    label=f"Avg Max: {random_stats['avg_max_leaders']:.2f}")
    axes[0].legend(fontsize=10)
    axes[0].grid(axis='y', alpha=0.3)

    # Exponential election
    all_counts_exp = exp_stats['all_results'].flatten()
    axes[1].hist(all_counts_exp, bins=range(0, simulator.num_regions+2),
                 alpha=0.7, edgecolor='black', color='seagreen')
    axes[1].set_xlabel('Number of Leader Roles per Node', fontsize=11)
    axes[1].set_ylabel('Frequency', fontsize=11)
    axes[1].set_title('Exponential Penalty Election', fontsize=12, fontweight='bold')
    axes[1].axvline(exp_stats['avg_max_leaders'],
                    color='red', linestyle='--', linewidth=2,
                    label=f"Avg Max: {exp_stats['avg_max_leaders']:.2f}")
    axes[1].legend(fontsize=10)
    axes[1].grid(axis='y', alpha=0.3)

    plt.tight_layout()
    plt.savefig('leader_distribution_comparison.png', dpi=300, bbox_inches='tight')
    print("✓ Saved: leader_distribution_comparison.png")

    # === Figure 2: Per-Node Average Leader Count ===
    fig, ax = plt.subplots(figsize=(10, 5))

    x = np.arange(simulator.num_nodes)
    width = 0.35

    bars1 = ax.bar(x - width/2, random_stats['mean_leaders_per_node'],
                   width, label='Random', alpha=0.8, color='steelblue')
    bars2 = ax.bar(x + width/2, exp_stats['mean_leaders_per_node'],
                   width, label='Exponential Penalty', alpha=0.8, color='seagreen')

    ax.set_xlabel('Node ID', fontsize=12)
    ax.set_ylabel('Average Number of Leader Roles', fontsize=12)
    ax.set_title('Average Leader Distribution per Node (1000 trials)',
                 fontsize=13, fontweight='bold')
    ax.set_xticks(x)
    ax.legend(fontsize=11)
    ax.grid(axis='y', alpha=0.3)

    # 添加均值线
    ax.axhline(np.mean(random_stats['mean_leaders_per_node']),
               color='steelblue', linestyle='--', alpha=0.5)
    ax.axhline(np.mean(exp_stats['mean_leaders_per_node']),
               color='seagreen', linestyle='--', alpha=0.5)

    plt.tight_layout()
    plt.savefig('per_node_leader_comparison.png', dpi=300, bbox_inches='tight')
    print("✓ Saved: per_node_leader_comparison.png")

    # === Figure 3: Peak Load Box Plot ===
    fig, ax = plt.subplots(figsize=(8, 6))

    data_to_plot = [
        np.max(random_stats['all_results'], axis=1),
        np.max(exp_stats['all_results'], axis=1)
    ]

    bp = ax.boxplot(data_to_plot, labels=['Random', 'Exponential\nPenalty'],
                    patch_artist=True, widths=0.6,
                    boxprops=dict(facecolor='lightblue', alpha=0.7),
                    medianprops=dict(color='red', linewidth=2),
                    whiskerprops=dict(linewidth=1.5),
                    capprops=dict(linewidth=1.5))

    bp['boxes'][0].set_facecolor('lightblue')
    bp['boxes'][1].set_facecolor('lightgreen')

    ax.set_ylabel('Maximum Leader Count per Node', fontsize=12)
    ax.set_title('Peak Leadership Load Distribution',
                 fontsize=13, fontweight='bold')
    ax.grid(axis='y', alpha=0.3)

    # 添加统计注释
    for i, stats in enumerate([random_stats, exp_stats]):
        y_pos = stats['median_max_leaders']
        ax.text(i+1.25, y_pos, f"Median: {stats['median_max_leaders']:.1f}\n"
                                f"95th: {stats['p95_max_leaders']:.1f}",
                fontsize=9, va='center')

    plt.tight_layout()
    plt.savefig('peak_load_boxplot.png', dpi=300, bbox_inches='tight')
    print("✓ Saved: peak_load_boxplot.png")

    # === Figure 4: Comparison Table ===
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.axis('tight')
    ax.axis('off')

    table_data = [
        ['Metric', 'Random', 'Exponential', 'Improvement'],
        ['Avg Max Leaders/Node',
         f"{random_stats['avg_max_leaders']:.2f}",
         f"{exp_stats['avg_max_leaders']:.2f}",
         f"{(1 - exp_stats['avg_max_leaders']/random_stats['avg_max_leaders'])*100:.1f}%"],
        ['Median Max Leaders',
         f"{random_stats['median_max_leaders']:.0f}",
         f"{exp_stats['median_max_leaders']:.0f}",
         f"{(random_stats['median_max_leaders'] - exp_stats['median_max_leaders']):.0f}"],
        ['95th Percentile Max',
         f"{random_stats['p95_max_leaders']:.0f}",
         f"{exp_stats['p95_max_leaders']:.0f}",
         f"{(random_stats['p95_max_leaders'] - exp_stats['p95_max_leaders']):.0f}"],
        ['Avg Std Dev',
         f"{random_stats['avg_std']:.2f}",
         f"{exp_stats['avg_std']:.2f}",
         f"{(1 - exp_stats['avg_std']/random_stats['avg_std'])*100:.1f}%"],
        ['Avg Gini Coefficient',
         f"{random_stats['avg_gini']:.3f}",
         f"{exp_stats['avg_gini']:.3f}",
         f"{(1 - exp_stats['avg_gini']/random_stats['avg_gini'])*100:.1f}%"],
    ]

    table = ax.table(cellText=table_data, cellLoc='center', loc='center',
                     colWidths=[0.35, 0.2, 0.2, 0.25])
    table.auto_set_font_size(False)
    table.set_fontsize(10)
    table.scale(1, 2)

    # 格式化表头
    for i in range(4):
        table[(0, i)].set_facecolor('#4CAF50')
        table[(0, i)].set_text_props(weight='bold', color='white')

    # 格式化improvement列
    for i in range(1, len(table_data)):
        table[(i, 3)].set_facecolor('#E8F5E9')

    plt.savefig('comparison_table.png', dpi=300, bbox_inches='tight')
    print("✓ Saved: comparison_table.png")


def print_statistics(random_stats, exp_stats):
    """打印统计结果"""
    print("\n" + "="*60)
    print("ELECTION STRATEGY COMPARISON RESULTS")
    print("="*60)

    print("\n📊 Random Election:")
    print(f"  Average Max Leaders per Node: {random_stats['avg_max_leaders']:.2f}")
    print(f"  Median Max Leaders: {random_stats['median_max_leaders']:.0f}")
    print(f"  95th Percentile Max: {random_stats['p95_max_leaders']:.0f}")
    print(f"  Average Std Dev: {random_stats['avg_std']:.2f}")
    print(f"  Average Gini Coefficient: {random_stats['avg_gini']:.3f}")

    print("\n🎯 Exponential Penalty Election:")
    print(f"  Average Max Leaders per Node: {exp_stats['avg_max_leaders']:.2f}")
    print(f"  Median Max Leaders: {exp_stats['median_max_leaders']:.0f}")
    print(f"  95th Percentile Max: {exp_stats['p95_max_leaders']:.0f}")
    print(f"  Average Std Dev: {exp_stats['avg_std']:.2f}")
    print(f"  Average Gini Coefficient: {exp_stats['avg_gini']:.3f}")

    print("\n✨ Improvement:")
    print(f"  Peak Load Reduction: {(1 - exp_stats['avg_max_leaders']/random_stats['avg_max_leaders'])*100:.1f}%")
    print(f"  Balance Improvement (Std Dev): {(1 - exp_stats['avg_std']/random_stats['avg_std'])*100:.1f}%")
    print(f"  Balance Improvement (Gini): {(1 - exp_stats['avg_gini']/random_stats['avg_gini'])*100:.1f}%")
    print("="*60)


def main():
    """主函数"""
    print("\n🚀 Starting Exponential Penalty Election Simulation...")
    print("Configuration:")
    print("  - Nodes: 15")
    print("  - Regions: 5")
    print("  - T_base: 100 ms")
    print("  - α (alpha): 0.5")
    print("  - Formula: T_j^(r) = 100 × 1.5^L_j + δ")
    print("  - Trials: 1000")

    # 创建模拟器
    simulator = ExponentialElectionSimulator(
        num_nodes=15,
        num_regions=5,
        T_base=100,
        alpha=0.5,
        delta_range=10
    )

    print("\n⏳ Running simulations...")

    # 运行试验
    random_results, exp_results = simulator.run_trials(num_trials=1000)

    # 计算统计
    random_stats = simulator.compute_statistics(random_results)
    exp_stats = simulator.compute_statistics(exp_results)

    # 打印结果
    print_statistics(random_stats, exp_stats)

    # 生成图表
    print("\n📈 Generating visualizations...")
    visualize_results(random_stats, exp_stats, simulator)

    print("\n✅ Simulation completed successfully!")
    print("Generated files:")
    print("  - leader_distribution_comparison.png")
    print("  - per_node_leader_comparison.png")
    print("  - peak_load_boxplot.png")
    print("  - comparison_table.png")


if __name__ == "__main__":
    main()


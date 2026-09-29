"""
UAV Handover 实验结果分析与论文图表生成

生成 5 张论文级图表：
Fig 1: 请求成功率 vs UAV 速度（按 leader 分布分组）
Fig 2: Handover 事件周围的延迟时间序列
Fig 3: Leader 数量差异 vs 额外延迟（散点图+回归）
Fig 4: 四种移动模型对比（分组柱状图）
Fig 5: Priority-based election 效果验证
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '.'))

import json
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter
import csv
from collections import defaultdict

# 论文风格设置
plt.rcParams.update({
    'font.size': 11,
    'axes.titlesize': 13,
    'axes.labelsize': 12,
    'xtick.labelsize': 10,
    'ytick.labelsize': 10,
    'legend.fontsize': 10,
    'figure.dpi': 150,
    'savefig.dpi': 300,
    'savefig.bbox': 'tight',
})

OUTPUT_DIR = "output/handover_experiment"
FIGURE_DIR = "output/handover_figures"


def load_aggregated_results():
    """加载聚合结果"""
    path = f"{OUTPUT_DIR}/aggregated_results.json"
    with open(path) as f:
        return json.load(f)


def load_csv_records(model, speed, strategy, seed):
    """加载单次实验的 CSV 记录"""
    path = f"{OUTPUT_DIR}/csv/{model}_s{speed}_{strategy}_seed{seed}.csv"
    if not os.path.exists(path):
        return []
    records = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            records.append({
                'seq': int(row['seq']),
                'timestamp_ms': float(row['timestamp_ms']),
                'node_id': int(row['node_id']),
                'region_id': int(row['region_id']),
                'latency_ms': float(row['latency_ms']),
                'success': bool(int(row['success'])),
                'was_forwarded': bool(int(row['was_forwarded'])),
                'is_handover_window': bool(int(row['is_handover_window'])),
                'handover_id': int(row['handover_id']),
                'leader_count_diff': int(row['leader_count_diff']),
            })
    return records


def fig1_success_rate_vs_speed(results):
    """
    Fig 1: 请求成功率 vs UAV 速度
    三条线：balanced / moderate / skewed
    """
    fig, ax = plt.subplots(1, 1, figsize=(7, 5))
    
    strategies = ["balanced", "moderate", "skewed"]
    colors = ['#2196F3', '#FF9800', '#F44336']
    markers = ['o', 's', '^']
    speeds = [10, 20, 40]
    
    for i, strategy in enumerate(strategies):
        rates = []
        for speed in speeds:
            # 取所有模型的平均
            vals = []
            for model in ["linear", "rwp", "gauss_markov", "predefined"]:
                key = f"{model}_s{speed}_{strategy}"
                if key in results:
                    vals.append(results[key]["success_rate"] * 100)
            rates.append(np.mean(vals) if vals else 100)
        
        ax.plot(speeds, rates, f'-{markers[i]}', color=colors[i],
                linewidth=2, markersize=8, label=f'{strategy.capitalize()}')
    
    ax.set_xlabel('UAV Speed (m/s)')
    ax.set_ylabel('Request Success Rate (%)')
    ax.set_title('End-to-End Reliability vs UAV Speed')
    ax.legend()
    ax.grid(True, alpha=0.3)
    ax.set_ylim(98, 100.5)
    ax.set_xticks(speeds)
    
    plt.tight_layout()
    plt.savefig(f"{FIGURE_DIR}/fig1_success_rate_vs_speed.png")
    plt.savefig(f"{FIGURE_DIR}/fig1_success_rate_vs_speed.pdf")
    plt.close()
    print("  Saved: fig1_success_rate_vs_speed")


def fig2_latency_around_handover(results):
    """
    Fig 2: Handover 事件周围的延迟时间序列
    展示 handover 前后 ±2s 的延迟变化
    """
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.5))
    
    strategies = ["balanced", "moderate", "skewed"]
    colors = ['#2196F3', '#FF9800', '#F44336']
    
    for ax_idx, strategy in enumerate(strategies):
        ax = axes[ax_idx]
        
        # 加载 CSV 数据（用 rwp, speed=40 的数据，handover 最多）
        records = load_csv_records("rwp", 40, strategy, 42)
        if not records:
            ax.text(0.5, 0.5, 'No data', ha='center', va='center',
                    transform=ax.transAxes)
            continue
        
        # 找到 handover 窗口内的请求，确定 handover 时刻
        ho_times = set()
        for r in records:
            if r['is_handover_window'] and r['handover_id'] >= 0:
                # 估算 handover 时刻（窗口中心）
                ho_times.add(r['handover_id'])
        
        # 对每个 handover 事件，收集前后的延迟
        window_ms = 2000  # ±2s
        interval_ms = 50
        
        # 按时间排列所有请求
        times = np.array([r['timestamp_ms'] for r in records])
        latencies = np.array([r['latency_ms'] for r in records])
        successes = np.array([r['success'] for r in records])
        ho_flags = np.array([r['is_handover_window'] for r in records])
        
        # 绘制完整时间序列（前 10 秒）
        mask = times <= 15000
        ax.scatter(times[mask & ~ho_flags] / 1000, 
                  latencies[mask & ~ho_flags],
                  s=8, alpha=0.5, c=colors[ax_idx], label='Stable')
        ax.scatter(times[mask & ho_flags] / 1000,
                  latencies[mask & ho_flags],
                  s=20, alpha=0.8, c='red', marker='x', label='HO window')
        
        # 标记 handover 时刻
        for r in records:
            if r['is_handover_window'] and r['handover_id'] >= 0:
                ho_t = r['timestamp_ms'] / 1000
                if ho_t <= 15:
                    ax.axvline(x=ho_t, color='red', linestyle='--', 
                              alpha=0.4, linewidth=1)
                    break
        
        ax.set_xlabel('Time (s)')
        ax.set_ylabel('Latency (ms)')
        ax.set_title(f'Leader: {strategy.capitalize()}')
        ax.legend(loc='upper right', fontsize=9)
        ax.grid(True, alpha=0.3)
        ax.set_ylim(0, max(latencies[mask]) * 1.2 if any(mask) else 30)
    
    plt.suptitle('Request Latency Timeline (RWP, 40 m/s)', fontsize=13, y=1.02)
    plt.tight_layout()
    plt.savefig(f"{FIGURE_DIR}/fig2_latency_timeline.png")
    plt.savefig(f"{FIGURE_DIR}/fig2_latency_timeline.pdf")
    plt.close()
    print("  Saved: fig2_latency_timeline")


def fig3_leader_diff_vs_latency(results):
    """
    Fig 3: Leader 数量差异 vs 额外延迟
    散点图 + 回归线
    """
    fig, ax = plt.subplots(1, 1, figsize=(7, 5))
    
    # 收集所有 handover 窗口内的请求
    all_diffs = []
    all_latencies = []
    
    for model in ["rwp", "gauss_markov", "predefined"]:
        for speed in [20, 40]:
            for strategy in ["balanced", "moderate", "skewed"]:
                records = load_csv_records(model, speed, strategy, 42)
                for r in records:
                    if r['is_handover_window'] and r['success']:
                        all_diffs.append(r['leader_count_diff'])
                        all_latencies.append(r['latency_ms'])
    
    if not all_diffs:
        ax.text(0.5, 0.5, 'No handover data', ha='center', va='center',
                transform=ax.transAxes)
    else:
        diffs = np.array(all_diffs)
        lats = np.array(all_latencies)
        
        # 散点图
        ax.scatter(diffs + np.random.normal(0, 0.1, len(diffs)),  # jitter
                  lats, s=20, alpha=0.4, c='steelblue')
        
        # 按 diff 分组计算均值
        unique_diffs = sorted(set(all_diffs))
        mean_lats = []
        for d in unique_diffs:
            mask = diffs == d
            mean_lats.append(np.mean(lats[mask]))
        
        ax.plot(unique_diffs, mean_lats, 'ro-', linewidth=2, markersize=10,
                label='Mean latency', zorder=5)
        
        # 线性回归
        if len(unique_diffs) > 1:
            z = np.polyfit(diffs, lats, 1)
            p = np.poly1d(z)
            x_fit = np.linspace(min(diffs), max(diffs), 50)
            ax.plot(x_fit, p(x_fit), 'k--', linewidth=1.5, alpha=0.7,
                    label=f'Linear fit (slope={z[0]:.2f} ms/Δleader)')
    
    ax.set_xlabel('|ΔLeaders| at Handover')
    ax.set_ylabel('Request Latency (ms)')
    ax.set_title('Leader Count Difference vs Handover Latency')
    ax.legend()
    ax.grid(True, alpha=0.3)
    
    plt.tight_layout()
    plt.savefig(f"{FIGURE_DIR}/fig3_leader_diff_vs_latency.png")
    plt.savefig(f"{FIGURE_DIR}/fig3_leader_diff_vs_latency.pdf")
    plt.close()
    print("  Saved: fig3_leader_diff_vs_latency")


def fig4_movement_model_comparison(results):
    """
    Fig 4: 四种移动模型对比（分组柱状图）
    指标：handover 频率、平均延迟、P99 延迟、成功率
    """
    fig, axes = plt.subplots(2, 2, figsize=(12, 9))
    
    models = ["linear", "rwp", "gauss_markov", "predefined"]
    model_labels = ["Linear", "RWP", "Gauss-Markov", "Predefined"]
    strategies = ["balanced", "moderate", "skewed"]
    colors = ['#2196F3', '#FF9800', '#F44336']
    
    x = np.arange(len(models))
    width = 0.25
    
    # 固定速度 = 40 m/s
    speed = 40
    
    # (a) Handover 频率
    ax = axes[0, 0]
    for i, strategy in enumerate(strategies):
        vals = []
        for model in models:
            key = f"{model}_s{speed}_{strategy}"
            vals.append(results.get(key, {}).get("num_handovers", 0))
        ax.bar(x + i * width, vals, width, color=colors[i], 
               label=strategy.capitalize())
    ax.set_xlabel('Movement Model')
    ax.set_ylabel('Handover Count (60s)')
    ax.set_title('(a) Handover Frequency')
    ax.set_xticks(x + width)
    ax.set_xticklabels(model_labels)
    ax.legend()
    ax.grid(True, alpha=0.3, axis='y')
    
    # (b) 平均延迟
    ax = axes[0, 1]
    for i, strategy in enumerate(strategies):
        vals = []
        for model in models:
            key = f"{model}_s{speed}_{strategy}"
            vals.append(results.get(key, {}).get("avg_latency_ms", 0))
        ax.bar(x + i * width, vals, width, color=colors[i],
               label=strategy.capitalize())
    ax.set_xlabel('Movement Model')
    ax.set_ylabel('Average Latency (ms)')
    ax.set_title('(b) Average End-to-End Latency')
    ax.set_xticks(x + width)
    ax.set_xticklabels(model_labels)
    ax.legend()
    ax.grid(True, alpha=0.3, axis='y')
    
    # (c) P99 延迟
    ax = axes[1, 0]
    for i, strategy in enumerate(strategies):
        vals = []
        for model in models:
            key = f"{model}_s{speed}_{strategy}"
            vals.append(results.get(key, {}).get("p99_latency_ms", 0))
        ax.bar(x + i * width, vals, width, color=colors[i],
               label=strategy.capitalize())
    ax.set_xlabel('Movement Model')
    ax.set_ylabel('P99 Latency (ms)')
    ax.set_title('(c) Tail Latency (P99)')
    ax.set_xticks(x + width)
    ax.set_xticklabels(model_labels)
    ax.legend()
    ax.grid(True, alpha=0.3, axis='y')
    
    # (d) 成功率
    ax = axes[1, 1]
    for i, strategy in enumerate(strategies):
        vals = []
        for model in models:
            key = f"{model}_s{speed}_{strategy}"
            vals.append(results.get(key, {}).get("success_rate", 1) * 100)
        ax.bar(x + i * width, vals, width, color=colors[i],
               label=strategy.capitalize())
    ax.set_xlabel('Movement Model')
    ax.set_ylabel('Success Rate (%)')
    ax.set_title('(d) Request Success Rate')
    ax.set_xticks(x + width)
    ax.set_xticklabels(model_labels)
    ax.legend()
    ax.grid(True, alpha=0.3, axis='y')
    ax.set_ylim(98, 100.5)
    
    plt.suptitle(f'Movement Model Comparison (v=40 m/s, duration=60s)', 
                 fontsize=14, y=1.01)
    plt.tight_layout()
    plt.savefig(f"{FIGURE_DIR}/fig4_model_comparison.png")
    plt.savefig(f"{FIGURE_DIR}/fig4_model_comparison.pdf")
    plt.close()
    print("  Saved: fig4_model_comparison")


def fig5_priority_election_effect(results):
    """
    Fig 5: Priority-based election 效果验证
    对比 balanced vs skewed 在不同速度下的延迟差异
    """
    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    
    speeds = [10, 20, 40]
    models = ["linear", "rwp", "gauss_markov", "predefined"]
    
    # (a) Handover 窗口内延迟对比
    ax = axes[0]
    
    balanced_lats = []
    skewed_lats = []
    for speed in speeds:
        b_vals = []
        s_vals = []
        for model in models:
            key_b = f"{model}_s{speed}_balanced"
            key_s = f"{model}_s{speed}_skewed"
            if key_b in results and results[key_b]["handover_window_avg_latency_ms"] > 0:
                b_vals.append(results[key_b]["handover_window_avg_latency_ms"])
            if key_s in results and results[key_s]["handover_window_avg_latency_ms"] > 0:
                s_vals.append(results[key_s]["handover_window_avg_latency_ms"])
        balanced_lats.append(np.mean(b_vals) if b_vals else 0)
        skewed_lats.append(np.mean(s_vals) if s_vals else 0)
    
    x = np.arange(len(speeds))
    width = 0.35
    ax.bar(x - width/2, balanced_lats, width, color='#2196F3', 
           label='Balanced (w/ priority election)')
    ax.bar(x + width/2, skewed_lats, width, color='#F44336',
           label='Skewed (w/o priority election)')
    
    ax.set_xlabel('UAV Speed (m/s)')
    ax.set_ylabel('Handover Window Latency (ms)')
    ax.set_title('(a) Latency During Handover')
    ax.set_xticks(x)
    ax.set_xticklabels([str(s) for s in speeds])
    ax.legend()
    ax.grid(True, alpha=0.3, axis='y')
    
    # (b) 延迟增加比例
    ax = axes[1]
    
    balanced_ratios = []
    skewed_ratios = []
    for speed in speeds:
        b_vals = []
        s_vals = []
        for model in models:
            key_b = f"{model}_s{speed}_balanced"
            key_s = f"{model}_s{speed}_skewed"
            if key_b in results and results[key_b].get("latency_increase_ratio", 0) > 0:
                b_vals.append(results[key_b]["latency_increase_ratio"])
            if key_s in results and results[key_s].get("latency_increase_ratio", 0) > 0:
                s_vals.append(results[key_s]["latency_increase_ratio"])
        balanced_ratios.append(np.mean(b_vals) if b_vals else 1)
        skewed_ratios.append(np.mean(s_vals) if s_vals else 1)
    
    ax.bar(x - width/2, balanced_ratios, width, color='#2196F3',
           label='Balanced (w/ priority election)')
    ax.bar(x + width/2, skewed_ratios, width, color='#F44336',
           label='Skewed (w/o priority election)')
    
    ax.axhline(y=1.0, color='gray', linestyle='--', alpha=0.5, label='No increase')
    ax.set_xlabel('UAV Speed (m/s)')
    ax.set_ylabel('Latency Increase Ratio')
    ax.set_title('(b) Handover Latency / Stable Latency')
    ax.set_xticks(x)
    ax.set_xticklabels([str(s) for s in speeds])
    ax.legend()
    ax.grid(True, alpha=0.3, axis='y')
    
    plt.suptitle('Effect of Priority-Based Leader Election on Handover Performance',
                 fontsize=13, y=1.02)
    plt.tight_layout()
    plt.savefig(f"{FIGURE_DIR}/fig5_priority_election_effect.png")
    plt.savefig(f"{FIGURE_DIR}/fig5_priority_election_effect.pdf")
    plt.close()
    print("  Saved: fig5_priority_election_effect")


def print_summary_table(results):
    """打印汇总表格"""
    print("\n" + "=" * 90)
    print("  Summary Table")
    print("=" * 90)
    print(f"{'Config':<30} {'Succ%':>7} {'AvgLat':>7} {'P99':>7} "
          f"{'HO_Lat':>7} {'Stable':>7} {'ΔLat':>6}")
    print("-" * 90)
    
    for key in sorted(results.keys()):
        r = results[key]
        delta = r.get('latency_increase_ms', 0)
        print(f"{key:<30} "
              f"{r['success_rate']*100:>6.2f}% "
              f"{r['avg_latency_ms']:>6.2f} "
              f"{r['p99_latency_ms']:>6.2f} "
              f"{r['handover_window_avg_latency_ms']:>6.2f} "
              f"{r['stable_avg_latency_ms']:>6.2f} "
              f"{delta:>+5.1f}")


def main():
    os.makedirs(FIGURE_DIR, exist_ok=True)
    
    print("=" * 60)
    print("  Handover Experiment Analysis")
    print("=" * 60)
    
    # 加载结果
    results = load_aggregated_results()
    print(f"\nLoaded {len(results)} experiment configurations")
    
    # 生成图表
    print("\nGenerating figures...")
    fig1_success_rate_vs_speed(results)
    fig2_latency_around_handover(results)
    fig3_leader_diff_vs_latency(results)
    fig4_movement_model_comparison(results)
    fig5_priority_election_effect(results)
    
    # 打印汇总
    print_summary_table(results)
    
    print(f"\n{'='*60}")
    print(f"  All figures saved to {FIGURE_DIR}/")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()

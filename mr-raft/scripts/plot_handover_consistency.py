#!/usr/bin/env python3
"""
绘制 UAV Handover 一致性实验结果图
X轴: 网络延迟 (ms)
Y轴(左): Handover 一致性率 (%)
Y轴(右): 平均读延迟 (ms)
"""

import json
import os
import glob
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.ticker import MultipleLocator

# 结果目录
RESULTS_DIR = os.path.join(os.path.dirname(__file__), '..', 'output', 'handover_results_full', 'handover_results')
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), '..', 'output', 'handover_figures')

def load_results(results_dir):
    """加载所有实验结果"""
    files = glob.glob(os.path.join(results_dir, 'result_d*ms_c100_*.json'))
    
    delays = {}
    for f in files:
        name = os.path.basename(f)
        parts = name.replace('result_d', '').replace('.json', '').split('_c100_')
        delay_str = parts[0].replace('ms', '')
        mode = parts[1]
        
        try:
            delay = int(delay_str)
        except ValueError:
            delay = float(delay_str)
        
        if delay not in delays:
            delays[delay] = {}
        
        with open(f) as fh:
            r = json.load(fh)
        delays[delay][mode] = r['summary']
    
    return delays


def plot_consistency_vs_delay(delays, output_dir):
    """绘制一致性 vs 延迟图"""
    os.makedirs(output_dir, exist_ok=True)
    
    sorted_delays = sorted(delays.keys())
    
    ri_ho_rates = []
    sr_ho_rates = []
    ri_all_rates = []
    sr_all_rates = []
    ri_latencies = []
    sr_latencies = []
    
    for d in sorted_delays:
        ri = delays[d].get('readindex', {})
        sr = delays[d].get('stale', {})
        
        ri_ho_rates.append(ri.get('handover_consistency_rate', 0) * 100 if ri else None)
        sr_ho_rates.append(sr.get('handover_consistency_rate', 0) * 100 if sr else None)
        ri_all_rates.append(ri.get('read_consistency_rate', 0) * 100 if ri else None)
        sr_all_rates.append(sr.get('read_consistency_rate', 0) * 100 if sr else None)
        ri_latencies.append(ri.get('avg_read_latency_us', 0) / 1000 if ri else None)
        sr_latencies.append(sr.get('avg_read_latency_us', 0) / 1000 if sr else None)
    
    # === 图1: Handover 一致性率 + 整体一致性率 ===
    fig, ax1 = plt.subplots(figsize=(7, 5))
    
    ax1.plot(sorted_delays, ri_ho_rates, 'o-', color='#0273bc', linewidth=2,
             markersize=6, label='ReadIndex (Handover)', zorder=5)
    ax1.plot(sorted_delays, ri_all_rates, 'o--', color='#0273bc', linewidth=1.5,
             markersize=4, alpha=0.6, label='ReadIndex (All Reads)', zorder=4)
    ax1.plot(sorted_delays, sr_ho_rates, 's-', color='#d65c58', linewidth=2,
             markersize=6, label='StaleRead (Handover)', zorder=5)
    ax1.plot(sorted_delays, sr_all_rates, 's--', color='#d65c58', linewidth=1.5,
             markersize=4, alpha=0.6, label='StaleRead (All Reads)', zorder=4)
    
    ax1.set_xlabel('Network Delay (ms)', fontsize=20, fontweight='bold')
    ax1.set_ylabel('Consistency Rate (%)', fontsize=20, fontweight='bold')
    ax1.set_ylim(80, 101)
    ax1.set_xlim(-1, max(sorted_delays) + 1)
    ax1.xaxis.set_major_locator(MultipleLocator(10))
    ax1.grid(True, alpha=0.3, linestyle='-', linewidth=0.5)
    ax1.legend(loc='lower left', fontsize=12, framealpha=0.9)
    ax1.tick_params(axis='both', labelsize=18)
    
    ax1.axhline(y=100, color='#0273bc', linestyle=':', alpha=0.3, linewidth=1)
    
    plt.tight_layout()
    out_path = os.path.join(output_dir, 'handover_consistency_vs_delay.png')
    plt.savefig(out_path, dpi=300, bbox_inches='tight')
    plt.savefig(out_path.replace('.png', '.pdf'), bbox_inches='tight')
    print(f'Saved: {out_path}')
    plt.close()
    
    # === 图2: 读延迟 vs 网络延迟 ===
    fig, ax1 = plt.subplots(figsize=(7, 5))
    
    ax1.plot(sorted_delays, ri_latencies, 'o-', color='#0273bc', linewidth=2,
             markersize=6, label='ReadIndex Read Latency')
    ax1.plot(sorted_delays, sr_latencies, 's-', color='#d65c58', linewidth=2,
             markersize=6, label='StaleRead Read Latency')
    
    ax1.set_xlabel('Network Delay (ms)', fontsize=20, fontweight='bold')
    ax1.set_ylabel('Average Read Latency (ms)', fontsize=20, fontweight='bold')
    ax1.set_xlim(-1, max(sorted_delays) + 1)
    ax1.xaxis.set_major_locator(MultipleLocator(10))
    ax1.grid(True, alpha=0.3, linestyle='-', linewidth=0.5)
    ax1.legend(loc='upper left', fontsize=12, framealpha=0.9)
    ax1.tick_params(axis='both', labelsize=18)
    
    plt.tight_layout()
    out_path = os.path.join(output_dir, 'read_latency_vs_delay.png')
    plt.savefig(out_path, dpi=300, bbox_inches='tight')
    plt.savefig(out_path.replace('.png', '.pdf'), bbox_inches='tight')
    print(f'Saved: {out_path}')
    plt.close()
    
    # === 图3: 组合图（双Y轴）===
    fig, ax1 = plt.subplots(figsize=(7, 5))
    
    # 左Y轴：一致性率
    ln1 = ax1.plot(sorted_delays, ri_ho_rates, 'o-', color='#0273bc', linewidth=2.5,
                   markersize=6, label='ReadIndex Consistency', zorder=5)
    ln2 = ax1.plot(sorted_delays, sr_ho_rates, 's-', color='#d65c58', linewidth=2.5,
                   markersize=6, label='StaleRead Consistency', zorder=5)
    
    ax1.set_xlabel('Network Delay (ms)', fontsize=20, fontweight='bold')
    ax1.set_ylabel('Handover Consistency Rate (%)', fontsize=20, fontweight='bold',
                   color='black')
    ax1.set_ylim(80, 101)
    ax1.set_xlim(-1, max(sorted_delays) + 1)
    ax1.xaxis.set_major_locator(MultipleLocator(10))
    ax1.grid(True, alpha=0.3, linestyle='-', linewidth=0.5)
    ax1.tick_params(axis='both', labelsize=18)
    
    # 右Y轴：读延迟
    ax2 = ax1.twinx()
    ln3 = ax2.plot(sorted_delays, ri_latencies, '^--', color='#598dca', linewidth=1.5,
                   markersize=4, alpha=0.7, label='ReadIndex Latency')
    ln4 = ax2.plot(sorted_delays, sr_latencies, 'v--', color='#e6867f', linewidth=1.5,
                   markersize=4, alpha=0.7, label='StaleRead Latency')
    ax2.set_ylabel('Average Read Latency (ms)', fontsize=20, fontweight='bold',
                   color='gray')
    ax2.tick_params(axis='y', labelsize=18, colors='gray')
    
    # 合并图例
    lns = ln1 + ln2 + ln3 + ln4
    labs = [l.get_label() for l in lns]
    ax1.legend(lns, labs, loc='center left', fontsize=12, framealpha=0.9)
    
    plt.tight_layout()
    out_path = os.path.join(output_dir, 'handover_combined.png')
    plt.savefig(out_path, dpi=300, bbox_inches='tight')
    plt.savefig(out_path.replace('.png', '.pdf'), bbox_inches='tight')
    print(f'Saved: {out_path}')
    plt.close()
    
    # 打印数据表
    print('\n' + '=' * 80)
    print(f'{"Delay":>6} | {"RI HO%":>10} | {"SR HO%":>10} | {"RI Lat(ms)":>10} | {"SR Lat(ms)":>10}')
    print('-' * 80)
    for i, d in enumerate(sorted_delays):
        print(f'{d:>5}ms | {ri_ho_rates[i]:>9.2f}% | {sr_ho_rates[i]:>9.2f}% | '
              f'{ri_latencies[i]:>9.2f} | {sr_latencies[i]:>9.2f}')


if __name__ == '__main__':
    delays = load_results(RESULTS_DIR)
    print(f'Loaded {len(delays)} delay groups')
    plot_consistency_vs_delay(delays, OUTPUT_DIR)

#!/usr/bin/env python3
"""
Per-Region TPS 绘图脚本

为每种分区方法生成 per-region 恢复曲线图，支持：
  1. 所有 Region 独立曲线
  2. 按故障角色分组聚合（Leader-Region / Follower-Region / Unaffected）

用法:
  python3 scripts/plot_region_recovery.py \
      --input output/fault_recovery/comp_clique/run_1/region_tps_samples.json \
      --method Clique \
      --output output/fault_recovery/figures/

  # 批量模式（对比实验）
  python3 scripts/plot_region_recovery.py \
      --inputs "comp_global/run_1,comp_threshold/run_1,comp_clique/run_1,comp_neighbor/run_1" \
      --labels "Global,Threshold,Clique,Neighbor" \
      --base-dir output/fault_recovery \
      --output output/fault_recovery/figures/
"""

import argparse
import json
import os
import sys
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, MultipleLocator

# ── Style ─────────────────────────────────────────────────
DPI = 150
FIG_SINGLE = (12, 5)
FIG_GROUPED = (12, 5)

REGION_COLORS = plt.cm.tab20(np.linspace(0, 1, 20))

GROUP_COLORS = {
    'leader': '#d65c58',
    'follower': '#0273bc',
    'unaffected': '#2ca02c',
}


def setup_style():
    plt.rcParams.update({
        'font.size': 12,
        'axes.titlesize': 16,
        'axes.labelsize': 14,
        'legend.fontsize': 10,
        'figure.dpi': DPI,
        'savefig.dpi': DPI,
        'savefig.bbox': 'tight',
    })


def tps_formatter(x, _pos):
    if x >= 1000:
        v = x / 1000
        if v == int(v):
            return f'{int(v)}K'
        return f'{v:.1f}K'
    return f'{int(x)}'


# ── Data loading ──────────────────────────────────────────

def load_region_tps(filepath):
    with open(filepath) as f:
        return json.load(f)


def snap_fault_to_samples(fault_events, data):
    """Snap fault_sec/restart_sec to the nearest next sample point."""
    if not fault_events or not data.get('regions'):
        return
    first_rid = next(iter(data['regions']))
    samples = data['regions'][first_rid]
    time_arr = np.array([s['time_sec'] for s in samples])
    if len(time_arr) == 0:
        return
    for evt in fault_events:
        idx = np.searchsorted(time_arr, evt['fault_sec'], side='left')
        if idx < len(time_arr):
            evt['fault_sec'] = time_arr[idx]
        idx = np.searchsorted(time_arr, evt['restart_sec'], side='left')
        if idx < len(time_arr):
            evt['restart_sec'] = time_arr[idx]


def classify_regions(data, fault_events, config_file=None):
    """根据故障事件将 Region 分为 leader/follower/unaffected 组"""
    if not fault_events:
        return {'all': list(data['regions'].keys())}

    # 从 fault_events 中提取故障节点 ID 列表
    fault_node_ids = set()
    for evt in fault_events:
        fault_node_ids.add(evt.get('node_id'))

    # 简单分类：如果 region 在故障期间 TPS 明显下降，则为 affected
    # 更精确的方式需要配置文件中的 leader 信息
    # 这里用数据驱动的方式：比较故障前后 TPS 变化
    region_ids = list(data['regions'].keys())
    interval_sec = data['interval_ms'] / 1000.0

    groups = {'leader': [], 'follower': [], 'unaffected': []}

    first_fault_sec = min(e['fault_sec'] for e in fault_events)
    last_restart_sec = max(e['restart_sec'] for e in fault_events)

    for rid in region_ids:
        samples = data['regions'][rid]
        # 故障前平均 TPS
        pre_tps = []
        fault_tps = []
        for s in samples:
            t = s['time_sec']
            if t < first_fault_sec and t > first_fault_sec - 10:
                pre_tps.append(s['tps'])
            elif first_fault_sec <= t <= last_restart_sec:
                fault_tps.append(s['tps'])

        avg_pre = np.mean(pre_tps) if pre_tps else 0
        avg_fault = np.mean(fault_tps) if fault_tps else 0

        if avg_pre == 0:
            groups['unaffected'].append(rid)
        elif avg_fault < avg_pre * 0.3:
            groups['leader'].append(rid)
        elif avg_fault < avg_pre * 0.7:
            groups['follower'].append(rid)
        else:
            groups['unaffected'].append(rid)

    return groups


# ── Plot functions ────────────────────────────────────────


def plot_all_regions(data, method_name, output_dir, fault_events=None):
    """绘制所有 Region 的独立 TPS 曲线"""
    setup_style()
    fig, ax = plt.subplots(figsize=FIG_SINGLE)

    region_ids = sorted(data['regions'].keys(), key=lambda x: int(x))
    for i, rid in enumerate(region_ids):
        samples = data['regions'][rid]
        times = [s['time_sec'] for s in samples]
        tps_vals = [s['tps'] for s in samples]
        color = REGION_COLORS[i % len(REGION_COLORS)]
        ax.plot(times, tps_vals, linewidth=1.2, color=color,
                label=f'R{rid}', alpha=0.8)

    if fault_events:
        first_fault = min(e['fault_sec'] for e in fault_events)
        last_restart = max(e['restart_sec'] for e in fault_events)
        ax.set_xlim(first_fault - 5, last_restart + 8)
        for evt in fault_events:
            ax.axvline(x=evt['fault_sec'], color='red', linewidth=1.5,
                       linestyle='--', alpha=0.7)
            ax.axvline(x=evt['restart_sec'], color='green', linewidth=1.5,
                       linestyle='--', alpha=0.7)

    ax.set_xlabel('Time (second)', fontweight='bold')
    ax.set_ylabel('TPS', fontweight='bold')
    ax.set_title(f'{method_name} - Per-Region TPS', fontweight='bold')
    ax.yaxis.set_major_formatter(FuncFormatter(tps_formatter))
    ax.grid(True, alpha=0.2)
    ax.set_ylim(0, None)
    ax.legend(loc='upper right', ncol=min(len(region_ids), 6),
              fontsize=8, framealpha=0.9)

    plt.tight_layout()
    name = method_name.lower().replace(' ', '_')
    for ext in ['png', 'pdf']:
        plt.savefig(os.path.join(output_dir, f'region_tps_{name}.{ext}'))
    print(f'  Saved: region_tps_{name}.{{png,pdf}}')
    plt.close()


def plot_grouped_regions(data, method_name, output_dir, fault_events=None):
    """按故障角色分组绘制聚合 TPS 曲线"""
    setup_style()

    groups = classify_regions(data, fault_events)
    if not any(groups.values()):
        return

    fig, ax = plt.subplots(figsize=FIG_GROUPED)
    interval_sec = data['interval_ms'] / 1000.0

    for group_name, region_ids in groups.items():
        if not region_ids:
            continue

        # 聚合该组所有 Region 的 TPS（求和）
        all_times = None
        sum_tps = None
        for rid in region_ids:
            samples = data['regions'][rid]
            times = np.array([s['time_sec'] for s in samples])
            tps_vals = np.array([s['tps'] for s in samples])
            if all_times is None:
                all_times = times
                sum_tps = tps_vals.copy()
            else:
                min_len = min(len(sum_tps), len(tps_vals))
                sum_tps = sum_tps[:min_len] + tps_vals[:min_len]
                all_times = all_times[:min_len]

        if all_times is None:
            continue

        color = GROUP_COLORS.get(group_name, '#808080')
        label = f'{group_name.capitalize()} ({len(region_ids)}R)'
        ax.plot(all_times, sum_tps, linewidth=2, color=color, label=label)

    if fault_events:
        first_fault = min(e['fault_sec'] for e in fault_events)
        last_restart = max(e['restart_sec'] for e in fault_events)
        ax.set_xlim(first_fault - 5, last_restart + 8)
        for evt in fault_events:
            ax.axvline(x=evt['fault_sec'], color='red', linewidth=1.5,
                       linestyle='--', alpha=0.7)
            ax.axvline(x=evt['restart_sec'], color='green', linewidth=1.5,
                       linestyle='--', alpha=0.7)

    ax.set_xlabel('Time (second)', fontweight='bold')
    ax.set_ylabel('TPS (grouped sum)', fontweight='bold')
    ax.set_title(f'{method_name} - Grouped Region Recovery', fontweight='bold')
    ax.yaxis.set_major_formatter(FuncFormatter(tps_formatter))
    ax.grid(True, alpha=0.2)
    ax.set_ylim(0, None)
    ax.legend(loc='upper right', fontsize=12, framealpha=0.9)

    plt.tight_layout()
    name = method_name.lower().replace(' ', '_')
    for ext in ['png', 'pdf']:
        plt.savefig(os.path.join(output_dir, f'region_grouped_{name}.{ext}'))
    print(f'  Saved: region_grouped_{name}.{{png,pdf}}')
    plt.close()


# ── Main ──────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Plot per-region TPS recovery')
    parser.add_argument('--input', help='Single region_tps_samples.json file')
    parser.add_argument('--inputs', help='Comma-separated run dirs (batch mode)')
    parser.add_argument('--labels', help='Comma-separated method labels')
    parser.add_argument('--base-dir', default='', help='Base directory prefix')
    parser.add_argument('--method', default='Method', help='Method name (single mode)')
    parser.add_argument('--output', required=True, help='Output directory')
    args = parser.parse_args()

    os.makedirs(args.output, exist_ok=True)

    if args.input:
        # 单文件模式
        data = load_region_tps(args.input)
        fault_events = data.get('meta', {}).get('fault_events', [])
        snap_fault_to_samples(fault_events, data)
        plot_all_regions(data, args.method, args.output, fault_events)
        plot_grouped_regions(data, args.method, args.output, fault_events)
    elif args.inputs:
        # 批量模式
        dirs = [d.strip() for d in args.inputs.split(',')]
        labels = [l.strip() for l in args.labels.split(',')] if args.labels else dirs

        for run_dir, label in zip(dirs, labels):
            full_path = os.path.join(args.base_dir, run_dir, 'region_tps_samples.json')
            if not os.path.exists(full_path):
                print(f'  SKIP: {full_path} not found')
                continue
            data = load_region_tps(full_path)
            fault_events = data.get('meta', {}).get('fault_events', [])
            snap_fault_to_samples(fault_events, data)
            plot_all_regions(data, label, args.output, fault_events)
            plot_grouped_regions(data, label, args.output, fault_events)
    else:
        print('ERROR: Provide --input or --inputs', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()

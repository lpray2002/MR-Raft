#!/usr/bin/env python3
"""
故障恢复实验绘图脚本（论文级，匹配 region-partitioning/run.py 风格）

一次性生成所有图：
  1. fault_recovery_panel.{png,pdf}     — 2x2 子图
  2. fault_recovery_overlay.{png,pdf}   — 4 条线叠加
  3. fault_recovery_{method}.{png,pdf}  — 各方法单独图

用法:
  python3 scripts/plot_fault_recovery.py \
      --inputs "out/comp_global,out/comp_threshold,out/comp_clique,out/comp_neighbor" \
      --labels "Global,Threshold,Clique,Neighbor" \
      --pre-stable 10 --post-stable 19 \
      --output output/fault_recovery/figures/
"""

import argparse
import json
import os
import glob
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, MaxNLocator

# ── Style (matching region-partitioning/run.py) ─────────────

COLORS = [
    '#0273bc', '#598dca', '#87a8d7', '#b1c4e5', '#d8e1f2',
    '#fbd7d3', '#f2aea8', '#e6867f', '#d65c58', '#c32934'
]

LINE_COLORS = ['#808080', '#d65c58', '#0273bc', '#2ca02c', '#9467bd']
MARKERS = ['s', 's', 's', 's', 's']
DPI = 300
FIG_SINGLE = (6, 5)
FIG_PANEL = (11, 8)


def setup_style():
    plt.rcParams.update({
        'font.size': 13,
        'axes.titlesize': 16,
        'axes.labelsize': 20,
        'axes.labelweight': 'bold',
        'xtick.labelsize': 18,
        'ytick.labelsize': 18,
        'legend.fontsize': 16,
        'figure.dpi': DPI,
        'savefig.dpi': DPI,
        'savefig.bbox': 'tight',
        # 保证导出的 PDF/SVG 中文字为可编辑文字对象（便于手动移动标签、故障线标注）
        'pdf.fonttype': 42,
        'svg.fonttype': 'none',
        'ps.fonttype': 42,
    })


def tps_k_formatter(x, _pos):
    v = x / 1000
    if v == int(v):
        return f'{int(v)}'
    return f'{v:.1f}'


# ── Data loading ────────────────────────────────────────────

def load_experiment(experiment_dir):
    run_dirs = sorted(glob.glob(os.path.join(experiment_dir, 'run_*')))
    all_samples, meta, base_ts = [], None, None
    for rd in run_dirs:
        tf = os.path.join(rd, 'tps_samples.json')
        mf = os.path.join(rd, 'experiment_meta.json')
        if not os.path.exists(tf):
            continue
        with open(tf) as f:
            tps_data = json.load(f)
        all_samples.append(tps_data.get('samples', []))
        if base_ts is None and 'base_ts' in tps_data:
            base_ts = tps_data['base_ts']
        if meta is None and os.path.exists(mf):
            with open(mf) as f:
                meta = json.load(f)
    return all_samples, meta, base_ts


def compute_mean_std(all_samples):
    if not all_samples:
        return np.array([]), np.array([]), np.array([])

    all_times = set()
    for samples in all_samples:
        for s in samples:
            all_times.add(s['time_sec'])

    sorted_times = sorted(all_times)
    if not sorted_times:
        return np.array([]), np.array([]), np.array([])

    time_arr = np.array(sorted_times)
    mat = np.full((len(all_samples), len(sorted_times)), np.nan)
    time_to_idx = {t: i for i, t in enumerate(sorted_times)}

    for i, samples in enumerate(all_samples):
        for s in samples:
            idx = time_to_idx.get(s['time_sec'])
            if idx is not None:
                mat[i, idx] = s['tps']

    return time_arr, np.nanmean(mat, axis=0), np.nanstd(mat, axis=0)


def resample(time_arr, mean_tps, std_tps, interval_sec):
    """Resample time series to a coarser interval by averaging bins."""
    if interval_sec <= 0 or len(time_arr) == 0:
        return time_arr, mean_tps, std_tps
    t_start = time_arr[0]
    t_end = time_arr[-1]
    bins = np.arange(t_start, t_end + interval_sec, interval_sec)
    new_t = []
    new_m = []
    new_s = []
    for i in range(len(bins) - 1):
        mask = (time_arr >= bins[i]) & (time_arr < bins[i + 1])
        if np.any(mask):
            new_t.append((bins[i] + bins[i + 1]) / 2)
            new_m.append(np.nanmean(mean_tps[mask]))
            new_s.append(np.nanmean(std_tps[mask]))
    return np.array(new_t), np.array(new_m), np.array(new_s)


def find_actual_recovery(time_arr, mean_tps, restart_sec, fault_sec, threshold_ratio=0.5):
    """Find the time when TPS actually recovers after a restart command.

    Looks for the first point after restart_sec where TPS >= threshold_ratio
    of the pre-fault stable TPS.
    """
    if time_arr is None or len(time_arr) == 0 or mean_tps is None:
        return restart_sec

    pre_mask = (time_arr >= fault_sec - 5) & (time_arr < fault_sec)
    if np.any(pre_mask):
        stable_tps = np.nanmean(mean_tps[pre_mask])
    else:
        stable_tps = np.nanmean(mean_tps[mean_tps > 0]) if np.any(mean_tps > 0) else 0

    if stable_tps <= 0:
        return restart_sec

    threshold = stable_tps * threshold_ratio
    post_mask = time_arr >= restart_sec
    post_indices = np.where(post_mask)[0]
    for idx in post_indices:
        if mean_tps[idx] >= threshold:
            return time_arr[idx]

    return restart_sec


def get_fault_events(meta, base_ts=None, time_arr=None, mean_tps=None):
    if not meta:
        return []
    if meta.get('multi_fault') and meta.get('fault_events'):
        events = meta['fault_events']
        if base_ts is not None and events and 'fault_epoch' in events[0]:
            for evt in events:
                evt['fault_sec'] = evt['fault_epoch'] - base_ts
                evt['restart_sec'] = evt['restart_epoch'] - base_ts
            if time_arr is not None and len(time_arr) > 0:
                for evt in events:
                    idx = np.searchsorted(time_arr, evt['fault_sec'], side='left')
                    if idx < len(time_arr):
                        evt['fault_sec'] = time_arr[idx]
                    raw_restart = evt['restart_sec']
                    idx = np.searchsorted(time_arr, raw_restart, side='left')
                    if idx < len(time_arr):
                        raw_restart = time_arr[idx]
                    if mean_tps is not None:
                        evt['restart_sec'] = find_actual_recovery(
                            time_arr, mean_tps, raw_restart, evt['fault_sec'])
                    else:
                        evt['restart_sec'] = raw_restart
        return events
    f_sec = meta.get('fault_time_sec', 10)
    dur = meta.get('fault_duration_sec', 5)
    return [{'node_id': meta.get('fault_node_id', '?'),
             'fault_sec': f_sec, 'restart_sec': f_sec + dur,
             'role': 'Node'}]



def compute_window(fault_events, time_arr, pre_stable, post_stable, duration_sec=None):
    if not fault_events or len(time_arr) == 0:
        return 0, len(time_arr)
    first_fault = min(e['fault_sec'] for e in fault_events)
    last_restart = max(e['restart_sec'] for e in fault_events)
    t_start = first_fault - pre_stable
    t_end = last_restart + post_stable
    if duration_sec is not None:
        t_end = min(t_end, duration_sec - 1)
    lo = np.searchsorted(time_arr, t_start, side='left')
    hi = np.searchsorted(time_arr, t_end, side='right')
    return int(lo), int(min(hi, len(time_arr)))


# ── Annotation (no overlap) ────────────────────────────────

def annotate_events(ax, fault_events, x_offset, fontsize=12, compact=False):
    """Draw event lines and place labels above the plot.
    Same event's Fails/re-joins are staggered vertically to avoid overlap."""
    y_upper = 1.15
    y_lower = 1.06

    for i, evt in enumerate(fault_events):
        f_x = evt['fault_sec'] - x_offset
        r_x = evt['restart_sec'] - x_offset
        nid = evt.get('node_id', '?')
        role = evt.get('role', '')

        ax.axvline(x=f_x, color='red', linewidth=1.5, zorder=8)
        ax.axvline(x=r_x, color='#2ca02c', linewidth=1.5, zorder=8)

        fail_label = f'$S_{{{nid}}}$ Fails'
        join_label = f'$S_{{{nid}}}$ re-joins'

        ax.text(f_x, y_upper, fail_label,
                transform=ax.get_xaxis_transform(),
                color='red', fontsize=fontsize, fontweight='bold',
                ha='center', va='bottom', clip_on=False)
        ax.text(r_x, y_lower, join_label,
                transform=ax.get_xaxis_transform(),
                color='#2ca02c', fontsize=fontsize, fontweight='bold',
                ha='center', va='bottom', clip_on=False)



# ── Plot helpers ───────────────────────────────────────────

def plot_method_on_ax(ax, exp_dir, label, color, marker, pre_stable, post_stable,
                      annotate=True, compact_annotations=False, plot_interval=0):
    all_samples, meta, base_ts = load_experiment(exp_dir)
    if not all_samples:
        return None

    time_arr, mean_tps, std_tps = compute_mean_std(all_samples)
    fault_events = get_fault_events(meta, base_ts, time_arr, mean_tps)
    duration_sec = meta.get('duration_sec') if meta else None
    lo, hi = compute_window(fault_events, time_arr, pre_stable, post_stable,
                            duration_sec=duration_sec)

    t = time_arr[lo:hi]
    m = mean_tps[lo:hi]
    s = std_tps[lo:hi]
    if len(t) == 0:
        return None

    if plot_interval > 0:
        t, m, s = resample(t, m, s, plot_interval)
        if len(t) == 0:
            return None

    x_offset = t[0]
    t_shifted = t - x_offset

    ax.plot(t_shifted, m, linestyle='-', color=color,
            linewidth=2, label=label, zorder=5)

    if annotate and fault_events:
        fs = 11 if compact_annotations else 16
        annotate_events(ax, fault_events, x_offset, fontsize=fs,
                        compact=compact_annotations)

    return {'fault_events': fault_events, 'x_offset': x_offset, 't_shifted': t_shifted}


def style_ax(ax, xlim=None):
    ax.set_xlabel('Time sequence (second)', fontweight='bold', fontsize=20)
    ax.set_ylabel('TPS (K)', fontweight='bold', fontsize=20)
    ax.yaxis.set_major_formatter(FuncFormatter(tps_k_formatter))
    ax.yaxis.set_major_locator(MaxNLocator(nbins=6, integer=False))
    ax.set_ylim(0, None)
    ax.grid(True, alpha=0.25, linestyle='-', linewidth=0.5, color='gray')
    ax.tick_params(axis='both', labelsize=18)
    if xlim:
        ax.set_xlim(xlim)


# ── Generate all plots ─────────────────────────────────────

def generate_all(input_dirs, labels, output_dir, pre_stable, post_stable,
                 plot_interval=0):
    setup_style()
    os.makedirs(output_dir, exist_ok=True)

    # ─── 1. Individual method plots ───
    for idx, (exp_dir, label) in enumerate(zip(input_dirs, labels)):
        fig, ax = plt.subplots(figsize=FIG_SINGLE)
        info = plot_method_on_ax(ax, exp_dir, label, LINE_COLORS[idx],
                                 MARKERS[idx], pre_stable, post_stable,
                                 plot_interval=plot_interval)
        style_ax(ax)
        plt.tight_layout()
        name = label.lower().replace('-', '_')
        for ext in ['png', 'pdf']:
            plt.savefig(os.path.join(output_dir, f'fault_recovery_{name}.{ext}'))
        print(f'  Saved: fault_recovery_{name}.{{png,pdf}}')
        plt.close()

    # ─── 2. Panel plot (2x2) ───
    n = len(input_dirs)
    rows = (n + 1) // 2
    fig, axes = plt.subplots(rows, 2, figsize=FIG_PANEL)
    if rows == 1:
        axes = axes.reshape(1, -1)
    axes_flat = axes.flatten()

    for idx, (exp_dir, label) in enumerate(zip(input_dirs, labels)):
        ax = axes_flat[idx]
        plot_method_on_ax(ax, exp_dir, label, LINE_COLORS[idx], MARKERS[idx],
                          pre_stable, post_stable, annotate=True,
                          compact_annotations=True, plot_interval=plot_interval)
        style_ax(ax)
        ax.set_title(label, fontsize=14, fontweight='bold', pad=35)

    for idx in range(n, len(axes_flat)):
        axes_flat[idx].set_visible(False)

    plt.tight_layout(h_pad=4.0, w_pad=2.5)
    for ext in ['png', 'pdf']:
        plt.savefig(os.path.join(output_dir, f'fault_recovery_panel.{ext}'))
    print(f'  Saved: fault_recovery_panel.{{png,pdf}}')
    plt.close()

    # ─── 3. Overlay plot ───
    fig, ax = plt.subplots(figsize=FIG_SINGLE)

    first_info = None
    for idx, (exp_dir, label) in enumerate(zip(input_dirs, labels)):
        info = plot_method_on_ax(ax, exp_dir, label, LINE_COLORS[idx],
                                 MARKERS[idx], pre_stable, post_stable,
                                 annotate=(idx == 0), plot_interval=plot_interval)
        if info and first_info is None:
            first_info = info

    style_ax(ax)
    plt.tight_layout()
    for ext in ['png', 'pdf']:
        plt.savefig(os.path.join(output_dir, f'fault_recovery_overlay.{ext}'))
    print(f'  Saved: fault_recovery_overlay.{{png,pdf}}')
    plt.close()


# ── Main ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Fault recovery plots (generates all figures at once)')
    parser.add_argument('--inputs', required=True,
                        help='Experiment directories, comma-separated')
    parser.add_argument('--labels', required=True,
                        help='Labels, comma-separated')
    parser.add_argument('--pre-stable', type=int, default=10)
    parser.add_argument('--post-stable', type=int, default=19)
    parser.add_argument('--plot-interval', type=float, default=0,
                        help='Resample interval for plotting (seconds). 0=use raw data')
    parser.add_argument('--output', default='output/fault_recovery/figures/')
    args = parser.parse_args()

    input_dirs = [d.strip() for d in args.inputs.split(',')]
    labels = [l.strip() for l in args.labels.split(',')]

    if len(input_dirs) != len(labels):
        print(f'ERROR: {len(input_dirs)} inputs but {len(labels)} labels')
        return

    generate_all(input_dirs, labels, args.output, args.pre_stable, args.post_stable,
                 plot_interval=args.plot_interval)


if __name__ == '__main__':
    main()

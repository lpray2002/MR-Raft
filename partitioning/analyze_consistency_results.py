"""
analyze_consistency_results.py

分析 Handover 一致性验证实验结果，生成论文图表。

Usage:
    python analyze_consistency_results.py [--results-dir output/handover_results]
"""

import os
import sys
import json
import glob
import argparse
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
from collections import defaultdict

plt.rcParams.update({
    'font.size': 11,
    'axes.labelsize': 12,
    'axes.titlesize': 13,
    'legend.fontsize': 10,
    'figure.dpi': 150,
    'savefig.dpi': 300,
})


def load_results(results_dir):
    """加载所有实验结果"""
    files = sorted(glob.glob(os.path.join(results_dir, "result_*.json")))
    results = []
    for f in files:
        with open(f) as fp:
            data = json.load(fp)
        # 从文件名解析实验参数
        basename = os.path.basename(f).replace(".json", "")
        parts = basename.split("_")

        # 支持多种文件名格式：
        #   result_s0_seed42_readindex.json  (多场景模式)
        #   result_single_readindex.json     (单次模式)
        #   result_c8_readindex.json         (并发模式)
        concurrency = 1
        if parts[1] == "single":
            scenario_idx = 0
            seed = 42
            read_mode = parts[2]
        elif parts[1].startswith("c"):
            scenario_idx = 0
            seed = 42
            concurrency = int(parts[1][1:])
            read_mode = parts[2]
        else:
            scenario_idx = int(parts[1][1:])
            seed = int(parts[2][4:])
            read_mode = parts[3]

        # 也从 config/summary 中读取 concurrency 和 actual_tps
        cfg_concurrency = data.get("config", {}).get("concurrency", concurrency)
        actual_tps = data.get("summary", {}).get("actual_tps", 0)

        data["_meta"] = {
            "file": f,
            "scenario_idx": scenario_idx,
            "seed": seed,
            "read_mode": read_mode,
            "concurrency": cfg_concurrency or concurrency,
            "actual_tps": actual_tps,
        }
        results.append(data)

    print(f"  Loaded {len(results)} result files from {results_dir}")
    return results


def compute_metrics(results):
    """计算每组实验的指标"""
    metrics = []
    for r in results:
        meta = r["_meta"]
        records = r["records"]
        summary = r["summary"]

        # 按类型分组
        writes = [rec for rec in records if rec["type"] == "write"]
        reads = [rec for rec in records if rec["type"] == "read"]
        handovers = [rec for rec in records if rec["type"] == "handover"]

        # Handover 后的第一次读取
        ho_first_reads = []
        ho_times = [h["time_ms"] for h in handovers]
        for read in reads:
            if read.get("is_handover"):
                # 找到最近的 handover 事件
                for ht in ho_times:
                    if 0 < read["time_ms"] - ht < 2000:
                        ho_first_reads.append(read)
                        break

        # 不一致窗口（StaleRead 模式下）
        # consistent 字段缺失时视为不一致（旧版 omitempty 导致 false 被省略）
        inconsistent_reads = [r for r in reads if not r.get("consistent", False)]
        stale_window_ms = 0
        if inconsistent_reads:
            first_inconsistent = min(r["time_ms"] for r in inconsistent_reads)
            last_inconsistent = max(r["time_ms"] for r in inconsistent_reads)
            stale_window_ms = last_inconsistent - first_inconsistent

        m = {
            "scenario_idx": meta["scenario_idx"],
            "seed": meta["seed"],
            "read_mode": meta["read_mode"],
            "concurrency": meta.get("concurrency", 1),
            "actual_tps": meta.get("actual_tps", summary.get("actual_tps", 0)),
            "write_success_rate": summary["write_success_rate"],
            "read_consistency_rate": summary["read_consistency_rate"],
            "ho_consistency_rate": summary.get("handover_consistency_rate", 1.0),
            "avg_write_latency_us": summary["avg_write_latency_us"],
            "avg_read_latency_us": summary["avg_read_latency_us"],
            "total_writes": summary["total_writes"],
            "total_reads": summary["total_reads"],
            "handovers": summary["handovers"],
            "ho_first_reads": len(ho_first_reads),
            "ho_first_consistent": sum(1 for r in ho_first_reads if r.get("consistent")),
            "inconsistent_count": len(inconsistent_reads),
            "stale_window_ms": stale_window_ms,
        }

        # 延迟分布
        write_latencies = [w["latency_us"] for w in writes if w.get("success")]
        read_latencies = [r["latency_us"] for r in reads]
        ho_read_latencies = [r["latency_us"] for r in reads if r.get("is_handover")]
        stable_read_latencies = [r["latency_us"] for r in reads if not r.get("is_handover")]

        m["write_latencies"] = write_latencies
        m["read_latencies"] = read_latencies
        m["ho_read_latencies"] = ho_read_latencies
        m["stable_read_latencies"] = stable_read_latencies

        metrics.append(m)

    return metrics


def plot_consistency_rate(metrics, output_dir):
    """Fig A: 数据一致率 vs 场景（READ_INDEX vs STALE_READ）"""
    fig, ax = plt.subplots(figsize=(8, 5))

    scenarios = sorted(set(m["scenario_idx"] for m in metrics))
    scenario_labels = [f"S{i+1}" for i in scenarios]

    readindex_rates = []
    stale_rates = []

    for s in scenarios:
        ri = [m["read_consistency_rate"] for m in metrics
              if m["scenario_idx"] == s and m["read_mode"] == "readindex"]
        st = [m["read_consistency_rate"] for m in metrics
              if m["scenario_idx"] == s and m["read_mode"] == "stale"]
        readindex_rates.append(np.mean(ri) if ri else 0)
        stale_rates.append(np.mean(st) if st else 0)

    x = np.arange(len(scenarios))
    width = 0.35

    bars1 = ax.bar(x - width/2, readindex_rates, width, label='ReadIndex (Linearizable)',
                   color='#2196F3', edgecolor='#1565C0', linewidth=0.8)
    bars2 = ax.bar(x + width/2, stale_rates, width, label='StaleRead (Eventual)',
                   color='#FF9800', edgecolor='#E65100', linewidth=0.8)

    ax.set_xlabel('Scenario')
    ax.set_ylabel('Data Consistency Rate')
    ax.set_title('Data Consistency Rate: ReadIndex vs StaleRead')
    ax.set_xticks(x)
    ax.set_xticklabels(scenario_labels)
    ax.set_ylim(0.95, 1.005)
    ax.legend(loc='lower left')
    ax.grid(axis='y', alpha=0.3)
    ax.axhline(y=1.0, color='green', linestyle='--', alpha=0.5, linewidth=0.8)

    # 标注数值
    for bar in bars1:
        h = bar.get_height()
        ax.annotate(f'{h:.4f}', xy=(bar.get_x() + bar.get_width()/2, h),
                    xytext=(0, 3), textcoords="offset points",
                    ha='center', va='bottom', fontsize=8)
    for bar in bars2:
        h = bar.get_height()
        ax.annotate(f'{h:.4f}', xy=(bar.get_x() + bar.get_width()/2, h),
                    xytext=(0, 3), textcoords="offset points",
                    ha='center', va='bottom', fontsize=8)

    path = os.path.join(output_dir, "fig_a_consistency_rate.pdf")
    fig.savefig(path)
    plt.close(fig)
    print(f"  Saved: {path}")


def plot_latency_timeline(metrics, results, output_dir):
    """Fig B: Handover 前后的验证延迟时间序列"""
    # 选一个有代表性的实验（scenario 0, seed 42, readindex）
    target = None
    for r in results:
        meta = r["_meta"]
        if meta["scenario_idx"] == 0 and meta["seed"] == 42 and meta["read_mode"] == "readindex":
            target = r
            break

    if target is None:
        print("  ⚠ No suitable result for timeline plot")
        return

    records = target["records"]
    reads = [rec for rec in records if rec["type"] == "read"]
    handovers = [rec for rec in records if rec["type"] == "handover"]

    fig, ax = plt.subplots(figsize=(12, 5))

    # 读取延迟时间序列
    times = [r["time_ms"] / 1000 for r in reads]
    latencies = [r["latency_us"] / 1000 for r in reads]  # 转 ms
    consistent = [r.get("consistent", True) for r in reads]

    colors = ['#4CAF50' if c else '#F44336' for c in consistent]
    ax.scatter(times, latencies, c=colors, s=8, alpha=0.6, zorder=3)

    # 标记 Handover 时刻
    for ho in handovers:
        t = ho["time_ms"] / 1000
        ax.axvline(x=t, color='red', linestyle='--', alpha=0.5, linewidth=1)
        ax.annotate(f'HO\n{ho["from_node"]}→{ho["to_node"]}',
                    xy=(t, ax.get_ylim()[1] * 0.9 if ax.get_ylim()[1] > 0 else 10),
                    fontsize=7, ha='center', color='red')

    ax.set_xlabel('Time (s)')
    ax.set_ylabel('Read Latency (ms)')
    ax.set_title('Read Latency Timeline with Handover Events')
    ax.grid(alpha=0.3)

    legend_elements = [
        Patch(facecolor='#4CAF50', label='Consistent'),
        Patch(facecolor='#F44336', label='Inconsistent'),
        plt.Line2D([0], [0], color='red', linestyle='--', label='Handover'),
    ]
    ax.legend(handles=legend_elements, loc='upper right')

    path = os.path.join(output_dir, "fig_b_latency_timeline.pdf")
    fig.savefig(path)
    plt.close(fig)
    print(f"  Saved: {path}")


def plot_ho_latency_comparison(metrics, output_dir):
    """Fig C: 稳定期 vs Handover 后的读延迟对比"""
    fig, ax = plt.subplots(figsize=(8, 5))

    # 只看 ReadIndex 模式
    ri_metrics = [m for m in metrics if m["read_mode"] == "readindex"]

    stable_latencies = []
    ho_latencies = []
    for m in ri_metrics:
        stable_latencies.extend(m["stable_read_latencies"])
        ho_latencies.extend(m["ho_read_latencies"])

    if not stable_latencies or not ho_latencies:
        print("  ⚠ Not enough data for latency comparison")
        return

    # CDF
    stable_sorted = np.sort(stable_latencies) / 1000  # µs → ms
    ho_sorted = np.sort(ho_latencies) / 1000

    stable_cdf = np.arange(1, len(stable_sorted) + 1) / len(stable_sorted)
    ho_cdf = np.arange(1, len(ho_sorted) + 1) / len(ho_sorted)

    ax.plot(stable_sorted, stable_cdf, label=f'Stable ({len(stable_sorted)} reads)',
            color='#2196F3', linewidth=2)
    ax.plot(ho_sorted, ho_cdf, label=f'Post-Handover ({len(ho_sorted)} reads)',
            color='#F44336', linewidth=2)

    # P50, P95 标注
    p50_stable = np.percentile(stable_sorted, 50)
    p95_stable = np.percentile(stable_sorted, 95)
    p50_ho = np.percentile(ho_sorted, 50)
    p95_ho = np.percentile(ho_sorted, 95)

    ax.axhline(y=0.5, color='gray', linestyle=':', alpha=0.5)
    ax.axhline(y=0.95, color='gray', linestyle=':', alpha=0.5)
    ax.text(ax.get_xlim()[1] * 0.7 if ax.get_xlim()[1] > 0 else 5, 0.52,
            f'P50: stable={p50_stable:.1f}ms, HO={p50_ho:.1f}ms', fontsize=8)
    ax.text(ax.get_xlim()[1] * 0.7 if ax.get_xlim()[1] > 0 else 5, 0.97,
            f'P95: stable={p95_stable:.1f}ms, HO={p95_ho:.1f}ms', fontsize=8)

    ax.set_xlabel('Read Latency (ms)')
    ax.set_ylabel('CDF')
    ax.set_title('Read Latency CDF: Stable vs Post-Handover (ReadIndex)')
    ax.legend(loc='lower right')
    ax.grid(alpha=0.3)

    path = os.path.join(output_dir, "fig_c_latency_cdf.pdf")
    fig.savefig(path)
    plt.close(fig)
    print(f"  Saved: {path}")


def plot_stale_window(metrics, output_dir):
    """Fig D: StaleRead 不一致窗口分析"""
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))

    stale_metrics = [m for m in metrics if m["read_mode"] == "stale"]
    ri_metrics = [m for m in metrics if m["read_mode"] == "readindex"]

    # 左图：不一致次数 per scenario
    scenarios = sorted(set(m["scenario_idx"] for m in metrics))
    scenario_labels = [f"S{i+1}" for i in scenarios]

    stale_inconsistent = []
    ri_inconsistent = []
    for s in scenarios:
        st = [m["inconsistent_count"] for m in stale_metrics if m["scenario_idx"] == s]
        ri = [m["inconsistent_count"] for m in ri_metrics if m["scenario_idx"] == s]
        stale_inconsistent.append(np.mean(st) if st else 0)
        ri_inconsistent.append(np.mean(ri) if ri else 0)

    x = np.arange(len(scenarios))
    width = 0.35
    ax1.bar(x - width/2, ri_inconsistent, width, label='ReadIndex',
            color='#2196F3', edgecolor='#1565C0')
    ax1.bar(x + width/2, stale_inconsistent, width, label='StaleRead',
            color='#FF9800', edgecolor='#E65100')
    ax1.set_xlabel('Scenario')
    ax1.set_ylabel('Inconsistent Reads (count)')
    ax1.set_title('Inconsistent Read Count')
    ax1.set_xticks(x)
    ax1.set_xticklabels(scenario_labels)
    ax1.legend()
    ax1.grid(axis='y', alpha=0.3)

    # 右图：不一致窗口持续时间
    stale_windows = []
    for s in scenarios:
        ws = [m["stale_window_ms"] for m in stale_metrics if m["scenario_idx"] == s]
        stale_windows.append(np.mean(ws) if ws else 0)

    ax2.bar(scenario_labels, stale_windows, color='#FF9800', edgecolor='#E65100')
    ax2.set_xlabel('Scenario')
    ax2.set_ylabel('Inconsistency Window (ms)')
    ax2.set_title('StaleRead Inconsistency Window Duration')
    ax2.grid(axis='y', alpha=0.3)

    fig.tight_layout()
    path = os.path.join(output_dir, "fig_d_stale_window.pdf")
    fig.savefig(path)
    plt.close(fig)
    print(f"  Saved: {path}")


def plot_tps_consistency(metrics, output_dir):
    """Fig E: TPS vs 一致率曲线（不同并发度）"""
    # 按 (concurrency, read_mode) 分组
    has_multi = len(set(m["concurrency"] for m in metrics)) > 1
    has_tps = any(m.get("actual_tps", 0) > 0 for m in metrics)
    if not has_multi and not has_tps:
        return

    fig, ax = plt.subplots(figsize=(8, 5))

    for mode, color, marker in [("readindex", "#2196F3", "o"), ("stale", "#FF9800", "s")]:
        mode_metrics = [m for m in metrics if m["read_mode"] == mode]
        if not mode_metrics:
            continue

        tps_vals = []
        consistency_vals = []
        ho_consistency_vals = []

        for m in sorted(mode_metrics, key=lambda x: x.get("actual_tps", x["concurrency"])):
            x_val = m.get("actual_tps", 0) or m["concurrency"] * 100
            tps_vals.append(x_val)
            consistency_vals.append(m["read_consistency_rate"] * 100)
            ho_consistency_vals.append(m["ho_consistency_rate"] * 100)

        label_mode = "ReadIndex" if mode == "readindex" else "StaleRead"
        ax.plot(tps_vals, consistency_vals, f'-{marker}', color=color,
                linewidth=2, markersize=8, label=f'{label_mode} (all reads)')
        ax.plot(tps_vals, ho_consistency_vals, f'--{marker}', color=color,
                linewidth=1.5, markersize=6, alpha=0.7,
                label=f'{label_mode} (HO reads)')

    ax.set_xlabel('Actual TPS (writes/s)')
    ax.set_ylabel('Consistency Rate (%)')
    ax.set_title('Consistency Rate vs Throughput')
    ax.legend(loc='lower left')
    ax.grid(alpha=0.3)
    ax.set_ylim(bottom=max(0, ax.get_ylim()[0] - 5))

    path = os.path.join(output_dir, "fig_e_tps_consistency.pdf")
    fig.savefig(path, bbox_inches='tight')
    plt.close(fig)
    print(f"  Saved: {path}")


def print_summary_table(metrics):
    """打印汇总表格"""
    has_concurrency = any(m.get("concurrency", 1) > 1 for m in metrics)

    print("\n  ╔═══════════════════════════════════════════════════════════════════════════╗")
    print("  ║  Experiment Summary                                                      ║")
    print("  ╠═══════════════════════════════════════════════════════════════════════════╣")
    if has_concurrency:
        print(f"  ║  {'Scen':<5} {'Mode':<10} {'C':<4} {'TPS':<8} {'Write%':<8} {'Read%':<8} {'HO%':<8} {'Lat':<8} ║")
    else:
        print(f"  ║  {'Scenario':<10} {'Mode':<12} {'Write%':<8} {'Read%':<8} {'HO%':<8} {'AvgLat':<10} ║")
    print("  ╠═══════════════════════════════════════════════════════════════════════════╣")

    grouped = defaultdict(list)
    for m in metrics:
        key = (m["scenario_idx"], m["read_mode"], m.get("concurrency", 1))
        grouped[key].append(m)

    for (sidx, mode, conc), group in sorted(grouped.items()):
        wr = np.mean([m["write_success_rate"] for m in group])
        rr = np.mean([m["read_consistency_rate"] for m in group])
        hr = np.mean([m["ho_consistency_rate"] for m in group])
        lat = np.mean([m["avg_read_latency_us"] for m in group]) / 1000
        tps = np.mean([m.get("actual_tps", 0) for m in group])

        if has_concurrency:
            print(f"  ║  S{sidx+1:<4} {mode:<10} {conc:<4} {tps:>6.0f}  {wr*100:>6.2f}% {rr*100:>6.2f}% {hr*100:>6.2f}% {lat:>5.1f}ms ║")
        else:
            print(f"  ║  S{sidx+1:<9} {mode:<12} {wr*100:>6.2f}% {rr*100:>6.2f}% {hr*100:>6.2f}% {lat:>7.2f}ms ║")

    print("  ╚═══════════════════════════════════════════════════════════════════════════╝")


def main():
    parser = argparse.ArgumentParser(description="Analyze handover consistency results")
    parser.add_argument("--results-dir", type=str,
                        default="/Users/lpray/workspace/work/2025_10_23_multi-region-raft/output/handover_results",
                        help="Directory containing result JSON files")
    parser.add_argument("--output-dir", type=str,
                        default="/Users/lpray/workspace/work/2025_10_22_region-partitioning/output/handover_figures",
                        help="Directory for output figures")
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    print("=" * 60)
    print("  Analyze Handover Consistency Results")
    print("=" * 60)

    # 加载结果
    results = load_results(args.results_dir)
    if not results:
        print("  No results found!")
        return

    # 计算指标
    metrics = compute_metrics(results)

    # 打印汇总
    print_summary_table(metrics)

    # 生成图表
    print(f"\n  Generating figures to {args.output_dir}/...")
    plot_consistency_rate(metrics, args.output_dir)
    plot_latency_timeline(metrics, results, args.output_dir)
    plot_ho_latency_comparison(metrics, args.output_dir)
    plot_stale_window(metrics, args.output_dir)
    plot_tps_consistency(metrics, args.output_dir)

    print(f"\n{'='*60}")
    print(f"  Done! 4 figures saved to {args.output_dir}/")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()

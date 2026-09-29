#!/usr/bin/env python3
"""
绘制 Raft 网络延迟 vs TPS 对比图
数据来源: AutoDL 32核服务器全拓扑压测 (15节点, 每节点30并发, 40s/组)
输出: 3幅竖排子图 (0.2-2ms, 2-10ms, 10-30ms)
"""

import csv
import os
import matplotlib.pyplot as plt
import matplotlib
matplotlib.use('Agg')

# 数据路径
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(SCRIPT_DIR, "output", "delay_benchmark_results.csv")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output", "delay_figures")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# 读取 CSV
data = []
with open(CSV_PATH) as f:
    reader = csv.DictReader(f)
    for row in reader:
        data.append({
            "method": row["method"],
            "delay": float(row["raft_delay_ms"]),
            "tps": float(row["tps"]),
            "latency": float(row["avg_latency_ms"]),
            "success_rate": float(row["success_rate"]),
        })

print(f"读取 {len(data)} 条记录")

# 按方法分组并排序
methods_data = {}
for d in data:
    methods_data.setdefault(d["method"], []).append(d)
for m in methods_data:
    methods_data[m].sort(key=lambda x: x["delay"])

# 样式配置
METHOD_CFG = {
    "threshold": {"label": "Threshold", "color": "#1976D2", "marker": "o"},
    "clique":    {"label": "Clique",    "color": "#D32F2F", "marker": "s"},
    "neighbor":  {"label": "Neighbor",  "color": "#388E3C", "marker": "^"},
    "global":    {"label": "Global",    "color": "#7B1FA2", "marker": "D"},
}
METHOD_ORDER = ["threshold", "clique", "neighbor", "global"]

# 3 个延迟范围
RANGES = [
    (0.2, 2.0),
    (2.0, 10.0),
    (10.0, 30.0),
]

# ============ 绘制竖排 3 幅子图 ============
fig, axes = plt.subplots(3, 1, figsize=(6, 10))

for idx, (dmin, dmax) in enumerate(RANGES):
    ax = axes[idx]
    for method in METHOD_ORDER:
        if method not in methods_data:
            continue
        cfg = METHOD_CFG[method]
        pts = [d for d in methods_data[method] if dmin <= d["delay"] <= dmax]
        if not pts:
            continue
        delays = [p["delay"] for p in pts]
        tps_vals = [p["tps"] for p in pts]
        ax.plot(delays, tps_vals,
                marker=cfg["marker"], color=cfg["color"],
                linewidth=1.8, markersize=5, alpha=0.9)

    ax.set_xlabel("Network Latency (ms)", fontsize=10)
    ax.set_ylabel("Transactions per second", fontsize=10)
    ax.grid(True, alpha=0.25, linestyle='--')
    margin = (dmax - dmin) * 0.03
    ax.set_xlim(dmin - margin, dmax + margin)
    ax.set_ylim(bottom=0)

# 图例放在最上方，单独一行
handles = []
labels = []
for method in METHOD_ORDER:
    if method not in methods_data:
        continue
    cfg = METHOD_CFG[method]
    h, = axes[0].plot([], [], marker=cfg["marker"], color=cfg["color"],
                      linewidth=1.8, markersize=5, label=cfg["label"])
    handles.append(h)
    labels.append(cfg["label"])

fig.legend(handles, labels, loc='upper center', ncol=4, fontsize=9,
           bbox_to_anchor=(0.5, 0.995), frameon=True, framealpha=0.9,
           edgecolor='#cccccc')

plt.tight_layout(rect=[0, 0, 1, 0.96])
for ext in ['png', 'pdf']:
    out_path = os.path.join(OUTPUT_DIR, f"delay_vs_tps.{ext}")
    plt.savefig(out_path, dpi=200, bbox_inches='tight')
    print(f"图表已保存: {out_path}")
plt.close()

# ============ 延迟-响应时间图（同样竖排） ============
fig2, axes2 = plt.subplots(3, 1, figsize=(6, 10))

for idx, (dmin, dmax) in enumerate(RANGES):
    ax = axes2[idx]
    for method in METHOD_ORDER:
        if method not in methods_data:
            continue
        cfg = METHOD_CFG[method]
        pts = [d for d in methods_data[method] if dmin <= d["delay"] <= dmax]
        if not pts:
            continue
        delays = [p["delay"] for p in pts]
        latencies = [p["latency"] for p in pts]
        ax.plot(delays, latencies,
                marker=cfg["marker"], color=cfg["color"],
                linewidth=1.8, markersize=5, alpha=0.9)

    ax.set_xlabel("Network Latency (ms)", fontsize=10)
    ax.set_ylabel("Avg Response Latency (ms)", fontsize=10)
    ax.grid(True, alpha=0.25, linestyle='--')
    margin = (dmax - dmin) * 0.03
    ax.set_xlim(dmin - margin, dmax + margin)
    ax.set_ylim(bottom=0)

fig2.legend(handles, labels, loc='upper center', ncol=4, fontsize=9,
            bbox_to_anchor=(0.5, 0.995), frameon=True, framealpha=0.9,
            edgecolor='#cccccc')

plt.tight_layout(rect=[0, 0, 1, 0.96])
for ext in ['png', 'pdf']:
    out_path2 = os.path.join(OUTPUT_DIR, f"delay_vs_latency.{ext}")
    plt.savefig(out_path2, dpi=200, bbox_inches='tight')
    print(f"图表已保存: {out_path2}")
plt.close()

# ============ 单图绘制：每个延迟范围分别出 TPS 和 Latency 图 ============
RANGE_NAMES = ["low", "mid", "high"]

# 论文风格参数（与阈值分析图统一）
LABEL_FS = 20
TICK_FS = 18
LEGEND_FS = 14
LINE_W = 3.5
MARKER_S = 10
FIG_SIZE_SINGLE = (7, 5)

for idx, (dmin, dmax) in enumerate(RANGES):
    rname = RANGE_NAMES[idx]

    # TPS 单图
    fig_s, ax_s = plt.subplots(figsize=FIG_SIZE_SINGLE)
    for method in METHOD_ORDER:
        if method not in methods_data:
            continue
        cfg = METHOD_CFG[method]
        pts = [d for d in methods_data[method] if dmin <= d["delay"] <= dmax]
        if not pts:
            continue
        delays = [p["delay"] for p in pts]
        tps_vals = [p["tps"] for p in pts]
        ax_s.plot(delays, tps_vals,
                  marker=cfg["marker"], color=cfg["color"],
                  linewidth=LINE_W, markersize=MARKER_S, alpha=0.9,
                  label=cfg["label"])
    ax_s.set_xlabel("Network Latency (ms)", fontsize=LABEL_FS, fontweight='bold')
    ax_s.set_ylabel("Transactions per second", fontsize=LABEL_FS, fontweight='bold')
    ax_s.grid(True, alpha=0.7, linestyle='--', linewidth=0.8)
    ax_s.tick_params(axis='both', labelsize=TICK_FS)
    margin = (dmax - dmin) * 0.03
    ax_s.set_xlim(dmin - margin, dmax + margin)
    ax_s.set_ylim(bottom=0)
    ax_s.legend(loc='lower center', bbox_to_anchor=(0.5, 1.01), ncol=4,
                prop={'weight': 'bold', 'size': LEGEND_FS},
                framealpha=0.95, columnspacing=1.0, handletextpad=0.4)
    plt.tight_layout()
    for ext in ['png', 'pdf']:
        p = os.path.join(OUTPUT_DIR, f"delay_vs_tps_{rname}.{ext}")
        plt.savefig(p, dpi=300, bbox_inches='tight')
    print(f"  Saved: delay_vs_tps_{rname}.{{png,pdf}}")
    plt.close()

    # Latency 单图
    fig_s, ax_s = plt.subplots(figsize=FIG_SIZE_SINGLE)
    for method in METHOD_ORDER:
        if method not in methods_data:
            continue
        cfg = METHOD_CFG[method]
        pts = [d for d in methods_data[method] if dmin <= d["delay"] <= dmax]
        if not pts:
            continue
        delays = [p["delay"] for p in pts]
        latencies = [p["latency"] for p in pts]
        ax_s.plot(delays, latencies,
                  marker=cfg["marker"], color=cfg["color"],
                  linewidth=LINE_W, markersize=MARKER_S, alpha=0.9,
                  label=cfg["label"])
    ax_s.set_xlabel("Network Latency (ms)", fontsize=LABEL_FS, fontweight='bold')
    ax_s.set_ylabel("Avg Response Latency (ms)", fontsize=LABEL_FS, fontweight='bold')
    ax_s.grid(True, alpha=0.7, linestyle='--', linewidth=0.8)
    ax_s.tick_params(axis='both', labelsize=TICK_FS)
    margin = (dmax - dmin) * 0.03
    ax_s.set_xlim(dmin - margin, dmax + margin)
    ax_s.set_ylim(bottom=0)
    ax_s.legend(loc='lower center', bbox_to_anchor=(0.5, 1.01), ncol=4,
                prop={'weight': 'bold', 'size': LEGEND_FS},
                framealpha=0.95, columnspacing=1.0, handletextpad=0.4)
    plt.tight_layout()
    for ext in ['png', 'pdf']:
        p = os.path.join(OUTPUT_DIR, f"delay_vs_latency_{rname}.{ext}")
        plt.savefig(p, dpi=300, bbox_inches='tight')
    print(f"  Saved: delay_vs_latency_{rname}.{{png,pdf}}")
    plt.close()

print("\n=== 关键数据摘要 ===")
for method in METHOD_ORDER:
    if method not in methods_data:
        continue
    pts = methods_data[method]
    d0 = next((p for p in pts if p["delay"] == 0), None)
    d2 = next((p for p in pts if p["delay"] == 2.0), None)
    d10 = next((p for p in pts if p["delay"] == 10.0), None)
    d30 = next((p for p in pts if p["delay"] == 30.0), None)
    print(f"\n{METHOD_CFG[method]['label']}:")
    if d0:  print(f"  0ms:  TPS={d0['tps']:.0f}, Latency={d0['latency']:.1f}ms")
    if d2:  print(f"  2ms:  TPS={d2['tps']:.0f}, Latency={d2['latency']:.1f}ms")
    if d10: print(f"  10ms: TPS={d10['tps']:.0f}, Latency={d10['latency']:.1f}ms")
    if d30: print(f"  30ms: TPS={d30['tps']:.0f}, Latency={d30['latency']:.1f}ms")

#!/usr/bin/env python3
"""绘制延迟-TPS对比图（3幅：0.2-2ms, 2-10ms, 10-30ms）"""

import json
import re
import matplotlib.pyplot as plt
import numpy as np
import os

# 解析结果数据
results_file = '/tmp/autodl_results_raw.txt'
with open(results_file) as f:
    raw = f.read()

results = []
for m in re.finditer(r'\{[^{}]+\}', raw):
    try:
        results.append(json.loads(m.group()))
    except:
        pass

print(f"解析到 {len(results)} 组结果")

# 按方法分组
methods_data = {}
for r in results:
    method = r["method"]
    if method not in methods_data:
        methods_data[method] = []
    methods_data[method].append({
        "delay": r["raft_delay_ms"],
        "tps": r["tps"],
        "latency": r["avg_latency_ms"],
        "success_rate": r["success_rate"],
    })

# 排序
for method in methods_data:
    methods_data[method].sort(key=lambda x: x["delay"])

# 方法显示名和颜色
method_labels = {
    "threshold": "Threshold (5 Regions)",
    "neighbor": "Neighbor (9 Regions)",
    "clique": "Clique (11 Regions)",
    "global": "Global (1 Region)",
}
method_colors = {
    "threshold": "#2196F3",
    "neighbor": "#4CAF50",
    "clique": "#FF9800",
    "global": "#F44336",
}
method_markers = {
    "threshold": "o",
    "neighbor": "s",
    "clique": "^",
    "global": "D",
}

# 定义3个延迟范围
ranges = [
    (0.2, 2.0, "Sub-millisecond (0.2-2ms)"),
    (2.0, 10.0, "LAN-level (2-10ms)"),
    (10.0, 30.0, "WAN-level (10-30ms)"),
]

output_dir = os.path.dirname(os.path.abspath(__file__)) + "/../output/delay_benchmark"
os.makedirs(output_dir, exist_ok=True)

fig, axes = plt.subplots(1, 3, figsize=(18, 5.5))
plt.rcParams['font.size'] = 10

for idx, (dmin, dmax, title) in enumerate(ranges):
    ax = axes[idx]

    for method in ["threshold", "clique", "neighbor", "global"]:
        if method not in methods_data:
            continue
        data = methods_data[method]
        delays = [d["delay"] for d in data if dmin <= d["delay"] <= dmax]
        tps = [d["tps"] / 1000 for d in data if dmin <= d["delay"] <= dmax]

        if delays:
            ax.plot(delays, tps,
                    marker=method_markers[method],
                    color=method_colors[method],
                    label=method_labels[method],
                    linewidth=2, markersize=6)

    ax.set_xlabel("Raft Network Delay (ms)")
    ax.set_ylabel("TPS (×1000 ops/s)")
    ax.set_title(title)
    ax.legend(loc="best", fontsize=9)
    ax.grid(True, alpha=0.3)
    ax.set_xlim(dmin - (dmax - dmin) * 0.05, dmax + (dmax - dmin) * 0.05)

plt.tight_layout()
output_path = os.path.join(output_dir, "delay_tps_comparison.png")
plt.savefig(output_path, dpi=150, bbox_inches='tight')
print(f"图表已保存: {output_path}")

# 也保存原始数据为 CSV
csv_path = os.path.join(output_dir, "delay_benchmark_results.csv")
with open(csv_path, 'w') as f:
    f.write("method,raft_delay_ms,tps,avg_latency_ms,success_rate\n")
    for r in results:
        f.write(f"{r['method']},{r['raft_delay_ms']},{r['tps']:.2f},{r['avg_latency_ms']:.3f},{r['success_rate']:.4f}\n")
print(f"CSV 已保存: {csv_path}")

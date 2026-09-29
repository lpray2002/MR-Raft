#!/usr/bin/env python3
"""
从 region_stats.jsonl 生成 tps_samples.json（兼容 plot_fault_recovery.py 格式）

输入: run_dir 下的 node_*_region_stats*.jsonl 文件
输出: run_dir/tps_samples.json

格式:
{
  "samples": [
    {"time_sec": 0.2, "success_in_sec": 100, "failed_in_sec": 5, "tps": 500.0, "cum_success": 100, "cum_failed": 5},
    ...
  ],
  "summary": {"total_success": ..., "total_failed": ..., "avg_tps": ..., "elapsed_sec": ...}
}
"""

import argparse
import glob
import json
import os
import sys
from collections import defaultdict


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-dir', required=True, help='实验 run 目录')
    parser.add_argument('--output', default=None, help='输出文件路径（默认: run_dir/tps_samples.json）')
    args = parser.parse_args()

    run_dir = args.run_dir
    output_file = args.output or os.path.join(run_dir, 'tps_samples.json')

    # 加载所有 region_stats jsonl 文件
    patterns = [
        os.path.join(run_dir, 'node_*_region_stats.jsonl'),
        os.path.join(run_dir, 'node_*_region_stats_restart*.jsonl'),
    ]
    all_records = []
    for pattern in patterns:
        for filepath in glob.glob(pattern):
            with open(filepath) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            all_records.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass

    if not all_records:
        print(f"  WARNING: No region_stats records found in {run_dir}", file=sys.stderr)
        # 写空结果
        with open(output_file, 'w') as f:
            json.dump({"samples": [], "summary": {"total_success": 0, "total_failed": 0, "avg_tps": 0, "elapsed_sec": 0}}, f)
        return

    # 获取采样间隔
    interval_ms = int(all_records[0].get('elapsed_ms', 200))
    interval_sec = interval_ms / 1000.0

    # 用最小 ts 作为基准
    all_ts = [rec['ts'] for rec in all_records]
    base_ts = min(all_ts)

    # 按时间 slot 聚合：每个节点取 max region success（= 客户端请求数）
    slot_data = defaultdict(lambda: {'success': 0, 'failed': 0})

    for rec in all_records:
        relative = rec['ts'] - base_ts
        slot = round(relative / interval_sec)
        regions = rec.get('regions', {})
        if regions:
            max_success = max(counts.get('success', 0) for counts in regions.values())
            max_failed = max(counts.get('failed', 0) for counts in regions.values())
            slot_data[slot]['success'] += max_success
            slot_data[slot]['failed'] += max_failed

    if not slot_data:
        with open(output_file, 'w') as f:
            json.dump({"samples": [], "summary": {"total_success": 0, "total_failed": 0, "avg_tps": 0, "elapsed_sec": 0}}, f)
        return

    # 找到第一个有数据的 slot 和最后一个 slot
    sorted_slots = sorted(slot_data.keys())
    first_active_slot = sorted_slots[0]
    for slot in sorted_slots:
        total = slot_data[slot]['success'] + slot_data[slot]['failed']
        if total > 0:
            first_active_slot = slot
            break
    last_slot = sorted_slots[-1]

    # 生成 samples（从 first_active_slot 开始，time_sec 从 interval_sec 开始）
    samples = []
    cum_success = 0
    cum_failed = 0
    for slot in range(first_active_slot, last_slot + 1):
        s = slot_data[slot]['success']
        f = slot_data[slot]['failed']
        cum_success += s
        cum_failed += f
        tps = s / interval_sec
        time_sec = (slot - first_active_slot + 1) * interval_sec
        samples.append({
            'time_sec': round(time_sec, 3),
            'success_in_sec': s,
            'failed_in_sec': f,
            'tps': round(tps, 1),
            'cum_success': cum_success,
            'cum_failed': cum_failed,
        })

    elapsed_sec = len(samples) * interval_sec
    avg_tps = cum_success / elapsed_sec if elapsed_sec > 0 else 0

    result = {
        'samples': samples,
        'base_ts': base_ts,
        'summary': {
            'total_success': cum_success,
            'total_failed': cum_failed,
            'avg_tps': round(avg_tps, 1),
            'elapsed_sec': round(elapsed_sec, 3),
        }
    }

    with open(output_file, 'w') as f:
        json.dump(result, f, indent=2)

    print(f"  Generated: {output_file} ({len(samples)} samples, avg_tps={avg_tps:.0f})")


if __name__ == '__main__':
    main()

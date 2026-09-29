#!/usr/bin/env python3
"""
从 per-node region_stats.jsonl 文件中提取 per-region TPS 时间序列。

用法:
  python3 scripts/extract_region_tps.py \
      --run-dir output/fault_recovery/comp_clique/run_1 \
      --output output/fault_recovery/comp_clique/run_1/region_tps_samples.json

输入: run_dir 下的 node_*_region_stats*.jsonl 文件
输出: region_tps_samples.json
"""

import argparse
import glob
import json
import os
import sys
from collections import defaultdict


def load_jsonl_files(run_dir):
    """加载 run_dir 下所有 region_stats jsonl 文件"""
    patterns = [
        os.path.join(run_dir, 'node_*_region_stats.jsonl'),
        os.path.join(run_dir, 'node_*_region_stats_restart*.jsonl'),
    ]
    all_records = []
    for pattern in patterns:
        for fpath in glob.glob(pattern):
            with open(fpath) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            all_records.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
    return all_records


def extract_region_tps(run_dir, interval_ms=None):
    """从所有节点的 jsonl 记录中聚合 per-region TPS"""
    records = load_jsonl_files(run_dir)
    if not records:
        return None

    if interval_ms is None:
        interval_ms = int(records[0].get('elapsed_ms', 200))
    interval_sec = interval_ms / 1000.0

    # ts 现在是 Unix 时间戳（秒），所有节点共享同一时间基准
    # 按绝对时间分桶，然后找到第一个有数据的时刻作为 t=0
    all_ts = [rec['ts'] for rec in records]
    if not all_ts:
        return None

    # 安全检查：过滤掉异常 ts（混入旧格式相对时间的记录）
    # Unix 时间戳 > 1e9（2001年以后），相对时间 < 1e6
    median_ts = sorted(all_ts)[len(all_ts) // 2]
    if median_ts > 1e9:
        records = [r for r in records if r['ts'] > 1e9]
    else:
        records = [r for r in records if r['ts'] < 1e6]
    all_ts = [rec['ts'] for rec in records]
    if not all_ts:
        return None

    # 用最小 ts 作为基准，转换为相对时间后分桶
    base_ts = min(all_ts)
    slot_data = defaultdict(lambda: defaultdict(lambda: {'success': 0, 'failed': 0}))

    for rec in records:
        relative = rec['ts'] - base_ts
        slot = round(relative / interval_sec)
        regions = rec.get('regions', {})
        for rid_str, counts in regions.items():
            slot_data[slot][rid_str]['success'] += counts.get('success', 0)
            slot_data[slot][rid_str]['failed'] += counts.get('failed', 0)

    if not slot_data:
        return None

    # 找到第一个有实际请求的 slot
    sorted_slots = sorted(slot_data.keys())
    first_active_slot = sorted_slots[0]
    for slot in sorted_slots:
        total = sum(
            v['success'] + v['failed']
            for v in slot_data[slot].values()
        )
        if total > 0:
            first_active_slot = slot
            break

    last_slot = sorted_slots[-1]

    all_region_ids = sorted(set(
        rid for slot_regions in slot_data.values() for rid in slot_regions.keys()
    ), key=lambda x: int(x))

    # 构建 per-region 时间序列（以第一个活跃 slot 为 t=0）
    region_samples = {}
    for rid in all_region_ids:
        samples = []
        for slot in range(first_active_slot, last_slot + 1):
            s = slot_data[slot][rid]['success']
            f = slot_data[slot][rid]['failed']
            tps = s / interval_sec
            relative_time = (slot - first_active_slot + 1) * interval_sec
            samples.append({
                'time_sec': round(relative_time, 3),
                'tps': round(tps, 1),
                'success': s,
                'failed': f,
            })
        region_samples[rid] = samples

    # 加载 experiment_meta.json
    meta_file = os.path.join(run_dir, 'experiment_meta.json')
    meta = {}
    if os.path.exists(meta_file):
        with open(meta_file) as f:
            meta = json.load(f)

    # 将 fault_events 的时间转换为与 time_sec 相同的基准
    # time_sec 的 T=0 = base_ts + first_active_slot * interval_sec
    time_origin = base_ts + first_active_slot * interval_sec
    if meta.get('fault_events'):
        for evt in meta['fault_events']:
            if 'fault_epoch' in evt:
                evt['fault_sec'] = evt['fault_epoch'] - time_origin
                evt['restart_sec'] = evt['restart_epoch'] - time_origin

    return {
        'interval_ms': interval_ms,
        'regions': region_samples,
        'meta': meta,
    }


def main():
    parser = argparse.ArgumentParser(description='Extract per-region TPS from jsonl stats')
    parser.add_argument('--run-dir', required=True, help='Single run directory')
    parser.add_argument('--output', default=None, help='Output JSON file path')
    parser.add_argument('--interval-ms', type=int, default=None, help='Override interval')
    args = parser.parse_args()

    result = extract_region_tps(args.run_dir, args.interval_ms)
    if result is None:
        print(f'  WARNING: No region stats found in {args.run_dir}', file=sys.stderr)
        sys.exit(0)

    output_path = args.output or os.path.join(args.run_dir, 'region_tps_samples.json')
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'w') as f:
        json.dump(result, f, indent=2)

    n_regions = len(result['regions'])
    n_samples = len(next(iter(result['regions'].values()))) if n_regions > 0 else 0
    print(f'  Extracted: {n_regions} regions, {n_samples} samples -> {output_path}')


if __name__ == '__main__':
    main()

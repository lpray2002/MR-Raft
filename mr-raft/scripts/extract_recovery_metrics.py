#!/usr/bin/env python3
"""
从故障恢复实验结果中提取关键指标。

支持两种 experiment_meta.json 格式:
  1. 单故障: fault_time_sec / restart_time_sec
  2. 多故障: fault_events 数组 (multi_fault=true)

输出 metrics.json 包含:
  - per-run 原始数据
  - summary 汇总统计

用法:
  python3 scripts/extract_recovery_metrics.py \
      --input output/fault_recovery/exp_a \
      --output output/fault_recovery/exp_a/metrics.json
"""

import argparse
import json
import os
import re
import glob
import statistics


def parse_recovery_probes(log_file):
    """从服务端日志提取 [RECOVERY_PROBE] 行"""
    probes = []
    if not os.path.exists(log_file):
        return probes
    with open(log_file) as f:
        for line in f:
            m = re.search(r'\[RECOVERY_PROBE\]\s+(.*)', line)
            if m:
                kv_str = m.group(1)
                probe = {}
                for pair in kv_str.split():
                    if '=' in pair:
                        k, v = pair.split('=', 1)
                        probe[k] = v
                probes.append(probe)
    return probes


def _compute_tps_recovery(tps_samples, fault_sec, restart_sec, threshold_pct=0.9):
    """Given per-second TPS samples and a single fault window,
    return the number of seconds after restart until TPS >= threshold."""
    pre_samples = [s for s in tps_samples if 3 <= s['time_sec'] < fault_sec]
    if not pre_samples:
        return None, 0

    pre_avg = sum(s['tps'] for s in pre_samples) / len(pre_samples)
    threshold = pre_avg * threshold_pct

    for s in tps_samples:
        if s['time_sec'] > restart_sec and s['tps'] >= threshold:
            return s['time_sec'] - restart_sec, pre_avg

    return None, pre_avg


def extract_run_metrics(run_dir):
    """从单次运行目录提取所有指标"""
    result = {
        'run_dir': run_dir,
        'meta': None,
        'tps_samples': [],
        'recovery_probes': [],
        'tps_recovery_sec': None,
        'per_region_recovery_ms': {},
        'per_event_metrics': [],
    }

    # 1. 读取元信息
    meta_file = os.path.join(run_dir, 'experiment_meta.json')
    if os.path.exists(meta_file):
        with open(meta_file) as f:
            result['meta'] = json.load(f)

    # 2. 读取 TPS samples
    tps_file = os.path.join(run_dir, 'tps_samples.json')
    if os.path.exists(tps_file):
        with open(tps_file) as f:
            tps_data = json.load(f)
        result['tps_samples'] = tps_data.get('samples', [])

    # 3. 读取所有重启日志中的 RECOVERY_PROBE
    restart_logs = glob.glob(os.path.join(run_dir, 'node_*_restart*.log'))
    for log_file in restart_logs:
        probes = parse_recovery_probes(log_file)
        result['recovery_probes'].extend(probes)

    # 4. 提取 per-region 恢复时间
    for probe in result['recovery_probes']:
        if probe.get('phase') == 'region_ready':
            rid = probe.get('region', 'unknown')
            elapsed = int(probe.get('elapsed_ms', 0))
            result['per_region_recovery_ms'].setdefault(rid, []).append(elapsed)

    meta = result['meta']
    samples = result['tps_samples']

    if not meta or not samples:
        return result

    # 5. 多故障模式: per-event metrics
    if meta.get('multi_fault') and meta.get('fault_events'):
        events = meta['fault_events']
        for i, evt in enumerate(events):
            f_sec = evt['fault_sec']
            r_sec = evt['restart_sec']
            role = evt.get('role', '')

            # Pre-event steady: 3s before fault (but after previous restart)
            lookback_start = max(3, events[i - 1]['restart_sec'] + 2) if i > 0 else 3
            pre_samples = [s for s in samples if lookback_start <= s['time_sec'] < f_sec]
            pre_avg = (sum(s['tps'] for s in pre_samples) / len(pre_samples)) if pre_samples else 0

            threshold = pre_avg * 0.9
            rec_sec = None
            for s in samples:
                if s['time_sec'] > r_sec and s['tps'] >= threshold:
                    rec_sec = s['time_sec'] - r_sec
                    break

            result['per_event_metrics'].append({
                'event_index': i,
                'node_id': evt.get('node_id'),
                'role': role,
                'fault_sec': f_sec,
                'restart_sec': r_sec,
                'pre_fault_avg_tps': pre_avg,
                'tps_recovery_sec': rec_sec,
            })

        # Overall tps_recovery: use first event for backward compat
        if events:
            first = events[0]
            result['tps_recovery_sec'], result['pre_fault_avg_tps'] = \
                _compute_tps_recovery(samples, first['fault_sec'], first['restart_sec'])

    else:
        # 单故障模式
        fault_sec = meta.get('fault_time_sec', 10)
        restart_sec = meta.get('restart_time_sec', 15)
        result['tps_recovery_sec'], result['pre_fault_avg_tps'] = \
            _compute_tps_recovery(samples, fault_sec, restart_sec)

    return result


def main():
    parser = argparse.ArgumentParser(description='提取故障恢复实验指标')
    parser.add_argument('--input', required=True, help='实验输出目录')
    parser.add_argument('--output', default=None, help='输出 JSON 文件路径')
    args = parser.parse_args()

    input_dir = args.input
    output_file = args.output or os.path.join(input_dir, 'metrics.json')

    run_dirs = sorted(glob.glob(os.path.join(input_dir, 'run_*')))
    if not run_dirs:
        print(f'No run_* directories found in {input_dir}')
        return

    all_runs = []
    for rd in run_dirs:
        print(f'Processing {rd} ...')
        metrics = extract_run_metrics(rd)
        all_runs.append(metrics)

    # Aggregate single-fault summary
    tps_recoveries = [r['tps_recovery_sec'] for r in all_runs
                      if r['tps_recovery_sec'] is not None]
    region_recoveries = {}
    for r in all_runs:
        for rid, ms_list in r['per_region_recovery_ms'].items():
            region_recoveries.setdefault(rid, []).extend(ms_list)

    summary = {
        'num_runs': len(all_runs),
        'tps_recovery_sec': {
            'values': tps_recoveries,
            'mean': statistics.mean(tps_recoveries) if tps_recoveries else None,
            'min': min(tps_recoveries) if tps_recoveries else None,
            'max': max(tps_recoveries) if tps_recoveries else None,
        },
        'per_region_recovery_ms': {
            rid: {
                'values': vals,
                'mean': statistics.mean(vals),
                'min': min(vals),
                'max': max(vals),
            }
            for rid, vals in region_recoveries.items()
        },
    }

    # Aggregate per-event metrics (multi-fault mode)
    per_event_agg = {}
    for r in all_runs:
        for em in r.get('per_event_metrics', []):
            idx = em['event_index']
            per_event_agg.setdefault(idx, {
                'node_id': em['node_id'],
                'role': em['role'],
                'fault_sec': em['fault_sec'],
                'restart_sec': em['restart_sec'],
                'pre_fault_avg_tps_values': [],
                'tps_recovery_sec_values': [],
            })
            per_event_agg[idx]['pre_fault_avg_tps_values'].append(em['pre_fault_avg_tps'])
            if em['tps_recovery_sec'] is not None:
                per_event_agg[idx]['tps_recovery_sec_values'].append(em['tps_recovery_sec'])

    if per_event_agg:
        summary['per_event'] = []
        for idx in sorted(per_event_agg.keys()):
            e = per_event_agg[idx]
            rec_vals = e['tps_recovery_sec_values']
            summary['per_event'].append({
                'event_index': idx,
                'node_id': e['node_id'],
                'role': e['role'],
                'fault_sec': e['fault_sec'],
                'restart_sec': e['restart_sec'],
                'pre_fault_avg_tps_mean': statistics.mean(e['pre_fault_avg_tps_values']),
                'tps_recovery_sec_mean': statistics.mean(rec_vals) if rec_vals else None,
                'tps_recovery_sec_values': rec_vals,
            })

    output = {
        'summary': summary,
        'runs': all_runs,
    }

    # Remove raw TPS samples to keep output small
    for run in output['runs']:
        run['tps_sample_count'] = len(run['tps_samples'])
        del run['tps_samples']

    os.makedirs(os.path.dirname(output_file) or '.', exist_ok=True)
    with open(output_file, 'w') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f'\nMetrics saved to {output_file}')
    print(f'\nSummary:')
    print(f'  Runs: {summary["num_runs"]}')
    if summary['tps_recovery_sec']['mean'] is not None:
        print(f'  TPS recovery: mean={summary["tps_recovery_sec"]["mean"]:.1f}s, '
              f'min={summary["tps_recovery_sec"]["min"]:.1f}s, '
              f'max={summary["tps_recovery_sec"]["max"]:.1f}s')
    for rid, stats in summary['per_region_recovery_ms'].items():
        print(f'  Region {rid} recovery: mean={stats["mean"]:.0f}ms, '
              f'min={stats["min"]}ms, max={stats["max"]}ms')

    if summary.get('per_event'):
        print(f'\n  Per-event (multi-fault):')
        for e in summary['per_event']:
            rec = e['tps_recovery_sec_mean']
            rec_str = f'{rec:.1f}s' if rec is not None else 'N/A'
            print(f'    Event {e["event_index"]}: node={e["node_id"]} '
                  f'role={e["role"]} recovery={rec_str}')


if __name__ == '__main__':
    main()

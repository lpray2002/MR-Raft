#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
TPS-CPU Total Analysis for 2025_11_12_full-topology-2cpu-8thread-mem data
绘制总TPS vs CPU曲线（区分Leader和Follower节点）
"""

import os
import re
import sys
import pandas as pd
import matplotlib.pyplot as plt
from typing import Dict, List, Tuple
from collections import defaultdict
from datetime import datetime, timedelta

# 导入项目颜色
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.visualization.plotter import COLORS

OUTPUT_DIR = 'output/tps_cpu_total'

# 两套 Leader 放置策略使用已有缓存数据，不重新解析原始性能日志。
DATASETS = [
    {
        'key': 'balanced',
        'label': 'Balanced',
        'csv': 'total_tps_cpu_data copy.csv',
    },
    {
        'key': 'worst',
        'label': 'Worst-case',
        'csv': 'total_tps_cpu_data_worst.csv',
    },
]

# 绘图参数：与 run.py 的阈值分析图统一。
FIGURE_SIZE_SINGLE = (7, 5)
FIGURE_SIZE_DOUBLE = (12, 4.5)
DPI = 300
AXIS_LABEL_FONTSIZE = 20
TICK_LABEL_FONTSIZE = 18
LEGEND_FONTSIZE = 16
MARKER_SIZE = 10

# 使导出的 PDF/SVG 文字可编辑，并与阈值图的导出设置一致。
plt.rcParams['pdf.fonttype'] = 42
plt.rcParams['svg.fonttype'] = 'none'

# 方法名称映射
METHOD_NAMES = {
    'clique': 'MR-Raft Clique',
    'global': 'MR-Raft Global',
    'neighbor': 'MR-Raft Neighbor',
    'threshold': 'MR-Raft Threshold',
}

# Leader节点配置（worst leader placement）
METHOD_LEADER_PLACEMENTS = {
    'global': {1: 0},  # Region 1的Leader是节点0
    'neighbor': {1: 12, 2: 2, 3: 11, 4: 12, 5: 12, 6: 12, 7: 1, 8: 11, 9: 11},  # 9个Region
    'clique': {1: 2, 2: 6, 3: 1, 4: 12, 5: 1, 6: 12, 7: 12, 8: 4, 9: 4, 10: 5, 11: 6},  # 11个Region
    'threshold': {1: 11, 2: 12, 3: 1, 4: 12, 5: 11}  # 5个Region
}

# 颜色索引
COLOR_INDICES = {
    'global': 0,      # '#0273bc' 深蓝
    'neighbor': 2,    # '#87a8d7' 中蓝
    'clique': 7,      # '#e6867f' 粉红
    'threshold': 9    # '#c32934' 深红
}

# 允许的最大非活动间隔（行数），用于判定同一运行段
MAX_INACTIVE_GAP_ROWS = 2


def extract_tps_from_client_log(log_path: str) -> float:
    """
    从客户端日志中提取总TPS
    """
    try:
        with open(log_path, 'r', encoding='utf-8') as f:
            content = f.read()
            
        # 匹配 "总TPS:        3439" 格式
        tps_match = re.search(r'总TPS:\s+(\d+)', content)
        if tps_match:
            return float(tps_match.group(1))
        
        # 如果没有找到总TPS，尝试匹配单个节点的TPS并求和
        node_tps_matches = re.findall(r'TPS:\s+(\d+)', content)
        if node_tps_matches:
            return sum(float(tps) for tps in node_tps_matches)
            
    except Exception as e:
        print(f"[WARNING] 无法读取 {log_path}: {e}")
    
    return 0.0


def parse_cpu_value(value: str) -> float:
    """
    解析CPU值，移除百分号并转换为浮点数
    """
    if pd.isna(value) or value == '' or value == ' ':
        return 0.0
    
    value = str(value).strip()
    if value.endswith('%'):
        value = value[:-1]
    
    try:
        return float(value)
    except:
        return 0.0


def build_run_info(df: pd.DataFrame) -> Dict[str, List[Dict[str, object]]]:
    """
    将CPU CSV拆分为按方法的运行段信息
    返回：{method: [run_entry...]}
    run_entry包含：run_id, start_idx, end_idx, start_time, end_time, columns_by_node
    """
    pattern = re.compile(r'full-(?P<method>\w+)-n(?P<node>\d+)(?:\.(?P<run>\d+))?', re.IGNORECASE)
    method_runs: Dict[str, Dict[int, List[Dict[str, object]]]] = defaultdict(lambda: defaultdict(list))

    for col_idx, col_name in enumerate(df.columns):
        match = pattern.match(str(col_name))
        if not match:
            continue

        method = match.group('method')
        node_id = int(match.group('node'))
        run_id = int(match.group('run')) if match.group('run') else 0

        series = df.iloc[:, col_idx]
        valid_mask = series.apply(
            lambda v: not (
                pd.isna(v)
                or (isinstance(v, str) and v.strip() == '')
                or (isinstance(v, str) and v.strip().lower() == 'nan')
            )
        )

        if not valid_mask.any():
            continue

        active_indices = valid_mask[valid_mask].index.to_list()
        segment_start = active_indices[0]
        prev_idx = active_indices[0]
        for current_idx in active_indices[1:]:
            if current_idx - prev_idx > MAX_INACTIVE_GAP_ROWS:
                method_runs[method][run_id].append({
                    'start_idx': segment_start,
                    'end_idx': prev_idx,
                    'node_id': node_id,
                    'col_idx': col_idx
                })
                segment_start = current_idx
            prev_idx = current_idx

        method_runs[method][run_id].append({
            'start_idx': segment_start,
            'end_idx': prev_idx,
            'node_id': node_id,
            'col_idx': col_idx
        })

    run_info: Dict[str, List[Dict[str, object]]] = {}
    for method, runs in method_runs.items():
        run_list: List[Dict[str, object]] = []
        for run_id, segments in runs.items():
            if not segments:
                continue

            segments.sort(key=lambda seg: seg['start_idx'])

            merged_segments: List[Dict[str, object]] = []
            for seg in segments:
                if not merged_segments:
                    merged_segments.append({
                        'run_id': run_id,
                        'start_idx': seg['start_idx'],
                        'end_idx': seg['end_idx'],
                        'columns_by_node': defaultdict(list, {seg['node_id']: [seg['col_idx']]})
                    })
                    continue

                last = merged_segments[-1]
                if seg['start_idx'] <= last['end_idx'] + MAX_INACTIVE_GAP_ROWS:
                    # 合并到当前段
                    last['end_idx'] = max(last['end_idx'], seg['end_idx'])
                    last['columns_by_node'][seg['node_id']].append(seg['col_idx'])
                else:
                    merged_segments.append({
                        'run_id': run_id,
                        'start_idx': seg['start_idx'],
                        'end_idx': seg['end_idx'],
                        'columns_by_node': defaultdict(list, {seg['node_id']: [seg['col_idx']]})
                    })

            for merged in merged_segments:
                merged['columns_by_node'] = dict(merged['columns_by_node'])
                merged['start_time'] = df.loc[merged['start_idx'], 'Time']
                merged['end_time'] = df.loc[merged['end_idx'], 'Time']
                run_list.append(merged)

        run_list.sort(key=lambda x: x['start_time'])
        run_info[method] = run_list

    return run_info


def extract_cpu_load_by_role(df: pd.DataFrame, run_entry: Dict[str, object],
                              leader_nodes: set, follower_nodes: set) -> Tuple[float, float]:
    """
    在指定运行段内计算Leader/Follower平均CPU
    """
    start_idx = run_entry['start_idx']
    end_idx = run_entry['end_idx']
    segment_df = df.iloc[start_idx:end_idx + 1]

    total_rows = len(segment_df)
    if total_rows > 10:
        stable_df = segment_df.iloc[5:-5]
    else:
        stable_df = segment_df

    leader_cpu_values: List[float] = []
    follower_cpu_values: List[float] = []

    columns_by_node: Dict[int, List[int]] = run_entry['columns_by_node']

    for node_id in leader_nodes:
        if node_id not in columns_by_node:
            continue
        for col_idx in columns_by_node[node_id]:
            cpu_values = stable_df.iloc[:, col_idx].apply(parse_cpu_value)
            cpu_values = cpu_values[cpu_values > 0]
            if not cpu_values.empty:
                leader_cpu_values.append(cpu_values.max())

    for node_id in follower_nodes:
        if node_id not in columns_by_node:
            continue
        for col_idx in columns_by_node[node_id]:
            cpu_values = stable_df.iloc[:, col_idx].apply(parse_cpu_value)
            cpu_values = cpu_values[cpu_values > 0]
            if not cpu_values.empty:
                follower_cpu_values.append(cpu_values.max())

    leader_avg = sum(leader_cpu_values) / len(leader_cpu_values) if leader_cpu_values else 0.0
    follower_avg = sum(follower_cpu_values) / len(follower_cpu_values) if follower_cpu_values else 0.0

    return leader_avg, follower_avg


def get_cpu_csv_for_concurrency(concurrency: int) -> str:
    """
    根据并发量获取对应的CPU CSV文件路径
    """
    return os.path.join(DATA_DIR, CPU_CSV_FILE)


def get_method_time_ranges(csv_path: str) -> Dict[str, Tuple[datetime, datetime]]:
    """
    获取每个方法的活动时间范围
    """
    try:
        df = pd.read_csv(csv_path, low_memory=False)
        df['Time'] = pd.to_datetime(df['Time'], errors='coerce')
        
        method_ranges = {}
        
        for method in METHOD_NAMES.keys():
            active_times = set()
            
            # 查找包含该方法的列
            method_cols = [col for col in df.columns if f'full-{method}-n' in col.lower()]
            
            if not method_cols:
                continue
            
            # 找到有数据的时间点
            for col in method_cols:
                for idx, row in df.iterrows():
                    value = row[col]
                    if pd.notna(value) and value != '' and value != '0.0':
                        try:
                            cpu_val = parse_cpu_value(str(value))
                            if cpu_val > 0:
                                active_times.add(row['Time'])
                        except:
                            pass
            
            if active_times:
                active_times = sorted(list(active_times))
                method_ranges[method] = (active_times[0], active_times[-1])
                print(f"{method} 方法活动时间: {active_times[0]} - {active_times[-1]}")
        
        return method_ranges
        
    except Exception as e:
        print(f"[WARNING] Failed to get method time ranges: {e}")
        return {}


def extract_cpu_data_by_time(csv_path: str, method: str, start_time: datetime, end_time: datetime) -> Tuple[float, float]:
    """
    从CPU CSV文件中提取指定时间段内指定方法的Leader和Follower平均CPU
    返回: (leader_avg_cpu, follower_avg_cpu)
    """
    try:
        df = pd.read_csv(csv_path, low_memory=False)
        df['Time'] = pd.to_datetime(df['Time'], errors='coerce')
        
        # 筛选时间范围
        time_mask = (df['Time'] >= start_time) & (df['Time'] <= end_time)
        time_filtered_df = df[time_mask]
        
        if len(time_filtered_df) == 0:
            return 0.0, 0.0
        
        # 获取leader和follower节点
        leader_placement = METHOD_LEADER_PLACEMENTS.get(method, {})
        leader_nodes = set(leader_placement.values())
        all_nodes = set(range(15))
        follower_nodes = all_nodes - leader_nodes
        
        # 找到所有包含指定方法的列
        leader_columns = []
        follower_columns = []
        
        for col in df.columns:
            if col == 'Time':
                continue
            # 匹配列名中包含 full-{method}-n 的列
            if f'full-{method}-n' in col.lower():
                import re
                match = re.search(rf'full-{method}-n(\d+)', col.lower())
                if match:
                    node_id = int(match.group(1))
                    if node_id in leader_nodes:
                        leader_columns.append(col)
                    elif node_id in follower_nodes:
                        follower_columns.append(col)
        
        if not leader_columns and not follower_columns:
            return 0.0, 0.0
        
        # 去掉前后5行作为稳定期
        total_rows = len(time_filtered_df)
        if total_rows > 10:
            stable_df = time_filtered_df.iloc[5:-5]
        else:
            stable_df = time_filtered_df
        
        # 计算每个节点的最大CPU值
        leader_cpu_values = []
        follower_cpu_values = []
        
        for col in leader_columns:
            cpu_values = stable_df[col].apply(parse_cpu_value)
            cpu_values = cpu_values[cpu_values > 0]
            if not cpu_values.empty:
                leader_cpu_values.append(cpu_values.max())
        
        for col in follower_columns:
            cpu_values = stable_df[col].apply(parse_cpu_value)
            cpu_values = cpu_values[cpu_values > 0]
            if not cpu_values.empty:
                follower_cpu_values.append(cpu_values.max())
        
        leader_avg = sum(leader_cpu_values) / len(leader_cpu_values) if leader_cpu_values else 0.0
        follower_avg = sum(follower_cpu_values) / len(follower_cpu_values) if follower_cpu_values else 0.0
        
        return leader_avg, follower_avg
        
    except Exception as e:
        print(f"[WARNING] Failed to extract CPU data: {e}")
        return 0.0, 0.0


def get_test_time_ranges_from_csv(base_dir: str, csv_path: str) -> Dict[str, List[Tuple[str, int, datetime, datetime]]]:
    """
    基于每个方法的专属时间段来分配测试时间
    返回: {method: [(dir_name, concurrency, start_time, end_time), ...]}
    """
    method_tests = {}
    
    # 获取每个方法的时间范围
    method_ranges = get_method_time_ranges(csv_path)
    if not method_ranges:
        print("[WARNING] 未找到方法时间范围")
        return method_tests
    
    # 遍历所有子目录，按并发量排序
    subdirs = [d for d in os.listdir(base_dir) 
              if os.path.isdir(os.path.join(base_dir, d)) and not d.startswith('.')]
    
    # 按方法和并发量分组
    method_concurrency = {}
    for subdir in subdirs:
        match = re.match(r'(\w+)_c0_(\d+),', subdir)
        if not match:
            continue
        method = match.group(1)
        concurrency = int(match.group(2))
        
        if method not in METHOD_NAMES:
            continue
            
        dir_path = os.path.join(base_dir, subdir)
        client_log = os.path.join(dir_path, f'full-{method}-client.log')
        if not os.path.exists(client_log):
            continue
            
        if method not in method_concurrency:
            method_concurrency[method] = []
        method_concurrency[method].append((subdir, concurrency))
    
    # 为每个方法分配测试时间
    for method in method_concurrency:
        if method not in method_ranges:
            print(f"[WARNING] 未找到 {method} 方法的时间范围")
            continue
            
        method_concurrency[method].sort(key=lambda x: x[1])  # 按并发量排序
        
        method_tests[method] = []
        total_tests = len(method_concurrency[method])
        
        # 获取该方法的时间范围
        start_time, end_time = method_ranges[method]
        total_duration = (end_time - start_time).total_seconds()
        
        print(f"{method} 方法: {total_tests} 个测试，总时长 {total_duration/3600:.2f} 小时")
        
        # 简单平均分配时间
        test_duration = total_duration / total_tests
        current_time = start_time
        for subdir, concurrency in method_concurrency[method]:
            test_start = current_time
            test_end = current_time + timedelta(seconds=test_duration)
            method_tests[method].append((subdir, concurrency, test_start, test_end))
            current_time = test_end
    
    return method_tests


def analyze_data_by_time(base_dir: str, cpu_csv_path: str) -> Dict[str, List[Dict]]:
    """
    按时间段分析数据
    返回: {method: [{'concurrency': int, 'tps': float, 'leader_cpu': float, 'follower_cpu': float}, ...]}
    """
    # 获取测试时间范围
    method_tests = get_test_time_ranges_from_csv(base_dir, cpu_csv_path)
    
    all_data = defaultdict(list)
    
    for method, tests in method_tests.items():
        print(f"\n分析 {method} 方法:")
        
        for dir_name, concurrency, start_time, end_time in tests:
            print(f"  并发量={concurrency:3d} | 时间段: {start_time.strftime('%H:%M:%S')} - {end_time.strftime('%H:%M:%S')}")
            
            # 提取TPS数据
            dir_path = os.path.join(base_dir, dir_name)
            client_log = os.path.join(dir_path, f'full-{method}-client.log')
            tps = extract_tps_from_client_log(client_log)
            
            # 提取CPU数据
            leader_cpu, follower_cpu = extract_cpu_data_by_time(cpu_csv_path, method, start_time, end_time)
            
            print(f"    TPS={tps:5.0f} | Leader CPU={leader_cpu:5.1f}% | Follower CPU={follower_cpu:5.1f}%")
            
            all_data[method].append({
                'concurrency': concurrency,
                'tps': tps,
                'leader_cpu': leader_cpu,
                'follower_cpu': follower_cpu,
                'start_time': start_time,
                'end_time': end_time
            })
    
    return all_data


def analyze_directory(base_dir: str) -> Dict[str, Dict[int, Dict[str, float]]]:
    """
    分析整个目录，提取TPS和CPU数据（旧版本，保留用于兼容）
    返回：{method: {total_tps: {'leader': cpu, 'follower': cpu, 'concurrency': int}}}
    """
    all_data = defaultdict(lambda: defaultdict(dict))
    
    # 遍历所有子目录
    subdirs = [d for d in os.listdir(base_dir) 
               if os.path.isdir(os.path.join(base_dir, d)) and not d.startswith('.')]
    
    print(f"\n找到 {len(subdirs)} 个测试目录")
    
    # 按方法和并发量组织数据
    method_concurrency_dirs = defaultdict(lambda: defaultdict(list))
    
    for subdir in sorted(subdirs):
        # 解析目录名：{method}_c0_{concurrency},..._q0
        # 例如：global_c0_1,1_1,2_1,3_1,4_1,5_1,6_1,7_1,8_1,9_1,10_1,11_1,12_1,13_1,14_1,_q0
        match = re.match(r'(\w+)_c0_(\d+),', subdir)
        if not match:
            continue
        
        method = match.group(1)
        concurrency = int(match.group(2))
        
        if method not in METHOD_NAMES:
            continue
        
        method_concurrency_dirs[method][concurrency].append(subdir)
    
    # 处理每个方法的每个并发量
    run_pointer: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for method in sorted(method_concurrency_dirs.keys()):
        print(f"\n处理方法: {method}")
        
        for concurrency in sorted(method_concurrency_dirs[method].keys()):
            subdirs_for_concurrency = method_concurrency_dirs[method][concurrency]
            
            if not subdirs_for_concurrency:
                continue
            
            # 使用第一个目录（它们应该是相同的测试）
            subdir = subdirs_for_concurrency[0]
            dir_path = os.path.join(base_dir, subdir)
            
            # 查找客户端日志
            client_log = None
            for f in os.listdir(dir_path):
                if 'client' in f.lower() and f.endswith('.log'):
                    client_log = os.path.join(dir_path, f)
                    break
            
            if not client_log:
                print(f"  [WARNING] 并发={concurrency}: 未找到客户端日志")
                continue
            
            # 提取总TPS
            total_tps = extract_tps_from_client_log(client_log)
            
            if total_tps == 0:
                print(f"  [WARNING] 并发={concurrency}: 无法提取TPS")
                continue
            
            # 获取对应的CPU CSV文件
            cpu_csv_path = get_cpu_csv_for_concurrency(concurrency)
            
            if not cpu_csv_path or not os.path.exists(cpu_csv_path):
                print(f"  [WARNING] 并发={concurrency}: 未找到对应的CPU CSV文件")
                continue
            
            # 读取CPU CSV（如果还没有读取）
            if not hasattr(analyze_directory, 'cpu_dfs'):
                analyze_directory.cpu_dfs = {}
            if not hasattr(analyze_directory, 'run_info_cache'):
                analyze_directory.run_info_cache = {}
            
            if cpu_csv_path not in analyze_directory.cpu_dfs:
                print(f"  读取CPU数据: {os.path.basename(cpu_csv_path)}")
                cpu_df = pd.read_csv(cpu_csv_path, low_memory=False)
                if 'Time' in cpu_df.columns:
                    cpu_df['Time'] = pd.to_datetime(cpu_df['Time'], errors='coerce')
                analyze_directory.cpu_dfs[cpu_csv_path] = cpu_df
                analyze_directory.run_info_cache[cpu_csv_path] = build_run_info(cpu_df)
            else:
                cpu_df = analyze_directory.cpu_dfs[cpu_csv_path]

            run_info_for_csv = analyze_directory.run_info_cache.get(cpu_csv_path, {})
            run_list = run_info_for_csv.get(method)
            if not run_list:
                print(f"  [WARNING] 并发={concurrency}: 在CPU CSV中未找到 {method} 的运行信息")
                continue

            # 确保运行段按照开始时间升序
            sorted_run_list = sorted(
                run_list,
                key=lambda entry: pd.to_datetime(entry.get('start_time')) if entry.get('start_time') is not None else pd.Timestamp.min
            )
            analyze_directory.run_info_cache[cpu_csv_path][method] = sorted_run_list
            run_list = sorted_run_list

            run_idx = run_pointer[cpu_csv_path][method]
            if run_idx >= len(run_list):
                print(f"  [WARNING] 并发={concurrency}: 运行段不足 (仅 {len(run_list)} 段)")
                run_idx = len(run_list) - 1
            run_entry = run_list[run_idx]
            run_pointer[cpu_csv_path][method] = run_idx + 1

            # 确定Leader和Follower节点
            leader_placement = METHOD_LEADER_PLACEMENTS.get(method, {})
            leader_nodes = set(leader_placement.values())
            all_nodes = set(run_entry['columns_by_node'].keys())
            follower_nodes = all_nodes - leader_nodes
            
            # 提取CPU负载
            leader_cpu, follower_cpu = extract_cpu_load_by_role(
                cpu_df, run_entry, leader_nodes, follower_nodes
            )
            
            # 存储数据
            all_data[method][int(total_tps)] = {
                'leader': leader_cpu,
                'follower': follower_cpu,
                'concurrency': concurrency,
                'run_start': run_entry['start_time'],
                'run_end': run_entry['end_time']
            }
            
            print(f"  并发={concurrency:3d} | TPS总={total_tps:5.0f} | "
                  f"Leader平均CPU={leader_cpu:5.1f}% | Follower平均CPU={follower_cpu:5.1f}% "
                  f"| 段索引={run_idx} run_id={run_entry.get('run_id')} "
                  f"时间={run_entry['start_time']}~{run_entry['end_time']}")
    
    return dict(all_data)


def plot_total_tps_cpu(data: Dict[str, List[Dict]], output_dir: str):
    """
    绘制总TPS vs CPU曲线
    """
    os.makedirs(output_dir, exist_ok=True)
    
    method_order = ['global', 'neighbor', 'clique', 'threshold']
    
    # 创建两个子图：Leader CPU 和 Follower CPU
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=FIGURE_SIZE_DOUBLE, dpi=DPI)
    
    # 绘制 Leader CPU vs Total TPS
    for method in method_order:
        if method not in data:
            continue
        
        # 按并发量排序
        sorted_data = sorted(data[method], key=lambda x: x['concurrency'])
        # 转换为 ktx/s (除以1000)
        total_tps_list = [item['tps'] / 1000.0 for item in sorted_data]
        leader_cpu = [item['leader_cpu'] for item in sorted_data]
        
        color = COLORS[COLOR_INDICES[method]]
        ax1.plot(total_tps_list, leader_cpu,
                marker='o',
                linewidth=3.5,
                markersize=MARKER_SIZE,
                label=METHOD_NAMES.get(method, method),
                color=color)
    
    ax1.set_xlabel('Total TPS (ktx/s)', fontsize=AXIS_LABEL_FONTSIZE, fontweight='bold')
    ax1.set_ylabel('Leader Avg CPU (%)', fontsize=AXIS_LABEL_FONTSIZE, fontweight='bold')
    ax1.grid(True, alpha=0.7, linestyle='--', linewidth=0.8)
    ax1.legend(loc='lower center', bbox_to_anchor=(0.5, 1.01), ncol=2,
               fontsize=LEGEND_FONTSIZE, framealpha=0.9)
    ax1.tick_params(axis='both', labelsize=TICK_LABEL_FONTSIZE)
    
    # 绘制 Follower CPU vs Total TPS
    for method in method_order:
        if method not in data:
            continue
        
        # 按并发量排序
        sorted_data = sorted(data[method], key=lambda x: x['concurrency'])
        # 转换为 ktx/s (除以1000)
        total_tps_list = [item['tps'] / 1000.0 for item in sorted_data]
        follower_cpu = [item['follower_cpu'] for item in sorted_data]
        
        color = COLORS[COLOR_INDICES[method]]
        ax2.plot(total_tps_list, follower_cpu,
                marker='s',
                linewidth=3.5,
                markersize=MARKER_SIZE,
                label=METHOD_NAMES.get(method, method),
                color=color)
    
    ax2.set_xlabel('Total TPS (ktx/s)', fontsize=AXIS_LABEL_FONTSIZE, fontweight='bold')
    ax2.set_ylabel('Follower Avg CPU (%)', fontsize=AXIS_LABEL_FONTSIZE, fontweight='bold')
    ax2.grid(True, alpha=0.7, linestyle='--', linewidth=0.8)
    ax2.legend(loc='lower center', bbox_to_anchor=(0.5, 1.01), ncol=2,
               fontsize=LEGEND_FONTSIZE, framealpha=0.9)
    ax2.tick_params(axis='both', labelsize=TICK_LABEL_FONTSIZE)
    
    # 保存合并图表
    output_path = os.path.join(output_dir, 'total_tps_cpu_comparison.png')
    fig.tight_layout()
    plt.savefig(output_path, dpi=DPI, bbox_inches='tight')
    plt.savefig(output_path.replace('.png', '.pdf'), bbox_inches='tight')
    print(f"\n[OK] 总TPS-CPU对比图（合并）已保存到: {output_path}")
    plt.close()
    
    # 输出 Leader CPU 单图
    fig_leader, ax_leader = plt.subplots(figsize=FIGURE_SIZE_SINGLE, dpi=DPI)
    
    for method in method_order:
        if method not in data:
            continue
        
        sorted_data = sorted(data[method], key=lambda x: x['concurrency'])
        total_tps_list = [item['tps'] / 1000.0 for item in sorted_data]
        leader_cpu = [item['leader_cpu'] for item in sorted_data]
        
        color = COLORS[COLOR_INDICES[method]]
        ax_leader.plot(total_tps_list, leader_cpu,
                marker='o',
                linewidth=3.5,
                markersize=MARKER_SIZE,
                label=METHOD_NAMES.get(method, method),
                color=color)
    
    ax_leader.set_xlabel('Total TPS (ktx/s)', fontsize=AXIS_LABEL_FONTSIZE, fontweight='bold')
    ax_leader.set_ylabel('Leader Avg CPU (%)', fontsize=AXIS_LABEL_FONTSIZE, fontweight='bold')
    ax_leader.grid(True, alpha=0.7, linestyle='--', linewidth=0.8)
    ax_leader.legend(loc='lower center', bbox_to_anchor=(0.5, 1.01), ncol=2,
                     fontsize=LEGEND_FONTSIZE, framealpha=0.9)
    ax_leader.tick_params(axis='both', labelsize=TICK_LABEL_FONTSIZE)
    
    output_path_leader = os.path.join(output_dir, 'total_tps_leader_cpu_placement2.png')
    fig_leader.tight_layout()
    plt.savefig(output_path_leader, dpi=DPI, bbox_inches='tight')
    plt.savefig(output_path_leader.replace('.png', '.pdf'), bbox_inches='tight')
    print(f"[OK] Leader CPU单图已保存到: {output_path_leader}")
    plt.close()
    
    # 输出 Follower CPU 单图
    fig_follower, ax_follower = plt.subplots(figsize=FIGURE_SIZE_SINGLE, dpi=DPI)
    
    for method in method_order:
        if method not in data:
            continue
        
        sorted_data = sorted(data[method], key=lambda x: x['concurrency'])
        total_tps_list = [item['tps'] / 1000.0 for item in sorted_data]
        follower_cpu = [item['follower_cpu'] for item in sorted_data]
        
        color = COLORS[COLOR_INDICES[method]]
        ax_follower.plot(total_tps_list, follower_cpu,
                marker='s',
                linewidth=3.5,
                markersize=MARKER_SIZE,
                label=METHOD_NAMES.get(method, method),
                color=color)
    
    ax_follower.set_xlabel('Total TPS (ktx/s)', fontsize=AXIS_LABEL_FONTSIZE, fontweight='bold')
    ax_follower.set_ylabel('Follower Avg CPU (%)', fontsize=AXIS_LABEL_FONTSIZE, fontweight='bold')
    ax_follower.grid(True, alpha=0.7, linestyle='--', linewidth=0.8)
    ax_follower.legend(loc='lower center', bbox_to_anchor=(0.5, 1.01), ncol=2,
                       fontsize=LEGEND_FONTSIZE, framealpha=0.9)
    ax_follower.tick_params(axis='both', labelsize=TICK_LABEL_FONTSIZE)
    
    output_path_follower = os.path.join(output_dir, 'total_tps_follower_cpu_placement2.png')
    fig_follower.tight_layout()
    plt.savefig(output_path_follower, dpi=DPI, bbox_inches='tight')
    plt.savefig(output_path_follower.replace('.png', '.pdf'), bbox_inches='tight')
    print(f"[OK] Follower CPU单图已保存到: {output_path_follower}")
    plt.close()


def plot_cpu_vertical_combined(data: Dict[str, List[Dict]], output_dir: str):
    """
    绘制上下布局的CPU图（Leader在上，Follower在下），共享横坐标和图例
    """
    os.makedirs(output_dir, exist_ok=True)
    
    method_order = ['global', 'neighbor', 'clique', 'threshold']
    
    # 创建上下两个子图，共享X轴
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(8, 8), dpi=DPI, sharex=True)
    
    # 绘制 Leader CPU (上图)
    for method in method_order:
        if method not in data:
            continue
        
        sorted_data = sorted(data[method], key=lambda x: x['concurrency'])
        total_tps_list = [item['tps'] / 1000.0 for item in sorted_data]
        leader_cpu = [item['leader_cpu'] for item in sorted_data]
        
        color = COLORS[COLOR_INDICES[method]]
        ax1.plot(total_tps_list, leader_cpu,
                marker='o',
                linewidth=3.5,
                markersize=MARKER_SIZE,
                label=METHOD_NAMES.get(method, method),
                color=color)
    
    ax1.set_ylabel('Leader Avg CPU (%)', fontsize=AXIS_LABEL_FONTSIZE, fontweight='bold')
    ax1.grid(True, alpha=0.7, linestyle='--', linewidth=0.8)
    ax1.tick_params(axis='y', labelsize=TICK_LABEL_FONTSIZE)
    ax1.tick_params(axis='x', labelsize=TICK_LABEL_FONTSIZE, labelbottom=False)
    
    # 绘制 Follower CPU (下图)
    for method in method_order:
        if method not in data:
            continue
        
        sorted_data = sorted(data[method], key=lambda x: x['concurrency'])
        total_tps_list = [item['tps'] / 1000.0 for item in sorted_data]
        follower_cpu = [item['follower_cpu'] for item in sorted_data]
        
        color = COLORS[COLOR_INDICES[method]]
        ax2.plot(total_tps_list, follower_cpu,
                marker='s',
                linewidth=3.5,
                markersize=MARKER_SIZE,
                color=color)
    
    ax2.set_xlabel('Total TPS (ktx/s)', fontsize=AXIS_LABEL_FONTSIZE, fontweight='bold')
    ax2.set_ylabel('Follower Avg CPU (%)', fontsize=AXIS_LABEL_FONTSIZE, fontweight='bold')
    ax2.grid(True, alpha=0.7, linestyle='--', linewidth=0.8)
    ax2.tick_params(axis='both', labelsize=TICK_LABEL_FONTSIZE)
    
    # 获取图例句柄和标签
    handles, labels = ax1.get_legend_handles_labels()
    
    # 将图例放在最上方
    fig.legend(handles, labels, loc='upper center', bbox_to_anchor=(0.5, 0.98),
              ncol=2, fontsize=LEGEND_FONTSIZE, framealpha=0.9, edgecolor='gray')
    
    # 调整布局，为顶部图例留出空间
    fig.tight_layout(rect=(0.04, 0.02, 0.98, 0.90))
    
    output_path = os.path.join(output_dir, 'total_tps_cpu_vertical.png')
    plt.savefig(output_path, dpi=DPI, bbox_inches='tight')
    plt.savefig(output_path.replace('.png', '.pdf'), bbox_inches='tight')
    print(f"[OK] CPU上下布局图已保存到: {output_path}")
    plt.close()


def load_data_from_csv(csv_path: str) -> Dict[str, List[Dict]]:
    """
    从已导出的CSV文件加载结果
    """
    if not os.path.exists(csv_path):
        print(f"[WARNING] CSV文件不存在: {csv_path}")
        return {}

    df = pd.read_csv(csv_path)
    if df.empty:
        print(f"[WARNING] CSV文件为空: {csv_path}")
        return {}

    all_data = defaultdict(list)

    for _, row in df.iterrows():
        method = row.get('Method')
        if pd.isna(method):
            continue
        method = str(method)
        
        try:
            concurrency = int(row.get('Concurrency', 0))
            total_tps = float(row.get('Total_TPS', 0))
            leader_cpu = float(row.get('Leader_Avg_CPU', 0.0) or 0.0)
            follower_cpu = float(row.get('Follower_Avg_CPU', 0.0) or 0.0)
        except (TypeError, ValueError):
            continue

        # 处理时间字段
        start_time = row.get('Run_Start')
        end_time = row.get('Run_End')
        if isinstance(start_time, str):
            start_time = pd.to_datetime(start_time)
        if isinstance(end_time, str):
            end_time = pd.to_datetime(end_time)

        all_data[method].append({
            'concurrency': concurrency,
            'tps': total_tps,
            'leader_cpu': leader_cpu,
            'follower_cpu': follower_cpu,
            'start_time': start_time,
            'end_time': end_time
        })

    print(f"[INFO] 从CSV加载 {sum(len(v) for v in all_data.values())} 条记录")
    return dict(all_data)


def export_to_csv(data: Dict[str, List[Dict]], output_dir: str):
    """
    导出数据到CSV（按并发量排序）
    """
    os.makedirs(output_dir, exist_ok=True)
    
    rows = []
    for method in sorted(data.keys()):
        # 按并发量排序
        sorted_items = sorted(data[method], key=lambda x: x['concurrency'])
        for item in sorted_items:
            row = {
                'Method': method,
                'Concurrency': item['concurrency'],
                'Total_TPS': round(item['tps'], 2),
                'Leader_Avg_CPU': round(item['leader_cpu'], 2),
                'Follower_Avg_CPU': round(item['follower_cpu'], 2),
                'Run_Start': item['start_time'].strftime('%Y-%m-%d %H:%M:%S'),
                'Run_End': item['end_time'].strftime('%Y-%m-%d %H:%M:%S')
            }
            rows.append(row)
    
    df = pd.DataFrame(rows)
    output_path = os.path.join(output_dir, 'total_tps_cpu_data.csv')
    df.to_csv(output_path, index=False)
    print(f"[OK] 数据已导出到: {output_path}")


def main():
    """从两套已处理的 CSV 数据生成 TPS--CPU 图。"""
    print("=" * 80)
    print("TPS-CPU总体分析（Balanced / Worst-case）")
    print("=" * 80)

    for dataset in DATASETS:
        csv_path = os.path.join(OUTPUT_DIR, dataset['csv'])
        print(f"\n--- 数据集: {dataset['label']} ({dataset['key']}) ---")
        all_data = load_data_from_csv(csv_path)
        if not all_data:
            print(f"[WARNING] 无法加载 {csv_path}，跳过该数据集")
            continue

        plot_dir = os.path.join(OUTPUT_DIR, dataset['key'])
        print(f"--- 绘制图表 -> {plot_dir} ---")
        plot_total_tps_cpu(all_data, plot_dir)
        plot_cpu_vertical_combined(all_data, plot_dir)

    print("\n" + "=" * 80)
    print("[OK] 分析完成！")
    print("=" * 80)


if __name__ == '__main__':
    main()

"""
export_cluster_config.py

从 Python 分区结果导出 Go 集群可用的配置文件：
  - output/handover_cluster/cluster_config.yaml  (Go 服务端 + Docker 部署)
  - output/handover_cluster/scenario.json        (Go Handover 客户端)

Usage:
    python export_cluster_config.py [--waypoints 0,4,8,9,12,0] [--speed 25] [--dwell 10]
"""

import sys
import os
import json
import argparse
import yaml
import numpy as np
import networkx as nx

sys.path.insert(0, os.path.dirname(__file__))

from src.topology.spatial_generator import SpatialTopologyGenerator
from src.topology.mec_builder import MECTopologyBuilder
from src.partition.strategies import PartitionStrategies


# Docker 网络配置（与现有 test-full-topology-benchmark.sh 一致）
SUBNET_PREFIX = "172.44"
BASE_RAFT_PORT = 33000
BASE_API_PORT = 43000
NETWORK_NAME = "handover_full_network"

# 实验参数
REQUEST_INTERVAL_MS = 0   # 请求间隔 (ms)，0=压测模式(不等待), 10=100req/s, 50=20req/s
CONCURRENCY_LIST = [1,2,4,8,10,20,40,60,80,100,120,140,160,180,200,220,240,260,280,300]  # 并发压测数组，--single 模式下遍历


def generate_topology(num_nodes=15, seed=42):
    """生成空间拓扑 + 分区"""
    gen = SpatialTopologyGenerator(
        area_size=2000, density=4e-6,
        coverage_radius=300, coverage_std=50,
        random_seed=seed
    )
    spatial = gen.generate_with_fixed_count(num_nodes)
    builder = MECTopologyBuilder()
    G = builder.build_from_spatial(spatial)

    if not nx.is_connected(G):
        G = builder.extract_largest_component(G, verbose=False)
        nodes_in_graph = set(G.nodes())
        spatial.base_stations = [bs for bs in spatial.base_stations if bs.id in nodes_in_graph]
        spatial.num_stations = len(spatial.base_stations)

    ps = PartitionStrategies(G)
    regions_list = ps.threshold_based(4)
    # region ID 从 1 开始（Dragonboat 要求 cluster_id > 0）
    regions = {i + 1: set(r) for i, r in enumerate(regions_list)}

    return spatial, G, regions


def compute_leader_placement(G, regions, strategy="balanced"):
    """计算 leader 放置"""
    placement = {}
    if strategy == "balanced":
        node_leader_count = {n: 0 for n in G.nodes()}
        for rid, members in sorted(regions.items()):
            best_node = min(members, key=lambda n: node_leader_count.get(n, 0))
            placement[rid] = best_node
            node_leader_count[best_node] = node_leader_count.get(best_node, 0) + 1
    else:
        for rid, members in regions.items():
            placement[rid] = min(members)
    return placement


def find_valid_waypoints(node_regions_map, all_nodes, num_waypoints=5):
    """找到相邻节点有共同 Region 的途径点路径"""
    import itertools

    best_path = None
    best_shared_count = 0

    for path in itertools.combinations(all_nodes, min(num_waypoints, len(all_nodes))):
        valid = True
        shared_count = 0
        for i in range(len(path) - 1):
            r1 = set(node_regions_map[path[i]])
            r2 = set(node_regions_map[path[i + 1]])
            shared = r1 & r2
            if not shared:
                valid = False
                break
            shared_count += len(shared)
        if valid and shared_count > best_shared_count:
            best_path = list(path)
            best_shared_count = shared_count

    if best_path:
        best_path.append(best_path[0])  # 闭合回路
    return best_path


def export_cluster_config(spatial, G, regions, leader_placement, output_dir):
    """导出 cluster_config.yaml"""
    os.makedirs(output_dir, exist_ok=True)

    # 构建节点配置
    node_regions_map = {}
    for bs in spatial.base_stations:
        nid = bs.id
        nregions = sorted([rid for rid, members in regions.items() if nid in members])
        node_regions_map[nid] = nregions

    mec_nodes = []
    for bs in spatial.base_stations:
        nid = bs.id
        ip = f"{SUBNET_PREFIX}.0.{10 + nid}"
        mec_nodes.append({
            "name": f"mec-node-{nid}",
            "id": nid,
            "address": f"{ip}:{BASE_RAFT_PORT + nid}",
            "api_port": BASE_API_PORT + nid,
            "regions": node_regions_map[nid],
            "x": round(float(bs.x), 1),
            "y": round(float(bs.y), 1),
            "coverage_radius": round(float(bs.coverage_radius), 1),
        })

    # Region 配置
    region_configs = []
    for rid, members in sorted(regions.items()):
        region_configs.append({
            "id": rid,
            "name": f"Region-{rid}",
            "members": sorted(members),
            "initial_leader": leader_placement[rid],
        })

    # Leader placement 字符串（与现有脚本格式一致）
    lp_str = ",".join(f"{rid}:{nid}" for rid, nid in sorted(leader_placement.items()))

    config = {
        "mec_nodes": mec_nodes,
        "regions": region_configs,
        "benchmark": {
            "duration": "60s",
            "base_port": 30000,
            "data_dir": "/data",
            "proposal_size": 128,
            "method_name": "handover",
        },
        "handover": {
            "leader_strategy": "balanced",
            "leader_placement": lp_str,
            "partition_method": "threshold_based",
            "partition_params": {"tau": 4},
            "network_name": NETWORK_NAME,
            "subnet": f"{SUBNET_PREFIX}.0.0/16",
        },
    }

    yaml_path = os.path.join(output_dir, "cluster_config.yaml")
    with open(yaml_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    print(f"  Saved: {yaml_path}")
    return config, node_regions_map


def export_scenario(spatial, G, regions, node_regions_map, leader_placement,
                    waypoints, speed, dwell_s, output_dir, seed=42,
                    request_interval_ms=50, timed_ticks=None, concurrency=1):
    """导出 scenario.json

    三种模式（优先级从高到低）：
    1. timed_ticks 非空: 从选择器导出的 tick-level 时序生成变长 dwell schedule
    2. dwell_s > 0: 固定驻留模式（每个节点停留 dwell_s 秒，handover 瞬时）
    3. dwell_s <= 0: 实际飞行时间模式（根据节点间距离和 speed 计算飞行时间）
    """
    os.makedirs(output_dir, exist_ok=True)

    # 节点地址映射
    node_addresses = {}
    for bs in spatial.base_stations:
        nid = bs.id
        ip = f"{SUBNET_PREFIX}.0.{10 + nid}"
        node_addresses[str(nid)] = f"{ip}:{BASE_API_PORT + nid}"

    # 节点坐标映射
    node_pos = {bs.id: (bs.x, bs.y) for bs in spatial.base_stations}

    # 构建 schedule
    schedule = []
    current_ms = 0

    if timed_ticks:
        # Tick-level 时序模式：将连续相同节点的 ticks 合并为一个 dwell phase
        phases = []  # [(node_id, start_ms, end_ms), ...]
        cur_node = timed_ticks[0]["node_id"]
        cur_start = timed_ticks[0]["time_ms"]
        for tt in timed_ticks[1:]:
            if tt["node_id"] != cur_node:
                phases.append((cur_node, cur_start, tt["time_ms"]))
                cur_node = tt["node_id"]
                cur_start = tt["time_ms"]
        phases.append((cur_node, cur_start, timed_ticks[-1]["time_ms"] + request_interval_ms))

        for i, (node_id, start_ms, end_ms) in enumerate(phases):
            schedule.append({
                "phase": "dwell",
                "node_id": node_id,
                "start_ms": start_ms,
                "end_ms": end_ms,
            })
            if i < len(phases) - 1:
                next_node = phases[i + 1][0]
                schedule.append({
                    "phase": "handover",
                    "from_node": node_id,
                    "to_node": next_node,
                    "time_ms": end_ms,
                })
        end_ms = phases[-1][2]

    elif dwell_s > 0:
        # 固定驻留模式
        for i, wp in enumerate(waypoints[:-1]):
            next_wp = waypoints[i + 1]

            end_ms = current_ms + int(dwell_s * 1000)
            schedule.append({
                "phase": "dwell",
                "node_id": wp,
                "start_ms": current_ms,
                "end_ms": end_ms,
            })
            schedule.append({
                "phase": "handover",
                "from_node": wp,
                "to_node": next_wp,
                "time_ms": end_ms,
            })
            current_ms = end_ms

        last_wp = waypoints[-1]
        end_ms = current_ms + int(dwell_s * 1000)
        schedule.append({
            "phase": "dwell",
            "node_id": last_wp,
            "start_ms": current_ms,
            "end_ms": end_ms,
        })
    else:
        # 实际飞行时间模式
        for i, wp in enumerate(waypoints[:-1]):
            next_wp = waypoints[i + 1]

            x1, y1 = node_pos[wp]
            x2, y2 = node_pos[next_wp]
            dist_m = float(np.sqrt((x2 - x1)**2 + (y2 - y1)**2))
            flight_ms = int((dist_m / speed) * 1000) if speed > 0 else 0

            end_ms = current_ms + flight_ms
            schedule.append({
                "phase": "dwell",
                "node_id": wp,
                "start_ms": current_ms,
                "end_ms": end_ms,
                "flight_distance_m": round(dist_m, 1),
                "flight_time_ms": flight_ms,
            })
            schedule.append({
                "phase": "handover",
                "from_node": wp,
                "to_node": next_wp,
                "time_ms": end_ms,
            })
            current_ms = end_ms

        last_wp = waypoints[-1]
        end_ms = current_ms + 3000
        schedule.append({
            "phase": "dwell",
            "node_id": last_wp,
            "start_ms": current_ms,
            "end_ms": end_ms,
        })

    # 分析 Handover 的 Region 交集
    handover_analysis = []
    for i in range(len(waypoints) - 1):
        from_n = waypoints[i]
        to_n = waypoints[i + 1]
        from_r = set(node_regions_map[from_n])
        to_r = set(node_regions_map[to_n])
        shared = sorted(from_r & to_r)
        handover_analysis.append({
            "from_node": from_n,
            "to_node": to_n,
            "from_regions": sorted(from_r),
            "to_regions": sorted(to_r),
            "shared_regions": shared,
            "can_verify": len(shared) > 0,
        })

    scenario = {
        "config": {
            "trajectory_mode": "custom",
            "schedule_mode": "timed_ticks" if timed_ticks else ("fixed_dwell" if dwell_s > 0 else "flight_time"),
            "waypoints": waypoints,
            "uav_speed_mps": speed,
            "dwell_s": dwell_s if dwell_s > 0 else None,
            "request_interval_ms": request_interval_ms,
            "concurrency": concurrency,
            "seed": seed,
        },
        "node_addresses": node_addresses,
        "node_regions": {str(k): v for k, v in node_regions_map.items()},
        "leader_placement": {str(rid): nid for rid, nid in leader_placement.items()},
        "schedule": schedule,
        "handover_analysis": handover_analysis,
        "total_duration_ms": end_ms,
    }

    json_path = os.path.join(output_dir, "scenario.json")
    with open(json_path, "w") as f:
        json.dump(scenario, f, indent=2)

    print(f"  Saved: {json_path}")
    return scenario


def main():
    parser = argparse.ArgumentParser(description="Export cluster config for handover experiment")
    parser.add_argument("--waypoints", type=str, default=None,
                        help="Comma-separated node IDs (e.g. 0,4,8,9,12,0)")
    parser.add_argument("--speed", type=float, default=25,
                        help="UAV speed in m/s")
    parser.add_argument("--dwell", type=float, default=0,
                        help="Dwell time at each waypoint in seconds (0 = use actual flight time)")
    parser.add_argument("--interval", type=int, default=REQUEST_INTERVAL_MS,
                        help="Request interval in ms (default %(default)d)")
    parser.add_argument("--concurrency", type=int, default=CONCURRENCY_LIST[0],
                        help="Concurrent workers per dwell phase (default %(default)d)")
    parser.add_argument("--ticks", type=str, default=None,
                        help="Path to timed_ticks.json (from selector, overrides --dwell)")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output-dir", type=str, default="output/handover_cluster")
    parser.add_argument("--real", action='store_true',
                        help="Use real Beijing base station topology")
    parser.add_argument("--partition", type=str, default="threshold",
                        choices=["threshold", "neighbor", "clique", "global"],
                        help="Partition method for real topology")
    parser.add_argument("--tau", type=int, default=5,
                        help="Threshold tau for real topology")
    args = parser.parse_args()

    print("=" * 60)
    print("  Export Cluster Config for Handover Experiment")
    print("=" * 60)

    # 生成拓扑
    if args.real:
        from interactive_waypoint_selector import setup_real_topology
        spatial, G, regions, _ = setup_real_topology(
            partition_method=args.partition,
            threshold_tau=args.tau
        )
        print(f"\n  Using REAL Beijing topology (partition={args.partition}, tau={args.tau})")
    else:
        spatial, G, regions = generate_topology(num_nodes=15, seed=args.seed)
    leader_placement = compute_leader_placement(G, regions, "balanced")

    print(f"\n  Topology: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")
    print(f"  Regions: {len(regions)}")

    # 节点 Region 映射
    node_regions_map = {}
    for bs in spatial.base_stations:
        nid = bs.id
        nregions = sorted([rid for rid, members in regions.items() if nid in members])
        node_regions_map[nid] = nregions

    print(f"\n  Node → Regions:")
    for nid in sorted(node_regions_map.keys()):
        leader_of = [rid for rid, lid in leader_placement.items() if lid == nid]
        leader_str = f" (leader of R{leader_of})" if leader_of else ""
        print(f"    Node {nid:>2}: Region {node_regions_map[nid]}{leader_str}")

    # 确定途径点
    if args.waypoints:
        waypoints = [int(x) for x in args.waypoints.split(",")]
    else:
        all_nodes = sorted(node_regions_map.keys())
        waypoints = find_valid_waypoints(node_regions_map, all_nodes, num_waypoints=5)
        if not waypoints:
            print("\n  WARNING: No valid path found, using default")
            waypoints = [all_nodes[0], all_nodes[1], all_nodes[2], all_nodes[0]]

    print(f"\n  Waypoints: {waypoints}")
    print(f"  Handover analysis:")
    for i in range(len(waypoints) - 1):
        from_n = waypoints[i]
        to_n = waypoints[i + 1]
        from_r = set(node_regions_map[from_n])
        to_r = set(node_regions_map[to_n])
        shared = from_r & to_r
        status = f"shared R{sorted(shared)}" if shared else "NO SHARED REGION!"
        print(f"    HO{i+1}: Node {from_n} → Node {to_n}: {status}")

    # 加载 timed_ticks（如果提供）
    timed_ticks = None
    if args.ticks:
        with open(args.ticks) as f:
            timed_ticks = json.load(f)
        print(f"\n  Loaded timed_ticks: {len(timed_ticks)} ticks from {args.ticks}")

    # 导出
    print(f"\n  Exporting to {args.output_dir}/...")
    config, _ = export_cluster_config(spatial, G, regions, leader_placement, args.output_dir)
    scenario = export_scenario(
        spatial, G, regions, node_regions_map, leader_placement,
        waypoints, args.speed, args.dwell, args.output_dir, args.seed,
        request_interval_ms=args.interval, timed_ticks=timed_ticks,
        concurrency=args.concurrency
    )

    # 打印摘要
    total_s = scenario["total_duration_ms"] / 1000
    num_ho = len(waypoints) - 1
    verifiable = sum(1 for h in scenario["handover_analysis"] if h["can_verify"])
    print(f"\n  Summary:")
    print(f"    Total duration: {total_s:.0f}s")
    print(f"    Handovers: {num_ho} ({verifiable} verifiable)")
    req_interval = scenario['config']['request_interval_ms']
    if req_interval and req_interval > 0:
        print(f"    Requests (est): {int(total_s * 1000 / req_interval)}")
    else:
        print(f"    Requests: BURST mode (as fast as possible)")
    print(f"    Leader placement: {config['handover']['leader_placement']}")

    print(f"\n{'='*60}")
    print(f"  Done! Files saved to {args.output_dir}/")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()

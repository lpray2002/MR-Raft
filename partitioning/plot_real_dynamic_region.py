"""
基于真实 OpenCelliD 拓扑绘制动态一致性分区更新过程。

复用 test_dynamic_region.py 中的 run_dynamic_test / draw_partition_state 风格，
输出适合放入论文或中期汇报 PPT 的真实拓扑动态更新图。
"""

import os
import sys

import networkx as nx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.partition.strategies import PartitionStrategies
from test_dynamic_region import run_dynamic_test
from run import (
    load_real_topology,
    REAL_DATA_CENTER_LAT,
    REAL_DATA_CENTER_LON,
    REAL_DATA_RADIUS_KM,
    REAL_DATA_N_MEC,
    REAL_DATA_COVERAGE_MIN,
    REAL_DATA_COVERAGE_MAX,
)


TAU = 5
OUTPUT_DIR = "output/real_dynamic"


def load_real_mec_graph():
    """加载 run.py 中配置的真实 MEC 覆盖图。"""
    _, graph, _ = load_real_topology()
    return graph


def choose_add_edges(graph, limit=5):
    """
    选择候选新增边：优先选择原图中距离为2、共同邻居较多的非邻接节点对，
    模拟覆盖范围扩大后产生新的局部重叠关系。
    """
    candidates = []
    nodes = sorted(graph.nodes())
    for i, u in enumerate(nodes):
        for v in nodes[i + 1:]:
            if graph.has_edge(u, v):
                continue
            try:
                dist = nx.shortest_path_length(graph, u, v)
            except nx.NetworkXNoPath:
                continue
            if dist <= 3:
                common = len(set(graph.neighbors(u)) & set(graph.neighbors(v)))
                score = (common, -dist, graph.degree(u) + graph.degree(v))
                candidates.append((score, u, v))

    candidates.sort(reverse=True)
    return [{"type": "add", "edge": (u, v)} for _, u, v in candidates[:limit]]


def choose_remove_edges(graph, limit=5):
    """
    选择候选删除边：优先删除非桥边，避免过早把真实拓扑割裂；
    若非桥边不足，再补充普通边。
    """
    bridges = {tuple(sorted(e)) for e in nx.bridges(graph)}
    scored = []
    fallback = []
    for u, v in graph.edges():
        common = len(set(graph.neighbors(u)) & set(graph.neighbors(v)))
        item = ((common, graph.degree(u) + graph.degree(v)), u, v)
        if tuple(sorted((u, v))) in bridges:
            fallback.append(item)
        else:
            scored.append(item)

    scored.sort(reverse=True)
    fallback.sort(reverse=True)
    chosen = scored[:limit]
    if len(chosen) < limit:
        chosen += fallback[: limit - len(chosen)]
    return [{"type": "remove", "edge": (u, v)} for _, u, v in chosen]


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    graph = load_real_mec_graph()
    strategies = PartitionStrategies(graph)
    initial_regions = strategies.threshold_based(TAU)

    print("=" * 72)
    print("Real topology dynamic region visualization")
    print(
        "run.py settings: "
        f"center=({REAL_DATA_CENTER_LAT}, {REAL_DATA_CENTER_LON}), "
        f"radius={REAL_DATA_RADIUS_KM}km, n_mec={REAL_DATA_N_MEC}, "
        f"coverage={REAL_DATA_COVERAGE_MIN}-{REAL_DATA_COVERAGE_MAX}m"
    )
    print(f"Nodes: {graph.number_of_nodes()}, edges: {graph.number_of_edges()}, tau={TAU}")
    print(f"Initial regions: {len(initial_regions)}")

    add_ops = choose_add_edges(graph, limit=5)
    remove_ops = choose_remove_edges(graph, limit=5)
    mixed_ops = []
    for add_op, remove_op in zip(add_ops[:3], remove_ops[:3]):
        mixed_ops.append(add_op)
        mixed_ops.append(remove_op)

    print("Add ops:", [op["edge"] for op in add_ops])
    print("Remove ops:", [op["edge"] for op in remove_ops])

    run_dynamic_test(
        graph,
        initial_regions,
        tau=TAU,
        operations=add_ops,
        test_name=f"Real Topology: Edge Additions (tau={TAU})",
        output_file=f"{OUTPUT_DIR}/real_dynamic_additions.png",
    )

    run_dynamic_test(
        graph,
        initial_regions,
        tau=TAU,
        operations=remove_ops,
        test_name=f"Real Topology: Edge Removals (tau={TAU})",
        output_file=f"{OUTPUT_DIR}/real_dynamic_removals.png",
    )

    run_dynamic_test(
        graph,
        initial_regions,
        tau=TAU,
        operations=mixed_ops,
        test_name=f"Real Topology: Mixed Changes (tau={TAU})",
        output_file=f"{OUTPUT_DIR}/real_dynamic_mixed.png",
    )

    print(f"Saved figures to {OUTPUT_DIR}")


if __name__ == "__main__":
    main()

"""
动态区域更新可视化验证
逐步展示 Algorithm 3/4 和批量更新的执行过程
风格与 run.py / TopologyPlotter.plot_partitions 一致
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '.'))

import networkx as nx
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from copy import deepcopy
from src.partition.dynamic_region import DynamicRegionManager, DynamicRegionManagerOptimized
from src.partition.strategies import PartitionStrategies
from src.visualization.plotter import COLORS


def draw_partition_state(ax, graph, regions_dict, title='',
                         pos=None, highlight_edges=None,
                         highlight_edge_color='#00CC00'):
    """
    绘制单个分区状态（与 TopologyPlotter.plot_partitions 风格一致）
    """
    ax.clear()
    
    if pos is None:
        pos = nx.kamada_kawai_layout(graph)
    
    # 绘制边 - 深色、加粗
    edge_list = list(graph.edges())
    edge_colors = []
    edge_widths = []
    for u, v in edge_list:
        if highlight_edges and ((u, v) in highlight_edges or (v, u) in highlight_edges):
            edge_colors.append(highlight_edge_color)
            edge_widths.append(4.0)
        else:
            edge_colors.append('dimgray')
            edge_widths.append(3.0)
    
    nx.draw_networkx_edges(graph, pos, ax=ax, edgelist=edge_list,
                           edge_color=edge_colors, alpha=0.8, width=edge_widths)
    
    # 按分区绘制节点
    sorted_rids = sorted(regions_dict.keys())
    for idx, rid in enumerate(sorted_rids):
        part = regions_dict[rid]
        nodelist = [n for n in part if graph.has_node(n)]
        if nodelist:
            nx.draw_networkx_nodes(
                graph, pos, ax=ax,
                nodelist=nodelist,
                node_color=COLORS[idx % len(COLORS)],
                node_size=700,
                edgecolors='k',
                linewidths=2
            )
    
    # 不属于任何 region 的节点（灰色）
    all_in_regions = set()
    for R in regions_dict.values():
        all_in_regions.update(R)
    orphans = [n for n in graph.nodes() if n not in all_in_regions]
    if orphans:
        nx.draw_networkx_nodes(graph, pos, ax=ax, nodelist=orphans,
                               node_color='#CCCCCC', node_size=700,
                               edgecolors='k', linewidths=2)
    
    # 标签：节点ID [所属分区编号列表]
    node_to_partitions = {node: [] for node in graph.nodes()}
    for rid in sorted_rids:
        for node in regions_dict[rid]:
            if graph.has_node(node):
                node_to_partitions[node].append(rid)
    
    y_coords = [y for x, y in pos.values()]
    y_range = max(y_coords) - min(y_coords) if y_coords else 1.0
    label_offset = y_range * 0.04
    
    for node, (x, y) in pos.items():
        groups = node_to_partitions.get(node, [])
        label_text = ','.join(str(g) for g in groups)
        ax.text(x, y + label_offset, f"{node} [{label_text}]",
                fontsize=10, ha='center', va='bottom', fontweight='bold')
    
    if title:
        ax.set_title(title, fontsize=12, fontweight='bold', pad=10)
    
    ax.axis('off')
    return pos


def run_dynamic_test(graph, initial_regions, tau, operations, 
                     test_name="Dynamic Test", output_file=None):
    """
    运行动态更新测试并生成逐步可视化
    """
    manager = DynamicRegionManager(graph, initial_regions, tau=tau)
    
    # 计算固定布局（包含所有可能出现的节点）
    all_nodes = set(graph.nodes())
    for op in operations:
        if op["type"] in ("add", "remove"):
            u, v = op["edge"]
            all_nodes.add(u)
            all_nodes.add(v)
        elif op["type"] == "batch":
            for _, u, v in op["changes"]:
                all_nodes.add(u)
                all_nodes.add(v)
    
    layout_graph = graph.copy()
    for n in all_nodes:
        if not layout_graph.has_node(n):
            layout_graph.add_node(n)
    pos = nx.kamada_kawai_layout(layout_graph)
    
    # 计算子图布局
    num_steps = len(operations) + 1
    cols = min(4, num_steps)
    rows = (num_steps + cols - 1) // cols
    
    fig, axes = plt.subplots(rows, cols, figsize=(6 * cols, 6 * rows))
    if rows == 1 and cols == 1:
        axes = np.array([axes])
    axes = np.array(axes).flatten()
    
    # Step 0: 初始状态
    draw_partition_state(axes[0], manager.graph, manager.regions,
                         title="Step 0: Initial", pos=pos)
    
    print(f"\n{'='*60}")
    print(f"  {test_name}")
    print(f"{'='*60}")
    print(f"  τ = {tau}")
    print(f"  Initial regions ({len(manager.regions)}):")
    for rid in sorted(manager.regions.keys()):
        print(f"    R{rid}: {sorted(manager.regions[rid])}")
    
    # 执行每步操作
    for step_idx, op in enumerate(operations, 1):
        op_type = op["type"]
        
        if op_type == "add":
            u, v = op["edge"]
            manager.edge_addition(u, v)
            highlight = [(u, v)]
            color = '#00CC00'
            desc = f"Add ({u},{v})"
            
        elif op_type == "remove":
            u, v = op["edge"]
            highlight = []
            manager.edge_removal(u, v)
            desc = f"Remove ({u},{v})"
            
        elif op_type == "batch":
            changes = op["changes"]
            add_edges = [(u, v) for a, u, v in changes if a == '+']
            manager.batch_update(changes)
            highlight = add_edges
            color = '#00CC00'
            desc = f"Batch ({len(changes)} ops)"
        
        draw_partition_state(axes[step_idx], manager.graph, manager.regions,
                             title=f"Step {step_idx}: {desc}", pos=pos,
                             highlight_edges=highlight,
                             highlight_edge_color=color if op_type != "remove" else '#FF3333')
        
        val = manager.validate_constraints()
        status_color = 'green' if (val["coverage_satisfied"] and 
                                    val["completeness_satisfied"]) else 'red'
        status_text = "OK" if status_color == 'green' else "VIOLATED"
        axes[step_idx].text(0.5, -0.02, f"Constraints: {status_text} | Regions: {val['num_regions']}",
                           transform=axes[step_idx].transAxes,
                           ha='center', fontsize=9, color=status_color,
                           fontweight='bold')
        
        print(f"  Step {step_idx}: {desc}")
        print(f"    Regions ({len(manager.regions)}):", end="")
        for rid in sorted(manager.regions.keys()):
            print(f" R{rid}={sorted(manager.regions[rid])}", end="")
        print(f"\n    Constraints: {status_text}")
        if not nx.is_connected(manager.graph):
            components = list(nx.connected_components(manager.graph))
            print(f"    ⚠ Graph disconnected! Components ({len(components)}):")
            for ci, c in enumerate(components):
                print(f"      C{ci}: {sorted(c)}")
    
    # 隐藏多余子图
    for idx in range(num_steps, len(axes)):
        axes[idx].axis('off')
    
    plt.tight_layout()
    
    if output_file:
        os.makedirs(os.path.dirname(output_file) if os.path.dirname(output_file) else 'output', 
                    exist_ok=True)
        plt.savefig(output_file, bbox_inches='tight', dpi=150)
        print(f"  Saved: {output_file}")
    
    plt.close()
    return manager


# =============================================================================
# 配置参数（直接修改这里）
# =============================================================================

TAU = 4  # 阈值分区的阈值参数


# =============================================================================
# 基础测试图：12节点，多个重叠三角形
# =============================================================================

def build_test_graph():
    """
    构建测试图：12节点，4个三角形通过共享节点连接
    
    结构：
    三角形1: (0,1,2)  三角形2: (2,3,4)  三角形3: (4,5,6)  三角形4: (6,7,8)
    额外连接: (3,5)
    分支: 1-9, (9,10,11) 三角形
    """
    G = nx.Graph()
    G.add_edges_from([
        (0, 1), (0, 2), (1, 2),       # 三角形 1
        (2, 3), (2, 4), (3, 4),       # 三角形 2
        (4, 5), (4, 6), (5, 6),       # 三角形 3
        (6, 7), (6, 8), (7, 8),       # 三角形 4
        (3, 5),                        # 连接三角形2和3
        (1, 9), (9, 10), (9, 11), (10, 11),  # 分支
    ])
    return G


# =============================================================================
# 测试用例
# =============================================================================

def test_1_edge_additions():
    """测试1：逐步加边 - 观察 region 合并和扩展"""
    G = build_test_graph()
    ps = PartitionStrategies(G)
    initial = ps.threshold_based(TAU)
    
    operations = [
        {"type": "add", "edge": (0, 3)},
        {"type": "add", "edge": (1, 4)},
        {"type": "add", "edge": (5, 7)},
        {"type": "add", "edge": (8, 10)},
        {"type": "add", "edge": (2, 6)},
        {"type": "add", "edge": (0, 9)},
        {"type": "add", "edge": (3, 7)},
    ]
    
    return run_dynamic_test(G, initial, tau=TAU, operations=operations,
                            test_name=f"Test 1: Edge Additions (τ={TAU})",
                            output_file="output/dynamic_test1_additions.png")


def test_2_edge_removals():
    """测试2：逐步删边 - 观察 region 缩小"""
    G = build_test_graph()
    ps = PartitionStrategies(G)
    initial = ps.threshold_based(TAU)
    
    operations = [
        {"type": "remove", "edge": (3, 5)},
        {"type": "remove", "edge": (2, 4)},
        {"type": "remove", "edge": (4, 6)},
        {"type": "remove", "edge": (1, 9)},
        {"type": "remove", "edge": (6, 7)},
        {"type": "remove", "edge": (0, 2)},
        {"type": "remove", "edge": (9, 10)},
    ]
    
    return run_dynamic_test(G, initial, tau=TAU, operations=operations,
                            test_name=f"Test 2: Edge Removals (τ={TAU})",
                            output_file="output/dynamic_test2_removals.png")


def test_3_mixed_operations():
    """测试3：混合操作 - 交替加边和删边"""
    G = build_test_graph()
    ps = PartitionStrategies(G)
    initial = ps.threshold_based(TAU)
    
    operations = [
        {"type": "add", "edge": (0, 3)},
        {"type": "remove", "edge": (3, 5)},
        {"type": "add", "edge": (5, 8)},
        {"type": "remove", "edge": (4, 5)},
        {"type": "add", "edge": (7, 10)},
        {"type": "remove", "edge": (1, 2)},
        {"type": "add", "edge": (0, 4)},
    ]
    
    return run_dynamic_test(G, initial, tau=TAU, operations=operations,
                            test_name=f"Test 3: Mixed Operations (τ={TAU})",
                            output_file="output/dynamic_test3_mixed.png")


def test_4_batch_vs_sequential():
    """测试4：批量更新 vs 逐步更新对比，绘制 sequential 每一步"""
    G = build_test_graph()
    ps = PartitionStrategies(G)
    initial = ps.threshold_based(TAU)
    
    changes = [
        ('+', 0, 3), ('+', 5, 8), ('-', 3, 5),
        ('-', 4, 6), ('+', 1, 4), ('+', 7, 10)
    ]
    
    # 计算固定布局
    all_nodes = set(G.nodes())
    for op, u, v in changes:
        all_nodes.add(u)
        all_nodes.add(v)
    layout_graph = G.copy()
    for n in all_nodes:
        if not layout_graph.has_node(n):
            layout_graph.add_node(n)
    pos = nx.kamada_kawai_layout(layout_graph)
    
    # --- Sequential: 逐步执行并记录每步状态 ---
    manager_seq = DynamicRegionManager(G.copy(), initial, tau=TAU)
    seq_states = []
    seq_states.append({
        'graph': manager_seq.graph.copy(),
        'regions': deepcopy(manager_seq.regions),
        'title': 'Initial',
        'highlight': [],
        'color': '#000000'
    })
    
    for op, u, v in changes:
        if op == '+':
            manager_seq.edge_addition(u, v)
            highlight = [(u, v)]
            color = '#00CC00'
            desc = f"+({u},{v})"
        else:
            manager_seq.edge_removal(u, v)
            highlight = []
            color = '#FF3333'
            desc = f"-({u},{v})"
        
        seq_states.append({
            'graph': manager_seq.graph.copy(),
            'regions': deepcopy(manager_seq.regions),
            'title': desc,
            'highlight': highlight,
            'color': color
        })
    
    # --- Batch: 一步完成 ---
    manager_batch = DynamicRegionManager(G.copy(), initial, tau=TAU)
    manager_batch.batch_update(changes)
    
    # --- 绘图：上面一行是 sequential 每步，下面一行是 initial + batch ---
    n_seq = len(seq_states)
    cols = n_seq
    fig, axes = plt.subplots(2, cols, figsize=(5 * cols, 10))
    
    # 上行：sequential 每步
    for i, state in enumerate(seq_states):
        ax = axes[0, i]
        draw_partition_state(ax, state['graph'], state['regions'],
                             title=f"Seq Step {i}: {state['title']}", pos=pos,
                             highlight_edges=state['highlight'],
                             highlight_edge_color=state['color'])
    
    # 下行：左边 initial，右边 batch，中间留空
    # initial
    draw_partition_state(axes[1, 0], G,
                         {i: set(r) for i, r in enumerate(initial)},
                         title="Initial", pos=pos)
    
    # batch 结果放在最后一列
    draw_partition_state(axes[1, cols - 1], manager_batch.graph, manager_batch.regions,
                         title=f"Batch ({len(changes)} ops)", pos=pos)
    
    val_b = manager_batch.validate_constraints()
    ok_b = val_b["coverage_satisfied"] and val_b["completeness_satisfied"]
    axes[1, cols - 1].text(
        0.5, -0.02,
        f"Constraints: {'OK' if ok_b else 'VIOLATED'} | Regions: {val_b['num_regions']}",
        transform=axes[1, cols - 1].transAxes, ha='center', fontsize=9,
        color='green' if ok_b else 'red', fontweight='bold')
    
    # 隐藏下行中间的空 axes
    for i in range(1, cols - 1):
        axes[1, i].axis('off')
    
    fig.suptitle(f"Test 4: Batch vs Sequential (τ={TAU})", fontsize=14, fontweight='bold')
    plt.tight_layout()
    os.makedirs("output", exist_ok=True)
    plt.savefig("output/dynamic_test4_batch_vs_seq.png", bbox_inches='tight', dpi=150)
    plt.close()
    
    # 打印对比信息
    val_s = manager_seq.validate_constraints()
    print(f"\n{'='*60}")
    print(f"  Test 4: Batch vs Sequential (τ={TAU})")
    print(f"{'='*60}")
    print(f"  Changes: {changes}")
    print(f"  Sequential ({len(manager_seq.regions)} regions):")
    for rid in sorted(manager_seq.regions.keys()):
        print(f"    R{rid}: {sorted(manager_seq.regions[rid])}")
    ok_s = val_s['coverage_satisfied'] and val_s['completeness_satisfied']
    print(f"    Constraints: {'OK' if ok_s else 'VIOLATED'}")
    print(f"  Batch ({len(manager_batch.regions)} regions):")
    for rid in sorted(manager_batch.regions.keys()):
        print(f"    R{rid}: {sorted(manager_batch.regions[rid])}")
    print(f"    Constraints: {'OK' if ok_b else 'VIOLATED'}")
    print(f"  Saved: output/dynamic_test4_batch_vs_seq.png")


def test_5_node_failure_recovery():
    """测试5：节点故障与恢复 - 用 batch 处理多边同时变化"""
    G = build_test_graph()
    ps = PartitionStrategies(G)
    initial = ps.threshold_based(TAU)
    
    # 节点 4 是关键枢纽（连接三角形2和3），模拟其故障和恢复
    node_to_fail = 4
    edges_of_node = list(G.edges(node_to_fail))
    
    # 故障：batch 删除所有边（一次性）
    failure_changes = [('-', u, v) for u, v in edges_of_node]
    # 恢复：batch 恢复所有边（一次性）
    recovery_changes = [('+', u, v) for u, v in edges_of_node]
    
    operations = [
        {"type": "batch", "changes": failure_changes},
        {"type": "batch", "changes": recovery_changes},
    ]
    
    return run_dynamic_test(G, initial, tau=TAU, operations=operations,
                            test_name=f"Test 5: Node {node_to_fail} Batch Failure & Recovery (τ={TAU})",
                            output_file="output/dynamic_test5_node_failure_batch.png")


def test_7_node_join_leave():
    """测试7：节点加入和离开 - batch 处理新节点上线/下线"""
    G = build_test_graph()
    ps = PartitionStrategies(G)
    initial = ps.threshold_based(TAU)
    
    # 模拟新节点 12 上线，与节点 3, 4, 5 建立连接
    new_node = 12
    new_edges = [('+', new_node, 3), ('+', new_node, 4), ('+', new_node, 5)]
    
    # 模拟节点 9 下线（删除所有关联边）
    node_leave = 9
    leave_edges = [('-', u, v) for u, v in G.edges(node_leave)]
    
    # 模拟节点 9 重新上线
    rejoin_edges = [('+', u, v) for u, v in G.edges(node_leave)]
    
    operations = [
        {"type": "batch", "changes": new_edges},
        {"type": "batch", "changes": leave_edges},
        {"type": "batch", "changes": rejoin_edges},
    ]
    
    return run_dynamic_test(G, initial, tau=TAU, operations=operations,
                            test_name=f"Test 7: Node Join/Leave via Batch (τ={TAU})",
                            output_file="output/dynamic_test7_node_join_leave.png")


def test_8_multi_node_failure():
    """测试8：多节点同时故障 - 模拟区域性基站中断"""
    G = build_test_graph()
    ps = PartitionStrategies(G)
    initial = ps.threshold_based(TAU)
    
    # 模拟节点 4 和 5 同时故障（如同一区域的两个基站同时断电）
    nodes_to_fail = [4, 5]
    failure_changes = []
    all_failed_edges = []
    for node in nodes_to_fail:
        for u, v in G.edges(node):
            edge_tuple = ('-', u, v)
            if edge_tuple not in failure_changes:
                failure_changes.append(edge_tuple)
                all_failed_edges.append((u, v))
    
    # 恢复
    recovery_changes = [('+', u, v) for u, v in all_failed_edges]
    
    operations = [
        {"type": "batch", "changes": failure_changes},
        {"type": "batch", "changes": recovery_changes},
    ]
    
    return run_dynamic_test(G, initial, tau=TAU, operations=operations,
                            test_name=f"Test 8: Multi-Node Failure (nodes {nodes_to_fail}, τ={TAU})",
                            output_file="output/dynamic_test8_multi_failure.png")


def test_9_non_adjacent_failure():
    """测试9：非相邻节点同时失效 - 产生非连通图"""
    G = build_test_graph()
    ps = PartitionStrategies(G)
    initial = ps.threshold_based(TAU)
    
    # 节点 2 和 7 无公共边、无公共邻居，同时失效
    nodes_to_fail = [2, 7]
    failure_changes = []
    all_failed_edges = []
    for node in nodes_to_fail:
        for u, v in G.edges(node):
            edge_tuple = ('-', u, v)
            if edge_tuple not in failure_changes:
                failure_changes.append(edge_tuple)
                all_failed_edges.append((u, v))
    
    recovery_changes = [('+', u, v) for u, v in all_failed_edges]
    
    operations = [
        {"type": "batch", "changes": failure_changes},
        {"type": "batch", "changes": recovery_changes},
    ]
    
    return run_dynamic_test(G, initial, tau=TAU, operations=operations,
                            test_name=f"Test 9: Non-Adjacent Failure (nodes {nodes_to_fail}, τ={TAU})",
                            output_file="output/dynamic_test9_non_adjacent_failure.png")


def test_6_region_id_stability():
    """测试6：验证 region ID 在多步操作后的稳定性"""
    G = build_test_graph()
    ps = PartitionStrategies(G)
    initial = ps.threshold_based(TAU)
    
    manager = DynamicRegionManager(G, initial, tau=TAU)
    
    print(f"\n{'='*60}")
    print(f"  Test 6: Region ID Stability (τ={TAU})")
    print(f"{'='*60}")
    print(f"  Initial IDs: {manager.region_ids}")
    for rid in sorted(manager.regions.keys()):
        print(f"    R{rid}: {sorted(manager.regions[rid])}")
    
    ops = [
        ('+', 0, 3), ('+', 5, 8), ('-', 3, 5),
        ('+', 1, 4), ('-', 6, 7), ('+', 7, 10),
        ('-', 2, 3), ('+', 0, 6),
    ]
    
    print(f"\n  Operations:")
    for op, u, v in ops:
        if op == '+':
            manager.edge_addition(u, v)
        else:
            manager.edge_removal(u, v)
        val = manager.validate_constraints()
        ok = val["coverage_satisfied"] and val["completeness_satisfied"]
        print(f"    {op}({u},{v}) → IDs={manager.region_ids} | "
              f"#R={len(manager.regions)} | OK={ok}")
    
    print(f"\n  Final regions:")
    for rid in sorted(manager.regions.keys()):
        print(f"    R{rid}: {sorted(manager.regions[rid])}")


def test_10_tau2_merge_behavior():
    """测试10：τ=2 时加边导致新极大团吞并旧 region 的行为
    
    图结构：0-1, 0-2, 1-2, 1-3, 2-3（即 {0,1,2} 和 {1,2,3} 两个三角形）
    加上 2-4 边，使 R5={2,4} 存在
    
    初始分区（τ=2 的阈值分区）：多个大小为2的 region
    加边 (0,3) 后：{0,1,2,3} 成为极大团（如果全连通），观察合并行为
    """
    # 构建图：两个三角形共享边 (1,2)，加上 2-4
    G = nx.Graph()
    G.add_edges_from([
        (0, 1), (0, 2), (1, 2),  # 三角形 {0,1,2}
        (1, 3), (2, 3),          # 使 {1,2,3} 也是三角形
        (2, 4),                  # 额外边
    ])
    
    tau = 2
    ps = PartitionStrategies(G)
    initial = ps.threshold_based(tau)
    
    print(f"\n{'='*60}")
    print(f"  Test 10: τ=2 Merge Behavior")
    print(f"{'='*60}")
    print(f"  Graph edges: {sorted(G.edges())}")
    print(f"  Initial regions (τ={tau}):")
    for i, r in enumerate(initial):
        print(f"    R{i}: {sorted(r)}")
    
    # 加边 (0,3)，使 {0,1,2,3} 全连通（如果 0-3 加上后）
    operations = [
        {"type": "add", "edge": (0, 3)},
    ]
    
    manager = run_dynamic_test(G, initial, tau=tau, operations=operations,
                               test_name=f"Test 10: τ=2 Add Edge (0,3)",
                               output_file="output/dynamic_test10_tau2_merge.png")
    
    # 验证：节点2应该同时属于包含{0,1,2,3}的region和包含{2,4}的region
    print(f"\n  Node membership after adding (0,3):")
    for node in sorted(manager.graph.nodes()):
        rids = [rid for rid, R in manager.regions.items() if node in R]
        print(f"    Node {node}: belongs to R{rids}")


def test_11_order_dependency():
    """测试11：顺序依赖 - 先加A再加B vs 先加B再加A 产生不同结果"""
    G = build_test_graph()
    ps = PartitionStrategies(G)
    initial = ps.threshold_based(TAU)
    
    edge_A = (7, 9)
    edge_B = (8, 10)
    
    print(f"\n{'='*60}")
    print(f"  Test 11: Order Dependency (τ={TAU})")
    print(f"{'='*60}")
    print(f"  Edge A: {edge_A}, Edge B: {edge_B}")
    print(f"  Initial regions:")
    for i, r in enumerate(initial):
        print(f"    R{i}: {sorted(r)}")
    
    # 计算固定布局
    all_nodes = set(G.nodes()) | {7, 8, 9, 10}
    layout_graph = G.copy()
    layout_graph.add_edge(*edge_A)
    layout_graph.add_edge(*edge_B)
    pos = nx.kamada_kawai_layout(layout_graph)
    
    # --- 顺序1: A → B ---
    m1 = DynamicRegionManager(G.copy(), initial, tau=TAU)
    states_ab = [{'regions': deepcopy(m1.regions), 'graph': m1.graph.copy(),
                  'title': 'Initial', 'highlight': [], 'color': '#000'}]
    
    m1.edge_addition(*edge_A)
    states_ab.append({'regions': deepcopy(m1.regions), 'graph': m1.graph.copy(),
                      'title': f'+{edge_A}', 'highlight': [edge_A], 'color': '#00CC00'})
    
    m1.edge_addition(*edge_B)
    states_ab.append({'regions': deepcopy(m1.regions), 'graph': m1.graph.copy(),
                      'title': f'+{edge_B}', 'highlight': [edge_B], 'color': '#00CC00'})
    
    # --- 顺序2: B → A ---
    m2 = DynamicRegionManager(G.copy(), initial, tau=TAU)
    states_ba = [{'regions': deepcopy(m2.regions), 'graph': m2.graph.copy(),
                  'title': 'Initial', 'highlight': [], 'color': '#000'}]
    
    m2.edge_addition(*edge_B)
    states_ba.append({'regions': deepcopy(m2.regions), 'graph': m2.graph.copy(),
                      'title': f'+{edge_B}', 'highlight': [edge_B], 'color': '#00CC00'})
    
    m2.edge_addition(*edge_A)
    states_ba.append({'regions': deepcopy(m2.regions), 'graph': m2.graph.copy(),
                      'title': f'+{edge_A}', 'highlight': [edge_A], 'color': '#00CC00'})
    
    # --- 绘图：上行 A→B，下行 B→A ---
    cols = 3
    fig, axes = plt.subplots(2, cols, figsize=(6 * cols, 12))
    
    for i, state in enumerate(states_ab):
        draw_partition_state(axes[0, i], state['graph'], state['regions'],
                             title=f"A→B Step {i}: {state['title']}", pos=pos,
                             highlight_edges=state['highlight'],
                             highlight_edge_color=state['color'])
    
    for i, state in enumerate(states_ba):
        draw_partition_state(axes[1, i], state['graph'], state['regions'],
                             title=f"B→A Step {i}: {state['title']}", pos=pos,
                             highlight_edges=state['highlight'],
                             highlight_edge_color=state['color'])
    
    fig.suptitle(f"Test 11: Order Dependency — A={edge_A}, B={edge_B} (τ={TAU})",
                 fontsize=14, fontweight='bold')
    plt.tight_layout()
    os.makedirs("output", exist_ok=True)
    plt.savefig("output/dynamic_test11_order_dependency.png", bbox_inches='tight', dpi=150)
    plt.close()
    
    # 打印对比
    same_content = (set(frozenset(r) for r in m1.regions.values()) ==
                    set(frozenset(r) for r in m2.regions.values()))
    same_ids = m1.regions == m2.regions
    
    print(f"\n  Order A→B final:")
    for rid in sorted(m1.regions.keys()):
        print(f"    R{rid}: {sorted(m1.regions[rid])}")
    print(f"\n  Order B→A final:")
    for rid in sorted(m2.regions.keys()):
        print(f"    R{rid}: {sorted(m2.regions[rid])}")
    print(f"\n  Same content? {same_content}")
    print(f"  Same IDs? {same_ids}")
    print(f"  Saved: output/dynamic_test11_order_dependency.png")


def test_12_optimized_correctness():
    """测试12：验证优化版与原版在各种场景下结果一致
    
    覆盖的正确性场景：
    1. 加边后新团吞并旧 region（R ⊆ K 的子集移除）
    2. 加边后 region 扩展使其他 region 成为子集
    3. 删边后节点踢出
    4. 混合操作序列
    5. 大规模随机操作
    """
    print(f"\n{'='*60}")
    print(f"  Test 12: Optimized Version Correctness")
    print(f"{'='*60}")
    
    all_pass = True
    
    # 用于绘图的状态收集
    plot_cases = []
    
    # --- Case 1: 加边导致新团吞并旧 region ---
    print(f"\n  Case 1: New clique subsumes old region")
    G = nx.Graph()
    G.add_edges_from([(0,1), (0,2), (1,2), (1,3), (2,3), (2,4)])
    tau = 2
    ps = PartitionStrategies(G)
    initial = ps.threshold_based(tau)
    
    m_orig = DynamicRegionManager(G.copy(), initial, tau=tau)
    m_opt = DynamicRegionManagerOptimized(G.copy(), initial, tau=tau)
    
    # 记录初始状态
    G_before = m_orig.graph.copy()
    regions_before = deepcopy(m_orig.regions)
    
    m_orig.edge_addition(0, 3)
    m_opt.edge_addition(0, 3)
    
    orig_content = set(frozenset(r) for r in m_orig.regions.values())
    opt_content = set(frozenset(r) for r in m_opt.regions.values())
    match = orig_content == opt_content
    print(f"    Add (0,3): match={match}")
    if not match:
        print(f"      Original: {[sorted(r) for r in m_orig.regions.values()]}")
        print(f"      Optimized: {[sorted(r) for r in m_opt.regions.values()]}")
        all_pass = False
    
    plot_cases.append({
        'title': 'Case 1: Subsume (τ=2, +edge 0-3)',
        'graph_before': G_before, 'regions_before': regions_before,
        'graph_after': m_orig.graph.copy(), 'regions_orig': deepcopy(m_orig.regions),
        'regions_opt': deepcopy(m_opt.regions),
        'highlight': [(0, 3)], 'match': match
    })
    
    # --- Case 2: 扩展后使其他 region 成为子集 ---
    print(f"\n  Case 2: Expansion makes another region a subset")
    G = nx.Graph()
    G.add_edges_from([(0,1), (1,2), (0,2), (2,3), (1,3)])
    tau = 4
    ps = PartitionStrategies(G)
    initial = ps.threshold_based(tau)
    print(f"    Initial: {[sorted(r) for r in initial]}")
    
    m_orig = DynamicRegionManager(G.copy(), initial, tau=tau)
    m_opt = DynamicRegionManagerOptimized(G.copy(), initial, tau=tau)
    
    G_before = m_orig.graph.copy()
    regions_before = deepcopy(m_orig.regions)
    
    m_orig.edge_addition(0, 3)
    m_opt.edge_addition(0, 3)
    
    orig_content = set(frozenset(r) for r in m_orig.regions.values())
    opt_content = set(frozenset(r) for r in m_opt.regions.values())
    match = orig_content == opt_content
    print(f"    Add (0,3): match={match}")
    print(f"      Original: {[sorted(r) for r in m_orig.regions.values()]}")
    print(f"      Optimized: {[sorted(r) for r in m_opt.regions.values()]}")
    if not match:
        all_pass = False
    
    plot_cases.append({
        'title': 'Case 2: Expansion (τ=4, +edge 0-3)',
        'graph_before': G_before, 'regions_before': regions_before,
        'graph_after': m_orig.graph.copy(), 'regions_orig': deepcopy(m_orig.regions),
        'regions_opt': deepcopy(m_opt.regions),
        'highlight': [(0, 3)], 'match': match
    })
    
    # --- Case 3: 删边后节点踢出 ---
    print(f"\n  Case 3: Edge removal kicks nodes out")
    G = build_test_graph()
    tau = TAU
    ps = PartitionStrategies(G)
    initial = ps.threshold_based(tau)
    
    m_orig = DynamicRegionManager(G.copy(), initial, tau=tau)
    m_opt = DynamicRegionManagerOptimized(G.copy(), initial, tau=tau)
    
    G_before = m_orig.graph.copy()
    regions_before = deepcopy(m_orig.regions)
    
    removals = [(2,0), (2,1), (2,3), (2,4), (7,6), (7,8)]
    for u, v in removals:
        m_orig.edge_removal(u, v)
        m_opt.edge_removal(u, v)
    
    orig_content = set(frozenset(r) for r in m_orig.regions.values())
    opt_content = set(frozenset(r) for r in m_opt.regions.values())
    match = orig_content == opt_content
    print(f"    Remove 6 edges: match={match}")
    if not match:
        print(f"      Original: {[sorted(r) for r in m_orig.regions.values()]}")
        print(f"      Optimized: {[sorted(r) for r in m_opt.regions.values()]}")
        all_pass = False
    
    plot_cases.append({
        'title': f'Case 3: Removal (τ={TAU}, -6 edges)',
        'graph_before': G_before, 'regions_before': regions_before,
        'graph_after': m_orig.graph.copy(), 'regions_orig': deepcopy(m_orig.regions),
        'regions_opt': deepcopy(m_opt.regions),
        'highlight': removals, 'match': match
    })
    
    # --- Case 4: 混合操作序列 ---
    print(f"\n  Case 4: Mixed operations sequence")
    G = build_test_graph()
    ps = PartitionStrategies(G)
    initial = ps.threshold_based(TAU)
    
    m_orig = DynamicRegionManager(G.copy(), initial, tau=TAU)
    m_opt = DynamicRegionManagerOptimized(G.copy(), initial, tau=TAU)
    
    G_before = m_orig.graph.copy()
    regions_before = deepcopy(m_orig.regions)
    
    ops = [
        ('+', 0, 3), ('+', 5, 8), ('-', 2, 3), ('+', 7, 9),
        ('-', 6, 7), ('+', 1, 4), ('-', 9, 10), ('+', 3, 8),
    ]
    for op, u, v in ops:
        if op == '+':
            m_orig.edge_addition(u, v)
            m_opt.edge_addition(u, v)
        else:
            m_orig.edge_removal(u, v)
            m_opt.edge_removal(u, v)
    
    orig_content = set(frozenset(r) for r in m_orig.regions.values())
    opt_content = set(frozenset(r) for r in m_opt.regions.values())
    match = orig_content == opt_content
    print(f"    8 mixed ops: match={match}")
    if not match:
        print(f"      Original ({len(m_orig.regions)} regions):")
        for rid in sorted(m_orig.regions.keys()):
            print(f"        R{rid}: {sorted(m_orig.regions[rid])}")
        print(f"      Optimized ({len(m_opt.regions)} regions):")
        for rid in sorted(m_opt.regions.keys()):
            print(f"        R{rid}: {sorted(m_opt.regions[rid])}")
        all_pass = False
    
    highlight_ops = [(u, v) for op, u, v in ops if op == '+']
    plot_cases.append({
        'title': f'Case 4: Mixed (τ={TAU}, 8 ops)',
        'graph_before': G_before, 'regions_before': regions_before,
        'graph_after': m_orig.graph.copy(), 'regions_orig': deepcopy(m_orig.regions),
        'regions_opt': deepcopy(m_opt.regions),
        'highlight': highlight_ops, 'match': match
    })
    
    # --- Case 5: 大规模随机操作 ---
    print(f"\n  Case 5: Large-scale random operations (100 nodes, 50 ops)")
    from src.topology.spatial_generator import generate_ppp_topology
    from src.topology.mec_builder import MECTopologyBuilder
    
    graph, _ = generate_ppp_topology(area_size=2000, lambda_bs=100/(2000**2),
                                      coverage_radius_mean=300, coverage_radius_std=50,
                                      plot_voronoi=False, plot_graph=False,
                                      random_seed=42, return_spatial=True)
    if not nx.is_connected(graph):
        builder = MECTopologyBuilder()
        graph = builder.extract_largest_component(graph, verbose=False)
    
    ps = PartitionStrategies(graph)
    initial = ps.threshold_based(TAU)
    
    rng = np.random.default_rng(123)
    non_edges = list(nx.non_edges(graph))
    edges = list(graph.edges())
    
    m_orig = DynamicRegionManager(graph.copy(), initial, tau=TAU)
    m_opt = DynamicRegionManagerOptimized(graph.copy(), initial, tau=TAU)
    
    num_ops = 50
    mismatch_count = 0
    for i in range(num_ops):
        if rng.random() < 0.6 and non_edges:
            idx = rng.integers(len(non_edges))
            u, v = non_edges[idx]
            m_orig.edge_addition(u, v)
            m_opt.edge_addition(u, v)
            non_edges.pop(idx)
            edges.append((u, v))
        elif edges:
            idx = rng.integers(len(edges))
            u, v = edges[idx]
            m_orig.edge_removal(u, v)
            m_opt.edge_removal(u, v)
            edges.pop(idx)
            non_edges.append((u, v))
        
        orig_content = set(frozenset(r) for r in m_orig.regions.values())
        opt_content = set(frozenset(r) for r in m_opt.regions.values())
        if orig_content != opt_content:
            mismatch_count += 1
            if mismatch_count <= 3:
                print(f"    ✗ Mismatch at op {i+1}")
    
    if mismatch_count == 0:
        print(f"    50 random ops: ALL MATCH ✓")
    else:
        print(f"    50 random ops: {mismatch_count} mismatches ✗")
        all_pass = False
    
    # --- Case 6: 验证约束满足 ---
    print(f"\n  Case 6: Constraint validation after all operations")
    val_orig = m_orig.validate_constraints()
    val_opt = m_opt.validate_constraints()
    print(f"    Original:  coverage={val_orig['coverage_satisfied']}, completeness={val_orig['completeness_satisfied']}")
    print(f"    Optimized: coverage={val_opt['coverage_satisfied']}, completeness={val_opt['completeness_satisfied']}")
    if not val_opt['coverage_satisfied'] or not val_opt['completeness_satisfied']:
        all_pass = False
    
    # --- 绘图 ---
    _plot_test12(plot_cases)
    
    # --- 总结 ---
    print(f"\n  {'='*40}")
    if all_pass:
        print(f"  ALL CASES PASSED ✓")
    else:
        print(f"  SOME CASES FAILED ✗")
    print(f"  {'='*40}")


def _plot_test12(plot_cases):
    """绘制 test_12 的对比图：每个 case 三列（Before / Original After / Optimized After）"""
    num_cases = len(plot_cases)
    fig, axes = plt.subplots(num_cases, 3, figsize=(18, 5 * num_cases))
    if num_cases == 1:
        axes = [axes]
    
    for row, case in enumerate(plot_cases):
        # 计算统一布局
        all_nodes = set(case['graph_after'].nodes()) | set(case['graph_before'].nodes())
        layout_graph = nx.Graph()
        layout_graph.add_nodes_from(all_nodes)
        layout_graph.add_edges_from(case['graph_before'].edges())
        layout_graph.add_edges_from(case['graph_after'].edges())
        pos = nx.kamada_kawai_layout(layout_graph)
        
        match_str = "✓ MATCH" if case['match'] else "✗ MISMATCH"
        
        # Before
        draw_partition_state(axes[row][0], case['graph_before'], case['regions_before'],
                             title=f"Before", pos=pos)
        
        # Original After
        draw_partition_state(axes[row][1], case['graph_after'], case['regions_orig'],
                             title=f"Original After", pos=pos,
                             highlight_edges=case['highlight'],
                             highlight_edge_color='#00CC00')
        
        # Optimized After
        draw_partition_state(axes[row][2], case['graph_after'], case['regions_opt'],
                             title=f"Optimized After ({match_str})", pos=pos,
                             highlight_edges=case['highlight'],
                             highlight_edge_color='#00CC00')
        
        # Row label
        axes[row][0].set_ylabel(case['title'], fontsize=11, fontweight='bold')
    
    fig.suptitle("Test 12: Original vs Optimized Correctness", fontsize=14, fontweight='bold')
    plt.tight_layout()
    os.makedirs("output", exist_ok=True)
    plt.savefig("output/dynamic_test12_optimized_correctness.png", bbox_inches='tight', dpi=150)
    plt.close()
    print(f"\n  Saved: output/dynamic_test12_optimized_correctness.png")


if __name__ == "__main__":
    os.makedirs("output", exist_ok=True)
    
    test_6_region_id_stability()
    test_1_edge_additions()
    test_2_edge_removals()
    test_3_mixed_operations()
    test_4_batch_vs_sequential()
    test_5_node_failure_recovery()
    test_7_node_join_leave()
    test_8_multi_node_failure()
    test_9_non_adjacent_failure()
    test_10_tau2_merge_behavior()
    test_11_order_dependency()
    test_12_optimized_correctness()
    
    print(f"\n{'='*60}")
    print(f"  All tests completed!")
    print(f"{'='*60}")

"""
区域划分策略
实现多种基于图的区域划分算法
"""

import networkx as nx
import numpy as np
from typing import List, Set, Dict, Tuple


class PartitionStrategies:
    """区域划分策略集合"""
    
    def __init__(self, graph: nx.Graph):
        """
        初始化策略
        
        Parameters:
        -----------
        graph : nx.Graph
            MEC拓扑图
        """
        self.graph = graph
        self.num_nodes = graph.number_of_nodes()
    
    def maximal_clique(self) -> List[Set[int]]:
        """
        极大团策略
        
        使用Bron-Kerbosch算法找出所有极大团
        每个极大团作为一个分区
        分区按最小节点ID排序，确保编号稳定性
        
        Returns:
        --------
        List[Set[int]] : 分区列表，每个分区是一个节点集合（按最小节点ID排序）
        """
        cliques = list(nx.find_cliques(self.graph))
        partitions = [set(c) for c in cliques]
        # 按最小节点ID排序，确保分区编号稳定
        return sorted(partitions, key=lambda x: (min(x), -len(x)))
    
    def threshold_based(self, merge_threshold: int = 3) -> List[Set[int]]:
        """
        阈值合并策略
        
        在极大团基础上，通过合并小分区来避免节点属于过多分区
        
        Parameters:
        -----------
        merge_threshold : int
            合并阈值，大小 <= threshold 的分区会尝试合并
        
        Returns:
        --------
        List[Set[int]] : 分区列表
        """
        # 首先获取所有极大团
        cliques = list(nx.find_cliques(self.graph))
        partitions = [set(c) for c in cliques]
        
        # 调用合并函数
        merged_partitions = self._merge_small_partitions(partitions, merge_threshold)
        
        return merged_partitions
    
    def _merge_small_partitions(self, partitions: List[Set[int]], 
                                merge_threshold: int) -> List[Set[int]]:
        """
        合并小分区（阈值限制）
        
        合并策略保证分区编号稳定性：
        - 分区统一按 (min(x), -len(x)) 排序，确保编号一致性
        - 大分区优先排序，编号更稳定（不会因为合并而改变）
        - 小分区按最小节点ID合并，保持节点所属分区编号的稳定性
        """
        # 统一使用 (min(x), -len(x)) 排序，确保分区编号的稳定性
        # 大分区优先，编号靠前，不会因为小分区合并而改变
        sorted_partitions = sorted(partitions, key=lambda x: (min(x), -len(x)))
        
        # 分离大分区和小分区
        large_partitions = [p for p in sorted_partitions if len(p) > merge_threshold]
        small_partitions = [p for p in sorted_partitions if len(p) <= merge_threshold]
        
        # 合并小分区：按顺序处理，优先保留包含更小节点ID的分区
        merged_small = []
        while small_partitions:
            first = small_partitions.pop(0)  # 取出第一个（包含最小节点ID的）
            merged = first.copy()
            changed = True
            while changed:
                changed = False
                for other in small_partitions[:]:
                    temp_merge = merged | other
                    # 只有当有重叠且合并后不超过阈值时才合并
                    if (merged & other) and len(temp_merge) <= merge_threshold:
                        merged = temp_merge
                        small_partitions.remove(other)
                        changed = True
            merged_small.append(merged)
        
        # 最终排序：统一使用 (min(x), -len(x))，确保编号稳定
        # 大分区在前，小分区在后，相同最小节点ID时大分区优先
        return sorted(large_partitions + merged_small, key=lambda x: (min(x), -len(x)))
    
    def neighbor_based(self) -> List[Set[int]]:
        """
        基于邻居节点的策略
        
        每个节点及其所有邻居组成一个分区。
        如果某个分区是另一个分区的子集，则会被合并到更大的分区中，避免重叠。
        分区按最小节点ID排序，确保编号稳定性。
        
        Returns:
        --------
        List[Set[int]] : 分区列表（按最小节点ID排序）
        """
        partitions = []
        
        # 按节点ID顺序遍历，确保结果稳定
        for node in sorted(self.graph.nodes()):
            # 节点自身加上所有邻居
            part = set([node]) | set(self.graph.neighbors(node))
            # 检查是否已存在相同的分区
            if part not in partitions:
                partitions.append(part)
        
        # 移除那些是其他分区子集的分区（保留更大的分区）
        filtered_partitions = []
        for i, part1 in enumerate(partitions):
            is_subset = False
            for j, part2 in enumerate(partitions):
                # 如果part1是part2的真子集（不相等且是子集）
                if i != j and part1 < part2:
                    is_subset = True
                    break
            
            # 只保留不是其他分区子集的分区
            if not is_subset:
                filtered_partitions.append(part1)
        
        # 按最小节点ID排序，确保分区编号稳定
        return sorted(filtered_partitions, key=lambda x: (min(x), -len(x)))
    
    def global_partition(self) -> List[Set[int]]:
        """
        全局分区策略
        
        所有节点组成单一分区（基准策略）
        
        Returns:
        --------
        List[Set[int]] : 只包含一个分区的列表
        """
        return [set(self.graph.nodes())]
    
    def greedy_edge_coverage(self, tau: int = 4) -> List[Set[int]]:
        """
        边覆盖贪心策略 (Baseline)
        
        将分区问题建模为 Set Cover：每个候选 region 覆盖若干边，
        每步贪心选择覆盖最多未覆盖边的候选，直到所有边被覆盖。
        
        候选集 = 所有大小 ≤ τ 的极大团。
        
        Parameters:
        -----------
        tau : int
            region 大小上限
            
        Returns:
        --------
        List[Set[int]] : 分区列表
        """
        # 生成候选集：所有极大团
        all_cliques = [set(c) for c in nx.find_cliques(self.graph)]
        
        # ≤ τ 的直接作为候选
        candidates = [c for c in all_cliques if len(c) <= tau]
        
        # > τ 的极大团：生成所有大小为 τ 的子集作为候选
        from itertools import combinations
        for c in all_cliques:
            if len(c) > tau:
                for sub in combinations(sorted(c), tau):
                    sub_set = set(sub)
                    # 只保留内部是团的子集
                    if all(self.graph.has_edge(u, v)
                           for u in sub_set for v in sub_set if u < v):
                        candidates.append(sub_set)
        
        # 去重
        candidates = [set(c) for c in set(frozenset(c) for c in candidates)]
        # 按大小降序排列（优先用大团覆盖）
        candidates.sort(key=lambda c: -len(c))
        
        # 预计算每个候选覆盖的边集
        cand_edges = []
        for c in candidates:
            edges = frozenset(
                (min(u, v), max(u, v)) for u in c for v in c
                if u < v and self.graph.has_edge(u, v))
            cand_edges.append((c, edges))
        
        uncovered = set(
            (min(u, v), max(u, v)) for u, v in self.graph.edges())
        
        regions = []
        
        while uncovered:
            best_cand = None
            best_count = 0
            best_covered = set()
            
            for c, c_edges in cand_edges:
                count = len(c_edges & uncovered)
                # 同样多边时优先选大 region（减少碎片）
                if count > best_count or (
                    count == best_count and best_cand is not None
                    and len(c) > len(best_cand)):
                    best_count = count
                    best_cand = c
                    best_covered = c_edges & uncovered
            
            if best_cand is None or best_count == 0:
                e = uncovered.pop()
                regions.append(set(e))
            else:
                regions.append(best_cand)
                uncovered -= best_covered
        
        # 移除子集 region
        filtered = []
        for i, r1 in enumerate(regions):
            is_subset = any(
                r1 < r2 for j, r2 in enumerate(regions) if i != j)
            if not is_subset:
                filtered.append(r1)
        
        return sorted(filtered, key=lambda x: (min(x), -len(x)))
    
    def greedy_node_centric(self, tau: int = 4) -> List[Set[int]]:
        """
        节点中心贪心策略 (Baseline)
        
        不依赖极大团枚举，不要求 region 内部全连通（团）。
        以覆盖所有边为目标，每步选择未覆盖边最多的节点为种子，
        贪心扩展为大小 ≤ τ 的连通子图。
        
        加入条件：候选节点只需与 R 中至少一个节点相邻即可（连通）。
        优先加入能覆盖更多未覆盖边的候选。
        
        复杂度: O(m * tau) — 多项式时间，无指数爆炸。
        
        Parameters:
        -----------
        tau : int
            region 大小上限
            
        Returns:
        --------
        List[Set[int]] : 分区列表
        """
        import heapq
        
        uncovered = set(
            (min(u, v), max(u, v)) for u, v in self.graph.edges())
        
        # 每个节点的未覆盖边集合
        node_uncov_edges = {n: set() for n in self.graph.nodes()}
        for e in uncovered:
            node_uncov_edges[e[0]].add(e)
            node_uncov_edges[e[1]].add(e)
        
        # 最大堆（用负数模拟）
        heap = [(-len(node_uncov_edges[n]), n) for n in self.graph.nodes()]
        heapq.heapify(heap)
        
        regions = []
        
        while uncovered:
            # 弹出未覆盖边最多的节点（跳过已过时的）
            while heap:
                neg_count, v = heapq.heappop(heap)
                actual = len(node_uncov_edges[v])
                if actual > 0:
                    if actual != -neg_count:
                        heapq.heappush(heap, (-actual, v))
                        continue
                    break
            else:
                break
            
            # 从 v 出发贪心扩展连通子图
            R = {v}
            R_neighbors = set(self.graph.neighbors(v))
            
            while len(R) < tau:
                # 候选：与 R 相邻且有未覆盖边的节点
                best_w = None
                best_gain = 0
                for w in R_neighbors:
                    if w in R:
                        continue
                    # gain = w 加入后能新增覆盖的边数
                    gain = 0
                    for u in R:
                        e = (min(w, u), max(w, u))
                        if e in uncovered:
                            gain += 1
                    # 也考虑 w 自身的未覆盖边（鼓励高度节点）
                    if gain > best_gain:
                        best_gain = gain
                        best_w = w
                
                if best_w is None or best_gain == 0:
                    break
                
                R.add(best_w)
                # 更新 R 的邻居集
                for nb in self.graph.neighbors(best_w):
                    R_neighbors.add(nb)
            
            # 计算本轮覆盖的边（R 内实际存在的未覆盖边）
            covered_now = []
            r_list = sorted(R)
            for i in range(len(r_list)):
                for j in range(i + 1, len(r_list)):
                    e = (r_list[i], r_list[j])
                    if e in uncovered:
                        covered_now.append(e)
            
            # 保底：至少覆盖 v 的一条未覆盖边
            if not covered_now:
                e = next(iter(node_uncov_edges[v]))
                other = e[1] if e[0] == v else e[0]
                R = {v, other}
                covered_now = [e]
            
            for e in covered_now:
                uncovered.discard(e)
                node_uncov_edges[e[0]].discard(e)
                node_uncov_edges[e[1]].discard(e)
            
            # 将受影响节点重新入堆
            affected = set()
            for e in covered_now:
                affected.add(e[0])
                affected.add(e[1])
            for n in affected:
                if node_uncov_edges[n]:
                    heapq.heappush(heap, (-len(node_uncov_edges[n]), n))
            
            regions.append(R)
        
        # 移除子集 region
        filtered = []
        for i, r1 in enumerate(regions):
            is_subset = any(
                r1 < r2 for j, r2 in enumerate(regions) if i != j)
            if not is_subset:
                filtered.append(r1)
        
        return sorted(filtered, key=lambda x: (min(x), -len(x)))
    
    def simulated_annealing(self, tau: int = 4, T0: float = 5.0,
                            alpha: float = 0.995, max_iter: int = 5000,
                            seed: int = 42) -> List[Set[int]]:
        """
        模拟退火分区策略 (Baseline, optimized)

        以最小化节点重叠度为目标，region 内部只要求连通子图，大小 <= tau。
        初始解：对每条边随机扩展为连通子图，不依赖其他算法。

        优化点：
        - node_to_regions 倒排索引，NodeTransfer 候选搜索与 region 总数解耦
        - 增量维护 cost，去掉每温度层的全量求和
        - 初始子集移除用倒排索引，避免 O(N_R^2) 两两比较
        """
        import random
        from collections import defaultdict
        rng = random.Random(seed)

        graph = self.graph
        all_edges = set((min(u, v), max(u, v)) for u, v in graph.edges())

        # === 初始解：每条边随机扩展 ===
        raw_regions = []
        for u, v in all_edges:
            R = {u, v}
            cands = list((set(graph.neighbors(u)) | set(graph.neighbors(v))) - R)
            rng.shuffle(cands)
            for w in cands:
                if len(R) >= tau:
                    break
                R.add(w)
            raw_regions.append(R)

        # === 去重 + 子集移除（倒排索引加速）===
        unique = list(set(frozenset(r) for r in raw_regions))
        unique.sort(key=lambda x: -len(x))
        node_to_kept = defaultdict(set)
        regions = []
        for r in unique:
            r_nodes = list(r)
            cand = set(node_to_kept[r_nodes[0]])
            for n in r_nodes[1:]:
                cand &= node_to_kept[n]
                if not cand:
                    break
            if any(r < regions[idx] for idx in cand):
                continue
            new_idx = len(regions)
            regions.append(set(r))
            for n in r:
                node_to_kept[n].add(new_idx)

        # === 倒排索引 + 边覆盖计数 ===
        node_to_regions = defaultdict(set)
        for idx, R in enumerate(regions):
            for n in R:
                node_to_regions[n].add(idx)

        edge_cov = {e: 0 for e in all_edges}
        for R in regions:
            for e in self._sa_edges_in_region(R, all_edges):
                edge_cov[e] += 1

        state = {
            'regions': regions,
            'node_to_regions': node_to_regions,
            'edge_cov': edge_cov,
            'cost': sum(len(R) for R in regions),
        }

        best_cost = state['cost']
        best_regions = [R.copy() for R in regions]

        T = T0
        for _ in range(max_iter):
            if T < 0.01:
                break
            for _ in range(50):
                if not state['regions']:
                    break
                move_type = rng.random()
                if move_type < 0.45:
                    self._sa_node_remove(state, all_edges, graph, rng, T)
                elif move_type < 0.75:
                    self._sa_node_transfer(state, all_edges, graph, rng, T, tau)
                elif move_type < 0.90:
                    self._sa_region_merge(state, all_edges, graph, rng, T, tau)
                else:
                    self._sa_region_split(state, all_edges, graph, rng, T)

            T *= alpha
            if state['cost'] < best_cost:
                best_cost = state['cost']
                best_regions = [R.copy() for R in state['regions']]

        # 最终子集移除 + 排序
        best_regions.sort(key=lambda x: -len(x))
        final = []
        for r in best_regions:
            if not any(r < bigger for bigger in final):
                final.append(r)
        return sorted(final, key=lambda x: (min(x), -len(x)))

    def _sa_is_connected(self, nodes_set):
        if len(nodes_set) <= 1:
            return True
        start = next(iter(nodes_set))
        visited = {start}
        queue = [start]
        while queue:
            cur = queue.pop()
            for nb in self.graph.neighbors(cur):
                if nb in nodes_set and nb not in visited:
                    visited.add(nb)
                    queue.append(nb)
        return len(visited) == len(nodes_set)

    def _sa_edges_in_region(self, R, all_edges):
        result = set()
        r_list = sorted(R)
        for i in range(len(r_list)):
            for j in range(i + 1, len(r_list)):
                e = (r_list[i], r_list[j])
                if e in all_edges:
                    result.add(e)
        return result

    def _sa_node_remove(self, state, all_edges, graph, rng, T):
        regions = state['regions']
        edge_cov = state['edge_cov']
        node_to_regions = state['node_to_regions']
        idx = rng.randrange(len(regions))
        R = regions[idx]
        if len(R) <= 2:
            return
        v = rng.choice(list(R))
        remaining = R - {v}
        if not self._sa_is_connected(remaining):
            return
        lost = []
        for u in R:
            if u != v:
                e = (min(v, u), max(v, u))
                if e in all_edges:
                    lost.append(e)
        for e in lost:
            if edge_cov[e] <= 1:
                return
        regions[idx] = remaining
        node_to_regions[v].discard(idx)
        for e in lost:
            edge_cov[e] -= 1
        state['cost'] -= 1

    def _sa_node_transfer(self, state, all_edges, graph, rng, T, tau):
        regions = state['regions']
        edge_cov = state['edge_cov']
        node_to_regions = state['node_to_regions']
        idx_i = rng.randrange(len(regions))
        Ri = regions[idx_i]
        if len(Ri) <= 2:
            return
        v = rng.choice(list(Ri))
        remaining = Ri - {v}
        if not self._sa_is_connected(remaining):
            return
        # 倒排索引：候选 = v 的邻居所在的 region
        neighbor_regions = set()
        for w in graph.neighbors(v):
            neighbor_regions |= node_to_regions[w]
        candidates_j = [j for j in neighbor_regions
                        if j != idx_i and j not in node_to_regions[v]
                        and len(regions[j]) < tau]
        if not candidates_j:
            return
        idx_j = rng.choice(candidates_j)
        Rj = regions[idx_j]
        lost = []
        for u in Ri:
            if u != v:
                e = (min(v, u), max(v, u))
                if e in all_edges:
                    lost.append(e)
        gained = []
        for u in Rj:
            e = (min(v, u), max(v, u))
            if e in all_edges:
                gained.append(e)
        gained_set = set(gained)
        for e in lost:
            new_cov = edge_cov[e] - 1 + (1 if e in gained_set else 0)
            if new_cov <= 0:
                return
        regions[idx_i] = remaining
        regions[idx_j] = Rj | {v}
        node_to_regions[v].discard(idx_i)
        node_to_regions[v].add(idx_j)
        for e in lost:
            edge_cov[e] -= 1
        for e in gained:
            edge_cov[e] += 1
        # cost 不变（一边 -1 一边 +1）

    def _sa_region_merge(self, state, all_edges, graph, rng, T, tau):
        regions = state['regions']
        edge_cov = state['edge_cov']
        node_to_regions = state['node_to_regions']
        if len(regions) < 2:
            return
        idx_i = rng.randrange(len(regions))
        idx_j = rng.randrange(len(regions))
        if idx_i == idx_j:
            return
        Ri, Rj = regions[idx_i], regions[idx_j]
        merged = Ri | Rj
        if len(merged) > tau or not self._sa_is_connected(merged):
            return
        delta = len(merged) - len(Ri) - len(Rj)
        if not (delta < 0 or rng.random() < np.exp(-delta / T)):
            return
        old_edges_i = self._sa_edges_in_region(Ri, all_edges)
        old_edges_j = self._sa_edges_in_region(Rj, all_edges)
        new_edges = self._sa_edges_in_region(merged, all_edges)
        for e in old_edges_i:
            edge_cov[e] -= 1
        for e in old_edges_j:
            edge_cov[e] -= 1
        for e in new_edges:
            edge_cov[e] += 1
        # 先用原索引清除引用
        for n in Ri:
            node_to_regions[n].discard(idx_i)
        for n in Rj:
            node_to_regions[n].discard(idx_j)
        # 确保 idx_i < idx_j，merged 放到较小位置
        if idx_i > idx_j:
            idx_i, idx_j = idx_j, idx_i
        regions[idx_i] = merged
        for n in merged:
            node_to_regions[n].add(idx_i)
        # swap-delete idx_j
        last = len(regions) - 1
        if idx_j != last:
            moved = regions[last]
            regions[idx_j] = moved
            for n in moved:
                node_to_regions[n].discard(last)
                node_to_regions[n].add(idx_j)
        regions.pop()
        state['cost'] += delta

    def _sa_region_split(self, state, all_edges, graph, rng, T):
        regions = state['regions']
        edge_cov = state['edge_cov']
        node_to_regions = state['node_to_regions']
        idx = rng.randrange(len(regions))
        R = regions[idx]
        if len(R) <= 2:
            return
        v = rng.choice(list(R))
        v_nbs = [u for u in R if u != v and graph.has_edge(v, u)]
        if not v_nbs:
            return
        k = rng.randint(1, min(len(v_nbs), len(R) - 2))
        rng.shuffle(v_nbs)
        part1 = {v} | set(v_nbs[:k])
        part2 = R - part1
        if not part2 or not self._sa_is_connected(part1) or not self._sa_is_connected(part2):
            return
        old_edges = self._sa_edges_in_region(R, all_edges)
        new1 = self._sa_edges_in_region(part1, all_edges)
        new2 = self._sa_edges_in_region(part2, all_edges)
        lost_by_split = old_edges - new1 - new2
        for e in lost_by_split:
            if edge_cov[e] <= 1:
                return
        if not (rng.random() < np.exp(-1.0 / T)):
            return
        for e in old_edges:
            edge_cov[e] -= 1
        for e in new1:
            edge_cov[e] += 1
        for e in new2:
            edge_cov[e] += 1
        regions[idx] = part1
        new_idx = len(regions)
        regions.append(part2)
        for n in part2:
            node_to_regions[n].discard(idx)
            node_to_regions[n].add(new_idx)
        # split 把 R 划分为两个不相交子集，节点总数不变，cost 不变

    def get_partition_statistics(self, partitions: List[Set[int]]) -> Dict:
        """
        计算分区统计信息
        
        Parameters:
        -----------
        partitions : List[Set[int]]
            分区列表
        
        Returns:
        --------
        Dict : 统计信息字典
        """
        if not partitions:
            return {
                'num_partitions': 0,
                'avg_size': 0,
                'max_size': 0,
                'min_size': 0,
                'avg_instances': 0,
                'max_instances': 0,
                'min_instances': 0
            }
        
        # 分区大小统计
        partition_sizes = [len(p) for p in partitions]
        
        # 节点实例数统计（每个节点属于多少个分区）
        node_to_partitions = {node: 0 for node in self.graph.nodes()}
        for partition in partitions:
            for node in partition:
                node_to_partitions[node] += 1
        
        instance_counts = list(node_to_partitions.values())
        
        return {
            'num_partitions': len(partitions),
            'avg_size': np.mean(partition_sizes),
            'max_size': max(partition_sizes),
            'min_size': min(partition_sizes),
            'std_size': np.std(partition_sizes),
            'avg_instances': np.mean(instance_counts),
            'max_instances': max(instance_counts),
            'min_instances': min(instance_counts),
            'std_instances': np.std(instance_counts)
        }
    
    def evaluate_partitions(self, partitions: List[Set[int]], 
                           alpha: float = 0.4, beta: float = 0.6) -> float:
        """
        评估分区质量
        
        综合考虑节点实例数和分区大小
        分数越小越好
        
        Parameters:
        -----------
        partitions : List[Set[int]]
            分区列表
        alpha : float
            实例数权重
        beta : float
            分区大小权重
        
        Returns:
        --------
        float : 评估分数（越小越好）
        """
        stats = self.get_partition_statistics(partitions)
        
        # 归一化
        norm_instances = stats['avg_instances'] / self.num_nodes
        norm_size = stats['avg_size'] / self.num_nodes
        
        # 综合评分
        score = alpha * norm_instances + beta * norm_size
        
        return score


if __name__ == '__main__':
    G = nx.Graph()
    G.add_edges_from([(0, 1), (0, 2), (1, 2), (2, 3), (2, 4), (3, 4)])
    strategies = PartitionStrategies(G)
    print(f"极大团: {len(strategies.maximal_clique())} 个分区")
    print(f"阈值合并(τ=3): {len(strategies.threshold_based(3))} 个分区")


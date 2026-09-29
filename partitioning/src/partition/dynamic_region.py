"""
动态区域管理器
实现论文 Algorithm 3 (边添加增量更新) 和 Algorithm 4 (边删除增量更新)
支持批量拓扑变化处理，维护分区编号稳定性
"""

import networkx as nx
import numpy as np
from typing import List, Set, Dict, Tuple, Optional
from copy import deepcopy


class DynamicRegionManager:
    """
    动态区域管理器
    
    维护图拓扑和 region 集合的状态，支持增量更新。
    每个 region 有稳定的编号（ID），在增量更新过程中尽量保持不变。
    """
    
    def __init__(self, graph: nx.Graph, regions: List[Set[int]], tau: int):
        """
        Parameters
        ----------
        graph : nx.Graph
            当前空间覆盖图
        regions : List[Set[int]]
            初始 region 集合（由静态分区算法生成）
        tau : int
            region 大小上限
        """
        self.graph = graph.copy()
        self.tau = tau
        self._next_region_id = 0
        
        # 用 dict 维护 region: {region_id: set_of_nodes}
        self.regions: Dict[int, Set[int]] = {}
        for r in regions:
            self.regions[self._next_region_id] = set(r)
            self._next_region_id += 1
        
        # 变化历史记录
        self.history: List[Dict] = []
    
    @property
    def region_list(self) -> List[Set[int]]:
        """返回按 ID 排序的 region 列表"""
        return [self.regions[rid] for rid in sorted(self.regions.keys())]
    
    @property
    def region_ids(self) -> List[int]:
        """返回排序后的 region ID 列表"""
        return sorted(self.regions.keys())
    
    def get_region_by_id(self, region_id: int) -> Optional[Set[int]]:
        """根据 ID 获取 region"""
        return self.regions.get(region_id)
    
    # =========================================================================
    # Algorithm 3: Incremental Region Update for Edge Addition
    # =========================================================================
    
    def edge_addition(self, mi: int, mj: int) -> Dict[int, Set[int]]:
        """
        论文 Algorithm 3: 边添加的增量 region 更新
        
        Parameters
        ----------
        mi, mj : int
            新增边的两个端点
            
        Returns
        -------
        Dict[int, Set[int]]
            更新后的 region 集合 {id: nodes}
        """
        import time as _time
        _t0 = _time.perf_counter()
        
        snapshot_before = deepcopy(self.regions)
        
        if self.graph.has_edge(mi, mj):
            self._record_history("edge_addition", (mi, mj), snapshot_before, 
                                 self.regions, "Edge already exists, no change")
            return self.regions
        
        # Step 1: 添加边到图
        self.graph.add_edge(mi, mj)
        _t1 = _time.perf_counter()
        
        # Step 2: 计算受影响节点集 C = {mi, mj} ∪ (N(mi) ∩ N(mj))
        neighbors_i = set(self.graph.neighbors(mi))
        neighbors_j = set(self.graph.neighbors(mj))
        common_neighbors = neighbors_i & neighbors_j
        affected_nodes = {mi, mj} | common_neighbors
        _t2 = _time.perf_counter()
        
        # Step 3: 在受影响子图中枚举所有极大团
        subgraph = self.graph.subgraph(affected_nodes)
        local_cliques = [set(c) for c in nx.find_cliques(subgraph)]
        _t3 = _time.perf_counter()
        
        # Step 4: 对每个局部团，检查是否需要更新 region
        for K in local_cliques:
            # 检查 K 是否已被某个现有 region 完全包含
            if self._is_covered(K):
                continue
            
            # 找到与 K 有交集的候选 region
            candidates = self._find_intersecting_regions(K)
            
            if not candidates:
                # 没有候选 region，创建新 region
                self.regions[self._next_region_id] = set(K)
                self._next_region_id += 1
            else:
                # 尝试合并
                self._merge_clique_with_regions(K, candidates)
        
        # Step 5: 移除被完全包含的子集 region
        self._remove_subset_regions()
        _t4 = _time.perf_counter()
        
        # 记录各步骤耗时
        self._last_timing = {
            'total_ms': (_t4 - _t0) * 1000,
            'step1_add_edge_ms': (_t1 - _t0) * 1000,
            'step2_affected_nodes_ms': (_t2 - _t1) * 1000,
            'step3_find_cliques_ms': (_t3 - _t2) * 1000,
            'step4_merge_ms': (_t4 - _t3) * 1000,
            'affected_size': len(affected_nodes),
            'num_cliques': len(local_cliques),
        }
        
        self._record_history("edge_addition", (mi, mj), snapshot_before, 
                             self.regions, f"Affected nodes: {affected_nodes}")
        return self.regions
    
    # =========================================================================
    # Algorithm 4: Incremental Region Update for Edge Removal
    # =========================================================================
    
    def edge_removal(self, mi: int, mj: int) -> Dict[int, Set[int]]:
        """
        论文 Algorithm 4: 边删除的增量 region 更新
        
        Parameters
        ----------
        mi, mj : int
            删除边的两个端点
            
        Returns
        -------
        Dict[int, Set[int]]
            更新后的 region 集合 {id: nodes}
        """
        import time as _time
        _t0 = _time.perf_counter()
        
        snapshot_before = deepcopy(self.regions)
        
        if not self.graph.has_edge(mi, mj):
            self._record_history("edge_removal", (mi, mj), snapshot_before,
                                 self.regions, "Edge does not exist, no change")
            return self.regions
        
        # Step 1: 从图中删除边
        self.graph.remove_edge(mi, mj)
        _t1 = _time.perf_counter()
        
        # Step 2: 对每个同时包含 mi 和 mj 的 region 进行处理
        regions_to_remove = []
        pruned: Set[int] = set()
        for rid, R in list(self.regions.items()):
            if mi in R and mj in R:
                nodes_to_remove = set()
                for x in [mi, mj]:
                    remaining = R - {x}
                    if remaining and not any(
                        self.graph.has_edge(x, y) for y in remaining
                    ):
                        nodes_to_remove.add(x)
                
                self.regions[rid] = R - nodes_to_remove
                pruned |= nodes_to_remove
                
                if not self.regions[rid]:
                    regions_to_remove.append(rid)
        _t2 = _time.perf_counter()
        
        # 只删除变空的 region；被剪掉且不属于任何 region 的节点自成单节点 region
        for rid in regions_to_remove:
            del self.regions[rid]
        for x in sorted(pruned):
            if self.graph.has_node(x) and not any(x in R for R in self.regions.values()):
                self.regions[self._next_region_id] = {x}
                self._next_region_id += 1
        _t3 = _time.perf_counter()
        
        # 记录各步骤耗时
        self._last_timing = {
            'total_ms': (_t3 - _t0) * 1000,
            'step1_remove_edge_ms': (_t1 - _t0) * 1000,
            'step2_check_regions_ms': (_t2 - _t1) * 1000,
            'step3_cleanup_ms': (_t3 - _t2) * 1000,
        }
        
        self._record_history("edge_removal", (mi, mj), snapshot_before,
                             self.regions, f"Removed edge ({mi},{mj})")
        return self.regions
    
    # =========================================================================
    # 批量更新（回应审稿人：多边同时变化）
    # =========================================================================
    
    def batch_update(self, changes: List[Tuple[str, int, int]]) -> Dict[int, Set[int]]:
        """
        批量拓扑变化处理
        
        先将所有变化应用到图，再对受影响子图做局部重分区。
        避免逐边更新的顺序依赖和级联重配置问题。
        
        Parameters
        ----------
        changes : List[Tuple[str, int, int]]
            变化列表，每项为 ('+', mi, mj) 或 ('-', mi, mj)
            
        Returns
        -------
        Dict[int, Set[int]]
            更新后的 region 集合
        """
        snapshot_before = deepcopy(self.regions)
        
        if not changes:
            return self.regions
        
        import time as _time
        _t0 = _time.perf_counter()
        
        # Step 1: 收集所有受影响节点（在应用变化之前）
        affected_nodes = set()
        for op, mi, mj in changes:
            affected_nodes.add(mi)
            affected_nodes.add(mj)
            if self.graph.has_node(mi):
                affected_nodes.update(self.graph.neighbors(mi))
            if self.graph.has_node(mj):
                affected_nodes.update(self.graph.neighbors(mj))
        _t1 = _time.perf_counter()
        
        # Step 2: 一次性应用所有变化到图
        for op, mi, mj in changes:
            if op == '+':
                self.graph.add_edge(mi, mj)
            elif op == '-':
                if self.graph.has_edge(mi, mj):
                    self.graph.remove_edge(mi, mj)
        
        # 应用变化后，更新受影响节点的邻居（新边可能引入新邻居）
        for op, mi, mj in changes:
            if self.graph.has_node(mi):
                affected_nodes.update(self.graph.neighbors(mi))
            if self.graph.has_node(mj):
                affected_nodes.update(self.graph.neighbors(mj))
        
        # 只保留图中实际存在的节点
        affected_nodes = affected_nodes & set(self.graph.nodes())
        _t2 = _time.perf_counter()
        
        # Step 3: 找出所有涉及受影响节点的旧 region
        preserved_regions = {}
        removed_region_ids = []
        for rid, R in self.regions.items():
            if R & affected_nodes:
                removed_region_ids.append(rid)
                affected_nodes.update(R)
            else:
                preserved_regions[rid] = R
        
        affected_nodes = affected_nodes & set(self.graph.nodes())
        _t3 = _time.perf_counter()
        
        # Step 4: 在受影响子图上做局部重分区
        subgraph = self.graph.subgraph(affected_nodes)
        if subgraph.number_of_edges() > 0:
            local_cliques = [set(c) for c in nx.find_cliques(subgraph)]
            new_local_regions = self._threshold_merge(local_cliques, self.tau)
        else:
            new_local_regions = []
        _t4 = _time.perf_counter()
        
        # Step 5: 将新 region 与旧 region 匹配，复用 ID 保持编号稳定
        self.regions = dict(preserved_regions)
        
        # 全局最优匹配：计算所有 (new_region, old_region) 的交集大小，
        # 按交集从大到小贪心匹配，确保最相似的配对优先
        old_regions_map = {rid: snapshot_before[rid] for rid in removed_region_ids
                           if rid in snapshot_before}
        
        # 构建所有候选匹配对 (overlap, new_idx, old_rid)
        valid_new_regions = [(i, r) for i, r in enumerate(new_local_regions) if len(r) >= 2]
        match_candidates = []
        for new_idx, r in valid_new_regions:
            for old_rid, old_R in old_regions_map.items():
                overlap = len(r & old_R)
                if overlap > 0:
                    match_candidates.append((overlap, new_idx, old_rid))
        
        # 按交集大小降序排列，贪心匹配
        match_candidates.sort(key=lambda x: -x[0])
        used_old_ids = set()
        used_new_idxs = set()
        assigned = {}  # new_idx -> old_rid
        
        for overlap, new_idx, old_rid in match_candidates:
            if new_idx in used_new_idxs or old_rid in used_old_ids:
                continue
            assigned[new_idx] = old_rid
            used_old_ids.add(old_rid)
            used_new_idxs.add(new_idx)
        
        # 分配 ID
        for new_idx, r in valid_new_regions:
            if new_idx in assigned:
                self.regions[assigned[new_idx]] = r
            else:
                self.regions[self._next_region_id] = r
                self._next_region_id += 1
        
        # Step 6: 去重，移除子集
        self._remove_subset_regions()
        _t5 = _time.perf_counter()
        
        # 记录各步骤耗时
        self._last_timing = {
            'total_ms': (_t5 - _t0) * 1000,
            'step1_collect_affected_ms': (_t1 - _t0) * 1000,
            'step2_apply_changes_ms': (_t2 - _t1) * 1000,
            'step3_find_regions_ms': (_t3 - _t2) * 1000,
            'step4_repartition_ms': (_t4 - _t3) * 1000,
            'step5_id_match_cleanup_ms': (_t5 - _t4) * 1000,
            'affected_size': len(affected_nodes),
            'num_changes': len(changes),
        }
        
        self._record_history("batch_update", changes, snapshot_before,
                             self.regions, 
                             f"Batch: {len(changes)} changes, "
                             f"affected {len(affected_nodes)} nodes")
        return self.regions
    
    # =========================================================================
    # 约束验证
    # =========================================================================
    
    def validate_constraints(self) -> Dict[str, any]:
        """
        验证当前 region 集合是否满足覆盖约束和完整性约束
        
        Returns
        -------
        dict with keys:
            - coverage_satisfied: bool (每条边至少被一个 region 覆盖)
            - completeness_satisfied: bool (每个最大团至少被一个 region 包含)
            - uncovered_edges: list of uncovered edges
            - uncovered_cliques: list of uncovered maximal cliques
        """
        all_regions = list(self.regions.values())
        
        # 覆盖约束：每条边至少被一个 region 包含
        uncovered_edges = []
        for u, v in self.graph.edges():
            covered = any(u in R and v in R for R in all_regions)
            if not covered:
                uncovered_edges.append((u, v))
        
        # 完整性约束：每个最大团（大小≥2）至少被一个 region 包含
        # 孤立节点（大小为1的团）不需要参与共识，不要求被覆盖
        maximal_cliques = [set(c) for c in nx.find_cliques(self.graph)]
        uncovered_cliques = []
        for K in maximal_cliques:
            if len(K) < 2:
                continue
            covered = any(K <= R for R in all_regions)
            if not covered:
                uncovered_cliques.append(K)
        
        return {
            "coverage_satisfied": len(uncovered_edges) == 0,
            "completeness_satisfied": len(uncovered_cliques) == 0,
            "uncovered_edges": uncovered_edges,
            "uncovered_cliques": uncovered_cliques,
            "num_regions": len(self.regions),
            "region_sizes": [len(R) for R in all_regions],
            "max_instances": max(
                sum(1 for R in all_regions if n in R) 
                for n in self.graph.nodes()
            ) if self.graph.nodes() else 0,
        }
    
    def get_statistics(self) -> Dict[str, float]:
        """获取当前 region 集合的统计信息"""
        all_regions = list(self.regions.values())
        if not all_regions or not self.graph.nodes():
            return {}
        
        instances = [
            sum(1 for R in all_regions if n in R) 
            for n in self.graph.nodes()
        ]
        sizes = [len(R) for R in all_regions]
        
        return {
            "num_regions": len(all_regions),
            "avg_region_size": np.mean(sizes),
            "max_region_size": max(sizes),
            "avg_instances": np.mean(instances),
            "max_instances": max(instances),
        }
    
    # =========================================================================
    # 内部辅助方法
    # =========================================================================
    
    def _is_covered(self, clique: Set[int]) -> bool:
        """检查团是否已被某个现有 region 完全包含"""
        return any(clique <= R for R in self.regions.values())
    
    def _find_intersecting_regions(self, clique: Set[int]) -> List[int]:
        """找到与给定团有交集的 region ID 列表"""
        return [rid for rid, R in self.regions.items() if R & clique]
    
    def _merge_clique_with_regions(self, K: Set[int], candidate_ids: List[int]):
        """
        将团 K 与候选 region 合并
        
        合并策略：
        1. 如果候选 region 被 K 完全包含，标记为待移除
        2. 如果 K 与候选 region 的并集不超过 τ，合并
        3. 否则创建新 region
        """
        merged = set(K)
        regions_to_remove = []
        merge_target_id = None
        
        for rid in candidate_ids:
            R = self.regions[rid]
            if R <= K:
                # region 被 K 完全包含，标记移除
                regions_to_remove.append(rid)
            elif len(merged | R) <= self.tau:
                # 可以合并
                merged = merged | R
                regions_to_remove.append(rid)
                if merge_target_id is None:
                    merge_target_id = rid
            # 否则跳过（不合并）
        
        # 移除被吞并的 region
        for rid in regions_to_remove:
            if rid in self.regions:
                del self.regions[rid]
        
        # 放置合并结果：优先复用最早的 region ID 以保持编号稳定
        if merge_target_id is not None and merge_target_id not in self.regions:
            # 复用被移除的最小 ID
            reuse_id = min(regions_to_remove) if regions_to_remove else self._next_region_id
            self.regions[reuse_id] = merged
            if reuse_id == self._next_region_id:
                self._next_region_id += 1
        elif merge_target_id is not None and merge_target_id in self.regions:
            self.regions[merge_target_id] = merged
        else:
            # 所有候选都被移除了，用最小的旧 ID
            if regions_to_remove:
                self.regions[min(regions_to_remove)] = merged
            else:
                self.regions[self._next_region_id] = merged
                self._next_region_id += 1
    
    def _remove_subset_regions(self):
        """移除被其他 region 完全包含的子集 region"""
        ids = list(self.regions.keys())
        to_remove = []
        for i, rid_i in enumerate(ids):
            for j, rid_j in enumerate(ids):
                if i != j and rid_i not in to_remove and rid_j not in to_remove:
                    if self.regions[rid_i] < self.regions[rid_j]:
                        to_remove.append(rid_i)
                        break
        for rid in to_remove:
            del self.regions[rid]
    
    def _threshold_merge(self, cliques: List[Set[int]], tau: int) -> List[Set[int]]:
        """
        阈值合并：将小团合并为不超过 τ 的 region
        与 strategies.py 中的逻辑一致
        """
        partitions = sorted(cliques, key=lambda x: (-len(x), min(x)))
        
        merged = True
        while merged:
            merged = False
            new_partitions = []
            used = [False] * len(partitions)
            
            for i in range(len(partitions)):
                if used[i]:
                    continue
                current = set(partitions[i])
                for j in range(i + 1, len(partitions)):
                    if used[j]:
                        continue
                    candidate = partitions[j]
                    # 有交集且合并后不超过 τ
                    if current & candidate and len(current | candidate) <= tau:
                        current = current | candidate
                        used[j] = True
                        merged = True
                new_partitions.append(current)
                used[i] = True
            
            partitions = new_partitions
        
        return partitions
    
    def _record_history(self, operation: str, params, before, after, note: str = ""):
        """记录一步变化历史"""
        self.history.append({
            "step": len(self.history),
            "operation": operation,
            "params": params,
            "regions_before": deepcopy(before),
            "regions_after": deepcopy(after),
            "graph_snapshot": self.graph.copy(),
            "note": note,
        })


# =============================================================================
# 便捷工厂函数
# =============================================================================

def create_manager_from_graph(graph: nx.Graph, tau: int, 
                               strategy: str = "threshold") -> DynamicRegionManager:
    """
    从图创建 DynamicRegionManager，自动执行初始分区
    
    Parameters
    ----------
    graph : nx.Graph
    tau : int
        region 大小上限
    strategy : str
        初始分区策略: "threshold", "clique"
    """
    from .strategies import PartitionStrategies
    
    ps = PartitionStrategies(graph)
    if strategy == "threshold":
        initial_regions = ps.threshold_based(tau)
    elif strategy == "clique":
        initial_regions = ps.maximal_clique()
    else:
        initial_regions = ps.threshold_based(tau)
    
    return DynamicRegionManager(graph, initial_regions, tau)


# =============================================================================
# 优化版本：反向索引 + 局部子集检查
# =============================================================================

class DynamicRegionManagerOptimized:
    """
    优化版动态区域管理器
    
    优化点：
    1. 维护 node→regions 反向索引，加速 _is_covered 和 _find_intersecting_regions
    2. 局部子集检查，只对被修改的 region 做子集移除，避免 O(R²)
    """
    
    def __init__(self, graph: nx.Graph, regions: List[Set[int]], tau: int):
        self.graph = graph.copy()
        self.tau = tau
        self._next_region_id = 0
        
        self.regions: Dict[int, Set[int]] = {}
        for r in regions:
            self.regions[self._next_region_id] = set(r)
            self._next_region_id += 1
        
        # 反向索引: node → set of region IDs
        self._node_to_regions: Dict[int, Set[int]] = {}
        self._rebuild_index()
        
        # 计时
        self._last_timing = {}
    
    def _rebuild_index(self):
        """重建反向索引"""
        self._node_to_regions = {}
        for rid, R in self.regions.items():
            for node in R:
                if node not in self._node_to_regions:
                    self._node_to_regions[node] = set()
                self._node_to_regions[node].add(rid)
    
    def _index_add(self, rid: int, nodes: Set[int]):
        """向索引中添加 region"""
        for node in nodes:
            if node not in self._node_to_regions:
                self._node_to_regions[node] = set()
            self._node_to_regions[node].add(rid)
    
    def _index_remove(self, rid: int, nodes: Set[int]):
        """从索引中移除 region"""
        for node in nodes:
            if node in self._node_to_regions:
                self._node_to_regions[node].discard(rid)
                if not self._node_to_regions[node]:
                    del self._node_to_regions[node]
    
    def _is_covered(self, K: Set[int]) -> bool:
        """O(|K| × avg_instances) 检查团是否被某个 region 完全包含"""
        first_node = next(iter(K))
        candidate_rids = self._node_to_regions.get(first_node, set()).copy()
        if not candidate_rids:
            return False
        for node in K:
            candidate_rids &= self._node_to_regions.get(node, set())
            if not candidate_rids:
                return False
        # candidate_rids 中的 region 包含 K 的所有节点
        # 还需确认 K ⊆ R（candidate 包含 K 的所有节点即可）
        return bool(candidate_rids)
    
    def _find_intersecting_regions(self, K: Set[int]) -> List[int]:
        """O(|K| × avg_instances) 找与 K 有交集的 region"""
        rids = set()
        for node in K:
            rids.update(self._node_to_regions.get(node, set()))
        return list(rids)
    
    def _remove_subset_regions_local(self, check_rids: Set[int]):
        """
        局部子集/重复消除：对 check_rids 中的每个（新）region R，检查与其有交集的
        所有 region O：
          - R ⊂ O（真子集）或 R == O（重复）→ 移除 R（重复时优先保留旧 ID）
          - O ⊂ R（真子集）→ 移除 O
        双向检查是必要的：局部重分区的子图可能包含被保留 region 的全部节点，
        从而重新生成一个与保留 region 相同或包含/被包含的 region。
        """
        removed: Set[int] = set()
        for rid in sorted(check_rids):
            if rid in removed or rid not in self.regions:
                continue
            R = self.regions[rid]
            candidates = set()
            for node in R:
                candidates.update(self._node_to_regions.get(node, set()))
            candidates.discard(rid)
            for other_rid in candidates:
                if other_rid in removed or other_rid not in self.regions:
                    continue
                O = self.regions[other_rid]
                if R < O:
                    removed.add(rid)
                    break
                if R == O:
                    # duplicate: keep the preserved/older region
                    if other_rid not in check_rids or other_rid < rid:
                        removed.add(rid)
                        break
                    removed.add(other_rid)
                    continue
                if O < R:
                    removed.add(other_rid)
        
        for rid in removed:
            if rid in self.regions:
                self._index_remove(rid, self.regions[rid])
                del self.regions[rid]
    
    def _merge_clique_with_regions(self, K: Set[int], candidate_ids: List[int]) -> Set[int]:
        """合并团 K 与候选 region，返回被修改的 region IDs"""
        merged = set(K)
        regions_to_remove = []
        merge_target_id = None
        
        for rid in candidate_ids:
            R = self.regions[rid]
            if R <= K:
                regions_to_remove.append(rid)
            elif len(merged | R) <= self.tau:
                merged = merged | R
                regions_to_remove.append(rid)
                if merge_target_id is None:
                    merge_target_id = rid
        
        # 移除被吞并的 region
        for rid in regions_to_remove:
            if rid in self.regions:
                self._index_remove(rid, self.regions[rid])
                del self.regions[rid]
        
        # 放置合并结果
        if regions_to_remove:
            reuse_id = min(regions_to_remove)
        else:
            reuse_id = self._next_region_id
            self._next_region_id += 1
        
        self.regions[reuse_id] = merged
        self._index_add(reuse_id, merged)
        
        return {reuse_id} | set(regions_to_remove)
    
    # =========================================================================
    # Algorithm 3 优化版
    # =========================================================================
    
    def edge_addition(self, mi: int, mj: int) -> Dict[int, Set[int]]:
        """优化版 edge_addition：反向索引 + 局部子集检查"""
        import time as _time
        _t0 = _time.perf_counter()
        
        if self.graph.has_edge(mi, mj):
            return self.regions
        
        # Step 1: 添加边
        self.graph.add_edge(mi, mj)
        _t1 = _time.perf_counter()
        
        # Step 2: 计算受影响节点集
        neighbors_i = set(self.graph.neighbors(mi))
        neighbors_j = set(self.graph.neighbors(mj))
        common_neighbors = neighbors_i & neighbors_j
        affected_nodes = {mi, mj} | common_neighbors
        _t2 = _time.perf_counter()
        
        # Step 3: 枚举局部极大团
        subgraph = self.graph.subgraph(affected_nodes)
        local_cliques = [set(c) for c in nx.find_cliques(subgraph)]
        _t3 = _time.perf_counter()
        
        # Step 4: 合并 + 局部子集检查
        modified_rids = set()
        for K in local_cliques:
            if self._is_covered(K):
                continue
            candidates = self._find_intersecting_regions(K)
            if not candidates:
                new_id = self._next_region_id
                self._next_region_id += 1
                self.regions[new_id] = set(K)
                self._index_add(new_id, K)
                modified_rids.add(new_id)
            else:
                changed = self._merge_clique_with_regions(K, candidates)
                modified_rids.update(changed)
        
        # 局部子集检查（只检查被修改的 region）
        self._remove_subset_regions_local(modified_rids)
        _t4 = _time.perf_counter()
        
        self._last_timing = {
            'total_ms': (_t4 - _t0) * 1000,
            'step1_add_edge_ms': (_t1 - _t0) * 1000,
            'step2_affected_nodes_ms': (_t2 - _t1) * 1000,
            'step3_find_cliques_ms': (_t3 - _t2) * 1000,
            'step4_merge_ms': (_t4 - _t3) * 1000,
            'affected_size': len(affected_nodes),
            'num_cliques': len(local_cliques),
        }
        return self.regions
    
    # =========================================================================
    # Algorithm 4 优化版
    # =========================================================================
    
    def edge_removal(self, mi: int, mj: int) -> Dict[int, Set[int]]:
        """优化版 edge_removal"""
        import time as _time
        _t0 = _time.perf_counter()
        
        if not self.graph.has_edge(mi, mj):
            return self.regions
        
        # Step 1: 删除边
        self.graph.remove_edge(mi, mj)
        _t1 = _time.perf_counter()
        
        # Step 2: 找同时包含 mi 和 mj 的 region（通过索引）
        rids_mi = self._node_to_regions.get(mi, set())
        rids_mj = self._node_to_regions.get(mj, set())
        affected_rids = rids_mi & rids_mj
        
        regions_to_remove = []
        pruned: Set[int] = set()
        for rid in list(affected_rids):
            if rid not in self.regions:
                continue
            R = self.regions[rid]
            nodes_to_remove = set()
            for x in [mi, mj]:
                remaining = R - {x}
                if remaining and not any(
                    self.graph.has_edge(x, y) for y in remaining
                ):
                    nodes_to_remove.add(x)
            
            if nodes_to_remove:
                self._index_remove(rid, nodes_to_remove)
                self.regions[rid] = R - nodes_to_remove
                pruned |= nodes_to_remove
            
            if not self.regions[rid]:
                regions_to_remove.append(rid)
        _t2 = _time.perf_counter()
        
        # 只删除变空的 region；单节点 region 保留（与静态阈值分区一致：孤立节点自成 region）
        for rid in regions_to_remove:
            del self.regions[rid]
        # 被剪掉且不再属于任何 region 的节点退化为单节点 region，继续独自服务自己的覆盖区
        self._ensure_singletons(pruned)
        _t3 = _time.perf_counter()
        
        self._last_timing = {
            'total_ms': (_t3 - _t0) * 1000,
            'step1_remove_edge_ms': (_t1 - _t0) * 1000,
            'step2_check_regions_ms': (_t2 - _t1) * 1000,
            'step3_cleanup_ms': (_t3 - _t2) * 1000,
        }
        return self.regions
    
    # =========================================================================
    # Batch Update 优化版
    # =========================================================================
    
    def batch_update(self, changes: List[Tuple[str, int, int]]) -> Dict[int, Set[int]]:
        """
        优化版批量更新：利用反向索引加速受影响 region 查找 + 局部子集检查
        """
        import time as _time
        
        if not changes:
            return self.regions
        
        _t0 = _time.perf_counter()
        
        # Step 1: 收集所有受影响节点
        affected_nodes = set()
        for op, mi, mj in changes:
            affected_nodes.add(mi)
            affected_nodes.add(mj)
            if self.graph.has_node(mi):
                affected_nodes.update(self.graph.neighbors(mi))
            if self.graph.has_node(mj):
                affected_nodes.update(self.graph.neighbors(mj))
        _t1 = _time.perf_counter()
        
        # Step 2: 一次性应用所有变化
        for op, mi, mj in changes:
            if op == '+':
                self.graph.add_edge(mi, mj)
            elif op == '-':
                if self.graph.has_edge(mi, mj):
                    self.graph.remove_edge(mi, mj)
        
        for op, mi, mj in changes:
            if self.graph.has_node(mi):
                affected_nodes.update(self.graph.neighbors(mi))
            if self.graph.has_node(mj):
                affected_nodes.update(self.graph.neighbors(mj))
        affected_nodes = affected_nodes & set(self.graph.nodes())
        _t2 = _time.perf_counter()
        
        # Step 3: 通过索引找受影响 region（O(|affected| × avg_instances)）
        affected_rids = set()
        for node in affected_nodes:
            affected_rids.update(self._node_to_regions.get(node, set()))
        
        # 把受影响 region 的所有节点也加入 affected_nodes。
        # 注意：这里不能再用扩展后的 affected_nodes 反向扩展 affected_rids——
        # 否则会移除一些 region 而其节点却不在局部子图中，导致这些 region 覆盖的边
        # 在重分区后失去覆盖（违反覆盖约束）。正确性条件只要求
        # “被移除 region 的全部节点 ⊆ 局部子图节点”，一次扩展即可满足。
        for rid in affected_rids:
            if rid in self.regions:
                affected_nodes.update(self.regions[rid])
        affected_nodes = affected_nodes & set(self.graph.nodes())
        
        # 分离保留 / 移除的 region
        preserved_regions = {}
        removed_region_ids = []
        old_regions_snapshot = {}
        for rid in list(self.regions.keys()):
            if rid in affected_rids:
                removed_region_ids.append(rid)
                old_regions_snapshot[rid] = self.regions[rid].copy()
            else:
                preserved_regions[rid] = self.regions[rid]
        _t3 = _time.perf_counter()
        
        # Step 4: 局部重分区
        subgraph = self.graph.subgraph(affected_nodes)
        if subgraph.number_of_edges() > 0:
            local_cliques = [set(c) for c in nx.find_cliques(subgraph)]
            new_local_regions = self._threshold_merge(local_cliques, self.tau)
        else:
            new_local_regions = []
        _t4 = _time.perf_counter()
        
        # Step 5: ID 匹配 + 索引更新
        # 移除旧 region 的索引
        for rid in removed_region_ids:
            if rid in self.regions:
                self._index_remove(rid, self.regions[rid])
                del self.regions[rid]
        
        # 构建 node → old_rid 映射，用于快速 overlap 计算
        node_to_old_rids: Dict[int, Set[int]] = {}
        for old_rid, old_R in old_regions_snapshot.items():
            for node in old_R:
                if node not in node_to_old_rids:
                    node_to_old_rids[node] = set()
                node_to_old_rids[node].add(old_rid)
        
        # 匹配新旧 region：通过节点索引计算 overlap，避免 O(new × old) 全配对
        valid_new_regions = [(i, r) for i, r in enumerate(new_local_regions) if len(r) >= 2]
        match_candidates = []
        for new_idx, r in valid_new_regions:
            overlap_count: Dict[int, int] = {}
            for node in r:
                for old_rid in node_to_old_rids.get(node, set()):
                    overlap_count[old_rid] = overlap_count.get(old_rid, 0) + 1
            for old_rid, count in overlap_count.items():
                match_candidates.append((count, new_idx, old_rid))
        
        match_candidates.sort(key=lambda x: -x[0])
        used_old_ids = set()
        used_new_idxs = set()
        assigned = {}
        
        for overlap, new_idx, old_rid in match_candidates:
            if new_idx in used_new_idxs or old_rid in used_old_ids:
                continue
            assigned[new_idx] = old_rid
            used_old_ids.add(old_rid)
            used_new_idxs.add(new_idx)
        
        # 分配 ID 并更新索引
        new_rids = set()
        for new_idx, r in valid_new_regions:
            if new_idx in assigned:
                rid = assigned[new_idx]
            else:
                rid = self._next_region_id
                self._next_region_id += 1
            self.regions[rid] = r
            self._index_add(rid, r)
            new_rids.add(rid)
        _t5 = _time.perf_counter()
        
        # Step 6: 局部子集检查（只检查新加入的 region）
        self._remove_subset_regions_local(new_rids)
        _t6 = _time.perf_counter()
        
        self._last_timing = {
            'total_ms': (_t6 - _t0) * 1000,
            'step1_collect_affected_ms': (_t1 - _t0) * 1000,
            'step2_apply_changes_ms': (_t2 - _t1) * 1000,
            'step3_find_regions_ms': (_t3 - _t2) * 1000,
            'step4_repartition_ms': (_t4 - _t3) * 1000,
            'step5_id_match_ms': (_t5 - _t4) * 1000,
            'step6_subset_check_ms': (_t6 - _t5) * 1000,
            'affected_size': len(affected_nodes),
            'num_changes': len(changes),
        }
        return self.regions

    # =========================================================================
    # Batch Update — 顺序单边语义（论文 Algorithm 3/4 逐条应用，一次重配）
    # =========================================================================

    def _ensure_singletons(self, nodes: Set[int]):
        """Algorithm 4 补充规则：失去区域内全部邻居而被剪掉的节点，若不再属于任何
        region，则自成单节点 region（与静态阈值分区对孤立节点的处理一致，保证每个
        在图中的节点始终至少属于一个 region）。"""
        for x in sorted(nodes):
            if self.graph.has_node(x) and not self._node_to_regions.get(x):
                rid = self._next_region_id
                self._next_region_id += 1
                self.regions[rid] = {x}
                self._index_add(rid, {x})

    def remove_nodes(self, nodes: Set[int]):
        """节点删除（论文：先用 Algorithm 4 删除全部关联边，再删除节点本身）。
        调用时节点的边应已删除；这里移除它残留的 region 成员身份（通常只剩它的单节点
        region）并把它从图中删掉。"""
        for x in nodes:
            for rid in list(self._node_to_regions.get(x, set())):
                self._index_remove(rid, {x})
                self.regions[rid].discard(x)
                if not self.regions[rid]:
                    del self.regions[rid]
            if self.graph.has_node(x):
                self.graph.remove_node(x)

    def add_nodes(self, nodes: Set[int]):
        """节点加入（论文：先加入节点，再用 Algorithm 3 逐条加入其关联边）。
        节点先以单节点 region 进入（保证覆盖），随后的 ``edge_addition`` 会把它并入
        更大的团；没有任何边的节点（覆盖范围内无邻居）就保持单节点 region。"""
        for x in sorted(nodes):
            if not self.graph.has_node(x):
                self.graph.add_node(x)
        self._ensure_singletons(set(nodes))

    def batch_update_sequential(self, changes: List[Tuple[str, int, int]],
                                node_removals: Set[int] = frozenset(),
                                node_additions: Set[int] = frozenset()) -> Dict[int, Set[int]]:
        """
        在一个同步间隔内收集到的 *净* 边变化上，逐条应用单边原语：
        先对所有删除边运行 Algorithm 4 (``edge_removal``)，再对所有新增边运行
        Algorithm 3 (``edge_addition``)。中间结果只存在于内存中；调用方只把
        最终的 region 差分交给 Raft 层（一次重配）。``node_removals`` 是本间隔内
        离开网络的节点：其关联边删除之后再把节点本身删掉（论文的节点删除语义），
        因此离开的节点不会留下单节点 region。

        与 ``batch_update``（受影响子图局部重算）的区别：
        * region 的 ID 由单边原语原地维护（合并时复用被吞并 region 的最小 ID），
          因此 planner 几乎总能把变化归为 kept/evolved，而不需要重叠匹配；
        * 结果倾向保留现状（能并入就并入，删除边只剪掉失去所有区域内邻居的节点），
          region 只会在阈值 τ 内增长或按 Algorithm 4 收缩，不会被重排；
        * 代价是 *过覆盖*：删除的边所在 region 不会被拆分，region 可以比
          对当前图重新做阈值分区更大（覆盖约束仍然满足，只是同步范围偏大）。
          ``overcoverage`` 可用于量化这一漂移。

        先删后加的原因：Algorithm 3 在受影响子图上枚举极大团，先应用删除能让
        这些团在 *最终* 图上计算，避免利用即将消失的边合并 region。
        """
        import time as _time
        if not changes and not node_removals and not node_additions:
            return self.regions
        _t0 = _time.perf_counter()
        removals = sorted({(min(mi, mj), max(mi, mj)) for op, mi, mj in changes if op == '-'})
        additions = sorted({(min(mi, mj), max(mi, mj)) for op, mi, mj in changes if op == '+'})
        per_op = []
        for mi, mj in removals:
            self.edge_removal(mi, mj)
            per_op.append(('-', mi, mj, dict(self._last_timing)))
        if node_removals:
            self.remove_nodes(set(node_removals))
        if node_additions:
            self.add_nodes(set(node_additions))
        for mi, mj in additions:
            self.edge_addition(mi, mj)
            per_op.append(('+', mi, mj, dict(self._last_timing)))
        _t1 = _time.perf_counter()
        affected = [t.get('affected_size', 0) for op, _, _, t in per_op if op == '+']
        self._last_timing = {
            'total_ms': (_t1 - _t0) * 1000,
            'num_changes': len(changes),
            'num_removals': len(removals),
            'num_additions': len(additions),
            'affected_size': max(affected) if affected else 0,
            'affected_size_sum': sum(affected),
            'ops': per_op,
        }
        return self.regions

    def overcoverage(self, tau: Optional[int] = None) -> Dict[str, float]:
        """
        过覆盖度量：当前 region 集合与对 *当前图* 重新做阈值分区的结果比较。
        返回 sum|R|（= N * avg instances）与 region 数的当前值/重算值及其比值。
        """
        from .strategies import PartitionStrategies
        tau = self.tau if tau is None else tau
        cur_sum = sum(len(r) for r in self.regions.values() if len(r) >= 2)   # singletons excluded on both sides
        if self.graph.number_of_edges() == 0:
            fresh = []
        else:
            fresh = [set(r) for r in PartitionStrategies(self.graph).threshold_based(tau) if len(r) >= 2]
        fresh_sum = sum(len(r) for r in fresh)
        return {
            'size_sum': cur_sum, 'fresh_size_sum': fresh_sum,
            'num_regions': len(self.regions), 'fresh_num_regions': len(fresh),
            'size_ratio': (cur_sum / fresh_sum) if fresh_sum else 1.0,
        }

    def validate_clique_completeness(self) -> Dict:
        """完备性约束（论文式 (5)）：每个极大团都完整包含在某个 region 中。"""
        missing = []
        for c in nx.find_cliques(self.graph):
            K = set(c)
            if len(K) < 2:
                continue
            if not self._is_covered(K):
                missing.append(K)
        return {"completeness_satisfied": len(missing) == 0, "uncovered_cliques": missing}
    
    def _threshold_merge(self, cliques: List[Set[int]], tau: int) -> List[Set[int]]:
        """
        阈值合并：先分离大团(>τ)不参与合并，只对小团做贪心合并。
        与 strategies.py._merge_small_partitions 逻辑一致。
        """
        sorted_partitions = sorted(cliques, key=lambda x: (min(x), -len(x)))
        large = [p for p in sorted_partitions if len(p) > tau]
        small = [p for p in sorted_partitions if len(p) <= tau]
        
        merged_small = []
        while small:
            first = small.pop(0)
            current = set(first)
            changed = True
            while changed:
                changed = False
                for other in small[:]:
                    if current & other and len(current | other) <= tau:
                        current = current | other
                        small.remove(other)
                        changed = True
            merged_small.append(current)
        
        return large + merged_small
    
    # =========================================================================
    # 约束验证（复用原版逻辑）
    # =========================================================================
    
    def validate_constraints(self) -> Dict:
        """验证覆盖和完备性约束"""
        all_edges = set(self.graph.edges())
        covered_edges = set()
        
        for R in self.regions.values():
            for u in R:
                for v in R:
                    if u < v and self.graph.has_edge(u, v):
                        covered_edges.add((u, v))
        
        uncovered = all_edges - covered_edges
        all_nodes_in_regions = set()
        for R in self.regions.values():
            all_nodes_in_regions.update(R)
        
        nodes_with_edges = set()
        for u, v in self.graph.edges():
            nodes_with_edges.add(u)
            nodes_with_edges.add(v)
        
        uncovered_nodes = nodes_with_edges - all_nodes_in_regions
        
        return {
            "coverage_satisfied": len(uncovered) == 0,
            "completeness_satisfied": len(uncovered_nodes) == 0,
            "uncovered_edges": uncovered,
            "uncovered_nodes": uncovered_nodes,
            "num_regions": len(self.regions),
        }

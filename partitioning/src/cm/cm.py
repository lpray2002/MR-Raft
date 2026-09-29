"""
Cluster Manager (CM).

Every ``t_sync`` seconds the CM
  1. samples the overlap margins (what a real CM would obtain from coverage
     measurements) and applies hysteresis / dwell filtering  -> observed graph,
  2. computes the *net* edge difference w.r.t. the graph it last partitioned
     (transient add+remove pairs inside the interval cancel out)   [T_batch],
  3. runs the incremental repartitioning (Algorithms 3/4, batched)  [T_part],
  4. turns the region diff into an ordered directive plan (planner) and
     dispatches it to the MEC nodes                                 [T_cfg, T_joint].

The same class drives either a real Go cluster (``RealExecutor``) or an
analytical timing model (``ModelExecutor``) so that long T_sync sweeps can be
simulated with protocol timings measured on the real cluster.
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

import networkx as nx
import numpy as np

from ..partition.dynamic_region import DynamicRegionManagerOptimized
from ..partition.strategies import PartitionStrategies
from .client import (ADD_MEMBER, FENCE_REGION, MIGRATE_STATE, PROMOTE_MEMBER, REMOVE_MEMBER, START_REGION, STOP_REGION,
                     ClusterClient, Directive, DirectiveOutcome)
from .dynamics import CoverageTrace, HysteresisObserver, edge_diff, norm_edge
from .planner import Plan, PlannedOp, build_plan

PHASES = ("create", "grow", "handoff", "shrink", "retire")


# --------------------------------------------------------------------------
# Naming: graph node <-> MEC node id <-> raft node id
# --------------------------------------------------------------------------

class Naming:
    """raft_node_id = cluster*100000 + generation*1000 + mec_node_id (mec id = graph id + 1)."""

    def __init__(self, nodes: List[int], raft_addr: Callable[[int], str]):
        self.nodes = list(nodes)
        self._addr = raft_addr
        self.gen: Dict[Tuple[int, int], int] = {}
        self.left: Set[Tuple[int, int]] = set()

    @staticmethod
    def mec_id(node: int) -> int:
        return node + 1

    @staticmethod
    def graph_id(mec_id: int) -> int:
        return mec_id - 1

    def addr(self, node: int) -> str:
        return self._addr(node)

    def raft_id(self, cluster: int, node: int) -> int:
        return cluster * 100000 + self.gen.get((cluster, node), 0) * 1000 + self.mec_id(node)

    def on_join(self, cluster: int, node: int):
        """Dragonboat refuses to re-add a removed raft node id -> bump generation."""
        if (cluster, node) in self.left:
            self.gen[(cluster, node)] = self.gen.get((cluster, node), 0) + 1
            self.left.discard((cluster, node))

    def on_leave(self, cluster: int, node: int):
        self.left.add((cluster, node))

    @staticmethod
    def decode(raft_id: int) -> Tuple[int, int, int]:
        """-> (cluster, generation, graph node)"""
        cluster = raft_id // 100000
        gen = (raft_id % 100000) // 1000
        mec = raft_id % 1000
        return cluster, gen, mec - 1


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------

@dataclass
class DirectiveRecord:
    round: int
    phase: str
    op: str
    cluster: int
    node: int
    target: int
    t_sent: float
    t_done: float
    ok: bool
    err: str
    elapsed_us: int
    leader_wait_us: int
    rpc_us: int
    bytes: int
    migrated_keys: int
    note: str


@dataclass
class RoundRecord:
    round: int
    t_tick: float                 # CM tick time (sim/real seconds since start)
    n_changes: int
    added_edges: List[Tuple[int, int]]
    removed_edges: List[Tuple[int, int]]
    n_observed_edges: int
    reconfigured: bool
    regions_before: int = 0
    regions_after: int = 0
    kinds: Dict[str, int] = field(default_factory=dict)
    n_directives: int = 0
    bytes_cfg: int = 0
    t_batch_ms: float = 0.0
    t_part_ms: float = 0.0
    t_cfg_ms: float = 0.0        # create phase wall time (incl. election of new groups)
    t_joint_ms: float = 0.0      # grow + shrink wall time (membership changes)
    t_migrate_ms: float = 0.0
    t_retire_ms: float = 0.0
    t_transfer_ms: float = 0.0   # adversarial leader-transfer phase wall time
    t_trans_ms: float = 0.0
    t_done: float = 0.0
    elections: int = 0
    membership_changes: int = 0
    failures: int = 0
    constraints_ok: bool = True
    # --- availability accounting (scope x duration of one reconfiguration) ---
    leader_transfers: int = 0            # leader transfers (removal of a leader + adversarial ones)
    n_reconf_nodes: int = 0              # nodes that received at least one directive
    n_transfer_member_nodes: int = 0     # sum |R| over regions whose leader was transferred (multiplicity)
    unavail_node_ms: float = 0.0         # sum_j unavail_j [ms], from leader-transfer waits x region size
    # --- over-coverage of the sequential (Algorithm 3/4) batch semantics ---
    size_sum: int = 0                    # sum |R| of the maintained regions (= N * avg instances)
    fresh_size_sum: int = 0              # sum |R| of a fresh threshold partition of the same graph
    size_ratio: float = 1.0


# --------------------------------------------------------------------------
# Executors
# --------------------------------------------------------------------------

class Clock:
    """Wall clock (real mode) or virtual clock (sim mode), seconds since start."""

    def __init__(self, virtual: bool):
        self.virtual = virtual
        self._t0 = time.time()
        self._vt = 0.0

    @property
    def epoch0(self) -> float:
        """Wall-clock epoch (s) of CM time 0 (real mode); used to align node event logs."""
        return self._t0

    def now(self) -> float:
        return self._vt if self.virtual else time.time() - self._t0

    def advance(self, dt: float):
        self._vt += dt

    def set(self, t: float):
        self._vt = t


class RealExecutor:
    def __init__(self, cluster: ClusterClient, naming: Naming, workers: int = 32,
                 members_lookup: Optional[Callable[[int], Set[int]]] = None):
        self.cluster = cluster
        self.naming = naming
        self.pool = ThreadPoolExecutor(max_workers=workers)
        self.members_lookup = members_lookup      # cluster id -> current serving graph nodes

    def _find_leader(self, cluster_id: int, exclude: int) -> Optional[int]:
        """Ask the serving members who the leader is; returns a graph node or None."""
        if self.members_lookup is None:
            return None
        for n in sorted(self.members_lookup(cluster_id) or []):
            if n == exclude:
                continue
            try:
                st = self.cluster[self.naming.mec_id(n)].get_status(cluster_id, timeout_s=1.0)
                if st.regions and st.regions[0].leader_id:
                    return Naming.decode(st.regions[0].leader_id)[2]
            except Exception:
                continue
        return None

    def run_sequences(self, version: int, sequences: List[List[Directive]]) -> List[DirectiveOutcome]:
        """Each sequence is executed in order; sequences run in parallel."""
        def apply(d: Directive) -> DirectiveOutcome:
            mec = self.naming.mec_id(d.node)
            try:
                return self.cluster[mec].apply_config(version, [d])[0]
            except Exception as e:  # RPC failure
                return DirectiveOutcome(d, False, f"rpc: {e}", 0, 0, 0, 0, 0, time.time() * 1000)

        def run_seq(seq: List[Directive]) -> List[DirectiveOutcome]:
            outs = []
            for d in seq:
                o = apply(d)
                # membership change failed (e.g. leader changed meanwhile): retry once on the current leader
                if not o.success and d.op in (ADD_MEMBER, REMOVE_MEMBER, PROMOTE_MEMBER):
                    leader = self._find_leader(d.region_id, exclude=Naming.decode(d.target_raft_node_id)[2])
                    if leader is not None and leader != d.node:
                        d2 = Directive(**{**d.__dict__, "node": leader, "note": d.note + " [retry on leader]"})
                        o2 = apply(d2)
                        o2.rpc_elapsed_us += o.rpc_elapsed_us
                        o = o2
                # state handoff is idempotent (version-aware LWW): retry a transiently failed copy
                # (e.g. proposals dropped right after the destination's membership change)
                if not o.success and d.op == MIGRATE_STATE:
                    for _ in range(2):
                        time.sleep(0.05)
                        o2 = apply(Directive(**{**d.__dict__, "note": d.note + " [retry]"}))
                        o2.rpc_elapsed_us += o.rpc_elapsed_us
                        o = o2
                        if o.success:
                            break
                outs.append(o)
                if not o.success and d.op in (ADD_MEMBER,):
                    break  # do not JOIN a node that was not added
            return outs
        results = list(self.pool.map(run_seq, sequences))
        return [o for seq in results for o in seq]

    def transfer_leader(self, cluster_id: int, leader_node: int, target_node: int) -> Tuple[bool, str, float, int]:
        """Hand leadership of ``cluster_id`` from ``leader_node`` to ``target_node`` (graph ids).
        Returns (ok, err, wait_ms, observed_leader_raft_id)."""
        target_raft = self.naming.raft_id(cluster_id, target_node)
        try:
            return self.cluster[self.naming.mec_id(leader_node)].request_leader_transfer(cluster_id, target_raft)
        except Exception as e:
            return False, f"rpc: {e}", 0.0, 0


class ModelExecutor:
    """Analytical timing model used for T_sync sweeps (sim mode).

    Leaders are modelled explicitly (random member at creation, the executing
    node after a leader removal) so that the availability accounting of the CM
    can be reproduced without a cluster: every leader transfer costs
    ``transfer_ms`` of write unavailability for all members of the region.
    """

    def __init__(self, naming: Naming, rtt_ms: float = 2.0, elect_ms: Tuple[float, float] = (150, 300),
                 commit_ms: float = 8.0, start_ms: float = 12.0, keys_per_region: int = 20, seed: int = 1,
                 transfer_ms: float = 3.0):
        self.naming = naming
        self.rtt = rtt_ms
        self.elect = elect_ms
        self.commit = commit_ms
        self.start = start_ms
        self.keys = keys_per_region
        self.transfer_ms = transfer_ms
        self.rng = np.random.default_rng(seed)
        self.last_elapsed_ms = 0.0
        self.leaders: Dict[int, int] = {}        # cluster id -> leader graph node (model state)

    def transfer_leader(self, cluster_id: int, leader_node: int, target_node: int) -> Tuple[bool, str, float, int]:
        self.leaders[cluster_id] = target_node
        self.last_elapsed_ms = self.transfer_ms + self.rtt
        return True, "", self.transfer_ms, self.naming.raft_id(cluster_id, target_node)

    def run_sequences(self, version: int, sequences: List[List[Directive]]) -> List[DirectiveOutcome]:
        outs = []
        seq_times = []
        for seq in sequences:
            t_seq = 0.0
            for d in seq:
                if d.op == START_REGION:
                    wait = 0.0 if d.join else float(self.rng.uniform(*self.elect))
                    el = self.start + wait
                    leader_raft = 0
                    if not d.join:
                        # bootstrap election: one member becomes leader (chosen once per region)
                        if d.region_id not in self.leaders:
                            members = sorted(Naming.decode(r)[2] for r in d.members) if d.members else [d.node]
                            self.leaders[d.region_id] = int(self.rng.choice(members))
                        leader_raft = self.naming.raft_id(d.region_id, self.leaders[d.region_id])
                    outs.append(DirectiveOutcome(d, True, "", int(el * 1000), int(wait * 1000), leader_raft, 0,
                                                 int((el + self.rtt) * 1000), 0))
                elif d.op == REMOVE_MEMBER:
                    target = Naming.decode(d.target_raft_node_id)[2]
                    wait = 0.0
                    if self.leaders.get(d.region_id) == target and target != d.node:
                        wait = self.transfer_ms               # leader transfer to the executing node first
                        self.leaders[d.region_id] = d.node
                    el = self.commit + wait
                    leader_raft = self.naming.raft_id(d.region_id, self.leaders[d.region_id]) \
                        if d.region_id in self.leaders else 0
                    outs.append(DirectiveOutcome(d, True, "", int(el * 1000), int(wait * 1000), leader_raft, 0,
                                                 int((el + self.rtt) * 1000), 0))
                elif d.op in (ADD_MEMBER, PROMOTE_MEMBER):
                    el = self.commit
                    outs.append(DirectiveOutcome(d, True, "", int(el * 1000), 0, 0, 0, int((el + self.rtt) * 1000), 0))
                elif d.op == MIGRATE_STATE:
                    el = self.commit * max(1, self.keys / 16)
                    outs.append(DirectiveOutcome(d, True, "", int(el * 1000), 0, 0, self.keys, int((el + self.rtt) * 1000), 0))
                else:  # FENCE / STOP: local, ~1 ms (drain of in-flight requests)
                    el = 1.0
                    outs.append(DirectiveOutcome(d, True, "", int(el * 1000), 0, 0, 0, int((el + self.rtt) * 1000), 0))
                t_seq += el + self.rtt
            seq_times.append(t_seq)
        self.last_elapsed_ms = max(seq_times) if seq_times else 0.0
        return outs


# --------------------------------------------------------------------------
# Cluster Manager
# --------------------------------------------------------------------------

@dataclass
class CMConfig:
    t_sync_s: float = 1.0
    tau: int = 5
    hysteresis_m: float = 0.0
    min_dwell_s: float = 0.0
    min_jaccard: float = 0.25
    directive_timeout_ms: int = 5000
    # 'sequential': net edge changes applied one by one with Algorithm 3/4 (paper semantics,
    #               one Raft reconfiguration per round);  'recompute': local re-partition of the
    #               affected subgraph (previous implementation).
    batch_mode: str = "sequential"
    # Adversarial leader placement used to measure the availability cost envelope:
    #   'none' -> CM behaviour (leaders avoided when shrinking regions)
    #   'one'  -> one leader transfer per reconfiguration round (one affected region)
    #   'all'  -> every affected (evolved) region transfers its leader once per round
    adversarial_leader: str = "none"


class ClusterManager:
    def __init__(self, trace: CoverageTrace, cfg: CMConfig, naming: Naming,
                 executor, clock: Clock, log_path: Optional[str] = None):
        self.trace = trace
        self.cfg = cfg
        self.naming = naming
        self.executor = executor
        self.clock = clock
        self.nodes = list(trace.nodes)
        self.version = 0
        self.next_cluster = 1
        self.cluster_of: Dict[int, int] = {}          # logical region id -> raft cluster id
        self.serving: Dict[int, Set[int]] = {}        # cluster id -> nodes currently serving
        self.leaders: Dict[int, int] = {}             # cluster id -> leader graph node
        self.rounds: List[RoundRecord] = []
        self.directives: List[DirectiveRecord] = []
        self.snapshots: List[Tuple[float, Dict[int, frozenset]]] = []   # serving view timeline
        self.ticks: List[float] = []
        self._log = open(log_path, "w") if log_path else None
        self.mgr: Optional[DynamicRegionManagerOptimized] = None
        self.observer: Optional[HysteresisObserver] = None
        self.stats_bytes_total = 0

    # ------------------------------------------------------------- bootstrap
    def bootstrap(self) -> RoundRecord:
        t = self.clock.now()
        margin = self.trace.margin_matrix(t)
        n = len(self.nodes)
        iu = np.triu_indices(n, k=1)
        edges = {norm_edge(self.nodes[a], self.nodes[b])
                 for a, b, m in zip(iu[0], iu[1], margin[iu]) if m > self.cfg.hysteresis_m}
        self.observer = HysteresisObserver(self.nodes, edges, self.cfg.hysteresis_m, self.cfg.min_dwell_s)
        G = nx.Graph()
        G.add_nodes_from(self.nodes)
        G.add_edges_from(edges)
        regions = PartitionStrategies(G).threshold_based(self.cfg.tau)
        regions = [set(r) for r in regions if len(r) >= 1]
        self.mgr = DynamicRegionManagerOptimized(G, regions, self.cfg.tau)

        rec = RoundRecord(round=0, t_tick=t, n_changes=len(edges), added_edges=sorted(edges),
                          removed_edges=[], n_observed_edges=len(edges), reconfigured=True,
                          regions_before=0, regions_after=len(self.mgr.regions))
        plan = build_plan({}, dict(self.mgr.regions), {}, self.cfg.min_jaccard)
        self._execute_plan(plan, rec)
        self.rounds.append(rec)
        self._emit({"type": "round", **_round_dict(rec)})
        return rec

    # ------------------------------------------------------------ sync round
    def sync_round(self) -> RoundRecord:
        t = self.clock.now()
        self.ticks.append(t)
        rnd = len(self.rounds)
        margin = self.trace.margin_matrix(t)
        observed = self.observer.observe(t, margin)
        current = {norm_edge(u, v) for u, v in self.mgr.graph.edges()}
        t_b0 = time.perf_counter()
        changes = edge_diff(current, observed)
        t_batch_ms = (time.perf_counter() - t_b0) * 1000
        rec = RoundRecord(round=rnd, t_tick=t, n_changes=len(changes),
                          added_edges=[(u, v) for op, u, v in changes if op == '+'],
                          removed_edges=[(u, v) for op, u, v in changes if op == '-'],
                          n_observed_edges=len(observed), reconfigured=False,
                          regions_before=len(self.mgr.regions), regions_after=len(self.mgr.regions),
                          t_batch_ms=t_batch_ms, t_done=t)
        # nodes that left the network in this interval (trace exposes ``absent``; node departure =
        # all incident edges removed with Algorithm 4, then the node itself is removed)
        absent = set(getattr(self.trace, "absent", ()))
        departed = {n for n in absent if self.mgr.graph.has_node(n)}
        # nodes back in the network (node join = the node first, then its edges with Algorithm 3);
        # a returning node without any overlap gets its single-node region back
        returned = {n for n in self.nodes if n not in absent and not self.mgr.graph.has_node(n)}
        if not changes and not departed and not returned:
            self.rounds.append(rec)
            self._emit({"type": "round", **_round_dict(rec)})
            return rec

        old = {rid: set(r) for rid, r in self.mgr.regions.items()}
        t_p0 = time.perf_counter()
        if self.cfg.batch_mode == "recompute":
            new_regions = self.mgr.batch_update(changes)
        else:
            new_regions = self.mgr.batch_update_sequential(changes, node_removals=departed,
                                                           node_additions=returned)
        rec.t_part_ms = (time.perf_counter() - t_p0) * 1000
        new = {rid: set(r) for rid, r in new_regions.items()}
        rec.regions_after = len(new)
        leaders_logical = {rid: self.leaders[self.cluster_of[rid]] for rid in old
                           if rid in self.cluster_of and self.cluster_of[rid] in self.leaders}
        plan = build_plan(old, new, leaders_logical, self.cfg.min_jaccard)
        if self.clock.virtual:
            self.clock.advance((rec.t_batch_ms + rec.t_part_ms) / 1000)
        self._execute_plan(plan, rec)
        if rec.n_directives > 0 and self.cfg.adversarial_leader != "none":
            self._adversarial_transfers(plan, rec)
        rec.reconfigured = rec.n_directives > 0
        v = self.mgr.validate_constraints()
        rec.constraints_ok = bool(v["coverage_satisfied"])
        if rec.reconfigured:
            oc = self.mgr.overcoverage()
            rec.size_sum, rec.fresh_size_sum, rec.size_ratio = oc["size_sum"], oc["fresh_size_sum"], oc["size_ratio"]
        self.rounds.append(rec)
        self._emit({"type": "round", **_round_dict(rec)})
        return rec

    # ---------------------------------------------------- adversarial leaders
    def _adversarial_transfers(self, plan: Plan, rec: RoundRecord):
        """Force leader transfers in the regions touched by this round (availability envelope).

        'one': the largest evolved region transfers its leader once; 'all': every evolved
        region does. New regions are excluded (their bootstrap election is already counted
        on the consistency side), as are regions that lost their leader in the shrink phase
        of this very round (they already paid one transfer)."""
        evolved = [c for c in plan.changes if c.kind == "evolved"]
        if not evolved:
            return
        evolved.sort(key=lambda c: (-len(c.nodes), c.logical_id))
        targets = evolved[:1] if self.cfg.adversarial_leader == "one" else evolved
        t0_wall = time.perf_counter()
        t_sent = self.clock.now()
        elapsed_total = 0.0
        for c in targets:
            cid = self.cluster_of.get(c.logical_id)
            if cid is None:
                continue
            members = sorted(self.serving.get(cid, set()) & c.nodes)
            leader = self.leaders.get(cid)
            if (leader is None or leader not in members) and hasattr(self.executor, "_find_leader"):
                leader = self.executor._find_leader(cid, exclude=-1)      # real mode: ask the members
                if leader is not None:
                    self.leaders[cid] = leader
            if leader is None or leader not in members or len(members) < 2:
                continue
            others = [m for m in members if m != leader]
            if not others:
                continue
            target = others[0]
            ok, err, wait_ms, observed = self.executor.transfer_leader(cid, leader, target)
            if self.clock.virtual:
                el = getattr(self.executor, "last_elapsed_ms", wait_ms)
                self.clock.advance(el / 1000)
                elapsed_total += el
            if ok:
                self.leaders[cid] = target
                rec.leader_transfers += 1
                rec.n_transfer_member_nodes += len(members)
                rec.unavail_node_ms += wait_ms * len(members)
            else:
                rec.failures += 1
            self.directives.append(DirectiveRecord(
                rec.round, "adversarial", "LEADER_TRANSFER", cid, leader, target, t_sent, self.clock.now(),
                ok, err, int(wait_ms * 1000), int(wait_ms * 1000), 0, 0, 0,
                f"adversarial {self.cfg.adversarial_leader}: |R|={len(members)}"))
            self._emit({"type": "directive", **asdict(self.directives[-1])})
        if not self.clock.virtual:
            elapsed_total = (time.perf_counter() - t0_wall) * 1000
        rec.t_transfer_ms = elapsed_total
        rec.t_trans_ms += elapsed_total
        rec.t_done = self.clock.now()
        self.snapshots.append((self.clock.now(), {c: frozenset(s) for c, s in self.serving.items()}))

    # --------------------------------------------------------------- execute
    def _execute_plan(self, plan: Plan, rec: RoundRecord):
        rec.kinds = {}
        for c in plan.changes:
            rec.kinds[c.kind] = rec.kinds.get(c.kind, 0) + 1
        # map new logical ids of evolved regions onto their carrier's cluster
        for c in plan.changes:
            if c.kind == "evolved" and c.carrier_logical_id in self.cluster_of:
                self.cluster_of[c.logical_id] = self.cluster_of[c.carrier_logical_id]
            elif c.kind == "new":
                self.cluster_of[c.logical_id] = self._alloc_cluster()
        self.version += 1
        phase_ms: Dict[str, float] = {}
        t_round0 = self.clock.now()
        self._round_nodes: Set[int] = set()
        for phase in PHASES:
            ops = plan.phases.get(phase, [])
            if not ops:
                phase_ms[phase] = 0.0
                continue
            sequences = self._materialize(phase, ops, plan)
            t0_wall = time.perf_counter()
            t_sent = self.clock.now()
            outcomes = self.executor.run_sequences(self.version, sequences)
            if self.clock.virtual:
                elapsed_ms = self.executor.last_elapsed_ms
                self.clock.advance(elapsed_ms / 1000)
            else:
                elapsed_ms = (time.perf_counter() - t0_wall) * 1000
            phase_ms[phase] = elapsed_ms
            self._apply_outcomes(phase, outcomes, rec, t_sent, self.clock.now())
            self.snapshots.append((self.clock.now(), {c: frozenset(s) for c, s in self.serving.items()}))
        # retire logical ids that no longer exist
        alive = set(self.mgr.regions.keys()) if self.mgr else set()
        for rid in list(self.cluster_of):
            if rid not in alive:
                del self.cluster_of[rid]
        rec.t_cfg_ms = phase_ms["create"]
        rec.t_joint_ms = phase_ms["grow"] + phase_ms["shrink"]
        rec.t_migrate_ms = phase_ms["handoff"]
        rec.t_retire_ms = phase_ms["retire"]
        rec.t_trans_ms = rec.t_batch_ms + rec.t_part_ms + sum(phase_ms.values())
        rec.t_done = self.clock.now()
        rec.n_directives = sum(len(v) for v in plan.phases.values())
        rec.elections = sum(1 for c in plan.changes if c.kind == "new")   # one election per new Raft group
        rec.n_reconf_nodes = len(self._round_nodes)
        _ = t_round0

    def _materialize(self, phase: str, ops: List[PlannedOp], plan: Plan) -> List[List[Directive]]:
        """PlannedOp -> Directive, grouped into per-region sequences."""
        groups: Dict[int, List[Directive]] = {}
        order: List[int] = []
        logical_nodes = {c.logical_id: c.nodes for c in plan.changes}
        for i, op in enumerate(ops):
            key = op.logical_id if phase in ("grow", "shrink") else i   # only grow/shrink are ordered per region
            if key not in groups:
                groups[key] = []
                order.append(key)
            d = self._directive(op, logical_nodes)
            if d is not None:
                groups[key].append(d)
        return [groups[k] for k in order if groups[k]]

    def _directive(self, op: PlannedOp, logical_nodes: Dict[int, Set[int]]) -> Optional[Directive]:
        nm = self.naming
        to = self.cfg.directive_timeout_ms
        if op.op == "CREATE":
            cluster = self.cluster_of[op.logical_id]
            nodes = logical_nodes[op.logical_id]
            members = {nm.raft_id(cluster, m): nm.addr(m) for m in nodes}
            return Directive(START_REGION, cluster, op.node, members=members,
                             self_raft_node_id=nm.raft_id(cluster, op.node), wait_for_leader=True,
                             timeout_ms=to, note=op.note)
        if op.op == "ADD":
            cluster = self.cluster_of[op.logical_id]
            nm.on_join(cluster, op.target)
            return Directive(ADD_MEMBER, cluster, op.node, target_raft_node_id=nm.raft_id(cluster, op.target),
                             target_addr=nm.addr(op.target), timeout_ms=to, as_observer=True, note=op.note)
        if op.op == "PROMOTE":
            cluster = self.cluster_of[op.logical_id]
            return Directive(PROMOTE_MEMBER, cluster, op.node, target_raft_node_id=nm.raft_id(cluster, op.target),
                             target_addr=nm.addr(op.target), timeout_ms=to, note=op.note)
        if op.op == "JOIN":
            cluster = self.cluster_of[op.logical_id]
            return Directive(START_REGION, cluster, op.node, join=True, as_observer=True,
                             self_raft_node_id=nm.raft_id(cluster, op.node), wait_for_leader=True,
                             timeout_ms=to, note=op.note)
        if op.op == "MIGRATE":
            dst = self.cluster_of[op.logical_id]
            src = self.cluster_of.get(op.src_logical_id)
            if src is None or src == dst:
                return None
            return Directive(MIGRATE_STATE, dst, op.node, src_region_id=src, timeout_ms=to, note=op.note)
        if op.op == "REMOVE":
            cluster = self.cluster_of[op.logical_id]
            d = Directive(REMOVE_MEMBER, cluster, op.node, target_raft_node_id=nm.raft_id(cluster, op.target),
                          timeout_ms=to, note=op.note)
            nm.on_leave(cluster, op.target)
            return d
        if op.op == "FENCE":
            cluster = self.cluster_of[op.logical_id]
            return Directive(FENCE_REGION, cluster, op.node, timeout_ms=to, note=op.note)
        if op.op == "STOP":
            cluster = self.cluster_of[op.logical_id]
            return Directive(STOP_REGION, cluster, op.node, timeout_ms=to, note=op.note)
        return None

    def _apply_outcomes(self, phase: str, outcomes: List[DirectiveOutcome], rec: RoundRecord,
                        t_sent: float, t_done: float):
        for o in outcomes:
            d = o.directive
            size = d.size_bytes()
            rec.bytes_cfg += size
            self.stats_bytes_total += size
            if not o.success:
                rec.failures += 1
            if d.op in (ADD_MEMBER, REMOVE_MEMBER, PROMOTE_MEMBER):
                rec.membership_changes += 1
            self._round_nodes.add(d.node)
            if d.target_raft_node_id:
                self._round_nodes.add(Naming.decode(d.target_raft_node_id)[2])
            # availability accounting: removing the leader of a region costs one leader transfer
            # during which no member of the region can commit (leader_wait_us measured node-side)
            if d.op == REMOVE_MEMBER and o.success and o.leader_wait_us > 0:
                members = self.serving.get(d.region_id, set())
                rec.leader_transfers += 1
                rec.n_transfer_member_nodes += len(members)
                rec.unavail_node_ms += o.leader_wait_us / 1000.0 * len(members)
            # maintain serving view
            if o.success:
                if d.op == START_REGION:
                    self.serving.setdefault(d.region_id, set()).add(d.node)
                elif d.op in (STOP_REGION, FENCE_REGION):   # fenced = no longer serving
                    s = self.serving.get(d.region_id)
                    if s is not None:
                        s.discard(d.node)
                        if not s:
                            del self.serving[d.region_id]
                if o.leader_id:
                    _, _, leader_node = Naming.decode(o.leader_id)
                    self.leaders[d.region_id] = leader_node
            target = -1
            if d.target_raft_node_id:
                target = Naming.decode(d.target_raft_node_id)[2]
            dr = DirectiveRecord(rec.round, phase, d.op_name, d.region_id, d.node, target, t_sent, t_done,
                                 o.success, o.error, o.elapsed_us, o.leader_wait_us, o.rpc_elapsed_us,
                                 size, o.migrated_keys, d.note)
            self.directives.append(dr)
            self._emit({"type": "directive", **asdict(dr)})
        for cid in list(self.leaders):
            if cid not in self.serving:
                del self.leaders[cid]

    def _alloc_cluster(self) -> int:
        c = self.next_cluster
        self.next_cluster += 1
        return c

    # ------------------------------------------------------------- helpers
    def serving_regions(self) -> Dict[int, Set[int]]:
        return {c: set(s) for c, s in self.serving.items()}

    def refresh_leaders(self, cluster: ClusterClient):
        """Ask nodes for current leaders (real mode)."""
        for cid, nodes in self.serving.items():
            for n in sorted(nodes):
                try:
                    st = cluster[self.naming.mec_id(n)].get_status(cid)
                    if st.regions and st.regions[0].leader_id:
                        self.leaders[cid] = Naming.decode(st.regions[0].leader_id)[2]
                        break
                except Exception:
                    continue

    def _emit(self, obj: dict):
        if self._log:
            self._log.write(json.dumps(obj, default=_json_default) + "\n")
            self._log.flush()

    def close(self):
        if self._log:
            self._log.close()

    def summary(self) -> dict:
        recon = [r for r in self.rounds[1:] if r.reconfigured]
        waits = [d.leader_wait_us / 1000.0 for d in self.directives
                 if d.op in ("REMOVE_MEMBER", "LEADER_TRANSFER") and d.ok and d.leader_wait_us > 0]
        lw = {"n": len(waits)}
        if waits:
            a = np.asarray(waits)
            lw.update(mean=float(a.mean()), p50=float(np.percentile(a, 50)), p95=float(np.percentile(a, 95)),
                      max=float(a.max()))
        return {
            "leader_wait_ms": lw,
            "rounds": len(self.rounds) - 1,
            "reconfig_rounds": len(recon),
            "elections": sum(r.elections for r in recon),
            "membership_changes": sum(r.membership_changes for r in recon),
            "directives": sum(r.n_directives for r in recon),
            "bytes_cfg": sum(r.bytes_cfg for r in recon),
            "failures": sum(r.failures for r in recon),
            "t_trans_ms_mean": float(np.mean([r.t_trans_ms for r in recon])) if recon else 0.0,
            "t_trans_ms_max": float(np.max([r.t_trans_ms for r in recon])) if recon else 0.0,
            "t_part_ms_mean": float(np.mean([r.t_part_ms for r in recon])) if recon else 0.0,
            "t_cfg_ms_mean": float(np.mean([r.t_cfg_ms for r in recon if r.t_cfg_ms > 0])) if any(r.t_cfg_ms > 0 for r in recon) else 0.0,
            "t_joint_ms_mean": float(np.mean([r.t_joint_ms for r in recon if r.t_joint_ms > 0])) if any(r.t_joint_ms > 0 for r in recon) else 0.0,
            "final_regions": len(self.serving),
            "batch_mode": self.cfg.batch_mode,
            "adversarial_leader": self.cfg.adversarial_leader,
            "leader_transfers": sum(r.leader_transfers for r in recon),
            "unavail_node_ms_total": float(sum(r.unavail_node_ms for r in recon)),
            "overcoverage_size_ratio_final": recon[-1].size_ratio if recon else 1.0,
        }


def _round_dict(rec: RoundRecord) -> dict:
    d = asdict(rec)
    d["added_edges"] = [list(e) for e in rec.added_edges]
    d["removed_edges"] = [list(e) for e in rec.removed_edges]
    return d


def _json_default(o):
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    return str(o)

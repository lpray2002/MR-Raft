"""
Region diff  ->  ordered reconfiguration plan.

``DynamicRegionManager.batch_update`` replaces every *affected* region by a
freshly computed one (new logical ids).  Executing that literally would mean
"stop old Raft groups, start new ones" for every change, which (a) costs one
leader election per region and (b) loses the replicated state.  The planner
therefore performs *carrier matching*: a new region that largely overlaps a
removed one is realised by evolving the removed region's Raft group through
Raft membership changes (state is carried over automatically), and only truly
new regions get a fresh Raft group.  State of dissolved regions is handed off
to every new/evolved region that intersects it (MIGRATE_STATE), executed by a
node that belongs to both.

Phase order inside one synchronization round (why it matters):
  1. CREATE   new Raft groups                    -> new overlap edges get covered ASAP
  2. GROW     evolved groups: ADD_MEMBER(observer) + JOIN + PROMOTE -> idem
  3. HANDOFF  MIGRATE_STATE from dissolved groups (before they are stopped)
  4. SHRINK   evolved groups: REMOVE_MEMBER + STOP on the leaving node
  5. RETIRE   STOP dissolved groups
Additions first / removals last: a lagging removal is only redundant
synchronisation (safe), a lagging addition is a coverage-constraint violation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

NodeSet = Set[int]


@dataclass
class RegionChange:
    kind: str                      # 'new' | 'evolved' | 'removed' | 'kept'
    logical_id: int                # id in the DynamicRegionManager (new side for new/evolved)
    nodes: NodeSet
    carrier_logical_id: Optional[int] = None    # evolved: old logical id whose Raft group is reused
    carrier_nodes: Optional[NodeSet] = None
    added_nodes: NodeSet = field(default_factory=set)
    removed_nodes: NodeSet = field(default_factory=set)
    jaccard: float = 0.0


@dataclass
class PlannedOp:
    op: str            # CREATE | ADD | JOIN | PROMOTE | MIGRATE | FENCE | REMOVE | STOP
    logical_id: int    # region (new-side logical id, or old id for removed regions)
    node: int          # executing MEC node (graph id)
    target: int = -1   # ADD/REMOVE: node being added/removed
    src_logical_id: int = -1   # MIGRATE: source region (old logical id)
    note: str = ""


@dataclass
class Plan:
    changes: List[RegionChange]
    phases: Dict[str, List[PlannedOp]]          # phase name -> ops
    order: Tuple[str, ...] = ("create", "grow", "handoff", "shrink", "retire")

    def n_ops(self) -> int:
        return sum(len(v) for v in self.phases.values())

    def summary(self) -> dict:
        kinds = {}
        for c in self.changes:
            kinds[c.kind] = kinds.get(c.kind, 0) + 1
        return {"regions": kinds, **{k: len(v) for k, v in self.phases.items()},
                "elections": len(self.phases["create"]) and
                len({op.logical_id for op in self.phases["create"]})}


def match_regions(old: Dict[int, NodeSet], new: Dict[int, NodeSet],
                  min_jaccard: float = 0.25) -> List[RegionChange]:
    """Classify regions into kept / evolved(carrier) / new / removed."""
    changes: List[RegionChange] = []
    old_ids = set(old)
    new_ids = set(new)

    # identical ids
    for rid in old_ids & new_ids:
        if old[rid] == new[rid]:
            changes.append(RegionChange("kept", rid, set(new[rid])))
        else:  # same id, different members -> evolve in place
            changes.append(RegionChange(
                "evolved", rid, set(new[rid]), carrier_logical_id=rid,
                carrier_nodes=set(old[rid]), added_nodes=new[rid] - old[rid],
                removed_nodes=old[rid] - new[rid], jaccard=_jaccard(old[rid], new[rid])))

    removed_pool = {rid: old[rid] for rid in old_ids - new_ids}
    added = {rid: new[rid] for rid in new_ids - old_ids}

    # greedy carrier matching by Jaccard similarity
    pairs = []
    for a, aset in added.items():
        for o, oset in removed_pool.items():
            inter = len(aset & oset)
            if inter == 0:
                continue
            pairs.append((_jaccard(aset, oset), inter, a, o))
    pairs.sort(key=lambda p: (-p[0], -p[1], p[2], p[3]))
    used_old: Set[int] = set()
    matched_new: Set[int] = set()
    for jac, inter, a, o in pairs:
        if a in matched_new or o in used_old or jac < min_jaccard:
            continue
        # a carrier must keep at least one node so the Raft group survives
        if inter < 1:
            continue
        used_old.add(o)
        matched_new.add(a)
        changes.append(RegionChange(
            "evolved", a, set(added[a]), carrier_logical_id=o,
            carrier_nodes=set(removed_pool[o]), added_nodes=added[a] - removed_pool[o],
            removed_nodes=removed_pool[o] - added[a], jaccard=jac))
    for a in added:
        if a not in matched_new:
            changes.append(RegionChange("new", a, set(added[a])))
    for o in removed_pool:
        if o not in used_old:
            changes.append(RegionChange("removed", o, set(removed_pool[o])))
    return changes


def build_plan(old: Dict[int, NodeSet], new: Dict[int, NodeSet],
               leaders: Optional[Dict[int, int]] = None,
               min_jaccard: float = 0.25) -> Plan:
    """
    old/new: logical region id -> node set (before/after batch_update).
    leaders: optional {old logical id: leader graph node} used to pick the
             executing node for membership changes.
    """
    leaders = leaders or {}
    changes = match_regions(old, new, min_jaccard)
    phases: Dict[str, List[PlannedOp]] = {k: [] for k in ("create", "grow", "handoff", "shrink", "retire")}

    new_side: Dict[int, NodeSet] = {}      # logical id -> nodes for regions that exist after the round
    for c in changes:
        if c.kind in ("kept", "evolved", "new"):
            new_side[c.logical_id] = c.nodes

    for c in changes:
        if c.kind == "new":
            for n in sorted(c.nodes):
                phases["create"].append(PlannedOp("CREATE", c.logical_id, n, note="new region"))
        elif c.kind == "evolved":
            stay = c.carrier_nodes & c.nodes                 # nodes present before and after
            exec_node = _pick_executor(stay, leaders.get(c.carrier_logical_id))
            for n in sorted(c.added_nodes):
                # observer-first join: ADD(observer) -> JOIN (start, wait for leader) -> PROMOTE (voter)
                phases["grow"].append(PlannedOp("ADD", c.logical_id, exec_node, target=n,
                                                note=f"carrier {c.carrier_logical_id} jac={c.jaccard:.2f}"))
                phases["grow"].append(PlannedOp("JOIN", c.logical_id, n, note="join evolved region"))
                phases["grow"].append(PlannedOp("PROMOTE", c.logical_id, exec_node, target=n,
                                                note="promote observer to voter"))
            for n in sorted(c.removed_nodes):
                # graceful leave: FENCE (stop routing requests, drain) -> REMOVE (leader transfer first
                # if n is the leader) -> STOP. Removing first would strand n's in-flight requests.
                phases["shrink"].append(PlannedOp("FENCE", c.logical_id, n, note="leaving evolved region"))
                phases["shrink"].append(PlannedOp("REMOVE", c.logical_id, exec_node, target=n))
                phases["shrink"].append(PlannedOp("STOP", c.logical_id, n, note="left evolved region"))

    # State handoff. A write at node a goes to every region a serves, and a handover
    # partner reads it through a region it shares with a. If a ends up in a region T whose
    # Raft group was not one of a's regions before the round (a joined an evolving group,
    # T is brand new, or a's old region dissolved), a's earlier writes are not in T yet:
    # copy one of a's old regions into T. Any old region of a holds all of a's writes, so
    # one source suffices; pick the one overlapping T the most (fewest distinct copies).
    # Groups are compared by carrier, so a node that stays inside an evolving group needs
    # nothing. Executed in 'handoff': after 'grow' (a already hosts T) and before
    # 'shrink'/'retire' (a still hosts the source).
    group_new = {c.logical_id: (c.carrier_logical_id if c.kind == "evolved" else c.logical_id)
                 for c in changes if c.kind in ("kept", "evolved", "new")}
    pairs: Set[Tuple[int, int]] = set()       # (src old id, dst new id)
    for dst_id, dst_nodes in new_side.items():
        for a in sorted(dst_nodes):
            old_regs = [oid for oid, ns in old.items() if a in ns]
            if not old_regs or group_new[dst_id] in old_regs:
                continue
            src = max(old_regs, key=lambda oid: (len(old[oid] & dst_nodes), -oid))
            pairs.add((src, dst_id))
    new_nodes_of_group = {c.carrier_logical_id: c.nodes for c in changes if c.kind == "evolved"}
    for src_id, dst_id in sorted(pairs):
        common = old[src_id] & new_side[dst_id]     # hosts both source (until shrink) and destination
        if not common:
            continue
        # prefer a node that stays in the source group (source not fenced on it during handoff)
        common_stay = common & new_nodes_of_group.get(src_id, set())
        phases["handoff"].append(PlannedOp("MIGRATE", dst_id, min(common_stay or common), src_logical_id=src_id,
                                           note=f"handoff {src_id}->{dst_id}"))
    for c in changes:
        if c.kind == "removed":
            for n in sorted(c.nodes):
                phases["retire"].append(PlannedOp("STOP", c.logical_id, n, note="region dissolved"))
    return Plan(changes=changes, phases=phases)


def _jaccard(a: NodeSet, b: NodeSet) -> float:
    u = len(a | b)
    return len(a & b) / u if u else 0.0


def _pick_executor(stay: NodeSet, leader: Optional[int]) -> int:
    if leader is not None and leader in stay:
        return leader
    return min(stay) if stay else -1

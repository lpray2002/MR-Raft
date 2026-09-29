"""
Metrics for the three questions raised by the reviewers:

* update responsiveness   -> lag between a ground-truth edge addition and the
                             moment a *serving* region covers both endpoints
* reconfiguration overhead -> reconfiguration rounds, directives (M_CM), bytes
                             (B_CM), elections, membership changes, T_trans
* consistency maintenance -> fraction of time / edges for which the coverage
                             constraint is violated, and UAV read outcomes
                             (attributed to coverage windows)
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

import numpy as np

from .dynamics import CoverageTrace, EdgeEvent, norm_edge

Snapshot = Tuple[float, Dict[int, FrozenSet[int]]]


class ServingTimeline:
    """Serving-region view as a function of time (from CM snapshots)."""

    def __init__(self, snapshots: Sequence[Snapshot]):
        self.times = [s[0] for s in snapshots]
        self.views = [s[1] for s in snapshots]

    def at(self, t: float) -> Dict[int, FrozenSet[int]]:
        k = bisect.bisect_right(self.times, t) - 1
        return self.views[k] if k >= 0 else {}

    def covered(self, t: float, u: int, v: int) -> bool:
        return any(u in m and v in m for m in self.at(t).values())

    def first_cover_time(self, t_from: float, u: int, v: int) -> Optional[float]:
        k = bisect.bisect_right(self.times, t_from) - 1
        k = max(k, 0)
        for i in range(k, len(self.times)):
            if any(u in m and v in m for m in self.views[i].values()):
                return max(self.times[i], t_from) if i == k else self.times[i]
        return None


@dataclass
class LagRecord:
    t_event: float
    edge: Tuple[int, int]
    lag_s: Optional[float]      # None => never covered before the edge disappeared again
    transient: bool             # edge disappeared again before being covered
    lifetime_s: Optional[float] # time until the edge disappeared (None = still present at end)


def coverage_lags(trace: CoverageTrace, timeline: ServingTimeline, t_start: float = 0.0) -> List[LagRecord]:
    events = [e for e in trace.true_edge_events() if e.t >= t_start]
    out: List[LagRecord] = []
    # index removals per edge
    removals: Dict[Tuple[int, int], List[float]] = {}
    for e in events:
        if e.op == '-':
            removals.setdefault(e.edge, []).append(e.t)
    for e in events:
        if e.op != '+':
            continue
        u, v = e.edge
        rem = [t for t in removals.get(e.edge, []) if t > e.t]
        t_gone = min(rem) if rem else None
        t_cov = timeline.first_cover_time(e.t, u, v)
        if t_cov is not None and (t_gone is None or t_cov <= t_gone):
            out.append(LagRecord(e.t, e.edge, t_cov - e.t, False, (t_gone - e.t) if t_gone else None))
        else:
            out.append(LagRecord(e.t, e.edge, None, True, (t_gone - e.t) if t_gone else None))
    return out


def violation_timeseries(trace: CoverageTrace, timeline: ServingTimeline,
                         t_start: float, t_end: float, dt: float = 0.1):
    ts = np.arange(t_start, t_end, dt)
    frac = np.zeros(len(ts))
    n_unc = np.zeros(len(ts), dtype=int)
    n_edges = np.zeros(len(ts), dtype=int)
    for k, t in enumerate(ts):
        E = trace.true_edges_at(t)
        view = timeline.at(t)
        unc = 0
        for (u, v) in E:
            if not any(u in m and v in m for m in view.values()):
                unc += 1
        n_edges[k] = len(E)
        n_unc[k] = unc
        frac[k] = unc / len(E) if E else 0.0
    return ts, frac, n_unc, n_edges


def overhead_summary(rounds, duration_s: float) -> dict:
    recon = [r for r in rounds if r.round > 0 and r.reconfigured]
    ticks = [r for r in rounds if r.round > 0]
    minutes = max(duration_s / 60.0, 1e-9)
    tt = [r.t_trans_ms for r in recon]
    return {
        "ticks": len(ticks),
        "ticks_with_changes": sum(1 for r in ticks if r.n_changes > 0),
        "reconfig_rounds": len(recon),
        "reconfig_per_min": len(recon) / minutes,
        "directives": sum(r.n_directives for r in recon),
        "directives_per_min": sum(r.n_directives for r in recon) / minutes,
        "bytes_cfg": sum(r.bytes_cfg for r in recon),
        "elections": sum(r.elections for r in recon),
        "membership_changes": sum(r.membership_changes for r in recon),
        "failures": sum(r.failures for r in recon),
        "edge_changes_processed": sum(r.n_changes for r in ticks),
        "t_trans_ms": _stats(tt),
        "t_part_ms": _stats([r.t_part_ms for r in recon]),
        "t_cfg_ms": _stats([r.t_cfg_ms for r in recon if r.t_cfg_ms > 0]),
        "t_joint_ms": _stats([r.t_joint_ms for r in recon if r.t_joint_ms > 0]),
        "t_migrate_ms": _stats([r.t_migrate_ms for r in recon if r.t_migrate_ms > 0]),
    }


def responsiveness_summary(lags: List[LagRecord]) -> dict:
    covered = [l.lag_s for l in lags if l.lag_s is not None]
    # additions that were *not* already covered by an existing region when they appeared
    uncovered_at_start = [l for l in covered if l > 1e-9]
    return {
        "edge_additions": len(lags),
        "covered": len(covered),
        "already_covered_at_appearance": sum(1 for l in covered if l <= 1e-9),
        "transient_uncovered": sum(1 for l in lags if l.transient),
        "lag_s": _stats(covered),
        "lag_uncovered_s": _stats(uncovered_at_start),
    }


def consistency_summary(ts, frac, n_unc, n_edges, uav_records, timeline: ServingTimeline,
                        trace: CoverageTrace) -> dict:
    viol_time_frac = float(np.mean(frac)) if len(frac) else 0.0
    edge_seconds = float(np.sum(n_unc) * (ts[1] - ts[0])) if len(ts) > 1 else 0.0
    recs = [r for r in uav_records if r.read_outcome not in ("none", "no_coverage")]
    by: Dict[str, int] = {}
    for r in recs:
        by[r.read_outcome] = by.get(r.read_outcome, 0) + 1
    ho = [r for r in recs if r.handover]
    # Overlap handovers are the case the coverage constraint is about: the previous and the
    # new serving node overlap (edge in the truth graph) at handover time. A UAV that flies
    # out of all coverage and re-enters at a non-overlapping node ("gap re-entry") is outside
    # the guarantee: no region is required to span two nodes that do not overlap.
    ho_overlap = [r for r in ho if norm_edge(r.prev_serving, r.serving) in trace.true_edges_at(r.t)]
    ho_gap = [r for r in ho if norm_edge(r.prev_serving, r.serving) not in trace.true_edges_at(r.t)]
    # "inconsistent" = a value came back but it was not the UAV's latest write (stale/missing);
    # "error" = the read failed (timeout / partial read) -> availability, not consistency
    is_incons = lambda r: r.read_outcome in ("stale", "missing")
    is_err = lambda r: r.read_outcome == "error"
    ho_bad = [r for r in ho if r.read_outcome != "consistent"]
    ho_overlap_bad = [r for r in ho_overlap if is_incons(r)]
    ho_overlap_err = [r for r in ho_overlap if is_err(r)]
    ho_gap_bad = [r for r in ho_gap if is_incons(r)]
    attributed = 0
    for r in ho_overlap_bad:
        e = norm_edge(r.prev_serving, r.serving)
        if not timeline.covered(r.t, *e):
            attributed += 1
    n_ok = by.get("consistent", 0)
    n_err = by.get("error", 0)
    return {
        "violation_time_fraction": viol_time_frac,
        "uncovered_edge_seconds": edge_seconds,
        "max_uncovered_edges": int(np.max(n_unc)) if len(n_unc) else 0,
        "reads": len(recs), "outcomes": by,
        "consistent_rate": n_ok / len(recs) if recs else 0.0,
        "read_error_rate": n_err / len(recs) if recs else 0.0,
        # consistency among reads that returned a value
        "consistent_rate_of_answered": n_ok / (len(recs) - n_err) if len(recs) - n_err > 0 else 0.0,
        "handovers": len(ho), "handover_inconsistent": len(ho_bad),
        "handover_inconsistent_rate": len(ho_bad) / len(ho) if ho else 0.0,
        # split: overlap handovers (in scope) vs. coverage-gap re-entries (out of scope)
        "overlap_handovers": len(ho_overlap),
        "overlap_handover_inconsistent": len(ho_overlap_bad),
        "overlap_handover_inconsistent_rate": len(ho_overlap_bad) / len(ho_overlap) if ho_overlap else 0.0,
        "overlap_handover_errors": len(ho_overlap_err),
        "gap_reentries": len(ho_gap),
        "gap_reentry_inconsistent": len(ho_gap_bad),
        "handover_inconsistent_in_uncovered_window": attributed,
    }


A_TARGETS = (0.999, 0.9999, 0.99999)


def availability_summary(rounds, n_nodes: int, duration_s: float) -> dict:
    """Availability cost of reconfiguration from the CM's own accounting.

    c_round = (1/N) * sum_j unavail_j  [ms]  -- node-normalised write-unavailability of one
    reconfiguration round (leader-transfer wait x members of the region, per transfer).
    Under saturated churn (one reconfiguration per T_sync) the interval needed for target
    availability A is  T_int >= c / (1 - A).
    """
    recon = [r for r in rounds if r.round > 0 and r.reconfigured]
    c_ms = [r.unavail_node_ms / max(n_nodes, 1) for r in recon]
    total_unavail_ms = sum(r.unavail_node_ms for r in recon)
    U = total_unavail_ms / (n_nodes * duration_s * 1000.0) if duration_s > 0 else 0.0
    st = _stats(c_ms)
    out = {
        "n_nodes": n_nodes,
        "reconfig_rounds": len(recon),
        "leader_transfers": sum(r.leader_transfers for r in recon),
        "leader_transfer_rounds": sum(1 for r in recon if r.leader_transfers > 0),
        "unavail_node_ms_total": total_unavail_ms,
        "unavailability_model": U,
        "availability_model": 1.0 - U,
        "c_round_ms": st,
        "scope_reconf_nodes": _stats([r.n_reconf_nodes for r in recon]),
        "scope_transfer_members": _stats([r.n_transfer_member_nodes for r in recon]),
        "scope_transfer_members_frac": _stats([r.n_transfer_member_nodes / max(n_nodes, 1) for r in recon]),
        "overcoverage_size_ratio": _stats([r.size_ratio for r in recon]),
        "overcoverage_size_ratio_final": recon[-1].size_ratio if recon else 1.0,
    }
    for q in ("mean", "p95", "max"):
        if q in st:
            out[f"t_int_required_s_{q}"] = {f"{A:g}": (st[q] / 1000.0) / (1.0 - A) for A in A_TARGETS}
    return out


# ---------------------------------------------------------------------------
# Node-side unavailability from the Go event logs (real mode)
# ---------------------------------------------------------------------------

def _merge_intervals(iv: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    iv = sorted((a, b) for a, b in iv if b > a)
    out: List[Tuple[float, float]] = []
    for a, b in iv:
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _intersect(a: List[Tuple[float, float]], b: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    out = []
    i = j = 0
    while i < len(a) and j < len(b):
        lo = max(a[i][0], b[j][0]); hi = min(a[i][1], b[j][1])
        if hi > lo:
            out.append((lo, hi))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def node_unavailability_from_logs(logs_dir: str, epoch0_s: float, t_end_s: float,
                                  rounds=None, nodes: Optional[Sequence[int]] = None) -> dict:
    """Reconstruct per-node write-unavailability intervals from ``events_<mec>.jsonl``.

    A node cannot commit a write while any region in its *serving table* has no known
    leader (bootstrap election, leader transfer, leader failure). Serving intervals come
    from START_REGION (registered at ts+elapsed, i.e. after wait_for_leader) / FENCE / STOP
    directive events; leader knowledge from LEADER_UPDATED events (leader_id == 0 -> none).
    Times are CM-clock seconds (epoch - epoch0).
    """
    import glob
    import json
    import os
    files = sorted(glob.glob(os.path.join(logs_dir, "events_*.jsonl")))
    per_node: Dict[int, List[Tuple[float, float]]] = {}
    per_node_regions: Dict[int, Dict[int, List[Tuple[float, float]]]] = {}
    n_leader_events = 0
    for path in files:
        try:
            mid = int(os.path.basename(path).split("_")[1].split(".")[0])
        except Exception:
            continue
        serving: Dict[int, List[Tuple[float, float]]] = {}
        open_since: Dict[int, float] = {}
        leader_ev: Dict[int, List[Tuple[float, int]]] = {}
        with open(path) as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                rid = ev.get("region", 0)
                t = ev["ts_ms"] / 1000.0 - epoch0_s
                op = ev.get("op")
                if op == "LEADER_UPDATED":
                    tu = ev.get("ts_us")
                    if tu:
                        t = tu / 1e6 - epoch0_s
                    leader_ev.setdefault(rid, []).append((t, int(ev.get("leader_id", 0) or 0)))
                    n_leader_events += 1
                elif op == "START_REGION" and ev.get("ok", False):
                    t_reg = t + ev.get("elapsed_us", 0) / 1e6
                    if rid not in open_since:
                        open_since[rid] = t_reg
                elif op in ("FENCE_REGION", "STOP_REGION"):
                    if rid in open_since:
                        serving.setdefault(rid, []).append((open_since.pop(rid), t))
        for rid, t0 in open_since.items():
            serving.setdefault(rid, []).append((t0, t_end_s))
        unavail: List[Tuple[float, float]] = []
        per_region: Dict[int, List[Tuple[float, float]]] = {}
        for rid, sv in serving.items():
            evs = sorted(leader_ev.get(rid, []))
            leaderless: List[Tuple[float, float]] = []
            # before the first leader event the leader is unknown
            state_t = -1e9; known = False
            for t, lid in evs:
                if not known:
                    leaderless.append((state_t, t))
                known = lid != 0
                state_t = t
            if not known:
                leaderless.append((state_t, t_end_s))
            iv = _intersect(_merge_intervals(sv), _merge_intervals(leaderless))
            per_region[rid] = iv
            unavail.extend(iv)
        graph_node = mid - 1                      # Naming: mec id = graph id + 1
        per_node[graph_node] = _merge_intervals(unavail)
        per_node_regions[graph_node] = per_region
    N = len(nodes) if nodes is not None else len(per_node)
    total = sum(b - a for iv in per_node.values() for a, b in iv)
    U = total / (N * t_end_s) if N and t_end_s > 0 else 0.0
    out = {
        "n_logs": len(files), "leader_events": n_leader_events,
        "unavail_node_seconds_total": total,
        "unavailability_measured": U, "availability_measured": 1.0 - U,
        "per_node_unavail_s": {int(n): sum(b - a for a, b in iv) for n, iv in per_node.items()},
        "intervals": {int(n): [(round(a, 4), round(b, 4)) for a, b in iv] for n, iv in per_node.items()},
    }
    if rounds is not None:
        # attribute node-unavailability to reconfiguration rounds: window [t_tick, t_done + 0.5 s]
        c_meas = []
        for r in rounds:
            if r.round == 0 or not r.reconfigured:
                continue
            w = [(r.t_tick, r.t_done + 0.5)]
            s = 0.0
            for iv in per_node.values():
                s += sum(b - a for a, b in _intersect(iv, w))
            c_meas.append(s * 1000.0 / max(N, 1))
        out["c_round_measured_ms"] = _stats(c_meas)
        out["c_round_measured_list_ms"] = c_meas
        # unavailability outside reconfiguration windows (leader failures, spurious elections)
        win = _merge_intervals([(r.t_tick, r.t_done + 0.5) for r in rounds if r.round > 0 and r.reconfigured])
        outside = 0.0
        for iv in per_node.values():
            inside = sum(b - a for a, b in _intersect(iv, win))
            outside += sum(b - a for a, b in iv) - inside
        out["unavail_outside_reconf_s"] = outside
        for q in ("mean", "p95", "max"):
            if q in out["c_round_measured_ms"]:
                out[f"t_int_required_s_{q}"] = {f"{A:g}": (out["c_round_measured_ms"][q] / 1000.0) / (1.0 - A)
                                                for A in A_TARGETS}
    return out


def per_second_read_rate(uav_records, t_end: float):
    n = int(np.ceil(t_end)) + 1
    tot = np.zeros(n)
    bad = np.zeros(n)
    for r in uav_records:
        if r.read_outcome in ("none", "no_coverage"):
            continue
        k = min(int(r.t), n - 1)
        tot[k] += 1
        if r.read_outcome != "consistent":
            bad[k] += 1
    with np.errstate(invalid="ignore", divide="ignore"):
        rate = np.where(tot > 0, 1 - bad / np.maximum(tot, 1), np.nan)
    return np.arange(n), rate, tot, bad


def _stats(xs: Sequence[float]) -> dict:
    if not xs:
        return {"n": 0}
    a = np.asarray(xs, dtype=float)
    return {"n": int(len(a)), "mean": float(a.mean()), "p50": float(np.percentile(a, 50)),
            "p95": float(np.percentile(a, 95)), "max": float(a.max()), "min": float(a.min())}

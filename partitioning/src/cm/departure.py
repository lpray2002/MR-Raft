"""
Node-departure study:  c(rho)  ->  T_sync^min(rho) = c / (1 - A).

One controlled perturbation per cycle instead of random churn. The CM performs at most one
reconfiguration per T_sync (one net diff per round), so U <= c / T_sync for any change rate,
where c is the node-normalised write-unavailability of one reconfiguration. The only quantity
that has to be measured is therefore c(rho); T_sync is derived, churn is not generated.

The only operation of the CM pipeline that costs write availability is the removal of a
region's *leader* (leader transfer, T_U per member); joins are observer-first and new regions
are not served before they have a leader. A single edge removal shrinks a region only when the
node loses its last intra-region link (Algorithm 4), so the smallest perturbation that shrinks
regions is a node departure: all incident edges disappear within one sync round.

Per overlap ratio rho, every cycle runs on a fresh local Go cluster:

  1. scale the coverage radii of the base topology to rho (locations fixed), tau* by composite
     score, threshold partition, CM bootstrap (deploy the Raft groups);
  2. hub node u* = argmax_u sum_{R ∋ u} |R| of the deployed partition;
  3. worst-case leader placement: transfer the leadership of every region containing u* to u*;
  4. u* departs gracefully: its coverage collapses -> every incident overlap margin is negative
     -> Algorithm 4 prunes u* from all its regions -> planner: FENCE u* / REMOVE u* (leader
     transfer to a staying member inside the Go node) / STOP u*;
  5. c = (1/N) sum_j unavail_j of that round from the node-side LEADER_UPDATED logs (real mode)
     or from the timing model (sim mode).

Fresh clusters keep every cycle on the identical partition (re-adding u* through Algorithm 3
does not restore the threshold partition exactly at high rho).
"""
from __future__ import annotations

import collections
import json
import os
import time
from typing import Dict, List, Optional, Set, Tuple

import numpy as np

from .client import ClusterClient
from .cm import ClusterManager, Clock, CMConfig, ModelExecutor, Naming, RealExecutor
from .dynamics import CoverageTrace, DynamicsConfig
from .metrics import _intersect, _merge_intervals, _stats, node_unavailability_from_logs
from .topology import Topology, overlap_ratio, scale_to_rho


# ---------------------------------------------------------------------------
# Static coverage with absent nodes (the CM's view of the topology)
# ---------------------------------------------------------------------------

class StaticCoverage:
    """Subset of the CoverageTrace interface used by the CM (``nodes``, ``margin_matrix``) for a
    static topology in which a set of *absent* nodes has collapsed coverage: every overlap
    margin incident to an absent node is -inf, so all its edges disappear in one sync round."""

    def __init__(self, topo: Topology):
        self._base = CoverageTrace(topo, DynamicsConfig(duration_s=1.0, dt_s=1.0, step_rate_per_min=0.0,
                                                       jitter_sigma=0.0, seed=0))
        self.topology = topo
        self.nodes: List[int] = self._base.nodes
        self.absent: Set[int] = set()
        self._M0 = self._base.margin_matrix(0.0)

    def margin_matrix(self, t: float) -> np.ndarray:
        M = self._M0.copy()
        for u in self.absent:
            k = self._base.idx[u]
            M[k, :] = -np.inf
            M[:, k] = -np.inf
        return M

    def summary(self) -> dict:
        return {"nodes": len(self.nodes), "absent": sorted(self.absent)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def pick_hub(regions: Dict[int, Set[int]]) -> Tuple[int, List[int]]:
    """Node whose departure touches the most node-instances: argmax_u sum_{R ∋ u} |R|
    (regions of size >= 2; ties -> smallest id). Returns (node, sizes of its regions)."""
    score: Dict[int, int] = {}
    for r in regions.values():
        if len(r) < 2:
            continue
        for u in r:
            score[u] = score.get(u, 0) + len(r)
    if not score:
        raise RuntimeError("no region of size >= 2 in the deployed partition")
    u = min(score, key=lambda x: (-score[x], x))
    return u, sorted(len(r) for r in regions.values() if u in r and len(r) >= 2)


def _leader_of(cm: ClusterManager, executor, cid: int) -> Optional[int]:
    if hasattr(executor, "_find_leader"):            # real mode: ask the members
        lead = executor._find_leader(cid, exclude=-1)
        if lead is not None:
            return lead
    if hasattr(executor, "leaders"):                 # sim mode: model state
        lead = executor.leaders.get(cid)
        if lead is not None:
            return lead
    return cm.leaders.get(cid)


def place_leader_on(cm: ClusterManager, executor, u: int, timeout_s: float = 3.0,
                    retry_s: float = 0.3) -> Dict[int, dict]:
    """Worst-case placement: make ``u`` the leader of every region it serves.

    Raft aborts a leadership transfer when the target's log is not up to date within an
    election timeout (e.g. a member that has just re-joined under write load), so the request
    is re-issued every ``retry_s`` until ``u`` is observed as leader or ``timeout_s`` expires."""
    out: Dict[int, dict] = {}
    for cid, members in sorted(cm.serving.items()):
        if u not in members or len(members) < 2:
            continue
        leader = _leader_of(cm, executor, cid)
        if leader == u:
            cm.leaders[cid] = u
            out[cid] = {"ok": True, "already": True, "size": len(members)}
            continue
        if leader is None:
            out[cid] = {"ok": False, "err": "leader unknown", "size": len(members)}
            continue
        ok, err, wait_ms, _ = executor.transfer_leader(cid, leader, u)
        attempts = 1
        seen = _leader_of(cm, executor, cid)
        if not cm.clock.virtual:
            t0 = time.time()
            t_retry = t0 + retry_s
            while seen != u and time.time() - t0 < timeout_s:
                time.sleep(0.02)
                seen = _leader_of(cm, executor, cid)
                if seen != u and time.time() >= t_retry:
                    cur = seen if seen is not None else leader
                    if cur is not None and cur != u:
                        ok2, err2, w2, _ = executor.transfer_leader(cid, cur, u)
                        ok, err = ok or ok2, err2 or err
                        wait_ms += w2
                    attempts += 1
                    t_retry = time.time() + retry_s
        if seen == u:
            cm.leaders[cid] = u
        out[cid] = {"ok": bool(ok and seen == u), "err": err, "wait_ms": wait_ms, "from": leader,
                    "size": len(members), "attempts": attempts}
    return out


def _sleep(clock: Clock, s: float):
    if clock.virtual:
        clock.advance(s)
    else:
        time.sleep(s)


def _jd(o):
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    return str(o)


# ---------------------------------------------------------------------------
# One departure cycle (fresh cluster)
# ---------------------------------------------------------------------------

def run_cycle(args, topo: Topology, tau: int, sub: str, mode: str, hub_override: Optional[int] = None) -> dict:
    os.makedirs(sub, exist_ok=True)
    n_nodes = len(topo.nodes)
    trace = StaticCoverage(topo)
    cfg = CMConfig(t_sync_s=args.tsync, tau=tau, hysteresis_m=0.0, min_dwell_s=0.0,
                   batch_mode=args.batch_mode, adversarial_leader="none")
    cluster = None
    cc = None
    try:
        if mode == "real":
            from .runner import LocalCluster
            cluster = LocalCluster(args.go_bin, os.path.join(sub, "data"), topo.nodes, args)
            cluster.start()
            cc = ClusterClient(cluster.node_cfgs)
            if not cc.wait_all_ready(60):
                raise RuntimeError("Go nodes did not become ready")
            naming = Naming(topo.nodes, cluster.raft_addr)
            clock = Clock(virtual=False)
            executor = RealExecutor(cc, naming)
        else:
            naming = Naming(topo.nodes, lambda n: f"127.0.0.1:{30001 + n}")
            clock = Clock(virtual=True)
            executor = ModelExecutor(naming, rtt_ms=args.model_rtt_ms,
                                     elect_ms=(args.model_elect_min_ms, args.model_elect_max_ms),
                                     commit_ms=args.model_commit_ms, start_ms=args.model_start_ms,
                                     seed=args.dyn_seed, transfer_ms=args.model_transfer_ms)
        cm = ClusterManager(trace, cfg, naming, executor, clock, log_path=os.path.join(sub, "cm_events.jsonl"))
        if mode == "real":
            executor.members_lookup = lambda cid: cm.serving.get(cid, set())

        # 1. deploy
        rec0 = cm.bootstrap()
        if mode == "real":
            cm.refresh_leaders(cc)
        regions0 = {int(rid): sorted(r) for rid, r in cm.mgr.regions.items()}

        # 2. hub
        if hub_override is None:
            u, sizes = pick_hub(cm.mgr.regions)
        else:
            u = hub_override
            sizes = sorted(len(r) for r in cm.mgr.regions.values() if u in r and len(r) >= 2)

        # 3. worst-case leader placement, then settle so that the placement transfers lie well
        #    outside the measurement window of the departure round
        t_place0 = clock.now()
        placement = place_leader_on(cm, executor, u)
        t_place1 = clock.now()
        _sleep(clock, args.settle_s)

        # 4. graceful departure of u: one sync round with all its edges gone
        trace.absent = {u}
        rec = cm.sync_round()
        _sleep(clock, args.settle_s)
        t_end = clock.now()
        cm.close()

        res = {
            "cycle": os.path.basename(sub), "mode": mode, "n_nodes": n_nodes, "tau": tau,
            "hub": int(u), "hub_regions": sizes, "regions_before": regions0,
            "regions_after": {int(rid): sorted(r) for rid, r in cm.mgr.regions.items()},
            "placement": {int(k): v for k, v in placement.items()},
            "placement_ok": bool(placement) and all(v.get("ok") for v in placement.values()),
            "bootstrap_t_cfg_ms": rec0.t_cfg_ms,
            "reconfigured": rec.reconfigured, "kinds": rec.kinds, "n_directives": rec.n_directives,
            "failures": rec.failures, "constraints_ok": rec.constraints_ok,
            "leader_transfers": rec.leader_transfers, "n_transfer_member_nodes": rec.n_transfer_member_nodes,
            "leader_wait_ms": [d.leader_wait_us / 1000.0 for d in cm.directives
                               if d.round == rec.round and d.leader_wait_us > 0],
            "t_part_ms": rec.t_part_ms, "t_joint_ms": rec.t_joint_ms, "t_trans_ms": rec.t_trans_ms,
            "t_tick": rec.t_tick, "t_done": rec.t_done,
            "c_model_ms": rec.unavail_node_ms / n_nodes,
            "c_meas_ms": None,
        }
        # 5. node-side measurement (real mode)
        if mode == "real":
            meas = node_unavailability_from_logs(os.path.join(sub, "data", "logs"), clock.epoch0, t_end,
                                                 rounds=cm.rounds, nodes=topo.nodes)
            lst = meas.get("c_round_measured_list_ms", [])
            res["c_meas_ms"] = float(lst[0]) if lst else None
            res["leader_events"] = meas.get("leader_events")
            w = [(rec.t_tick, rec.t_done + 0.5)]
            intervals = {int(n): [tuple(x) for x in iv] for n, iv in meas.get("intervals", {}).items()}
            res["unavail_in_window_ms"] = {n: 1000.0 * sum(b - a for a, b in _intersect(iv, w))
                                           for n, iv in intervals.items()}
            # leaderless time outside both the placement transfers and the departure round:
            # anything here is environment noise (spurious elections), not reconfiguration cost
            known = _merge_intervals([(rec0.t_tick, rec0.t_done + 0.5), (t_place0 - 0.05, t_place1 + 0.5),
                                      (rec.t_tick, rec.t_done + 0.5)])
            res["unavail_outside_departure_s"] = meas.get("unavail_outside_reconf_s")   # incl. placement
            res["unavail_outside_all_s"] = sum(sum(b - a for a, b in iv) - sum(b - a for a, b in _intersect(iv, known))
                                               for iv in intervals.values())
            res["placement_window"] = [t_place0, t_place1]
        res["c_ms"] = res["c_meas_ms"] if (mode == "real" and res["c_meas_ms"] is not None) else res["c_model_ms"]
        if res["n_transfer_member_nodes"] > 0 and res["c_ms"] is not None:
            res["t_u_ms"] = res["c_ms"] * n_nodes / res["n_transfer_member_nodes"]
        return res
    finally:
        if cc is not None:
            cc.close()
        if cluster is not None:
            cluster.stop()


# ---------------------------------------------------------------------------
# Aggregation over cycles and the rho loop
# ---------------------------------------------------------------------------

def aggregate(cycles: List[dict], rho: float, rho_ach: float, tau: int, scale: float, A: float, n_nodes: int) -> dict:
    valid = [c for c in cycles if "error" not in c and c.get("c_ms") is not None]
    worst = [c for c in valid if c.get("placement_ok")]        # leader really was on the departing node
    cs = [c["c_ms"] for c in worst]
    st = _stats(cs)
    hubs = collections.Counter(c["hub"] for c in valid)
    return {
        "rho": rho, "rho_achieved": rho_ach, "tau": tau, "scale": scale, "n_nodes": n_nodes, "availability": A,
        "n_cycles": len(cycles), "n_valid": len(valid), "n_worst_case": len(worst),
        "hub": hubs.most_common(1)[0][0] if hubs else None,
        "hub_regions": worst[0]["hub_regions"] if worst else (valid[0]["hub_regions"] if valid else []),
        "c_ms": st,
        "c_model_ms": _stats([c["c_model_ms"] for c in worst]),
        "t_sync_min_s": {q: st[q] / 1000.0 / (1.0 - A) for q in ("mean", "p95", "max") if q in st},
        "leader_transfers": _stats([c["leader_transfers"] for c in worst]),
        "t_u_ms": _stats([c["t_u_ms"] for c in worst if c.get("t_u_ms") is not None]),
        "leader_wait_ms": _stats([w for c in worst for w in c["leader_wait_ms"]]),
        "t_trans_ms": _stats([c["t_trans_ms"] for c in worst]),
        "placement_failed": sum(1 for c in valid if not c.get("placement_ok")),
        "cycles_failed": sum(1 for c in cycles if "error" in c or c.get("failures", 0) > 0),
        "cycles_outside_unavail": sum(1 for c in worst if (c.get("unavail_outside_all_s") or 0) > 1e-4),
    }


def run_node_departure(args, base_topo: Topology, out_dir: str) -> dict:
    from .runner import resolve_tau
    from . import visualize as viz
    A = args.availability
    rhos: List[Optional[float]] = [float(x) for x in args.rho_sweep.split(",")] if args.rho_sweep else [None]
    results_path = os.path.join(out_dir, "departure_results.json")
    # keep the entries of rhos not in this run (re-running one column updates only that entry)
    by_rho: Dict[float, dict] = {}
    if os.path.exists(results_path):
        with open(results_path) as f:
            for r in json.load(f):
                by_rho[round(r["rho"], 4)] = r
    results: List[dict] = []
    for rho in rhos:
        if rho is None:
            topo_r, scale = base_topo, 1.0
            rho_t = rho_ach = overlap_ratio(base_topo.graph())
        else:
            topo_r, scale, rho_ach = scale_to_rho(base_topo, rho, args.rho_margin)
            rho_t = rho
        tau = resolve_tau(args, topo_r)
        tag = f"rho{rho_t:.2f}"
        print(f"[departure] === {tag} (achieved {rho_ach:.3f}, scale x{scale:.3f}) tau*={tau} "
              f"mode={args.mode} cycles={args.cycles} ===")
        cycles: List[dict] = []
        for k in range(args.cycles):
            sub = os.path.join(out_dir, "cycles", f"{tag}_c{k:02d}")
            done = os.path.join(sub, "cycle.json")
            if args.resume and os.path.exists(done):
                with open(done) as f:
                    cycles.append(json.load(f))
                continue
            try:
                res = run_cycle(args, topo_r, tau, sub, args.mode, args.departure_node)
            except Exception as e:  # keep the sweep alive; the cycle is reported as failed
                res = {"cycle": os.path.basename(sub), "error": repr(e)}
                print(f"[departure] {tag} cycle {k}: FAILED {e!r}")
            os.makedirs(sub, exist_ok=True)
            with open(done, "w") as f:
                json.dump(res, f, indent=2, default=_jd)
            cycles.append(res)
            if "error" not in res:
                print(f"[departure] {tag} cycle {k}: hub={res['hub']} regions={res['hub_regions']} "
                      f"placement_ok={res['placement_ok']} kinds={res['kinds']} transfers={res['leader_transfers']} "
                      f"members={res['n_transfer_member_nodes']} leader_wait={[round(w, 2) for w in res['leader_wait_ms']]} ms "
                      f"c_meas={res['c_meas_ms'] if res['c_meas_ms'] is None else round(res['c_meas_ms'], 3)} ms "
                      f"c_model={res['c_model_ms']:.3f} ms T_trans={res['t_trans_ms']:.0f} ms fail={res['failures']} "
                      f"noise_outside={res.get('unavail_outside_all_s', 0) or 0:.4f} s")
        agg = aggregate(cycles, rho_t, rho_ach, tau, scale, A, len(topo_r.nodes))
        by_rho[round(rho_t, 4)] = agg
        results = [by_rho[k] for k in sorted(by_rho)]
        tm = agg["t_sync_min_s"]
        print(f"[departure] {tag}: c p95={agg['c_ms'].get('p95', float('nan')):.3f} ms "
              f"(mean {agg['c_ms'].get('mean', float('nan')):.3f}, max {agg['c_ms'].get('max', float('nan')):.3f}) -> "
              f"T_sync^min(A={A:g}) p95={tm.get('p95', float('nan')):.2f} s | worst-case cycles {agg['n_worst_case']}/"
              f"{agg['n_cycles']} | T_U={agg['t_u_ms'].get('p95', float('nan')):.2f} ms | "
              f"outside-window cycles={agg['cycles_outside_unavail']}")
        with open(results_path, "w") as f:
            json.dump(results, f, indent=2, default=_jd)
        tt, tt_note = resolve_t_trans(args, out_dir)
        viz.plot_departure_tsync(os.path.join(out_dir, "departure_tsync"), results, a=A,
                                 title=_figure_title(args, base_topo) if getattr(args, "fig_title", False) else "",
                                 t_trans_s=tt, t_trans_note=tt_note, yscale=getattr(args, "fig_yscale", "log"),
                                 rho_band=viz.rho_band_from_survey(getattr(args, "rho_survey", "") or ""))
    return {"node_departure": results}


def measured_t_trans_s(dirs: List[str], q: float = 95.0) -> Optional[float]:
    """T_trans [s] = percentile ``q`` of the wall time of every reconfiguration round found in
    ``dirs``: departure-study cycles (``cycles/*/cycle.json``, the departure round) and
    continuous verify points (``points/*/point.json``, departure and rejoin rounds)."""
    import glob
    vals: List[float] = []
    for d in dirs:
        for f in glob.glob(os.path.join(d, "cycles", "*", "cycle.json")):
            with open(f) as fh:
                c = json.load(fh)
            if c.get("reconfigured") and c.get("t_trans_ms"):
                vals.append(c["t_trans_ms"] / 1000.0)
        for f in glob.glob(os.path.join(d, "points", "*", "point.json")):
            with open(f) as fh:
                pt = json.load(fh)
            for e in pt.get("periods_log", []):
                if e.get("reconfigured") and e.get("t_trans_ms"):
                    vals.append(e["t_trans_ms"] / 1000.0)
    if not vals:
        return None
    return float(np.percentile(vals, q))


def resolve_t_trans(args, out_dir: str) -> Tuple[Optional[float], str]:
    """--t-trans-s: a number, or 'auto' = percentile --t-trans-q (default 99) of the measured
    reconfiguration rounds in out_dir (+ --t-trans-from dirs). Returns (value [s], note)."""
    v = getattr(args, "t_trans_s", None)
    if v is None:
        return None, ""
    if str(v).lower() == "auto":
        q = float(getattr(args, "t_trans_q", 99.0) or 99.0)
        dirs = [out_dir] + [d for d in (getattr(args, "t_trans_from", "") or "").split(",") if d]
        return measured_t_trans_s(dirs, q), f"p{q:g} of measured reconfiguration rounds"
    return float(v), ""


def _figure_title(args, base_topo: Topology) -> str:
    return (f"hub departure, leader on the departing node\n{base_topo.name}, N={len(base_topo.nodes)}, "
            f"{'real cluster' if args.mode == 'real' else 'timing model'}, {args.cycles} cycles per rho, p95 of c")


def replot(out_dir: str, a: Optional[float] = None, t_trans_s=None, t_trans_from: str = "", t_trans_q: float = 99.0,
           title: bool = False, yscale: str = "log", rho_survey: str = ""):
    """Re-draw the figure from departure_results.json / args.json (no runs). ``t_trans_s``: number
    or 'auto' (percentile ``t_trans_q`` of the measured reconfiguration rounds in out_dir and
    ``t_trans_from`` dirs). ``rho_survey``: rho_survey.csv -> shaded real-deployment rho band."""
    import argparse
    from . import visualize as viz
    from .topology import load_topology
    with open(os.path.join(out_dir, "departure_results.json")) as f:
        results = json.load(f)
    with open(os.path.join(out_dir, "args.json")) as f:
        args = argparse.Namespace(**json.load(f))
    from .runner import REPO_ROOT
    base_topo = load_topology(args.topology, REPO_ROOT, seed=args.topo_seed, lambda_bs=args.lambda_bs)
    args.t_trans_s, args.t_trans_from, args.t_trans_q = t_trans_s, t_trans_from, t_trans_q
    tt, tt_note = resolve_t_trans(args, out_dir)
    if tt is not None:
        print(f"[departure] T_trans = {tt:.3f} s ({tt_note or 'given'})")
    viz.plot_departure_tsync(os.path.join(out_dir, "departure_tsync"), results,
                             a=args.availability if a is None else a,
                             title=_figure_title(args, base_topo) if title else "",
                             t_trans_s=tt, t_trans_note=tt_note, yscale=yscale,
                             rho_band=viz.rho_band_from_survey(rho_survey))

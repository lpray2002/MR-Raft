"""
Closed-loop verification of the availability bound   1 - A = c / T_sync.

The node-departure study (departure.py) measures the cost c(rho) of one worst-case
reconfiguration on a fresh cluster per cycle; T_sync^min = c / (1 - A) is then *derived*.
This module runs the same perturbation continuously on ONE cluster at a fixed T_sync and
measures A directly from the node logs, which checks the assumptions behind the derivation:

  * additivity: consecutive reconfigurations do not interfere (no fresh cluster, no settle);
  * the rejoin is free: observer-first join + promote, the leader does not move (c_join ~ 0);
  * nothing is unaccounted: A is measured over the whole run, not only inside the windows.

Schedule (period = T_sync, K periods, hub u, leader initially placed on u):

  period 2k+1  departure : u's coverage collapses -> Algorithm 4 prunes u -> FENCE / REMOVE
                           (leader transfer off u inside the Go node) / STOP        cost c_dep
  period 2k+2  rejoin    : u's edges reappear -> Algorithm 3 -> observer join + promote
                                                                                     cost c_join
               placement : the adversary moves the leader of every region of u back onto u,
                           so that the next departure is again the worst case.

The placement transfers are the adversary's action, not a system cost: their leaderless
intervals are attributed separately and excluded from A_sys (A_raw keeps them). The periods
partition the measured time axis, so U_dep + U_join + U_place = U_raw exactly.

Prediction for this schedule from the fresh-cluster c(rho): U_pred = c / (2 T_sync)
(one departure every two periods, c_join = 0). The paper's guarantee (a worst-case
reconfiguration in *every* period) is U <= c_p95 / T_sync; the measured U_sys must stay below it.

Optional request-based cross-check (``--verify-load``, workers/rate from ``--load-conc`` /
``--load-rate``): a Propose load on every staying node; a request counts as unavailable if it
fails or exceeds ``--load-deadline-ms``.

Other perturbation schedules (``--verify-schedule``), same accounting:
  nodes       every node in turn with the leader placed on it -> c per node (is the argmax node
              used for c(rho) really the worst?)
  allleaders  all current leaders depart in the same round -> per-round worst case c_all
  random      Poisson churn with natural leader placement -> typical operation, margin of the bound
"""
from __future__ import annotations

import glob
import json
import os
import time
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .client import ClusterClient
from .cm import ClusterManager, Clock, CMConfig, ModelExecutor, Naming, RealExecutor
from .departure import StaticCoverage, _jd, _leader_of, _sleep, pick_hub, place_leader_on
from .metrics import _intersect, _merge_intervals, _stats, node_unavailability_from_logs
from .topology import Topology, overlap_ratio, scale_to_rho

Interval = Tuple[float, float]


def _sum_in(intervals: Dict[int, List[Interval]], windows: List[Interval]) -> float:
    """Total node-seconds of ``intervals`` (per node, merged) inside ``windows``."""
    win = _merge_intervals(windows)
    if not win:
        return 0.0
    return sum(sum(b - a for a, b in _intersect(iv, win)) for iv in intervals.values())


def _load_fresh_c(path: Optional[str], rho: float) -> Optional[dict]:
    """c(rho) statistics [ms] of the fresh-cluster departure study for the matching rho."""
    if not path or not os.path.exists(path):
        return None
    with open(path) as f:
        rows = json.load(f)
    for r in rows:
        if abs(float(r["rho"]) - rho) < 1e-6 and r.get("c_ms", {}).get("n", 0) > 0:
            return dict(r["c_ms"])
    return None


# ---------------------------------------------------------------------------
# Accounting (standalone, so that finished points can be recomputed from their logs)
# ---------------------------------------------------------------------------

def _intervals_from_logs(logs_dir: str, epoch0: float, t_end: float, nodes: Sequence[int], res: dict
                         ) -> Dict[int, List[Interval]]:
    meas = node_unavailability_from_logs(logs_dir, epoch0, t_end + 0.3, rounds=None, nodes=list(nodes))
    res["leader_events"] = meas.get("leader_events")
    return {int(n): [tuple(x) for x in iv] for n, iv in meas.get("intervals", {}).items()}


def account(res: dict, intervals: Optional[Dict[int, List[Interval]]], availability: float,
            c_fresh: Optional[dict] = None) -> dict:
    """Fill the availability accounting of a point from its period log.

    intervals: per-node leaderless intervals (real mode) or None (timing model). The windows
    partition [t_start, t_end) along the *actual* round times (robust to late rounds):
    departure period = [tick_k, tick_{k+1}); rejoin period = [tick_k, placement start) +
    placement = [placement start, tick_{k+1}). "quiet" = the idle tails of the periods (noise
    indicator; a subset of the departure / rejoin windows)."""
    plog = res["periods_log"]
    n_nodes, t_sync = res["n_nodes"], res["t_sync"]
    t_start, t_end = res["t_start"], res["t_end"]
    T = res["duration_s"]
    dep = [e for e in plog if e["kind"] in ("departure", "churn")]
    rej = [e for e in plog if e["kind"] == "rejoin"]
    schedule = res.get("schedule", "hub")
    starts = [e["t_tick"] for e in plog] + [t_end]
    starts[0] = t_start
    dep_w, join_w, place_w, quiet_w = [], [], [], []
    # per-reconfiguration cost windows (round + 0.5 s margin, clipped to the period): the cost of
    # one departure / rejoin / placement without background events in the idle tail
    cdep_w, cjoin_w, cplace_w = [], [], []
    for i, e in enumerate(plog):
        a, b = starts[i], starts[i + 1]
        if e["kind"] in ("departure", "churn"):
            dep_w.append((a, b))
            quiet_w.append((e["t_done"] + 0.2, b))
            cdep_w.append((a, min(e["t_done"] + 0.5, b)))
        elif "t_place" in e:
            join_w.append((a, e["t_place"][0]))
            place_w.append((e["t_place"][0], b))
            quiet_w.append((e["t_place"][1] + 0.2, b))
            cjoin_w.append((a, min(e["t_done"] + 0.5, e["t_place"][0])))
            cplace_w.append((e["t_place"][0], min(e["t_place"][1] + 0.5, b)))
        else:
            join_w.append((a, b))
            quiet_w.append((e["t_done"] + 0.2, b))
            cjoin_w.append((a, min(e["t_done"] + 0.5, b)))
    quiet_sys_w = _intersect(_merge_intervals(quiet_w), _merge_intervals(dep_w + join_w))
    quiet_place_w = _intersect(_merge_intervals(quiet_w), _merge_intervals(place_w))
    if intervals is not None:
        u_dep = _sum_in(intervals, dep_w)
        u_join = _sum_in(intervals, join_w)
        u_place = _sum_in(intervals, place_w)
        u_quiet = _sum_in(intervals, quiet_w)
        u_quiet_sys = _sum_in(intervals, quiet_sys_w)
        u_quiet_place = _sum_in(intervals, quiet_place_w)
        c_dep = [1000.0 * _sum_in(intervals, [w]) / n_nodes for w in cdep_w]
        c_join = [1000.0 * _sum_in(intervals, [w]) / n_nodes for w in cjoin_w]
        c_place = [1000.0 * _sum_in(intervals, [w]) / n_nodes for w in cplace_w]
        per_node = {int(n): 1000.0 * _sum_in({n: iv}, dep_w + join_w) for n, iv in intervals.items()}
        res["source"] = "node logs (LEADER_UPDATED)"
    else:
        u_dep = sum(e["unavail_model_ms"] for e in dep) / 1000.0
        u_join = sum(e["unavail_model_ms"] for e in rej) / 1000.0
        u_place = sum(e.get("placement_model_ms", 0.0) for e in rej) / 1000.0
        u_quiet = u_quiet_sys = u_quiet_place = 0.0
        c_dep = [e["unavail_model_ms"] / n_nodes for e in dep]
        c_join = [e["unavail_model_ms"] / n_nodes for e in rej]
        c_place = [e.get("placement_model_ms", 0.0) / n_nodes for e in rej]
        per_node = {}
        res["source"] = "timing model"
    if schedule == "nodes":
        warm = [c for e, c in zip(dep, c_dep) if e.get("pass", 0) >= 1]
    else:
        warm = c_dep[1:]
    denom = n_nodes * T
    # quiet-window unavailability (background leader losses: spurious elections on the shared
    # host) lies in the idle tails of the periods; U_reconf = U_sys - (quiet part inside the
    # departure / rejoin windows) is the part attributable to the reconfigurations
    res.update({
        "unavail_node_s": {"departure": u_dep, "rejoin": u_join, "placement": u_place, "quiet": u_quiet,
                           "quiet_in_system_windows": u_quiet_sys, "quiet_in_placement_windows": u_quiet_place,
                           "total": u_dep + u_join + u_place},
        "U_raw": (u_dep + u_join + u_place) / denom, "U_sys": (u_dep + u_join) / denom,
        "U_reconf": max(0.0, u_dep + u_join - u_quiet_sys) / denom,
        "U_dep": u_dep / denom, "U_join": u_join / denom, "U_place": u_place / denom,
        "U_place_clean": max(0.0, u_place - u_quiet_place) / denom,
        "U_quiet": u_quiet / denom, "U_quiet_sys": u_quiet_sys / denom,
        "c_dep_ms": _stats(c_dep), "c_join_ms": _stats(c_join), "c_place_ms": _stats(c_place),
        # the first departure of a cluster is the "cold" case of the fresh-cluster study (first
        # leadership transfer to the staying executor: Raft transport connections are set up
        # lazily); the following ones are the steady state. nodes schedule: warm = pass >= 1.
        "c_dep_first_ms": c_dep[0] if c_dep else None,
        "c_dep_warm_ms": _stats(warm),
        "c_dep_list_ms": c_dep,
        "c_dep_model_ms": _stats([e["unavail_model_ms"] / n_nodes for e in dep]),
        "per_node_unavail_sys_ms": per_node,
    })
    if schedule == "nodes":
        by_node: Dict[int, List[float]] = {}
        by_node_warm: Dict[int, List[float]] = {}
        sizes: Dict[int, list] = {}
        for e, c in zip(dep, c_dep):
            v = int(e["node"])
            by_node.setdefault(v, []).append(c)
            sizes[v] = e["hub_regions_before"]
            if e.get("pass", 0) >= 1:
                by_node_warm.setdefault(v, []).append(c)
        res["c_by_node_ms"] = {v: {"all": _stats(by_node[v]), "warm": _stats(by_node_warm.get(v, [])),
                                   "regions": sizes[v]} for v in sorted(by_node)}
        means = {v: st["warm"]["mean"] for v, st in res["c_by_node_ms"].items() if st["warm"].get("n", 0) > 0}
        if means:
            res["worst_node"] = max(means, key=lambda v: (means[v], -v))
            res["worst_node_c_warm_ms"] = res["c_by_node_ms"][res["worst_node"]]["warm"]
            hub_n = pick_hub({i: set(r) for i, r in enumerate(res["regions0"].values())})[0]
            res["hub"] = int(hub_n)
            res["hub_c_warm_ms"] = res["c_by_node_ms"].get(hub_n, {}).get("warm", {})
    if schedule == "allleaders":
        res["n_absent_per_departure"] = _stats([e["n_absent"] for e in dep])
        res["targets_per_departure"] = _stats([e.get("targets", 0) for e in dep])
        res["transfers_per_departure"] = _stats([e["leader_transfers"] for e in dep])
    if schedule == "random":
        res["churn"] = {
            "events_per_period": _stats([e.get("n_events", 0) for e in dep]),
            "visible_departures_per_period": _stats([e["n_depart"] for e in dep]),
            "visible_returns_per_period": _stats([e["n_return"] for e in dep]),
            "absent_per_period": _stats([e["n_absent"] for e in dep]),
            "periods_with_transfer": sum(1 for e in dep if e["leader_transfers"] > 0),
            "periods_reconfigured": sum(1 for e in dep if e["reconfigured"]),
            "transfers_per_period": _stats([e["leader_transfers"] for e in dep]),
            "c_per_period_ms": _stats(c_dep),
        }
    if warm:
        st = res["c_dep_warm_ms"]
        res["t_sync_min_warm_s"] = {q: st[q] / 1000.0 / (1.0 - availability) for q in ("mean", "p50", "p95", "max")}
    res["A_raw"] = 1.0 - res["U_raw"]
    res["A_sys"] = 1.0 - res["U_sys"]
    res["A_reconf"] = 1.0 - res["U_reconf"]
    # predictions from the fresh-cluster study
    if c_fresh:
        res["c_fresh_ms"] = c_fresh
        res["U_pred_mean"] = c_fresh["mean"] / 1000.0 / (2 * t_sync)     # this schedule, expected
        res["U_pred_p50"] = c_fresh["p50"] / 1000.0 / (2 * t_sync)
        res["U_pred_p95"] = c_fresh["p95"] / 1000.0 / (2 * t_sync)
        res["U_bound_p95"] = c_fresh["p95"] / 1000.0 / t_sync            # paper: worst case every period
        res["ratio_meas_over_pred_mean"] = (res["U_reconf"] / res["U_pred_mean"]
                                            if res["U_pred_mean"] > 0 else None)
        res["bound_holds"] = res["U_sys"] <= res["U_bound_p95"]
    return res


def load_summary(reqs: List[Sequence], plog: List[dict], deadline_ms: float) -> dict:
    """Request-based availability of a Propose load: a request is unavailable if it failed or
    took longer than ``deadline_ms``. Failures whose request started inside a placement window
    are the adversary's (forced transfer to the just re-joined hub: Raft drops proposals while a
    leadership transfer is pending); the rest is attributable to the system's reconfigurations.
    Also reports the latency of the successful requests inside departure rounds vs. idle time."""
    n_req = len(reqs)
    place_w = _merge_intervals([(e["t_place"][0] - 0.005, e["t_place"][1] + 0.005) for e in plog if "t_place" in e])
    dep_w = _merge_intervals([(e["t_tick"] - 0.005, e["t_done"] + 0.02) for e in plog if e["kind"] == "departure"])
    join_w = _merge_intervals([(e["t_tick"] - 0.005, e["t_done"] + 0.02) for e in plog if e["kind"] == "rejoin"])

    def inside(t: float, win: List[Interval]) -> bool:
        return any(a <= t <= b for a, b in win)

    bad = [r for r in reqs if (not r[2]) or r[1] > deadline_ms]
    bad_place = [r for r in bad if inside(r[0], place_w)]
    bad_sys = [r for r in bad if not inside(r[0], place_w)]
    lat_ok = [r[1] for r in reqs if r[2]]
    lat_dep = [r[1] for r in reqs if r[2] and inside(r[0], dep_w)]
    lat_join = [r[1] for r in reqs if r[2] and inside(r[0], join_w)]
    lat_idle = [r[1] for r in reqs if r[2] and not (inside(r[0], dep_w) or inside(r[0], join_w) or inside(r[0], place_w))]
    out = {
        "deadline_ms": deadline_ms, "requests": n_req,
        "failed": sum(1 for r in reqs if not r[2]),
        "slow": sum(1 for r in reqs if r[2] and r[1] > deadline_ms),
        "unavailable": len(bad),
        "unavailable_in_placement": len(bad_place),
        "unavailable_system": len(bad_sys),
        "unavailable_system_in_departure": sum(1 for r in bad_sys if inside(r[0], dep_w)),
        "unavailable_system_in_rejoin": sum(1 for r in bad_sys if inside(r[0], join_w)),
        "fail_rate": (sum(1 for r in reqs if not r[2]) / n_req) if n_req else None,
        "unavail_rate_deadline": len(bad) / n_req if n_req else None,          # all, incl. adversary
        "unavail_rate_system": len(bad_sys) / n_req if n_req else None,        # system reconfigurations only
        "latency_ms": _stats(lat_ok),
        "latency_ms_departure_rounds": _stats(lat_dep),
        "latency_ms_rejoin_rounds": _stats(lat_join),
        "latency_ms_idle": _stats(lat_idle),
    }
    if bad and len(bad[0]) > 4:
        import collections
        out["errors"] = collections.Counter(r[4] for r in bad if not r[2]).most_common(5)
    return out


def _epoch0_estimate(sub: str) -> Optional[float]:
    """Wall-clock epoch of CM time 0 for a finished point that did not store it: align the first
    FENCE_REGION directive (CM time) with the node's FENCE_REGION event (epoch ms)."""
    try:
        with open(os.path.join(sub, "cm_events.jsonl")) as f:
            d = next(e for e in map(json.loads, f) if e.get("type") == "directive" and e["op"] == "FENCE_REGION")
        with open(os.path.join(sub, "data", "logs", f"events_{d['node'] + 1}.jsonl")) as f:
            ev = next(json.loads(l) for l in f if '"FENCE_REGION"' in l)
        return ev["ts_ms"] / 1000.0 - d["t_sent"]
    except (StopIteration, FileNotFoundError, KeyError):
        return None


def recompute(out_dir: str):
    """Re-derive the accounting of every finished real-mode point in ``out_dir`` from its node
    logs (e.g. after a change of the accounting) and re-draw the figures."""
    import argparse
    from .topology import load_topology
    from .runner import REPO_ROOT
    with open(os.path.join(out_dir, "args.json")) as f:
        args = argparse.Namespace(**json.load(f))
    results = []
    for sub in sorted(glob.glob(os.path.join(out_dir, "points", "*"))):
        pj = os.path.join(sub, "point.json")
        if not os.path.exists(pj):
            continue
        with open(pj) as f:
            res = json.load(f)
        if "error" in res:
            continue
        res.setdefault("schedule", "hub")
        if res.get("mode") == "real":
            epoch0 = res.get("epoch0") or _epoch0_estimate(sub)
            if epoch0 is None:
                print(f"[verify] {res['point']}: epoch0 unknown, kept as is")
            else:
                res["epoch0"] = epoch0
                nodes = list(range(res["n_nodes"]))
                intervals = _intervals_from_logs(os.path.join(sub, "data", "logs"), epoch0, res["t_end"], nodes, res)
                account(res, intervals, getattr(args, "availability", 0.9999), res.get("c_fresh_ms"))
                lr = os.path.join(sub, "load_requests.json")
                if os.path.exists(lr):
                    with open(lr) as f:
                        reqs = json.load(f)
                    old = res.get("load", {})
                    res["load"] = load_summary(reqs, res["periods_log"], old.get("deadline_ms", 100.0))
                    res["load"].update({k: old[k] for k in ("workers_per_node", "rate_per_worker") if k in old})
                with open(pj, "w") as f:
                    json.dump(res, f, indent=2, default=_jd)
        results.append(res)
    results.sort(key=lambda r: (r.get("schedule", "hub"), r.get("rho", 0), r.get("t_sync", 0)))
    with open(os.path.join(out_dir, "verify_results.json"), "w") as f:
        json.dump(results, f, indent=2, default=_jd)
    base_topo = load_topology(args.topology, REPO_ROOT, seed=args.topo_seed, lambda_bs=args.lambda_bs)
    _plots(args, base_topo, out_dir, results)
    return results


# ---------------------------------------------------------------------------
# One (rho, T_sync) point: K periods on one cluster
# ---------------------------------------------------------------------------

def run_point(args, topo: Topology, tau: int, sub: str, mode: str, t_sync: float, periods: int,
              node_override: Optional[int] = None, c_fresh: Optional[dict] = None,
              schedule: str = "hub", seed: int = 0) -> dict:
    """One point = one cluster, K periods of T_sync. ``schedule``:

    hub         the worst-case node departs / rejoins in alternating periods, the leader is put
                back on it before every departure (bound check; default)
    nodes       every node in turn (round robin), leader put on the departing node: c per node,
                checks that the argmax node used for c(rho) really is the worst
    allleaders  every period with a departure removes *all* current leaders at once, so every
                region transfers in the same round (per-round worst case c_all); no placement
    random      Poisson churn: present nodes leave at rate churn_rate / (N T_sync), absent nodes
                return after an Exp(dwell_periods T_sync) stay, natural leader placement;
                events fall between ticks, so flaps inside a period cancel (net diff)"""
    os.makedirs(sub, exist_ok=True)
    rng = np.random.default_rng(seed)
    n_nodes = len(topo.nodes)
    trace = StaticCoverage(topo)
    cfg = CMConfig(t_sync_s=t_sync, tau=tau, hysteresis_m=0.0, min_dwell_s=0.0,
                   batch_mode=args.batch_mode, adversarial_leader="none")
    cluster = None
    cc = None
    load = None
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

        # deploy, pick the departing node, worst-case placement, settle
        rec0 = cm.bootstrap()
        if mode == "real":
            cm.refresh_leaders(cc)
        regions0 = {int(rid): sorted(r) for rid, r in cm.mgr.regions.items()}
        order = sorted(topo.nodes)
        if schedule == "nodes":
            u = order[0]
        elif node_override is None:
            u, _ = pick_hub(cm.mgr.regions)
        else:
            u = node_override
        sizes0 = sorted(len(r) for r in cm.mgr.regions.values() if u in r and len(r) >= 2)
        if mode == "real" and args.verify_load and args.load_conc > 0:
            from .uav_workload import LoadGenerator
            # Propose load on the staying nodes only (ClusterClient is keyed by mec id)
            load = LoadGenerator(cc, clock, concurrency_per_node=args.load_conc, rate_per_worker=args.load_rate,
                                 nodes=[LocalCluster.mec_id(n) for n in topo.nodes if n != u], record_requests=True)
            load.start(duration_s=1e12)          # stopped explicitly
        placement0 = place_leader_on(cm, executor, u) if schedule in ("hub", "nodes") else {}
        _sleep(clock, args.settle_s)

        def leaders_now() -> Dict[int, int]:
            if mode == "real":
                cm.refresh_leaders(cc)
            out = {}
            for cid, members in cm.serving.items():
                if len(members) < 2:
                    continue
                lead = _leader_of(cm, executor, cid)
                if lead is not None:
                    out[cid] = lead
            return out

        # random schedule: continuous-time churn state (Gillespie between ticks)
        churn_absent: set = set()
        lam_leave = args.churn_rate / (n_nodes * t_sync)          # per present node per second
        lam_return = 1.0 / max(args.dwell_periods * t_sync, 1e-9)  # per absent node per second

        def churn_advance(t_from: float, t_to: float) -> int:
            n_ev = 0
            t = t_from
            while True:
                n_p = n_nodes - len(churn_absent)
                rate = n_p * lam_leave + len(churn_absent) * lam_return
                if rate <= 0:
                    break
                t += rng.exponential(1.0 / rate)
                if t > t_to:
                    break
                if rng.random() < n_p * lam_leave / rate:
                    churn_absent.add(int(rng.choice([n for n in topo.nodes if n not in churn_absent])))
                else:
                    churn_absent.discard(int(rng.choice(sorted(churn_absent))))
                n_ev += 1
            return n_ev

        # K periods on a fixed tick grid
        t_start = clock.now()
        plog: List[dict] = []
        next_tick = t_start
        for k in range(periods):
            if clock.virtual:
                if clock.now() < next_tick:
                    clock.set(next_tick)
            else:
                while True:
                    dt = next_tick - clock.now()
                    if dt <= 0:
                        break
                    time.sleep(min(dt, 0.005))
            late_s = max(0.0, clock.now() - next_tick)
            prev_absent = set(trace.absent)
            extra: dict = {"schedule": schedule}
            place_target: Optional[int] = None
            if schedule == "hub":
                depart = (k % 2 == 0)
                kind = "departure" if depart else "rejoin"
                absent = {u} if depart else set()
                extra["node"] = int(u)
                if not depart:
                    place_target = u
            elif schedule == "nodes":
                depart = (k % 2 == 0)
                kind = "departure" if depart else "rejoin"
                v = order[(k // 2) % n_nodes]
                absent = {v} if depart else set()
                extra["node"] = int(v)
                extra["pass"] = (k // 2) // n_nodes
                if not depart:
                    place_target = order[((k // 2) + 1) % n_nodes]
            elif schedule == "allleaders":
                depart = (k % 2 == 0)
                kind = "departure" if depart else "rejoin"
                if depart:
                    lead = leaders_now()
                    absent = set(lead.values())
                    extra["targets"] = len(lead)
                else:
                    absent = set()
            elif schedule == "random":
                kind = "churn"
                extra["n_events"] = churn_advance(next_tick - t_sync if k > 0 else t_start, next_tick)
                absent = set(churn_absent)
            else:
                raise ValueError(f"unknown schedule {schedule}")
            extra.update({"absent": sorted(absent), "n_absent": len(absent),
                          "n_depart": len(absent - prev_absent), "n_return": len(prev_absent - absent)})
            v_reg = extra.get("node", u)              # regions of the node that departs / rejoins
            hub_regions = sorted(len(r) for r in cm.mgr.regions.values() if v_reg in r and len(r) >= 2)
            trace.absent = absent
            rec = cm.sync_round()
            e = {
                "k": k, "kind": kind, "round": rec.round,
                "t_period": next_tick, "t_tick": rec.t_tick, "t_done": rec.t_done, "late_s": late_s,
                "reconfigured": rec.reconfigured, "kinds": rec.kinds, "n_directives": rec.n_directives,
                "failures": rec.failures, "leader_transfers": rec.leader_transfers,
                "n_transfer_member_nodes": rec.n_transfer_member_nodes,
                "unavail_model_ms": rec.unavail_node_ms, "t_trans_ms": rec.t_trans_ms,
                "hub_regions_before": hub_regions, "n_regions_after": len(cm.mgr.regions),
                "size_ratio": rec.size_ratio, **extra,
            }
            if place_target is not None:
                # adversary: leader onto the node that departs next (a voter again after this round)
                t_p0 = clock.now()
                pl = place_leader_on(cm, executor, place_target)
                t_p1 = clock.now()
                e["t_place"] = [t_p0, t_p1]
                e["place_node"] = int(place_target)
                e["placement_ok"] = all(v.get("ok") for v in pl.values())
                e["placement_n"] = len(pl)
                e["placement_model_ms"] = sum(v.get("wait_ms", 0.0) * v.get("size", 0) for v in pl.values()
                                              if v.get("ok") and not v.get("already"))
            e["t_period_end"] = clock.now()
            e["overrun"] = e["t_period_end"] > next_tick + t_sync
            plog.append(e)
            if e["failures"] or not e.get("placement_ok", True):
                print(f"[verify]   period {k} ({e['kind']}): failures={e['failures']} "
                      f"placement_ok={e.get('placement_ok')}")
            next_tick += t_sync
        t_end = t_start + periods * t_sync
        if clock.virtual:
            clock.set(t_end)
        else:
            while clock.now() < t_end + 0.3:          # let the last LEADER_UPDATED events be written
                time.sleep(0.01)
        if load is not None:
            load.stop()
        cm.close()

        # ------------------------------------------------------------ accounting
        T = periods * t_sync
        dep = [e for e in plog if e["kind"] in ("departure", "churn")]
        rej = [e for e in plog if e["kind"] == "rejoin"]
        res = {
            "point": os.path.basename(sub), "mode": mode, "n_nodes": n_nodes, "tau": tau, "t_sync": t_sync,
            "schedule": schedule, "seed": seed,
            "churn_rate": args.churn_rate if schedule == "random" else None,
            "dwell_periods": args.dwell_periods if schedule == "random" else None,
            "periods": periods, "n_departures": len(dep), "n_rejoins": len(rej), "node": int(u),
            "hub_regions0": sizes0, "regions0": regions0,
            "t_start": t_start, "t_end": t_end, "duration_s": T,
            "bootstrap_t_cfg_ms": rec0.t_cfg_ms,
            "placement0_ok": all(v.get("ok") for v in placement0.values()),
            "failures": sum(e["failures"] for e in plog),
            "overruns": sum(1 for e in plog if e["overrun"]),
            "late_max_s": max((e["late_s"] for e in plog), default=0.0),
            "placement_failed": sum(1 for e in plog if "t_place" in e and not e.get("placement_ok", False)),
            "transfers_per_departure": _stats([e["leader_transfers"] for e in dep]),
            "hub_regions_per_departure": [e["hub_regions_before"] for e in dep],
            "n_regions_after_rejoin": [e["n_regions_after"] for e in rej],
            "size_ratio_after_rejoin": [e["size_ratio"] for e in rej],
            "periods_log": plog,
        }
        if mode == "real":
            res["epoch0"] = clock.epoch0
            intervals = _intervals_from_logs(os.path.join(sub, "data", "logs"), clock.epoch0, t_end, topo.nodes, res)
        else:
            intervals = None
        account(res, intervals, args.availability, c_fresh)
        # request-based cross-check
        if load is not None:
            reqs = [r for r in load.requests if t_start <= r[0] < t_end]
            with open(os.path.join(sub, "load_requests.json"), "w") as f:
                json.dump(reqs, f)
            res["load"] = load_summary(reqs, plog, args.load_deadline_ms)
            res["load"].update({"workers_per_node": args.load_conc, "rate_per_worker": args.load_rate})
        return res
    finally:
        if load is not None:
            load.stop()
        if cc is not None:
            cc.close()
        if cluster is not None:
            cluster.stop()


# ---------------------------------------------------------------------------
# rho x T_sync loop
# ---------------------------------------------------------------------------

def _point_key(rho: float, t_sync: float, schedule: str = "hub") -> str:
    return f"rho{rho:.2f}_ts{t_sync:g}" + ("" if schedule in (None, "hub") else f"_{schedule}")


def run_verify(args, base_topo: Topology, out_dir: str) -> dict:
    from .runner import resolve_tau
    rhos: List[Optional[float]] = [float(x) for x in args.rho_sweep.split(",")] if args.rho_sweep else [None]
    tsyncs: List[float] = [float(x) for x in args.verify_tsyncs.split(",")] if args.verify_tsyncs else [args.tsync]
    results_path = os.path.join(out_dir, "verify_results.json")
    by_key: Dict[str, dict] = {}
    if os.path.exists(results_path):
        with open(results_path) as f:
            for r in json.load(f):
                if "error" in r or r.get("t_sync") is None:
                    continue                        # failed points are re-run
                by_key[_point_key(r["rho"], r["t_sync"], r.get("schedule", "hub"))] = r
    schedule = args.verify_schedule
    est = sum(args.periods * ts + 8 for ts in tsyncs) * len(rhos)
    print(f"[verify] schedule={schedule}: {len(rhos)} rho x {len(tsyncs)} T_sync, {args.periods} periods each; "
          f"estimated {est / 60:.0f} min (+ bootstrap)")
    results: List[dict] = []
    for rho in rhos:
        if rho is None:
            topo_r, scale = base_topo, 1.0
            rho_t = rho_ach = overlap_ratio(base_topo.graph())
        else:
            topo_r, scale, rho_ach = scale_to_rho(base_topo, rho, args.rho_margin)
            rho_t = rho
        tau = resolve_tau(args, topo_r)
        c_fresh = _load_fresh_c(args.departure_results, rho_t)
        for ts in tsyncs:
            key = _point_key(rho_t, ts, schedule)
            sub = os.path.join(out_dir, "points", key)
            done = os.path.join(sub, "point.json")
            res = None
            if args.resume and os.path.exists(done):
                with open(done) as f:
                    res = json.load(f)
                if "error" in res:
                    res = None                      # failed point -> run again
                else:
                    print(f"[verify] resume {key}")
            if res is None:
                print(f"[verify] === {key} (rho achieved {rho_ach:.3f}, scale x{scale:.3f}) tau*={tau} "
                      f"mode={args.mode} periods={args.periods} ===")
                try:
                    res = run_point(args, topo_r, tau, sub, args.mode, ts, args.periods,
                                    args.departure_node, c_fresh, schedule=schedule,
                                    seed=args.churn_seed + int(round(1000 * rho_t)))
                except Exception as e:      # keep the sweep alive
                    res = {"point": key, "t_sync": ts, "periods": args.periods, "schedule": schedule,
                           "error": repr(e)}
                    print(f"[verify] {key}: FAILED {e!r}")
                res.update({"rho": rho_t, "rho_achieved": rho_ach, "scale": scale})
                os.makedirs(sub, exist_ok=True)
                with open(done, "w") as f:
                    json.dump(res, f, indent=2, default=_jd)
            if "error" not in res:
                pred = (f" | pred (fresh c): U={res['U_pred_mean']:.2e} bound={res['U_bound_p95']:.2e} "
                        f"meas/pred={res['ratio_meas_over_pred_mean']:.2f}"
                        if res.get("U_pred_mean") is not None else "")
                ld = res.get("load")
                lds = (f" | load: n={ld['requests']} unavailable(fail or >{ld['deadline_ms']:g}ms)="
                       f"{ld['unavailable']} of which in adversary placement={ld['unavailable_in_placement']}, "
                       f"system={ld['unavailable_system']} (rate {ld['unavail_rate_system']:.2e})"
                       if ld and ld["requests"] else "")
                extra = ""
                if schedule == "nodes" and res.get("worst_node") is not None:
                    extra = (f" | worst node={res['worst_node']} c_warm mean/p95="
                             f"{res['worst_node_c_warm_ms']['mean']:.3f}/{res['worst_node_c_warm_ms']['p95']:.3f} ms"
                             f" hub={res.get('hub')} c_warm mean={res.get('hub_c_warm_ms', {}).get('mean', float('nan')):.3f} ms")
                elif schedule == "allleaders":
                    extra = (f" | leaders removed/departure={res['n_absent_per_departure'].get('mean', 0):.1f} "
                             f"transfers/departure={res['transfers_per_departure'].get('mean', 0):.1f}")
                elif schedule == "random":
                    ch = res["churn"]
                    extra = (f" | churn: events/period={ch['events_per_period']['mean']:.2f} visible dep/ret="
                             f"{ch['visible_departures_per_period']['mean']:.2f}/{ch['visible_returns_per_period']['mean']:.2f} "
                             f"absent={ch['absent_per_period']['mean']:.1f} periods with transfer="
                             f"{ch['periods_with_transfer']}/{res['n_departures']}")
                print(f"[verify] {key}: node={res['node']} regions={res['hub_regions0']} "
                      f"U_sys={res['U_sys']:.2e} (A_sys={100 * res['A_sys']:.4f}%) U_reconf={res['U_reconf']:.2e} "
                      f"U_raw={res['U_raw']:.2e} "
                      f"[dep {res['U_dep']:.2e} join {res['U_join']:.2e} place {res['U_place']:.2e} "
                      f"quiet {res['U_quiet']:.2e}] c_dep first="
                      f"{res['c_dep_first_ms'] if res.get('c_dep_first_ms') is not None else float('nan'):.3f} "
                      f"warm p50/p95={res['c_dep_warm_ms'].get('p50', float('nan')):.3f}/"
                      f"{res['c_dep_warm_ms'].get('p95', float('nan')):.3f} ms c_join p95="
                      f"{res['c_join_ms'].get('p95', float('nan')):.3f} ms fail={res['failures']} "
                      f"overruns={res['overruns']}{pred}{lds}{extra}")
            by_key[key] = res
            results = [by_key[k] for k in sorted(by_key, key=lambda k: (by_key[k].get("schedule", "hub"),
                                                                        by_key[k].get("rho", 0),
                                                                        by_key[k].get("t_sync", 0)))]
            with open(results_path, "w") as f:
                json.dump(results, f, indent=2, default=_jd)
            _plots(args, base_topo, out_dir, results)
    return {"verify": results}


def _plots(args, base_topo: Topology, out_dir: str, results: List[dict]):
    from . import visualize as viz
    viz.plot_verify_tsync(os.path.join(out_dir, "verify_tsync"), results,
                          title=_figure_title(args, base_topo, results))
    fresh = None
    if args.departure_results and os.path.exists(args.departure_results):
        with open(args.departure_results) as f:
            fresh = json.load(f)
    viz.plot_verify_c_rho(os.path.join(out_dir, "verify_tsync_rho"), results, fresh, a=args.availability,
                          title=f"worst-case departure cost: first transfer of a cluster (cold) vs steady state (warm)\n"
                                f"{base_topo.name}, N={len(base_topo.nodes)}, real cluster")
    viz.plot_verify_schedules(os.path.join(out_dir, "verify_schedules_rho"), results, fresh,
                              title=(f"unavailability per schedule at fixed T_sync\n"
                                     f"{base_topo.name}, N={len(base_topo.nodes)}, real cluster")
                              if getattr(args, "fig_title", False) else "")
    viz.plot_verify_nodes(os.path.join(out_dir, "verify_nodes"), results, fresh,
                          title=f"departure cost per node (leader placed on the departing node), steady state\n"
                                f"{base_topo.name}, N={len(base_topo.nodes)}, real cluster")


def _figure_title(args, base_topo: Topology, results: Optional[List[dict]] = None) -> str:
    ps = sorted({r["periods"] for r in (results or []) if "error" not in r and r.get("periods")})
    per = (f", {ps[0]} periods per point" if len(ps) == 1 else f", {ps[0]}-{ps[-1]} periods per point") if ps else ""
    return (f"continuous hub departure / rejoin, leader placed on the hub before every departure\n"
            f"{base_topo.name}, N={len(base_topo.nodes)}, "
            f"{'real cluster' if args.mode == 'real' else 'timing model'}{per}")


def replot(out_dir: str):
    """Re-draw the figure from verify_results.json / args.json (no runs)."""
    import argparse
    from .topology import load_topology
    with open(os.path.join(out_dir, "verify_results.json")) as f:
        results = json.load(f)
    with open(os.path.join(out_dir, "args.json")) as f:
        args = argparse.Namespace(**json.load(f))
    from .runner import REPO_ROOT
    base_topo = load_topology(args.topology, REPO_ROOT, seed=args.topo_seed, lambda_bs=args.lambda_bs)
    _plots(args, base_topo, out_dir, results)

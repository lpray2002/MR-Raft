"""
One-shot orchestration of the CM end-to-end pipeline.

Real mode (``--mode real``)
    launch N ``mec-node-cm`` processes locally -> CM bootstrap (initial
    threshold-based partition) -> UAV fleet + background load -> coverage
    dynamics + CM sync rounds for ``--duration`` seconds -> metrics + figures
    (+ optional GIF animation).

Sim mode (``--mode sim``)
    same CM / planner / dynamics / UAV model, but protocol timings come from
    an analytical model (defaults = values measured on the real cluster) so
    that long T_sync sweeps can be run in seconds  (``--sweep 0.2,0.5,1,2,5,10``).

Batch semantics (``--batch-mode``)
    sequential (default): the net edge changes of one interval are applied one by one with the
    single-edge primitives of the paper (Algorithm 4 for removals, then Algorithm 3 for
    additions); only the final region difference is pushed to the Raft layer (one
    reconfiguration per round).  recompute: local re-partition of the affected subgraph.

rho -> T_sync study (``--rho-sweep``)
    For every overlap ratio the coverage radii are scaled (locations fixed, as in the paper's
    Table III), tau* is selected, and the pipeline runs under the configured churn for each
    ``--adversarial-variants`` value (none = CM behaviour; one/all = forced leader transfers in
    the affected regions).  Per run the availability cost of one reconfiguration
    c = (1/N) sum_j unavail_j is obtained from the CM accounting (leader-transfer wait x region
    members) and, in real mode, from the nodes' LEADER_UPDATED / START / FENCE / STOP event
    logs.  ``rho_tsync.png`` shows c(rho), T_sync^min = c_p95/(1-A), the consistency bound
    from ``--budget-s`` and the churn absorbed by the sequential semantics (over-coverage).

Node-departure study (``--node-departure``, departure.py)
    Per rho, fresh cluster per cycle: hub departs with the leader on it; c(rho) from the node
    logs -> ``departure_tsync.png`` = c_p95 / (1 - A).

Closed-loop verification (``--verify-tsync``, verify.py)
    Per rho and T_sync, one cluster: the hub departs / rejoins in alternating periods with the
    leader placed back on it before every departure; A is measured over the whole run and
    compared with the prediction c(rho) / (2 T_sync) from ``--departure-results`` ->
    ``verify_tsync.png``.  ``--load-conc > 0`` adds a request-based cross-check.

Examples
    python -m src.cm.runner --mode real --topology real --duration 180 --tsync 1 --animate
    python -m src.cm.runner --mode sim  --topology real --duration 600 --sweep 0.2,0.5,1,2,5,10 --seeds 3
    python -m src.cm.runner --mode sim  --topology real --duration 180 --tsync 1 --tau auto \
        --rho-sweep 0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8,0.9,1.0 --adversarial-variants none,one,all --budget-s 3
    python -m src.cm.runner --mode real --topology real --duration 120 --tsync 1 --tau auto --rho 0.5 \
        --adversarial-leader all
    python -m src.cm.runner --mode real --topology real --node-departure --rho-sweep 0.1,0.2,0.5,1.0 \
        --cycles 20 --tau auto --out output/cm_e2e/node_departure_v1
    python -m src.cm.runner --mode real --topology real --verify-tsync --rho-sweep 0.1,0.5,1.0 \
        --verify-tsyncs 1,2,5 --periods 100 --tau auto \
        --departure-results output/cm_e2e/node_departure_v1/departure_results.json --out output/cm_e2e/verify_v1
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict
from typing import Dict, List, Optional, Tuple

import numpy as np

from .client import ClusterClient
from .cm import Clock, CMConfig, ClusterManager, ModelExecutor, Naming, RealExecutor
from .dynamics import CoverageTrace, DynamicsConfig
from .metrics import (A_TARGETS, ServingTimeline, availability_summary, consistency_summary, coverage_lags,
                      node_unavailability_from_logs, overhead_summary, responsiveness_summary,
                      violation_timeseries)
from .topology import Topology, load_topology, overlap_ratio, scale_to_rho
from .uav_workload import LoadGenerator, ModelDataPlane, UAVConfig, UAVFleet
from . import visualize as viz

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT_GO_BIN = os.path.abspath(os.path.join(REPO_ROOT, "..", "mr-raft", "bin", "mec-node-cm"))


# ---------------------------------------------------------------------------
# Go cluster lifecycle
# ---------------------------------------------------------------------------

class LocalCluster:
    def __init__(self, go_bin: str, data_dir: str, nodes: List[int], args):
        self.go_bin = go_bin
        self.data_dir = data_dir
        self.nodes = nodes
        self.args = args
        self.procs: Dict[int, subprocess.Popen] = {}
        self.node_cfgs: Dict[int, dict] = {}
        self.logs = []

    @staticmethod
    def mec_id(n: int) -> int:
        return n + 1

    def raft_addr(self, n: int) -> str:
        return f"127.0.0.1:{30000 + self.mec_id(n)}"

    def grpc_addr(self, n: int) -> str:
        return f"127.0.0.1:{50050 + self.mec_id(n)}"

    def _check_ports_free(self):
        import socket
        busy = []
        for n in self.nodes:
            for port in (30000 + self.mec_id(n), 50050 + self.mec_id(n)):
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    if s.connect_ex(("127.0.0.1", port)) == 0:
                        busy.append(port)
        if busy:
            raise RuntimeError(f"ports already in use (stale mec-node-cm processes?): {busy}. "
                               f"Run: pkill -f bin/mec-node-cm")

    def start(self):
        self._check_ports_free()
        shutil.rmtree(self.data_dir, ignore_errors=True)
        os.makedirs(os.path.join(self.data_dir, "logs"), exist_ok=True)
        import atexit
        atexit.register(self.stop)
        for n in self.nodes:
            mid = self.mec_id(n)
            env = dict(os.environ,
                       MEC_NODE_ID=str(mid), MEC_NODE_NAME=f"mec-{mid}",
                       MEC_NODE_ADDRESS=self.raft_addr(n), GRPC_PORT=str(50050 + mid),
                       MEC_NODE_DATA_DIR=os.path.join(self.data_dir, "raft"),
                       USE_MEMORY_SM="false" if self.args.disk_sm else "true",
                       RAFT_TICK_MS=str(self.args.tick_ms), ELECTION_RTT=str(self.args.election_rtt),
                       HEARTBEAT_RTT=str(self.args.heartbeat_rtt),
                       PROPOSE_TIMEOUT_MS=str(self.args.propose_timeout_ms),
                       SNAPSHOT_ENTRIES="10000",
                       EVENT_LOG=os.path.join(self.data_dir, "logs", f"events_{mid}.jsonl"),
                       GOMAXPROCS=str(self.args.gomaxprocs),
                       RAFT_MEMFS="false" if self.args.no_memfs else "true")
            if self.args.delay_us > 0:
                env["NETWORK_DELAY_US"] = str(self.args.delay_us)
            log = open(os.path.join(self.data_dir, "logs", f"node_{mid}.log"), "w")
            self.logs.append(log)
            self.procs[n] = subprocess.Popen([self.go_bin], env=env, stdout=log, stderr=subprocess.STDOUT)
            self.node_cfgs[mid] = {"grpc": self.grpc_addr(n), "raft": self.raft_addr(n)}
        time.sleep(1.5)
        dead = [n for n, p in self.procs.items() if p.poll() is not None]
        if dead:
            tails = []
            for n in dead[:3]:
                path = os.path.join(self.data_dir, "logs", f"node_{self.mec_id(n)}.log")
                with open(path) as f:
                    tails.append(f"--- node {self.mec_id(n)}:\n" + "".join(f.readlines()[-4:]))
            self.stop()
            raise RuntimeError(f"{len(dead)} Go node(s) exited at startup\n" + "\n".join(tails))

    def stop(self):
        procs = list(self.procs.values())
        self.procs = {}
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(8)
            except Exception:
                p.kill()
        for f in self.logs:
            try:
                f.close()
            except Exception:
                pass
        self.logs = []


# ---------------------------------------------------------------------------
# Analysis shared by both modes
# ---------------------------------------------------------------------------

def analyze(out_dir: str, topo: Topology, trace: CoverageTrace, cm: ClusterManager, fleet: UAVFleet,
            load_samples, t_end: float, t_sync: float, title: str, animate: bool, label: str = "",
            logs_dir: Optional[str] = None, extra: Optional[dict] = None) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    timeline = ServingTimeline(cm.snapshots)
    t_start = cm.rounds[0].t_done if cm.rounds else 0.0
    lags = coverage_lags(trace, timeline, t_start)
    vts, vfrac, vunc, vedges = violation_timeseries(trace, timeline, t_start, t_end, 0.1)
    n_nodes = len(topo.nodes)
    summary = {
        "label": label, "topology": topo.name, "nodes": n_nodes, "duration_s": t_end, "t_sync_s": t_sync,
        "rho": overlap_ratio(topo.graph()), "tau": cm.cfg.tau,
        "cm": cm.summary(),
        "dynamics": trace.summary(),
        "responsiveness": responsiveness_summary(lags),
        "overhead": overhead_summary(cm.rounds, t_end - t_start),
        "consistency": consistency_summary(vts, vfrac, vunc, vedges, fleet.records, timeline, trace),
        "availability": availability_summary(cm.rounds, n_nodes, t_end - t_start),
        "uav": fleet.summary(),
        "load": {"ok": sum(s["ok"] for s in load_samples), "fail": sum(s["fail"] for s in load_samples)} if load_samples else {},
    }
    if extra:
        summary.update(extra)
    if logs_dir and os.path.isdir(logs_dir):
        # node-side measurement (LEADER_UPDATED + START/FENCE/STOP events of every Go node)
        meas = node_unavailability_from_logs(logs_dir, cm.clock.epoch0, t_end, rounds=cm.rounds, nodes=topo.nodes)
        summary["availability_measured"] = {k: v for k, v in meas.items() if k not in ("intervals",)}
        summary["availability_measured"].update(_node_side_transfer_stats(cm.rounds, meas, n_nodes, t_end - t_start))
        with open(os.path.join(out_dir, "unavailability_intervals.json"), "w") as f:
            json.dump(meas["intervals"], f)
    with open(os.path.join(out_dir, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2, default=_jd)
    with open(os.path.join(out_dir, "rounds.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["round", "t_tick", "t_done", "n_changes", "added", "removed", "reconfigured", "regions_before",
                    "regions_after", "kinds", "n_directives", "bytes_cfg", "elections", "membership_changes",
                    "failures", "t_batch_ms", "t_part_ms", "t_cfg_ms", "t_joint_ms", "t_migrate_ms", "t_retire_ms",
                    "t_transfer_ms", "t_trans_ms", "constraints_ok", "leader_transfers", "n_reconf_nodes",
                    "n_transfer_member_nodes", "unavail_node_ms", "size_sum", "fresh_size_sum", "size_ratio"])
        for r in cm.rounds:
            w.writerow([r.round, f"{r.t_tick:.3f}", f"{r.t_done:.3f}", r.n_changes, r.added_edges, r.removed_edges,
                        r.reconfigured, r.regions_before, r.regions_after, json.dumps(r.kinds), r.n_directives,
                        r.bytes_cfg, r.elections, r.membership_changes, r.failures, f"{r.t_batch_ms:.3f}",
                        f"{r.t_part_ms:.3f}", f"{r.t_cfg_ms:.1f}", f"{r.t_joint_ms:.1f}", f"{r.t_migrate_ms:.1f}",
                        f"{r.t_retire_ms:.1f}", f"{r.t_transfer_ms:.1f}", f"{r.t_trans_ms:.1f}", r.constraints_ok,
                        r.leader_transfers, r.n_reconf_nodes, r.n_transfer_member_nodes, f"{r.unavail_node_ms:.3f}",
                        r.size_sum, r.fresh_size_sum, f"{r.size_ratio:.3f}"])
    with open(os.path.join(out_dir, "directives.csv"), "w", newline="") as f:
        w = csv.writer(f)
        keys = list(asdict(cm.directives[0]).keys()) if cm.directives else []
        w.writerow(keys)
        for d in cm.directives:
            w.writerow([asdict(d)[k] for k in keys])
    with open(os.path.join(out_dir, "lags.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_event", "u", "v", "lag_s", "transient", "lifetime_s"])
        for l in lags:
            w.writerow([f"{l.t_event:.3f}", l.edge[0], l.edge[1], "" if l.lag_s is None else f"{l.lag_s:.3f}",
                        l.transient, "" if l.lifetime_s is None else f"{l.lifetime_s:.3f}"])
    if load_samples:
        with open(os.path.join(out_dir, "load_samples.json"), "w") as f:
            json.dump(load_samples, f)
    np.savetxt(os.path.join(out_dir, "violation_timeseries.csv"),
               np.column_stack([vts, vfrac, vunc, vedges]), delimiter=",", header="t,frac,uncovered,edges", comments="")

    viz.plot_timeline(os.path.join(out_dir, "timeline"), trace, cm, fleet.records, load_samples,
                      vts, vfrac, vunc, t_end, title=title)
    viz.plot_ttrans_breakdown(os.path.join(out_dir, "t_trans_breakdown"), cm, t_sync)
    if animate:
        n = viz.animate(os.path.join(out_dir, "animation.gif"), trace, cm.snapshots, fleet.records, t_end,
                        frame_dt=0.5, fps=6, area=topo.area_size)
        summary["animation_frames"] = n
    return summary


def _node_side_transfer_stats(rounds, meas: dict, n_nodes: int, duration_s: float) -> dict:
    """Node-side leader-transfer duration T_U (per transfer round: c_meas * N / members) and the
    in-window unavailability (reconfiguration-attributable) as opposed to the total, which also
    contains leader losses unrelated to reconfiguration (spurious elections, failures)."""
    from .metrics import _stats
    recon = [r for r in rounds if r.round > 0 and r.reconfigured]
    cs = meas.get("c_round_measured_list_ms", [])
    tu = [c * n_nodes / r.n_transfer_member_nodes for c, r in zip(cs, recon)
          if r.leader_transfers > 0 and r.n_transfer_member_nodes > 0 and c > 0]
    inside = meas.get("unavail_node_seconds_total", 0.0) - meas.get("unavail_outside_reconf_s", 0.0)
    U_in = inside / (n_nodes * duration_s) if duration_s > 0 else 0.0
    return {"t_u_node_ms": _stats(tu), "unavailability_measured_reconf": U_in,
            "availability_measured_reconf": 1.0 - U_in}


def _jd(o):
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    return str(o)


# ---------------------------------------------------------------------------
# Real mode
# ---------------------------------------------------------------------------

def run_real(args, topo: Topology, out_dir: str, t_sync: Optional[float] = None,
             hysteresis: Optional[float] = None, dwell: Optional[float] = None, dyn_seed: Optional[int] = None,
             label: str = "real", animate: Optional[bool] = None, phase: Optional[float] = None) -> dict:
    """One real-time end-to-end run on a freshly started local Go cluster.

    The per-point parameters default to the CLI values; the sweep passes them
    explicitly so that every sweep point runs on its own clean cluster.
    """
    t_sync = args.tsync if t_sync is None else t_sync
    hysteresis = args.hysteresis if hysteresis is None else hysteresis
    dwell = args.dwell if dwell is None else dwell
    dyn_seed = args.dyn_seed if dyn_seed is None else dyn_seed
    animate = args.animate if animate is None else animate
    os.makedirs(out_dir, exist_ok=True)
    dyn = DynamicsConfig(duration_s=args.duration + 10, seed=dyn_seed, step_rate_per_min=args.step_rate,
                         jitter_sigma=args.jitter, warmup_s=args.warmup)
    trace = CoverageTrace(topo, dyn)
    print(f"[dynamics] {trace.summary()}")

    cluster_proc = LocalCluster(args.go_bin, os.path.join(out_dir, "data"), topo.nodes, args)
    cluster_proc.start()
    cc = ClusterClient(cluster_proc.node_cfgs)
    try:
        if not cc.wait_all_ready(60):
            raise RuntimeError("Go nodes did not become ready")
        print(f"[cluster] {len(topo.nodes)} nodes ready")
        naming = Naming(topo.nodes, cluster_proc.raft_addr)
        clock = Clock(virtual=False)
        executor = RealExecutor(cc, naming)
        cm = ClusterManager(trace, _cm_config(args, topo, t_sync, hysteresis, dwell), naming, executor, clock,
                            log_path=os.path.join(out_dir, "cm_events.jsonl"))
        executor.members_lookup = lambda cid: cm.serving.get(cid, set())
        rec0 = cm.bootstrap()
        print(f"[cm] bootstrap: {rec0.regions_after} regions, T_cfg={rec0.t_cfg_ms:.0f} ms, "
              f"serving={ {c: sorted(s) for c, s in cm.serving.items()} }")
        cm.refresh_leaders(cc)

        fleet = UAVFleet(trace, topo.spatial, UAVConfig(n_uavs=args.uavs, speed_mps=args.uav_speed,
                                                        handover_prob=args.handover_prob, stale_read=args.stale_read,
                                                        seed=args.uav_seed),
                         clock, cluster=cc, mec_id=LocalCluster.mec_id,
                         log_path=os.path.join(out_dir, "uav_records.jsonl"))
        load = (LoadGenerator(cc, clock, concurrency_per_node=args.load_conc, rate_per_worker=args.load_rate)
                if args.load_conc > 0 else None)
        fleet.run_real(args.duration)
        if load:
            load.start(args.duration)

        phase = _tick_phase(args, t_sync, dyn_seed) if phase is None else phase
        next_tick = clock.now() + t_sync * phase
        last_print = 0.0
        while clock.now() < args.duration:
            now = clock.now()
            if now < next_tick:
                time.sleep(min(next_tick - now, 0.02))
                continue
            r = cm.sync_round()
            if r.reconfigured:
                print(f"[cm] t={r.t_tick:7.2f} +{len(r.added_edges)} -{len(r.removed_edges)} edges | regions "
                      f"{r.regions_before}->{r.regions_after} kinds={r.kinds} | dirs={r.n_directives} elect={r.elections} "
                      f"mc={r.membership_changes} fail={r.failures} | T_part={r.t_part_ms:.1f} T_cfg={r.t_cfg_ms:.0f} "
                      f"T_joint={r.t_joint_ms:.0f} T_mig={r.t_migrate_ms:.0f} T_trans={r.t_trans_ms:.0f} ms ok={r.constraints_ok}")
            if clock.now() - last_print > 30:
                last_print = clock.now()
                s = fleet.summary()
                print(f"[uav] t={last_print:5.0f}s reads={s['reads']} consistent={s['consistent_rate']:.3f} "
                      f"handovers={s['handovers']} ho_inconsistent={s['handover_inconsistent']}")
            # fixed period; if a round overran, resume on the next period boundary
            now = clock.now()
            next_tick += t_sync
            if next_tick <= now:
                next_tick = (int(now / t_sync) + 1) * t_sync
        fleet.stop()
        if load:
            load.stop()
        time.sleep(1.0)
        t_end = min(clock.now(), args.duration)
        fleet.close()
        cm.close()
        summary = analyze(out_dir, topo, trace, cm, fleet, load.samples if load else [], t_end, t_sync,
                          title=f"MR-Raft CM end-to-end ({topo.name}, N={len(topo.nodes)}, T_sync={t_sync}s, "
                                f"h={hysteresis}m, dwell={dwell}s, {cm.cfg.batch_mode}, adv={cm.cfg.adversarial_leader})",
                          animate=animate, label=label, logs_dir=os.path.join(out_dir, "data", "logs"),
                          extra={"mode": "real"})
        summary["node_event_logs"] = os.path.join(out_dir, "data", "logs")
        return summary
    finally:
        cc.close()
        cluster_proc.stop()


# ---------------------------------------------------------------------------
# Sim mode
# ---------------------------------------------------------------------------

def run_sim_once(args, topo: Topology, out_dir: Optional[str], t_sync: float, hysteresis: float, dwell: float,
                 dyn_seed: int, label: str, animate: bool = False, do_analyze: bool = True,
                 phase: Optional[float] = None) -> dict:
    dyn = DynamicsConfig(duration_s=args.duration + 10, seed=dyn_seed, step_rate_per_min=args.step_rate,
                         jitter_sigma=args.jitter, warmup_s=args.warmup)
    trace = CoverageTrace(topo, dyn)
    naming = Naming(topo.nodes, lambda n: f"127.0.0.1:{30001 + n}")
    clock = Clock(virtual=True)
    model = ModelExecutor(naming, rtt_ms=args.model_rtt_ms, elect_ms=(args.model_elect_min_ms, args.model_elect_max_ms),
                          commit_ms=args.model_commit_ms, start_ms=args.model_start_ms, seed=dyn_seed,
                          transfer_ms=args.model_transfer_ms)
    cm = ClusterManager(trace, _cm_config(args, topo, t_sync, hysteresis, dwell),
                        naming, model, clock, log_path=os.path.join(out_dir, "cm_events.jsonl") if out_dir else None)
    cm.bootstrap()
    dp = ModelDataPlane(cm.serving_regions)
    fleet = UAVFleet(trace, topo.spatial, UAVConfig(n_uavs=args.uavs, speed_mps=args.uav_speed,
                                                    handover_prob=args.handover_prob, seed=args.uav_seed),
                     clock, cluster=None, model=dp)
    rng_state: dict = {}
    phase = _tick_phase(args, t_sync, dyn_seed) if phase is None else phase
    next_tick = t_sync * phase
    while next_tick < args.duration:
        fleet.run_sim_step(next_tick - 1e-9, rng_state)     # UAV steps up to the tick see the pre-round state
        clock.set(next_tick)
        cm.sync_round()
        now = clock.now()                                   # advanced by T_trans inside the round
        next_tick += t_sync
        if next_tick <= now:
            next_tick = (int(now / t_sync) + 1) * t_sync
    fleet.run_sim_step(args.duration, rng_state)
    if not do_analyze:
        return {"cm": cm, "fleet": fleet, "trace": trace}
    return analyze(out_dir, topo, trace, cm, fleet, [], args.duration, t_sync,
                   title=f"CM simulation ({topo.name}, T_sync={t_sync}s, h={hysteresis}m, dwell={dwell}s, "
                         f"{cm.cfg.batch_mode}, adv={cm.cfg.adversarial_leader})",
                   animate=animate, label=label, extra={"mode": "sim"})


def _cm_config(args, topo: Topology, t_sync: float, hysteresis: float, dwell: float) -> CMConfig:
    return CMConfig(t_sync_s=t_sync, tau=resolve_tau(args, topo), hysteresis_m=hysteresis, min_dwell_s=dwell,
                    batch_mode=args.batch_mode, adversarial_leader=args.adversarial_leader)


def resolve_tau(args, topo: Topology) -> int:
    """--tau N  -> fixed threshold;  --tau auto -> tau* by composite-score minimisation
    (alpha=0.4, beta=0.6, max-normalisation; thresholds 2..N as in the overlap study)."""
    if str(args.tau).lower() != "auto":
        return int(args.tau)
    cache = getattr(args, "_tau_cache", None)
    if cache is None:
        cache = args._tau_cache = {}
    if topo.name not in cache:
        from ..partition.optimizer import PartitionOptimizer
        G = topo.graph()
        tau, _, _ = PartitionOptimizer(G).find_optimal_threshold(
            threshold_range=list(range(2, max(G.number_of_nodes(), 3) + 1)), verbose=False)
        cache[topo.name] = int(tau)
    return cache[topo.name]


def _sweep_point(args, topo: Topology, sub: str, ts: float, h: float, dw: float, seed: int, label: str) -> dict:
    """Run (or resume) one sweep point. In real mode every point gets its own freshly
    started Go cluster, so points are independent and the sweep can be resumed."""
    done = os.path.join(sub, "summary.json")
    if args.resume and os.path.exists(done):
        with open(done) as f:
            summ = json.load(f)
        print(f"[sweep] resume {sub}")
        return summ
    os.makedirs(sub, exist_ok=True)
    if args.mode == "real":
        print(f"[sweep] === {label} T_sync={ts:g}s seed={seed} (real cluster, {args.duration:.0f}s) ===")
        return run_real(args, topo, sub, t_sync=ts, hysteresis=h, dwell=dw, dyn_seed=seed,
                        label=label, animate=False)
    return run_sim_once(args, topo, sub, ts, h, dw, seed, label, animate=False)


def _aggregate_point(per_seed: List[dict], label: str, ts: float, duration: float) -> dict:
    mins = duration / 60
    return {
        "label": label, "t_sync": ts, "n_runs": len(per_seed),
        "lag_mean": _mean([p["responsiveness"]["lag_s"].get("mean", np.nan) for p in per_seed]),
        "lag_p95": _mean([p["responsiveness"]["lag_s"].get("p95", np.nan) for p in per_seed]),
        "lag_unc_mean": _mean([p["responsiveness"]["lag_uncovered_s"].get("mean", np.nan) for p in per_seed]),
        "lag_unc_p95": _mean([p["responsiveness"]["lag_uncovered_s"].get("p95", np.nan) for p in per_seed]),
        "lag_unc_n": _mean([p["responsiveness"]["lag_uncovered_s"].get("n", 0) for p in per_seed]),
        "reconfig_per_min": _mean([p["overhead"]["reconfig_per_min"] for p in per_seed]),
        "elections_per_min": _mean([p["overhead"]["elections"] / mins for p in per_seed]),
        "membership_changes_per_min": _mean([p["overhead"]["membership_changes"] / mins for p in per_seed]),
        "directives_per_min": _mean([p["overhead"]["directives_per_min"] for p in per_seed]),
        "bytes_per_min": _mean([p["cm"]["bytes_cfg"] / mins for p in per_seed]),
        "failures": _mean([p["overhead"]["failures"] for p in per_seed]),
        "violation_frac": _mean([p["consistency"]["violation_time_fraction"] for p in per_seed]),
        "handover_inconsistent_rate": _mean([p["consistency"]["handover_inconsistent_rate"] for p in per_seed]),
        "overlap_handover_inconsistent_rate": _mean([p["consistency"]["overlap_handover_inconsistent_rate"] for p in per_seed]),
        "overlap_handover_errors": _mean([p["consistency"].get("overlap_handover_errors", 0) for p in per_seed]),
        "read_error_rate": _mean([p["consistency"].get("read_error_rate", 0) for p in per_seed]),
        "gap_reentries": _mean([p["consistency"]["gap_reentries"] for p in per_seed]),
        "t_trans_mean_ms": _mean([p["overhead"]["t_trans_ms"].get("mean", np.nan) for p in per_seed]),
        "t_trans_p95_ms": _mean([p["overhead"]["t_trans_ms"].get("p95", np.nan) for p in per_seed]),
        "t_trans_max_ms": _mean([p["overhead"]["t_trans_ms"].get("max", np.nan) for p in per_seed]),
        "load_fail": _mean([p.get("load", {}).get("fail", 0) for p in per_seed]),
    }


def run_sweep(args, topo: Topology, out_dir: str) -> dict:
    """T_sync sweep. --mode sim: analytical timing model (fast, many seeds);
    --mode real: every point is a live end-to-end run on a fresh local Go cluster."""
    tsyncs = [float(x) for x in args.sweep.split(",")]
    variants = [("no hysteresis", 0.0, 0.0)]
    if args.hysteresis > 0 or args.dwell > 0:
        variants.append((f"hysteresis {args.hysteresis:g} m / dwell {args.dwell:g} s", args.hysteresis, args.dwell))
    results = []
    results_path = os.path.join(out_dir, "sweep_results.json")
    for label, h, dw in variants:
        for ts in tsyncs:
            per_seed = []
            for s in range(args.seeds):
                sub = os.path.join(out_dir, "sweep_runs", f"{label.replace(' ', '_').replace('/', '-')}_ts{ts:g}_s{s}")
                per_seed.append(_sweep_point(args, topo, sub, ts, h, dw, args.dyn_seed + s, label))
            agg = _aggregate_point(per_seed, label, ts, args.duration)
            results.append(agg)
            print(f"[sweep] {label:32s} T_sync={ts:5.2f}s lag={agg['lag_mean']:.2f}s (p95 {agg['lag_p95']:.2f}) "
                  f"lag|uncovered={agg['lag_unc_mean']:.2f}s (p95 {agg['lag_unc_p95']:.2f}, n={agg['lag_unc_n']:.0f}) "
                  f"reconfig/min={agg['reconfig_per_min']:.1f} elect/min={agg['elections_per_min']:.1f} "
                  f"mc/min={agg['membership_changes_per_min']:.1f} fail={agg['failures']:.0f} "
                  f"viol={100*agg['violation_frac']:.2f}% ho_incons={100*agg['handover_inconsistent_rate']:.1f}% "
                  f"(overlap {100*agg['overlap_handover_inconsistent_rate']:.2f}%, read_err {100*agg['read_error_rate']:.2f}%) "
                  f"T_trans={agg['t_trans_mean_ms']:.0f}/{agg['t_trans_p95_ms']:.0f}/{agg['t_trans_max_ms']:.0f} ms")
            with open(results_path, "w") as f:      # checkpoint after every point
                json.dump(results, f, indent=2, default=_jd)
    plot_results = list(results)
    if args.overlay:
        # e.g. overlay the model sweep (lines) under the real-cluster points (markers)
        with open(args.overlay) as f:
            overlay = json.load(f)
        own_tag = " [cluster]" if args.mode == "real" else " [model]"
        other_tag = " [model]" if args.mode == "real" else " [cluster]"
        plot_results = [{**r, "label": r["label"] + own_tag} for r in results] + \
                       [{**r, "label": r["label"] + other_tag} for r in overlay]
    t_trans = max(r["t_trans_max_ms"] for r in results)
    viz.plot_sweep(os.path.join(out_dir, "sweep"), plot_results, t_trans_ms=t_trans, budget_s=args.budget_s)
    return {"sweep": results}


# ---------------------------------------------------------------------------
# rho sweep  (x: overlap ratio, y: availability cost / required T_sync)
# ---------------------------------------------------------------------------

def _struct_stats(topo: Topology, tau: int) -> dict:
    """Static structure of the threshold partition at this rho (analytic envelopes of c)."""
    from ..partition.strategies import PartitionStrategies
    G = topo.graph()
    n = G.number_of_nodes()
    regions = [set(r) for r in PartitionStrategies(G).threshold_based(tau) if len(r) >= 2]
    sizes = [len(r) for r in regions]
    return {
        "nodes": n, "edges": G.number_of_edges(), "rho": overlap_ratio(G), "tau": tau,
        "num_regions": len(regions),
        "avg_region_size": float(np.mean(sizes)) if sizes else 0.0,
        "max_region_size": max(sizes) if sizes else 0,
        "avg_region_frac": float(np.mean(sizes)) / n if sizes and n else 0.0,   # |R|/N of one region
        "avg_instances": sum(sizes) / n if n else 0.0,                          # I-bar = sum|R| / N
    }


def _rho_aggregate(per_seed: List[dict], rho: float, variant: str, duration: float, struct: dict) -> dict:
    mins = duration / 60
    av = [p["availability"] for p in per_seed]
    meas = [p.get("availability_measured") for p in per_seed]
    meas = [m for m in meas if m]

    def cstat(key, q):
        xs = [a["c_round_ms"].get(q) for a in av if a["c_round_ms"].get("n", 0) > 0]
        return _mean(xs) if xs else float("nan")

    def mstat(q):
        xs = [m["c_round_measured_ms"].get(q) for m in meas if m.get("c_round_measured_ms", {}).get("n", 0) > 0]
        return _mean(xs) if xs else float("nan")

    out = {
        "rho": rho, "rho_achieved": _mean([p["rho"] for p in per_seed]), "variant": variant, "n_runs": len(per_seed),
        "tau": per_seed[0]["tau"], "struct": struct,
        "reconfig_rounds": _mean([a["reconfig_rounds"] for a in av]),
        "reconfig_per_min": _mean([p["overhead"]["reconfig_per_min"] for p in per_seed]),
        "edge_events_per_min": _mean([p["dynamics"]["edge_events"] / max(p["dynamics"]["duration_s"] / 60, 1e-9)
                                      for p in per_seed]),
        "leader_transfers": _mean([a["leader_transfers"] for a in av]),
        "c_model_mean_ms": cstat("c_round_ms", "mean"), "c_model_p95_ms": cstat("c_round_ms", "p95"),
        "c_model_max_ms": cstat("c_round_ms", "max"),
        "c_meas_mean_ms": mstat("mean"), "c_meas_p95_ms": mstat("p95"), "c_meas_max_ms": mstat("max"),
        "U_model": _mean([a["unavailability_model"] for a in av]),
        "U_measured": _mean([m["unavailability_measured"] for m in meas]) if meas else float("nan"),
        "U_measured_reconf": _mean([m.get("unavailability_measured_reconf", np.nan) for m in meas]) if meas else float("nan"),
        "unavail_outside_reconf_s": _mean([m.get("unavail_outside_reconf_s", 0.0) for m in meas]) if meas else float("nan"),
        "t_u_node_ms_p95": _mean([m.get("t_u_node_ms", {}).get("p95", np.nan) for m in meas]) if meas else float("nan"),
        "t_u_node_ms_mean": _mean([m.get("t_u_node_ms", {}).get("mean", np.nan) for m in meas]) if meas else float("nan"),
        "scope_transfer_members_frac_mean": _mean([a["scope_transfer_members_frac"].get("mean", np.nan) for a in av]),
        "scope_reconf_nodes_mean": _mean([a["scope_reconf_nodes"].get("mean", np.nan) for a in av]),
        "overcoverage_final": _mean([a["overcoverage_size_ratio_final"] for a in av]),
        "t_trans_p95_ms": _mean([p["overhead"]["t_trans_ms"].get("p95", np.nan) for p in per_seed]),
        "t_trans_max_ms": _mean([p["overhead"]["t_trans_ms"].get("max", np.nan) for p in per_seed]),
        "lag_unc_p95": _mean([p["responsiveness"]["lag_uncovered_s"].get("p95", np.nan) for p in per_seed]),
        "violation_frac": _mean([p["consistency"]["violation_time_fraction"] for p in per_seed]),
        "leader_wait_ms_p95": _mean([p["cm"].get("leader_wait_ms", {}).get("p95", np.nan) for p in per_seed]),
        "leader_wait_ms_mean": _mean([p["cm"].get("leader_wait_ms", {}).get("mean", np.nan) for p in per_seed]),
        "failures": _mean([p["overhead"]["failures"] for p in per_seed]),
    }
    return out


def run_rho_analytic(args, base_topo: Topology, out_dir: str) -> dict:
    """Formula-only rho -> T_sync figure: structure of the threshold partition at every rho
    (no cluster, no simulation) x measured T_U; optional overlay of a measured sweep."""
    rhos = [float(x) for x in args.rho_sweep.split(",")]
    struct_by_rho: Dict[float, dict] = {}
    for rho in rhos:
        topo_r, scale, rho_ach = scale_to_rho(base_topo, rho, args.rho_margin)
        tau = resolve_tau(args, topo_r)
        st = _struct_stats(topo_r, tau)
        st["scale"] = scale
        struct_by_rho[rho] = st
        print(f"[rho-analytic] rho={rho:.2f} (achieved {rho_ach:.3f}) tau*={tau} regions={st['num_regions']} "
              f"|R|/N={st['avg_region_frac']:.3f} Ibar={st['avg_instances']:.3f} -> "
              f"c_one={st['avg_region_frac'] * args.model_transfer_ms:.3f} ms  "
              f"c_all={st['avg_instances'] * args.model_transfer_ms:.3f} ms")
    overlay = None
    if args.overlay:
        with open(args.overlay) as f:
            overlay = json.load(f)
    with open(os.path.join(out_dir, "rho_struct.json"), "w") as f:
        json.dump(struct_by_rho, f, indent=2, default=_jd)
    viz.plot_rho_analytic(os.path.join(out_dir, "rho_tsync_analytic"), struct_by_rho, args.model_transfer_ms,
                          args.budget_s, args.analytic_t_trans_ms, p_hit=args.analytic_p_hit, overlay=overlay,
                          title=f"analytic ({base_topo.name}, N={len(base_topo.nodes)}): threshold partition per rho "
                                f"x $T_U$={args.model_transfer_ms:g} ms; hollow markers = measured p95")
    return {"rho_struct": struct_by_rho}


def run_rho_sweep(args, base_topo: Topology, out_dir: str) -> dict:
    """For every rho: scale the coverage radii of the base topology (locations fixed), select tau*,
    run the CM pipeline (real cluster or timing model) under the configured churn for each
    adversarial-leader variant, and aggregate the availability cost c(rho) of one reconfiguration."""
    import copy
    rhos = [float(x) for x in args.rho_sweep.split(",")]
    variants = [v.strip() for v in args.adversarial_variants.split(",") if v.strip()]
    results = []
    results_path = os.path.join(out_dir, "rho_results.json")
    t_sync = args.tsync
    for rho in rhos:
        topo_r, scale, rho_ach = scale_to_rho(base_topo, rho, args.rho_margin)
        tau = resolve_tau(args, topo_r)
        struct = _struct_stats(topo_r, tau)
        struct["scale"] = scale
        print(f"[rho] target={rho:.2f} achieved={rho_ach:.3f} scale={scale:.3f} tau*={tau} "
              f"regions={struct['num_regions']} avg|R|={struct['avg_region_size']:.2f} Ibar={struct['avg_instances']:.2f}")
        for variant in variants:
            a2 = copy.copy(args)
            a2.adversarial_leader = variant
            a2.tau = str(tau)
            per_seed = []
            for s in range(args.seeds):
                sub = os.path.join(out_dir, "rho_runs", f"rho{rho:.2f}_{variant}_s{s}")
                label = f"rho={rho:.2f} {variant}"
                done = os.path.join(sub, "summary.json")
                if args.resume and os.path.exists(done):
                    with open(done) as f:
                        per_seed.append(json.load(f))
                    print(f"[rho] resume {sub}")
                    continue
                os.makedirs(sub, exist_ok=True)
                if args.mode == "real":
                    print(f"[rho] === rho={rho:.2f} adversarial={variant} seed={s} (real cluster, {args.duration:.0f}s) ===")
                    per_seed.append(run_real(a2, topo_r, sub, t_sync=t_sync, hysteresis=args.hysteresis,
                                             dwell=args.dwell, dyn_seed=args.dyn_seed + s, label=label, animate=False))
                else:
                    per_seed.append(run_sim_once(a2, topo_r, sub, t_sync, args.hysteresis, args.dwell,
                                                 args.dyn_seed + s, label, animate=False))
            agg = _rho_aggregate(per_seed, rho, variant, args.duration, struct)
            results.append(agg)
            print(f"[rho] rho={rho:.2f} {variant:5s} reconfig/min={agg['reconfig_per_min']:.1f} "
                  f"events/min={agg['edge_events_per_min']:.1f} transfers={agg['leader_transfers']:.1f} "
                  f"c_model p95={agg['c_model_p95_ms']:.3f} ms c_meas p95={agg['c_meas_p95_ms']:.3f} ms "
                  f"U_model={agg['U_model']:.2e} U_meas={agg['U_measured']:.2e} "
                  f"(reconf-only {agg['U_measured_reconf']:.2e}) T_U(node)={agg['t_u_node_ms_p95']:.2f} ms "
                  f"overcov={agg['overcoverage_final']:.2f} T_trans p95={agg['t_trans_p95_ms']:.0f} ms "
                  f"lag_unc p95={agg['lag_unc_p95']:.2f} s")
            with open(results_path, "w") as f:
                json.dump(results, f, indent=2, default=_jd)
    viz.plot_rho_tsync(os.path.join(out_dir, "rho_tsync"), results, budget_s=args.budget_s,
                       t_u_ms=args.model_transfer_ms, t_sync=t_sync, mode=args.mode)
    return {"rho_sweep": results}


def _tsync_for_rho(args, rho: float) -> Tuple[float, str]:
    """T_sync of an end-to-end point: --tsync-from-departure PATH takes T_sync^min(rho) (p95) from
    a node-departure study, otherwise --tsync. Returns (T_sync [s], source label)."""
    path = getattr(args, "tsync_from_departure", None)
    if path:
        with open(path) as f:
            rows = json.load(f)
        for r in rows:
            if abs(float(r["rho"]) - rho) < 1e-6 and r.get("t_sync_min_s", {}).get("p95") is not None:
                return float(r["t_sync_min_s"]["p95"]), "T_sync^min(rho)"
        raise ValueError(f"no T_sync^min for rho={rho} in {path}")
    return float(args.tsync), f"fixed {args.tsync:g} s"


def run_rho_e2e(args, base_topo: Topology, out_dir: str) -> dict:
    """End-to-end availability check under the paper's topology dynamics (coverage steps, UAVs,
    background load): for every rho the pipeline runs on a fresh cluster with T_sync either taken
    from the node-departure study (--tsync-from-departure) or fixed (--tsync), for
    clamp(--duration-periods x T_sync, --duration-min, --duration-max) seconds. Availability is
    read from the node logs over the whole run; lag, overhead and consistency come for free."""
    rhos = [float(x) for x in args.rho_sweep.split(",")]
    results_path = os.path.join(out_dir, "e2e_results.json")
    by_key: Dict[str, dict] = {}
    if os.path.exists(results_path):
        with open(results_path) as f:
            for r in json.load(f):
                by_key[r["key"]] = r
    results: List[dict] = []
    for rho in rhos:
        t_sync, src = _tsync_for_rho(args, rho)
        duration = args.duration
        if args.duration_periods > 0:
            duration = float(min(max(args.duration_periods * t_sync, args.duration_min), args.duration_max))
        key = f"rho{rho:.2f}_ts{t_sync:.3g}"
        sub = os.path.join(out_dir, "runs", key)
        done = os.path.join(sub, "summary.json")
        topo_r, scale, rho_ach = scale_to_rho(base_topo, rho, args.rho_margin)
        tau = resolve_tau(args, topo_r)
        if args.resume and os.path.exists(done):
            with open(done) as f:
                summ = json.load(f)
            print(f"[e2e] resume {key}")
        else:
            import copy
            a2 = copy.copy(args)
            a2.tau = str(tau)
            a2.duration = duration
            a2.adversarial_leader = "none"
            print(f"[e2e] === rho={rho:.2f} (achieved {rho_ach:.3f}, x{scale:.3f}) tau*={tau} T_sync={t_sync:.3g} s "
                  f"({src}) duration={duration:.0f} s ({duration / t_sync:.1f} periods) ===")
            os.makedirs(sub, exist_ok=True)
            if args.mode == "real":
                summ = run_real(a2, topo_r, sub, t_sync=t_sync, hysteresis=args.hysteresis, dwell=args.dwell,
                                dyn_seed=args.dyn_seed, label=f"rho={rho:.2f} T_sync={t_sync:.3g}", animate=False)
            else:
                summ = run_sim_once(a2, topo_r, sub, t_sync, args.hysteresis, args.dwell, args.dyn_seed,
                                    f"rho={rho:.2f} T_sync={t_sync:.3g}", animate=False)
        am = summ.get("availability_measured") or {}
        if not am:                                  # timing model (no node logs): model accounting
            av = summ["availability"]
            am = {"unavailability_measured": av.get("unavailability_model"),
                  "availability_measured": av.get("availability_model"),
                  "unavailability_measured_reconf": av.get("unavailability_model"),
                  "unavail_outside_reconf_s": 0.0}
        cons, resp, ov, cm_s = summ["consistency"], summ["responsiveness"], summ["overhead"], summ["cm"]
        row = {
            "key": key, "rho": rho, "rho_achieved": rho_ach, "scale": scale, "tau": tau,
            "t_sync": t_sync, "t_sync_source": src, "duration_s": summ["duration_s"],
            "periods": summ["duration_s"] / t_sync,
            "U_measured": am.get("unavailability_measured"), "A_measured": am.get("availability_measured"),
            "U_measured_reconf": am.get("unavailability_measured_reconf"),
            "unavail_outside_reconf_s": am.get("unavail_outside_reconf_s"),
            "source": "node logs" if summ.get("availability_measured") else "timing model",
            "U_model": summ["availability"].get("unavailability_model"),
            "reconfig_per_min": ov.get("reconfig_per_min"), "membership_changes": ov.get("membership_changes"),
            "leader_transfers": cm_s.get("leader_transfers"), "elections": ov.get("elections"),
            "edge_changes": ov.get("edge_changes_processed"),
            "lag_mean_s": resp.get("lag_s", {}).get("mean"), "lag_p95_s": resp.get("lag_s", {}).get("p95"),
            "violation_time_fraction": cons.get("violation_time_fraction"),
            "handover_inconsistent_rate": cons.get("handover_inconsistent_rate"),
            "overlap_handover_inconsistent_rate": cons.get("overlap_handover_inconsistent_rate"),
            "read_error_rate": cons.get("read_error_rate"),
            "load_ok": summ.get("load", {}).get("ok"), "load_fail": summ.get("load", {}).get("fail"),
            "failures": cm_s.get("failures"),
        }
        u = row["U_measured"] or 0.0
        f0 = lambda v, fmt: (fmt % v) if v is not None else "n/a"
        print(f"[e2e] {key}: A={100 * (1 - u):.5f}% (U={u:.2e}, reconf-only {f0(row['U_measured_reconf'], '%.2e')}, "
              f"outside {f0(row['unavail_outside_reconf_s'], '%.3f')} node-s) | reconfig/min={f0(row['reconfig_per_min'], '%.2f')} "
              f"transfers={row['leader_transfers']} edge changes={row['edge_changes']} | lag p95={f0(row['lag_p95_s'], '%.2f')} s "
              f"violation={f0(100 * (row['violation_time_fraction'] or 0), '%.2f')}% overlap-handover-inconsistent="
              f"{f0(100 * (row['overlap_handover_inconsistent_rate'] or 0), '%.2f')}% "
              f"read_err={f0(100 * (row['read_error_rate'] or 0), '%.2f')}% "
              f"load fail={row['load_fail']}/{(row['load_ok'] or 0) + (row['load_fail'] or 0)}")
        by_key[key] = row
        results = sorted(by_key.values(), key=lambda r: (r["t_sync_source"], r["rho"], r["t_sync"]))
        with open(results_path, "w") as f:
            json.dump(results, f, indent=2, default=_jd)
        viz.plot_rho_e2e(os.path.join(out_dir, "e2e_availability"), results, a=args.availability)
    return {"rho_e2e": results}


def _tick_phase(args, t_sync: float, seed: int) -> float:
    """Fraction of T_sync before the first CM tick. The CM clock has an arbitrary phase
    relative to topology events in practice; --tick-phase random samples it per point so
    that a sweep does not align every T_sync with the same event (e.g. 5/10/20 s all ticking
    at t=60 s for an event at t=57 s)."""
    if args.tick_phase == "random":
        rng = np.random.default_rng([seed, int(round(t_sync * 1000))])
        return float(rng.uniform(0.05, 1.0))
    return float(args.tick_phase)


def _mean(xs):
    xs = [x for x in xs if x is not None and not (isinstance(x, float) and np.isnan(x))]
    return float(np.mean(xs)) if xs else float("nan")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="MR-Raft Cluster Manager end-to-end pipeline")
    p.add_argument("--mode", choices=["real", "sim"], default="real")
    p.add_argument("--topology", choices=["real", "synthetic"], default="real")
    p.add_argument("--lambda-bs", type=float, default=4e-6)
    p.add_argument("--topo-seed", type=int, default=56)
    p.add_argument("--out", default=None)
    p.add_argument("--duration", type=float, default=180.0)
    p.add_argument("--tsync", type=float, default=1.0)
    p.add_argument("--tau", default="5", help="merge threshold, or 'auto' = tau* by composite score (alpha=0.4)")
    p.add_argument("--hysteresis", type=float, default=0.0, help="hysteresis margin h (m) on overlap detection")
    p.add_argument("--dwell", type=float, default=0.0, help="minimum dwell time (s) before accepting an edge flip")
    p.add_argument("--budget-s", type=float, default=None, help="reconfiguration budget T_budget (s) for sweep plots")
    p.add_argument("--batch-mode", choices=["sequential", "recompute"], default="sequential",
                   help="sequential: net changes applied one by one with Algorithm 3/4, one reconfiguration "
                        "(paper semantics); recompute: local re-partition of the affected subgraph")
    p.add_argument("--adversarial-leader", choices=["none", "one", "all"], default="none",
                   help="force leader transfers in affected regions per round (availability cost envelope)")
    # rho study
    p.add_argument("--rho", type=float, default=None,
                   help="scale coverage radii (locations fixed) to this overlap ratio before running")
    p.add_argument("--rho-margin", type=float, default=0.05,
                   help="extra radius inflation applied when the target graph is complete (rho=1)")
    p.add_argument("--rho-sweep", default=None,
                   help="comma-separated overlap ratios, e.g. 0.1,0.2,...,1.0; runs the rho -> T_sync study")
    p.add_argument("--adversarial-variants", default="none,one,all",
                   help="rho sweep: adversarial-leader variants to run per rho")
    p.add_argument("--rho-e2e", action="store_true",
                   help="per rho (--rho-sweep): end-to-end run under the topology dynamics with T_sync from "
                        "--tsync-from-departure (or --tsync); availability from the node logs")
    p.add_argument("--tsync-from-departure", default=None,
                   help="rho-e2e: departure_results.json; T_sync(rho) = its T_sync^min p95")
    p.add_argument("--duration-periods", type=float, default=0.0,
                   help="rho-e2e: run for this many periods (clamped to --duration-min/--duration-max); 0 = --duration")
    p.add_argument("--duration-min", type=float, default=300.0, help="rho-e2e: minimum run length [s]")
    p.add_argument("--duration-max", type=float, default=1800.0, help="rho-e2e: maximum run length [s]")
    p.add_argument("--rho-analytic", action="store_true",
                   help="with --rho-sweep: formula-only figure (partition structure x --model-transfer-ms), no runs")
    p.add_argument("--analytic-t-trans-ms", type=float, default=200.0,
                   help="rho-analytic: T_trans used for the mechanism and consistency bounds")
    p.add_argument("--analytic-p-hit", type=float, default=0.003,
                   help="rho-analytic: measured probability that a removed member is the leader")
    # node-departure study (controlled perturbation instead of churn; see departure.py)
    p.add_argument("--node-departure", action="store_true",
                   help="per rho (--rho-sweep): hub-departure cycles with the leader on the departing node; "
                        "c(rho) -> T_sync^min = c_p95 / (1 - A)")
    p.add_argument("--cycles", type=int, default=20, help="node-departure: departure cycles per rho (fresh cluster each)")
    p.add_argument("--settle-s", type=float, default=2.0,
                   help="node-departure: settle time after leader placement and after the departure round")
    p.add_argument("--availability", type=float, default=0.9999, help="node-departure: availability target A")
    p.add_argument("--t-trans-s", default=None,
                   help="node-departure figure: T_trans lower bound [s] as a number, or 'auto' = --t-trans-q "
                        "percentile of the measured reconfiguration rounds (this run + --t-trans-from)")
    p.add_argument("--t-trans-from", default="",
                   help="comma-separated result dirs whose reconfiguration rounds enter --t-trans-s auto")
    p.add_argument("--t-trans-q", type=float, default=99.0, help="--t-trans-s auto: percentile (default p99)")
    p.add_argument("--fig-title", action="store_true", help="node-departure figure: draw the descriptive title")
    p.add_argument("--fig-yscale", choices=["linear", "log"], default="log", help="node-departure figure y axis")
    p.add_argument("--rho-survey", default="",
                   help="node-departure figure: rho_survey.csv (src.cm.rho_survey) -> shaded p5–p95 band of "
                        "the overlap ratio measured on real OpenCelliD deployments")
    p.add_argument("--departure-node", type=int, default=None,
                   help="node-departure / verify-tsync: force the departing graph node (default: hub = argmax sum |R|)")
    # closed-loop verification of 1 - A = c / T_sync (see verify.py)
    p.add_argument("--verify-tsync", action="store_true",
                   help="per rho (--rho-sweep) and T_sync (--verify-tsyncs): continuous hub departure / rejoin "
                        "on one cluster, A measured from node logs and compared with c(rho) / (2 T_sync)")
    p.add_argument("--verify-tsyncs", default=None,
                   help="verify-tsync: comma-separated T_sync values (default: --tsync)")
    p.add_argument("--periods", type=int, default=100,
                   help="verify-tsync: periods per point (departure / rejoin alternate; even number recommended)")
    p.add_argument("--departure-results", default=None,
                   help="verify-tsync: departure_results.json of the node-departure study (c(rho) for the model lines)")
    p.add_argument("--verify-schedule", choices=["hub", "nodes", "allleaders", "random"], default="hub",
                   help="verify-tsync: hub = worst node departs/rejoins with the leader on it (bound check); "
                        "nodes = every node in turn with the leader on it (c per node); allleaders = all current "
                        "leaders depart in the same round (per-round worst case); random = Poisson churn with "
                        "natural leaders (typical)")
    p.add_argument("--churn-rate", type=float, default=1.0,
                   help="verify-tsync random: expected node departures per period (all nodes present)")
    p.add_argument("--dwell-periods", type=float, default=2.0,
                   help="verify-tsync random: mean absence of a departed node, in periods")
    p.add_argument("--churn-seed", type=int, default=7, help="verify-tsync random: RNG seed (+ 1000*rho)")
    p.add_argument("--verify-load", action="store_true",
                   help="verify-tsync: run a Propose load on the staying nodes (--load-conc workers/node, "
                        "--load-rate ops/s) for a request-based cross-check of the time-based availability")
    p.add_argument("--load-deadline-ms", type=float, default=100.0,
                   help="verify-tsync with --verify-load: a request slower than this counts as unavailable")
    # dynamics
    p.add_argument("--dyn-seed", type=int, default=3)
    p.add_argument("--step-rate", type=float, default=0.6, help="power-adjustment events per BS per minute")
    p.add_argument("--jitter", type=float, default=0.015, help="AR(1) radius jitter std (fraction of r0)")
    p.add_argument("--warmup", type=float, default=5.0)
    # UAV
    p.add_argument("--uavs", type=int, default=12)
    p.add_argument("--uav-speed", type=float, default=25.0)
    p.add_argument("--handover-prob", type=float, default=0.3)
    p.add_argument("--uav-seed", type=int, default=11)
    p.add_argument("--stale-read", action="store_true", help="use local StaleRead instead of ReadIndex")
    p.add_argument("--load-conc", type=int, default=1, help="background load workers per node (0 = off)")
    p.add_argument("--load-rate", type=float, default=20.0, help="ops/s per load worker (0 = closed loop)")
    # Go cluster
    p.add_argument("--go-bin", default=DEFAULT_GO_BIN)
    p.add_argument("--disk-sm", action="store_true")
    p.add_argument("--tick-ms", type=int, default=50)
    p.add_argument("--election-rtt", type=int, default=3)
    p.add_argument("--heartbeat-rtt", type=int, default=1)
    p.add_argument("--propose-timeout-ms", type=int, default=2000)
    p.add_argument("--delay-us", type=int, default=0)
    p.add_argument("--gomaxprocs", type=int, default=2, help="GOMAXPROCS per Go node (limits oversubscription)")
    p.add_argument("--no-memfs", action="store_true", help="keep Raft WAL on disk (default: in-memory FS for local runs)")
    # sim model (defaults = measured on the local cluster)
    p.add_argument("--model-rtt-ms", type=float, default=1.5)
    p.add_argument("--model-elect-min-ms", type=float, default=150)
    p.add_argument("--model-elect-max-ms", type=float, default=300)
    p.add_argument("--model-commit-ms", type=float, default=9.0)
    p.add_argument("--model-start-ms", type=float, default=12.0)
    p.add_argument("--model-transfer-ms", type=float, default=3.0,
                   help="leader-transfer wait T_U used by the timing model and the analytic envelopes")
    p.add_argument("--sweep", default=None,
                   help="comma-separated T_sync values; with --mode real each point is a live run on a fresh cluster")
    p.add_argument("--seeds", type=int, default=1)
    p.add_argument("--resume", action="store_true", help="sweep: skip points whose summary.json already exists")
    p.add_argument("--tick-phase", default="1.0",
                   help="fraction of T_sync before the first CM tick, or 'random' (seeded per point; use for sweeps)")
    p.add_argument("--overlay", default=None,
                   help="sweep: another sweep_results.json to draw alongside (e.g. model sweep under real points)")
    p.add_argument("--animate", action="store_true")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    topo = load_topology(args.topology, REPO_ROOT, seed=args.topo_seed, lambda_bs=args.lambda_bs)
    print(f"[topology] {topo.name}: {len(topo.nodes)} nodes, {topo.graph().number_of_edges()} edges, "
          f"rho={overlap_ratio(topo.graph()):.3f}")
    if args.rho is not None and not args.rho_sweep:
        topo, scale, rho = scale_to_rho(topo, args.rho, args.rho_margin)
        print(f"[topology] scaled radii x{scale:.3f} -> rho={rho:.3f}, {topo.graph().number_of_edges()} edges")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = args.out or os.path.join(REPO_ROOT, "output", "cm_e2e", f"{args.mode}_{topo.name}_{stamp}")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "args.json"), "w") as f:
        json.dump({k: v for k, v in vars(args).items() if not k.startswith("_")}, f, indent=2)
    if str(args.tau).lower() == "auto" and not args.rho_sweep:
        print(f"[cm] tau* = {resolve_tau(args, topo)}")
    if args.verify_tsync:
        from .verify import run_verify
        summary = run_verify(args, topo, out_dir)
    elif args.node_departure:
        from .departure import run_node_departure
        summary = run_node_departure(args, topo, out_dir)
    elif args.rho_sweep and args.rho_e2e:
        summary = run_rho_e2e(args, topo, out_dir)
    elif args.rho_sweep and args.rho_analytic:
        summary = run_rho_analytic(args, topo, out_dir)
    elif args.rho_sweep:
        summary = run_rho_sweep(args, topo, out_dir)
    elif args.sweep:
        summary = run_sweep(args, topo, out_dir)
    elif args.mode == "real":
        summary = run_real(args, topo, out_dir)
    else:
        summary = run_sim_once(args, topo, out_dir, args.tsync, args.hysteresis, args.dwell, args.dyn_seed,
                               "sim", animate=args.animate)
    print(json.dumps({k: v for k, v in summary.items()
                      if k not in ("sweep", "rho_sweep", "rho_struct", "node_departure", "verify", "rho_e2e")},
                     indent=2, default=_jd))
    print(f"[done] outputs in {out_dir}")


if __name__ == "__main__":
    main()

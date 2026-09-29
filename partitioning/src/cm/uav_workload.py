"""
UAV fleet workload.

Each UAV follows a random-waypoint trajectory (UAVTrajectoryGenerator, 25 m/s
by default). At every step it
  1. determines A_u(t) = MEC nodes whose *current* coverage disk contains it
     (time-varying radii from the CoverageTrace, i.e. ground truth),
  2. keeps its serving node if still reachable, otherwise hands over to the
     nearest reachable node; inside an overlap area it additionally switches
     with probability ``handover_prob`` (signal fluctuation / access selection),
  3. reads its own state key ``uav-<id>`` from the (possibly new) serving node
     with a linearizable ReadIndex read and checks that the value written in the
     previous step is visible  (read-your-writes across handover),
  4. writes the new state (value = step index, Ver = step index) through the
     serving node, which replicates it to all regions the node belongs to.

Outcomes per read: ``consistent`` (previous value visible), ``stale`` (older
value), ``missing`` (key not found), ``error`` (RPC/quorum failure) and
``no_coverage`` steps. Every record carries the (from, to) serving pair so the
metrics module can attribute inconsistencies to coverage-constraint windows.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np

from ..simulation.uav_handover import UAVTrajectoryGenerator
from .client import ClusterClient
from .dynamics import CoverageTrace


@dataclass
class UAVConfig:
    n_uavs: int = 12
    speed_mps: float = 25.0
    step_s: float = 0.5
    handover_prob: float = 0.3
    seed: int = 11
    stale_read: bool = False          # compare: local StaleRead instead of ReadIndex
    mobility: str = "random_waypoint" # or "gauss_markov"
    rpc_timeout_s: float = 3.0


@dataclass
class UAVRecord:
    t: float
    uav: int
    step: int
    x: float
    y: float
    serving: int
    prev_serving: int
    handover: bool
    n_reachable: int
    read_outcome: str            # consistent | stale | missing | error | none
    read_value: str
    read_latency_ms: float
    write_ok: bool
    write_latency_ms: float
    err: str = ""


class ModelDataPlane:
    """Sim-mode stand-in for the cluster: a read succeeds iff the writer node and
    the reader node currently share a *serving* region (coverage constraint)."""

    def __init__(self, serving_fn: Callable[[], Dict[int, Set[int]]]):
        self.serving_fn = serving_fn
        self.store: Dict[str, tuple] = {}     # key -> (value, writer node)

    def write(self, node: int, key: str, val: str):
        self.store[key] = (val, node)
        return True, 0.5

    def read(self, node: int, key: str):
        if key not in self.store:
            return False, "", 0.5
        val, writer = self.store[key]
        if writer == node:
            return True, val, 0.5
        for members in self.serving_fn().values():
            if node in members and writer in members:
                return True, val, 0.5
        return False, "", 0.5


class UAVFleet:
    def __init__(self, trace: CoverageTrace, spatial, cfg: UAVConfig,
                 clock, cluster: Optional[ClusterClient] = None,
                 mec_id: Callable[[int], int] = lambda n: n + 1,
                 model: Optional[ModelDataPlane] = None,
                 log_path: Optional[str] = None):
        self.trace = trace
        self.cfg = cfg
        self.clock = clock
        self.cluster = cluster
        self.model = model
        self.mec_id = mec_id
        self.records: List[UAVRecord] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._log = open(log_path, "w") if log_path else None
        self.trajectories = self._make_trajectories(spatial)

    # ------------------------------------------------------------------ setup
    def _make_trajectories(self, spatial):
        trajs = {}
        for u in range(self.cfg.n_uavs):
            gen = UAVTrajectoryGenerator(spatial, speed_mps=self.cfg.speed_mps,
                                         sample_interval_ms=int(self.cfg.step_s * 1000),
                                         random_seed=self.cfg.seed * 1000 + u)
            if self.cfg.mobility == "gauss_markov":
                pts = gen.gauss_markov(self.trace.cfg.duration_s + 5)
            else:
                pts = gen.random_waypoint(self.trace.cfg.duration_s + 5, pause_s=2.0)
            trajs[u] = [(p.time_ms / 1000.0, float(p.x), float(p.y)) for p in pts]
        return trajs

    def position(self, u: int, t: float):
        pts = self.trajectories[u]
        k = min(int(t / self.cfg.step_s), len(pts) - 1)
        return pts[k][1], pts[k][2]

    # -------------------------------------------------------------- serving
    def _choose_serving(self, rng, prev: int, reachable: List[int], x: float, y: float):
        if not reachable:
            return -1
        if prev in reachable and (len(reachable) == 1 or rng.random() >= self.cfg.handover_prob):
            return prev
        # nearest reachable (signal strength proxy), excluding prev when switching
        cands = [n for n in reachable if n != prev] or reachable
        d = [np.hypot(self.trace.pos[self.trace.idx[n], 0] - x,
                      self.trace.pos[self.trace.idx[n], 1] - y) for n in cands]
        return cands[int(np.argmin(d))]

    # ------------------------------------------------------------------ run
    def run_real(self, duration_s: float):
        """Real mode: one thread per UAV paced by the wall clock."""
        threads = [threading.Thread(target=self._uav_loop, args=(u, duration_s), daemon=True)
                   for u in range(self.cfg.n_uavs)]
        for th in threads:
            th.start()
        return threads

    def stop(self):
        self._stop.set()

    def _uav_loop(self, u: int, duration_s: float):
        rng = np.random.default_rng(self.cfg.seed * 7919 + u)
        step = 0
        prev_serving = -1
        last_written = None
        key = f"uav-{u}"
        next_t = 0.0
        while not self._stop.is_set():
            now = self.clock.now()
            if now >= duration_s:
                break
            if now < next_t:
                time.sleep(min(next_t - now, 0.05))
                continue
            t = now
            x, y = self.position(u, t)
            reachable = self.trace.covering_nodes(x, y, t)
            serving = self._choose_serving(rng, prev_serving, reachable, x, y)
            rec = UAVRecord(t=t, uav=u, step=step, x=x, y=y, serving=serving, prev_serving=prev_serving,
                            handover=(serving != prev_serving and prev_serving != -1 and serving != -1),
                            n_reachable=len(reachable), read_outcome="none", read_value="",
                            read_latency_ms=0.0, write_ok=False, write_latency_ms=0.0)
            if serving == -1:
                rec.read_outcome = "no_coverage"
            else:
                self._do_step(rec, key, step, serving, last_written)
                prev_serving = serving
                if rec.write_ok:
                    last_written = step
            self._record(rec)
            step += 1
            next_t += self.cfg.step_s

    def _do_step(self, rec: UAVRecord, key: str, step: int, serving: int, last_written):
        """Read the last successfully written state (version = last_written) then write step."""
        expected = last_written
        node = self.mec_id(serving)
        # -- read previous state
        if expected is not None:
            if self.cluster is not None:
                r = self.cluster[node].sync_read(key, stale=self.cfg.stale_read, timeout_s=self.cfg.rpc_timeout_s)
                rec.read_latency_ms = r.latency_ms
                if r.error and not r.found:
                    rec.read_outcome = "error" if "hosts no region" not in r.error and "not found" not in r.error.lower() else "missing"
                    rec.err = r.error[:120]
                elif not r.found:
                    rec.read_outcome = "missing"
                else:
                    rec.read_value = r.value
                    try:
                        got = int(r.value.split(":")[0])
                    except ValueError:
                        got = -1
                    rec.read_outcome = "consistent" if got >= expected else "stale"
            else:
                found, val, lat = self.model.read(serving, key)
                rec.read_latency_ms = lat
                if not found:
                    rec.read_outcome = "missing"
                else:
                    rec.read_value = val
                    rec.read_outcome = "consistent" if int(val.split(":")[0]) >= expected else "stale"
        # -- write new state
        val = f"{step}:{rec.uav}:{rec.t:.3f}"
        if self.cluster is not None:
            w = self.cluster[node].propose(key, val, ver=step + 1, client_id=rec.uav + 1,
                                           request_id=step, timeout_s=self.cfg.rpc_timeout_s)
            rec.write_ok, rec.write_latency_ms = w.success, w.latency_ms
            if not w.success and not rec.err:
                rec.err = w.error[:120]
        else:
            rec.write_ok, rec.write_latency_ms = self.model.write(serving, key, val)

    # ------------------------------------------------------------ sim mode
    def run_sim_step(self, t: float, rng_state: dict):
        """Sim mode: advance every UAV whose next step time <= t (called by the sim loop)."""
        for u in range(self.cfg.n_uavs):
            st = rng_state.setdefault(u, {"rng": np.random.default_rng(self.cfg.seed * 7919 + u),
                                          "step": 0, "prev": -1, "next_t": 0.0, "last_written": None})
            while st["next_t"] <= t:
                tt = st["next_t"]
                x, y = self.position(u, tt)
                reachable = self.trace.covering_nodes(x, y, tt)
                serving = self._choose_serving(st["rng"], st["prev"], reachable, x, y)
                rec = UAVRecord(t=tt, uav=u, step=st["step"], x=x, y=y, serving=serving, prev_serving=st["prev"],
                                handover=(serving != st["prev"] and st["prev"] != -1 and serving != -1),
                                n_reachable=len(reachable), read_outcome="none", read_value="",
                                read_latency_ms=0.0, write_ok=False, write_latency_ms=0.0)
                if serving == -1:
                    rec.read_outcome = "no_coverage"
                else:
                    self._do_step(rec, f"uav-{u}", st["step"], serving, st["last_written"])
                    st["prev"] = serving
                    if rec.write_ok:
                        st["last_written"] = st["step"]
                self._record(rec)
                st["step"] += 1
                st["next_t"] += self.cfg.step_s

    # --------------------------------------------------------------- records
    def _record(self, rec: UAVRecord):
        with self._lock:
            self.records.append(rec)
            if self._log:
                self._log.write(json.dumps(rec.__dict__) + "\n")

    def close(self):
        if self._log:
            self._log.close()

    def summary(self) -> dict:
        recs = [r for r in self.records if r.read_outcome not in ("none", "no_coverage")]
        n = len(recs)
        by = {}
        for r in recs:
            by[r.read_outcome] = by.get(r.read_outcome, 0) + 1
        ho = [r for r in recs if r.handover]
        ho_bad = sum(1 for r in ho if r.read_outcome != "consistent")
        writes = [r for r in self.records if r.serving != -1]
        return {
            "steps": len(self.records), "reads": n, "outcomes": by,
            "consistent_rate": by.get("consistent", 0) / n if n else 0.0,
            "handovers": len(ho), "handover_inconsistent": ho_bad,
            "handover_inconsistent_rate": ho_bad / len(ho) if ho else 0.0,
            "write_ok_rate": sum(1 for r in writes if r.write_ok) / len(writes) if writes else 0.0,
            "read_latency_ms_mean": float(np.mean([r.read_latency_ms for r in recs])) if recs else 0.0,
            "write_latency_ms_mean": float(np.mean([r.write_latency_ms for r in writes if r.write_ok])) if writes else 0.0,
        }


class LoadGenerator:
    """Background Propose load on every node (per-second success/failure samples)."""

    def __init__(self, cluster: ClusterClient, clock, concurrency_per_node: int = 2,
                 payload: int = 128, timeout_s: float = 3.0, rate_per_worker: float = 0.0,
                 nodes: Optional[Sequence[int]] = None, record_requests: bool = False):
        """nodes: subset of nodes to load (default all); record_requests: keep every
        (t, latency_ms, success) tuple in ``requests`` (deadline-based availability)."""
        self.cluster = cluster
        self.clock = clock
        self.conc = concurrency_per_node
        self.payload = "L" * payload
        self.timeout = timeout_s
        self.interval = 1.0 / rate_per_worker if rate_per_worker > 0 else 0.0   # 0 = closed loop
        self.nodes = list(nodes) if nodes is not None else list(self.cluster.nodes)
        self.record = record_requests
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.ok = 0
        self.fail = 0
        self.samples: List[dict] = []     # per second: {t, ok, fail, lat_ms}
        self.requests: List[tuple] = []   # (t, latency_ms, success, node, error) if record_requests
        self._lat: List[float] = []
        self._threads: List[threading.Thread] = []

    def start(self, duration_s: float):
        ths = []
        for nid in self.nodes:
            for w in range(self.conc):
                th = threading.Thread(target=self._worker, args=(nid, w, duration_s), daemon=True)
                th.start()
                ths.append(th)
        th = threading.Thread(target=self._sampler, args=(duration_s,), daemon=True)
        th.start()
        ths.append(th)
        self._threads = ths
        return ths

    def stop(self, join_s: float = 1.0):
        """Signal the workers and wait (bounded) for them, so that the cluster client can be
        closed afterwards without in-flight requests hitting a closed channel."""
        self._stop.set()
        deadline = time.time() + join_s
        for th in self._threads:
            th.join(timeout=max(0.0, deadline - time.time()))

    def _worker(self, nid: int, w: int, duration_s: float):
        i = 0
        next_t = self.clock.now()
        while not self._stop.is_set() and self.clock.now() < duration_s:
            if self.interval > 0:
                now = self.clock.now()
                if now < next_t:
                    time.sleep(min(next_t - now, 0.05))
                    continue
                next_t += self.interval
            t_req = self.clock.now()
            try:
                r = self.cluster[nid].propose(f"load-{nid}-{w}", f"{i}:{self.payload}", client_id=1000 + nid,
                                              request_id=i, timeout_s=self.timeout)
            except Exception:                       # e.g. channel closed during shutdown
                if self._stop.is_set():
                    break
                raise
            with self._lock:
                if r.success:
                    self.ok += 1
                    self._lat.append(r.latency_ms)
                else:
                    self.fail += 1
                if self.record:
                    self.requests.append((t_req, r.latency_ms, bool(r.success), nid,
                                          "" if r.success else str(getattr(r, "error", ""))[:120]))
            i += 1

    def _sampler(self, duration_s: float):
        last_ok = last_fail = 0
        next_t = 1.0
        while not self._stop.is_set() and self.clock.now() < duration_s:
            now = self.clock.now()
            if now < next_t:
                time.sleep(min(next_t - now, 0.05))
                continue
            with self._lock:
                ok, fail = self.ok, self.fail
                lat = float(np.mean(self._lat)) if self._lat else 0.0
                self._lat.clear()
            self.samples.append({"t": next_t, "ok": ok - last_ok, "fail": fail - last_fail, "lat_ms": lat})
            last_ok, last_fail = ok, fail
            next_t += 1.0

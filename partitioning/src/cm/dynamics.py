"""
Time-varying coverage model  ->  spatial coverage graph G(t) and edge events.

Physical model (ground truth)
-----------------------------
A MEC node m_i serves a set of base stations; its effective region is the union
of their coverage disks (paper Eq. (1)). Positions are fixed; the disks of node
i are scaled by a common time-varying factor

    s_i(t) = 1 + level_i(t) + eps_i(t),        r_{i,b}(t) = clip(r_{i,b}^0 * s_i(t))

* ``level_i(t)``  piecewise-constant power-adjustment level (Poisson arrivals,
  linear ramp to the new target) -> *real* topology changes (energy saving,
  CCO/SON tilt or power tuning, ...)
* ``eps_i(t)``    small AR(1) jitter (shadowing / interference) -> boundary
  flapping of node pairs whose overlap margin is close to zero -> *transient* edges

Two MEC nodes overlap iff any pair of their disks intersects, so the overlap
margin of a node pair is  m_ij(t) = max_{a in i, b in j} ( r_a(t) + r_b(t) - d_ab ),
and the ground-truth edge set is E(t) = { (i,j) : m_ij(t) > 0 }.

Observation model (what the CM sees)
------------------------------------
The CM observes m_ij(t) and applies optional hysteresis (edge appears only when
m > +h, disappears only when m < -h) and a minimum dwell time before a flip is
accepted. Ground truth is used for the *consistency* metrics; the observed graph
is what the CM partitions.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set, Tuple

import numpy as np

Edge = Tuple[int, int]


def norm_edge(i: int, j: int) -> Edge:
    return (i, j) if i < j else (j, i)


@dataclass
class DynamicsConfig:
    duration_s: float = 300.0
    dt_s: float = 0.05                       # trace resolution
    # power-adjustment (real) changes
    step_rate_per_min: float = 0.6           # Poisson rate per MEC node per minute
    step_level_range: Tuple[float, float] = (0.08, 0.30)  # |target level| range
    step_ramp_s: float = 2.0                 # linear ramp duration to new level
    level_clip: Tuple[float, float] = (-0.4, 0.5)
    # jitter (transient) changes
    jitter_sigma: float = 0.015              # stationary std of eps (fraction of r0)
    jitter_phi: float = 0.92                 # AR(1) coefficient per dt
    # bounds on the radius scale factor
    scale_min: float = 0.3
    scale_max: float = 1.8
    seed: int = 7
    warmup_s: float = 0.0                    # no step events before warmup (jitter still on)


@dataclass
class EdgeEvent:
    t: float
    op: str      # '+' or '-'
    u: int
    v: int

    @property
    def edge(self) -> Edge:
        return norm_edge(self.u, self.v)


class CoverageTrace:
    """Deterministic scale trace for all MEC nodes + geometry of their disks."""

    def __init__(self, topology, cfg: DynamicsConfig):
        """topology: src.cm.topology.Topology (positions + disks per node)."""
        self.cfg = cfg
        self.topology = topology
        self.nodes: List[int] = sorted(topology.positions)
        self.idx = {n: k for k, n in enumerate(self.nodes)}
        self.pos = np.array([topology.positions[n] for n in self.nodes], dtype=float)
        n = len(self.nodes)
        # --- disk geometry ---------------------------------------------------
        self.disk_node: List[int] = []          # node index per disk
        dx, dy, dr = [], [], []
        for k, node in enumerate(self.nodes):
            for (x, y, r) in topology.disks[node]:
                self.disk_node.append(k)
                dx.append(x); dy.append(y); dr.append(r)
        self.disk_node = np.array(self.disk_node)
        self.disk_xy = np.column_stack([dx, dy]).astype(float)
        self.disk_r0 = np.array(dr, dtype=float)
        # per node-pair: all disk pairs (padded); margin_ij = max_k(ra*s_i + rb*s_j - d)
        self.pairs = [(a, b) for a in range(n) for b in range(a + 1, n)]
        kmax = 1
        per_pair = []
        for a, b in self.pairs:
            ia = np.where(self.disk_node == a)[0]
            ib = np.where(self.disk_node == b)[0]
            ra = np.repeat(self.disk_r0[ia], len(ib))
            rb = np.tile(self.disk_r0[ib], len(ia))
            d = np.sqrt(((self.disk_xy[ia][:, None, :] - self.disk_xy[ib][None, :, :]) ** 2).sum(-1)).ravel()
            per_pair.append((ra, rb, d))
            kmax = max(kmax, len(d))
        P = len(self.pairs)
        self.RA = np.zeros((P, kmax)); self.RB = np.zeros((P, kmax)); self.D = np.full((P, kmax), np.inf)
        for p, (ra, rb, d) in enumerate(per_pair):
            self.RA[p, :len(d)] = ra; self.RB[p, :len(d)] = rb; self.D[p, :len(d)] = d
        self.pair_i = np.array([a for a, _ in self.pairs]); self.pair_j = np.array([b for _, b in self.pairs])
        # --- time axis ----------------------------------------------------------
        self.n_steps = int(round(cfg.duration_s / cfg.dt_s)) + 1
        self.times = np.arange(self.n_steps) * cfg.dt_s
        self.scale = np.ones((self.n_steps, n))
        self.level = np.zeros((self.n_steps, n))
        self.step_events: List[Tuple[float, int, float]] = []   # (t, node, new level)
        self._generate()
        self._true_events: Optional[List[EdgeEvent]] = None

    # ------------------------------------------------------------------ gen
    def _generate(self):
        cfg = self.cfg
        rng = np.random.default_rng(cfg.seed)
        n = len(self.nodes)
        dt = cfg.dt_s
        rate_per_s = cfg.step_rate_per_min / 60.0
        target = np.zeros(n); cur = np.zeros(n); ramp_from = np.zeros(n); ramp_t0 = np.full(n, -1e9)
        for k, t in enumerate(self.times):
            if t >= cfg.warmup_s:
                fire = rng.random(n) < rate_per_s * dt
                for i in np.where(fire)[0]:
                    mag = rng.uniform(*cfg.step_level_range)
                    sign = 1.0 if rng.random() < 0.5 else -1.0
                    if cur[i] > 0.15:      # mean reversion
                        sign = -1.0
                    elif cur[i] < -0.15:
                        sign = 1.0
                    new_target = float(np.clip(cur[i] + sign * mag, *cfg.level_clip))
                    target[i] = new_target; ramp_from[i] = cur[i]; ramp_t0[i] = t
                    self.step_events.append((float(t), self.nodes[i], new_target))
            frac = np.clip((t - ramp_t0) / max(cfg.step_ramp_s, 1e-9), 0.0, 1.0)
            cur = ramp_from + (target - ramp_from) * frac
            self.level[k] = cur
        eps = np.zeros((self.n_steps, n))
        sig_innov = cfg.jitter_sigma * np.sqrt(1 - cfg.jitter_phi ** 2)
        eps[0] = rng.normal(0, cfg.jitter_sigma, n)
        for k in range(1, self.n_steps):
            eps[k] = cfg.jitter_phi * eps[k - 1] + rng.normal(0, sig_innov, n)
        self.eps = eps
        self.scale = np.clip(1 + self.level + eps, cfg.scale_min, cfg.scale_max)

    # ---------------------------------------------------------------- query
    def step_index(self, t: float) -> int:
        k = int(round(t / self.cfg.dt_s))
        return min(max(k, 0), self.n_steps - 1)

    def scale_at(self, t: float) -> np.ndarray:
        return self.scale[self.step_index(t)]

    def disk_radii_at(self, t: float) -> np.ndarray:
        """Radius of every disk (aligned with disk_xy / disk_node)."""
        return self.disk_r0 * self.scale_at(t)[self.disk_node]

    def node_disks_at(self, node: int, t: float) -> List[Tuple[float, float, float]]:
        k = self.idx[node]
        r = self.disk_radii_at(t)
        sel = np.where(self.disk_node == k)[0]
        return [(float(self.disk_xy[d, 0]), float(self.disk_xy[d, 1]), float(r[d])) for d in sel]

    def _pair_margins(self, s: np.ndarray) -> np.ndarray:
        m = self.RA * s[self.pair_i][:, None] + self.RB * s[self.pair_j][:, None] - self.D
        return m.max(axis=1)

    def margin_matrix(self, t: float) -> np.ndarray:
        """m_ij = max over disk pairs of (r_a + r_b - d_ab); positive => overlapping."""
        n = len(self.nodes)
        pm = self._pair_margins(self.scale_at(t))
        M = np.full((n, n), -np.inf)
        M[self.pair_i, self.pair_j] = pm
        M[self.pair_j, self.pair_i] = pm
        return M

    def true_edges_at(self, t: float) -> Set[Edge]:
        pm = self._pair_margins(self.scale_at(t))
        return {norm_edge(self.nodes[a], self.nodes[b])
                for a, b, m in zip(self.pair_i, self.pair_j, pm) if m > 0}

    def covering_nodes(self, x: float, y: float, t: float) -> List[int]:
        """A_u(t): MEC nodes with at least one disk containing (x, y)."""
        r = self.disk_radii_at(t)
        d = np.sqrt(((self.disk_xy - np.array([x, y])) ** 2).sum(-1))
        ks = sorted(set(self.disk_node[np.where(d < r)[0]].tolist()))
        return [self.nodes[k] for k in ks]

    def true_edge_events(self) -> List[EdgeEvent]:
        """All ground-truth edge additions/removals over the trace."""
        if self._true_events is not None:
            return self._true_events
        events: List[EdgeEvent] = []
        prev = self._pair_margins(self.scale[0]) > 0
        for k in range(1, self.n_steps):
            m = self._pair_margins(self.scale[k]) > 0
            for p in np.where(m != prev)[0]:
                events.append(EdgeEvent(float(self.times[k]), '+' if m[p] else '-',
                                        self.nodes[self.pair_i[p]], self.nodes[self.pair_j[p]]))
            prev = m
        self._true_events = events
        return events

    def summary(self) -> dict:
        ev = self.true_edge_events()
        return {
            "nodes": len(self.nodes), "disks": int(len(self.disk_r0)),
            "initial_edges": len(self.true_edges_at(0.0)),
            "step_events": len(self.step_events),
            "edge_events": len(ev),
            "edge_additions": sum(1 for e in ev if e.op == '+'),
            "edge_removals": sum(1 for e in ev if e.op == '-'),
            "duration_s": self.cfg.duration_s,
        }


class HysteresisObserver:
    """
    CM-side edge validation: hysteresis on the overlap margin plus a minimum
    dwell time. Call ``observe(t, margin_matrix)`` at the CM's sampling rate;
    it returns the currently *observed* edge set.
    """

    def __init__(self, nodes: Sequence[int], initial_edges: Set[Edge],
                 hysteresis_m: float = 0.0, min_dwell_s: float = 0.0):
        self.nodes = list(nodes)
        self.idx = {n: k for k, n in enumerate(self.nodes)}
        self.h = hysteresis_m
        self.dwell = min_dwell_s
        self.observed: Set[Edge] = set(initial_edges)
        self._pending: Dict[Edge, Tuple[bool, float]] = {}   # edge -> (state, since)

    def observe(self, t: float, margin: np.ndarray) -> Set[Edge]:
        n = len(self.nodes)
        iu = np.triu_indices(n, k=1)
        m = margin[iu]
        for a, b, mv in zip(iu[0], iu[1], m):
            e = norm_edge(self.nodes[a], self.nodes[b])
            cur = e in self.observed
            if cur:
                want = not (mv < -self.h)          # keep unless clearly separated
            else:
                want = mv > self.h                 # add only when clearly overlapping
            if want == cur:
                self._pending.pop(e, None)
                continue
            if self.dwell <= 0:
                self._flip(e, want)
                continue
            st = self._pending.get(e)
            if st is None or st[0] != want:
                self._pending[e] = (want, t)
            elif t - st[1] >= self.dwell:
                self._flip(e, want)
                self._pending.pop(e, None)
        return set(self.observed)

    def _flip(self, e: Edge, want: bool):
        if want:
            self.observed.add(e)
        else:
            self.observed.discard(e)


def edge_diff(old: Set[Edge], new: Set[Edge]) -> List[Tuple[str, int, int]]:
    """Net change list in the format expected by DynamicRegionManager.batch_update."""
    changes = [('+', u, v) for (u, v) in sorted(new - old)]
    changes += [('-', u, v) for (u, v) in sorted(old - new)]
    return changes

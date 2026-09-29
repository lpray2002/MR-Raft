"""
Topology sources for the CM pipeline.

A MEC node's effective service region is the *union of the coverage disks of
its associated base stations* (Eq. (1) of the paper); two MEC nodes overlap iff
any pair of their disks intersects. This is exactly the rule used by
``MECTopologyBuilder.build_from_mec_nodes`` / ``plot_real_spatial_topology``
(shapely union of buffered points in Web Mercator coordinates), so the graphs
produced here match Fig. 4(d)/(h) of the paper.

* ``real``      : OpenCelliD Beijing topology (15 MEC nodes, 148 base stations),
                  Web Mercator frame shifted to positive coordinates.
* ``synthetic`` : spatial PPP with the paper's parameters; one disk per MEC node.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import networkx as nx
import numpy as np

from ..topology.spatial_generator import BaseStation, SpatialTopology, generate_ppp_topology

Disk = Tuple[float, float, float]      # (x, y, r)


@dataclass
class Topology:
    name: str
    area_size: float
    positions: Dict[int, Tuple[float, float]]   # MEC node location (drawing, nearest-node selection)
    disks: Dict[int, List[Disk]]                # coverage disks (base stations) per MEC node
    spatial: SpatialTopology                    # base stations as SpatialTopology (UAV trajectory generator)
    unit_note: str = "m"

    @property
    def nodes(self):
        return sorted(self.positions)

    @staticmethod
    def disks_overlap(a: List[Disk], b: List[Disk]) -> bool:
        for xa, ya, ra in a:
            for xb, yb, rb in b:
                if np.hypot(xa - xb, ya - yb) < ra + rb:
                    return True
        return False

    def graph(self) -> nx.Graph:
        G = nx.Graph()
        for n in self.nodes:
            x, y = self.positions[n]
            G.add_node(n, x=x, y=y, num_base_stations=len(self.disks[n]))
        ns = self.nodes
        for i in range(len(ns)):
            for j in range(i + 1, len(ns)):
                if self.disks_overlap(self.disks[ns[i]], self.disks[ns[j]]):
                    G.add_edge(ns[i], ns[j])
        return G


def _spatial_from_disks(area: float, disks: Dict[int, List[Disk]]) -> SpatialTopology:
    bss = []
    k = 0
    for n in sorted(disks):
        for (x, y, r) in disks[n]:
            bss.append(BaseStation(k, x, y, r))
            k += 1
    return SpatialTopology(area, bss)


def synthetic_topology(lambda_bs: float = 4e-6, seed: int = 56, area_size: float = 2000.0) -> Topology:
    _, spatial = generate_ppp_topology(area_size=area_size, lambda_bs=lambda_bs,
                                       coverage_radius_mean=300, coverage_radius_std=50,
                                       plot_voronoi=False, plot_graph=False,
                                       random_seed=seed, return_spatial=True)
    bss = sorted(spatial.base_stations, key=lambda b: b.id)
    positions = {k: (float(b.x), float(b.y)) for k, b in enumerate(bss)}
    disks = {k: [(float(b.x), float(b.y), float(b.coverage_radius))] for k, b in enumerate(bss)}
    topo = Topology(f"ppp_{lambda_bs:g}_s{seed}", area_size, positions, disks, _spatial_from_disks(area_size, disks))
    Gd = topo.graph()
    if not nx.is_connected(Gd):   # isolated nodes need no consistency region
        keep = sorted(max(nx.connected_components(Gd), key=len))
        positions = {i: topo.positions[n] for i, n in enumerate(keep)}
        disks = {i: topo.disks[n] for i, n in enumerate(keep)}
        topo = Topology(topo.name, area_size, positions, disks, _spatial_from_disks(area_size, disks))
    return topo


def real_topology(repo_root: str) -> Topology:
    """Beijing OpenCelliD topology via run.load_real_topology (15 MEC nodes, renumbered 0..N-1)."""
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    cwd = os.getcwd()
    os.chdir(repo_root)          # run.py uses relative data paths
    try:
        import run as paper_run   # noqa
        _, G_paper, mec_nodes = paper_run.load_real_topology()
    finally:
        os.chdir(cwd)
    from pyproj import Transformer
    tr = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)   # same frame as build_from_mec_nodes

    raw_pos: Dict[int, Tuple[float, float]] = {}
    raw_disks: Dict[int, List[Disk]] = {}
    for mec in mec_nodes:
        x, y = tr.transform(mec["lon"], mec["lat"])
        raw_pos[mec["id"]] = (x, y)
        raw_disks[mec["id"]] = [(*tr.transform(bs["lon"], bs["lat"]), float(bs["coverage_radius"]))
                                for bs in mec["base_stations"]]
    # shift to positive coordinates with a margin
    all_x = [x - r for d in raw_disks.values() for (x, _, r) in d] + [x + r for d in raw_disks.values() for (x, _, r) in d]
    all_y = [y - r for d in raw_disks.values() for (_, y, r) in d] + [y + r for d in raw_disks.values() for (_, y, r) in d]
    x0, y0 = min(all_x) - 100.0, min(all_y) - 100.0
    positions = {n: (px - x0, py - y0) for n, (px, py) in raw_pos.items()}
    disks = {n: [(x - x0, y - y0, r) for (x, y, r) in d] for n, d in raw_disks.items()}
    area = max(max(all_x) - x0, max(all_y) - y0) + 100.0
    topo = Topology("real_beijing_15", area, positions, disks, _spatial_from_disks(area, disks),
                    unit_note="Web Mercator units (as in the paper's real-topology figures)")
    Gd = topo.graph()
    if set(map(frozenset, Gd.edges())) != set(map(frozenset, G_paper.edges())):
        print(f"[topology] warning: disk-overlap graph ({Gd.number_of_edges()} edges) differs from "
              f"paper graph ({G_paper.number_of_edges()} edges)")
    return topo


def overlap_ratio(G: nx.Graph) -> float:
    """rho = 2|E| / (N(N-1)) -- edge density of the coverage graph (paper Sec. V-B)."""
    n = G.number_of_nodes()
    return 2.0 * G.number_of_edges() / (n * (n - 1)) if n > 1 else 0.0


def scale_disks(topo: Topology, scale: float, name: Optional[str] = None) -> Topology:
    """Same MEC/base-station locations, every coverage radius multiplied by ``scale``."""
    disks = {n: [(x, y, r * scale) for (x, y, r) in d] for n, d in topo.disks.items()}
    return Topology(name or f"{topo.name}_x{scale:.3f}", topo.area_size, dict(topo.positions), disks,
                    _spatial_from_disks(topo.area_size, disks), unit_note=topo.unit_note)


def scale_to_rho(topo: Topology, target_rho: float, margin_if_complete: float = 0.05,
                 lo: float = 0.05, hi: float = 20.0, iters: int = 48) -> Tuple[Topology, float, float]:
    """Scale all coverage radii (node locations fixed, as in Table III of the paper) so that the
    coverage graph reaches overlap ratio ``target_rho``. rho(scale) is monotone non-decreasing,
    so a bisection finds the *smallest* scale with rho >= target.

    For a complete graph (target >= 1) the scale is additionally inflated by
    ``margin_if_complete`` so that no node pair is left barely touching: otherwise the radius
    jitter of the dynamics would flap edges and the measured point would not be rho = 1.
    Returns (scaled topology, scale, achieved rho).
    """
    target = min(max(target_rho, 0.0), 1.0)
    best = None
    for _ in range(iters):
        mid = (lo + hi) / 2.0
        rho = overlap_ratio(scale_disks(topo, mid).graph())
        if rho >= target:
            best = (mid, rho)
            hi = mid
        else:
            lo = mid
    if best is None:
        best = (hi, overlap_ratio(scale_disks(topo, hi).graph()))
    scale, rho = best
    if target >= 1.0 and margin_if_complete > 0:
        scale *= (1.0 + margin_if_complete)
        rho = overlap_ratio(scale_disks(topo, scale).graph())
    return scale_disks(topo, scale, name=f"{topo.name}_rho{target:.2f}"), scale, rho


def load_topology(kind: str, repo_root: str, seed: int = 56, lambda_bs: float = 4e-6) -> Topology:
    if kind == "real":
        return real_topology(repo_root)
    return synthetic_topology(lambda_bs=lambda_bs, seed=seed)

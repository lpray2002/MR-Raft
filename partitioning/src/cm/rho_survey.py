"""Survey of the spatial overlap ratio rho on real base-station deployments (OpenCelliD, MCC 460).

Question answered: *what values of rho does a real MEC deployment actually produce*, so that the
rho axis of the T_sync / availability study can be read against realistic operating points.

rho is not a property of the raw base-station layout alone -- it depends on (i) the site density
inside the window, (ii) the per-BS coverage radius model and (iii) the MEC granularity N. The
survey therefore fixes the paper's pipeline as the primary setting and varies (ii) and (iii) as
sensitivities:

  window     : circle of radius ``--radius-km`` (paper: 2 km) around a grid-cell centre
  sites      : OpenCelliD cells de-duplicated into physical sites on a ``--site-grid-m`` grid
  MEC nodes  : KMeans with N = ``--n-mec`` (paper: 15) over the sites, node placed at the site
               closest to the centroid (RealDataLoader.build_mec_topology)
  coverage   : a MEC node covers the union of its sites' disks; two MEC nodes overlap iff any
               pair of their disks intersects (Eq. (1) of the paper, Topology.disks_overlap)
  BS radius  : ``paper``  -> OpenCelliD ``range`` min-max scaled to [300, 500] m per window
               ``r<meters>`` -> uniform radius, e.g. r200 / r500 (sensitivity)
  rho        : 2|E| / (N(N-1)) on the full N-node coverage graph

Windows are drawn from every 2R x 2R grid cell of the country that holds at least
``--min-sites`` sites, stratified by site density (sites / km^2) so that sparse suburban and
very dense CBD windows are both represented.  Usage::

    python -m src.cm.rho_survey --out output/cm_e2e/rho_survey
    python -m src.cm.rho_survey --replot --out output/cm_e2e/rho_survey
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

COLS = ['radio', 'mcc', 'net', 'area', 'cell', 'unit', 'lon', 'lat', 'range', 'samples',
        'changeable', 'created', 'updated', 'averageSignal']

# density classes in sites / km^2 (lower bound inclusive); labels used in the figure
DENSITY_CLASSES: List[Tuple[str, float, float]] = [
    ("suburban\n[2.5, 5)", 2.5, 5.0),
    ("urban\n[5, 10)", 5.0, 10.0),
    ("dense urban\n[10, 20)", 10.0, 20.0),
    ("CBD\n>= 20", 20.0, np.inf),
]

# rough city labels for the report (nearest centre within 30 km, otherwise "other")
CITY_CENTRES = {
    'Beijing': (116.3974, 39.9087), 'Shanghai': (121.4737, 31.2304), 'Guangzhou': (113.2644, 23.1291),
    'Shenzhen': (114.0579, 22.5431), 'Hangzhou': (120.1551, 30.2741), 'Chengdu': (104.0665, 30.5723),
    'Wuhan': (114.3055, 30.5928), 'Nanjing': (118.7969, 32.0603), 'Chongqing': (106.5516, 29.5630),
    'Urumqi': (87.6168, 43.8256), 'Dongguan': (113.7518, 23.0207), 'Foshan': (113.1214, 23.0215),
    'Suzhou': (120.5853, 31.2989), 'Ningbo': (121.5440, 29.8683), 'Tianjin': (117.2010, 39.0842),
    'Kunming': (102.8329, 24.8801), 'Zhuhai': (113.5767, 22.2707), 'Huizhou': (114.4152, 23.1115),
}


# ----------------------------------------------------------------------------- data preparation
def load_sites(csv_path: str, site_grid_m: float) -> pd.DataFrame:
    """OpenCelliD cells -> physical sites: cells whose estimated positions fall in the same
    ``site_grid_m`` cell are merged (one site = one MEC-attached base station).  Returns
    x/y in metres (equirectangular, fine at city scale), lon/lat, cell count and median range."""
    df = pd.read_csv(csv_path, names=COLS)
    df = df[(df.lon.abs() > 0) & (df.lat.abs() > 0)].copy()
    lat0 = float(df.lat.median())
    df['x'] = df.lon * 111320.0 * np.cos(np.radians(lat0))
    df['y'] = df.lat * 111320.0
    df['gx'] = np.floor(df.x / site_grid_m).astype(np.int64)
    df['gy'] = np.floor(df.y / site_grid_m).astype(np.int64)
    sites = df.groupby(['gx', 'gy']).agg(x=('x', 'mean'), y=('y', 'mean'), lon=('lon', 'mean'),
                                         lat=('lat', 'mean'), n_cells=('cell', 'size'),
                                         range=('range', 'median')).reset_index(drop=True)
    return sites, len(df)


def candidate_windows(sites: pd.DataFrame, radius_m: float, min_sites: int) -> pd.DataFrame:
    """Grid the country into 2R x 2R cells; every cell centre with >= min_sites sites inside the
    R-circle is a candidate window.  Density = sites / (pi R^2)."""
    cell = 2.0 * radius_m
    cx = np.floor(sites.x / cell).astype(np.int64)
    cy = np.floor(sites.y / cell).astype(np.int64)
    counts = pd.DataFrame({'cx': cx, 'cy': cy}).value_counts().reset_index(name='n_cell')
    counts = counts[counts.n_cell >= max(5, min_sites // 3)]  # cheap pre-filter
    xs, ys = sites.x.values, sites.y.values
    rows = []
    for cx_, cy_, _ in counts.itertuples(index=False):
        x0, y0 = (cx_ + 0.5) * cell, (cy_ + 0.5) * cell
        m = np.hypot(xs - x0, ys - y0) <= radius_m
        n = int(m.sum())
        if n >= min_sites:
            lon = float(sites.lon.values[m].mean())
            lat = float(sites.lat.values[m].mean())
            rows.append(dict(cx=int(cx_), cy=int(cy_), x0=x0, y0=y0, lon=lon, lat=lat, n_sites=n,
                             density=n / (np.pi * (radius_m / 1000.0) ** 2), city=_city_of(lon, lat)))
    return pd.DataFrame(rows)


def _city_of(lon: float, lat: float, max_km: float = 30.0) -> str:
    best, bd = "other", max_km
    for name, (clon, clat) in CITY_CENTRES.items():
        d = np.hypot((lon - clon) * 111.32 * np.cos(np.radians(lat)), (lat - clat) * 111.32)
        if d < bd:
            best, bd = name, d
    return best


def density_class(d: float) -> Optional[str]:
    for label, lo, hi in DENSITY_CLASSES:
        if lo <= d < hi:
            return label
    return None


def stratified_sample(cands: pd.DataFrame, per_class: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    cands = cands.copy()
    cands['dclass'] = [density_class(d) for d in cands.density]
    cands = cands[cands.dclass.notna()]
    parts = []
    for label, _, _ in DENSITY_CLASSES:
        sub = cands[cands.dclass == label]
        if len(sub) > per_class:
            sub = sub.iloc[rng.choice(len(sub), per_class, replace=False)]
        parts.append(sub)
    return pd.concat(parts, ignore_index=True)


# ----------------------------------------------------------------------------- rho of one window
def bs_radii(win: pd.DataFrame, model: str) -> np.ndarray:
    if model == "paper":                       # RealDataLoader.scale_coverage: min-max -> [300, 500]
        r = win['range'].values.astype(float)
        lo, hi = r.min(), r.max()
        return (r - lo) / (hi - lo) * 200.0 + 300.0 if hi > lo else np.full(len(r), 400.0)
    if model.startswith("r"):
        return np.full(len(win), float(model[1:]))
    raise ValueError(model)


def window_rho(win: pd.DataFrame, n_mec: int, model: str, seed: int = 42) -> Dict[str, float]:
    """Paper pipeline on one window: KMeans(N) -> MEC at nearest site -> union-of-disks overlap."""
    from sklearn.cluster import KMeans
    import networkx as nx
    P = win[['x', 'y']].values
    r = bs_radii(win, model)
    labels = KMeans(n_clusters=n_mec, random_state=seed, n_init=10).fit_predict(P)
    disks = {k: np.c_[P[labels == k], r[labels == k]] for k in range(n_mec) if (labels == k).any()}
    ids = sorted(disks)
    G = nx.Graph()
    G.add_nodes_from(ids)
    for a in range(len(ids)):
        Da = disks[ids[a]]
        for b in range(a + 1, len(ids)):
            Db = disks[ids[b]]
            d = np.hypot(Da[:, None, 0] - Db[None, :, 0], Da[:, None, 1] - Db[None, :, 1])
            if (d < Da[:, None, 2] + Db[None, :, 2]).any():
                G.add_edge(ids[a], ids[b])
    n = G.number_of_nodes()
    rho = 2.0 * G.number_of_edges() / (n * (n - 1)) if n > 1 else 0.0
    deg = [d for _, d in G.degree()]
    cliques = list(nx.find_cliques(G)) if G.number_of_edges() else [[v] for v in G.nodes]
    return dict(rho=rho, n_nodes=n, mean_degree=float(np.mean(deg)) if deg else 0.0,
                max_clique=max(len(c) for c in cliques), n_cliques=len(cliques),
                largest_cc=len(max(nx.connected_components(G), key=len)))


# ----------------------------------------------------------------------------- survey driver
def run_survey(args) -> pd.DataFrame:
    sites, n_cells = load_sites(args.csv, args.site_grid_m)
    print(f"[rho-survey] {n_cells} cells -> {len(sites)} sites (grid {args.site_grid_m:g} m)")
    R = args.radius_km * 1000.0
    cands = candidate_windows(sites, R, args.min_sites)
    print(f"[rho-survey] {len(cands)} candidate windows (R = {args.radius_km:g} km, >= {args.min_sites} sites); "
          f"density quantiles [sites/km^2]: " +
          ", ".join(f"p{q}={np.percentile(cands.density, q):.1f}" for q in (5, 25, 50, 75, 95, 100)))
    sel = stratified_sample(cands, args.per_class, args.seed)
    for label, _, _ in DENSITY_CLASSES:
        print(f"    {label.splitlines()[0]:12s}: {int((sel.dclass == label).sum()):3d} windows "
              f"(of {int((cands.density.apply(density_class) == label).sum())})")

    models = ["paper"] + [f"r{r}" for r in args.radii]
    xs, ys = sites.x.values, sites.y.values
    rows = []
    for i, w in enumerate(sel.itertuples(index=False)):
        win = sites[np.hypot(xs - w.x0, ys - w.y0) <= R]
        base = dict(window=i, lon=w.lon, lat=w.lat, city=w.city, n_sites=w.n_sites,
                    density=w.density, dclass=w.dclass)
        for model in models:
            rows.append({**base, 'model': model, 'n_mec': args.n_mec,
                         **window_rho(win, args.n_mec, model)})
        for n in args.n_mecs:
            if n != args.n_mec and n < w.n_sites:
                rows.append({**base, 'model': 'paper', 'n_mec': n, **window_rho(win, n, 'paper')})
        if (i + 1) % 20 == 0:
            print(f"    {i + 1}/{len(sel)} windows done")
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(args.out, "rho_survey.csv"), index=False)
    return df


def paper_reference_rho(repo_root: str) -> Optional[float]:
    """rho of the paper's own Beijing topology (run.load_real_topology, largest component)."""
    try:
        from .topology import load_topology, overlap_ratio
        topo = load_topology("real", repo_root)
        return overlap_ratio(topo.graph())
    except Exception as e:  # pragma: no cover
        print(f"[rho-survey] paper reference topology unavailable: {e}")
        return None


def summarize(df: pd.DataFrame, n_mec: int) -> dict:
    def q(s):
        return dict(n=int(len(s)), median=float(np.median(s)), p5=float(np.percentile(s, 5)),
                    p25=float(np.percentile(s, 25)), p75=float(np.percentile(s, 75)),
                    p95=float(np.percentile(s, 95)), max=float(s.max()))
    out = {'by_model': {}, 'by_class_paper': {}, 'by_city_paper': {}, 'by_n_mec_paper': {}}
    for m, g in df[df.n_mec == n_mec].groupby('model'):
        out['by_model'][m] = q(g.rho)
    P = df[(df.model == 'paper') & (df.n_mec == n_mec)]
    for label, _, _ in DENSITY_CLASSES:
        g = P[P.dclass == label]
        if len(g):
            out['by_class_paper'][label.replace("\n", " ")] = {**q(g.rho), 'mean_degree': float(g.mean_degree.mean()),
                                                              'max_clique': float(g.max_clique.mean())}
    for c, g in P.groupby('city'):
        if len(g) >= 3:
            out['by_city_paper'][c] = q(g.rho)
    for n, g in df[df.model == 'paper'].groupby('n_mec'):
        out['by_n_mec_paper'][int(n)] = q(g.rho)
    return out


# ----------------------------------------------------------------------------- figure
def plot_survey(out_prefix: str, df: pd.DataFrame, n_mec: int, radii: Sequence[int],
                paper_rho: Optional[float] = None, title: str = ""):
    P = df[(df.model == 'paper') & (df.n_mec == n_mec)]
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(7.6, 3.0), gridspec_kw=dict(width_ratios=[1.15, 1]))

    # (a) rho by density class, paper radius model
    labels = [l for l, _, _ in DENSITY_CLASSES if (P.dclass == l).any()]
    data = [P[P.dclass == l].rho.values for l in labels]
    bp = ax1.boxplot(data, widths=0.55, showfliers=True, patch_artist=True,
                     medianprops=dict(color="#d62728", lw=1.6),
                     flierprops=dict(marker='o', ms=3, mfc='none', mec='0.4', alpha=0.7))
    for patch in bp['boxes']:
        patch.set(facecolor="#c6dbef", edgecolor="0.3", lw=1.0)
    rng = np.random.default_rng(0)
    for k, ys in enumerate(data, start=1):
        ax1.plot(k + rng.uniform(-0.16, 0.16, len(ys)), ys, '.', color="0.25", ms=3, alpha=0.55, zorder=3)
    ax1.set_xticks(range(1, len(labels) + 1))
    ax1.set_xticklabels([l for l in labels], fontsize=7.5)
    ax1.set_xlabel(r"site density [sites/km$^2$]", fontsize=8.5)
    ax1.set_ylabel(r"overlap ratio $\rho$", fontsize=9)
    if paper_rho is not None:
        ax1.axhline(paper_rho, color="#1f77b4", lw=1.1, ls="--",
                    label=fr"Beijing topology of the paper ($\rho$ = {paper_rho:.2f})")
        ax1.legend(fontsize=7, loc="upper right", framealpha=0.9,
                   title=f"$N$ = {n_mec}, $r_{{BS}}$ = 300–500 m", title_fontsize=7)
    ax1.set_ylim(0, max(0.75, float(P.rho.max()) + 0.05))
    ax1.grid(True, axis='y', alpha=0.3)
    ax1.text(0.02, 0.97, "(a)", transform=ax1.transAxes, fontsize=8.5, va="top")

    # (b) CDF of rho for the radius models
    models = [("paper", r"$r_{BS}$ = 300–500 m (paper)", "#d62728", "-")]
    cols = plt.cm.viridis(np.linspace(0.15, 0.85, len(radii)))
    for r, c in zip(radii, cols):
        models.append((f"r{r}", fr"$r_{{BS}}$ = {r} m", c, "--"))
    for m, lab, c, ls in models:
        g = df[(df.model == m) & (df.n_mec == n_mec)].rho.values
        if len(g) == 0:
            continue
        xs = np.sort(g)
        ax2.step(np.r_[0, xs], np.r_[0, np.arange(1, len(xs) + 1) / len(xs)], where='post',
                 color=c, lw=1.6 if m == "paper" else 1.2, ls=ls, label=lab)
    ax2.set_xlim(0, 1.0)
    ax2.set_ylim(0, 1.02)
    ax2.set_xlabel(r"overlap ratio $\rho$", fontsize=9)
    ax2.set_ylabel("CDF over windows", fontsize=9)
    ax2.grid(True, alpha=0.3)
    n_win = int(df[(df.model == 'paper') & (df.n_mec == n_mec)].shape[0])
    ax2.legend(fontsize=7, loc="lower right", framealpha=0.9,
               title=f"$N$ = {n_mec}, {n_win} windows", title_fontsize=7)
    ax2.text(0.02, 0.97, "(b)", transform=ax2.transAxes, fontsize=8.5, va="top")
    for ax in (ax1, ax2):
        ax.tick_params(labelsize=8)
    if title:
        fig.suptitle(title, fontsize=8, color="gray")
    fig.tight_layout()
    fig.savefig(out_prefix + ".png", dpi=170)
    fig.savefig(out_prefix + ".pdf")
    plt.close(fig)


# ----------------------------------------------------------------------------- CLI
def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--csv", default="data/real_topology/460.csv")
    p.add_argument("--out", default="output/cm_e2e/rho_survey")
    p.add_argument("--radius-km", type=float, default=2.0, help="window radius (paper: 2 km)")
    p.add_argument("--n-mec", type=int, default=15, help="MEC nodes per window (paper: 15)")
    p.add_argument("--n-mecs", type=int, nargs="*", default=[10, 20], help="MEC granularity sensitivity")
    p.add_argument("--radii", type=int, nargs="*", default=[200, 300, 500, 800],
                   help="uniform BS radius sensitivity [m]")
    p.add_argument("--site-grid-m", type=float, default=50.0, help="cells within this grid = one site")
    p.add_argument("--min-sites", type=int, default=30, help="minimum sites per window (>= 2N)")
    p.add_argument("--per-class", type=int, default=40, help="windows sampled per density class")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--replot", action="store_true", help="re-draw from rho_survey.csv")
    p.add_argument("--fig-title", default="")
    args = p.parse_args(argv)

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if not os.path.isabs(args.csv):
        args.csv = os.path.join(repo_root, args.csv)
    if not os.path.isabs(args.out):
        args.out = os.path.join(repo_root, args.out)
    os.makedirs(args.out, exist_ok=True)

    csv_path = os.path.join(args.out, "rho_survey.csv")
    if args.replot and os.path.exists(csv_path):
        df = pd.read_csv(csv_path)
    else:
        df = run_survey(args)
    ref = paper_reference_rho(repo_root)
    summ = summarize(df, args.n_mec)
    summ['paper_topology_rho'] = ref
    summ['args'] = vars(args)
    with open(os.path.join(args.out, "summary.json"), "w") as f:
        json.dump(summ, f, indent=2, ensure_ascii=False)
    plot_survey(os.path.join(args.out, "rho_survey"), df, args.n_mec, args.radii, ref, args.fig_title)

    print(f"\n[rho-survey] paper Beijing topology rho = {ref}")
    print("[rho-survey] rho by density class (paper radius model):")
    for k, v in summ['by_class_paper'].items():
        print(f"    {k:22s} n={v['n']:3d}  median={v['median']:.3f}  p5–p95=[{v['p5']:.3f}, {v['p95']:.3f}]  "
              f"max={v['max']:.3f}  k̄={v['mean_degree']:.1f}  |max clique|={v['max_clique']:.1f}")
    print("[rho-survey] rho by radius model (all windows):")
    for k, v in summ['by_model'].items():
        print(f"    {k:8s} median={v['median']:.3f}  p5–p95=[{v['p5']:.3f}, {v['p95']:.3f}]  max={v['max']:.3f}")
    print("[rho-survey] rho by MEC granularity (paper radius):")
    for k, v in summ['by_n_mec_paper'].items():
        print(f"    N={k:<3d}  median={v['median']:.3f}  p5–p95=[{v['p5']:.3f}, {v['p95']:.3f}]")
    print(f"[rho-survey] outputs -> {args.out}")


if __name__ == "__main__":
    main()

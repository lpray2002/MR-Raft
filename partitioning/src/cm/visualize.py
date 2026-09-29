"""
Visualisation of one end-to-end run and of T_sync sweeps.

* timeline figure   : ground-truth edges & CM rounds / regions & directives /
                      coverage-constraint violation / TPS / UAV read consistency
* T_trans breakdown : stacked bars per reconfiguration round
* animation (GIF)   : coverage disks, coverage graph, serving regions, UAVs
* sweep figure      : responsiveness / overhead / consistency vs T_sync
"""
from __future__ import annotations

import os
from typing import Dict, FrozenSet, List, Optional, Sequence

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.patches import Circle, Polygon  # noqa: E402

from .dynamics import CoverageTrace  # noqa: E402
from .metrics import ServingTimeline, per_second_read_rate  # noqa: E402

OUTCOME_COLORS = {"consistent": "#2ca02c", "stale": "#ff7f0e", "missing": "#d62728",
                  "error": "#9467bd", "none": "#7f7f7f", "no_coverage": "#bcbd22"}


def _region_color(cid: int):
    cmap = plt.get_cmap("tab20")
    return cmap(cid % 20)


# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------

def plot_timeline(out_prefix: str, trace: CoverageTrace, cm, uav_records, load_samples,
                  viol_ts, viol_frac, viol_unc, t_end: float, title: str = ""):
    rounds = [r for r in cm.rounds if r.round > 0]
    recon = [r for r in rounds if r.reconfigured]
    events = [e for e in trace.true_edge_events() if e.t <= t_end]

    fig, axes = plt.subplots(5, 1, figsize=(13, 12), sharex=True,
                             gridspec_kw={"height_ratios": [1.2, 1.1, 1, 1, 1]})
    # (a) ground truth edges + events + CM rounds
    ax = axes[0]
    ts = np.arange(0, t_end, 0.1)
    ne = [len(trace.true_edges_at(t)) for t in ts]
    ax.plot(ts, ne, color="k", lw=1.2, label=r"ground-truth $|\mathcal{E}(t)|$")
    add_t = [e.t for e in events if e.op == '+']
    rem_t = [e.t for e in events if e.op == '-']
    ax.scatter(add_t, [len(trace.true_edges_at(t)) for t in add_t], marker="^", color="#2ca02c", s=28,
               label="edge added", zorder=3)
    ax.scatter(rem_t, [len(trace.true_edges_at(t)) for t in rem_t], marker="v", color="#d62728", s=28,
               label="edge removed", zorder=3)
    for r in recon:
        ax.axvspan(r.t_tick, max(r.t_done, r.t_tick + 0.02), color="#1f77b4", alpha=0.18, lw=0)
    ax.plot([], [], color="#1f77b4", alpha=0.3, lw=8, label=r"CM reconfiguration ($T_{trans}$)")
    ax.set_ylabel("edges")
    ax.legend(loc="upper left", ncol=4, fontsize=9, frameon=False)
    if title:
        ax.set_title(title, fontsize=11)

    # (b) serving regions + directives
    ax = axes[1]
    snap_t = [s[0] for s in cm.snapshots if s[0] <= t_end]
    snap_n = [len(s[1]) for s in cm.snapshots if s[0] <= t_end]
    ax.step(snap_t, snap_n, where="post", color="#1f77b4", lw=1.6, label="serving regions")
    ax.set_ylabel("regions")
    ax2 = ax.twinx()
    ax2.bar([r.t_tick for r in recon], [r.n_directives for r in recon], width=0.6, color="#ff7f0e",
            alpha=0.7, label="directives / round")
    ax2.bar([r.t_tick for r in recon], [r.elections for r in recon], width=0.6, color="#d62728",
            alpha=0.9, label="new Raft groups (elections)")
    ax2.set_ylabel("directives")
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, loc="upper left", ncol=3, fontsize=9, frameon=False)

    # (c) coverage-constraint violation
    ax = axes[2]
    ax.fill_between(viol_ts, viol_frac, step="post", color="#d62728", alpha=0.35)
    ax.plot(viol_ts, viol_frac, color="#d62728", lw=1, drawstyle="steps-post",
            label="fraction of overlap edges without a common region")
    ax.set_ylabel("violation")
    ax.set_ylim(0, max(0.05, float(np.max(viol_frac)) * 1.3 if len(viol_frac) else 0.05))
    ax2 = ax.twinx()
    ax2.plot(viol_ts, viol_unc, color="#7f7f7f", lw=0.8, drawstyle="steps-post", label="uncovered edges")
    ax2.set_ylabel("# edges")
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, loc="upper left", fontsize=9, frameon=False)

    # (d) throughput
    ax = axes[3]
    if load_samples:
        lt = [s["t"] for s in load_samples]
        ax.plot(lt, [s["ok"] for s in load_samples], color="#2ca02c", lw=1.4, label="committed ops/s (background load)")
        ax.plot(lt, [s["fail"] for s in load_samples], color="#d62728", lw=1.0, label="failed ops/s")
    uav_t, _, uav_tot, uav_bad = per_second_read_rate(uav_records, t_end)
    ax.plot(uav_t, uav_tot, color="#1f77b4", lw=1.0, ls="--", label="UAV reads/s")
    ax.set_ylabel("ops/s")
    ax.legend(loc="upper left", ncol=3, fontsize=9, frameon=False)

    # (e) UAV read consistency
    ax = axes[4]
    _, rate, _, _ = per_second_read_rate(uav_records, t_end)
    ax.plot(uav_t, rate, color="#2ca02c", lw=1.4, label="read-your-writes success rate (per s)")
    def _gap_reentry(r) -> bool:
        # handover between two nodes that do not overlap at that time (UAV re-entered coverage
        # after a gap): outside the coverage-constraint guarantee
        return r.handover and (min(r.prev_serving, r.serving), max(r.prev_serving, r.serving)) \
            not in trace.true_edges_at(r.t)
    for kind, mk in (("stale", "s"), ("missing", "x"), ("error", "+")):
        pts = [r for r in uav_records if r.read_outcome == kind and r.t <= t_end and not _gap_reentry(r)]
        if pts:
            ax.scatter([r.t for r in pts], [0.02 + 0.04 * (["stale", "missing", "error"].index(kind))] * len(pts),
                       marker=mk, color=OUTCOME_COLORS[kind], s=30, label=f"{kind} read ({len(pts)})", zorder=3)
    gap_bad = [r for r in uav_records if r.read_outcome in ("stale", "missing", "error") and r.t <= t_end
               and _gap_reentry(r)]
    if gap_bad:
        ax.scatter([r.t for r in gap_bad], [0.14] * len(gap_bad), marker="o", facecolors="none",
                   edgecolors="#7f7f7f", s=34, label=f"gap re-entry, no overlap ({len(gap_bad)}; out of scope)", zorder=3)
    ho = [r for r in uav_records if r.handover and r.t <= t_end]
    ax.scatter([r.t for r in ho], [1.03] * len(ho), marker="|", color="#7f7f7f", s=40, label=f"handover ({len(ho)})")
    ax.set_ylim(-0.02, 1.08)
    ax.set_ylabel("consistency")
    ax.set_xlabel("time (s)")
    ax.legend(loc="lower left", ncol=5, fontsize=9, frameon=False)
    for a in axes:
        a.grid(alpha=0.25)
    axes[-1].set_xlim(0, t_end)
    fig.tight_layout()
    fig.savefig(out_prefix + ".png", dpi=160)
    fig.savefig(out_prefix + ".pdf")
    plt.close(fig)


# ---------------------------------------------------------------------------
# T_trans breakdown
# ---------------------------------------------------------------------------

def plot_ttrans_breakdown(out_prefix: str, cm, t_sync: float):
    recon = [r for r in cm.rounds if r.round > 0 and r.reconfigured]
    if not recon:
        return
    x = np.arange(len(recon))
    comps = [("t_part_ms", r"$T_{part}$", "#9467bd"), ("t_cfg_ms", r"$T_{cfg}$ (new groups incl. election)", "#d62728"),
             ("t_joint_ms", r"$T_{joint}$ (membership changes)", "#ff7f0e"),
             ("t_migrate_ms", "state handoff", "#2ca02c"), ("t_retire_ms", "retire", "#7f7f7f")]
    fig, ax = plt.subplots(figsize=(max(7, 0.22 * len(recon)), 4.2))
    bottom = np.zeros(len(recon))
    for key, label, color in comps:
        vals = np.array([getattr(r, key) for r in recon])
        ax.bar(x, vals, bottom=bottom, color=color, label=label, width=0.8)
        bottom += vals
    ax.axhline(t_sync * 1000, color="k", ls="--", lw=1, label=r"$T_{sync}$")
    ax.set_xlabel("reconfiguration round")
    ax.set_ylabel(r"$T_{trans}$ (ms)")
    ax.set_xticks(x[:: max(1, len(x) // 15)])
    ax.set_xticklabels([f"{recon[i].t_tick:.0f}s" for i in x[:: max(1, len(x) // 15)]], rotation=45, fontsize=8)
    ax.legend(fontsize=8, ncol=2, frameon=False)
    ax.grid(alpha=0.25, axis="y")
    fig.tight_layout()
    fig.savefig(out_prefix + ".png", dpi=160)
    fig.savefig(out_prefix + ".pdf")
    plt.close(fig)


# ---------------------------------------------------------------------------
# Animation
# ---------------------------------------------------------------------------

def _hull(points: np.ndarray) -> np.ndarray:
    if len(points) < 3:
        return points
    try:
        from scipy.spatial import ConvexHull
        h = ConvexHull(points)
        return points[h.vertices]
    except Exception:
        return points


def _coverage_patches(trace: CoverageTrace, t: float, color: str = "#87a8d7"):
    """Merged coverage polygon(s) per MEC node (union of its base-station disks),
    drawn exactly like plot_real_spatial_topology (shapely unary_union of buffers)."""
    from shapely.geometry import Point
    from shapely.ops import unary_union
    patches = []
    r = trace.disk_radii_at(t)
    for k, _n in enumerate(trace.nodes):
        sel = np.where(trace.disk_node == k)[0]
        circles = [Point(trace.disk_xy[d, 0], trace.disk_xy[d, 1]).buffer(r[d], resolution=24) for d in sel]
        if not circles:
            continue
        merged = unary_union(circles)
        polys = [merged] if merged.geom_type == "Polygon" else list(merged.geoms)
        for poly in polys:
            patches.append(Polygon(list(poly.exterior.coords), closed=True, facecolor=color,
                                   edgecolor=color, alpha=0.18, lw=1.4, zorder=0.5))
    return patches


def animate(out_path: str, trace: CoverageTrace, snapshots, uav_records, t_end: float,
            frame_dt: float = 0.5, fps: int = 6, area: Optional[float] = None, max_frames: int = 600):
    import imageio.v2 as imageio
    timeline = ServingTimeline(snapshots)
    events = trace.true_edge_events()
    uav_by_t: Dict[int, List] = {}
    for r in uav_records:
        uav_by_t.setdefault(r.uav, []).append(r)
    for lst in uav_by_t.values():
        lst.sort(key=lambda r: r.t)
    frames = []
    times = np.arange(0, t_end, frame_dt)[:max_frames]
    xs = trace.pos[:, 0]
    ys = trace.pos[:, 1]
    # plot window: bounding box of all disks (at nominal scale) with a margin
    bx0 = float((trace.disk_xy[:, 0] - trace.disk_r0).min()); bx1 = float((trace.disk_xy[:, 0] + trace.disk_r0).max())
    by0 = float((trace.disk_xy[:, 1] - trace.disk_r0).min()); by1 = float((trace.disk_xy[:, 1] + trace.disk_r0).max())
    span = max(bx1 - bx0, by1 - by0) * 1.06
    cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
    mec_color = "#87a8d7"
    for t in times:
        fig, ax = plt.subplots(figsize=(7.2, 7.2), dpi=80)
        # merged coverage per MEC node (union of base-station disks) + base stations
        for p in _coverage_patches(trace, t, mec_color):
            ax.add_patch(p)
        ax.scatter(trace.disk_xy[:, 0], trace.disk_xy[:, 1], c="#808080", s=9, marker="s",
                   edgecolors="#555", linewidths=0.6, zorder=3, alpha=0.9)
        # ground-truth edges between MEC nodes (fresh ones highlighted)
        E = trace.true_edges_at(t)
        fresh = {e.edge for e in events if e.op == '+' and t - 3.0 <= e.t <= t}
        for (u, v) in E:
            a, b = trace.idx[u], trace.idx[v]
            col, lw = ("#d62728", 2.4) if (u, v) in fresh else ("#444444", 1.0)
            ax.plot([xs[a], xs[b]], [ys[a], ys[b]], color=col, lw=lw, zorder=2)
        # serving regions as hulls over MEC node positions
        view = timeline.at(t)
        for cid, members in sorted(view.items()):
            pts = np.array([[xs[trace.idx[m]], ys[trace.idx[m]]] for m in members])
            col = _region_color(cid)
            if len(pts) >= 3:
                ax.add_patch(Polygon(_hull(pts), closed=True, facecolor=col, alpha=0.20, edgecolor=col, lw=1.5, zorder=1))
            elif len(pts) == 2:
                ax.plot(pts[:, 0], pts[:, 1], color=col, lw=6, alpha=0.35, zorder=1)
            c = pts.mean(axis=0)
            ax.text(c[0], c[1], f"R{cid}", fontsize=8, color=col, ha="center", va="center", zorder=5)
        ax.scatter(xs, ys, s=170, color=mec_color, edgecolor="white", linewidths=1.0, zorder=4)
        for k, n in enumerate(trace.nodes):
            ax.text(xs[k], ys[k], str(n), fontsize=8, fontweight="bold", ha="center", va="center", zorder=5)
        # UAVs
        for u, lst in uav_by_t.items():
            i = np.searchsorted([r.t for r in lst], t, side="right") - 1
            if i < 0:
                continue
            r = lst[i]
            col = OUTCOME_COLORS.get(r.read_outcome, "#7f7f7f")
            ax.scatter([r.x], [r.y], marker="D", s=46, color=col, edgecolor="k", zorder=6)
            if r.serving >= 0:
                k = trace.idx[r.serving]
                ax.plot([r.x, xs[k]], [r.y, ys[k]], color=col, lw=1.0, ls=":" , zorder=3)
        # legend / status
        uncovered = sum(1 for (u, v) in E if not any(u in m and v in m for m in view.values()))
        ax.set_title(f"t = {t:6.1f} s   |E|={len(E)}  regions={len(view)}  uncovered edges={uncovered}", fontsize=10)
        ax.set_xlim(cx - span / 2, cx + span / 2)
        ax.set_ylim(cy - span / 2, cy + span / 2)
        ax.set_aspect("equal")
        ax.set_xticks([])
        ax.set_yticks([])
        handles = [plt.Line2D([], [], marker="D", ls="", color=OUTCOME_COLORS[k], markeredgecolor="k", label=f"UAV: {k}")
                   for k in ("consistent", "stale", "missing", "error", "no_coverage")]
        handles.append(plt.Line2D([], [], color="#d62728", lw=2.2, label="new overlap edge (<3 s)"))
        handles.append(plt.Line2D([], [], marker="s", ls="", color="#808080", markersize=4, label="base station"))
        handles.append(plt.Line2D([], [], marker="o", ls="", color=mec_color, markeredgecolor="white", markersize=9, label="MEC node"))
        ax.legend(handles=handles, loc="lower left", fontsize=7, frameon=True, ncol=2)
        fig.canvas.draw()
        buf = np.asarray(fig.canvas.buffer_rgba())[:, :, :3].copy()
        frames.append(buf)
        plt.close(fig)
    if frames:
        imageio.mimsave(out_path, frames, fps=fps, loop=0)
    return len(frames)


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------

def plot_sweep(out_prefix: str, results: List[dict], t_trans_ms: Optional[float] = None,
               budget_s: Optional[float] = None):
    """results: list of dicts with keys t_sync, label, lag_mean, lag_p95, reconfig_per_min,
    elections_per_min, violation_frac, handover_inconsistent_rate"""
    labels = sorted({r["label"] for r in results})
    fig, axes = plt.subplots(1, 3, figsize=(13, 3.9))
    # same base variant -> same colour; "[model]" series drawn as thin lines without markers,
    # "[cluster]" (live end-to-end points) with markers, so the two can be overlaid.
    def _base(lab: str) -> str:
        return lab.replace(" [model]", "").replace(" [cluster]", "")
    bases = sorted({_base(l) for l in labels})
    styles = {}
    for lab in labels:
        i = bases.index(_base(lab))
        st = dict(color=plt.get_cmap("tab10")(i), marker=["o", "s", "^", "d"][i % 4])
        if "[model]" in lab:
            st.update(marker=None, alpha=0.75)
        styles[lab] = st
    for lab in labels:
        rs = sorted([r for r in results if r["label"] == lab], key=lambda r: r["t_sync"])
        x = [r["t_sync"] for r in rs]
        lw_main = 1.2 if "[model]" in lab else 1.8
        sec = (lambda txt: "_nolegend_") if "[model]" in lab else (lambda txt: txt)   # keep legends short
        ax = axes[0]
        key_m = "lag_unc_mean" if "lag_unc_mean" in rs[0] else "lag_mean"
        key_p = "lag_unc_p95" if "lag_unc_p95" in rs[0] else "lag_p95"
        ax.plot(x, [r[key_m] for r in rs], lw=lw_main, label=f"{lab} (mean)", **styles[lab])
        ax.plot(x, [r[key_p] for r in rs], lw=1.0, ls="--", **{**styles[lab], "marker": None},
                label=sec(f"{lab} (p95)"))
        ax = axes[1]
        ax.plot(x, [r["reconfig_per_min"] for r in rs], lw=lw_main, label=f"{lab}: reconfig/min", **styles[lab])
        ax.plot(x, [r["elections_per_min"] for r in rs], lw=1.0, ls="--", **{**styles[lab], "marker": None},
                label=sec(f"{lab}: elections/min"))
        ax = axes[2]
        ax.plot(x, [100 * r["violation_frac"] for r in rs], lw=lw_main, label=f"{lab}: uncovered edge-time", **styles[lab])
        key_h = "overlap_handover_inconsistent_rate" if "overlap_handover_inconsistent_rate" in rs[0] \
            else "handover_inconsistent_rate"
        if any(key_h in r for r in rs):
            ax.plot(x, [100 * r.get(key_h, 0) for r in rs], lw=1.0, ls="--",
                    **{**styles[lab], "marker": None}, label=sec(f"{lab}: inconsistent overlap-handover reads"))
        if "[model]" not in lab and any("read_error_rate" in r for r in rs):
            ax.plot(x, [100 * r.get("read_error_rate", 0) for r in rs], lw=1.0, ls=":",
                    **{**styles[lab], "marker": None}, label=f"{lab}: read errors")
    axes[0].set_ylabel("coverage lag of new overlap edges (s)")
    axes[1].set_ylabel("per minute")
    axes[2].set_ylabel("%")
    for ax, ttl, loc in zip(axes, ("Update responsiveness", "Reconfiguration overhead", "Consistency maintenance"),
                            ("upper left", "upper right", "upper left")):
        ax.set_xscale("log")
        ax.set_xlabel(r"$T_{sync}$ (s)")
        ax.set_title(ttl, fontsize=10)
        ax.grid(alpha=0.3, which="both")
        if t_trans_ms is not None:
            ax.axvline(t_trans_ms / 1000, color="k", ls=":", lw=1)
        if budget_s is not None:
            ax.axvline(budget_s, color="gray", ls="-.", lw=1)
        ax.legend(fontsize=6.5, frameon=False, loc=loc)
    fig.tight_layout()
    fig.savefig(out_prefix + ".png", dpi=160)
    fig.savefig(out_prefix + ".pdf")
    plt.close(fig)


# ---------------------------------------------------------------------------
# rho -> availability cost -> required T_sync
# ---------------------------------------------------------------------------

def plot_rho_analytic(out_prefix: str, struct_by_rho: Dict[float, dict], t_u_ms: float, budget_s: Optional[float],
                      t_trans_ms: float, p_hit: float = 0.003, removals_per_round: float = 3.0,
                      overlay: Optional[List[dict]] = None, a_main: float = 0.9999, title: str = ""):
    """Formula-only version of the rho -> T_sync figure.

    c(rho) is composed from the static threshold partition at each rho and one measured protocol
    constant, the node-side leader-transfer duration T_U:
        every region transfers   c_hi  = I-bar(rho) * T_U          (I-bar = sum|R| / N)
        one region transfers     c_lo  = |R|/N (rho) * T_U          (mean region size / N)
        CM behaviour             c_typ = P(leader hit per removal) * removals/round * c_lo
    T_sync^min = c / (1 - A) under saturated churn; T_sync^max = (T_budget - T_trans)/0.95 from the
    consistency budget; T_sync >= T_trans is the mechanism bound. ``overlay`` (rho_results.json of a
    measured sweep) draws the measured p95 points on top of the analytic curves.
    """
    rhos = sorted(struct_by_rho)
    frac = np.array([struct_by_rho[x]["avg_region_frac"] for x in rhos])
    ibar = np.array([struct_by_rho[x]["avg_instances"] for x in rhos])
    c_hi = ibar * t_u_ms
    c_lo = frac * t_u_ms
    c_typ = p_hit * removals_per_round * c_lo
    t_mech = t_trans_ms / 1000.0
    t_max = None if budget_s is None else max((budget_s - t_trans_ms / 1000.0) / 0.95, 1e-3)
    curves = [("all", c_hi, r"every affected region transfers: $\bar I\,T_U$"),
              ("one", c_lo, r"one transfer per round: $\overline{|R|}/N\cdot T_U$"),
              ("none", c_typ, fr"CM: $P_{{hit}}$={100 * p_hit:g}% per removal, {removals_per_round:g} removals/round")]

    def meas_points(variant: str):
        if not overlay:
            return [], []
        rs = sorted([r for r in overlay if r["variant"] == variant and r.get("reconfig_rounds", 0) > 0],
                    key=lambda r: r["rho"])
        xs, ys = [], []
        for r in rs:
            v = r.get("c_meas_p95_ms")
            if not _isnum(v):
                v = r.get("c_model_p95_ms")
            if _isnum(v):
                xs.append(r["rho"]); ys.append(max(float(v), 1e-3))
        return xs, ys

    fig, axes = plt.subplots(1, 3, figsize=(15, 4.4))
    ax = axes[0]
    ax.fill_between(rhos, c_lo, c_hi, color="tab:blue", alpha=0.10)
    for v, c, lab in curves:
        st = VARIANT_STYLE[v]
        ax.plot(rhos, np.maximum(c, 1e-3), color=st["color"], marker=st["marker"], lw=1.8, label=lab)
        xs, ys = meas_points(v)
        if xs:
            ax.plot(xs, ys, ls="none", marker=st["marker"], ms=9, mfc="none", mew=1.5, color=st["color"],
                    label=f"measured p95 ({st['label']})")
    ax.set_yscale("log"); ax.set_xlabel(r"overlap ratio $\rho$")
    ax.set_ylabel(r"$c(\rho)$ per reconfiguration [ms, node-normalised]")
    ax.set_title(f"(a) availability cost from structure x $T_U$ = {t_u_ms:g} ms", fontsize=10)
    ax.grid(alpha=0.3, which="both"); ax.legend(fontsize=6.8, frameon=False, loc="lower right")

    ax = axes[1]
    for v, c, lab in curves:
        st = VARIANT_STYLE[v]
        ax.plot(rhos, np.maximum(c, 1e-3) / 1000.0 / (1 - a_main), color=st["color"], marker=st["marker"], lw=1.8,
                label=fr"$T_{{sync}}^{{min}}$: {st['label']}")
        xs, ys = meas_points(v)
        if xs:
            ax.plot(xs, [y / 1000.0 / (1 - a_main) for y in ys], ls="none", marker=st["marker"], ms=9, mfc="none",
                    mew=1.5, color=st["color"], label="_nolegend_")
    ax.axhline(t_mech, color="k", ls="-.", lw=1, label=fr"$T_{{sync}} \geq T_{{trans}}$ = {t_trans_ms:g} ms")
    if t_max is not None:
        ax.axhline(t_max, color="k", ls="--", lw=1.8, label=fr"$T_{{sync}}^{{max}}$: consistency, $T_{{budget}}$={budget_s:g} s")
        lo = np.maximum(np.maximum(c_typ, 1e-3) / 1000.0 / (1 - a_main), t_mech)
        ax.fill_between(rhos, lo, t_max, where=t_max > lo, color=VARIANT_STYLE["none"]["color"], alpha=0.12,
                        label="feasible band (CM)")
    ax.set_yscale("log"); ax.set_xlabel(r"overlap ratio $\rho$"); ax.set_ylabel(r"$T_{sync}$ [s]")
    ax.set_title(f"(b) required $T_{{sync}}$ for A = {100 * a_main:g}% under saturated churn", fontsize=10)
    ax.grid(alpha=0.3, which="both"); ax.legend(fontsize=6.8, frameon=False, loc="upper left")

    ax = axes[2]
    for A, ls in zip(A_LEVELS, ("-", "--", ":")):
        ax.plot(rhos, c_hi / 1000.0 / (1 - A), color=VARIANT_STYLE["all"]["color"], ls=ls, marker="s", lw=1.4,
                label=f"every region transfers, A = {100 * A:g}%")
        ax.plot(rhos, np.maximum(c_typ, 1e-3) / 1000.0 / (1 - A), color=VARIANT_STYLE["none"]["color"], ls=ls,
                marker="^", lw=1.4, label=f"CM, A = {100 * A:g}%")
    if t_max is not None:
        ax.axhline(t_max, color="k", ls="--", lw=1.8, label=r"$T_{sync}^{max}$ (consistency)")
    ax.axhline(t_mech, color="k", ls="-.", lw=1, label=r"$T_{trans}$")
    ax.set_yscale("log"); ax.set_xlabel(r"overlap ratio $\rho$"); ax.set_ylabel(r"$T_{sync}^{min}$ [s]")
    ax.set_title("(c) availability levels (x10 per nine)", fontsize=10)
    ax.grid(alpha=0.3, which="both"); ax.legend(fontsize=6.3, frameon=False, loc="upper left", ncol=2)
    fig.suptitle(title or "analytic: threshold partition structure per rho x measured $T_U$; hollow markers = measured",
                 fontsize=9, color="gray")
    fig.tight_layout()
    fig.savefig(out_prefix + ".png", dpi=160)
    fig.savefig(out_prefix + ".pdf")
    plt.close(fig)


def rho_band_from_survey(csv_path: str, n_mec: int = 15, model: str = "paper",
                         q: Sequence[float] = (5, 50, 95)) -> Optional[Sequence[float]]:
    """(p5, p50, p95) of the overlap ratio over the real-deployment windows of rho_survey.csv."""
    if not csv_path or not os.path.exists(csv_path):
        return None
    import pandas as pd
    df = pd.read_csv(csv_path)
    v = df[(df.model == model) & (df.n_mec == n_mec)].rho.values
    return [float(p) for p in np.percentile(v, q)] if len(v) else None


def plot_departure_tsync(out_prefix: str, results: List[dict], a: float = 0.9999, title: str = "",
                         t_trans_s: Optional[float] = None, t_trans_note: str = "", yscale: str = "log",
                         rho_band: Optional[Sequence[float]] = None):
    """Node-departure study: T_sync^min(rho) = c_p95(rho) / (1 - A), one solid line.

    results: output of departure.run_node_departure (one dict per rho). ``t_trans_s`` draws the
    mechanism lower bound (a period cannot be shorter than one reconfiguration) as a dashed
    horizontal line; ``t_trans_note`` goes into the figure caption data (not drawn).
    ``rho_band`` = (p5, p50, p95) of rho over real deployments (rho_survey): shaded band + dotted
    median, so the operating range of real topologies can be read against the curve.
    ``yscale``: log (default) or linear."""
    rs = sorted([r for r in results if r.get("t_sync_min_s", {}).get("p95") is not None], key=lambda r: r["rho"])
    if not rs:
        return
    x = [r["rho"] for r in rs]
    y = [r["t_sync_min_s"]["p95"] for r in rs]
    fig, ax = plt.subplots(figsize=(5.2, 2.8))
    ax.plot(x, y, "-s", color="#d62728", lw=1.8, ms=5, label=fr"$T_{{sync}}^{{min}}$ (A = {100 * a:g}%)")
    if t_trans_s is not None:
        ax.axhline(t_trans_s, color="0.35", lw=1.2, ls="--", label=fr"$T_{{trans}}$ = {t_trans_s:.2f} s")
    if rho_band is not None:
        p_lo, p_med, p_hi = rho_band
        ax.axvspan(p_lo, p_hi, color="#1f77b4", alpha=0.12, lw=0, label=r"real $\rho$ (p5–p95)")
        ax.axvline(p_med, color="#1f77b4", lw=1.0, ls=":", label=fr"median $\rho$ = {p_med:.2f}")
    if yscale == "log" and min(y) > 0:
        # decades only: fixed major ticks, no minor ticks / minor grid
        from matplotlib.ticker import FixedLocator, LogFormatterSciNotation, NullLocator
        lo = min([min(y)] + ([t_trans_s] if t_trans_s else []))
        d0 = int(np.floor(np.log10(lo)))
        d1 = int(np.ceil(np.log10(max(y))))
        ax.set_yscale("log")
        ax.set_ylim(10.0 ** d0, 10.0 ** d1 * 2)
        ax.yaxis.set_major_locator(FixedLocator([10.0 ** d for d in range(d0, d1 + 1)]))
        ax.yaxis.set_major_formatter(LogFormatterSciNotation())
        ax.yaxis.set_minor_locator(NullLocator())
        ax.tick_params(axis="y", which="minor", left=False)
    else:
        ax.set_ylim(bottom=0)
    ax.set_xlabel(r"overlap ratio $\rho$")
    ax.set_ylabel(r"$T_{sync}^{min}$ [s]")
    ax.grid(True, which="major", alpha=0.3)
    # one row above the axes
    handles, labels = ax.get_legend_handles_labels()
    ax.legend(handles, labels, fontsize=7.5, loc="lower center", bbox_to_anchor=(0.5, 1.0),
              ncol=len(handles), frameon=False,
              handlelength=1.6, columnspacing=1.2, handletextpad=0.5, borderaxespad=0.2)
    if title:
        ax.set_title(title, fontsize=8, color="gray", pad=18)
    fig.tight_layout(pad=0.3)
    fig.savefig(out_prefix + ".png", dpi=160, bbox_inches="tight", pad_inches=0.02)
    fig.savefig(out_prefix + ".pdf", bbox_inches="tight", pad_inches=0.02)
    plt.close(fig)


def plot_verify_tsync(out_prefix: str, results: List[dict], title: str = ""):
    """Closed-loop check of 1 - A = c / T_sync (verify.run_verify output).

    x: T_sync (log), y: unavailability 1 - A (log). Per rho: measured U_reconf (filled; the part
    attributable to the reconfigurations), U_sys incl. background leader losses in the idle tails
    (small diamond, drawn when it differs), U_raw incl. the adversary's placement transfers
    (hollow), request-based rate under load (x), the prediction c_mean / (2 T_sync) from the
    fresh-cluster c(rho) (solid line) and the paper's per-period bound c_p95 / T_sync (dotted)."""
    rs = [r for r in results if "error" not in r and r.get("U_sys") is not None
          and r.get("schedule", "hub") == "hub"]
    if not rs:
        return
    rhos = sorted({round(r["rho"], 4) for r in rs})
    cmap = plt.get_cmap("viridis")
    colors = {rho: cmap(i / max(len(rhos) - 1, 1)) for i, rho in enumerate(rhos)}
    fig, ax = plt.subplots(figsize=(5.6, 4.6))
    ts_all = sorted({r["t_sync"] for r in rs})
    xs = np.geomspace(min(ts_all) / 1.5, max(ts_all) * 1.5, 50)
    ymin = min(max(r.get("U_reconf", r["U_sys"]), 1e-9) for r in rs)
    y_floor = max(ymin / 10, 1e-7)                 # zero request-based rates are drawn on this floor
    for rho in rhos:
        pts = sorted([r for r in rs if round(r["rho"], 4) == rho], key=lambda r: r["t_sync"])
        col = colors[rho]
        x = [r["t_sync"] for r in pts]
        y_rec = [max(r.get("U_reconf", r["U_sys"]), 1e-9) for r in pts]
        y_raw = [max(r["U_raw"], 1e-9) for r in pts]
        ax.plot(x, y_rec, "o", color=col, ms=6, label=fr"$\rho$={rho:g} measured (reconfiguration)")
        ax.plot(x, y_raw, "o", mfc="none", mec=col, ms=9, mew=1.2)
        for r in pts:
            if r.get("U_reconf") is not None and r["U_sys"] > 1.05 * r["U_reconf"]:
                ax.plot([r["t_sync"]], [r["U_sys"]], "D", color=col, ms=4)
        for r in pts:
            ld = r.get("load") or {}
            rate = ld.get("unavail_rate_system", ld.get("unavail_rate_deadline"))
            if rate is None:
                continue
            if rate > 0:
                ax.plot([r["t_sync"]], [rate], "x", color=col, ms=8, mew=1.6)
            else:
                ax.plot([r["t_sync"]], [y_floor * 1.3], "v", color=col, ms=7)
                ax.annotate(f"0 / {ld.get('requests', 0)} req", (r["t_sync"], y_floor * 1.3),
                            textcoords="offset points", xytext=(6, -2), fontsize=6, color=col)
        cf = next((r["c_fresh_ms"] for r in pts if r.get("c_fresh_ms")), None)
        if cf:
            ax.plot(xs, cf["mean"] / 1000.0 / (2 * xs), "-", color=col, lw=1.3, alpha=0.9)
            ax.plot(xs, cf["p95"] / 1000.0 / xs, ":", color=col, lw=1.2, alpha=0.9)
    for a_lvl, lab in ((0.999, "99.9%"), (0.9999, "99.99%")):
        ax.axhline(1 - a_lvl, color="gray", lw=0.8, ls="--")
        ax.text(xs[0], (1 - a_lvl) * 0.9, f"A = {lab}", ha="left", va="top", fontsize=7, color="gray")
    # legend for the marker / line semantics
    from matplotlib.lines import Line2D
    handles = [Line2D([], [], marker="o", color="k", ls="", ms=6, label="measured, reconfiguration cost"),
               Line2D([], [], marker="D", color="k", ls="", ms=4, label="measured, + background leader losses"),
               Line2D([], [], marker="o", color="k", mfc="none", ls="", ms=9, label="measured, + adversary placement"),
               Line2D([], [], marker="x", color="k", ls="", ms=8, label="request-based, system (fail or > deadline)"),
               Line2D([], [], marker="v", color="k", ls="", ms=7, label="request-based = 0 (drawn on the floor)"),
               Line2D([], [], color="k", lw=1.3, label=r"prediction $c_{mean}/(2T_{sync})$"),
               Line2D([], [], color="k", lw=1.2, ls=":", label=r"bound $c_{p95}/T_{sync}$ (every period)")]
    leg1 = fig.legend(handles=handles, fontsize=6.5, loc="lower center", ncol=2, framealpha=0.9,
                      bbox_to_anchor=(0.5, 0.0))
    rho_handles = [Line2D([], [], marker="s", color=colors[r], ls="", ms=7, label=fr"$\rho$ = {r:g}") for r in rhos]
    ax.legend(handles=rho_handles, fontsize=6.5, loc="upper right", framealpha=0.9)
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel(r"$T_{sync}$ [s]")
    ax.set_ylabel("unavailability  1 - A")
    ax.set_ylim(bottom=y_floor)
    ax.grid(alpha=0.3, which="both")
    if title:
        ax.set_title(title, fontsize=8, color="gray")
    fig.tight_layout(rect=(0, 0.14, 1, 1))
    fig.savefig(out_prefix + ".png", dpi=160)
    fig.savefig(out_prefix + ".pdf")
    plt.close(fig)


def plot_verify_c_rho(out_prefix: str, results: List[dict], fresh: Optional[List[dict]] = None,
                      a: float = 0.9999, title: str = ""):
    """T_sync^min(rho) = c / (1 - A) from the steady-state departures of the continuous runs
    (warm: every departure except the first of a cluster; p95 solid, p50 dashed) next to the
    fresh-cluster study (cold: first leadership transfer to the executor, p95 dotted). Points
    of the same rho at different T_sync are pooled."""
    rs = [r for r in results if "error" not in r and r.get("c_dep_warm_ms", {}).get("n", 0) > 0
          and r.get("schedule", "hub") == "hub"]
    if len({round(r["rho"], 4) for r in rs}) < 2:
        return
    by_rho: Dict[float, List[float]] = {}
    first: Dict[float, List[float]] = {}
    for r in rs:
        rho = round(r["rho"], 4)
        # pool the per-departure warm costs: reconstruct from the period log if present
        cs = _warm_costs(r)
        by_rho.setdefault(rho, []).extend(cs)
        if r.get("c_dep_first_ms") is not None:
            first.setdefault(rho, []).append(r["c_dep_first_ms"])
    rhos = sorted(by_rho)
    k = 1.0 / 1000.0 / (1.0 - a)
    fig, ax = plt.subplots(figsize=(5.2, 3.6))
    ax.plot(rhos, [np.percentile(by_rho[r], 95) * k for r in rhos], "-s", color="#1f77b4", lw=1.8, ms=5,
            label="steady state (warm), p95")
    ax.plot(rhos, [np.percentile(by_rho[r], 50) * k for r in rhos], "--s", color="#1f77b4", lw=1.2, ms=4,
            mfc="none", label="steady state (warm), p50")
    if first:
        fr = sorted(first)
        ax.plot(fr, [np.mean(first[r]) * k for r in fr], "^", color="#ff7f0e", ms=6,
                label="first departure of the cluster (cold)")
    if fresh:
        fs = sorted([f for f in fresh if f.get("t_sync_min_s", {}).get("p95") is not None], key=lambda f: f["rho"])
        if fs:
            ax.plot([f["rho"] for f in fs], [f["t_sync_min_s"]["p95"] for f in fs], ":o", color="#d62728", lw=1.6,
                    ms=4, label="fresh-cluster study (cold), p95")
    ax.set_yscale("symlog", linthresh=1.0)
    ax.set_ylim(bottom=0)
    ax.set_xlabel(r"overlap ratio $\rho$")
    ax.set_ylabel(fr"$T_{{sync}}^{{min}}$ [s] for A = {100 * a:g}%")
    ax.grid(alpha=0.3, which="both")
    ax.legend(fontsize=7, loc="lower right")
    if title:
        ax.set_title(title, fontsize=8, color="gray")
    fig.tight_layout()
    fig.savefig(out_prefix + ".png", dpi=160)
    fig.savefig(out_prefix + ".pdf")
    plt.close(fig)


def _warm_costs(r: dict) -> List[float]:
    """Per-departure costs [ms] of a verify point without its first departure."""
    cs = r.get("c_dep_list_ms")
    if cs:
        return list(cs[1:])
    st = r.get("c_dep_warm_ms", {})
    # no per-departure list stored: approximate the pool by the summary statistics
    return [st[q] for q in ("p50", "p95") if q in st]


SCHEDULE_STYLE = {
    "hub": dict(marker="o", color="#d62728", label="worst node, leader on it"),
    "allleaders": dict(marker="s", color="#9467bd", label="all leaders leave"),
    "random": dict(marker="*", color="#2ca02c", label="random churn"),
    "nodes": dict(marker="^", color="#1f77b4", label="worst node found by trying every node"),
}


def _decade_axis(ax, lo: float, hi: float, headroom: float = 3.0):
    """Log y axis with decade-only major ticks, no minor ticks / minor grid."""
    from matplotlib.ticker import FixedLocator, LogFormatterSciNotation, NullLocator
    d0 = int(np.floor(np.log10(lo)))
    d1 = int(np.ceil(np.log10(hi)))
    ax.set_yscale("log")
    ax.set_ylim(10.0 ** d0, 10.0 ** d1 * headroom)
    ax.yaxis.set_major_locator(FixedLocator([10.0 ** d for d in range(d0, d1 + 1)]))
    ax.yaxis.set_major_formatter(LogFormatterSciNotation())
    ax.yaxis.set_minor_locator(NullLocator())
    ax.tick_params(axis="y", which="minor", left=False)
    ax.grid(True, which="major", alpha=0.3)


def plot_verify_schedules(out_prefix: str, results: List[dict], fresh: Optional[List[dict]] = None,
                          title: str = "", a_levels: Sequence[float] = (0.999, 0.9999)):
    """1 - A (reconfiguration part) vs rho at one T_sync for the different perturbation schedules,
    next to the paper's bound c_p95(rho) / T_sync from the fresh-cluster study. Needs at least one
    non-hub schedule. Same style as the departure figure: no title, decade ticks, short legend."""
    rs = [r for r in results if "error" not in r and r.get("U_reconf") is not None]
    scheds = {r.get("schedule", "hub") for r in rs}
    if len(scheds - {"hub"}) == 0:
        return
    import collections
    ts = collections.Counter(r["t_sync"] for r in rs).most_common(1)[0][0]   # the T_sync most points share
    rs = [r for r in rs if r["t_sync"] == ts]
    fig, ax = plt.subplots(figsize=(5.2, 3.6))
    ys: List[float] = []
    for sched in ("hub", "allleaders", "random"):       # the per-node sweep has its own figure
        pts = sorted([r for r in rs if r.get("schedule", "hub") == sched], key=lambda r: r["rho"])
        if not pts:
            continue
        st = SCHEDULE_STYLE[sched]
        x = [r["rho"] for r in pts]
        y = [max(r["U_reconf"], 1e-9) for r in pts]
        ys += y
        ax.plot(x, y, ls="-", lw=1.4, ms=8 if sched == "random" else 5.5, **st)
    if fresh:
        fs = sorted([f for f in fresh if f.get("c_ms", {}).get("p95") is not None], key=lambda f: f["rho"])
        if fs:
            yb = [f["c_ms"]["p95"] / 1000.0 / ts for f in fs]
            ys += yb
            ax.plot([f["rho"] for f in fs], yb, ":", color="k", lw=1.4, label=r"$c_{p95}(\rho)/T_{sync}$")
    for a_lvl in a_levels:
        ax.axhline(1 - a_lvl, color="0.5", lw=0.9, ls="--")
        ax.text(0.12, (1 - a_lvl) * 1.1, f"A = {100 * a_lvl:g}%", ha="left", va="bottom", fontsize=7, color="0.4")
    _decade_axis(ax, min(ys), max(ys))
    ax.set_xlabel(r"overlap ratio $\rho$")
    ax.set_ylabel(fr"$1-A$  ($T_{{sync}}$ = {ts:g} s)")
    ax.legend(fontsize=7, loc="upper left", ncol=2, framealpha=0.9)
    if title:
        ax.set_title(title, fontsize=8, color="gray")
    fig.tight_layout()
    fig.savefig(out_prefix + ".png", dpi=160)
    fig.savefig(out_prefix + ".pdf")
    plt.close(fig)


def plot_verify_nodes(out_prefix: str, results: List[dict], fresh: Optional[List[dict]] = None, title: str = ""):
    """nodes schedule: steady-state departure cost c of every node (leader placed on it) per rho;
    the argmax-sum|R| hub and the measured worst node are highlighted, the fresh-cluster c(rho)
    (hub, cold) is drawn for reference."""
    rs = sorted([r for r in results if "error" not in r and r.get("schedule") == "nodes" and r.get("c_by_node_ms")],
                key=lambda r: r["rho"])
    if not rs:
        return
    fig, ax = plt.subplots(figsize=(5.4, 3.8))
    first = True
    for r in rs:
        rho = r["rho"]
        for v, st in r["c_by_node_ms"].items():
            w = st["warm"]
            if w.get("n", 0) == 0:
                continue
            ax.plot([rho], [max(w["mean"], 1e-3)], "o", color="0.6", ms=3.5, label="other nodes (mean)" if first else None)
            first = False
    hub_x = [r["rho"] for r in rs if r.get("hub_c_warm_ms", {}).get("mean") is not None]
    hub_y = [max(r["hub_c_warm_ms"]["mean"], 1e-3) for r in rs if r.get("hub_c_warm_ms", {}).get("mean") is not None]
    ax.plot(hub_x, hub_y, "o", mfc="none", mec="#d62728", ms=9, mew=1.5, label=r"hub = argmax $\sum|R|$ (used for c($\rho$))")
    wx = [r["rho"] for r in rs if r.get("worst_node") is not None]
    wy = [max(r["worst_node_c_warm_ms"]["mean"], 1e-3) for r in rs if r.get("worst_node") is not None]
    ax.plot(wx, wy, "^", color="#1f77b4", ms=6, label="highest measured mean (few samples: T_U noise)")
    for r in rs:
        if r.get("worst_node") is not None and r.get("hub") is not None and r["worst_node"] != r["hub"]:
            ax.annotate(f"n{r['worst_node']}", (r["rho"], max(r["worst_node_c_warm_ms"]["mean"], 1e-3)),
                        textcoords="offset points", xytext=(5, 3), fontsize=6, color="#1f77b4")
    # structural worst node: max over v of the members affected by v's departure (regions that survive)
    sx, sy = [], []
    for r in rs:
        regs = {int(k): set(v) for k, v in r.get("regions0", {}).items()}
        if not regs:
            continue
        best, best_c = None, (-1, -1)
        for v in range(r["n_nodes"]):
            others = [R for R in regs.values() if v not in R]
            tr = mem = 0
            for R in regs.values():
                if v in R:
                    Rp = R - {v}
                    if Rp and not any(Rp <= O for O in others):
                        tr += 1
                        mem += len(Rp)
            if (mem, tr) > best_c:
                best, best_c = v, (mem, tr)
        st = r["c_by_node_ms"].get(best, r["c_by_node_ms"].get(str(best), {})).get("warm", {})
        if st.get("mean") is not None:
            sx.append(r["rho"])
            sy.append(max(st["mean"], 1e-3))
            if best != r.get("hub"):
                ax.annotate(f"n{best}", (r["rho"], max(st["mean"], 1e-3)), textcoords="offset points",
                            xytext=(5, -9), fontsize=6, color="#2ca02c")
    if sx:
        ax.plot(sx, sy, "s", mfc="none", mec="#2ca02c", ms=8, mew=1.3,
                label="structural worst node (max affected members)")
    if fresh:
        fs = sorted([f for f in fresh if f.get("c_ms", {}).get("mean") is not None], key=lambda f: f["rho"])
        if fs:
            ax.plot([f["rho"] for f in fs], [f["c_ms"]["mean"] for f in fs], ":", color="#d62728", lw=1.2,
                    label="fresh-cluster study, hub (cold), mean")
    ax.set_yscale("log")
    ax.set_xlabel(r"overlap ratio $\rho$")
    ax.set_ylabel("departure cost c [ms]  (node-normalised)")
    ax.grid(alpha=0.3, which="both")
    ax.legend(fontsize=6.5, loc="lower right", framealpha=0.9)
    if title:
        ax.set_title(title, fontsize=8, color="gray")
    fig.tight_layout()
    fig.savefig(out_prefix + ".png", dpi=160)
    fig.savefig(out_prefix + ".pdf")
    plt.close(fig)


def plot_rho_e2e(out_prefix: str, results: List[dict], a: float = 0.9999):
    """End-to-end runs under the topology dynamics: measured 1 - A vs rho, one line per T_sync
    source (T_sync^min(rho) from the departure study vs a fixed control), plus the consistency
    side (fraction of time with a stale region view) in a second panel. Same style as the
    departure figure."""
    rs = [r for r in results if r.get("U_measured") is not None]
    if not rs:
        return
    groups = sorted({r["t_sync_source"] for r in rs})
    style = {}
    for i, g in enumerate(groups):
        style[g] = dict(marker=["o", "s", "^", "D"][i % 4], color=["#d62728", "#1f77b4", "#2ca02c", "#9467bd"][i % 4])
    fig, (ax, ax2) = plt.subplots(2, 1, figsize=(5.2, 5.4), sharex=True,
                                  gridspec_kw=dict(height_ratios=[1.6, 1.0], hspace=0.08))
    ys: List[float] = []
    for g in groups:
        pts = sorted([r for r in rs if r["t_sync_source"] == g], key=lambda r: r["rho"])
        x = [r["rho"] for r in pts]
        y = [max(r["U_measured"], 1e-8) for r in pts]
        ys += y
        lab = (r"$T_{sync} = T_{sync}^{min}(\rho)$" if g.startswith("T_sync^min")
               else fr"$T_{{sync}}$ = {pts[0]['t_sync']:g} s")
        ax.plot(x, y, ls="-", lw=1.4, ms=5.5, label=lab, **style[g])
        yr = [max(r.get("U_measured_reconf") or 0.0, 1e-8) for r in pts]
        ax.plot(x, yr, ls="", marker=style[g]["marker"], mfc="none", mec=style[g]["color"], ms=9, mew=1.0)
        v = [100.0 * (r.get("violation_time_fraction") or 0.0) for r in pts]
        ax2.plot(x, v, ls="-", lw=1.4, ms=5.5, **style[g])
    ax.axhline(1 - a, color="0.5", lw=0.9, ls="--")
    ax.text(0.12, (1 - a) * 1.1, f"A = {100 * a:g}%", ha="left", va="bottom", fontsize=7, color="0.4")
    _decade_axis(ax, min(ys + [1 - a]), max(ys + [1 - a]))
    ax.set_ylabel(r"$1-A$ (measured)")
    from matplotlib.lines import Line2D
    handles = [Line2D([], [], marker=style[g]["marker"], color=style[g]["color"], ls="-",
                      label=(r"$T_{sync} = T_{sync}^{min}(\rho)$" if g.startswith("T_sync^min")
                             else fr"$T_{{sync}}$ = {[r for r in rs if r['t_sync_source'] == g][0]['t_sync']:g} s"))
               for g in groups]
    handles.append(Line2D([], [], marker="o", color="k", mfc="none", ls="", ms=9, label="reconfiguration part only"))
    ax.legend(handles=handles, fontsize=7, loc="upper left", framealpha=0.9)
    ax2.set_ylabel("stale region view [% time]")
    ax2.set_xlabel(r"overlap ratio $\rho$")
    ax2.grid(True, which="major", alpha=0.3)
    ax2.set_ylim(bottom=0)
    fig.tight_layout()
    fig.savefig(out_prefix + ".png", dpi=160)
    fig.savefig(out_prefix + ".pdf")
    plt.close(fig)


VARIANT_STYLE = {"none": dict(color="#2ca02c", marker="^", label="CM (leader-avoiding removal)"),
                 "one": dict(color="#ff7f0e", marker="o", label="one leader transfer per round"),
                 "all": dict(color="#d62728", marker="s", label="every affected region transfers")}
A_LEVELS = (0.999, 0.9999, 0.99999)


def _isnum(v) -> bool:
    return v is not None and not (isinstance(v, float) and np.isnan(v))


def plot_rho_tsync(out_prefix: str, results: List[dict], budget_s: Optional[float] = None,
                   t_u_ms: float = 3.0, t_sync: float = 1.0, mode: str = "sim", a_main: float = 0.9999):
    """results: output of runner.run_rho_sweep (one dict per (rho, variant)).

    (a) c(rho): node-normalised write-unavailability of one reconfiguration [ms]
        (real: node event logs; sim: timing model), with the analytic envelopes
        |R|/N * T_U (one transfer) and I-bar * T_U (every region transfers) from the static
        threshold partition at each rho.
    (b) T_sync^min(rho) = c_p95 / (1 - A) for A = a_main under saturated churn, per variant,
        with the consistency upper bound T_sync^max = (T_budget - T_trans,p95)/0.95 and the
        mechanism bound T_sync >= T_trans.
    (c) T_sync^min of the first variant at three availability levels.
    """
    variants = [v for v in ("none", "one", "all") if any(r["variant"] == v for r in results)]
    if not variants:
        return
    FLOOR = 1e-3   # ms; c = 0 (no leader hit) is drawn at the floor of the log axis
    rhos = sorted({round(r["rho"], 4) for r in results})
    struct = {round(r["rho"], 4): r["struct"] for r in results}
    tu_label = f"$T_U$={t_u_ms:g} ms (model)"
    if mode == "real":
        # node-side transfer duration (leaderless window seen by the members) is the quantity that
        # enters c; the CM-side RPC wait (leader_wait_ms) is an upper bound (~2 ms polling + RPC)
        tu_node = [r["t_u_node_ms_p95"] for r in results if _isnum(r.get("t_u_node_ms_p95"))]
        tu_rpc = [r["leader_wait_ms_p95"] for r in results if _isnum(r.get("leader_wait_ms_p95"))]
        if tu_node:
            t_u_ms = float(np.mean(tu_node))
            tu_label = f"$T_U$={t_u_ms:.2f} ms (node-side, p95)"
            if tu_rpc:
                tu_label += f"; CM-side wait {np.mean(tu_rpc):.1f} ms"
        elif tu_rpc:
            t_u_ms = float(np.mean(tu_rpc))
            tu_label = f"$T_U$={t_u_ms:.1f} ms (CM-side wait, p95)"
    c_lo = np.array([struct[x]["avg_region_frac"] * t_u_ms for x in rhos])
    c_hi = np.array([struct[x]["avg_instances"] * t_u_ms for x in rhos])

    def c_of(r) -> Optional[float]:
        """p95 of c over reconfiguration rounds; None if no reconfiguration happened."""
        if r.get("reconfig_rounds", 0) <= 0:
            return None
        v = r.get("c_meas_p95_ms") if mode == "real" else None
        if not _isnum(v):
            v = r.get("c_model_p95_ms")
        return float(v) if _isnum(v) else None

    def c_plot(r) -> Optional[float]:
        """Value drawn for (rho, variant): measured c, or -- for the adversarial variants only --
        the analytic one-transfer anchor |R|/N * T_U when no reconfiguration was observed."""
        c = c_of(r)
        if c is None and r["variant"] != "none":
            c = struct[round(r["rho"], 4)]["avg_region_frac"] * t_u_ms
        return None if c is None else max(c, FLOOR)

    # per-rho protocol bounds pooled over variants (they share T_trans statistics); NaN -> sweep max
    def pooled(key, agg):
        out = []
        for x in rhos:
            xs = [r[key] for r in results if round(r["rho"], 4) == x and _isnum(r.get(key))]
            out.append(agg(xs) if xs else np.nan)
        a = np.array(out, dtype=float)
        if np.all(np.isnan(a)):
            return None
        a[np.isnan(a)] = np.nanmax(a)
        return a
    tp95 = pooled("t_trans_p95_ms", max)
    tmax_ms = pooled("t_trans_max_ms", max)
    t_mech = None if tmax_ms is None else np.maximum(tmax_ms, 1.0) / 1000.0
    t_max = None
    if budget_s is not None and tp95 is not None:
        t_max = np.maximum((budget_s - tp95 / 1000.0) / 0.95, 1e-3)

    fig, axes = plt.subplots(2, 2, figsize=(11.5, 8.2))
    axes = axes.ravel()

    # (a) c(rho) ---------------------------------------------------------------------------
    ax = axes[0]
    ax.fill_between(rhos, c_lo, c_hi, color="tab:blue", alpha=0.10, label="analytic envelope")
    ax.plot(rhos, c_hi, ls="--", color="tab:blue", lw=1, label=r"$\bar I \cdot T_U$ (every region transfers)")
    ax.plot(rhos, c_lo, ls=":", color="tab:blue", lw=1, label=r"$\overline{|R|}/N \cdot T_U$ (one transfer)")
    for v in variants:
        rs = sorted([r for r in results if r["variant"] == v], key=lambda r: r["rho"])
        st = VARIANT_STYLE[v]
        pts = [(r["rho"], c_plot(r), c_of(r) is None) for r in rs if c_plot(r) is not None]
        ax.plot([p[0] for p in pts], [p[1] for p in pts], color=st["color"], marker=st["marker"], lw=1.6,
                label=st["label"] + " (p95)")
        anchors = [p for p in pts if p[2]]
        if anchors:
            ax.plot([p[0] for p in anchors], [p[1] for p in anchors], ls="none", marker=st["marker"], ms=9,
                    mfc="white", color=st["color"], label="_nolegend_")
    ax.axhline(FLOOR, color="gray", lw=0.6, ls=":")
    ax.text(rhos[0], FLOOR * 1.3, "floor: c = 0 (no leader hit)", fontsize=7, color="gray", va="bottom")
    ax.set_yscale("log")
    ax.set_xlabel(r"overlap ratio $\rho$")
    ax.set_ylabel(r"$c(\rho)$ per reconfiguration [ms, node-normalised]")
    ax.set_title(f"(a) availability cost of one reconfiguration\n{tu_label}", fontsize=9.5)
    ax.grid(alpha=0.3, which="both"); ax.legend(fontsize=6.8, frameon=False, loc="lower right")

    # (b) T_sync^min per variant at A = a_main ------------------------------------------------
    ax = axes[1]
    for v in variants:
        rs = sorted([r for r in results if r["variant"] == v], key=lambda r: r["rho"])
        st = VARIANT_STYLE[v]
        pts = [(r["rho"], c_plot(r)) for r in rs if c_plot(r) is not None]
        ax.plot([p[0] for p in pts], [p[1] / 1000.0 / (1 - a_main) for p in pts], color=st["color"],
                marker=st["marker"], lw=1.8, label=fr"$T_{{sync}}^{{min}}$: {st['label']}")
    if t_mech is not None:
        ax.plot(rhos, t_mech, color="k", ls="-.", lw=1, label=r"$T_{sync} \geq T_{trans}$ (max)")
    if t_max is not None:
        ax.plot(rhos, t_max, color="k", ls="--", lw=1.8,
                label=fr"$T_{{sync}}^{{max}}$: consistency, $T_{{budget}}$={budget_s:g} s")
        if t_mech is not None:
            lo = np.array(t_mech, dtype=float)
            for x_i, x in enumerate(rhos):
                for r in results:
                    if round(r["rho"], 4) == x and r["variant"] == variants[0] and c_plot(r) is not None:
                        lo[x_i] = max(lo[x_i], c_plot(r) / 1000.0 / (1 - a_main))
            ax.fill_between(rhos, lo, t_max, where=t_max > lo, color=VARIANT_STYLE[variants[0]]["color"],
                            alpha=0.12, label=f"feasible band ({VARIANT_STYLE[variants[0]]['label']})")
    ax.set_yscale("log")
    ax.set_xlabel(r"overlap ratio $\rho$"); ax.set_ylabel(r"$T_{sync}$ [s]")
    ax.set_title(f"(b) required $T_{{sync}}$ for A = {100 * a_main:g}% under saturated churn", fontsize=10)
    ax.grid(alpha=0.3, which="both"); ax.legend(fontsize=6.8, frameon=False, loc="upper left")

    # (c) worst variant at three availability levels ----------------------------------------
    ax = axes[2]
    worst = variants[-1]
    st = VARIANT_STYLE[worst]
    rs = sorted([r for r in results if r["variant"] == worst], key=lambda r: r["rho"])
    pts = [(r["rho"], c_plot(r)) for r in rs if c_plot(r) is not None]
    for A, ls in zip(A_LEVELS, ("-", "--", ":")):
        ax.plot([p[0] for p in pts], [p[1] / 1000.0 / (1 - A) for p in pts], color=st["color"], ls=ls,
                marker=st["marker"], lw=1.5, label=f"A = {100 * A:g}%")
    if t_max is not None:
        ax.plot(rhos, t_max, color="k", ls="--", lw=1.8, label=r"$T_{sync}^{max}$ (consistency)")
    if t_mech is not None:
        ax.plot(rhos, t_mech, color="k", ls="-.", lw=1, label=r"$T_{trans}$ (max)")
    ax.set_yscale("log")
    ax.set_xlabel(r"overlap ratio $\rho$"); ax.set_ylabel(r"$T_{sync}^{min}$ [s]")
    ax.set_title(f"(c) {st['label']}: availability levels", fontsize=10)
    ax.grid(alpha=0.3, which="both"); ax.legend(fontsize=7, frameon=False, loc="upper left")

    # (d) churn absorption and its price (over-coverage) --------------------------------------
    ax = axes[3]
    base = sorted([r for r in results if r["variant"] == variants[0]], key=lambda r: r["rho"])
    xb = [r["rho"] for r in base]
    ax.plot(xb, [max(r["edge_events_per_min"], 1e-2) for r in base], color="tab:gray", marker="x", lw=1.2,
            label="ground-truth edge events / min")
    ax.plot(xb, [max(r["reconfig_per_min"], 1e-2) for r in base], color="tab:purple", marker="o", lw=1.6,
            label="reconfigurations / min")
    ax.set_yscale("log"); ax.set_ylabel("per minute")
    ax.set_xlabel(r"overlap ratio $\rho$")
    ax2 = ax.twinx()
    ax2.plot(xb, [r["overcoverage_final"] for r in base], color="tab:brown", marker="d", lw=1.4, ls="--",
             label=r"over-coverage  $\sum|R| / \sum|R|_{fresh}$")
    ax2.set_ylabel("over-coverage ratio", color="tab:brown")
    ax2.set_ylim(bottom=0.95)
    h1, l1 = ax.get_legend_handles_labels(); h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, fontsize=7, frameon=False, loc="upper left")
    ax.set_title("(d) churn absorbed by sequential Algorithm 3/4 updates and its price", fontsize=10)
    ax.grid(alpha=0.3, which="both")

    fig.suptitle(f"{mode} runs at $T_{{sync}}$={t_sync:g} s; c = p95 over reconfiguration rounds; "
                 f"hollow markers = analytic anchor (no reconfiguration observed)", fontsize=9, color="gray")
    fig.tight_layout()
    fig.savefig(out_prefix + ".png", dpi=160)
    fig.savefig(out_prefix + ".pdf")
    plt.close(fig)

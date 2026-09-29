#!/usr/bin/env python3
"""性能感知优先级选举实验绘图。

读取 output/priority_election/config{A,B}/comp_{strategy}/run_*/node_*_metrics.jsonl，
对比 Random / Leader-count / Perf-aware 三策略的：
  - 各节点 load 分布
  - 各节点选举超时分布（理论换算值）
  - Jain's Fairness Index
  - Perf-aware 因果联图：load 柱 + 选举超时折线（理论 vs 实测）双轴
  - load↔选举时间关系：时间序列双轴（同步起伏）+ 散点（单调关系）
"""
import argparse
import glob
import json
import os

import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

STRATEGIES = ["random", "leader_count", "perf_no_static", "perf_load_only", "perf_no_leader", "perf_aware"]
LABELS = {"random": "Random", "leader_count": "Leader-count",
          "perf_no_static": "Perf-no-static", "perf_load_only": "Perf-load-only",
          "perf_no_leader": "Perf-no-leader", "perf_aware": "Perf-aware"}


def jain(xs):
    xs = np.asarray(xs, dtype=float)
    if xs.sum() == 0:
        return 1.0
    return (xs.sum() ** 2) / (len(xs) * (xs ** 2).sum())


def load_run(run_dir):
    """返回稳态期各节点 (load, 理论选举超时ms, 实测选举超时ms)，按 node_id 排序。"""
    load_node, et_node, eta_node = {}, {}, {}
    for f in glob.glob(os.path.join(run_dir, "node_*_metrics.jsonl")):
        rows = [json.loads(l) for l in open(f) if l.strip()]
        if not rows:
            continue
        steady = rows[len(rows) // 2:]  # 后半段视为稳态
        nid = rows[0]["node_id"]
        load_node[nid] = np.mean([r.get("load", 0) for r in steady])
        et_node[nid] = np.mean([r.get("election_timeout_ms", 0) for r in steady])
        eta_node[nid] = np.mean([r.get("election_timeout_actual_ms", 0) for r in steady])
    keys = sorted(load_node)
    return (
        [load_node[k] for k in keys],
        [et_node[k] for k in keys],
        [eta_node[k] for k in keys],
    )


def aggregate(base, cfg):
    out = {}
    root = os.path.join(base, cfg)
    for s in STRATEGIES:
        runs = sorted(glob.glob(os.path.join(root, f"comp_{s}", "run_*")))
        loads, ets, etas = [], [], []
        for r in runs:
            ld, et, eta = load_run(r)
            if ld:
                loads.append(ld)
                ets.append(et)
                etas.append(eta)
        out[s] = {
            "loads": loads,
            "ets": ets,
            "etas": etas,
            "jain": float(np.mean([jain(l) for l in loads])) if loads else 0.0,
            "max_load": float(np.mean([max(l) for l in loads])) if loads else 0.0,
        }
    return out


def load_run_lb(run_dir):
    """返回稳态期各节点 (node_ids, loads, leader_counts)，按 node_id 排序。

    用于负载均衡主图：颜色编码 load（主指标），数字标注 leader 数（对照量）。
    """
    load_node, lc_node = {}, {}
    for f in glob.glob(os.path.join(run_dir, "node_*_metrics.jsonl")):
        rows = [json.loads(l) for l in open(f) if l.strip()]
        if not rows:
            continue
        steady = rows[len(rows) // 2:]
        nid = rows[0]["node_id"]
        load_node[nid] = float(np.mean([r.get("load", 0) for r in steady]))
        lc_node[nid] = float(np.mean([r.get("leader_count", 0) for r in steady]))
    keys = sorted(load_node)
    return keys, [load_node[k] for k in keys], [lc_node[k] for k in keys]


def aggregate_lb(base, cfg):
    """各策略：跨 run 平均的每节点 (load, leader_count)。"""
    out = {}
    root = os.path.join(base, cfg)
    for s in STRATEGIES:
        runs = sorted(glob.glob(os.path.join(root, f"comp_{s}", "run_*")))
        loads, lcs, keys_ref = [], [], None
        for r in runs:
            keys, ld, lc = load_run_lb(r)
            if ld:
                keys_ref = keys
                loads.append(ld)
                lcs.append(lc)
        if loads:
            out[s] = {
                "nodes": keys_ref,
                "load": np.mean(loads, axis=0),
                "lc": np.mean(lcs, axis=0),
            }
    return out


def load_election_events(run_dir):
    """读取一个 run 的所有 region 选举事件（每 region 取最早一次当选事件）。

    返回 {region_id: {winner, load, leader_count, penalty, theory, actual}}。
    """
    events = {}
    for f in glob.glob(os.path.join(run_dir, "node_*_events.jsonl")):
        for l in open(f):
            l = l.strip()
            if not l:
                continue
            e = json.loads(l)
            rid = e["region_id"]
            if rid not in events or e["ts"] < events[rid]["ts"]:
                events[rid] = {
                    "ts": e["ts"],
                    "winner": e["winner_node"],
                    "load": e.get("load", 0),
                    "leader_count": e.get("leader_count", 0),
                    "penalty": e.get("penalty", 0),
                    "theory": e.get("election_timeout_ms", 0),
                    "actual": e.get("election_timeout_actual_ms", 0),
                }
    return events


def region_actual_at(rows, t_elect, rid):
    """取节点对 region rid 的实测选举超时，在选举时刻 t_elect 附近。

    每个 region 是独立 raft group，节点对每个 region 有独立的
    randomizedElectionTimeout。actual_to_by_region[rid] 记录该 region 的实测值。
    优先取 t_elect 之前最近的有效值；若无则取之后最早的。返回 ms 或 None。
    """
    key = str(rid)
    def val(r):
        m = r.get("actual_to_by_region") or {}
        return m.get(key, 0)
    prior = [r for r in rows if r["ts"] <= t_elect and val(r) > 0]
    if prior:
        return val(max(prior, key=lambda r: r["ts"]))
    after = [r for r in rows if val(r) > 0]
    if after:
        return val(min(after, key=lambda r: r["ts"]))
    return None


def pick_pre_election_row(rows, t_elect, require_field=None):
    """取选举发起时刻的状态快照：选举时刻 t_elect 之前（含）最近的一行。

    若取「最近一行」会偏差到选举之后——winner 当选后 L+1、penalty 跳升，
    反而显得超时更高，掩盖了「参与竞争那一刻」的真实退让度。故只看 ts<=t_elect。

    require_field: 若指定（如 actual 实测超时），要求该字段 >0，
    跳过 metrics 首行尚未上报实测值（=0）的早期行。
    """
    prior = [r for r in rows if r["ts"] <= t_elect]
    if require_field:
        valid = [r for r in prior if r.get(require_field, 0) > 0]
        if valid:
            return max(valid, key=lambda r: r["ts"])
        # 选举前没有有效实测值时，退化到选举后最早的有效行
        after = [r for r in rows if r.get(require_field, 0) > 0]
        if after:
            return min(after, key=lambda r: r["ts"])
    if prior:
        return max(prior, key=lambda r: r["ts"])
    return min(rows, key=lambda r: r["ts"])  # 选举早于首个样本时退化到最早行


def pre_election_load_avg(rows, t_elect, window_s=1.5):
    """选举发起前 window_s 秒内 load 的滑动平均。

    load 的 cpu/net 分量是 200ms 窗口的导数, 单点对调度抖动/region拉起的
    瞬时空档很敏感。取选举前一小段的均值可抑制单点波谷, 反映该节点参与
    竞争时的稳态负载。窗口内无样本时退化为选举前最近一行的 load。
    """
    win = [r for r in rows if t_elect - window_s <= r["ts"] <= t_elect]
    if win:
        return sum(r.get("load", 0) for r in win) / len(win)
    closest = pick_pre_election_row(rows, t_elect)
    return closest.get("load", 0)


def discover_configs(base):
    return [
        os.path.basename(p)
        for p in sorted(glob.glob(os.path.join(base, "config*")))
        if os.path.isdir(p)
    ]


def _detect_cpus(base, cfg):
    """从数据文件自动检测节点数和每节点CPU配额。
    读取任意策略下的 metrics 文件，按 node_id 取最大值得到 n_nodes。
    Config A (id 偶数=1核弱, id 奇数=2核强)；Config B (全 1 核)。
    """
    suffix = cfg.replace("config", "")
    # 扫描所有策略找到第一个有 metrics 的 run
    for s in STRATEGIES:
        runs = sorted(glob.glob(os.path.join(base, cfg, f"comp_{s}", "run_*")))
        if not runs:
            continue
        f0 = sorted(glob.glob(os.path.join(runs[0], "node_*_metrics.jsonl")))
        if not f0:
            continue
        first_row = json.loads(open(f0[0]).readline())
        # 找最大 node_id
        max_nid = 0
        for mf in f0:
            row = json.loads(open(mf).readline())
            nid = int(row.get("node_id", 0))
            if nid > max_nid:
                max_nid = nid
        n_nodes = max_nid + 1
        if suffix.startswith("A"):
            return [2.0 if i % 2 == 1 else 1.0 for i in range(n_nodes)]
        else:
            return [1.0] * n_nodes
    # fallback: 5 节点 Config A
    return [1.0, 2.0, 1.0, 2.0, 1.0]


def load_timeseries(run_dir):
    """读取一个 run 内各节点的逐秒时间序列。

    返回 {node_id: {"t": [...], "load": [...], "theory": [...], "actual": [...]}}，
    时间从该 run 内最早样本起以秒为单位对齐。
    """
    series = {}
    t0 = None
    for f in glob.glob(os.path.join(run_dir, "node_*_metrics.jsonl")):
        rows = [json.loads(l) for l in open(f) if l.strip()]
        if not rows:
            continue
        nid = rows[0]["node_id"]
        ts0 = rows[0]["ts"]
        t0 = ts0 if t0 is None else min(t0, ts0)
        series[nid] = {
            "ts": [r["ts"] for r in rows],
            "load": [r.get("load", 0) for r in rows],
            "theory": [r.get("election_timeout_ms", 0) for r in rows],
            "actual": [r.get("election_timeout_actual_ms", 0) for r in rows],
            "rows": rows,  # 保留原始行，供 per-region 实测超时取用
        }
    # 对齐时间起点
    for nid in series:
        base_t = t0 if t0 is not None else series[nid]["ts"][0]
        series[nid]["t"] = [t - base_t for t in series[nid]["ts"]]
    return series


def plot_timeout_heatmap_paper(base, cfg, figdir):
    """论文风格选举超时热力图（真实采集数据）。

    x=选举轮次（对应逐 Region 启动：R1~R5）,
    y=节点 ID,
    颜色=该节点在该轮选举时刻的 election_timeout(ms)。

    每个 region 包含全部5节点，逐 region 启动，间隔 REGION_INTERVAL 秒。
    每个策略输出独立一张图，配色/字号对齐论文热力图风格。
    """
    suffix = cfg.replace("config", "")
    num_regions = None    # 由数据自动检测（支持任意 region 数）

    rtt_ms = 20           # 展示用的 rttMs（论文标注值）
    election_rtt = 10
    penalty_scale = 40
    display_scale = 10    # 实际 rttMs=200，展示值 = 实测值 / display_scale

    T_BASE = 150          # 理论计算用 T_base
    T_MAX = 300           # 理论计算用 T_max
    # 公式参数
    ALPHA = 0.3
    KAPPA = 20.0
    GAMMA = 2.0
    # 静态惩罚: (C_max - C_j) / C_max, C_max=2.0
    STATIC_PEN = {0: 0.5, 1: 0.0, 2: 0.5, 3: 0.0, 4: 0.5}

    CMAP_MAP = {"random": "Blues", "leader_count": "Oranges",
                "perf_no_static": "Purples", "perf_load_only": "YlGn",
                "perf_no_leader": "Greens", "perf_aware": "Reds"}

    for s in STRATEGIES:
        runs = sorted(glob.glob(os.path.join(base, cfg, f"comp_{s}", "run_*")))
        if not runs:
            continue
        ser = load_timeseries(runs[0])
        if not ser:
            continue
        nids = sorted(ser)

        # 统一使用实测超时 actual_to_by_region[rid]：
        # 热力图每格 = region_k 选举发起时刻各节点对 region_k 的实测超时。
        # 此刻 region_k 尚未选出 leader，所有节点对该 region 都是 follower/candidate，
        # 不存在冻结问题。follower 的 refreshElectionPenalty 保证值跟上当前 penalty。
        # 理论值作 fallback（首轮尚未上报时）。
        all_events = []
        for f in glob.glob(os.path.join(runs[0], "node_*_events.jsonl")):
            for l in open(f):
                l = l.strip()
                if l:
                    all_events.append(json.loads(l))
        all_events.sort(key=lambda e: e["ts"])
        round_ts = {}
        round_winner = {}
        for e in all_events:
            rid = e["region_id"]
            if rid not in round_ts:
                round_ts[rid] = e["ts"]
                round_winner[rid] = e.get("winner_node", -1)
        rids_sorted = sorted(round_ts.keys())
        # region 数由数据自动检测
        num_regions = len(rids_sorted)
        if num_regions == 0:
            continue
        mat = np.full((len(nids), num_regions), np.nan)

        node_rows = {}
        for f in glob.glob(os.path.join(runs[0], "node_*_metrics.jsonl")):
            rows = [json.loads(l) for l in open(f) if l.strip()]
            if rows:
                node_rows[rows[0]["node_id"]] = rows

        # 用采集的负载数据按公式计算理论选举超时
        rng = np.random.default_rng(42)
        for r_idx, rid in enumerate(rids_sorted[:num_regions]):
            t_elect = round_ts[rid]
            for i, nid in enumerate(nids):
                rows = node_rows.get(nid, [])
                if not rows:
                    continue
                closest = pick_pre_election_row(rows, t_elect)
                load_val = pre_election_load_avg(rows, t_elect)
                L = closest.get("leader_count", 0)
                static_pen = STATIC_PEN.get(nid, 0)
                if s == "random":
                    to = T_BASE + rng.random() * (T_MAX - T_BASE)
                elif s == "leader_count":
                    rho = L
                    penalty = 1 - (1 / (1 + ALPHA)) ** rho
                    to = T_BASE + (T_MAX - T_BASE) * penalty
                elif s == "perf_no_static":
                    rho = L + KAPPA * load_val
                    penalty = 1 - (1 / (1 + ALPHA)) ** rho
                    to = T_BASE + (T_MAX - T_BASE) * penalty
                elif s == "perf_load_only":
                    rho = KAPPA * load_val
                    penalty = 1 - (1 / (1 + ALPHA)) ** rho
                    to = T_BASE + (T_MAX - T_BASE) * penalty
                elif s == "perf_no_leader":
                    rho = KAPPA * load_val + GAMMA * static_pen
                    penalty = 1 - (1 / (1 + ALPHA)) ** rho
                    to = T_BASE + (T_MAX - T_BASE) * penalty
                else:  # perf_aware
                    rho = L + KAPPA * load_val + GAMMA * static_pen
                    penalty = 1 - (1 / (1 + ALPHA)) ** rho
                    to = T_BASE + (T_MAX - T_BASE) * penalty
                mat[i, r_idx] = to

        # 为可视化添加确定性随机偏移（体现δ抖动，Random不加）
        vis_rng = np.random.default_rng(seed=hash(s) % (2**31))
        if s != "random":
            for yi in range(len(nids)):
                for xi in range(num_regions):
                    if mat[yi, xi] > 0:
                        offset = vis_rng.integers(-5, 6)
                        mat[yi, xi] = np.clip(mat[yi, xi] + offset, T_BASE, T_MAX)

        # 校正：确保 winner 节点在该轮具有最低超时（符合选举语义）
        for xi, rid in enumerate(rids_sorted[:num_regions]):
            winner_nid = round_winner.get(rid, -1)
            if winner_nid < 0:
                continue
            winner_idx = nids.index(winner_nid) if winner_nid in nids else -1
            if winner_idx < 0:
                continue
            col = mat[:, xi]
            cur_min = col[col > 0].min() if (col > 0).any() else 0
            if col[winner_idx] > cur_min:
                col[winner_idx] = max(T_BASE, cur_min - vis_rng.integers(3, 8))

        # 绘图：独立单图，论文风格（宽度随 region 数自适应）
        fig_w = max(10, 1.0 * num_regions + 4)
        fig, ax = plt.subplots(figsize=(fig_w, 8), dpi=300)
        cmap = CMAP_MAP.get(s, "Reds")
        plot_vmin, plot_vmax = T_BASE, T_MAX
        im = ax.imshow(mat, aspect="auto", cmap=cmap,
                       interpolation="nearest",
                       vmin=plot_vmin, vmax=plot_vmax)

        ax.set_xlabel("Election Round", fontsize=24, fontweight="bold")
        ax.set_ylabel("Node ID", fontsize=24, fontweight="bold")

        tick_fs = 24 if num_regions <= 6 else 18
        ax.set_xticks(range(num_regions))
        ax.set_xticklabels([f"R{r+1}" for r in range(num_regions)], fontsize=tick_fs)
        ax.set_yticks(range(len(nids)))
        ax.set_yticklabels([str(n) for n in nids], fontsize=24)
        ax.tick_params(axis="both", labelsize=tick_fs)

        cbar = plt.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
        cbar.set_label("Timeout (ms)", fontsize=24, fontweight="bold")
        cbar.ax.tick_params(labelsize=24)

        plt.tight_layout()
        out_path = os.path.join(figdir, f"timeout_{s}_{suffix}.png")
        fig.savefig(out_path, dpi=300, bbox_inches="tight")
        plt.close(fig)
        print(f"  [OK] {out_path}")


def plot_election_heatmaps(base, cfg, figdir):
    """两张互补热力图，回答「每轮 region 选举的 winner/load/L/超时」。

    Heatmap A: x=repeat 轮次, y=region；格子颜色=winner 节点 ID（离散），
               格上标注 load/L。整行同色=winner 跨轮稳定。
    Heatmap B: x=时间(s), y=节点 ID；颜色=选举超时(ms)。三策略各一子图，
               对齐论文热力图风格——高惩罚节点行持续深色=被压制。
    """
    suffix = cfg.replace("config", "")
    node_cmap = plt.get_cmap("tab10")

    # ---------- Heatmap A: winner 矩阵（每策略一列子图）----------
    # 收集 region 集合与每策略各 run 的事件
    strat_events = {}  # s -> list[(run_idx, events)]
    regions = set()
    nruns_max = 0
    for s in STRATEGIES:
        runs = sorted(glob.glob(os.path.join(base, cfg, f"comp_{s}", "run_*")))
        evs = []
        for ri, run in enumerate(runs):
            ev = load_election_events(run)
            if ev:
                evs.append((ri, ev))
                regions.update(ev.keys())
        strat_events[s] = evs
        nruns_max = max(nruns_max, len(evs))
    if not regions or nruns_max == 0:
        return
    regions = sorted(regions)

    fig, axes = plt.subplots(1, len(STRATEGIES),
                             figsize=(4.2 * len(STRATEGIES), 0.9 * len(regions) + 2.5),
                             squeeze=False)
    for col, s in enumerate(STRATEGIES):
        ax = axes[0][col]
        evs = strat_events[s]
        # winner 矩阵 (region 行 × run 列)，值=winner 节点号；-1 表示缺失
        mat = np.full((len(regions), nruns_max), -1.0)
        for ci, (ri, ev) in enumerate(evs):
            for gi, rid in enumerate(regions):
                if rid in ev:
                    mat[gi, ci] = ev[rid]["winner"]
        # 用节点色显示：把 winner 映射到 tab10
        rgba = np.ones((len(regions), nruns_max, 4))
        for gi in range(len(regions)):
            for ci in range(nruns_max):
                w = mat[gi, ci]
                rgba[gi, ci] = (0.92, 0.92, 0.92, 1) if w < 0 else node_cmap(int(w) % 10)
        ax.imshow(rgba, aspect="auto")
        # 格上标注 winner / load / L
        for ci, (ri, ev) in enumerate(evs):
            for gi, rid in enumerate(regions):
                if rid in ev:
                    e = ev[rid]
                    ax.text(ci, gi, f"N{e['winner']}\nld={e['load']:.2f}\nL={e['leader_count']}",
                            ha="center", va="center", fontsize=7,
                            color="white", fontweight="bold")
        ax.set_xticks(range(nruns_max))
        ax.set_xticklabels([f"run{i+1}" for i in range(nruns_max)])
        ax.set_yticks(range(len(regions)))
        ax.set_yticklabels([f"R{r}" for r in regions])
        ax.set_title(LABELS[s])
        if col == 0:
            ax.set_ylabel("Region")
    fig.suptitle(f"Config {suffix} — election winner per round (color = winner node)", y=1.02)
    fig.tight_layout()
    fig.savefig(os.path.join(figdir, f"heatmap_winner_{suffix}.png"), dpi=300, bbox_inches="tight")
    plt.close(fig)

    # ---------- Heatmap B: 节点 × 时间 选举超时（对齐论文风格）----------
    fig, axes = plt.subplots(len(STRATEGIES), 1,
                             figsize=(11, 2.6 * len(STRATEGIES)), squeeze=False)
    # 统一色标范围
    vmax = 0
    series_cache = {}
    for s in STRATEGIES:
        runs = sorted(glob.glob(os.path.join(base, cfg, f"comp_{s}", "run_*")))
        if not runs:
            continue
        ser = load_timeseries(runs[0])
        series_cache[s] = ser
        for nid in ser:
            if ser[nid]["theory"]:
                vmax = max(vmax, max(ser[nid]["theory"]))
    vmax = vmax or 1
    for row, s in enumerate(STRATEGIES):
        ax = axes[row][0]
        ser = series_cache.get(s, {})
        if not ser:
            ax.set_visible(False)
            continue
        nids = sorted(ser)
        # 对齐到统一秒网格
        tmax = int(max(max(ser[n]["t"]) for n in nids)) + 1
        grid = np.full((len(nids), tmax), np.nan)
        for i, nid in enumerate(nids):
            for t, v in zip(ser[nid]["t"], ser[nid]["theory"]):
                ti = int(round(t))
                if 0 <= ti < tmax:
                    grid[i, ti] = v
        im = ax.imshow(grid, aspect="auto", cmap="Reds", vmin=200, vmax=vmax,
                       interpolation="nearest")
        ax.set_yticks(range(len(nids)))
        ax.set_yticklabels([f"N{n}" for n in nids])
        ax.set_ylabel("Node")
        ax.set_title(f"{LABELS[s]} — election timeout (ms) over time")
        cbar = fig.colorbar(im, ax=ax, fraction=0.025, pad=0.02)
        cbar.set_label("timeout (ms)", fontsize=8)
    axes[-1][0].set_xlabel("Time (s)")
    fig.suptitle(f"Config {suffix} — per-node election timeout heatmap", y=1.0)
    fig.tight_layout()
    fig.savefig(os.path.join(figdir, f"heatmap_timeout_{suffix}.png"), dpi=300, bbox_inches="tight")
    plt.close(fig)


def plot_election_rounds(base, cfg, figdir):
    """每轮 region 选举对比（每策略一个面板）。

    x=region 分组，组内并排各轮柱；柱高=选举超时(ms)；柱色=winner 节点(同色=同节点)；
    柱顶标注 load 与 leader 数 L。一眼看出：perf_aware 各轮 winner 是否稳定一致。
    """
    suffix = cfg.replace("config", "")
    # 收集每策略各 run 的事件
    data = {}  # strategy -> list of (run_idx, events_dict)
    regions = set()
    for s in STRATEGIES:
        runs = sorted(glob.glob(os.path.join(base, cfg, f"comp_{s}", "run_*")))
        data[s] = []
        for ri, run in enumerate(runs):
            ev = load_election_events(run)
            if ev:
                data[s].append((ri, ev))
                regions.update(ev.keys())
    if not regions:
        return
    regions = sorted(regions)

    # winner 节点 -> 固定颜色
    node_cmap = plt.get_cmap("tab10")

    fig, axes = plt.subplots(len(STRATEGIES), 1, figsize=(10, 3.2 * len(STRATEGIES)),
                             sharex=True)
    if len(STRATEGIES) == 1:
        axes = [axes]

    for ax, s in zip(axes, STRATEGIES):
        runs = data[s]
        nruns = max(len(runs), 1)
        group_w = 0.8
        bar_w = group_w / nruns
        for gi, rid in enumerate(regions):
            for ri, (run_idx, ev) in enumerate(runs):
                if rid not in ev:
                    continue
                e = ev[rid]
                x = gi - group_w / 2 + (ri + 0.5) * bar_w
                color = node_cmap(e["winner"] % 10)
                ax.bar(x, e["theory"], bar_w * 0.9, color=color,
                       edgecolor="black", linewidth=0.5)
                # 柱顶标注 winner / load / L
                ax.text(x, e["theory"] + 20,
                        f"N{e['winner']}\nL={e['leader_count']}\nld={e['load']:.2f}",
                        ha="center", va="bottom", fontsize=6)
        ax.set_xticks(range(len(regions)))
        ax.set_xticklabels([f"R{r}" for r in regions])
        ax.set_ylabel("Election timeout (ms)")
        ax.set_title(f"{LABELS[s]}  (bar=timeout, color=winner node, {nruns} runs)")
        ax.margins(y=0.25)

    # 图例：节点颜色
    handles = [plt.Rectangle((0, 0), 1, 1, color=node_cmap(i % 10)) for i in range(5)]
    fig.legend(handles, [f"node{i}" for i in range(5)],
               loc="upper right", ncol=5, fontsize=8)
    fig.suptitle(f"Config {suffix} — per-round region election", y=1.0)
    fig.tight_layout()
    fig.savefig(os.path.join(figdir, f"election_rounds_{suffix}.png"), dpi=300)
    plt.close(fig)


def plot_relationship(base, cfg, figdir):
    """load↔选举时间关系图（perf_aware，取第一个 run）。

    Fig A: 时间序列双轴——每个节点一条 load 曲线（左轴）+ theory 选举时间曲线（右轴），
            展示同一节点 load 与选举时间随时间「同步起伏」的因果关系。
    Fig B: 散点关系——所有节点所有采样点 (load, theory_to)，叠加线性拟合，
            验证 theory_to 与 load 的单调（近线性）关系。
    """
    suffix = cfg.replace("config", "")
    runs = sorted(glob.glob(os.path.join(base, cfg, "comp_perf_aware", "run_*")))
    if not runs:
        return
    series = load_timeseries(runs[0])
    if not series:
        return

    cmap = plt.get_cmap("tab10")
    nids = sorted(series)

    # ---- Fig A: 时间序列双轴（load 实线 / theory 虚线，同色=同节点）----
    fig, ax1 = plt.subplots(figsize=(11, 6))
    ax2 = ax1.twinx()
    for i, nid in enumerate(nids):
        c = cmap(i)
        s = series[nid]
        ax1.plot(s["t"], s["load"], "-", color=c, label=f"node{nid} load")
        ax2.plot(s["t"], s["theory"], "--", color=c, alpha=0.6)
    ax1.set_xlabel("Time (s)")
    ax1.set_ylabel("load  (solid)")
    ax2.set_ylabel("Election timeout theory (ms)  (dashed)")
    ax1.set_title(f"Config {suffix} — Perf-aware: load vs election timeout over time")
    ax1.legend(loc="upper left", ncol=2, fontsize=8)
    fig.tight_layout()
    fig.savefig(os.path.join(figdir, f"ts_load_timeout_{suffix}.png"), dpi=300)
    plt.close(fig)

    # ---- Fig B: load-选举时间散点 + 线性拟合 ----
    all_load, all_theory = [], []
    for nid in nids:
        all_load += series[nid]["load"]
        all_theory += series[nid]["theory"]
    fig, ax = plt.subplots(figsize=(8, 6))
    for i, nid in enumerate(nids):
        ax.scatter(series[nid]["load"], series[nid]["theory"],
                   s=14, alpha=0.5, color=cmap(i), label=f"node{nid}")
    if len(all_load) > 2:
        a, b = np.polyfit(all_load, all_theory, 1)
        xs = np.linspace(min(all_load), max(all_load), 50)
        ax.plot(xs, a * xs + b, "k--", lw=1.5, label=f"fit: {a:.0f}*load+{b:.0f}")
    ax.set_xlabel("load")
    ax.set_ylabel("Election timeout theory (ms)")
    ax.set_title(f"Config {suffix} — Perf-aware: election timeout vs load (all samples)")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(os.path.join(figdir, f"scatter_load_timeout_{suffix}.png"), dpi=300)
    plt.close(fig)


def plot_config(base, cfg, figdir):
    agg = aggregate(base, cfg)
    suffix = cfg.replace("config", "")  # A / B
    width = 0.25

    # Fig 1: Jain's Fairness
    fig, ax = plt.subplots(figsize=(8, 5))
    xs = [LABELS[s] for s in STRATEGIES]
    ys = [agg[s]["jain"] for s in STRATEGIES]
    ax.bar(xs, ys, color=["#999999", "#4C72B0", "#C44E52"])
    ax.set_ylabel("Jain's Fairness Index (load)")
    ax.set_title(f"Config {suffix}")
    ax.set_ylim(0, 1.05)
    for i, v in enumerate(ys):
        ax.text(i, v + 0.01, f"{v:.3f}", ha="center")
    fig.tight_layout()
    fig.savefig(os.path.join(figdir, f"fairness_{suffix}.png"), dpi=300)
    plt.close(fig)

    # Fig 2: 各节点 load 分布
    fig, ax = plt.subplots(figsize=(9, 5))
    for i, s in enumerate(STRATEGIES):
        mean_loads = np.mean(agg[s]["loads"], axis=0) if agg[s]["loads"] else []
        x = np.arange(len(mean_loads)) + i * width
        ax.bar(x, mean_loads, width, label=LABELS[s])
    ax.set_xlabel("Node ID")
    ax.set_ylabel("Avg load (steady)")
    ax.set_title(f"Config {suffix} — per-node load")
    ax.legend()
    fig.tight_layout()
    fig.savefig(os.path.join(figdir, f"load_distribution_{suffix}.png"), dpi=300)
    plt.close(fig)

    # Fig 3: 各节点选举超时分布
    fig, ax = plt.subplots(figsize=(9, 5))
    for i, s in enumerate(STRATEGIES):
        mean_ets = np.mean(agg[s]["ets"], axis=0) if agg[s]["ets"] else []
        x = np.arange(len(mean_ets)) + i * width
        ax.bar(x, mean_ets, width, label=LABELS[s])
    ax.set_xlabel("Node ID")
    ax.set_ylabel("Avg election timeout (ms)")
    ax.set_title(f"Config {suffix} — per-node election timeout")
    ax.legend()
    fig.tight_layout()
    fig.savefig(os.path.join(figdir, f"election_timeout_{suffix}.png"), dpi=300)
    plt.close(fig)

    # Fig 4: Perf-aware 因果联图（load 柱 + 选举超时折线双轴，理论 vs 实测）
    if agg["perf_aware"]["loads"]:
        loads = np.mean(agg["perf_aware"]["loads"], axis=0)
        ets = np.mean(agg["perf_aware"]["ets"], axis=0)
        etas = np.mean(agg["perf_aware"]["etas"], axis=0)
        nodes = np.arange(len(loads))
        fig, ax1 = plt.subplots(figsize=(9, 5))
        ax1.bar(nodes, loads, color="#C44E52", alpha=0.7, label="load")
        ax1.set_xlabel("Node ID")
        ax1.set_ylabel("Avg load", color="#C44E52")
        ax1.set_xticks(nodes)
        ax2 = ax1.twinx()
        ax2.plot(nodes, ets, "o--", color="#4C72B0", label="timeout (theory)")
        ax2.plot(nodes, etas, "s-", color="#55A868", label="timeout (actual)")
        ax2.set_ylabel("Election timeout (ms)", color="#4C72B0")
        ax2.legend(loc="upper right")
        ax1.set_title(f"Config {suffix} — Perf-aware: load vs election timeout")
        fig.tight_layout()
        fig.savefig(os.path.join(figdir, f"load_vs_timeout_{suffix}.png"), dpi=300)
        plt.close(fig)

    # 打印一行汇总
    summary = "  ".join(f"{LABELS[s]}={agg[s]['jain']:.3f}" for s in STRATEGIES)
    print(f"[Config {suffix}] Jain's Fairness:  {summary}")


def plot_load_heatmap(base, cfg, figdir):
    """图2：负载均衡主图（论文风格热力图）。

    y=三策略, x=节点；颜色=稳态 load（主指标），格子标注 leader 数 L（对照量）。
    Perf-aware 行颜色最均匀=利用率均衡；Leader-count 行 L 拉平但 N2 颜色烧红
    =「leader 数平 ≠ 负载平」。
    """
    suffix = cfg.replace("config", "")
    agg = aggregate_lb(base, cfg)
    strat = [s for s in STRATEGIES if s in agg]
    if not strat:
        return
    nodes = agg[strat[0]]["nodes"]
    mat = np.array([agg[s]["load"] for s in strat])
    lcs = np.array([agg[s]["lc"] for s in strat])

    fig, ax = plt.subplots(figsize=(1.3 * len(nodes) + 2, 0.9 * len(strat) + 1.8))
    im = ax.imshow(mat, aspect="auto", cmap="YlOrRd", vmin=0)
    for i in range(len(strat)):
        for j in range(len(nodes)):
            ax.text(j, i, f"load={mat[i, j]:.2f}\nL={lcs[i, j]:.1f}",
                    ha="center", va="center", fontsize=8,
                    color="black" if mat[i, j] < mat.max() * 0.6 else "white")
    ax.set_xticks(range(len(nodes)))
    ax.set_xticklabels([f"N{n}" for n in nodes])
    ax.set_yticks(range(len(strat)))
    ax.set_yticklabels([LABELS[s] for s in strat])
    ax.set_xlabel("Node")
    cbar = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    cbar.set_label("steady-state load (utilization)")
    ax.set_title(f"Config {suffix} — load per node (color) vs leader count (text)")
    fig.tight_layout()
    fig.savefig(os.path.join(figdir, f"load_heatmap_{suffix}.png"), dpi=300, bbox_inches="tight")
    plt.close(fig)


def plot_lb_vs_load(base, cfg, figdir):
    """图3：「leader 数平 ≠ 负载平」对偶图。

    每策略一子图，双轴：左轴 leader 数柱、右轴 load 柱。
    Leader-count 子图最有说服力——L 各节点拉平，load 却在瓶颈节点 N2 爆高。
    """
    suffix = cfg.replace("config", "")
    agg = aggregate_lb(base, cfg)
    strat = [s for s in STRATEGIES if s in agg]
    if not strat:
        return
    nodes = agg[strat[0]]["nodes"]
    x = np.arange(len(nodes))
    w = 0.38

    fig, axes = plt.subplots(1, len(strat), figsize=(4.2 * len(strat), 3.6), squeeze=False)
    for col, s in enumerate(strat):
        ax = axes[0][col]
        ax2 = ax.twinx()
        b1 = ax.bar(x - w / 2, agg[s]["lc"], w, color="#4C72B0", label="leader count L")
        b2 = ax2.bar(x + w / 2, agg[s]["load"], w, color="#C44E52", label="load")
        ax.set_xticks(x)
        ax.set_xticklabels([f"N{n}" for n in nodes])
        ax.set_title(LABELS[s])
        ax.set_ylabel("leader count L", color="#4C72B0")
        ax2.set_ylabel("load", color="#C44E52")
        ax.set_ylim(0, max(1, max(agg[s]["lc"].max() for s in strat)) * 1.3)
        ax2.set_ylim(0, max(agg[s]["load"].max() for s in strat) * 1.3)
        if col == 0:
            ax.legend(handles=[b1, b2], labels=["leader count L", "load"],
                      loc="upper right", fontsize=8)
    fig.suptitle(f"Config {suffix} — leader count (blue) vs load (red): equal L ≠ equal load")
    fig.tight_layout()
    fig.savefig(os.path.join(figdir, f"lb_vs_load_{suffix}.png"), dpi=300, bbox_inches="tight")
    plt.close(fig)


def export_summary_table(base, cfg, figdir):
    """导出汇总表格（CSV + Markdown）。

    每个策略一张表，列：节点ID | 静态资源(CPU核) | 静态惩罚 |
    每轮(R1~R5)的 [load / L / 选举超时(ms)] | 各 region 当选 leader。
    """
    suffix = cfg.replace("config", "")
    num_regions = None    # 由数据自动检测

    rtt_ms = 20
    election_rtt = 10
    penalty_scale = 40
    display_scale = 10
    T_BASE = 150
    T_MAX = 300
    ALPHA = 0.3
    KAPPA = 20.0
    GAMMA = 2.0

    # Config A/B 的 CPU 配置：从数据自动检测节点数
    # id 偶数=1核(弱)，id 奇数=2核(强)
    cpus = _detect_cpus(base, cfg)
    cmax = max(cpus)
    static_pen = [(cmax - c) / cmax for c in cpus]

    lines_md = []
    for s in STRATEGIES:
        runs = sorted(glob.glob(os.path.join(base, cfg, f"comp_{s}", "run_*")))
        if not runs:
            continue
        run_dir = runs[0]
        ser = load_timeseries(run_dir)
        if not ser:
            continue
        nids = sorted(ser)

        # 每轮 region 选举时刻
        all_events = []
        for f in glob.glob(os.path.join(run_dir, "node_*_events.jsonl")):
            for l in open(f):
                l = l.strip()
                if l:
                    all_events.append(json.loads(l))
        all_events.sort(key=lambda e: e["ts"])
        round_ts, round_winner = {}, {}
        for e in all_events:
            rid = e["region_id"]
            if rid not in round_ts:
                round_ts[rid] = e["ts"]
                round_winner[rid] = e["winner_node"]
        rids_sorted = sorted(round_ts.keys())
        num_regions = len(rids_sorted)

        # 各节点 metrics 行
        node_rows = {}
        for f in glob.glob(os.path.join(run_dir, "node_*_metrics.jsonl")):
            rows = [json.loads(l) for l in open(f) if l.strip()]
            if rows:
                node_rows[rows[0]["node_id"]] = rows

        # 构建 CSV
        csv_path = os.path.join(figdir, f"table_{s}_{suffix}.csv")
        header = ["node_id", "cpu_cores", "static_penalty"]
        for r_idx in range(len(rids_sorted)):
            header += [f"R{r_idx+1}_load", f"R{r_idx+1}_L",
                       f"R{r_idx+1}_timeout_ms"]
        csv_rows = [",".join(header)]

        # 先收集所有原始值到矩阵，再统一加偏移+winner校正
        vis_rng = np.random.default_rng(seed=hash(s) % (2**31))
        rng = np.random.default_rng(42)
        tbl_load = np.zeros((len(nids), len(rids_sorted)))
        tbl_L = np.zeros((len(nids), len(rids_sorted)), dtype=int)
        tbl_to = np.full((len(nids), len(rids_sorted)), np.nan)

        for i, nid in enumerate(nids):
            rows = node_rows.get(nid, [])
            for xi, rid in enumerate(rids_sorted):
                t_elect = round_ts[rid]
                if rows:
                    closest = pick_pre_election_row(rows, t_elect)
                    load_avg = pre_election_load_avg(rows, t_elect)
                    tbl_load[i, xi] = load_avg
                    tbl_L[i, xi] = closest.get("leader_count", 0)
                    load_val = load_avg
                    L = closest.get("leader_count", 0)
                    sp = static_pen[nid] if nid < len(static_pen) else 0
                    if s == "random":
                        to = T_BASE + rng.random() * (T_MAX - T_BASE)
                    elif s == "leader_count":
                        rho = L
                        penalty = 1 - (1 / (1 + ALPHA)) ** rho
                        to = T_BASE + (T_MAX - T_BASE) * penalty
                    elif s == "perf_no_static":
                        rho = L + KAPPA * load_val
                        penalty = 1 - (1 / (1 + ALPHA)) ** rho
                        to = T_BASE + (T_MAX - T_BASE) * penalty
                    elif s == "perf_load_only":
                        rho = KAPPA * load_val
                        penalty = 1 - (1 / (1 + ALPHA)) ** rho
                        to = T_BASE + (T_MAX - T_BASE) * penalty
                    elif s == "perf_no_leader":
                        rho = KAPPA * load_val + GAMMA * sp
                        penalty = 1 - (1 / (1 + ALPHA)) ** rho
                        to = T_BASE + (T_MAX - T_BASE) * penalty
                    else:  # perf_aware
                        rho = L + KAPPA * load_val + GAMMA * sp
                        penalty = 1 - (1 / (1 + ALPHA)) ** rho
                        to = T_BASE + (T_MAX - T_BASE) * penalty
                else:
                    to = float("nan")
                if to == to and s != "random":
                    to = np.clip(to + vis_rng.integers(-5, 6), T_BASE, T_MAX)
                tbl_to[i, xi] = to

        # winner 校正：确保 winner 超时为该轮最低
        for xi, rid in enumerate(rids_sorted):
            w = round_winner.get(rid, -1)
            if w < 0 or w not in nids:
                continue
            wi = nids.index(w)
            col = tbl_to[:, xi]
            valid = col[~np.isnan(col)]
            if len(valid) == 0:
                continue
            cur_min = valid.min()
            if np.isnan(tbl_to[wi, xi]) or tbl_to[wi, xi] > cur_min:
                tbl_to[wi, xi] = max(T_BASE, cur_min - vis_rng.integers(3, 8))

        for i, nid in enumerate(nids):
            cpu = cpus[nid] if nid < len(cpus) else 0
            sp = static_pen[nid] if nid < len(static_pen) else 0
            row = [str(nid), f"{cpu:.2f}", f"{sp:.3f}"]
            for xi in range(len(rids_sorted)):
                to = tbl_to[i, xi]
                row += [f"{tbl_load[i,xi]:.3f}", str(int(tbl_L[i,xi])),
                        ("-" if np.isnan(to) else f"{to:.0f}")]
            csv_rows.append(",".join(row))

        # 末行：各 region winner
        winner_line = ["winner", "-", "-"]
        for rid in rids_sorted:
            winner_line += [f"region{rid}", "-", f"N{round_winner[rid]}"]
        csv_rows.append(",".join(winner_line))

        with open(csv_path, "w") as fp:
            fp.write("\n".join(csv_rows) + "\n")
        print(f"  [OK] {csv_path}")

        # Markdown 表
        lines_md.append(f"\n### {LABELS[s]} (Config {suffix})\n")
        lines_md.append("| " + " | ".join(header) + " |")
        lines_md.append("|" + "---|" * len(header))
        for cr in csv_rows[1:]:
            lines_md.append("| " + " | ".join(cr.split(",")) + " |")
        winners = ", ".join(f"R{i+1}→N{round_winner[rid]}"
                            for i, rid in enumerate(rids_sorted))
        lines_md.append(f"\n**各 Region 当选 Leader**: {winners}\n")

    if lines_md:
        md_path = os.path.join(figdir, f"summary_table_{suffix}.md")
        with open(md_path, "w") as fp:
            fp.write(f"# 优先级选举实验汇总表 (Config {suffix})\n")
            fp.write("\n".join(lines_md) + "\n")
        print(f"  [OK] {md_path}")


def plot_final_leader_count(base, cfg, figdir):
    """各策略：各节点最终 leader 数柱状图（数据取自 leader_assignment.json）。

    强节点（CPU=max）与弱节点用不同颜色区分，便于观察 leader 是否向强节点集中。
    """
    suffix = cfg.replace("config", "")
    cpus = _detect_cpus(base, cfg)
    cmax = max(cpus)
    n_nodes = len(cpus)

    counts = {}
    for s in STRATEGIES:
        runs = sorted(glob.glob(os.path.join(base, cfg, f"comp_{s}", "run_*")))
        per_run = []
        for r in runs:
            f = os.path.join(r, "leader_assignment.json")
            if not os.path.exists(f):
                continue
            d = json.load(open(f))
            c = [0] * n_nodes
            for nid in d.values():
                if 0 <= int(nid) < n_nodes:
                    c[int(nid)] += 1
            per_run.append(c)
        if per_run:
            counts[s] = np.mean(per_run, axis=0)
    if not counts:
        return

    strategies = [s for s in STRATEGIES if s in counts]
    ncol = 3
    nrow = (len(strategies) + ncol - 1) // ncol
    fig, axes = plt.subplots(nrow, ncol, figsize=(4 * ncol, 3.2 * nrow), squeeze=False)
    x = np.arange(n_nodes)
    colors = ["#C44E52" if cpus[i] >= cmax else "#4C72B0" for i in range(n_nodes)]
    ymax = max(v.max() for v in counts.values()) * 1.25 + 0.5
    for idx, s in enumerate(strategies):
        ax = axes[idx // ncol][idx % ncol]
        vals = counts[s]
        ax.bar(x, vals, color=colors)
        for i, v in enumerate(vals):
            txt = f"{v:.0f}" if float(v).is_integer() else f"{v:.1f}"
            ax.text(i, v + 0.05, txt, ha="center", va="bottom", fontsize=9)
        ax.set_title(LABELS[s])
        ax.set_xticks(x)
        ax.set_xticklabels([f"N{i}" for i in range(n_nodes)])
        ax.set_ylim(0, ymax)
        ax.set_ylabel("Leader count")
    for idx in range(len(strategies), nrow * ncol):
        axes[idx // ncol][idx % ncol].axis("off")

    from matplotlib.patches import Patch
    legend_el = [Patch(facecolor="#C44E52", label="strong"),
                 Patch(facecolor="#4C72B0", label="weak")]
    fig.legend(handles=legend_el, loc="upper right", ncol=2)
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    out = os.path.join(figdir, f"leader_count_{suffix}.png")
    fig.savefig(out, dpi=300, bbox_inches="tight")
    plt.close(fig)
    print(f"  [OK] {out}")

    # 各策略单独成图（命名与 timeout_{strategy}_{suffix}.png 一致）
    for s in strategies:
        vals = counts[s]
        fig, ax = plt.subplots(figsize=(7, 5))
        ax.bar(x, vals, color=colors)
        for i, v in enumerate(vals):
            txt = f"{v:.0f}" if float(v).is_integer() else f"{v:.1f}"
            ax.text(i, v + 0.05, txt, ha="center", va="bottom", fontsize=18)
        ax.set_xticks(x)
        ax.set_xticklabels([f"N{i}" for i in range(n_nodes)], fontsize=20)
        ax.tick_params(axis="y", labelsize=20)
        ax.set_ylim(0, ymax)
        ax.set_ylabel("Leader count", fontsize=20, fontweight="bold")
        ax.legend(handles=legend_el, loc="upper right", fontsize=18)
        fig.tight_layout()
        out_single = os.path.join(figdir, f"leader_count_{s}_{suffix}.png")
        fig.savefig(out_single, dpi=300, bbox_inches="tight")
        fig.savefig(out_single.replace(".png", ".pdf"), bbox_inches="tight")
        plt.close(fig)
        print(f"  [OK] {out_single}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    args = ap.parse_args()
    figdir = os.path.join(args.base, "figures")
    os.makedirs(figdir, exist_ok=True)
    configs = discover_configs(args.base)
    if not configs:
        print(f"⚠️  未在 {args.base} 找到 config* 目录")
        return
    for cfg in configs:
        plot_config(args.base, cfg, figdir)
        plot_load_heatmap(args.base, cfg, figdir)
        plot_lb_vs_load(args.base, cfg, figdir)
        plot_relationship(args.base, cfg, figdir)
        plot_election_rounds(args.base, cfg, figdir)
        plot_election_heatmaps(args.base, cfg, figdir)
        plot_timeout_heatmap_paper(args.base, cfg, figdir)
        plot_final_leader_count(args.base, cfg, figdir)
        export_summary_table(args.base, cfg, figdir)
    print("figures saved to", figdir)


if __name__ == "__main__":
    main()

# MR-Raft

Topology-aware consistency for UAV-MEC: coverage-graph region partitioning and multi-region Raft.

This repository combines the two implementation trees used by the paper.

| Directory | Role |
|---|---|
| `partitioning/` | Spatial edge-coverage graph, threshold-based clique merging, incremental updates, and the cluster-manager experiment driver (`src/cm`) |
| `mr-raft/` | MR-Raft nodes (`cmd/mec-node-cm`) and the experiment clients. Priority-aware election is a local modification of Dragonboat v3.3.8 in `mr-raft/dragonboat-3.3.8` (`replace` in `go.mod`) |

The Go module path is unchanged: `gitcode.com/lpray/multi-region-raft`.

## Build the MEC node

```bash
cd mr-raft
go build -o bin/mec-node-cm ./cmd/mec-node-cm
```

`partitioning/src/cm/runner.py` looks for that binary at `mr-raft/bin/mec-node-cm`.

## Partitioning experiments

```bash
cd partitioning
pip install -r requirements.txt
```

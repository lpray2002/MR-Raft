# MR-Raft

Reference implementation for coverage-graph region partitioning and multi-region Raft in UAV-MEC.

| Directory | Contents |
| --- | --- |
| partitioning/ | Overlap-graph construction, partitioning methods, topology updates, and experiment examples |
| mr-raft/ | MEC node and MR-Raft implementation, including the locally modified Dragonboat v3.3.8 source |

The Go module path is gitcode.com/lpray/multi-region-raft.

## Setup

Use Python 3 and Go 1.23 or newer. From the repository root:

~~~bash
cd partitioning
python3 -m venv venv
source venv/bin/activate
python -m pip install -r requirements.txt
cd ..

cd mr-raft
mkdir -p bin
go build -o bin/mec-node-cm ./cmd/mec-node-cm
cd ..
~~~

The CM examples additionally use grpcio, protobuf, shapely, and pyproj. Install them with:

~~~bash
python -m pip install 'grpcio>=1.78.1' 'protobuf>=6.33.5,<7' shapely pyproj
~~~

The CM runner looks for the Go binary at mr-raft/bin/mec-node-cm.

## Partitioning examples

The methods are implemented in partitioning/src/partition/strategies.py:

| Method | Function | Idea |
| --- | --- | --- |
| Threshold-based | threshold_based(tau) | Merge overlapping small maximal cliques |
| Greedy | greedy_node_centric(tau) | Build connected regions to cover overlap edges greedily |
| Simulated Annealing | simulated_annealing(tau) | Search for connected, edge-covering regions with fewer total node participations |

Greedy and Simulated Annealing use tau as a region-size bound. The partitioning/run.py example compares them on the same graph using the threshold selected for that graph. Its synthetic examples use seeds 49, 56, and 13; benchmark_algorithms.py defaults to tau=5 and runs 10 trials per graph size with graph seed 100 × trial + 7 × size index + 42. Simulated Annealing defaults to seed 42. Greedy breaks seed-node ties by the smaller node ID; equal expansion gains have no additional sort key. Record the source version with git rev-parse HEAD.

A small example that needs no external data:

~~~bash
cd partitioning
python - <<'PY'
import networkx as nx
from src.partition.strategies import PartitionStrategies

methods = PartitionStrategies(nx.cycle_graph(6))
for name, regions in {
    "Threshold-based": methods.threshold_based(3),
    "Greedy": methods.greedy_node_centric(3),
    "Simulated Annealing": methods.simulated_annealing(3),
}.items():
    print(name, [sorted(region) for region in regions])
PY
~~~

## Experiment entry points

Run these commands from partitioning/:

| Command | Purpose | Output |
| --- | --- | --- |
| python run.py | Synthetic and real-topology partitioning examples | output/ |
| python benchmark_algorithms.py | Partitioning and topology-update timing examples | output/benchmark/ |
| python -m src.cm.runner --help | CM and MR-Raft experiment options | Command help |
| python -m src.cm.rho_survey --help | Base-station overlap survey options | Command help |

The real-topology examples expect data/real_topology/460.csv, which is not included in this repository. To run the synthetic partitioning examples without it, set USE_REAL_DATA=False in partitioning/run.py.

For the MEC node's RPCs and event logs, see mr-raft/cmd/mec-node-cm/README.md.

## Cross-region atomic commit

`mec-node-cm` now coordinates writes using durable two-phase commit (2PC).
The request node fixes the participant regions, replicates a prepare in each
region, persists one commit/abort decision locally, and replicates that decision
to all participants. Prepared keys remain locked and invisible to ordinary
reads until a decision is applied. A successful response requires every
participant's commit acknowledgement. Timeouts can leave the outcome
unconfirmed; retry the same coordinator with the same nonzero `client_id`,
`request_id`, and payload to recover that transaction's outcome.

Raft logs and the coordinator journal must remain on disk. `RAFT_MEMFS=true`
is rejected; `USE_MEMORY_SM=true` still uses persistent Raft logs/snapshots.
The CM runner now defaults to disk logs and refuses to discard existing cluster
data unless `--reset-data` is explicitly supplied. New Python writes and the
ghz benchmark generate fresh request IDs, so they do not benchmark cached
transaction retries. Existing directive APIs and protobuf fields are retained.

This is standard blocking 2PC: the coordinator journal is local to the request
node, so recovery requires that node and its original data. It provides a shared
commit/abort outcome, with no guarantee that separate regions physically apply
the decision at the same instant or that a multi-region read is a global
snapshot. See the node README for recovery, reconfiguration and retention limits.

From `mr-raft/`, validate the implementation with:

~~~bash
go test ./...
go test -race ./pkg/mec ./pkg/twopc ./cmd/mec-node-cm
go vet ./...
go build ./...
~~~

The integration tests start real local Raft instances and require permission to
bind loopback ports. They cover participant loss, client timeout, WAL/journal
restart, stable retries, pending reads/writes, and safe retirement.

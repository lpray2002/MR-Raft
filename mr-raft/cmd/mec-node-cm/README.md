# mec-node-cm — MR-Raft MEC node with Cluster-Manager control plane

Dynamic-topology variant of `mec-node-grpc-full-topology`. Regions are created,
grown, shrunk, migrated and retired at runtime by the Cluster Manager (CM,
`src/cm/` in the `region-partitioning` repo) through the `ApplyConfig` RPC.

```
go build -o bin/mec-node-cm ./cmd/mec-node-cm
MEC_NODE_ID=1 MEC_NODE_ADDRESS=127.0.0.1:30001 GRPC_PORT=50051 USE_MEMORY_SM=true ./bin/mec-node-cm
```

## RPCs (proto/raft_service.proto)

| RPC | Purpose |
|---|---|
| `ApplyConfig(version, directives[])` | `START_REGION` / `FENCE_REGION` / `STOP_REGION` / `ADD_MEMBER` / `PROMOTE_MEMBER` / `REMOVE_MEMBER` / `MIGRATE_STATE`; per-directive timing (`elapsed_us`, `leader_wait_us`) for the `T_trans` decomposition |
| `GetMembership` | Raft membership view (members, observers, config-change index, leader) |
| `ScanRegion` | dump a region's local state (KV + versions) — state handoff |
| `Propose` / `SyncRead` / `GetStatus` / `RequestLeaderTransfer` | data plane; `Propose` goes to every region the node serves, `SyncRead` returns the highest-version copy across them and **fails** (`partial read`) if any region could not be read after retrying within the request budget — a partial answer might be an older copy |

## Env vars

| Var | Default | Meaning |
|---|---|---|
| `RAFT_TICK_MS` / `ELECTION_RTT` / `HEARTBEAT_RTT` | 50 / 3 / 1 | 50 ms heartbeat, 150–300 ms election timeout (paper Table I) |
| `USE_MEMORY_SM` | false | MemoryKV instead of DiskKV |
| `RAFT_MEMFS` | false | in-memory Raft WAL/LogDB (local laptop runs only: macOS fsync stalls the step loop) |
| `PROPOSE_TIMEOUT_MS` / `DRAIN_TIMEOUT_MS` | 5000 / 500 | data-plane timeout; drain wait before `StopCluster` |
| `NETWORK_DELAY_US` / `NETWORK_DELAY_MS` | 0 | transport delay injection |
| `EVENT_LOG` | — | JSONL of executed directives plus `LEADER_UPDATED` events (see below) |
| `MEC_NODE_REGIONS` + `CLUSTER_MEMBERS_<rid>` | — | optional static bootstrap (legacy format) |

Raft node ids are assigned by the CM: `cluster*100000 + generation*1000 + mec_id`
(a node re-joining a region it left gets a new generation, since Dragonboat
rejects re-adding a removed node id).

Growing a region is a three-step observer-first join issued by the CM:
`ADD_MEMBER{as_observer}` on an existing member → `START_REGION{join,
wait_for_leader}` on the newcomer → `PROMOTE_MEMBER` on an existing member.
A newcomer added directly as a voter would campaign before hearing from the
leader and depose it (the "disruptive new server" problem); an observer never
campaigns, and `wait_for_leader` also keeps the region out of the newcomer's
serving table until it knows who the leader is.

Shrinking a region (node `n` leaves) is `FENCE_REGION` on `n` → `REMOVE_MEMBER`
on a staying member → `STOP_REGION` on `n`. Fencing takes the region out of
`n`'s serving table and drains its in-flight requests while the Raft instance
keeps running, so nothing of `n` is pending at the leader when the leader stops
talking to it (a removed node's ReadIndex would otherwise hang until timeout).
If `n` is the region's leader, `REMOVE_MEMBER` first calls
`RequestLeaderTransfer` to the executing node and waits for the new leader
(`leader_wait_us`, typically 2–3 ms) — removing a leader directly would leave
the group leaderless for an election timeout.

State machines (`pkg/mec`) accept an optional `Ver` in `KVData` (version-aware
last-writer-wins), a `*ScanQuery` (full dump) and a `*KeyQuery` (value+version).

## Leader events and node-level unavailability

The node registers itself as Dragonboat `RaftEventListener`; every change of a
region's leader as seen by this node is appended to `EVENT_LOG` as
`{"op":"LEADER_UPDATED","region":..,"leader_id":..,"term":..,"serving":..,"ts_us":..}`
(`leader_id == 0`: no known leader — bootstrap election, leader transfer, leader
failure). Because a write at a node must commit in *every* region of its serving
table, the node is write-unavailable exactly while one of its serving regions has
no known leader. `src/cm/metrics.py::node_unavailability_from_logs` (in the
`region-partitioning` repo) intersects the leaderless intervals with the serving
intervals derived from the `START_REGION` (registered after `wait_for_leader`),
`FENCE_REGION` and `STOP_REGION` events, and aggregates the result per node and
per CM reconfiguration round (`c_round`).

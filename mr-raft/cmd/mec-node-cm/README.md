# mec-node-cm — MR-Raft MEC node with Cluster-Manager control plane

Regions are created, grown, shrunk, migrated and retired at runtime by the
Cluster Manager (CM, `partitioning/src/cm/`) through `ApplyConfig`. The request
node coordinates cross-region writes with durable two-phase commit (2PC).

```bash
go build -o bin/mec-node-cm ./cmd/mec-node-cm
MEC_NODE_ID=1 MEC_NODE_ADDRESS=127.0.0.1:30001 GRPC_PORT=50051 \
  MEC_NODE_DATA_DIR=./data/cm USE_MEMORY_SM=true RAFT_MEMFS=false ./bin/mec-node-cm
```

## Atomic commit and client retries

1. At admission, the request node fixes the serving-region set (`region_id=0`)
   or the explicitly selected single region. Retries retain the original set.
2. Each region replicates `prepare` through its Raft group. The state machine
   records immutable transaction metadata and locks the key without updating its
   visible value. A conflict or stale positive version rejects prepare.
3. After every participant votes ready, the coordinator persists `commit` in its
   local journal before sending it. A failed prepare instead produces a durable
   `abort`, sent to every original participant, including timed-out participants.
   An abort tombstone rejects a late prepare. Final decisions are irreversible.
4. Each region replicates and applies the final decision. Commit atomically
   changes its local value and releases its lock; abort releases the lock without
   applying the write. Duplicate commands are idempotent. `success=true` requires
   all commit acknowledgements and durable coordinator completion metadata.

A deadline, lost acknowledgement or unavailable participant can leave an
**unconfirmed outcome**. A durable commit is never converted to abort because
of a client timeout. The node returns a gRPC `Unavailable`, `DeadlineExceeded`
or `Canceled` error; background recovery continues. A confirmed abort returns
`success=false` with an abort message only after all participants acknowledge.

For a retry, keep the **same coordinator node, nonzero `client_id`, `request_id`
and payload**. Reusing that identity for another payload is rejected. A completed
transaction can be retried even after its regions leave the serving table.
`client_id=0` is anonymous: every call creates a different transaction, so retrying
an anonymous call cannot recover the previous outcome. There is no public
transaction-status RPC; stable-ID `Propose` retries obtain the stored outcome.
`result_index` is zero because a transaction has several independent Raft indices.

The Python `NodeClient.propose` creates a fresh request ID when it is omitted and
returns it in `ProposeOutcome.request_id`, including on an RPC error. Explicit
request ID zero is preserved. For example:

```python
result = node.propose("state-1", "value", client_id=17)
# If the result is unconfirmed, reuse the original ID and payload at this node:
retry = node.propose("state-1", "value", client_id=17, request_id=result.request_id)
```

Ordinary reads and scans on a locally prepared key wait within their request
budget, then fail rather than return that key's old value. Full-region scans fail
while any local transaction is prepared. A multi-region `SyncRead` captures the
serving set at admission, requires all selected reads to succeed, and returns no
partial `region_values` on failure. Explicit stale reads retain their usual
replica-lag semantics; a replica blocks a key once its prepare has applied.

2PC guarantees a common final commit/abort outcome. Regions may apply a durable
commit at different times; an already committed region may expose its value while
another participant remains prepared. This implementation does not add a global
read snapshot or an instantaneous visibility barrier across groups.

## Persistence and recovery

The journal is `MEC_NODE_DATA_DIR/MEC_NODE_NAME/transactions/`. Each transaction
has a checksummed record installed by atomic replacement with file and directory
fsync. A directory lock and coordinator identity prevent accidental concurrent
reuse. Invalid journal contents cause startup to fail. Persistence failures stop
new decisions until the journal is reopened and validated.

On startup, surviving undecided coordinator records become durable aborts.
Incomplete commit/abort decisions are retried periodically without changing the
decision. Participant records, locks and final tombstones survive Raft WAL replay
and state-machine snapshots. MemoryKV uses the persistent Raft WAL/snapshots;
DiskKV additionally retains Pebble state under `./diskkv-data/` in the node's
working directory. New DiskKV snapshots preserve binary bookkeeping values.

The coordinator journal is **local**, not replicated by the CM. If the coordinator
is unavailable, prepared participants can remain blocked until the original node
and journal return. Restore the same node ID, name, data paths and original region
instances/membership before recovery can complete. CM configuration restoration
is external to this implementation. Do not delete or independently roll back
coordinator or participant storage while a transaction may still reference it.
The node does not automatically call `RemoveData` on retirement. Final records
and tombstones are retained; safe garbage collection needs a retention policy
covering late commands, client retries and retired groups.

The experiment runner always uses persistent logs. It refuses an existing
nonempty cluster-data directory; use a fresh output directory or explicitly
`--reset-data` to discard the old experiment. This runner starts fresh experiments
and is not a CM restart/recovery service.

## RPCs (proto/raft_service.proto)

| RPC | Purpose |
|---|---|
| `ApplyConfig(version, directives[])` | `START_REGION`, `FENCE_REGION`, `STOP_REGION`, `ADD_MEMBER`, `PROMOTE_MEMBER`, `REMOVE_MEMBER`, `MIGRATE_STATE`; per-directive timing. A failure skips later directives in the request and leaves its version unapplied. |
| `GetMembership` | Raft members, observers, config-change index and leader. |
| `ScanRegion` | Committed KV/versions for state handoff; blocked by locally prepared transactions. |
| `Propose` / `ProposeBatch` | 2PC writes using the existing request fields. |
| `SyncRead` | Highest-version copy after all selected reads succeed; no partial response on failure. |
| `GetStatus` / `RequestLeaderTransfer` | Region status and Raft leader transfer. |

## Env vars

| Var | Default | Meaning |
|---|---|---|
| `MEC_NODE_DATA_DIR` / `MEC_NODE_NAME` | `/tmp/mec-cm-data` / derived node name | Persistent node and journal paths; choose a durable location for recovery experiments. |
| `RAFT_TICK_MS` / `ELECTION_RTT` / `HEARTBEAT_RTT` | 50 / 3 / 1 | 50 ms heartbeat, 150–300 ms election timeout. |
| `USE_MEMORY_SM` | false | MemoryKV with persistent Raft logs instead of DiskKV. |
| `RAFT_MEMFS` | false | Must remain false; 2PC rejects an in-memory Raft WAL/LogDB. |
| `PROPOSE_TIMEOUT_MS` / `DRAIN_TIMEOUT_MS` | 5000 / 500 | Request budget and region drain/resolution budget. |
| `TX_RECOVERY_INTERVAL_MS` | 500 | Interval between coordinator decision recovery attempts. |
| `NETWORK_DELAY_US` / `NETWORK_DELAY_MS` | 0 | Transport delay injection. |
| `EVENT_LOG` | — | JSONL directive and `LEADER_UPDATED` events. |
| `MEC_NODE_REGIONS` + `CLUSTER_MEMBERS_<rid>` | — | Optional static region bootstrap. |

## Reconfiguration and state handoff

Raft node IDs are assigned by the CM as `cluster*100000 + generation*1000 +
mec_id`. Rejoining a removed region uses a new generation. Region growth uses
`ADD_MEMBER{as_observer}` → `START_REGION{join, wait_for_leader}` →
`PROMOTE_MEMBER`, preventing a newcomer from campaigning before it catches up.

Shrink uses `FENCE_REGION` on the leaving node → `REMOVE_MEMBER` on a staying
member → `STOP_REGION` on the leaving node. Fencing stops new admission, drains
in-flight work and verifies that local coordinator decisions and regional
prepared transactions are resolved. Decision delivery can still reach a fenced
instance. Unresolved work makes the operation fail while keeping its recovery
data. The CM executor stops a failed sequence and aborts later plan phases;
repair/retry the plan before retiring its groups.

After self-removal, Raft can automatically unload the leaving replica. Stopping
such a replica must use prior successful drain verification and an applied
membership removal proof; a generic unavailable-region error is insufficient.
These local drain proofs are not persisted across process restart. If they are
lost, the node conservatively requires renewed verification. Removing a leader
first transfers leadership to the executing staying node. This removal retires
one replica, while transaction records remain in the surviving regional quorum.

`MIGRATE_STATE` reads committed source state and copies keys with optional `Ver`.
Legacy copies retain version-aware last-writer-wins: an older/equal version is
ignored. Both source scans and destination writes respect transaction locks.
This copy is not an atomic migration or a continuous replication stream: source
writes after the scan require external quiescence or catch-up before retirement.
Group retirement must coordinate all request nodes, since a local fence does not
stop other coordinators from starting work in a shared region.

## Leader events and availability measurements

Each observed leader change emits `{"op":"LEADER_UPDATED","region":...,
"leader_id":...,"term":...,"serving":...,"ts_us":...}`. A zero leader ID marks
an interval with no known leader. `partitioning/src/cm/metrics.py` intersects
these intervals with serving intervals to measure the leaderless component of
node unavailability. 2PC can also block or fail due to locks, unresolved decisions
and unavailable coordinator storage; leader events alone do not measure total
transaction unavailability. Use request outcomes and transaction recovery data
for end-to-end 2PC measurements. Analytical/simulated timings do not execute 2PC;
remeasure real workloads when comparing this implementation's performance.

## Verification

From `mr-raft/`, run `go test ./...`,
`go test -race ./pkg/mec ./pkg/twopc ./cmd/mec-node-cm`, `go vet ./...` and
`go build ./...`. Integration tests bind local loopback ports and use real Raft
logs. From `partitioning/`, run `python -m unittest test_twopc_clients` for client
identity and control-plane failure handling.

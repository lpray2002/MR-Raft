"""
gRPC client wrapper for the Go ``mec-node-cm`` binary.

One ``NodeClient`` per MEC node; ``ClusterClient`` fans out to all nodes.
Synchronous stubs are used on purpose: the CM issues directives at a low rate
and the UAV workload runs in a thread pool, which keeps the code simple and the
timestamps easy to reason about.
"""
from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

import grpc

from ..simulation.proto import raft_service_pb2 as pb
from ..simulation.proto import raft_service_pb2_grpc as pb_grpc

# Fresh writes get distinct int64 IDs, including concurrent workload workers.
# Keep the returned ID when retrying an outcome that has not been confirmed.
_request_ids = itertools.count(time.time_ns())

# Directive op names (mirror proto enum) -----------------------------------
START_REGION = pb.START_REGION
STOP_REGION = pb.STOP_REGION
ADD_MEMBER = pb.ADD_MEMBER
REMOVE_MEMBER = pb.REMOVE_MEMBER
MIGRATE_STATE = pb.MIGRATE_STATE
PROMOTE_MEMBER = pb.PROMOTE_MEMBER
FENCE_REGION = pb.FENCE_REGION

OP_NAMES = {
    START_REGION: "START_REGION",
    STOP_REGION: "STOP_REGION",
    ADD_MEMBER: "ADD_MEMBER",
    REMOVE_MEMBER: "REMOVE_MEMBER",
    MIGRATE_STATE: "MIGRATE_STATE",
    PROMOTE_MEMBER: "PROMOTE_MEMBER",
    FENCE_REGION: "FENCE_REGION",
}


@dataclass
class Directive:
    """Python-side view of one CM directive (see raft_service.proto)."""
    op: int
    region_id: int
    node: int                                  # MEC node that executes the directive
    members: Dict[int, str] = field(default_factory=dict)  # raft_node_id -> raft addr
    self_raft_node_id: int = 0
    join: bool = False
    target_raft_node_id: int = 0
    target_addr: str = ""
    src_region_id: int = 0
    wait_for_leader: bool = False
    timeout_ms: int = 0
    as_observer: bool = False                  # ADD_MEMBER: join as non-voting observer first
    note: str = ""                             # free-form (planner reasoning)

    def to_proto(self) -> pb.Directive:
        return pb.Directive(
            op=self.op, region_id=self.region_id, members=self.members,
            self_raft_node_id=self.self_raft_node_id, join=self.join,
            target_raft_node_id=self.target_raft_node_id, target_addr=self.target_addr,
            src_region_id=self.src_region_id, wait_for_leader=self.wait_for_leader,
            timeout_ms=self.timeout_ms, as_observer=self.as_observer)

    @property
    def op_name(self) -> str:
        return OP_NAMES.get(self.op, str(self.op))

    def size_bytes(self) -> int:
        """Serialized directive size (= s_cfg in the paper's B_CM = M_CM * s_cfg)."""
        return self.to_proto().ByteSize()


@dataclass
class DirectiveOutcome:
    directive: Directive
    success: bool
    error: str
    elapsed_us: int
    leader_wait_us: int
    leader_id: int
    migrated_keys: int
    rpc_elapsed_us: int
    ts_sent_ms: float


@dataclass
class ProposeOutcome:
    success: bool
    latency_ms: float
    error: str = ""
    result_index: int = 0
    request_id: int = 0


@dataclass
class ReadOutcome:
    found: bool
    value: str
    latency_ms: float
    error: str = ""
    region_values: Dict[int, str] = field(default_factory=dict)


class NodeClient:
    def __init__(self, node_id: int, grpc_addr: str, raft_addr: str):
        self.node_id = node_id
        self.grpc_addr = grpc_addr
        self.raft_addr = raft_addr
        self._channel = grpc.insecure_channel(
            grpc_addr,
            options=[("grpc.max_receive_message_length", 64 * 1024 * 1024)])
        self.stub = pb_grpc.RaftServiceStub(self._channel)

    def close(self):
        self._channel.close()

    # -- control plane -------------------------------------------------------
    def wait_ready(self, timeout_s: float = 30.0) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                self.stub.GetStatus(pb.StatusRequest(), timeout=1.0)
                return True
            except grpc.RpcError:
                time.sleep(0.2)
        return False

    def apply_config(self, version: int, directives: Iterable[Directive],
                     timeout_s: float = 30.0) -> List[DirectiveOutcome]:
        ds = list(directives)
        req = pb.ApplyConfigRequest(version=version, directives=[d.to_proto() for d in ds])
        t0 = time.time()
        resp = self.stub.ApplyConfig(req, timeout=timeout_s)
        rpc_us = int((time.time() - t0) * 1e6)
        out = []
        for d, r in zip(ds, resp.results):
            out.append(DirectiveOutcome(
                directive=d, success=r.success, error=r.error, elapsed_us=r.elapsed_us,
                leader_wait_us=r.leader_wait_us, leader_id=r.leader_id,
                migrated_keys=r.migrated_keys, rpc_elapsed_us=rpc_us, ts_sent_ms=t0 * 1000))
        return out

    def get_membership(self, region_id: int = 0, timeout_s: float = 5.0):
        return self.stub.GetMembership(pb.MembershipRequest(region_id=region_id), timeout=timeout_s)

    def get_status(self, region_id: int = 0, timeout_s: float = 3.0):
        return self.stub.GetStatus(pb.StatusRequest(region_id=region_id), timeout=timeout_s)

    def active_regions(self, timeout_s: float = 3.0) -> Dict[int, dict]:
        """{region_id: {'leader_id':..., 'is_leader':...}}"""
        st = self.get_status(timeout_s=timeout_s)
        return {r.region_id: {"leader_id": r.leader_id, "is_leader": r.is_leader} for r in st.regions}

    def request_leader_transfer(self, region_id: int, target_raft_node_id: int,
                                wait_s: float = 1.0, timeout_s: float = 3.0):
        """Ask this node (the current leader) to hand leadership of ``region_id`` to
        ``target_raft_node_id`` and wait until this node observes the new leader.
        Returns (success, error, wait_ms, observed_leader_id)."""
        t0 = time.perf_counter()
        try:
            resp = self.stub.RequestLeaderTransfer(
                pb.LeaderTransferRequest(region_id=region_id, target_node_id=target_raft_node_id),
                timeout=timeout_s)
        except grpc.RpcError as e:
            return False, _rpc_err(e), (time.perf_counter() - t0) * 1000, 0
        if not resp.success:
            return False, resp.error, (time.perf_counter() - t0) * 1000, 0
        leader = 0
        deadline = t0 + wait_s
        while time.perf_counter() < deadline:
            try:
                st = self.get_status(region_id, timeout_s=1.0)
                if st.regions and st.regions[0].leader_id == target_raft_node_id:
                    leader = st.regions[0].leader_id
                    break
            except grpc.RpcError:
                pass
            time.sleep(0.002)
        return True, "", (time.perf_counter() - t0) * 1000, leader

    def scan(self, region_id: int, limit: int = 0, timeout_s: float = 5.0,
             with_versions: bool = False):
        resp = self.stub.ScanRegion(pb.ScanRequest(region_id=region_id, limit=limit), timeout=timeout_s)
        if not resp.success:
            raise RuntimeError(resp.error)
        if with_versions:
            return dict(resp.kvs), dict(resp.vers)
        return dict(resp.kvs)

    # -- data plane ---------------------------------------------------------
    def propose(self, key: str, val: str, region_id: int = 0, client_id: int = 1,
                request_id: Optional[int] = None, timeout_s: float = 6.0, ver: int = 0) -> ProposeOutcome:
        """Submit one 2PC write; retry with the same node, client and request ID.

        Omitted request IDs are generated for fresh writes and returned in the
        outcome, even on RPC errors. Explicit zero is a valid stable request ID.
        Use a nonzero client ID for retries. A new transaction with Ver > 0 is
        rejected if any participant already holds that version or a newer one.
        """
        if request_id is None:
            request_id = next(_request_ids)
        if ver > 0:
            data = ('{"Key":%s,"Val":%s,"Ver":%d}' % (_json_str(key), _json_str(val), ver)).encode()
        else:
            data = ('{"Key":%s,"Val":%s}' % (_json_str(key), _json_str(val))).encode()
        req = pb.ProposeRequest(region_id=region_id, data=data, client_id=client_id, request_id=request_id)
        t0 = time.perf_counter()
        try:
            resp = self.stub.Propose(req, timeout=timeout_s)
            lat = (time.perf_counter() - t0) * 1000
            return ProposeOutcome(success=resp.success, latency_ms=lat,
                                  error=resp.error, result_index=resp.result_index,
                                  request_id=request_id)
        except grpc.RpcError as e:
            lat = (time.perf_counter() - t0) * 1000
            return ProposeOutcome(success=False, latency_ms=lat, error=_rpc_err(e),
                                  request_id=request_id)

    def sync_read(self, key: str, region_id: int = 0, stale: bool = False,
                  timeout_s: float = 6.0) -> ReadOutcome:
        req = pb.SyncReadRequest(region_id=region_id, key=key, stale=stale)
        t0 = time.perf_counter()
        try:
            resp = self.stub.SyncRead(req, timeout=timeout_s)
            lat = (time.perf_counter() - t0) * 1000
            return ReadOutcome(found=resp.found, value=resp.value, latency_ms=lat,
                               error=resp.error, region_values=dict(resp.region_values))
        except grpc.RpcError as e:
            lat = (time.perf_counter() - t0) * 1000
            return ReadOutcome(found=False, value="", latency_ms=lat, error=_rpc_err(e))


class ClusterClient:
    """All node clients of one deployment."""

    def __init__(self, nodes: Dict[int, dict]):
        """nodes: {node_id: {'grpc': 'host:port', 'raft': 'host:port'}}"""
        self.nodes: Dict[int, NodeClient] = {
            nid: NodeClient(nid, cfg["grpc"], cfg["raft"]) for nid, cfg in nodes.items()}

    def __getitem__(self, node_id: int) -> NodeClient:
        return self.nodes[node_id]

    def raft_addr(self, node_id: int) -> str:
        return self.nodes[node_id].raft_addr

    def wait_all_ready(self, timeout_s: float = 60.0) -> bool:
        return all(c.wait_ready(timeout_s) for c in self.nodes.values())

    def close(self):
        for c in self.nodes.values():
            c.close()

    def snapshot_regions(self) -> Dict[int, set]:
        """Reconstruct {region_id: {node ids}} from every node's status."""
        regions: Dict[int, set] = {}
        for nid, c in self.nodes.items():
            try:
                for rid in c.active_regions():
                    regions.setdefault(rid, set()).add(nid)
            except grpc.RpcError:
                pass
        return regions


def _json_str(s: str) -> str:
    import json
    return json.dumps(s)


def _rpc_err(e: grpc.RpcError) -> str:
    try:
        return f"{e.code().name}: {e.details()}"
    except Exception:  # pragma: no cover
        return str(e)

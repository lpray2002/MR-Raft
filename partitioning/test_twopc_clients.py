"""2PC client identity and CM failure safety, without starting a Go cluster."""
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import grpc

from src.cm.client import (
    Directive, DirectiveOutcome, FENCE_REGION, NodeClient, REMOVE_MEMBER,
    STOP_REGION,
)
from src.cm.cm import Clock, ClusterManager, Naming, RealExecutor, RoundRecord
from src.cm.planner import Plan, PlannedOp
from src.cm.runner import LocalCluster
from src.cm.uav_workload import LoadGenerator, UAVConfig, UAVFleet, UAVRecord
from src.simulation.grpc_bridge import MRRaftClient
from src.simulation.proto import raft_service_pb2 as pb


class LostReply(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.DEADLINE_EXCEEDED

    def details(self):
        return "decision acknowledgment lost"


def node_client():
    client = NodeClient.__new__(NodeClient)
    client.stub = Mock()
    client.stub.Propose.return_value = pb.ProposeResponse(success=True)
    return client


def directive_outcome(directive, success=False):
    return DirectiveOutcome(directive, success, "" if success else "unresolved transaction",
                            0, 0, 0, 0, 0, 0)


class ClientIdentityTests(unittest.TestCase):
    def test_lost_reply_can_retry_original_identity_and_zero_is_explicit(self):
        client = node_client()
        client.stub.Propose.side_effect = [LostReply(), pb.ProposeResponse(success=True),
                                           pb.ProposeResponse(success=True),
                                           pb.ProposeResponse(success=True)]
        unknown = client.propose("key", "value", client_id=12, region_id=4)
        self.assertFalse(unknown.success)
        retried = client.propose("key", "value", client_id=12, region_id=9,
                                 request_id=unknown.request_id)
        fresh = client.propose("key", "value", client_id=12)
        explicit_zero = client.propose("other", "value", client_id=12, request_id=0)
        requests = [call.args[0] for call in client.stub.Propose.call_args_list]
        self.assertTrue(retried.success)
        self.assertEqual(requests[0].data, requests[1].data)
        self.assertEqual(requests[0].request_id, requests[1].request_id)
        self.assertEqual(requests[0].client_id, requests[1].client_id)
        self.assertNotEqual(fresh.request_id, unknown.request_id)
        self.assertEqual(explicit_zero.request_id, 0)
        self.assertEqual(requests[3].request_id, 0)

    def test_concurrent_load_workers_and_restarted_generators_get_fresh_ids(self):
        client = node_client()
        local = threading.local()
        requests = []
        lock = threading.Lock()

        class WorkerClock:
            def now(self):
                return 2.0 if getattr(local, "writes", 0) >= 2 else 0.0

        def propose(request, **kwargs):
            with lock:
                requests.append(request)
            local.writes = getattr(local, "writes", 0) + 1
            return pb.ProposeResponse(success=True)

        client.stub.Propose.side_effect = propose
        # A new load generator starts its per-worker payload counter at zero.
        # Those fresh writes must not accidentally reuse a prior transaction ID.
        for _ in range(2):
            load = LoadGenerator({1: client}, WorkerClock(), nodes=[1])

            def worker(worker_id):
                local.writes = 0
                load._worker(1, worker_id, 1.0)

            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(worker, range(4)))
            self.assertEqual(load.ok, 8)
        self.assertEqual(len(requests), 16)
        self.assertEqual(len({r.request_id for r in requests}), 16)
        self.assertEqual({r.client_id for r in requests}, {1001})

    def test_uav_steps_use_distinct_transactions_with_monotonic_versions(self):
        client = node_client()
        fleet = UAVFleet.__new__(UAVFleet)
        fleet.cluster = {1: client}
        fleet.cfg = UAVConfig()
        fleet.mec_id = Naming.mec_id
        for step in (0, 1):
            rec = UAVRecord(0.5 * step, 2, step, 0, 0, 0, -1, False, 1,
                            "none", "", 0, False, 0)
            fleet._do_step(rec, "uav-2", step, 0, None)
            self.assertTrue(rec.write_ok)
        requests = [call.args[0] for call in client.stub.Propose.call_args_list]
        self.assertNotEqual(requests[0].request_id, requests[1].request_id)
        self.assertEqual({r.client_id for r in requests}, {3})
        self.assertEqual([json.loads(r.data)["Ver"] for r in requests], [1, 2])


class BridgeIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_timeout_and_disconnection_retain_retry_identity(self):
        client = MRRaftClient(mode="real")
        stub = SimpleNamespace(Propose=AsyncMock(side_effect=[
            TimeoutError("reply lost"), pb.ProposeResponse(success=True),
            pb.ProposeResponse(success=True), pb.ProposeResponse(success=True)]))
        client._stubs[4] = stub
        unknown = await client.propose(4, 7, b'{"Key":"k","Val":"v"}', client_id=8)
        self.assertFalse(unknown.success)
        retry = await client.propose(4, 9, b'{"Key":"k","Val":"v"}',
                                     client_id=8, request_id=unknown.request_id)
        fresh = await client.propose(4, 7, b'{"Key":"k","Val":"v"}', client_id=8)
        zero = await client.propose(4, 7, b'{"Key":"other","Val":"v"}',
                                    client_id=8, request_id=0)
        requests = [call.args[0] for call in stub.Propose.await_args_list]
        self.assertTrue(retry.success)
        self.assertEqual(requests[0].data, requests[1].data)
        self.assertEqual(requests[0].request_id, requests[1].request_id)
        self.assertEqual(requests[0].client_id, requests[1].client_id)
        self.assertNotEqual(fresh.request_id, unknown.request_id)
        self.assertEqual(zero.request_id, 0)
        disconnected = await client.propose(99, 7, request_id=unknown.request_id)
        self.assertFalse(disconnected.success)
        self.assertEqual(disconnected.request_id, unknown.request_id)


class ReconfigurationSafetyTests(unittest.TestCase):
    def test_failed_fence_never_removes_or_stops_its_participant(self):
        for rpc_failure in (False, True):
            with self.subTest(rpc_failure=rpc_failure):
                client = Mock()
                if rpc_failure:
                    client.apply_config.side_effect = LostReply()
                else:
                    client.apply_config.side_effect = lambda version, ds: [directive_outcome(ds[0])]
                executor = RealExecutor({1: client}, Naming([0], lambda n: "unused"), workers=1)
                try:
                    seq = [Directive(FENCE_REGION, 11, 0),
                           Directive(REMOVE_MEMBER, 11, 0), Directive(STOP_REGION, 11, 0)]
                    outcomes = executor.run_sequences(3, [seq])
                finally:
                    executor.pool.shutdown(wait=True)
                self.assertEqual(len(outcomes), 1)
                self.assertFalse(outcomes[0].success)
                self.assertEqual(client.apply_config.call_count, 1)
                self.assertEqual(client.apply_config.call_args.args[1][0].op, FENCE_REGION)

    def test_failed_phase_does_not_retire_or_drop_region_mapping(self):
        manager = ClusterManager.__new__(ClusterManager)
        manager.version = 2
        manager.clock = Clock(virtual=True)
        manager.cluster_of = {7: 101}
        manager.serving = {101: {0}}
        manager.snapshots = []
        manager.mgr = SimpleNamespace(regions={})
        failure = directive_outcome(Directive(FENCE_REGION, 101, 0))
        manager.executor = SimpleNamespace(last_elapsed_ms=12,
                                           run_sequences=Mock(return_value=[failure]))
        manager._materialize = Mock(return_value=[[failure.directive]])
        manager._apply_outcomes = Mock()
        manager._emit = Mock()
        plan = Plan([], {"shrink": [PlannedOp("FENCE", 7, 0)],
                         "retire": [PlannedOp("STOP", 7, 0)]})
        rec = RoundRecord(1, 0, 1, [], [], 0, True)
        with self.assertRaisesRegex(RuntimeError, "phase shrink failed"):
            manager._execute_plan(plan, rec)
        self.assertFalse(rec.constraints_ok)
        self.assertEqual(manager.cluster_of, {7: 101})
        self.assertEqual(manager.executor.run_sequences.call_count, 1)
        self.assertEqual([c.args[0] for c in manager._materialize.call_args_list], ["shrink"])
        self.assertEqual(manager._emit.call_args.args[0]["type"], "reconfiguration_failed")


class LocalClusterPersistenceTests(unittest.TestCase):
    def args(self, reset_data=False):
        return SimpleNamespace(reset_data=reset_data, disk_sm=False, tick_ms=50,
                               election_rtt=3, heartbeat_rtt=1, propose_timeout_ms=2000,
                               gomaxprocs=2, delay_us=0)

    def test_default_start_uses_disk_wal_even_with_inherited_memfs_flag(self):
        with tempfile.TemporaryDirectory() as data_dir:
            cluster = LocalCluster("unused-go-binary", data_dir, [0], self.args())
            proc = Mock()
            proc.poll.return_value = None
            with patch.object(cluster, "_check_ports_free"), \
                    patch("src.cm.runner.subprocess.Popen", return_value=proc) as popen, \
                    patch("src.cm.runner.time.sleep"), patch("atexit.register"), \
                    patch.dict(os.environ, {"RAFT_MEMFS": "true"}):
                try:
                    cluster.start()
                    env = popen.call_args.kwargs["env"]
                    self.assertEqual(env["RAFT_MEMFS"], "false")
                    self.assertEqual(env["MEC_NODE_DATA_DIR"], os.path.join(data_dir, "raft"))
                finally:
                    cluster.stop()

    def test_previous_journal_is_preserved_unless_reset_is_explicit(self):
        with tempfile.TemporaryDirectory() as data_dir:
            journal = Path(data_dir, "coordinator.json")
            journal.write_text("unresolved decision")
            cluster = LocalCluster("unused-go-binary", data_dir, [0], self.args())
            with patch.object(cluster, "_check_ports_free"), \
                    patch("src.cm.runner.subprocess.Popen") as popen, \
                    patch("src.cm.runner.shutil.rmtree") as remove:
                with self.assertRaisesRegex(RuntimeError, "retain it for 2PC recovery"):
                    cluster.start()
            self.assertEqual(journal.read_text(), "unresolved decision")
            remove.assert_not_called()
            popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()

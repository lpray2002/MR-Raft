package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"gitcode.com/lpray/multi-region-raft/pkg/mec"
	"gitcode.com/lpray/multi-region-raft/pkg/twopc"
	pb "gitcode.com/lpray/multi-region-raft/proto/raftservice"
	"github.com/lni/dragonboat/v3"
	"github.com/lni/dragonboat/v3/config"
)

const txnTestRegionA uint64 = 11
const txnTestRegionB uint64 = 22

func TestCMRejectsVolatileRaftStorage(t *testing.T) {
	t.Setenv("RAFT_MEMFS", "true")
	n := &MECNodeCM{}
	if err := n.createNodeHost(); err == nil || n.NodeHost != nil {
		t.Fatal("volatile storage must be rejected before a Raft host starts")
	}
	if err := n.initTransactions(); err == nil || n.txns != nil {
		t.Fatal("volatile storage must not start a transaction coordinator")
	}
}

// The participant state machines are in memory, but their Raft WAL and LogDB
// use the real filesystem. Recreating the NodeHost must therefore replay the
// durable log, rather than retaining a state-machine instance in the test.
func newTxnTestNode(t *testing.T, dataDir, address string, restart bool) *MECNodeCM {
	t.Helper()
	t.Setenv("RAFT_MEMFS", "false")
	n := &MECNodeCM{
		NodeID: 1, NodeName: "txn-integration", NodeAddress: address,
		DataDir: dataDir, UseMemorySM: true,
		TickMs: 20, ElectionRTT: 20, HeartbeatRTT: 1, CheckQuorum: true,
		SnapshotEntries: 10000, CompactionOverhead: 5000,
		ProposeTimeout: 8 * time.Second, DrainTimeout: 200 * time.Millisecond,
		TxnRecoveryInterval: 100 * time.Millisecond,
		regions:             make(map[uint64]*regionEntry), stats: &proposeStats{start: time.Now()},
	}
	openTxnTestNodeHost(t, n)
	for _, region := range []uint64{txnTestRegionA, txnTestRegionB} {
		var members map[uint64]string
		if !restart {
			members = map[uint64]string{region*100 + 1: address}
		}
		if _, err := n.startRegion(region, region*100+1, members, false, false, true, 10*time.Second); err != nil {
			t.Fatalf("start region %d: %v", region, err)
		}
		txnTestWaitReady(t, n, region)
	}
	return n
}

func openTxnTestNodeHost(t *testing.T, n *MECNodeCM) {
	t.Helper()
	base := filepath.Join(n.DataDir, n.NodeName)
	logDB := config.GetTinyMemLogDBConfig()
	logDB.Shards = 1
	nh, err := dragonboat.NewNodeHost(config.NodeHostConfig{
		WALDir: filepath.Join(base, "wal"), NodeHostDir: filepath.Join(base, "nh"),
		RTTMillisecond: n.TickMs, RaftAddress: n.NodeAddress, RaftEventListener: n,
		Expert: config.ExpertConfig{
			LogDB:  logDB,
			Engine: config.EngineConfig{ExecShards: 1, CommitShards: 1, ApplyShards: 1, SnapshotShards: 1, CloseShards: 1},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	n.NodeHost = nh
	t.Cleanup(func() { closeTxnTestNode(t, n) })
}

func txnTestWaitReady(t *testing.T, n *MECNodeCM, region uint64) {
	t.Helper()
	// Leader election alone does not establish that log replay is finished.
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	var err error
	for {
		attemptCtx, attemptCancel := context.WithTimeout(ctx, 250*time.Millisecond)
		_, err = n.NodeHost.SyncRead(attemptCtx, region, &mec.TxnQuery{ID: "readiness-probe"})
		attemptCancel()
		if err == nil || ctx.Err() != nil {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if err != nil {
		t.Fatalf("ready region %d: %v", region, err)
	}
}

func closeTxnTestNode(t *testing.T, n *MECNodeCM) {
	t.Helper()
	if n.NodeHost == nil {
		return
	}
	if err := n.closeTransactions(); err != nil {
		t.Errorf("close coordinator: %v", err)
	}
	n.NodeHost.Stop()
	n.NodeHost = nil
}

func txnTestAddress(t *testing.T) string {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	address := l.Addr().String()
	if err := l.Close(); err != nil {
		t.Fatal(err)
	}
	return address
}

func txnTestCommand(t *testing.T, n *MECNodeCM, region uint64, command mec.TxnCommand) mec.TxnReply {
	t.Helper()
	data, err := json.Marshal(command)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	result, err := n.NodeHost.SyncPropose(ctx, n.NodeHost.GetNoOPSession(region), data)
	if err != nil {
		t.Fatalf("%s region %d: %v", command.Op, region, err)
	}
	reply, err := mec.DecodeTxnReply(result)
	if err != nil {
		t.Fatal(err)
	}
	return reply
}

func txnTestRecord(t *testing.T, n *MECNodeCM, region uint64, id string) *mec.TxnRecord {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	result, err := n.NodeHost.SyncRead(ctx, region, &mec.TxnQuery{ID: id})
	if err != nil {
		t.Fatal(err)
	}
	record, ok := result.(*mec.TxnRecord)
	if !ok {
		t.Fatalf("transaction query returned %T", result)
	}
	return record
}

func txnTestPropose(t *testing.T, n *MECNodeCM, region uint64, client, request int64, kv mec.KVData) (*pb.ProposeResponse, error) {
	t.Helper()
	data, err := json.Marshal(kv)
	if err != nil {
		t.Fatal(err)
	}
	return n.Propose(context.Background(), &pb.ProposeRequest{RegionId: region, ClientId: client, RequestId: request, Data: data})
}

func txnTestAssertValue(t *testing.T, n *MECNodeCM, region uint64, key, value string) {
	t.Helper()
	resp, err := n.SyncRead(context.Background(), &pb.SyncReadRequest{RegionId: region, Key: key})
	if err != nil || resp.Error != "" || !resp.Found || resp.Value != value {
		t.Fatalf("region %d read %q: response=%+v err=%v, want %q", region, key, resp, err, value)
	}
}

func txnTestAssertReadBlocked(t *testing.T, n *MECNodeCM, region uint64, key string) {
	t.Helper()
	for _, stale := range []bool{false, true} {
		resp, err := n.SyncRead(context.Background(), &pb.SyncReadRequest{RegionId: region, Key: key, Stale: stale})
		if err == nil && resp.Error == "" {
			t.Fatalf("pending transaction allowed read: region=%d stale=%v response=%+v", region, stale, resp)
		}
		if resp != nil && (resp.Found || len(resp.RegionValues) != 0) {
			t.Fatalf("blocked read exposed partial values: %+v", resp)
		}
	}
}

func TestCMTransactionCommitRetryAndWALRestart(t *testing.T) {
	dataDir, address := t.TempDir(), txnTestAddress(t)
	n := newTxnTestNode(t, dataDir, address, false)
	if err := n.initTransactions(); err != nil {
		t.Fatal(err)
	}
	kv := mec.KVData{Key: "shared", Val: "committed", Ver: 7}
	if resp, err := txnTestPropose(t, n, 0, 17, 31, kv); err != nil || !resp.Success {
		t.Fatalf("commit response=%+v err=%v", resp, err)
	}
	id := "node:1/client:17/request:31"
	for _, region := range []uint64{txnTestRegionA, txnTestRegionB} {
		if got := txnTestRecord(t, n, region, id); got.State != mec.TxnCommitted {
			t.Fatalf("region %d state=%s", region, got.State)
		}
		txnTestAssertValue(t, n, region, kv.Key, kv.Val)
	}
	// Same request identity is idempotent despite an already-applied version.
	if resp, err := txnTestPropose(t, n, 0, 17, 31, kv); err != nil || !resp.Success {
		t.Fatalf("retry response=%+v err=%v", resp, err)
	}
	if resp, err := txnTestPropose(t, n, 0, 17, 31, mec.KVData{Key: kv.Key, Val: "different", Ver: 8}); err == nil && resp.Success {
		t.Fatal("request identity accepted a different write")
	}
	closeTxnTestNode(t, n)
	// Prove that the test used persistent files before replaying both regions.
	files := 0
	if err := filepath.Walk(filepath.Join(dataDir, "txn-integration", "wal"), func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if !info.IsDir() {
			files++
		}
		return nil
	}); err != nil || files == 0 {
		t.Fatalf("disk WAL files=%d err=%v", files, err)
	}
	n = newTxnTestNode(t, dataDir, address, true)
	if err := n.initTransactions(); err != nil {
		t.Fatal(err)
	}
	for _, region := range []uint64{txnTestRegionA, txnTestRegionB} {
		if got := txnTestRecord(t, n, region, id); got.State != mec.TxnCommitted {
			t.Fatalf("WAL replay region %d state=%s", region, got.State)
		}
		txnTestAssertValue(t, n, region, kv.Key, kv.Val)
	}
	if resp, err := txnTestPropose(t, n, 0, 17, 31, kv); err != nil || !resp.Success {
		t.Fatalf("restart retry response=%+v err=%v", resp, err)
	}
}

func TestCMPreparedParticipantGuardsReadsWritesAndRetirement(t *testing.T) {
	dataDir, address := t.TempDir(), txnTestAddress(t)
	n := newTxnTestNode(t, dataDir, address, false)
	if err := n.initTransactions(); err != nil {
		t.Fatal(err)
	}
	if resp, err := txnTestPropose(t, n, 0, 17, 32, mec.KVData{Key: "locked", Val: "old", Ver: 1}); err != nil || !resp.Success {
		t.Fatalf("seed response=%+v err=%v", resp, err)
	}
	txn := mec.Transaction{ID: "other-coordinator/pending", Coordinator: "other-coordinator", Participants: []uint64{txnTestRegionA, txnTestRegionB}, Write: mec.KVData{Key: "locked", Val: "new", Ver: 2}}
	if reply := txnTestCommand(t, n, txnTestRegionA, mec.TxnCommand{Op: mec.OpPrepare, Transaction: txn}); reply.State != mec.TxnPrepared {
		t.Fatalf("prepare: %+v", reply)
	}
	closeTxnTestNode(t, n)
	n = newTxnTestNode(t, dataDir, address, true)
	n.ProposeTimeout = 300 * time.Millisecond
	if err := n.initTransactions(); err != nil {
		t.Fatal(err)
	}
	if got := txnTestRecord(t, n, txnTestRegionA, txn.ID); got.State != mec.TxnPrepared {
		t.Fatalf("prepared record lost during WAL replay: %+v", got)
	}
	txnTestAssertReadBlocked(t, n, txnTestRegionA, txn.Write.Key)
	txnTestAssertReadBlocked(t, n, 0, txn.Write.Key)
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	if _, err := n.NodeHost.SyncRead(ctx, txnTestRegionA, &mec.ScanQuery{}); !errors.Is(err, mec.ErrTxnPending) {
		t.Fatalf("prepared scan err=%v", err)
	}
	legacy, err := json.Marshal(mec.KVData{Key: "locked", Val: "bypass", Ver: 3})
	if err != nil {
		t.Fatal(err)
	}
	result, err := n.NodeHost.SyncPropose(ctx, n.NodeHost.GetNoOPSession(txnTestRegionA), legacy)
	if err != nil {
		t.Fatal(err)
	}
	if reply, err := mec.DecodeTxnReply(result); err != nil || reply.State != mec.TxnRejected {
		t.Fatalf("legacy lock bypass reply=%+v err=%v", reply, err)
	}
	if resp, err := txnTestPropose(t, n, txnTestRegionA, 17, 33, mec.KVData{Key: "locked", Val: "single-region-bypass", Ver: 3}); err == nil && resp.Success {
		t.Fatal("single-region request bypassed a prepared key lock")
	}
	if resp, err := n.ScanRegion(ctx, &pb.ScanRequest{RegionId: txnTestRegionA}); err == nil && resp.Success {
		t.Fatal("ScanRegion exposed state with a prepared transaction")
	}
	if _, err := n.migrateState(ctx, txnTestRegionA, txnTestRegionB); err == nil {
		t.Fatal("migration scanned a prepared source")
	}
	if _, err := n.migrateState(ctx, txnTestRegionB, txnTestRegionA); err == nil {
		t.Fatal("migration reported success writing to a locked destination")
	}
	if err := n.stopRegion(txnTestRegionA); err == nil {
		t.Fatal("STOP_REGION retired a prepared participant")
	}
	if _, ok := n.getRegion(txnTestRegionA); !ok {
		t.Fatal("failed retirement removed the participant recovery target")
	}
	if got := txnTestRecord(t, n, txnTestRegionA, txn.ID); got.State != mec.TxnPrepared {
		t.Fatalf("prepared state changed after rejected operations: %+v", got)
	}
	if reply := txnTestCommand(t, n, txnTestRegionA, mec.TxnCommand{Op: mec.OpAbort, Transaction: txn}); reply.State != mec.TxnAborted {
		t.Fatalf("abort fenced participant: %+v", reply)
	}
	// Recovery must still reach a fenced participant; it need not serve users.
	if err := n.stopRegion(txnTestRegionA); err != nil {
		t.Fatalf("stop resolved participant: %v", err)
	}
}

func txnTestID(client, request int64) string {
	return fmt.Sprintf("node:1/client:%d/request:%d", client, request)
}

// This wrapper only selects the fault boundary. Every successful participant
// acknowledgement comes from an actual Raft proposal. The second participant
// is stopped without deleting its WAL, as an unavailable region would be.
type txnFaultTransport struct {
	node                              *MECNodeCM
	id, mode                          string
	preparedA, committedA             chan struct{}
	prepareOnce, commitOnce, stopOnce sync.Once
}

func (f *txnFaultTransport) Propose(ctx context.Context, region uint64, command mec.TxnCommand) (mec.TxnReply, error) {
	base := raftTxnTransport{node: f.node}
	if command.Transaction.ID != f.id {
		return base.Propose(ctx, region, command)
	}
	if region == txnTestRegionB && ((f.mode == "partial-prepare" && command.Op == mec.OpPrepare) || (f.mode != "partial-prepare" && command.Op == mec.OpCommit)) {
		boundary := f.committedA
		if command.Op == mec.OpPrepare {
			boundary = f.preparedA
		}
		select {
		case <-boundary:
		case <-ctx.Done():
			return mec.TxnReply{}, ctx.Err()
		}
		var stopErr error
		f.stopOnce.Do(func() { stopErr = f.node.NodeHost.StopCluster(region) })
		if stopErr != nil {
			return mec.TxnReply{}, stopErr
		}
		if f.mode == "client-timeout" {
			<-ctx.Done()
			return mec.TxnReply{}, ctx.Err()
		}
	}
	reply, err := base.Propose(ctx, region, command)
	if err == nil && region == txnTestRegionA {
		if command.Op == mec.OpPrepare && reply.State == mec.TxnPrepared {
			f.prepareOnce.Do(func() { close(f.preparedA) })
		}
		if command.Op == mec.OpCommit && reply.State == mec.TxnCommitted {
			f.commitOnce.Do(func() { close(f.committedA) })
		}
	}
	return reply, err
}

func txnTestCoordinatorRecord(t *testing.T, n *MECNodeCM, id string) twopc.Record {
	t.Helper()
	for _, record := range n.txns.Records() {
		if record.Transaction.ID == id {
			return record
		}
	}
	t.Fatalf("coordinator record %q missing", id)
	return twopc.Record{}
}

func txnTestAssertRawValue(t *testing.T, n *MECNodeCM, region uint64, key, value string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	result, err := n.NodeHost.SyncRead(ctx, region, &mec.KeyQuery{Key: key})
	if err != nil {
		t.Fatal(err)
	}
	kv, ok := result.(*mec.KVResult)
	if !ok || !kv.Found || kv.Val != value {
		t.Fatalf("raw region %d value=%+v, want %q", region, result, value)
	}
}

func TestCMTransactionConsensusFailuresRecoverFromJournalAndWAL(t *testing.T) {
	for _, mode := range []string{"partial-prepare", "partial-commit", "client-timeout"} {
		t.Run(mode, func(t *testing.T) {
			dataDir, address := t.TempDir(), txnTestAddress(t)
			n := newTxnTestNode(t, dataDir, address, false)
			id := txnTestID(19, 42)
			fault := &txnFaultTransport{node: n, id: id, mode: mode, preparedA: make(chan struct{}), committedA: make(chan struct{})}
			engine, err := twopc.Open(filepath.Join(dataDir, n.NodeName, "transactions"), "node:1", fault)
			if err != nil {
				t.Fatal(err)
			}
			n.txns = engine
			if resp, err := txnTestPropose(t, n, 0, 19, 41, mec.KVData{Key: "fault-key", Val: "old", Ver: 1}); err != nil || !resp.Success {
				t.Fatalf("seed response=%+v err=%v", resp, err)
			}
			write := mec.KVData{Key: "fault-key", Val: "new", Ver: 2}
			payload, err := json.Marshal(write)
			if err != nil {
				t.Fatal(err)
			}
			ctx := context.Background()
			cancel := func() {}
			if mode == "client-timeout" {
				ctx, cancel = context.WithTimeout(ctx, 2*time.Second)
			}
			resp, err := n.Propose(ctx, &pb.ProposeRequest{ClientId: 19, RequestId: 42, Data: payload})
			cancel()
			if err == nil && resp.Success {
				t.Fatalf("fault acknowledged success: %+v", resp)
			}
			decision := mec.TxnCommitted
			if mode == "partial-prepare" {
				decision = mec.TxnAborted
			}
			record := txnTestCoordinatorRecord(t, n, id)
			if record.State != decision || record.Complete {
				t.Fatalf("fault journal=%+v, want incomplete %s", record, decision)
			}
			if got := txnTestRecord(t, n, txnTestRegionA, id); got.State != decision {
				t.Fatalf("first participant state=%s, want %s", got.State, decision)
			}
			visible := "new"
			if decision == mec.TxnAborted {
				visible = "old"
			}
			txnTestAssertRawValue(t, n, txnTestRegionA, write.Key, visible)
			n.ProposeTimeout = 250 * time.Millisecond
			// 2PC does not provide simultaneous physical application. A region
			// that applied the durable decision may serve it, while a combined
			// read must fail closed if another participant cannot be read.
			txnTestAssertValue(t, n, txnTestRegionA, write.Key, visible)
			txnTestAssertReadBlocked(t, n, txnTestRegionB, write.Key)
			txnTestAssertReadBlocked(t, n, 0, write.Key)
			if err := n.stopRegion(txnTestRegionA); err == nil {
				t.Fatal("retired region while coordinator decision delivery was incomplete")
			}
			if _, ok := n.getRegion(txnTestRegionA); !ok {
				t.Fatal("retirement removed unresolved coordinator participant")
			}
			closeTxnTestNode(t, n)
			n = newTxnTestNode(t, dataDir, address, true)
			if got := txnTestRecord(t, n, txnTestRegionA, id); got.State != decision {
				t.Fatalf("first participant WAL state=%s", got.State)
			}
			if decision == mec.TxnCommitted {
				if got := txnTestRecord(t, n, txnTestRegionB, id); got.State != mec.TxnPrepared {
					t.Fatalf("second participant lost prepared WAL record: %+v", got)
				}
				ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
				_, err := n.NodeHost.SyncRead(ctx, txnTestRegionB, &mec.KeyQuery{Key: write.Key})
				cancel()
				if !errors.Is(err, mec.ErrTxnPending) {
					t.Fatalf("prepared WAL replay allowed read: %v", err)
				}
				n.ProposeTimeout = 250 * time.Millisecond
				txnTestAssertReadBlocked(t, n, txnTestRegionB, write.Key)
				txnTestAssertReadBlocked(t, n, 0, write.Key)
				n.ProposeTimeout = 8 * time.Second
			}
			// User admission stays fenced while the real production recovery
			// transport is allowed to deliver the saved final decision.
			e, ok := n.getRegion(txnTestRegionB)
			if !ok {
				t.Fatal("restarted participant missing")
			}
			atomic.StoreInt32(&e.fenced, 1)
			if err := n.initTransactions(); err != nil {
				t.Fatal(err)
			}
			deadline := time.Now().Add(8 * time.Second)
			for {
				record = txnTestCoordinatorRecord(t, n, id)
				if record.Complete || time.Now().After(deadline) {
					break
				}
				time.Sleep(20 * time.Millisecond)
			}
			if record.State != decision || !record.Complete {
				t.Fatalf("recovered journal=%+v", record)
			}
			for _, region := range []uint64{txnTestRegionA, txnTestRegionB} {
				if got := txnTestRecord(t, n, region, id); got.State != decision {
					t.Fatalf("recovered participant %d state=%s", region, got.State)
				}
				txnTestAssertRawValue(t, n, region, write.Key, visible)
			}
			// Retrying with a narrower current target must reuse the journal's
			// original two participants, including the fenced recovery target.
			resp, err = txnTestPropose(t, n, txnTestRegionA, 19, 42, write)
			if decision == mec.TxnCommitted && (err != nil || !resp.Success) {
				t.Fatalf("commit retry response=%+v err=%v", resp, err)
			}
			if decision == mec.TxnAborted && err == nil && resp.Success {
				t.Fatal("aborted request identity was reused for commit")
			}
			atomic.StoreInt32(&e.fenced, 0)
			txnTestAssertValue(t, n, 0, write.Key, visible)
		})
	}
}

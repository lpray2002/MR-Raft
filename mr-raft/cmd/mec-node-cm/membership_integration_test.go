package main

import (
	"context"
	"errors"
	"fmt"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"gitcode.com/lpray/multi-region-raft/pkg/mec"
	"gitcode.com/lpray/multi-region-raft/pkg/twopc"
	pb "gitcode.com/lpray/multi-region-raft/proto/raftservice"
	"github.com/lni/dragonboat/v3"
)

func newTxnTestThreeMembers(t *testing.T) []*MECNodeCM {
	t.Helper()
	t.Setenv("RAFT_MEMFS", "false")
	root := t.TempDir()
	members := make(map[uint64]string)
	nodes := make([]*MECNodeCM, 3)
	for i := range nodes {
		id := i + 1
		address := txnTestAddress(t)
		members[txnTestRegionA*100+uint64(id)] = address
		nodes[i] = &MECNodeCM{
			NodeID: id, NodeName: fmt.Sprintf("member-%d", id), NodeAddress: address,
			DataDir: root, UseMemorySM: true,
			TickMs: 20, ElectionRTT: 20, HeartbeatRTT: 1, CheckQuorum: true,
			SnapshotEntries: 10000, CompactionOverhead: 5000,
			ProposeTimeout: 8 * time.Second, DrainTimeout: 3 * time.Second,
			TxnRecoveryInterval: 100 * time.Millisecond,
			regions:             make(map[uint64]*regionEntry), stats: &proposeStats{start: time.Now()},
		}
		openTxnTestNodeHost(t, nodes[i])
	}
	for _, n := range nodes {
		if _, err := n.startRegion(txnTestRegionA, txnTestRegionA*100+uint64(n.NodeID), members, false, false, false, 0); err != nil {
			t.Fatal(err)
		}
	}
	for _, n := range nodes {
		txnTestWaitReady(t, n, txnTestRegionA)
		if err := n.initTransactions(); err != nil {
			t.Fatal(err)
		}
	}
	return nodes
}

func txnTestWaitSelfRemoved(t *testing.T, n *MECNodeCM, e *regionEntry) {
	t.Helper()
	deadline := time.Now().Add(8 * time.Second)
	for {
		m := e.appliedMembership.AppliedMembership()
		_, removed := m.Removed[e.raftNodeID]
		_, _, err := n.NodeHost.GetLeaderID(e.regionID)
		if removed && errors.Is(err, dragonboat.ErrClusterNotFound) {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("self-removal did not unload replica: membership=%+v err=%v", m, err)
		}
		time.Sleep(10 * time.Millisecond)
	}
}

type txnCommitBarrier struct {
	node           *MECNodeCM
	id             string
	ready, release chan struct{}
	once           sync.Once
}

func (b *txnCommitBarrier) Propose(ctx context.Context, region uint64, command mec.TxnCommand) (mec.TxnReply, error) {
	if command.Transaction.ID == b.id && command.Op == mec.OpCommit {
		b.once.Do(func() { close(b.ready) })
		select {
		case <-b.release:
		case <-ctx.Done():
			return mec.TxnReply{}, ctx.Err()
		}
	}
	return (raftTxnTransport{node: b.node}).Propose(ctx, region, command)
}

func TestCMRemovedReplicaCanStopOnlyAfterVerifiedFence(t *testing.T) {
	for _, remoteRace := range []bool{false, true} {
		name := "normal-remove-stop"
		if remoteRace {
			name = "remote-prepare-after-removal-preflight"
		}
		t.Run(name, func(t *testing.T) {
			nodes := newTxnTestThreeMembers(t)
			leaving, staying, other := nodes[0], nodes[1], nodes[2]
			if resp, err := txnTestPropose(t, leaving, txnTestRegionA, 41, 1, mec.KVData{Key: "membership-key", Val: "old", Ver: 1}); err != nil || !resp.Success {
				t.Fatalf("seed response=%+v err=%v", resp, err)
			}
			if err := leaving.fenceRegion(txnTestRegionA); err != nil {
				t.Fatal(err)
			}
			e, ok := leaving.getRegion(txnTestRegionA)
			if !ok || e.appliedMembership == nil || atomic.LoadInt32(&e.fenceVerified) != 1 {
				t.Fatal("fence did not retain verifiable removal evidence")
			}
			ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
			defer cancel()
			var barrier *txnCommitBarrier
			var outcome chan struct {
				response *pb.ProposeResponse
				err      error
			}
			if !remoteRace {
				if _, err := staying.removeMember(ctx, txnTestRegionA, e.raftNodeID); err != nil {
					t.Fatal(err)
				}
			} else {
				// Model the unavoidable interval between a successful preflight
				// query and the membership proposal: another coordinator prepares
				// through the surviving Raft member after the leaving node fenced.
				if err := staying.ensureRegionResolved(ctx, txnTestRegionA); err != nil {
					t.Fatal(err)
				}
				if err := staying.closeTransactions(); err != nil {
					t.Fatal(err)
				}
				id := "node:2/client:42/request:2"
				barrier = &txnCommitBarrier{node: staying, id: id, ready: make(chan struct{}), release: make(chan struct{})}
				engine, err := twopc.Open(filepath.Join(staying.DataDir, staying.NodeName, "transactions"), "node:2", barrier)
				if err != nil {
					t.Fatal(err)
				}
				staying.txns = engine
				outcome = make(chan struct {
					response *pb.ProposeResponse
					err      error
				}, 1)
				go func() {
					resp, err := txnTestPropose(t, staying, txnTestRegionA, 42, 2, mec.KVData{Key: "membership-key", Val: "new", Ver: 2})
					outcome <- struct {
						response *pb.ProposeResponse
						err      error
					}{resp, err}
				}()
				select {
				case <-barrier.ready:
				case <-ctx.Done():
					t.Fatal("remote transaction did not reach durable commit boundary")
				}
				if got := txnTestRecord(t, leaving, txnTestRegionA, id); got.State != mec.TxnPrepared {
					t.Fatalf("remote prepare did not replicate to fenced replica: %+v", got)
				}
				// The preflight already finished above. Issue its actual Raft
				// removal proposal without repeating that query, as in the race.
				if err := staying.NodeHost.SyncRequestDeleteNode(ctx, txnTestRegionA, e.raftNodeID, 0); err != nil {
					t.Fatal(err)
				}
			}
			txnTestWaitSelfRemoved(t, leaving, e)
			if leaving.txns.PendingForRegion(txnTestRegionA) {
				t.Fatal("leaving coordinator has unresolved decisions")
			}
			if !replicaRemovalProven(e) {
				t.Fatal("applied removal did not prove safe old-replica retirement")
			}
			// The accessor is a deep copy, including the irreversible Removed
			// set; caller mutations cannot manufacture or erase removal proof.
			copy := e.appliedMembership.AppliedMembership()
			delete(copy.Removed, e.raftNodeID)
			copy.Nodes[e.raftNodeID] = "forged"
			if !replicaRemovalProven(e) {
				t.Fatal("caller mutation changed applied membership")
			}
			// Missing process-local fence evidence (as after a restart) must
			// fail closed even when this replica is genuinely removed.
			atomic.StoreInt32(&e.fenceVerified, 0)
			if err := leaving.stopRegion(txnTestRegionA); err == nil {
				t.Fatal("removed replica stopped without prior verified fence")
			}
			if _, ok := leaving.getRegion(txnTestRegionA); !ok {
				t.Fatal("failed stop discarded the region entry")
			}
			atomic.StoreInt32(&e.fenceVerified, 1)
			if err := leaving.stopRegion(txnTestRegionA); err != nil {
				t.Fatalf("REMOVE_MEMBER then STOP_REGION: %v", err)
			}
			if _, ok := leaving.getRegion(txnTestRegionA); ok {
				t.Fatal("resolved removed replica remained registered")
			}
			if remoteRace {
				for _, n := range []*MECNodeCM{staying, other} {
					readCtx, readCancel := context.WithTimeout(context.Background(), 150*time.Millisecond)
					resp, err := n.SyncRead(readCtx, &pb.SyncReadRequest{RegionId: txnTestRegionA, Key: "membership-key"})
					readCancel()
					if err == nil && (resp.Error == "" || resp.Found || len(resp.RegionValues) > 0) {
						t.Fatalf("remaining prepared participant exposed data: %+v", resp)
					}
				}
				// The removed replica can have been the leader. Wait for a new
				// ReadIndex barrier before releasing COMMIT; a proposal sent in
				// the leaderless interval can legitimately return uncertain.
				for _, n := range []*MECNodeCM{staying, other} {
					txnTestWaitReady(t, n, txnTestRegionA)
				}
				close(barrier.release)
				select {
				case got := <-outcome:
					if got.err != nil || !got.response.Success {
						t.Fatalf("remote commit after retirement response=%+v err=%v", got.response, got.err)
					}
				case <-ctx.Done():
					t.Fatal("remaining quorum did not finish remote commit")
				}
			}
			value := "old"
			if remoteRace {
				value = "new"
			}
			for _, n := range []*MECNodeCM{staying, other} {
				txnTestAssertValue(t, n, txnTestRegionA, "membership-key", value)
			}
			// A plain unavailable replica has no committed removal proof and
			// must not receive the same exception, even with no local decisions.
			if err := other.NodeHost.StopCluster(txnTestRegionA); err != nil {
				t.Fatal(err)
			}
			if err := other.stopRegion(txnTestRegionA); err == nil {
				t.Fatal("ordinary stopped replica was mistaken for an applied membership removal")
			}
			if _, ok := other.getRegion(txnTestRegionA); !ok {
				t.Fatal("unverified stop discarded the region entry")
			}
		})
	}
}

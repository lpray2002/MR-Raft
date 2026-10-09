package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"path/filepath"
	"sort"
	"strings"
	"sync/atomic"
	"time"

	"gitcode.com/lpray/multi-region-raft/pkg/mec"
	"gitcode.com/lpray/multi-region-raft/pkg/twopc"
	pb "gitcode.com/lpray/multi-region-raft/proto/raftservice"
	"github.com/lni/dragonboat/v3"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func (n *MECNodeCM) requestTimeout() time.Duration {
	if n.ProposeTimeout > 0 {
		return n.ProposeTimeout
	}
	return 5 * time.Second
}

func (n *MECNodeCM) drainTimeout() time.Duration {
	if n.DrainTimeout > 0 {
		return n.DrainTimeout
	}
	return 500 * time.Millisecond
}

func (n *MECNodeCM) initTransactions() error {
	if getEnv("RAFT_MEMFS", "false") == "true" {
		return errors.New("2PC requires persistent participant Raft logs; RAFT_MEMFS must be false")
	}
	engine, err := twopc.Open(filepath.Join(n.DataDir, n.NodeName, "transactions"),
		fmt.Sprintf("node:%d", n.NodeID), raftTxnTransport{node: n})
	if err != nil {
		return err
	}
	n.txns = engine
	ctx, cancel := context.WithCancel(context.Background())
	n.txnCancel = cancel
	interval := n.TxnRecoveryInterval
	if interval <= 0 {
		interval = 500 * time.Millisecond
	}
	n.txnWorkers.Add(1)
	go func() {
		defer n.txnWorkers.Done()
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		for {
			// Existing regions may not have been restarted by the CM yet.
			// Keep the journal and retry without changing a final decision.
			rctx, rcancel := context.WithTimeout(ctx, n.requestTimeout())
			_ = engine.Recover(rctx)
			rcancel()
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
			}
		}
	}()
	return nil
}

func (n *MECNodeCM) closeTransactions() error {
	if n.txnCancel != nil {
		n.txnCancel()
	}
	n.txnWorkers.Wait()
	if n.txns != nil {
		return n.txns.Close()
	}
	return nil
}

// Admission increments inflight while holding the same table lock used by
// fencing/removal. A request cannot slip in after the drain has observed zero.
func (n *MECNodeCM) acquireServingRegions(rid uint64) ([]*regionEntry, error) {
	n.mu.RLock()
	defer n.mu.RUnlock()
	var entries []*regionEntry
	if rid != 0 {
		e, ok := n.regions[rid]
		if !ok || atomic.LoadInt32(&e.fenced) != 0 {
			return nil, fmt.Errorf("region %d not served on node %d", rid, n.NodeID)
		}
		entries = append(entries, e)
	} else {
		for _, e := range n.regions {
			if atomic.LoadInt32(&e.fenced) == 0 {
				entries = append(entries, e)
			}
		}
	}
	if len(entries) == 0 {
		return nil, fmt.Errorf("node %d hosts no serving region", n.NodeID)
	}
	sort.Slice(entries, func(i, j int) bool { return entries[i].regionID < entries[j].regionID })
	for _, e := range entries {
		atomic.AddInt64(&e.inflight, 1)
	}
	return entries, nil
}

// Decision delivery and retries use the original footprint even after the
// service entry has been fenced. A fenced Raft instance must stay available
// until its pending transactions have been resolved.
func (n *MECNodeCM) acquireRegionIDs(ids []uint64, allowFenced bool) ([]*regionEntry, error) {
	n.mu.RLock()
	defer n.mu.RUnlock()
	seen := make(map[uint64]bool)
	entries := make([]*regionEntry, 0, len(ids))
	for _, id := range ids {
		if seen[id] {
			continue
		}
		seen[id] = true
		e, ok := n.regions[id]
		if !ok || (!allowFenced && atomic.LoadInt32(&e.fenced) != 0) {
			return nil, fmt.Errorf("region %d unavailable on coordinator node %d", id, n.NodeID)
		}
		entries = append(entries, e)
	}
	for _, e := range entries {
		atomic.AddInt64(&e.inflight, 1)
	}
	return entries, nil
}

func releaseRegions(entries []*regionEntry) {
	for _, e := range entries {
		atomic.AddInt64(&e.inflight, -1)
	}
}

func waitRegionDrain(ctx context.Context, e *regionEntry) error {
	for atomic.LoadInt64(&e.inflight) > 0 {
		select {
		case <-ctx.Done():
			return fmt.Errorf("region %d did not drain: %w", e.regionID, ctx.Err())
		case <-time.After(time.Millisecond):
		}
	}
	return nil
}

// The optional fork accessor retains this replica's applied Raft membership,
// even after automatic unloading. It is evidence of removal, not a replacement
// for a linearizable query when the replica still belongs to the group.
type appliedMembershipReader interface {
	AppliedMembership() *dragonboat.Membership
}

func localReplicaPresent(m *dragonboat.Membership, self uint64) bool {
	if _, ok := m.Nodes[self]; ok {
		return true
	}
	if _, ok := m.Observers[self]; ok {
		return true
	}
	_, ok := m.Witnesses[self]
	return ok
}

func replicaRemovalProven(e *regionEntry) bool {
	if e == nil || atomic.LoadInt32(&e.fenced) == 0 || atomic.LoadInt32(&e.fenceVerified) == 0 ||
		atomic.LoadInt64(&e.inflight) != 0 || e.appliedMembership == nil {
		return false
	}
	m := e.appliedMembership.AppliedMembership()
	if m == nil || m.ConfigChangeID <= atomic.LoadUint64(&e.fenceMembershipIndex) || localReplicaPresent(m, e.raftNodeID) {
		return false
	}
	_, removed := m.Removed[e.raftNodeID]
	return removed
}

func (n *MECNodeCM) ensureRegionResolved(ctx context.Context, rid uint64) error {
	if n.txns != nil && n.txns.PendingForRegion(rid) {
		_ = n.txns.Recover(ctx)
		if n.txns.PendingForRegion(rid) {
			return fmt.Errorf("region %d has unresolved coordinator decisions", rid)
		}
	}
	// REMOVE_MEMBER can close/unload the old replica before STOP_REGION. Only
	// a prior verified fence and the replica's applied removal permit this
	// exception. Its retained WAL is not deleted; remote prepared transactions
	// still belong to the surviving Raft group, not this removed replica.
	if e, ok := n.getRegion(rid); n.txns != nil && ok && replicaRemovalProven(e) {
		return nil
	}
	result, err := n.NodeHost.SyncRead(ctx, rid, &mec.PendingTxnQuery{})
	if err != nil {
		return fmt.Errorf("cannot verify pending transactions in region %d: %w", rid, err)
	}
	pending, ok := result.(*mec.PendingTxnResult)
	if !ok {
		return fmt.Errorf("unexpected pending-transaction result %T", result)
	}
	if len(pending.Transactions) > 0 {
		return fmt.Errorf("region %d has %d prepared transactions; resolve before reconfiguration", rid, len(pending.Transactions))
	}
	return nil
}

type raftTxnTransport struct{ node *MECNodeCM }

func (t raftTxnTransport) Propose(ctx context.Context, rid uint64, command mec.TxnCommand) (mec.TxnReply, error) {
	entries, err := t.node.acquireRegionIDs([]uint64{rid}, true)
	if err != nil {
		return mec.TxnReply{}, err
	}
	defer releaseRegions(entries)
	payload, err := json.Marshal(command)
	if err != nil {
		return mec.TxnReply{}, err
	}
	result, err := t.node.NodeHost.SyncPropose(ctx, t.node.NodeHost.GetNoOPSession(rid), payload)
	if err != nil {
		if t.node.stats != nil && (errors.Is(err, dragonboat.ErrClusterClosed) || errors.Is(err, dragonboat.ErrClusterNotFound)) {
			atomic.AddUint64(&t.node.stats.closedRegionFailures, 1)
		}
		return mec.TxnReply{}, err
	}
	return mec.DecodeTxnReply(result)
}

func (n *MECNodeCM) transactionID(req *pb.ProposeRequest) (string, error) {
	if req.ClientId != 0 {
		return fmt.Sprintf("node:%d/client:%d/request:%d", n.NodeID, req.ClientId, req.RequestId), nil
	}
	var nonce [16]byte
	if _, err := rand.Read(nonce[:]); err != nil {
		return "", err
	}
	return fmt.Sprintf("node:%d/anonymous:%s", n.NodeID, hex.EncodeToString(nonce[:])), nil
}

func transactionRetryHint(req *pb.ProposeRequest) string {
	if req.ClientId == 0 {
		return "automatic recovery will continue; anonymous retries create new transactions"
	}
	return "retry the same coordinator with the same client_id/request_id and payload"
}

func (n *MECNodeCM) proposalStats(success bool, latency time.Duration) {
	if n.stats == nil {
		return
	}
	if !success {
		atomic.AddUint64(&n.stats.failed, 1)
		return
	}
	atomic.AddUint64(&n.stats.success, 1)
	if atomic.AddUint64(&n.stats.sampleCounter, 1)%10 == 0 {
		n.stats.mu.Lock()
		if len(n.stats.samples) < cap(n.stats.samples) {
			n.stats.samples = append(n.stats.samples, latency)
		}
		n.stats.mu.Unlock()
	}
}

func (n *MECNodeCM) proposeTransaction(ctx context.Context, req *pb.ProposeRequest) (*pb.ProposeResponse, error) {
	start := time.Now()
	if n.stats != nil {
		atomic.AddUint64(&n.stats.total, 1)
	}
	success := false
	defer func() { n.proposalStats(success, time.Since(start)) }()
	if n.txns == nil {
		return nil, status.Error(codes.FailedPrecondition, "2PC coordinator is not initialized")
	}
	var write mec.KVData
	if err := json.Unmarshal(req.Data, &write); err != nil || write.Key == "" {
		write = mec.KVData{Key: fmt.Sprintf("key-%d-%d", req.ClientId, req.RequestId), Val: string(req.Data)}
	}
	if strings.HasPrefix(write.Key, "\x00") || write.Key == "disk_kv_applied_index" {
		return nil, status.Error(codes.InvalidArgument, "reserved state-machine key")
	}
	id, err := n.transactionID(req)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "transaction identity: %v", err)
	}
	var targets []*regionEntry
	var participantIDs []uint64
	if record, ok := n.txns.Get(id); ok {
		if record.Transaction.Write != write {
			return nil, status.Error(codes.InvalidArgument, "client_id/request_id already used for a different write")
		}
		participantIDs = record.Transaction.Participants
		if !record.Complete {
			targets, err = n.acquireRegionIDs(participantIDs, true)
		}
	} else {
		targets, err = n.acquireServingRegions(req.RegionId)
		for _, e := range targets {
			participantIDs = append(participantIDs, e.regionID)
		}
	}
	if err != nil {
		return nil, status.Errorf(codes.Unavailable, "transaction %s unavailable: %v; %s", id, err, transactionRetryHint(req))
	}
	defer releaseRegions(targets)
	pctx, cancel := context.WithTimeout(ctx, n.requestTimeout())
	defer cancel()
	outcome, err := n.txns.Execute(pctx, id, write, participantIDs)
	if errors.Is(err, twopc.ErrConflict) {
		return nil, status.Error(codes.InvalidArgument, "client_id/request_id already used for a different write")
	}
	resp := &pb.ProposeResponse{LatencyUs: time.Since(start).Microseconds(), RequestId: req.RequestId}
	if outcome.Complete && outcome.State == mec.TxnCommitted && err == nil {
		success = true
		resp.Success = true
		return resp, nil
	}
	if outcome.Complete && outcome.State == mec.TxnAborted {
		resp.Error = fmt.Sprintf("transaction %s aborted: %v", id, err)
		return resp, nil
	}
	if errors.Is(err, twopc.ErrUncertain) || !outcome.Complete {
		code := codes.Unavailable
		if errors.Is(pctx.Err(), context.DeadlineExceeded) {
			code = codes.DeadlineExceeded
		}
		if errors.Is(pctx.Err(), context.Canceled) {
			code = codes.Canceled
		}
		return nil, status.Errorf(code, "transaction %s outcome unconfirmed: %v; %s", id, err, transactionRetryHint(req))
	}
	return nil, status.Errorf(codes.FailedPrecondition, "transaction %s: %v", id, err)
}

func (n *MECNodeCM) readRegion(ctx context.Context, rid uint64, query interface{}, stale bool) (interface{}, error) {
	for {
		var result interface{}
		var err error
		if stale {
			result, err = n.NodeHost.StaleRead(rid, query)
		} else {
			result, err = n.NodeHost.SyncRead(ctx, rid, query)
		}
		if err == nil {
			return result, nil
		}
		if ctx.Err() != nil {
			return nil, fmt.Errorf("read unresolved: %w", ctx.Err())
		}
		if !(errors.Is(err, mec.ErrTxnPending) || errors.Is(err, dragonboat.ErrClusterNotReady) || errors.Is(err, dragonboat.ErrSystemBusy) || errors.Is(err, dragonboat.ErrTimeout)) {
			return nil, err
		}
		select {
		case <-ctx.Done():
			return nil, fmt.Errorf("read unresolved: %w", ctx.Err())
		case <-time.After(10 * time.Millisecond):
		}
	}
}

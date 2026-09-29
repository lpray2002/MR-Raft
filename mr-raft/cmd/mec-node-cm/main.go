// mec-node-cm: MR-Raft MEC node with a Cluster-Manager (CM) control plane.
//
// Compared with cmd/mec-node-grpc-full-topology this binary
//   - keeps a *dynamic* region table that the CM can change at runtime via the
//     ApplyConfig RPC (START_REGION / STOP_REGION / ADD_MEMBER / REMOVE_MEMBER /
//     MIGRATE_STATE), instead of a static env-var configuration;
//   - drains in-flight requests before stopping a region;
//   - exposes GetMembership (Raft membership view) and ScanRegion (state dump
//     for handoff) to the CM;
//   - uses Raft timing that matches the paper (Table I): tick 50 ms,
//     ElectionRTT 3 (=> 150-300 ms randomized election timeout), HeartbeatRTT 1
//     (=> 50 ms heartbeat); all overridable through env vars;
//   - writes a JSONL event log with per-directive timestamps so that the CM
//     can decompose T_trans = T_batch + T_part + T_cfg + T_joint.
//
// Env vars (all optional except MEC_NODE_ID):
//   MEC_NODE_ID, MEC_NODE_NAME, MEC_NODE_ADDRESS (raft addr), GRPC_PORT,
//   MEC_NODE_DATA_DIR, USE_MEMORY_SM (true/false),
//   RAFT_TICK_MS (50), ELECTION_RTT (3), HEARTBEAT_RTT (1), CHECK_QUORUM (true),
//   SNAPSHOT_ENTRIES (10000), COMPACTION_OVERHEAD (5000),
//   PROPOSE_TIMEOUT_MS (5000), DRAIN_TIMEOUT_MS (500),
//   NETWORK_DELAY_US / NETWORK_DELAY_MS (raft transport delay injection),
//   EVENT_LOG (path of JSONL event log),
//   MEC_NODE_REGIONS + CLUSTER_MEMBERS_<rid> (optional static bootstrap, same
//   format as the legacy binary; raft node id = rid*100 + node id).
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"os/signal"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"gitcode.com/lpray/multi-region-raft/pkg/mec"
	pb "gitcode.com/lpray/multi-region-raft/proto/raftservice"
	"github.com/lni/dragonboat/v3"
	"github.com/lni/dragonboat/v3/config"
	"github.com/lni/dragonboat/v3/logger"
	"github.com/lni/dragonboat/v3/raftio"
	sm "github.com/lni/dragonboat/v3/statemachine"
	gvfs "github.com/lni/goutils/vfs"
	"google.golang.org/grpc"
)

// ---------------------------------------------------------------------------
// Region table
// ---------------------------------------------------------------------------

type regionEntry struct {
	regionID   uint64
	raftNodeID uint64            // this node's raft node id inside the region
	members    map[uint64]string // last known members (raft node id -> addr)
	inflight   int64             // in-flight data-plane requests
	fenced     int32             // 1: no longer serving data-plane requests (leaving the region)
	startedAt  time.Time
}

// ---------------------------------------------------------------------------
// Node
// ---------------------------------------------------------------------------

type MECNodeCM struct {
	pb.UnimplementedRaftServiceServer

	NodeID      int
	NodeName    string
	NodeAddress string
	GRPCPort    int
	DataDir     string
	UseMemorySM bool

	TickMs             uint64
	ElectionRTT        uint64
	HeartbeatRTT       uint64
	CheckQuorum        bool
	SnapshotEntries    uint64
	CompactionOverhead uint64
	ProposeTimeout     time.Duration
	DrainTimeout       time.Duration
	NetworkDelay       time.Duration

	NodeHost   *dragonboat.NodeHost
	grpcServer *grpc.Server

	mu      sync.RWMutex
	regions map[uint64]*regionEntry // active (serving) regions
	version uint64                  // last applied CM config version

	cfgMu sync.Mutex // serializes ApplyConfig calls

	stats    *proposeStats
	eventLog *eventLogger
}

type proposeStats struct {
	total, success, failed uint64
	closedRegionFailures   uint64 // failures caused by a region being stopped mid-flight
	samples                []time.Duration
	sampleCounter          uint64
	mu                     sync.Mutex
	start                  time.Time
}

// ---------------------------------------------------------------------------
// Event log (JSONL)
// ---------------------------------------------------------------------------

type eventLogger struct {
	mu sync.Mutex
	f  *os.File
}

type nodeEvent struct {
	TsMs         int64  `json:"ts_ms"`
	TsUs         int64  `json:"ts_us,omitempty"` // microsecond timestamp (leader events)
	Node         int    `json:"node"`
	Version      uint64 `json:"version"`
	Op           string `json:"op"`
	Region       uint64 `json:"region"`
	Target       uint64 `json:"target,omitempty"`
	OK           bool   `json:"ok"`
	Err          string `json:"err,omitempty"`
	ElapsedUs    int64  `json:"elapsed_us"`
	LeaderWaitUs int64  `json:"leader_wait_us,omitempty"`
	LeaderID     uint64 `json:"leader_id,omitempty"`
	Term         uint64 `json:"term,omitempty"`
	Serving      bool   `json:"serving,omitempty"` // LEADER_UPDATED: region is in this node's serving table
	Migrated     uint32 `json:"migrated,omitempty"`
}

// LeaderUpdated implements raftio.IRaftEventListener. Dragonboat calls it
// whenever this node's view of a region's leader changes (LeaderID == 0: no
// known leader, e.g. during a leader transfer or a bootstrap election). Together
// with the START/FENCE/STOP directive events this gives the exact intervals in
// which a *serving* region of this node could not commit, i.e. the node-level
// write-unavailability used by the availability analysis (a write at this node
// must commit in every region of its serving table).
func (n *MECNodeCM) LeaderUpdated(info raftio.LeaderInfo) {
	now := time.Now()
	_, serving := n.servingRegion(info.ClusterID)
	n.eventLog.log(nodeEvent{
		TsMs: now.UnixMilli(), TsUs: now.UnixMicro(), Node: n.NodeID, Op: "LEADER_UPDATED",
		Region: info.ClusterID, OK: info.LeaderID != 0, LeaderID: info.LeaderID, Term: info.Term,
		Serving: serving,
	})
}

func newEventLogger(path string) (*eventLogger, error) {
	if path == "" {
		return &eventLogger{}, nil
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil, err
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return nil, err
	}
	return &eventLogger{f: f}, nil
}

func (l *eventLogger) log(ev nodeEvent) {
	if l == nil || l.f == nil {
		return
	}
	b, _ := json.Marshal(ev)
	l.mu.Lock()
	l.f.Write(append(b, '\n'))
	l.mu.Unlock()
}

func (l *eventLogger) close() {
	if l != nil && l.f != nil {
		l.f.Sync()
		l.f.Close()
	}
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------

func main() {
	logger.GetLogger("dragonboat").SetLevel(logger.WARNING)
	logger.GetLogger("raft").SetLevel(logger.WARNING)
	logger.GetLogger("rsm").SetLevel(logger.WARNING)
	logger.GetLogger("transport").SetLevel(logger.ERROR)
	logger.GetLogger("logdb").SetLevel(logger.WARNING)

	n := &MECNodeCM{
		regions: make(map[uint64]*regionEntry),
		stats:   &proposeStats{samples: make([]time.Duration, 0, 100000), start: time.Now()},
	}
	if err := n.loadConfigFromEnv(); err != nil {
		fmt.Printf("❌ 加载配置失败: %v\n", err)
		os.Exit(1)
	}
	var err error
	n.eventLog, err = newEventLogger(getEnv("EVENT_LOG", ""))
	if err != nil {
		fmt.Printf("❌ 打开事件日志失败: %v\n", err)
		os.Exit(1)
	}
	defer n.eventLog.close()

	fmt.Printf("\n╔══════════════════════════════════════════════════════════════╗\n")
	fmt.Printf("║  MEC节点启动 (CM 动态拓扑模式)                               ║\n")
	fmt.Printf("╚══════════════════════════════════════════════════════════════╝\n")
	fmt.Printf("  节点: %s (id=%d)  raft=%s  grpc=%d\n", n.NodeName, n.NodeID, n.NodeAddress, n.GRPCPort)
	fmt.Printf("  Raft: tick=%dms electionRTT=%d (%d-%dms) heartbeatRTT=%d (%dms) checkQuorum=%v\n",
		n.TickMs, n.ElectionRTT, n.ElectionRTT*n.TickMs, 2*n.ElectionRTT*n.TickMs,
		n.HeartbeatRTT, n.HeartbeatRTT*n.TickMs, n.CheckQuorum)
	fmt.Printf("  状态机: %s  数据目录: %s\n", map[bool]string{true: "MemoryKV", false: "DiskKV"}[n.UseMemorySM], n.DataDir)
	if n.NetworkDelay > 0 {
		fmt.Printf("  网络延迟注入: %v\n", n.NetworkDelay)
	}

	if err := n.createNodeHost(); err != nil {
		fmt.Printf("❌ 创建NodeHost失败: %v\n", err)
		os.Exit(1)
	}
	defer n.NodeHost.Stop()

	if err := n.bootstrapStaticRegions(); err != nil {
		fmt.Printf("❌ 静态 region 启动失败: %v\n", err)
		os.Exit(1)
	}

	if err := n.startGRPCServer(); err != nil {
		fmt.Printf("❌ 启动gRPC服务失败: %v\n", err)
		os.Exit(1)
	}
	defer n.grpcServer.GracefulStop()

	fmt.Printf("✓ MEC节点就绪，等待 CM 指令 / 客户端请求\n\n")

	stopStats := make(chan struct{})
	go n.printRealtimeStats(stopStats)

	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, syscall.SIGINT, syscall.SIGTERM)
	<-sigChan
	close(stopStats)
	n.printFinalStats()
	fmt.Printf("✓ MEC节点已关闭\n")
}

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

func (n *MECNodeCM) loadConfigFromEnv() error {
	var err error
	n.NodeID, err = strconv.Atoi(getEnv("MEC_NODE_ID", "0"))
	if err != nil || n.NodeID <= 0 {
		return fmt.Errorf("MEC_NODE_ID 必须为正整数")
	}
	n.NodeName = getEnv("MEC_NODE_NAME", fmt.Sprintf("mec-node-%d", n.NodeID))
	n.NodeAddress = getEnv("MEC_NODE_ADDRESS", fmt.Sprintf("127.0.0.1:%d", 30000+n.NodeID))
	n.GRPCPort, _ = strconv.Atoi(getEnv("GRPC_PORT", strconv.Itoa(50050+n.NodeID)))
	n.DataDir = getEnv("MEC_NODE_DATA_DIR", "/tmp/mec-cm-data")
	n.UseMemorySM = getEnv("USE_MEMORY_SM", "false") == "true"

	n.TickMs = uint64(getEnvInt("RAFT_TICK_MS", 50))
	n.ElectionRTT = uint64(getEnvInt("ELECTION_RTT", 3))
	n.HeartbeatRTT = uint64(getEnvInt("HEARTBEAT_RTT", 1))
	n.CheckQuorum = getEnv("CHECK_QUORUM", "true") == "true"
	n.SnapshotEntries = uint64(getEnvInt("SNAPSHOT_ENTRIES", 10000))
	n.CompactionOverhead = uint64(getEnvInt("COMPACTION_OVERHEAD", 5000))
	n.ProposeTimeout = time.Duration(getEnvInt("PROPOSE_TIMEOUT_MS", 5000)) * time.Millisecond
	n.DrainTimeout = time.Duration(getEnvInt("DRAIN_TIMEOUT_MS", 500)) * time.Millisecond

	if n.ElectionRTT <= 2*n.HeartbeatRTT {
		return fmt.Errorf("dragonboat 要求 ELECTION_RTT > 2*HEARTBEAT_RTT (got %d, %d)", n.ElectionRTT, n.HeartbeatRTT)
	}

	if delayUs := getEnvInt("NETWORK_DELAY_US", 0); delayUs > 0 {
		n.NetworkDelay = time.Duration(delayUs) * time.Microsecond
	} else if delayMs := getEnvInt("NETWORK_DELAY_MS", 0); delayMs > 0 {
		n.NetworkDelay = time.Duration(delayMs) * time.Millisecond
	}
	if n.NetworkDelay > 0 && uint64(n.NetworkDelay/time.Millisecond)*4 > n.HeartbeatRTT*n.TickMs {
		fmt.Printf("⚠️  注入延迟 %v 相对心跳 %dms 偏大，选举可能抖动\n", n.NetworkDelay, n.HeartbeatRTT*n.TickMs)
	}
	return nil
}

func (n *MECNodeCM) raftConfig(regionID, raftNodeID uint64) config.Config {
	return config.Config{
		NodeID:             raftNodeID,
		ClusterID:          regionID,
		ElectionRTT:        n.ElectionRTT,
		HeartbeatRTT:       n.HeartbeatRTT,
		CheckQuorum:        n.CheckQuorum,
		SnapshotEntries:    n.SnapshotEntries,
		CompactionOverhead: n.CompactionOverhead,
	}
}

func (n *MECNodeCM) createNodeHost() error {
	dataDir := filepath.Join(n.DataDir, n.NodeName)
	nhc := config.NodeHostConfig{
		WALDir:            filepath.Join(dataDir, "wal"),
		NodeHostDir:       filepath.Join(dataDir, "nh"),
		RTTMillisecond:    n.TickMs,
		RaftAddress:       n.NodeAddress,
		RaftEventListener: n, // LEADER_UPDATED events -> node-level unavailability analysis
	}
	if n.NetworkDelay > 0 {
		nhc.Expert.TransportFactory = &DelayTransportFactory{Delay: n.NetworkDelay}
	}
	// RAFT_MEMFS=true keeps the Raft WAL/LogDB in memory. Intended for local
	// functional runs on laptops where per-entry fsync (macOS F_FULLFSYNC
	// ~10-20 ms) stalls the Raft step loop and triggers spurious elections at
	// the paper's 50 ms tick; production/server runs should keep the default.
	if getEnv("RAFT_MEMFS", "false") == "true" {
		nhc.Expert.FS = gvfs.NewStrictMem()
		fmt.Printf("  Raft 日志: 内存文件系统 (RAFT_MEMFS=true)\n")
	}
	nh, err := dragonboat.NewNodeHost(nhc)
	if err != nil {
		return err
	}
	n.NodeHost = nh
	fmt.Printf("✓ NodeHost已创建\n")
	return nil
}

// bootstrapStaticRegions supports the legacy env-var bootstrap so that the same
// binary can also be used for static experiments.
func (n *MECNodeCM) bootstrapStaticRegions() error {
	regionsStr := strings.TrimSpace(getEnv("MEC_NODE_REGIONS", ""))
	if regionsStr == "" {
		return nil
	}
	for _, r := range strings.Split(regionsStr, ",") {
		rid, err := strconv.ParseUint(strings.TrimSpace(r), 10, 64)
		if err != nil {
			return fmt.Errorf("无效的Region ID: %v", err)
		}
		membersEnv := getEnv(fmt.Sprintf("CLUSTER_MEMBERS_%d", rid), "")
		if membersEnv == "" {
			return fmt.Errorf("未找到Region %d 的成员配置", rid)
		}
		members := make(map[uint64]string)
		for _, m := range strings.Split(membersEnv, ",") {
			parts := strings.SplitN(strings.TrimSpace(m), ":", 2)
			if len(parts) != 2 {
				return fmt.Errorf("无效的成员格式: %s", m)
			}
			id, err := strconv.ParseUint(parts[0], 10, 64)
			if err != nil {
				return fmt.Errorf("无效的节点ID: %s", parts[0])
			}
			members[id] = parts[1]
		}
		self := rid*100 + uint64(n.NodeID)
		if _, err := n.startRegion(rid, self, members, false, false, false, 0); err != nil {
			return err
		}
		fmt.Printf("  ✓ 静态 Region-%d 已启动 (%d 成员)\n", rid, len(members))
	}
	return nil
}

// ---------------------------------------------------------------------------
// Region lifecycle primitives
// ---------------------------------------------------------------------------

func (n *MECNodeCM) getRegion(rid uint64) (*regionEntry, bool) {
	n.mu.RLock()
	defer n.mu.RUnlock()
	e, ok := n.regions[rid]
	return e, ok
}

// activeRegions returns the regions this node currently serves data-plane
// requests for (fenced regions excluded).
func (n *MECNodeCM) activeRegions() []*regionEntry {
	n.mu.RLock()
	defer n.mu.RUnlock()
	out := make([]*regionEntry, 0, len(n.regions))
	for _, e := range n.regions {
		if atomic.LoadInt32(&e.fenced) == 0 {
			out = append(out, e)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].regionID < out[j].regionID })
	return out
}

// servingRegion is getRegion restricted to the data plane (fenced -> not found).
func (n *MECNodeCM) servingRegion(rid uint64) (*regionEntry, bool) {
	e, ok := n.getRegion(rid)
	if !ok || atomic.LoadInt32(&e.fenced) != 0 {
		return nil, false
	}
	return e, true
}

// fenceRegion stops routing data-plane requests to the region on this node and
// waits for in-flight ones to drain. The Raft instance keeps running, so the
// group keeps its quorum and the node can still be removed through a normal
// membership change; STOP_REGION afterwards shuts the instance down. Fencing
// before REMOVE_MEMBER guarantees that no request of this node is pending at
// the leader when the leader stops talking to it.
func (n *MECNodeCM) fenceRegion(rid uint64) error {
	e, ok := n.getRegion(rid)
	if !ok {
		return nil // idempotent (already stopped)
	}
	atomic.StoreInt32(&e.fenced, 1)
	deadline := time.Now().Add(n.DrainTimeout)
	for atomic.LoadInt64(&e.inflight) > 0 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	return nil
}

func (n *MECNodeCM) activeRegionIDs() []uint64 {
	rs := n.activeRegions()
	ids := make([]uint64, 0, len(rs))
	for _, e := range rs {
		ids = append(ids, e.regionID)
	}
	return ids
}

// startRegion starts (or joins) a Raft instance for the region on this node.
// Returns the leader-wait duration when waitLeader is set.
func (n *MECNodeCM) startRegion(rid, self uint64, members map[uint64]string,
	join bool, observer bool, waitLeader bool, timeout time.Duration) (time.Duration, error) {
	if _, ok := n.getRegion(rid); ok {
		return 0, nil // idempotent
	}
	rc := n.raftConfig(rid, self)
	// A node that was added with AddObserver must start with IsObserver=true
	// (dragonboat panics otherwise when it applies its own AddObserver entry);
	// the later PROMOTE_MEMBER (AddNode) turns it into a voter in place.
	rc.IsObserver = join && observer
	var err error
	if n.UseMemorySM {
		err = n.NodeHost.StartCluster(members, join, func(c, nid uint64) sm.IStateMachine {
			return mec.NewMemoryKV(c, nid)
		}, rc)
	} else {
		err = n.NodeHost.StartOnDiskCluster(members, join, func(c, nid uint64) sm.IOnDiskStateMachine {
			return mec.NewDiskKV(c, nid)
		}, rc)
	}
	if err != nil && !errors.Is(err, dragonboat.ErrClusterAlreadyExist) {
		return 0, err
	}
	register := func() {
		n.mu.Lock()
		n.regions[rid] = &regionEntry{regionID: rid, raftNodeID: self, members: members, startedAt: time.Now()}
		n.mu.Unlock()
	}
	if !waitLeader {
		register()
		return 0, nil
	}
	// The region enters the node's serving table only once a leader is known:
	// requests to a leaderless (bootstrapping) Raft group would just time out.
	if timeout <= 0 {
		timeout = 5 * time.Second
	}
	t0 := time.Now()
	deadline := t0.Add(timeout)
	for time.Now().Before(deadline) {
		if _, valid, err := n.NodeHost.GetLeaderID(rid); err == nil && valid {
			register()
			return time.Since(t0), nil
		}
		time.Sleep(2 * time.Millisecond)
	}
	register() // serve anyway; the CM will see the failure and may retry
	return time.Since(t0), fmt.Errorf("region %d: leader not elected within %v", rid, timeout)
}

// stopRegion drains in-flight requests, then stops the Raft instance and
// removes its on-disk data.
func (n *MECNodeCM) stopRegion(rid uint64) error {
	n.mu.Lock()
	e, ok := n.regions[rid]
	if ok {
		delete(n.regions, rid) // new requests no longer target this region
	}
	n.mu.Unlock()
	if !ok {
		return nil // idempotent
	}
	deadline := time.Now().Add(n.DrainTimeout)
	for atomic.LoadInt64(&e.inflight) > 0 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	if err := n.NodeHost.StopCluster(rid); err != nil && !errors.Is(err, dragonboat.ErrClusterNotFound) {
		return err
	}
	// Best effort cleanup; ignore errors (e.g. data already removed).
	go func() {
		time.Sleep(200 * time.Millisecond)
		_ = n.NodeHost.RemoveData(rid, e.raftNodeID)
	}()
	return nil
}

// membership change kinds
const (
	mcAddVoter    = iota // SyncRequestAddNode (also promotes an existing observer)
	mcAddObserver        // SyncRequestAddObserver: joins without voting power, never campaigns
	mcRemove             // SyncRequestDeleteNode
)

// membershipChange runs a Raft membership change with retry on ErrSystemBusy
// (another config change pending) and idempotent handling of ErrRejected.
//
// New members are added as observers first and promoted after they have
// started and caught up (PROMOTE_MEMBER): a freshly started voter that has not
// yet heard from the leader would campaign with a higher term and depose the
// leader (the classic "disruptive new server" problem).
func (n *MECNodeCM) membershipChange(ctx context.Context, rid, target uint64,
	addr string, kind int) error {
	if _, ok := n.getRegion(rid); !ok {
		return fmt.Errorf("region %d not hosted on node %d", rid, n.NodeID)
	}
	backoff := 5 * time.Millisecond
	for {
		var err error
		switch kind {
		case mcAddVoter:
			err = n.NodeHost.SyncRequestAddNode(ctx, rid, target, addr, 0)
		case mcAddObserver:
			err = n.NodeHost.SyncRequestAddObserver(ctx, rid, target, addr, 0)
		default:
			err = n.NodeHost.SyncRequestDeleteNode(ctx, rid, target, 0)
		}
		if err == nil {
			return nil
		}
		if errors.Is(err, dragonboat.ErrRejected) {
			// Already applied? Check the membership and treat as success.
			mctx, cancel := context.WithTimeout(ctx, 2*time.Second)
			m, merr := n.NodeHost.SyncGetClusterMembership(mctx, rid)
			cancel()
			if merr == nil {
				_, isVoter := m.Nodes[target]
				_, isObserver := m.Observers[target]
				switch kind {
				case mcAddVoter:
					if isVoter {
						return nil
					}
				case mcAddObserver:
					if isVoter || isObserver {
						return nil
					}
				default:
					if !isVoter && !isObserver {
						return nil
					}
				}
			}
			return err
		}
		if errors.Is(err, dragonboat.ErrSystemBusy) || errors.Is(err, dragonboat.ErrTimeout) {
			select {
			case <-ctx.Done():
				return fmt.Errorf("%v (last: %v)", ctx.Err(), err)
			case <-time.After(backoff):
			}
			if backoff < 100*time.Millisecond {
				backoff *= 2
			}
			continue
		}
		return err
	}
}

// removeMember removes target from the region. If target is the current
// leader, leadership is first transferred to this (staying) node so that the
// region never goes through a leaderless election-timeout window: removing the
// leader directly would make it step down, leave the group without a leader
// for 150-300 ms and strand in-flight ReadIndex requests until they time out.
// Returns the time spent waiting for the transfer.
func (n *MECNodeCM) removeMember(ctx context.Context, rid, target uint64) (time.Duration, error) {
	e, ok := n.getRegion(rid)
	if !ok {
		return 0, fmt.Errorf("region %d not hosted on node %d", rid, n.NodeID)
	}
	var wait time.Duration
	if lid, valid, err := n.NodeHost.GetLeaderID(rid); err == nil && valid && lid == target && e.raftNodeID != target {
		t0 := time.Now()
		if terr := n.NodeHost.RequestLeaderTransfer(rid, e.raftNodeID); terr == nil {
			deadline := t0.Add(time.Second)
			if d, ok := ctx.Deadline(); ok && d.Before(deadline) {
				deadline = d
			}
			for time.Now().Before(deadline) {
				if lid2, valid2, _ := n.NodeHost.GetLeaderID(rid); valid2 && lid2 != target {
					break
				}
				time.Sleep(2 * time.Millisecond)
			}
		}
		wait = time.Since(t0) // proceed with the removal even if the transfer did not finish
	}
	return wait, n.membershipChange(ctx, rid, target, "", mcRemove)
}

// migrateState re-proposes every KV pair of the local src region into dst.
func (n *MECNodeCM) migrateState(ctx context.Context, src, dst uint64) (uint32, error) {
	if _, ok := n.getRegion(src); !ok {
		return 0, fmt.Errorf("src region %d not hosted", src)
	}
	if _, ok := n.getRegion(dst); !ok {
		return 0, fmt.Errorf("dst region %d not hosted", dst)
	}
	rctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	res, err := n.NodeHost.SyncRead(rctx, src, &mec.ScanQuery{})
	cancel()
	if err != nil {
		return 0, fmt.Errorf("scan src %d: %v", src, err)
	}
	scan, ok := res.(*mec.ScanResult)
	if !ok {
		return 0, fmt.Errorf("unexpected scan result type %T", res)
	}
	if len(scan.KVs) == 0 {
		return 0, nil
	}
	sem := make(chan struct{}, 16)
	var wg sync.WaitGroup
	var migrated uint32
	var firstErr error
	var errMu sync.Mutex
	session := n.NodeHost.GetNoOPSession(dst)
	for k, v := range scan.KVs {
		// carry the version so that the destination applies version-aware LWW
		payload, _ := json.Marshal(mec.KVData{Key: k, Val: v, Ver: scan.Vers[k]})
		wg.Add(1)
		sem <- struct{}{}
		go func(p []byte) {
			defer wg.Done()
			defer func() { <-sem }()
			pctx, pcancel := context.WithTimeout(ctx, n.ProposeTimeout)
			defer pcancel()
			if _, err := n.NodeHost.SyncPropose(pctx, session, p); err != nil {
				errMu.Lock()
				if firstErr == nil {
					firstErr = err
				}
				errMu.Unlock()
				return
			}
			atomic.AddUint32(&migrated, 1)
		}(payload)
	}
	wg.Wait()
	return migrated, firstErr
}

// ---------------------------------------------------------------------------
// CM control-plane RPCs
// ---------------------------------------------------------------------------

func (n *MECNodeCM) ApplyConfig(ctx context.Context, req *pb.ApplyConfigRequest) (*pb.ApplyConfigResponse, error) {
	n.cfgMu.Lock()
	defer n.cfgMu.Unlock()

	if req.Version < atomic.LoadUint64(&n.version) {
		return nil, fmt.Errorf("stale config version %d < applied %d", req.Version, n.version)
	}
	batchStart := time.Now()
	results := make([]*pb.DirectiveResult, 0, len(req.Directives))
	for _, d := range req.Directives {
		results = append(results, n.applyDirective(ctx, req.Version, d))
	}
	atomic.StoreUint64(&n.version, req.Version)
	return &pb.ApplyConfigResponse{
		Version:       req.Version,
		Results:       results,
		ElapsedUs:     time.Since(batchStart).Microseconds(),
		ActiveRegions: n.activeRegionIDs(),
	}, nil
}

func (n *MECNodeCM) applyDirective(ctx context.Context, version uint64, d *pb.Directive) *pb.DirectiveResult {
	t0 := time.Now()
	res := &pb.DirectiveResult{Op: d.Op, RegionId: d.RegionId, Success: true}
	timeout := time.Duration(d.TimeoutMs) * time.Millisecond
	if timeout <= 0 {
		timeout = 5 * time.Second
	}
	dctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	var err error
	switch d.Op {
	case pb.DirectiveOp_START_REGION:
		var wait time.Duration
		wait, err = n.startRegion(d.RegionId, d.SelfRaftNodeId, d.Members, d.Join, d.AsObserver, d.WaitForLeader, timeout)
		res.LeaderWaitUs = wait.Microseconds()
		if lid, valid, lerr := n.NodeHost.GetLeaderID(d.RegionId); lerr == nil && valid {
			res.LeaderId = lid
		}
	case pb.DirectiveOp_STOP_REGION:
		err = n.stopRegion(d.RegionId)
	case pb.DirectiveOp_FENCE_REGION:
		err = n.fenceRegion(d.RegionId)
	case pb.DirectiveOp_ADD_MEMBER:
		kind := mcAddVoter
		if d.AsObserver {
			kind = mcAddObserver
		}
		err = n.membershipChange(dctx, d.RegionId, d.TargetRaftNodeId, d.TargetAddr, kind)
	case pb.DirectiveOp_PROMOTE_MEMBER:
		err = n.membershipChange(dctx, d.RegionId, d.TargetRaftNodeId, d.TargetAddr, mcAddVoter)
	case pb.DirectiveOp_REMOVE_MEMBER:
		var wait time.Duration
		wait, err = n.removeMember(dctx, d.RegionId, d.TargetRaftNodeId)
		res.LeaderWaitUs = wait.Microseconds() // leader-transfer wait (0 when the target was not the leader)
		if lid, valid, lerr := n.NodeHost.GetLeaderID(d.RegionId); lerr == nil && valid {
			res.LeaderId = lid
		}
	case pb.DirectiveOp_MIGRATE_STATE:
		var cnt uint32
		cnt, err = n.migrateState(dctx, d.SrcRegionId, d.RegionId)
		res.MigratedKeys = cnt
	default:
		err = fmt.Errorf("unknown directive op %v", d.Op)
	}
	if err != nil {
		res.Success = false
		res.Error = err.Error()
	}
	res.ElapsedUs = time.Since(t0).Microseconds()

	n.eventLog.log(nodeEvent{
		TsMs: t0.UnixMilli(), Node: n.NodeID, Version: version, Op: d.Op.String(),
		Region: d.RegionId, Target: d.TargetRaftNodeId, OK: res.Success, Err: res.Error,
		ElapsedUs: res.ElapsedUs, LeaderWaitUs: res.LeaderWaitUs, LeaderID: res.LeaderId,
		Migrated: res.MigratedKeys,
	})
	return res
}

func (n *MECNodeCM) GetMembership(ctx context.Context, req *pb.MembershipRequest) (*pb.MembershipResponse, error) {
	var entries []*regionEntry
	if req.RegionId != 0 {
		if e, ok := n.getRegion(req.RegionId); ok {
			entries = []*regionEntry{e}
		}
	} else {
		entries = n.activeRegions()
	}
	out := make([]*pb.RegionMembership, 0, len(entries))
	for _, e := range entries {
		rm := &pb.RegionMembership{RegionId: e.regionID, SelfRaftNodeId: e.raftNodeID}
		mctx, cancel := context.WithTimeout(ctx, 2*time.Second)
		m, err := n.NodeHost.SyncGetClusterMembership(mctx, e.regionID)
		cancel()
		if err == nil {
			rm.Members = m.Nodes
			rm.Observers = m.Observers
			rm.ConfigChangeId = m.ConfigChangeID
		} else {
			rm.Members = e.members // fall back to the CM-provided view
		}
		if lid, valid, lerr := n.NodeHost.GetLeaderID(e.regionID); lerr == nil {
			rm.LeaderId = lid
			rm.LeaderValid = valid
		}
		out = append(out, rm)
	}
	return &pb.MembershipResponse{
		Version:     atomic.LoadUint64(&n.version),
		Regions:     out,
		TimestampMs: time.Now().UnixMilli(),
	}, nil
}

func (n *MECNodeCM) ScanRegion(ctx context.Context, req *pb.ScanRequest) (*pb.ScanResponse, error) {
	if _, ok := n.getRegion(req.RegionId); !ok {
		return &pb.ScanResponse{Success: false, Error: fmt.Sprintf("region %d not hosted", req.RegionId)}, nil
	}
	rctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	res, err := n.NodeHost.SyncRead(rctx, req.RegionId, &mec.ScanQuery{Limit: int(req.Limit)})
	if err != nil {
		return &pb.ScanResponse{Success: false, Error: err.Error()}, nil
	}
	scan, ok := res.(*mec.ScanResult)
	if !ok {
		return &pb.ScanResponse{Success: false, Error: fmt.Sprintf("unexpected result %T", res)}, nil
	}
	return &pb.ScanResponse{Success: true, Kvs: scan.KVs, Vers: scan.Vers}, nil
}

// ---------------------------------------------------------------------------
// Data-plane RPCs
// ---------------------------------------------------------------------------

// Propose replicates the request to all regions this node currently belongs to
// (region_id == 0) or to one specific region; succeeds only if every target
// region commits (cross-region atomic commitment, Sec. IV-B.5).
func (n *MECNodeCM) Propose(ctx context.Context, req *pb.ProposeRequest) (*pb.ProposeResponse, error) {
	var data mec.KVData
	if err := json.Unmarshal(req.Data, &data); err != nil || data.Key == "" {
		data = mec.KVData{Key: fmt.Sprintf("key-%d-%d", req.ClientId, req.RequestId), Val: string(req.Data)}
	}
	payload, err := json.Marshal(data)
	if err != nil {
		atomic.AddUint64(&n.stats.total, 1)
		atomic.AddUint64(&n.stats.failed, 1)
		return nil, fmt.Errorf("序列化失败: %v", err)
	}

	var targets []*regionEntry
	if req.RegionId != 0 {
		e, ok := n.servingRegion(req.RegionId)
		if !ok {
			atomic.AddUint64(&n.stats.total, 1)
			atomic.AddUint64(&n.stats.failed, 1)
			return nil, fmt.Errorf("region %d not served on node %d", req.RegionId, n.NodeID)
		}
		targets = []*regionEntry{e}
	} else {
		targets = n.activeRegions()
	}
	atomic.AddUint64(&n.stats.total, 1)
	if len(targets) == 0 {
		atomic.AddUint64(&n.stats.failed, 1)
		return nil, fmt.Errorf("node %d hosts no region", n.NodeID)
	}

	pctx, cancel := context.WithTimeout(ctx, n.ProposeTimeout)
	defer cancel()
	start := time.Now()

	var wg sync.WaitGroup
	var mu sync.Mutex
	results := make(map[uint64]sm.Result, len(targets))
	failures := make(map[uint64]error)
	for _, e := range targets {
		wg.Add(1)
		atomic.AddInt64(&e.inflight, 1)
		go func(e *regionEntry) {
			defer wg.Done()
			defer atomic.AddInt64(&e.inflight, -1)
			session := n.NodeHost.GetNoOPSession(e.regionID)
			cs, err := n.NodeHost.SyncPropose(pctx, session, payload)
			mu.Lock()
			defer mu.Unlock()
			if err != nil {
				if errors.Is(err, dragonboat.ErrClusterClosed) || errors.Is(err, dragonboat.ErrClusterNotFound) {
					atomic.AddUint64(&n.stats.closedRegionFailures, 1)
				}
				failures[e.regionID] = err
				return
			}
			results[e.regionID] = cs
		}(e)
	}
	wg.Wait()
	lat := time.Since(start)

	if len(failures) == 0 {
		atomic.AddUint64(&n.stats.success, 1)
		if c := atomic.AddUint64(&n.stats.sampleCounter, 1); c%10 == 0 {
			n.stats.mu.Lock()
			if len(n.stats.samples) < cap(n.stats.samples) {
				n.stats.samples = append(n.stats.samples, lat)
			}
			n.stats.mu.Unlock()
		}
		var any sm.Result
		for _, r := range results {
			any = r
			break
		}
		return &pb.ProposeResponse{Success: true, LatencyUs: lat.Microseconds(), RequestId: req.RequestId, ResultIndex: any.Value}, nil
	}
	atomic.AddUint64(&n.stats.failed, 1)
	var sb strings.Builder
	fmt.Fprintf(&sb, "Propose失败: %d/%d regions失败: ", len(failures), len(targets))
	for rid, ferr := range failures {
		fmt.Fprintf(&sb, "[%d: %v] ", rid, ferr)
	}
	return nil, errors.New(sb.String())
}

// SyncRead reads a key from the node's regions (ReadIndex linearizable read by
// default, local StaleRead when req.Stale is set).
func (n *MECNodeCM) SyncRead(ctx context.Context, req *pb.SyncReadRequest) (*pb.SyncReadResponse, error) {
	start := time.Now()
	var targets []*regionEntry
	if req.RegionId != 0 {
		if e, ok := n.servingRegion(req.RegionId); ok {
			targets = []*regionEntry{e}
		}
	} else {
		targets = n.activeRegions()
	}
	regionValues := make(map[uint64]string)
	var failures []string
	var bestVer uint64
	var bestVal string
	found := false
	query := &mec.KeyQuery{Key: req.Key}
	// Read the regions concurrently; a node in several regions returns the
	// freshest committed copy (highest version) among them. That claim only
	// holds if *every* region answered: a region that could not be read (e.g.
	// momentarily leaderless) is retried within the request budget, and if it
	// still fails the whole read fails instead of silently returning a
	// possibly older copy from the other regions. Regions closed concurrently
	// by a STOP_REGION are simply skipped (their state has been handed off).
	type rres struct {
		rid uint64
		res interface{}
		err error
	}
	results := make(chan rres, len(targets))
	rctx, rcancel := context.WithTimeout(ctx, n.ProposeTimeout)
	defer rcancel()
	for _, e := range targets {
		atomic.AddInt64(&e.inflight, 1)
		go func(e *regionEntry) {
			defer atomic.AddInt64(&e.inflight, -1)
			var result interface{}
			var err error
			if req.Stale {
				result, err = n.NodeHost.StaleRead(e.regionID, query)
			} else {
				for {
					result, err = n.NodeHost.SyncRead(rctx, e.regionID, query)
					if err == nil || rctx.Err() != nil {
						break
					}
					if !(errors.Is(err, dragonboat.ErrClusterNotReady) || errors.Is(err, dragonboat.ErrSystemBusy) ||
						errors.Is(err, dragonboat.ErrTimeout)) {
						break
					}
					select { // transient (no leader yet / busy): retry shortly
					case <-rctx.Done():
					case <-time.After(20 * time.Millisecond):
					}
					if rctx.Err() != nil {
						break
					}
				}
			}
			results <- rres{e.regionID, result, err}
		}(e)
	}
	for range targets {
		r := <-results
		if r.err != nil {
			if errors.Is(r.err, dragonboat.ErrClusterClosed) || errors.Is(r.err, dragonboat.ErrClusterNotFound) {
				continue // region retired while the read was in flight
			}
			failures = append(failures, fmt.Sprintf("region %d: %v", r.rid, r.err))
			continue
		}
		kv, ok := r.res.(*mec.KVResult)
		if !ok || !kv.Found {
			continue
		}
		regionValues[r.rid] = kv.Val
		if !found || kv.Ver > bestVer {
			found, bestVer, bestVal = true, kv.Ver, kv.Val
		}
	}
	resp := &pb.SyncReadResponse{LatencyUs: time.Since(start).Microseconds(), RegionValues: regionValues}
	switch {
	case len(targets) == 0:
		resp.Error = fmt.Sprintf("node %d hosts no region", n.NodeID)
	case len(failures) > 0:
		// partial read: freshness cannot be guaranteed -> report failure (client may retry)
		resp.Error = "partial read: " + strings.Join(failures, "; ")
	default:
		resp.Found, resp.Value = found, bestVal
	}
	return resp, nil
}

func (n *MECNodeCM) GetStatus(ctx context.Context, req *pb.StatusRequest) (*pb.StatusResponse, error) {
	var entries []*regionEntry
	if req.RegionId != 0 {
		if e, ok := n.getRegion(req.RegionId); ok {
			entries = []*regionEntry{e}
		}
	} else {
		entries = n.activeRegions()
	}
	regions := make([]*pb.RegionStatus, 0, len(entries))
	for _, e := range entries {
		leaderID, valid, err := n.NodeHost.GetLeaderID(e.regionID)
		if err != nil || !valid {
			regions = append(regions, &pb.RegionStatus{RegionId: e.regionID})
			continue
		}
		regions = append(regions, &pb.RegionStatus{RegionId: e.regionID, IsLeader: leaderID == e.raftNodeID, LeaderId: leaderID})
	}
	return &pb.StatusResponse{Regions: regions, Timestamp: time.Now().Unix()}, nil
}

func (n *MECNodeCM) ProposeBatch(stream pb.RaftService_ProposeBatchServer) error {
	for {
		req, err := stream.Recv()
		if err != nil {
			return err
		}
		resp, err := n.Propose(stream.Context(), req)
		if err != nil {
			resp = &pb.ProposeResponse{Success: false, Error: err.Error(), RequestId: req.RequestId}
		}
		if err := stream.Send(resp); err != nil {
			return err
		}
	}
}

func (n *MECNodeCM) RequestLeaderTransfer(ctx context.Context, req *pb.LeaderTransferRequest) (*pb.LeaderTransferResponse, error) {
	target := req.TargetNodeId
	if target == 0 {
		if e, ok := n.getRegion(req.RegionId); ok {
			target = e.raftNodeID
		}
	}
	if err := n.NodeHost.RequestLeaderTransfer(req.RegionId, target); err != nil {
		return &pb.LeaderTransferResponse{Success: false, Error: err.Error(), RegionId: req.RegionId, TargetNodeId: target}, nil
	}
	return &pb.LeaderTransferResponse{Success: true, RegionId: req.RegionId, TargetNodeId: target}, nil
}

// ---------------------------------------------------------------------------
// gRPC server & stats
// ---------------------------------------------------------------------------

func (n *MECNodeCM) startGRPCServer() error {
	lis, err := net.Listen("tcp", fmt.Sprintf("0.0.0.0:%d", n.GRPCPort))
	if err != nil {
		return fmt.Errorf("监听失败: %v", err)
	}
	n.grpcServer = grpc.NewServer(
		grpc.MaxRecvMsgSize(100*1024*1024),
		grpc.MaxSendMsgSize(100*1024*1024),
		grpc.MaxConcurrentStreams(10000),
	)
	pb.RegisterRaftServiceServer(n.grpcServer, n)
	go func() {
		if err := n.grpcServer.Serve(lis); err != nil {
			fmt.Printf("❌ gRPC服务错误: %v\n", err)
		}
	}()
	fmt.Printf("✓ gRPC 服务监听 :%d\n", n.GRPCPort)
	return nil
}

func (n *MECNodeCM) printRealtimeStats(stop <-chan struct{}) {
	t := time.NewTicker(5 * time.Second)
	defer t.Stop()
	var lastTotal uint64
	for {
		select {
		case <-stop:
			return
		case <-t.C:
			total := atomic.LoadUint64(&n.stats.total)
			if total == lastTotal {
				continue
			}
			lastTotal = total
			fmt.Printf("[Stats] regions=%v total=%d ok=%d fail=%d (closed-region=%d)\n",
				n.activeRegionIDs(), total, atomic.LoadUint64(&n.stats.success),
				atomic.LoadUint64(&n.stats.failed), atomic.LoadUint64(&n.stats.closedRegionFailures))
		}
	}
}

func (n *MECNodeCM) printFinalStats() {
	total := atomic.LoadUint64(&n.stats.total)
	fmt.Printf("\n服务端统计 (node %d): total=%d ok=%d fail=%d closed-region-fail=%d version=%d regions=%v\n",
		n.NodeID, total, atomic.LoadUint64(&n.stats.success), atomic.LoadUint64(&n.stats.failed),
		atomic.LoadUint64(&n.stats.closedRegionFailures), atomic.LoadUint64(&n.version), n.activeRegionIDs())
}

func getEnv(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func getEnvInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if i, err := strconv.Atoi(v); err == nil {
			return i
		}
	}
	return def
}

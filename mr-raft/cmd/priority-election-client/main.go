package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	pb "gitcode.com/lpray/multi-region-raft/proto/raftservice"
	"github.com/bojand/ghz/runner"
	"github.com/jhump/protoreflect/dynamic"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

// ---- 配置结构 (与 grpc-client-full-topology 完全一致) ----

type NodeConfig struct {
	NodeID      int
	Host        string
	Port        int
	Regions     []uint64
	Concurrency int
	TargetQPS   int
}

type BenchmarkConfig struct {
	Nodes          []NodeConfig
	Duration       time.Duration
	ProposalSize   int
	SingleNodeMode bool
	OutputFile     string
}

// ---- 逐时间窗口采样输出结构 ----

type SecondSample struct {
	TimeSec      float64 `json:"time_sec"`
	SuccessInSec uint64  `json:"success_in_sec"`
	FailedInSec  uint64  `json:"failed_in_sec"`
	TPS          float64 `json:"tps"`
	CumSuccess   uint64  `json:"cum_success"`
	CumFailed    uint64  `json:"cum_failed"`
}

type NodeResult struct {
	NodeID      int     `json:"node_id"`
	TotalReqs   uint64  `json:"total_requests"`
	SuccessReqs uint64  `json:"success_requests"`
	FailedReqs  uint64  `json:"failed_requests"`
	TPS         float64 `json:"tps"`
	AvgLatency  float64 `json:"avg_latency_ms"`
	P50Latency  float64 `json:"p50_latency_ms"`
	P95Latency  float64 `json:"p95_latency_ms"`
	P99Latency  float64 `json:"p99_latency_ms"`
	Duration    float64 `json:"duration_seconds"`
}

type RecoveryOutput struct {
	Config      OutputConfig   `json:"config"`
	Samples     []SecondSample `json:"samples"`
	NodeResults []NodeResult   `json:"node_results"`
	Summary     OutputSummary  `json:"summary"`
}

type OutputConfig struct {
	Nodes        int    `json:"nodes"`
	Duration     int    `json:"duration_seconds"`
	Concurrency  string `json:"concurrency"`
	ProposalSize int    `json:"proposal_size"`
	StartTime    string `json:"start_time"`
}

type OutputSummary struct {
	TotalSuccess uint64  `json:"total_success"`
	TotalFailed  uint64  `json:"total_failed"`
	AvgTPS       float64 `json:"avg_tps"`
	ElapsedSec   float64 `json:"elapsed_seconds"`
}

// LeaderInfo 存储region的leader信息
type LeaderInfo struct {
	RegionID uint64
	LeaderID uint64
	NodeID   int
	NodeHost string
	Term     uint64
}

// ---- Main ----

func main() {
	config := loadConfig()

	fmt.Printf("\n╔══════════════════════════════════════════════════════════════╗\n")
	fmt.Printf("║  故障恢复压测客户端 (基于 ghz, 逐秒采样)                     ║\n")
	fmt.Printf("╚══════════════════════════════════════════════════════════════╝\n\n")
	fmt.Printf("配置信息:\n")
	fmt.Printf("  节点数量:     %d\n", len(config.Nodes))
	fmt.Printf("  测试时长:     %v\n", config.Duration)
	fmt.Printf("  提案大小:     %d bytes\n", config.ProposalSize)
	fmt.Printf("\n节点列表:\n")
	for _, node := range config.Nodes {
		qpsStr := "不限制"
		if node.TargetQPS > 0 {
			qpsStr = fmt.Sprintf("%d", node.TargetQPS)
		}
		fmt.Printf("  Node-%d: %s:%d, Regions=%v, 并发=%d, QPS=%s\n",
			node.NodeID, node.Host, node.Port, node.Regions, node.Concurrency, qpsStr)
	}

	// 等待服务端就绪（带重试，只检查 gRPC 端口可连，不等 region leader 就绪）
	readyTimeout := time.Duration(getEnvInt("READY_TIMEOUT_SECONDS", 60)) * time.Second
	if config.SingleNodeMode {
		readyTimeout = 15 * time.Second
	}
	fmt.Printf("\n⏳ 等待服务端就绪（最多 %v，轮询重试）...\n", readyTimeout)
	if err := waitNodesReady(config.Nodes, readyTimeout); err != nil {
		fmt.Printf("❌ 等待节点就绪超时: %v\n", err)
		os.Exit(1)
	}
	fmt.Printf("  ✓ 所有节点连接成功\n")

	if !config.SingleNodeMode {
		fmt.Printf("\n👑 查询当前 Region Leader 信息（非阻塞）...\n")
		queryAndPrintLeaders(config.Nodes)
	}

	fmt.Printf("\n🚀 开始压测...\n\n")

	// 为每个节点启动压测 (与 grpc-client-full-topology 完全一致)
	var wg sync.WaitGroup
	reports := make([]*runner.Report, len(config.Nodes))
	errors := make([]error, len(config.Nodes))

	startTime := time.Now()

	for i, node := range config.Nodes {
		if node.Concurrency < 1 {
			continue
		}
		wg.Add(1)
		go func(idx int, n NodeConfig) {
			defer wg.Done()
			report, err := runNodeBenchmark(n, config)
			reports[idx] = report
			errors[idx] = err
		}(i, node)
	}

	// 实时进度输出（ghz runner.Run 是阻塞的，无法在内部回调，外部心跳展示进度）
	progressDone := make(chan struct{})
	go func() {
		ticker := time.NewTicker(10 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ticker.C:
				elapsed := time.Since(startTime)
				pct := float64(elapsed) / float64(config.Duration) * 100
				fmt.Printf("[压测进度] 已运行 %v / %v (%.0f%%)\n",
					elapsed.Round(time.Second), config.Duration, pct)
			case <-progressDone:
				return
			}
		}
	}()

	wg.Wait()
	close(progressDone)
	elapsed := time.Since(startTime)

	hasError := false
	for i, err := range errors {
		if err != nil {
			fmt.Printf("Node-%d 压测失败: %v\n", config.Nodes[i].NodeID, err)
			hasError = true
		}
	}
	if hasError {
		os.Exit(1)
	}

	// 输出聚合结果
	printResults(reports, config)

	// 从 Details 中提取逐时间窗口 TPS
	samples := extractPerSecondSamples(reports, startTime)
	nodeResults := extractNodeResults(reports, config)

	var totalSuccess, totalFailed uint64
	for _, nr := range nodeResults {
		totalSuccess += nr.SuccessReqs
		totalFailed += nr.FailedReqs
	}

	output := RecoveryOutput{
		Config: OutputConfig{
			Nodes:        len(config.Nodes),
			Duration:     int(config.Duration.Seconds()),
			ProposalSize: config.ProposalSize,
			StartTime:    startTime.Format(time.RFC3339),
		},
		Samples:     samples,
		NodeResults: nodeResults,
		Summary: OutputSummary{
			TotalSuccess: totalSuccess,
			TotalFailed:  totalFailed,
			AvgTPS:       float64(totalSuccess) / elapsed.Seconds(),
			ElapsedSec:   elapsed.Seconds(),
		},
	}

	jsonData, _ := json.MarshalIndent(output, "", "  ")
	if err := os.WriteFile(config.OutputFile, jsonData, 0644); err != nil {
		fmt.Printf("❌ 写入输出文件失败: %v\n", err)
		os.Exit(1)
	}
	fmt.Printf("\n✅ 结果已保存到: %s\n", config.OutputFile)
}

// ---- ghz 压测 (与 grpc-client-full-topology 完全一致的配置) ----

func runNodeBenchmark(node NodeConfig, config *BenchmarkConfig) (*runner.Report, error) {
	testData := make([]byte, config.ProposalSize)
	for i := range testData {
		testData[i] = byte('A' + (i % 26))
	}

	// A repeated client/request pair is a retry of one transaction. Generate a
	// fresh ID per RPC so this benchmark measures new commits, including when
	// several ghz workers call the provider concurrently.
	var requestID atomic.Int64
	requestID.Store(time.Now().UnixNano())

	connections := uint(node.Concurrency)
	if connections < 1 {
		connections = 1
	}
	if connections > 1000 {
		connections = 1000
	}

	opts := []runner.Option{
		runner.WithInsecure(true),
		runner.WithConcurrency(uint(node.Concurrency)),
		runner.WithConnections(connections),
		runner.WithRunDuration(config.Duration),
		runner.WithDataProvider(func(*runner.CallData) ([]*dynamic.Message, error) {
			request := &pb.ProposeRequest{Data: testData, ClientId: int64(node.NodeID), RequestId: requestID.Add(1)}
			message, err := dynamic.AsDynamicMessage(request)
			if err != nil {
				return nil, err
			}
			return []*dynamic.Message{message}, nil
		}),
		runner.WithTimeout(10 * time.Second),
		runner.WithDialTimeout(10 * time.Second),
		runner.WithKeepalive(30 * time.Second),
		runner.WithProtoFile("proto/raft_service.proto", []string{"proto"}),
		runner.WithStreamDynamicMessages(true),
	}

	if node.TargetQPS > 0 {
		opts = append(opts, runner.WithRPS(uint(node.TargetQPS)))
	}

	report, err := runner.Run(
		"raftservice.RaftService.Propose",
		fmt.Sprintf("%s:%d", node.Host, node.Port),
		opts...,
	)

	if err != nil {
		return nil, fmt.Errorf("ghz运行失败: %v", err)
	}

	return report, nil
}

// ---- TPS 采样提取 ----

type bucket struct {
	success uint64
	failed  uint64
}

func extractPerSecondSamples(reports []*runner.Report, benchStart time.Time) []SecondSample {
	const intervalSec = 0.2
	buckets := make(map[int]*bucket)

	for _, report := range reports {
		if report == nil {
			continue
		}
		for _, d := range report.Details {
			// 用请求发起时间分桶
			startAt := d.Timestamp.Add(-d.Latency)
			elapsed := startAt.Sub(benchStart).Seconds()
			slot := int(elapsed/intervalSec) + 1
			if slot < 1 {
				slot = 1
			}
			b, ok := buckets[slot]
			if !ok {
				b = &bucket{}
				buckets[slot] = b
			}
			if d.Error != "" {
				b.failed++
			} else {
				b.success++
			}
		}
	}

	if len(buckets) == 0 {
		return nil
	}

	maxSlot := 0
	for slot := range buckets {
		if slot > maxSlot {
			maxSlot = slot
		}
	}

	samples := make([]SecondSample, 0, maxSlot)
	var cumSuccess, cumFailed uint64
	for slot := 1; slot <= maxSlot; slot++ {
		b := buckets[slot]
		var s, f uint64
		if b != nil {
			s = b.success
			f = b.failed
		}
		cumSuccess += s
		cumFailed += f
		tps := float64(s) / intervalSec
		samples = append(samples, SecondSample{
			TimeSec:      float64(slot) * intervalSec,
			SuccessInSec: s,
			FailedInSec:  f,
			TPS:          tps,
			CumSuccess:   cumSuccess,
			CumFailed:    cumFailed,
		})
	}
	return samples
}

func extractNodeResults(reports []*runner.Report, config *BenchmarkConfig) []NodeResult {
	results := make([]NodeResult, 0, len(reports))
	for i, report := range reports {
		if report == nil {
			continue
		}
		node := config.Nodes[i]
		var failedReqs uint64
		for _, errCount := range report.ErrorDist {
			failedReqs += uint64(errCount)
		}
		successReqs := report.Count - failedReqs

		var p50, p95, p99 float64
		for _, ld := range report.LatencyDistribution {
			latencyMs := float64(ld.Latency.Microseconds()) / 1000
			switch ld.Percentage {
			case 50:
				p50 = latencyMs
			case 95:
				p95 = latencyMs
			case 99:
				p99 = latencyMs
			}
		}

		results = append(results, NodeResult{
			NodeID:      node.NodeID,
			TotalReqs:   report.Count,
			SuccessReqs: successReqs,
			FailedReqs:  failedReqs,
			TPS:         float64(successReqs) / report.Total.Seconds(),
			AvgLatency:  float64(report.Average.Microseconds()) / 1000,
			P50Latency:  p50,
			P95Latency:  p95,
			P99Latency:  p99,
			Duration:    report.Total.Seconds(),
		})
	}
	return results
}

// ---- 辅助函数 ----

// waitNodesReady 轮询直到所有节点 gRPC 可用或超时。容忍逐 Region 启动与容器拉起延迟。
func waitNodesReady(nodes []NodeConfig, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	for {
		var notReady []int
		for _, node := range nodes {
			if err := verifyNodeConnection(node); err != nil {
				notReady = append(notReady, node.NodeID)
			}
		}
		if len(notReady) == 0 {
			return nil
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("仍有节点未就绪: %v", notReady)
		}
		time.Sleep(1 * time.Second)
	}
}

func verifyNodeConnection(node NodeConfig) error {
	dialCtx, dialCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer dialCancel()
	conn, err := grpc.DialContext(dialCtx, fmt.Sprintf("%s:%d", node.Host, node.Port),
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithBlock(),
	)
	if err != nil {
		return err
	}
	conn.Close()
	return nil
}

func queryAndPrintLeaders(nodes []NodeConfig) {
	regionLeaders := make(map[uint64]LeaderInfo)
	var mu sync.Mutex
	var wg sync.WaitGroup

	for _, node := range nodes {
		wg.Add(1)
		go func(n NodeConfig) {
			defer wg.Done()
			conn, err := grpc.Dial(fmt.Sprintf("%s:%d", n.Host, n.Port),
				grpc.WithTransportCredentials(insecure.NewCredentials()))
			if err != nil {
				return
			}
			defer conn.Close()
			client := pb.NewRaftServiceClient(conn)
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			resp, err := client.GetStatus(ctx, &pb.StatusRequest{RegionId: 0})
			if err != nil {
				return
			}
			mu.Lock()
			for _, region := range resp.Regions {
				if region.IsLeader {
					regionLeaders[region.RegionId] = LeaderInfo{
						RegionID: region.RegionId,
						LeaderID: region.LeaderId,
						NodeID:   n.NodeID,
						NodeHost: n.Host,
						Term:     region.Term,
					}
				}
			}
			mu.Unlock()
		}(node)
	}
	wg.Wait()

	if len(regionLeaders) == 0 {
		fmt.Printf("  ⚠️  未找到任何region leader信息\n")
		return
	}
	regionIDs := make([]uint64, 0, len(regionLeaders))
	for rid := range regionLeaders {
		regionIDs = append(regionIDs, rid)
	}
	sort.Slice(regionIDs, func(i, j int) bool { return regionIDs[i] < regionIDs[j] })
	for _, rid := range regionIDs {
		info := regionLeaders[rid]
		fmt.Printf("  Region %d: Leader=Node-%d (ID=%d, Term=%d)\n",
			rid, info.NodeID, info.LeaderID, info.Term)
	}
}

func printResults(reports []*runner.Report, config *BenchmarkConfig) {
	fmt.Printf("\n╔══════════════════════════════════════════════════════════════╗\n")
	fmt.Printf("║  压测结果报告                                                ║\n")
	fmt.Printf("╚══════════════════════════════════════════════════════════════╝\n\n")

	var totalSuccess, totalErrors uint64
	for i, report := range reports {
		if report == nil {
			continue
		}
		node := config.Nodes[i]
		var failedReqs uint64
		for _, errCount := range report.ErrorDist {
			failedReqs += uint64(errCount)
		}
		success := report.Count - failedReqs
		tps := float64(success) / report.Total.Seconds()
		fmt.Printf("  Node-%d (%d regions):\n", node.NodeID, len(node.Regions))
		fmt.Printf("    总请求数:   %d\n", report.Count)
		fmt.Printf("    成功:       %d\n", success)
		fmt.Printf("    失败:       %d\n", failedReqs)
		fmt.Printf("    TPS:        %.0f\n", tps)
		totalSuccess += success
		totalErrors += failedReqs
	}

	totalTPS := float64(totalSuccess) / config.Duration.Seconds()
	fmt.Printf("\n  总请求数:     %d\n", totalSuccess+totalErrors)
	fmt.Printf("  总TPS:        %.0f\n", totalTPS)
	fmt.Printf("  成功率:       %.2f%%\n", float64(totalSuccess)/float64(totalSuccess+totalErrors)*100)
}

func loadConfig() *BenchmarkConfig {
	nodeConfigsStr := os.Getenv("NODE_CONFIGS")
	if nodeConfigsStr == "" {
		fmt.Println("NODE_CONFIGS 未设置")
		fmt.Println("格式: node_id:host:port:regions:concurrency[:qps];...")
		fmt.Println("例如: 0:127.0.0.1:50051:1,2,3:50:1000;1:127.0.0.1:50052:4,5:50")
		os.Exit(1)
	}

	config := &BenchmarkConfig{
		ProposalSize: getEnvInt("PROPOSAL_SIZE", 128),
		OutputFile:   getEnv("OUTPUT_FILE", "tps_samples.json"),
	}

	durationSec := getEnvInt("DURATION_SECONDS", 60)
	config.Duration = time.Duration(durationSec) * time.Second
	config.SingleNodeMode = getEnv("SINGLE_NODE_MODE", "false") == "true"

	for _, nodeStr := range strings.Split(nodeConfigsStr, ";") {
		nodeStr = strings.TrimSpace(nodeStr)
		if nodeStr == "" {
			continue
		}
		parts := strings.Split(nodeStr, ":")
		if len(parts) < 5 {
			continue
		}
		nodeID, _ := strconv.Atoi(parts[0])
		port, _ := strconv.Atoi(parts[2])
		concurrency, _ := strconv.Atoi(parts[4])

		var regions []uint64
		for _, r := range strings.Split(parts[3], ",") {
			regionID, _ := strconv.ParseUint(strings.TrimSpace(r), 10, 64)
			regions = append(regions, regionID)
		}

		var targetQPS int
		if len(parts) > 5 && parts[5] != "" {
			targetQPS, _ = strconv.Atoi(parts[5])
		}

		config.Nodes = append(config.Nodes, NodeConfig{
			NodeID:      nodeID,
			Host:        parts[1],
			Port:        port,
			Regions:     regions,
			Concurrency: concurrency,
			TargetQPS:   targetQPS,
		})
	}

	sort.Slice(config.Nodes, func(i, j int) bool {
		return config.Nodes[i].NodeID < config.Nodes[j].NodeID
	})

	return config
}

func getEnv(key, defaultValue string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return defaultValue
}

func getEnvInt(key string, defaultValue int) int {
	if v := os.Getenv(key); v != "" {
		if i, err := strconv.Atoi(v); err == nil {
			return i
		}
	}
	return defaultValue
}

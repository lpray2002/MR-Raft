"""
Handover Benchmark 实时驱动器

按预生成的 UAV 轨迹和 handover 序列，实时驱动请求发送，
记录每个请求的详细结果（延迟、成功率、是否在 handover 窗口内）。
"""

import asyncio
import time
import csv
import os
import numpy as np
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .grpc_bridge import MRRaftClient, RequestResult
from .uav_handover import generate_handover_scenario


@dataclass
class ExperimentConfig:
    """实验配置"""
    # 场景参数
    movement_model: str = "gauss_markov"
    uav_speed: float = 20.0
    leader_strategy: str = "balanced"
    duration_s: float = 60.0
    
    # 请求参数
    request_interval_ms: int = 50       # 请求发送间隔
    proposal_size_bytes: int = 64       # 提案数据大小
    
    # Handover 窗口
    handover_window_ms: int = 500       # handover 前后多少 ms 算"窗口内"
    
    # 集群参数
    mode: str = "simulated"             # "simulated" 或 "real"
    node_configs: Dict[int, str] = field(default_factory=dict)
    
    # 随机种子
    seed: int = 42


@dataclass
class RequestRecord:
    """单次请求记录"""
    seq: int                    # 请求序号
    timestamp_ms: float         # 发送时间戳
    node_id: int                # 目标节点
    region_id: int              # 目标 region
    latency_ms: float           # 端到端延迟
    success: bool               # 是否成功
    was_forwarded: bool         # 是否被转发
    is_handover_window: bool    # 是否在 handover 窗口内
    handover_id: int            # 最近的 handover 事件 ID（-1 表示无）
    leader_count_diff: int      # handover 前后 leader 数量差
    error: str = ""


@dataclass
class ExperimentResult:
    """实验结果"""
    config: ExperimentConfig
    records: List[RequestRecord] = field(default_factory=list)
    handover_events: List[Dict] = field(default_factory=list)
    
    # 汇总统计
    total_requests: int = 0
    success_requests: int = 0
    failed_requests: int = 0
    avg_latency_ms: float = 0.0
    p50_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    p99_latency_ms: float = 0.0
    success_rate: float = 0.0
    
    # Handover 相关统计
    handover_window_requests: int = 0
    handover_window_success_rate: float = 0.0
    handover_window_avg_latency_ms: float = 0.0
    stable_avg_latency_ms: float = 0.0
    
    def compute_statistics(self):
        """计算汇总统计"""
        if not self.records:
            return
        
        self.total_requests = len(self.records)
        self.success_requests = sum(1 for r in self.records if r.success)
        self.failed_requests = self.total_requests - self.success_requests
        self.success_rate = self.success_requests / self.total_requests
        
        # 延迟统计（只统计成功的请求）
        success_latencies = [r.latency_ms for r in self.records if r.success]
        if success_latencies:
            self.avg_latency_ms = np.mean(success_latencies)
            self.p50_latency_ms = np.percentile(success_latencies, 50)
            self.p95_latency_ms = np.percentile(success_latencies, 95)
            self.p99_latency_ms = np.percentile(success_latencies, 99)
        
        # Handover 窗口内的统计
        ho_records = [r for r in self.records if r.is_handover_window]
        stable_records = [r for r in self.records if not r.is_handover_window]
        
        self.handover_window_requests = len(ho_records)
        if ho_records:
            ho_success = [r for r in ho_records if r.success]
            self.handover_window_success_rate = len(ho_success) / len(ho_records)
            if ho_success:
                self.handover_window_avg_latency_ms = np.mean(
                    [r.latency_ms for r in ho_success])
        
        if stable_records:
            stable_success = [r for r in stable_records if r.success]
            if stable_success:
                self.stable_avg_latency_ms = np.mean(
                    [r.latency_ms for r in stable_success])


class HandoverBenchmark:
    """
    Handover Benchmark 实时驱动器
    
    工作流程：
    1. 预生成 UAV 轨迹和 handover 事件
    2. 创建 gRPC 客户端（真实或模拟）
    3. 按真实时钟回放轨迹
    4. 每 request_interval_ms 发送一个 Propose 请求
    5. 在 handover 时刻切换目标节点
    6. 记录所有请求结果
    """
    
    def __init__(self, config: ExperimentConfig, spatial_topology,
                 mec_graph, regions: Dict[int, set]):
        """
        Parameters
        ----------
        config : ExperimentConfig
        spatial_topology : SpatialTopology
        mec_graph : nx.Graph
        regions : Dict[int, Set[int]]
        """
        self.config = config
        self.spatial = spatial_topology
        self.graph = mec_graph
        self.regions = regions
        
        # 生成场景
        self.scenario = generate_handover_scenario(
            spatial_topology, mec_graph, regions,
            uav_speed=config.uav_speed,
            duration_s=config.duration_s,
            movement_model=config.movement_model,
            leader_strategy=config.leader_strategy,
            random_seed=config.seed,
        )
        
        # 提取 leader 分布
        self.leader_map = {}
        for node_str, info in self.scenario["leader_distribution"].items():
            node_id = int(node_str)
            self.leader_map[node_id] = info["leader_of"]
        
        # 创建客户端
        self.client = MRRaftClient(
            node_configs=config.node_configs,
            mode=config.mode,
            leader_map=self.leader_map,
            regions=regions,
            seed=config.seed,
        )
    
    async def run(self) -> ExperimentResult:
        """运行实验"""
        await self.client.connect()
        
        try:
            result = await self._execute()
        finally:
            await self.client.close()
        
        return result
    
    async def _execute(self) -> ExperimentResult:
        """执行实验主循环"""
        config = self.config
        trajectory = self.scenario["trajectory"]
        handover_events = self.scenario["handover_events"]
        
        result = ExperimentResult(
            config=config,
            handover_events=handover_events,
        )
        
        # 构建 handover 时间索引
        ho_times = [e["time_ms"] for e in handover_events]
        
        # 确定初始连接节点
        current_node = trajectory[0]["node"] if trajectory else 0
        
        # 计算总请求数
        total_duration_ms = int(config.duration_s * 1000)
        num_requests = total_duration_ms // config.request_interval_ms
        
        # 主循环
        request_seq = 0
        
        for i in range(num_requests):
            current_time_ms = i * config.request_interval_ms
            
            # 检查是否发生 handover（更新当前节点）
            for ho_idx, ho in enumerate(handover_events):
                if (ho["time_ms"] <= current_time_ms and 
                    (ho_idx == 0 or handover_events[ho_idx-1]["time_ms"] < current_time_ms)):
                    current_node = ho["to_node"]
            
            # 也从轨迹中获取当前节点（更精确）
            traj_idx = min(
                current_time_ms // (self.scenario["sample_interval_ms"]),
                len(trajectory) - 1
            )
            current_node = trajectory[traj_idx]["node"]
            
            # 断连状态（不在任何基站覆盖范围内）：记录失败
            if current_node == -1:
                record = RequestRecord(
                    seq=request_seq,
                    timestamp_ms=current_time_ms,
                    node_id=-1,
                    region_id=-1,
                    latency_ms=0,
                    success=False,
                    was_forwarded=False,
                    is_handover_window=False,
                    handover_id=-1,
                    leader_count_diff=0,
                    error="disconnected",
                )
                result.records.append(record)
                request_seq += 1
                continue
            
            # 判断是否在 handover 窗口内
            is_handover_window = False
            nearest_ho_id = -1
            leader_count_diff = 0
            
            for ho_idx, ho in enumerate(handover_events):
                ho_time = ho["time_ms"]
                if abs(current_time_ms - ho_time) <= config.handover_window_ms:
                    is_handover_window = True
                    nearest_ho_id = ho_idx
                    leader_count_diff = abs(
                        ho["to_leaders"] - ho["from_leaders"])
                    break
            
            # 选择目标 region（当前节点参与的第一个 region）
            node_regions = []
            for rid, members in self.regions.items():
                if current_node in members:
                    node_regions.append(rid)
            
            if not node_regions:
                # 节点不在任何 region 中，跳过
                continue
            
            target_region = node_regions[0]
            
            # 发送请求
            req_result = await self.client.propose(
                node_id=current_node,
                region_id=target_region,
                data=os.urandom(config.proposal_size_bytes),
                is_handover_window=is_handover_window,
                leader_count_diff=leader_count_diff,
            )
            
            # 记录结果
            record = RequestRecord(
                seq=request_seq,
                timestamp_ms=current_time_ms,
                node_id=current_node,
                region_id=target_region,
                latency_ms=req_result.latency_ms,
                success=req_result.success,
                was_forwarded=req_result.was_forwarded,
                is_handover_window=is_handover_window,
                handover_id=nearest_ho_id,
                leader_count_diff=leader_count_diff,
                error=req_result.error,
            )
            result.records.append(record)
            request_seq += 1
        
        # 计算统计
        result.compute_statistics()
        
        return result
    
    def run_sync(self) -> ExperimentResult:
        """同步运行（方便在非 async 环境中调用）"""
        return asyncio.run(self.run())


def save_results_csv(result: ExperimentResult, output_path: str):
    """保存实验结果到 CSV"""
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else '.', 
                exist_ok=True)
    
    with open(output_path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([
            'seq', 'timestamp_ms', 'node_id', 'region_id',
            'latency_ms', 'success', 'was_forwarded',
            'is_handover_window', 'handover_id', 'leader_count_diff', 'error'
        ])
        for r in result.records:
            writer.writerow([
                r.seq, r.timestamp_ms, r.node_id, r.region_id,
                f"{r.latency_ms:.3f}", int(r.success), int(r.was_forwarded),
                int(r.is_handover_window), r.handover_id, r.leader_count_diff,
                r.error
            ])


def save_summary(result: ExperimentResult, output_path: str):
    """保存实验汇总"""
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else '.', 
                exist_ok=True)
    
    with open(output_path, 'w') as f:
        c = result.config
        f.write(f"# Handover Benchmark Result\n\n")
        f.write(f"## Configuration\n")
        f.write(f"- Movement model: {c.movement_model}\n")
        f.write(f"- UAV speed: {c.uav_speed} m/s\n")
        f.write(f"- Leader strategy: {c.leader_strategy}\n")
        f.write(f"- Duration: {c.duration_s}s\n")
        f.write(f"- Request interval: {c.request_interval_ms}ms\n")
        f.write(f"- Mode: {c.mode}\n")
        f.write(f"- Seed: {c.seed}\n\n")
        
        f.write(f"## Overall Statistics\n")
        f.write(f"- Total requests: {result.total_requests}\n")
        f.write(f"- Success rate: {result.success_rate:.4f} ({result.success_rate*100:.2f}%)\n")
        f.write(f"- Avg latency: {result.avg_latency_ms:.2f} ms\n")
        f.write(f"- P50 latency: {result.p50_latency_ms:.2f} ms\n")
        f.write(f"- P95 latency: {result.p95_latency_ms:.2f} ms\n")
        f.write(f"- P99 latency: {result.p99_latency_ms:.2f} ms\n\n")
        
        f.write(f"## Handover Impact\n")
        f.write(f"- Handover events: {len(result.handover_events)}\n")
        f.write(f"- Requests in handover window: {result.handover_window_requests}\n")
        f.write(f"- Handover window success rate: {result.handover_window_success_rate:.4f}\n")
        f.write(f"- Handover window avg latency: {result.handover_window_avg_latency_ms:.2f} ms\n")
        f.write(f"- Stable period avg latency: {result.stable_avg_latency_ms:.2f} ms\n")
        f.write(f"- Latency increase during handover: "
                f"{result.handover_window_avg_latency_ms - result.stable_avg_latency_ms:.2f} ms\n")

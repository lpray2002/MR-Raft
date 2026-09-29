"""
MR-Raft gRPC Bridge
支持两种模式：
1. 真实模式 (real): 通过 gRPC 连接 Go MR-Raft 集群
2. 模拟模式 (simulated): 基于真实 benchmark 数据的延迟分布模拟响应

模拟模式不需要 grpcio 依赖，可以在任何环境下运行。
"""

import time
import asyncio
import numpy as np
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from enum import Enum


class NodeRole(Enum):
    LEADER = "leader"
    FOLLOWER = "follower"


@dataclass
class RequestResult:
    """单次请求的结果"""
    success: bool
    latency_ms: float
    error: str = ""
    node_id: int = -1
    region_id: int = -1
    was_forwarded: bool = False  # 是否被转发到 leader
    timestamp_ms: float = 0.0


@dataclass
class RegionStatus:
    """Region 状态"""
    region_id: int
    role: NodeRole
    leader_id: int
    term: int = 0


@dataclass
class NodeStatus:
    """节点状态"""
    node_id: int
    regions: List[RegionStatus] = field(default_factory=list)


class SimulatedLatencyModel:
    """
    基于真实 benchmark 数据的延迟模型
    
    参数来源：Go MR-Raft 集群的实际测量值
    （从 test-full-topology-benchmark.sh 的输出中提取）
    """
    
    def __init__(self, seed: int = 42):
        self.rng = np.random.default_rng(seed)
        
        # 基础共识延迟参数（ms）- 来自真实 benchmark
        self.consensus_base_ms = 2.0       # 基础 Raft 共识延迟
        self.consensus_std_ms = 1.5        # 标准差
        self.consensus_p99_factor = 4.0    # P99 = base * factor
        
        # 转发延迟（follower → leader）
        self.forward_base_ms = 1.0         # 节点间转发延迟
        self.forward_std_ms = 0.5
        
        # Handover 期间的额外延迟
        self.handover_penalty_ms = 5.0     # handover 切换瞬间的额外延迟
        self.handover_window_ms = 200      # handover 影响窗口
        
        # 失败概率
        self.base_failure_rate = 0.001     # 正常情况下的失败率
        self.handover_failure_rate = 0.05  # handover 期间的失败率
    
    def sample_latency(self, is_leader: bool, is_handover_window: bool,
                       leader_count_diff: int = 0) -> Tuple[float, bool]:
        """
        采样一次请求的延迟和成功/失败
        
        Parameters
        ----------
        is_leader : bool
            当前节点是否是目标 region 的 leader
        is_handover_window : bool
            是否在 handover 影响窗口内
        leader_count_diff : int
            handover 前后 leader 数量差异
            
        Returns
        -------
        (latency_ms, success)
        """
        # 基础共识延迟（对数正态分布，模拟长尾）
        latency = self.rng.lognormal(
            np.log(self.consensus_base_ms), 
            self.consensus_std_ms / self.consensus_base_ms
        )
        
        # 如果不是 leader，加转发延迟
        if not is_leader:
            forward_latency = max(0.1, self.rng.normal(
                self.forward_base_ms, self.forward_std_ms))
            latency += forward_latency
        
        # Handover 窗口内的额外延迟
        if is_handover_window:
            penalty = self.handover_penalty_ms * (1 + 0.5 * abs(leader_count_diff))
            latency += self.rng.exponential(penalty)
        
        # 失败判定
        failure_rate = (self.handover_failure_rate if is_handover_window 
                       else self.base_failure_rate)
        # leader 数量差异越大，失败率越高
        if is_handover_window and leader_count_diff > 0:
            failure_rate *= (1 + 0.3 * leader_count_diff)
        
        success = self.rng.random() > failure_rate
        
        # 失败时延迟更高（超时）
        if not success:
            latency = self.rng.uniform(50, 200)  # 超时延迟
        
        return latency, success


class MRRaftClient:
    """
    MR-Raft 集群客户端
    
    支持真实 gRPC 连接和模拟模式
    """
    
    def __init__(self, node_configs: Dict[int, str] = None,
                 mode: str = "simulated",
                 leader_map: Dict[int, List[int]] = None,
                 regions: Dict[int, set] = None,
                 seed: int = 42):
        """
        Parameters
        ----------
        node_configs : Dict[int, str]
            {node_id: "host:port"} 节点地址映射（真实模式需要）
        mode : str
            "real" 或 "simulated"
        leader_map : Dict[int, List[int]]
            {node_id: [region_ids where this node is leader]}（模拟模式需要）
        regions : Dict[int, Set[int]]
            {region_id: set_of_nodes}（模拟模式需要）
        seed : int
            随机种子（模拟模式）
        """
        self.mode = mode
        self.node_configs = node_configs or {}
        self.leader_map = leader_map or {}
        self.regions = regions or {}
        self.seed = seed
        
        # 模拟模式的延迟模型
        if mode == "simulated":
            self.latency_model = SimulatedLatencyModel(seed=seed)
        
        # 真实模式的 gRPC channels
        self._channels = {}
        self._stubs = {}
    
    async def connect(self):
        """建立连接（真实模式）"""
        if self.mode == "real":
            try:
                import grpc
                from .proto import raft_service_pb2 as pb
                from .proto import raft_service_pb2_grpc as pb_grpc
                
                for node_id, addr in self.node_configs.items():
                    channel = grpc.aio.insecure_channel(addr)
                    self._channels[node_id] = channel
                    self._stubs[node_id] = pb_grpc.RaftServiceStub(channel)
            except ImportError:
                raise RuntimeError(
                    "grpcio not installed. Use mode='simulated' or install grpcio:\n"
                    "  conda install -c conda-forge grpcio grpcio-tools"
                )
    
    async def close(self):
        """关闭连接"""
        for channel in self._channels.values():
            await channel.close()
        self._channels.clear()
        self._stubs.clear()
    
    async def propose(self, node_id: int, region_id: int, 
                      data: bytes = b"test",
                      is_handover_window: bool = False,
                      leader_count_diff: int = 0) -> RequestResult:
        """
        发送 Propose 请求
        
        Parameters
        ----------
        node_id : int
            目标节点
        region_id : int
            目标 region
        data : bytes
            提案数据
        is_handover_window : bool
            是否在 handover 窗口内
        leader_count_diff : int
            leader 数量差异
        """
        timestamp = time.time() * 1000
        
        if self.mode == "simulated":
            return await self._propose_simulated(
                node_id, region_id, is_handover_window, leader_count_diff, timestamp)
        else:
            return await self._propose_real(node_id, region_id, data, timestamp)
    
    async def _propose_simulated(self, node_id: int, region_id: int,
                                  is_handover_window: bool,
                                  leader_count_diff: int,
                                  timestamp: float) -> RequestResult:
        """模拟模式的 Propose（不真实等待，直接返回采样延迟）"""
        # 判断当前节点是否是该 region 的 leader
        is_leader = region_id in self.leader_map.get(node_id, [])
        
        # 采样延迟
        latency, success = self.latency_model.sample_latency(
            is_leader=is_leader,
            is_handover_window=is_handover_window,
            leader_count_diff=leader_count_diff
        )
        
        return RequestResult(
            success=success,
            latency_ms=latency,
            error="" if success else "timeout",
            node_id=node_id,
            region_id=region_id,
            was_forwarded=not is_leader,
            timestamp_ms=timestamp,
        )
    
    async def _propose_real(self, node_id: int, region_id: int,
                            data: bytes, timestamp: float) -> RequestResult:
        """真实模式的 Propose"""
        try:
            from .proto import raft_service_pb2 as pb
            
            stub = self._stubs.get(node_id)
            if stub is None:
                return RequestResult(
                    success=False, latency_ms=0,
                    error=f"No connection to node {node_id}",
                    node_id=node_id, region_id=region_id,
                    timestamp_ms=timestamp)
            
            start = time.time()
            request = pb.ProposeRequest(
                region_id=region_id,
                data=data,
                client_id=1,
                request_id=int(timestamp * 1000),
            )
            response = await stub.Propose(request, timeout=5.0)
            latency = (time.time() - start) * 1000
            
            return RequestResult(
                success=response.success,
                latency_ms=latency,
                error=response.error if not response.success else "",
                node_id=node_id,
                region_id=region_id,
                was_forwarded=False,  # 无法从响应中判断
                timestamp_ms=timestamp,
            )
        except Exception as e:
            latency = (time.time() - start) * 1000 if 'start' in dir() else 0
            return RequestResult(
                success=False, latency_ms=latency,
                error=str(e), node_id=node_id, region_id=region_id,
                timestamp_ms=timestamp)
    
    async def get_status(self, node_id: int) -> NodeStatus:
        """获取节点状态"""
        if self.mode == "simulated":
            # 从 leader_map 和 regions 构建状态
            node_regions = []
            for rid, members in self.regions.items():
                if node_id in members:
                    role = (NodeRole.LEADER if rid in self.leader_map.get(node_id, [])
                            else NodeRole.FOLLOWER)
                    leader_id = -1
                    for nid, leader_regions in self.leader_map.items():
                        if rid in leader_regions:
                            leader_id = nid
                            break
                    node_regions.append(RegionStatus(
                        region_id=rid, role=role, leader_id=leader_id))
            return NodeStatus(node_id=node_id, regions=node_regions)
        else:
            # 真实模式 - 调用 GetStatus RPC
            raise NotImplementedError("Real mode GetStatus not yet implemented")
    
    async def transfer_leader(self, node_id: int, region_id: int, 
                               target_node: int) -> bool:
        """请求 leader 转移"""
        if self.mode == "simulated":
            # 模拟 leader 转移
            for nid in list(self.leader_map.keys()):
                if region_id in self.leader_map[nid]:
                    self.leader_map[nid].remove(region_id)
            if target_node not in self.leader_map:
                self.leader_map[target_node] = []
            self.leader_map[target_node].append(region_id)
            return True
        else:
            raise NotImplementedError("Real mode LeaderTransfer not yet implemented")

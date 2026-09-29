"""
UAV 轨迹生成与 Handover 仿真
用于验证 UAV 频繁 handover 时 MR-Raft 的端到端可靠性
"""

import numpy as np
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass, field
import json


@dataclass
class Position:
    """二维位置"""
    x: float
    y: float
    
    def distance_to(self, other: 'Position') -> float:
        return np.sqrt((self.x - other.x)**2 + (self.y - other.y)**2)


@dataclass
class HandoverEvent:
    """Handover 事件"""
    time_ms: int
    from_node: int
    to_node: int
    position: Tuple[float, float]
    from_leaders: int = 0
    to_leaders: int = 0
    to_regions: List[int] = field(default_factory=list)
    to_is_leader_of: List[int] = field(default_factory=list)


@dataclass
class TrajectoryPoint:
    """轨迹点"""
    time_ms: int
    x: float
    y: float
    connected_node: int


class UAVTrajectoryGenerator:
    """
    UAV 轨迹生成器
    
    基于空间拓扑生成 UAV 飞行轨迹，支持三种移动模型：
    1. Linear: 直线匀速飞行
    2. Random Waypoint: 随机选目标点匀速飞行
    3. Predefined Path: 按预定义路径飞行
    """
    
    def __init__(self, spatial_topology, speed_mps: float = 20.0,
                 sample_interval_ms: int = 100, random_seed: int = 42):
        """
        Parameters
        ----------
        spatial_topology : SpatialTopology
            空间拓扑（包含基站位置和覆盖半径）
        speed_mps : float
            UAV 飞行速度 (m/s)
        sample_interval_ms : int
            轨迹采样间隔 (ms)
        random_seed : int
            随机种子
        """
        self.spatial = spatial_topology
        self.speed = speed_mps
        self.sample_interval_ms = sample_interval_ms
        self.area_size = spatial_topology.area_size
        self.rng = np.random.default_rng(random_seed)
        
        # 预计算基站位置和覆盖半径
        self.bs_positions = np.array([
            [bs.x, bs.y] for bs in spatial_topology.base_stations
        ])
        self.bs_radii = np.array([
            bs.coverage_radius for bs in spatial_topology.base_stations
        ])
        self.bs_ids = [bs.id for bs in spatial_topology.base_stations]
    
    def linear(self, start: Tuple[float, float], 
               direction_deg: float, duration_s: float) -> List[TrajectoryPoint]:
        """
        直线飞行
        
        Parameters
        ----------
        start : (x, y)
            起始位置
        direction_deg : float
            飞行方向（角度，0=向右，90=向上）
        duration_s : float
            飞行时长 (秒)
        """
        direction_rad = np.radians(direction_deg)
        vx = self.speed * np.cos(direction_rad)
        vy = self.speed * np.sin(direction_rad)
        
        num_samples = int(duration_s * 1000 / self.sample_interval_ms)
        trajectory = []
        
        for i in range(num_samples):
            t_ms = i * self.sample_interval_ms
            t_s = t_ms / 1000.0
            x = start[0] + vx * t_s
            y = start[1] + vy * t_s
            
            # 边界反弹
            x, y = self._bounce(x, y)
            
            connected = self._find_best_node(x, y)
            trajectory.append(TrajectoryPoint(t_ms, x, y, connected))
        
        return trajectory
    
    def random_waypoint(self, duration_s: float, 
                        pause_s: float = 0.0,
                        start_pos: Tuple[float, float] = None) -> List[TrajectoryPoint]:
        """
        Random Waypoint 模型
        
        随机选目标点，匀速飞过去，到达后暂停 pause_s 秒再选下一个目标
        
        Parameters
        ----------
        start_pos : (x, y), optional
            起始位置。如果不指定，随机选择（在覆盖范围内）
        """
        if start_pos is not None:
            x, y = start_pos
        else:
            # 随机选一个基站位置作为起点（确保在覆盖范围内）
            idx = self.rng.integers(0, len(self.bs_positions))
            x = self.bs_positions[idx, 0] + self.rng.normal(0, 50)
            y = self.bs_positions[idx, 1] + self.rng.normal(0, 50)
        
        num_samples = int(duration_s * 1000 / self.sample_interval_ms)
        trajectory = []
        
        # 选第一个目标（优先选基站附近，确保 UAV 在覆盖区域内活动）
        target_idx = self.rng.integers(0, len(self.bs_positions))
        target_x = self.bs_positions[target_idx, 0] + self.rng.normal(0, 100)
        target_y = self.bs_positions[target_idx, 1] + self.rng.normal(0, 100)
        pause_remaining_ms = 0
        
        for i in range(num_samples):
            t_ms = i * self.sample_interval_ms
            
            if pause_remaining_ms > 0:
                pause_remaining_ms -= self.sample_interval_ms
            else:
                # 计算到目标的距离
                dist = np.sqrt((target_x - x)**2 + (target_y - y)**2)
                step = self.speed * (self.sample_interval_ms / 1000.0)
                
                if dist <= step:
                    # 到达目标
                    x, y = target_x, target_y
                    # 暂停
                    pause_remaining_ms = int(pause_s * 1000)
                    # 选新目标（选一个基站附近的位置）
                    t_idx = self.rng.integers(0, len(self.bs_positions))
                    target_x = self.bs_positions[t_idx, 0] + self.rng.normal(0, 100)
                    target_y = self.bs_positions[t_idx, 1] + self.rng.normal(0, 100)
                else:
                    # 向目标移动
                    dx = (target_x - x) / dist * step
                    dy = (target_y - y) / dist * step
                    x += dx
                    y += dy
            
            connected = self._find_best_node(x, y)
            trajectory.append(TrajectoryPoint(t_ms, x, y, connected))
        
        return trajectory
    
    def predefined_path(self, waypoints: List[Tuple[float, float]], 
                        duration_s: float) -> List[TrajectoryPoint]:
        """
        预定义路径
        
        按 waypoints 序列飞行，到达一个后飞向下一个。
        到达最后一个 waypoint 后停止（不循环）。
        
        Parameters
        ----------
        waypoints : List[(x, y)]
            路径点序列
        duration_s : float
            总飞行时长（如果提前到达终点则停在终点）
        """
        if not waypoints:
            raise ValueError("waypoints cannot be empty")
        
        num_samples = int(duration_s * 1000 / self.sample_interval_ms)
        trajectory = []
        
        x, y = waypoints[0]
        wp_idx = 1  # 当前目标 waypoint
        finished = False  # 是否已到达最后一个 waypoint
        
        for i in range(num_samples):
            t_ms = i * self.sample_interval_ms
            
            if not finished and wp_idx < len(waypoints):
                target_x, target_y = waypoints[wp_idx]
                dist = np.sqrt((target_x - x)**2 + (target_y - y)**2)
                step = self.speed * (self.sample_interval_ms / 1000.0)
                
                if dist <= step:
                    x, y = target_x, target_y
                    wp_idx += 1
                    if wp_idx >= len(waypoints):
                        finished = True
                else:
                    dx = (target_x - x) / dist * step
                    dy = (target_y - y) / dist * step
                    x += dx
                    y += dy
            # else: 已到达终点，停在原地
            
            connected = self._find_best_node(x, y)
            trajectory.append(TrajectoryPoint(t_ms, x, y, connected))
        
        return trajectory
    
    def gauss_markov(self, duration_s: float, alpha: float = 0.85,
                     speed_mean: float = None, 
                     direction_mean_deg: float = None) -> List[TrajectoryPoint]:
        """
        Gauss-Markov 2D 移动模型
        
        速度和方向具有时间相关性，产生平滑的 UAV 飞行轨迹。
        参考 UavNetSim 的 Gauss-Markov 实现，简化为 2D 版本。
        
        Parameters
        ----------
        duration_s : float
            飞行时长 (秒)
        alpha : float
            记忆因子 (0~1)。α→1 趋近直线，α→0 趋近随机游走
        speed_mean : float, optional
            平均速度 (m/s)，默认使用 self.speed
        direction_mean_deg : float, optional
            平均方向 (度)，默认随机
        """
        if speed_mean is None:
            speed_mean = self.speed
        if direction_mean_deg is None:
            direction_mean_deg = self.rng.uniform(0, 360)
        
        direction_mean = np.radians(direction_mean_deg)
        
        num_samples = int(duration_s * 1000 / self.sample_interval_ms)
        trajectory = []
        
        # 起始位置：在某个基站覆盖范围内
        start_bs_idx = self.rng.integers(0, len(self.bs_positions))
        x = self.bs_positions[start_bs_idx, 0] + self.rng.normal(0, 50)
        y = self.bs_positions[start_bs_idx, 1] + self.rng.normal(0, 50)
        
        # 初始速度和方向
        speed = speed_mean
        direction = direction_mean
        
        # 速度和方向的标准差
        speed_std = speed_mean * 0.3
        direction_std = np.radians(30)
        
        for i in range(num_samples):
            t_ms = i * self.sample_interval_ms
            dt = self.sample_interval_ms / 1000.0
            
            # Gauss-Markov 更新公式
            # s(t) = α·s(t-1) + (1-α)·s_mean + sqrt(1-α²)·N(0, σ_s)
            # d(t) = α·d(t-1) + (1-α)·d_mean + sqrt(1-α²)·N(0, σ_d)
            speed_noise = np.sqrt(1 - alpha**2) * self.rng.normal(0, speed_std)
            direction_noise = np.sqrt(1 - alpha**2) * self.rng.normal(0, direction_std)
            
            speed = alpha * speed + (1 - alpha) * speed_mean + speed_noise
            speed = max(1.0, min(speed, speed_mean * 3))  # 限制速度范围
            
            direction = alpha * direction + (1 - alpha) * direction_mean + direction_noise
            
            # 更新位置
            vx = speed * np.cos(direction)
            vy = speed * np.sin(direction)
            x += vx * dt
            y += vy * dt
            
            # 边界反弹
            if x < 0 or x > self.area_size:
                direction = np.pi - direction
                x = np.clip(x, 0, self.area_size)
            if y < 0 or y > self.area_size:
                direction = -direction
                y = np.clip(y, 0, self.area_size)
            
            # 覆盖回归：如果不在任何基站覆盖范围内，方向偏向最近基站
            connected = self._find_best_node(x, y)
            if connected == -1:
                # 找最近基站，调整方向均值朝向它
                dists = np.sqrt(
                    (self.bs_positions[:, 0] - x)**2 +
                    (self.bs_positions[:, 1] - y)**2
                )
                nearest_idx = np.argmin(dists)
                nearest_pos = self.bs_positions[nearest_idx]
                direction_mean = np.arctan2(
                    nearest_pos[1] - y, nearest_pos[0] - x)
            
            trajectory.append(TrajectoryPoint(t_ms, x, y, connected))
        
        return trajectory
    
    def _find_best_node(self, x: float, y: float) -> int:
        """
        找到 UAV 当前位置最佳连接的 MEC 节点
        
        策略：选择覆盖范围内、信号最强（距离最近）的基站
        如果不在任何基站覆盖范围内，返回 -1（断连）
        """
        distances = np.sqrt(
            (self.bs_positions[:, 0] - x)**2 + 
            (self.bs_positions[:, 1] - y)**2
        )
        
        # 在覆盖范围内的基站
        in_coverage = distances <= self.bs_radii
        
        if np.any(in_coverage):
            # 选覆盖范围内最近的
            masked_dist = np.where(in_coverage, distances, np.inf)
            best_idx = np.argmin(masked_dist)
            return self.bs_ids[best_idx]
        else:
            # 不在任何覆盖范围内，断连
            return -1
    
    def _bounce(self, x: float, y: float) -> Tuple[float, float]:
        """边界反弹"""
        while x < 0 or x > self.area_size or y < 0 or y > self.area_size:
            if x < 0:
                x = -x
            if x > self.area_size:
                x = 2 * self.area_size - x
            if y < 0:
                y = -y
            if y > self.area_size:
                y = 2 * self.area_size - y
        return x, y


class HandoverDecider:
    """
    Handover 决策器
    
    从 UAV 轨迹中提取 handover 事件，附加 leader 分布信息
    """
    
    def __init__(self, mec_graph, regions: Dict[int, set],
                 hysteresis_ms: int = 500):
        """
        Parameters
        ----------
        mec_graph : nx.Graph
            MEC 拓扑图
        regions : Dict[int, Set[int]]
            分区结果 {region_id: set_of_nodes}
        hysteresis_ms : int
            滞后时间（防止乒乓切换），ms
        """
        self.graph = mec_graph
        self.regions = regions
        self.hysteresis_ms = hysteresis_ms
        
        # 预计算节点到 region 的映射
        self.node_to_regions = {}
        for rid, nodes in regions.items():
            for n in nodes:
                if n not in self.node_to_regions:
                    self.node_to_regions[n] = []
                self.node_to_regions[n].append(rid)
    
    def extract_handovers(self, trajectory: List[TrajectoryPoint]) -> List[HandoverEvent]:
        """
        从轨迹中提取 handover 事件
        
        加入滞后机制：只有当新节点持续 hysteresis_ms 时间后才确认切换
        断连状态（connected_node == -1）不触发 handover，但会重置 pending
        """
        if not trajectory:
            return []
        
        events = []
        # 找到第一个有效连接的节点作为初始状态
        current_node = trajectory[0].connected_node
        for point in trajectory:
            if point.connected_node != -1:
                current_node = point.connected_node
                break
        
        pending_node = None
        pending_start_ms = 0
        
        for point in trajectory:
            # 断连状态：不触发 handover，重置 pending
            if point.connected_node == -1:
                pending_node = None
                continue
            
            if point.connected_node != current_node:
                if pending_node == point.connected_node:
                    # 持续在新节点覆盖范围内
                    elapsed = point.time_ms - pending_start_ms
                    if elapsed >= self.hysteresis_ms:
                        # 确认 handover
                        events.append(HandoverEvent(
                            time_ms=point.time_ms,
                            from_node=current_node,
                            to_node=pending_node,
                            position=(point.x, point.y),
                        ))
                        current_node = pending_node
                        pending_node = None
                else:
                    # 新的候选节点
                    pending_node = point.connected_node
                    pending_start_ms = point.time_ms
            else:
                # 回到当前节点，取消 pending
                pending_node = None
        
        return events
    
    def enrich_with_leader_info(self, events: List[HandoverEvent],
                                 leader_map: Dict[int, List[int]]) -> List[HandoverEvent]:
        """
        为 handover 事件附加 leader 分布信息
        
        Parameters
        ----------
        leader_map : Dict[int, List[int]]
            {node_id: [region_ids where this node is leader]}
        """
        for event in events:
            from_leaders = leader_map.get(event.from_node, [])
            to_leaders = leader_map.get(event.to_node, [])
            
            event.from_leaders = len(from_leaders)
            event.to_leaders = len(to_leaders)
            event.to_regions = self.node_to_regions.get(event.to_node, [])
            event.to_is_leader_of = to_leaders
        
        return events
    
    def generate_leader_distribution(self, strategy: str = "balanced") -> Dict[int, List[int]]:
        """
        生成 leader 分布
        
        Parameters
        ----------
        strategy : str
            "balanced" - 均匀分布（priority-based election 的结果）
            "moderate" - 中等不均匀
            "skewed" - 严重不均匀
            
        Returns
        -------
        Dict[int, List[int]]
            {node_id: [region_ids where this node is leader]}
        """
        nodes = list(self.graph.nodes())
        region_ids = sorted(self.regions.keys())
        leader_map = {n: [] for n in nodes}
        
        if strategy == "balanced":
            # 轮询分配：每个 region 的 leader 尽量均匀分布
            node_idx = 0
            for rid in region_ids:
                members = sorted(self.regions[rid])
                # 选 leader 数最少的成员
                best = min(members, key=lambda n: len(leader_map[n]))
                leader_map[best].append(rid)
                
        elif strategy == "moderate":
            # 中等不均：前 1/3 节点承担 60% 的 leader
            heavy_nodes = nodes[:max(1, len(nodes) // 3)]
            light_nodes = nodes[max(1, len(nodes) // 3):]
            
            for i, rid in enumerate(region_ids):
                members = sorted(self.regions[rid])
                # 60% 概率分配给 heavy 节点
                heavy_members = [n for n in members if n in heavy_nodes]
                light_members = [n for n in members if n in light_nodes]
                
                if heavy_members and (not light_members or i % 5 < 3):
                    chosen = heavy_members[i % len(heavy_members)]
                elif light_members:
                    chosen = light_members[i % len(light_members)]
                else:
                    chosen = members[0]
                leader_map[chosen].append(rid)
                
        elif strategy == "skewed":
            # 严重不均：尽量集中到少数节点
            for rid in region_ids:
                members = sorted(self.regions[rid])
                # 总是选第一个成员（导致某些节点承担大量 leader）
                leader_map[members[0]].append(rid)
        
        return leader_map


def generate_handover_scenario(spatial_topology, mec_graph, regions,
                                uav_speed: float, duration_s: float,
                                movement_model: str = "linear",
                                leader_strategy: str = "balanced",
                                random_seed: int = 42) -> Dict:
    """
    生成完整的 handover 仿真场景
    
    Returns
    -------
    dict : 完整场景数据（可序列化为 JSON）
    """
    # 生成轨迹
    gen = UAVTrajectoryGenerator(spatial_topology, speed_mps=uav_speed,
                                  random_seed=random_seed)
    
    # 预计算基站位置，用于选择合理的起点
    bs_positions = np.array([[bs.x, bs.y] for bs in spatial_topology.base_stations])
    bs_ids = [bs.id for bs in spatial_topology.base_stations]
    
    # 选择一个基站作为起点（选度数最高的，确保在网络中心区域）
    rng = np.random.default_rng(random_seed)
    # 选择靠近区域中心的基站
    center = np.array([spatial_topology.area_size / 2, spatial_topology.area_size / 2])
    dists_to_center = np.linalg.norm(bs_positions - center, axis=1)
    start_bs_idx = np.argmin(dists_to_center)
    start_pos = (bs_positions[start_bs_idx, 0], bs_positions[start_bs_idx, 1])
    
    if movement_model == "linear":
        # 沿基站链飞行：从起点基站出发，依次飞向相邻基站
        # 构建一条穿越多个基站的路径
        dists_from_start = np.linalg.norm(
            bs_positions - np.array(start_pos), axis=1)
        sorted_idx = np.argsort(dists_from_start)
        # 取最近的 5 个基站作为 waypoints
        waypoints = [start_pos]
        for idx in sorted_idx[1:min(6, len(sorted_idx))]:
            waypoints.append((bs_positions[idx, 0], bs_positions[idx, 1]))
        trajectory = gen.predefined_path(waypoints, duration_s=duration_s)
        
    elif movement_model == "rwp":
        trajectory = gen.random_waypoint(duration_s=duration_s, pause_s=0.5,
                                          start_pos=start_pos)
        
    elif movement_model == "gauss_markov":
        # Gauss-Markov：起点在基站覆盖内，方向朝向基站密集区中心
        bs_center = bs_positions.mean(axis=0)
        direction_to_center = np.degrees(np.arctan2(
            bs_center[1] - start_pos[1], bs_center[0] - start_pos[0]))
        trajectory = gen.gauss_markov(duration_s=duration_s, alpha=0.85,
                                       direction_mean_deg=direction_to_center)
        
    elif movement_model == "predefined":
        # 预定义路径：穿越多个基站（选择分散的基站作为 waypoints）
        # 按距离中心从近到远排序，选取分散的基站
        dists_to_center = np.linalg.norm(bs_positions - center, axis=1)
        sorted_idx = np.argsort(dists_to_center)
        # 选 5-6 个分散的基站作为路径点
        selected = []
        for idx in sorted_idx:
            pos = (bs_positions[idx, 0], bs_positions[idx, 1])
            # 确保与已选点有一定距离
            if not selected or all(
                np.sqrt((pos[0]-s[0])**2 + (pos[1]-s[1])**2) > 200 
                for s in selected):
                selected.append(pos)
            if len(selected) >= 5:
                break
        selected.append(selected[0])  # 回到起点形成环路
        trajectory = gen.predefined_path(selected, duration_s=duration_s)
    else:
        raise ValueError(f"Unknown movement model: {movement_model}")
    
    # 提取 handover 事件
    decider = HandoverDecider(mec_graph, regions, hysteresis_ms=300)
    events = decider.extract_handovers(trajectory)
    
    # 生成 leader 分布并附加信息
    leader_map = decider.generate_leader_distribution(leader_strategy)
    events = decider.enrich_with_leader_info(events, leader_map)
    
    # 构建输出
    scenario = {
        "scenario": f"{movement_model}_speed_{int(uav_speed)}_{leader_strategy}",
        "movement_model": movement_model,
        "uav_speed_mps": uav_speed,
        "leader_strategy": leader_strategy,
        "total_duration_s": duration_s,
        "sample_interval_ms": gen.sample_interval_ms,
        "num_trajectory_points": len(trajectory),
        "num_handovers": len(events),
        "handover_frequency_per_min": len(events) / (duration_s / 60) if duration_s > 0 else 0,
        "handover_events": [
            {
                "time_ms": e.time_ms,
                "from_node": e.from_node,
                "to_node": e.to_node,
                "position": e.position,
                "from_leaders": e.from_leaders,
                "to_leaders": e.to_leaders,
                "to_regions": e.to_regions,
                "to_is_leader_of": e.to_is_leader_of,
            }
            for e in events
        ],
        "leader_distribution": {
            str(node): {
                "regions": decider.node_to_regions.get(node, []),
                "leader_of": leader_map.get(node, []),
                "num_leaders": len(leader_map.get(node, [])),
            }
            for node in sorted(mec_graph.nodes())
        },
        "trajectory": [
            {"time_ms": p.time_ms, "x": p.x, "y": p.y, "node": p.connected_node}
            for p in trajectory
        ],
    }
    
    return scenario


def save_scenario(scenario: Dict, output_path: str):
    """保存场景到 JSON 文件"""
    with open(output_path, 'w') as f:
        json.dump(scenario, f, indent=2)

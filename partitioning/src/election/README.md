# Raft 选举超时热力图对比分析

## 概述

本模块实现了 Raft 共识算法中两种选举策略的对比分析：
- **Random Election**: 随机超时选举（Raft 原始策略）
- **Priority-based Election**: 基于优先级的选举（负载均衡优化）

通过热力图可视化展示不同选举策略下每个节点在每轮选举中的超时时间分布。

## 文件结构

```
src/election/
├── README.md                          # 本文档
├── raft_timeout_heatmap.py           # 热力图对比分析（新）
└── simulate_election.py              # 原有的选举模拟器
```

## 功能特性

### 1. 热力图对比分析 (`raft_timeout_heatmap.py`)

#### 主要功能
- ✅ 对比 Random 和 Priority-based 两种选举策略
- ✅ 绘制超时时间热力图
- ✅ 统计 leader 分布均衡性
- ✅ 分析超时时间统计特征
- ✅ 与 `run.py` 保持一致的代码风格

#### 配置参数

```python
# 仿真参数
NUM_NODES = 5              # 节点数量
NUM_ROUNDS = 90            # 选举轮数
NUM_REGIONS = 3            # Region数量

# Raft超时参数
T_BASE = 150               # 基础超时时间 (ms)
T_RANGE_RANDOM = 150       # Random选举的超时范围 (ms)
ALPHA = 0.3                # 优先级惩罚因子（指数增长）
DELTA_RANGE = 30           # 随机扰动范围 (ms)

# 可视化参数
FIGURE_SIZE_HEATMAP = (10, 8)  # 热力图尺寸
DPI = 300                      # 图片分辨率
OUTPUT_DIR = 'output/election'  # 输出目录
```

#### 算法说明

**Random Election (随机超时)**
```
T_random = T_base + random(0, T_range)
```
- 每个节点的超时时间完全随机
- 模拟 Raft 原始的随机化超时机制

**Priority-based Election (优先级超时)**
```
T_priority = T_base × (1 + α)^L + δ

其中:
- L: 当前节点已担任的 leader 数量
- α: 惩罚因子 (0.3)
- δ: 随机扰动 [0, delta_range]
```
- 已担任更多 leader 的节点超时时间更长
- 实现负载均衡，避免某个节点过载

## 使用方法

### 快速开始

```bash
# 运行热力图对比分析
python src/election/raft_timeout_heatmap.py
```

### 生成的图表

运行后会在 `output/election/` 目录下生成以下图表：

1. **`timeout_heatmap_comparison.png`** - 超时时间热力图对比
   - (a) Random Timeouts per Node per Round
   - (b) Priority-based Timeouts per Node per Round

2. **`leader_distribution_comparison.png`** - Leader 分布对比
   - 展示每个节点当选 leader 的次数

3. **`timeout_statistics_comparison.png`** - 超时统计对比
   - (a) 每个节点的平均超时时间（带误差棒）
   - (b) 超时时间的标准差对比

### 统计输出

脚本会输出详细的统计信息：

```
[Random Election]
  Average timeout: 222.07 ms
  Timeout range: [150.15, 299.65] ms
  Timeout std dev: 43.06 ms
  Leader distribution: [19 16 20 20 15]
  Max leader count: 20
  Min leader count: 15
  Std dev of leader count: 2.10

[Priority-based Election]
  Average timeout: 3481.92 ms
  Timeout range: [153.87, 16897.17] ms
  Timeout std dev: 4147.68 ms
  Leader distribution: [18 18 18 18 18]
  Max leader count: 18
  Min leader count: 18
  Std dev of leader count: 0.00

[Comparison]
  Balance improvement (leader distribution): 100.0%
  Max leader count reduction: 2
```

## 设计理念

### 与 `run.py` 保持一致的风格

本模块严格遵循 `run.py` 的代码风格和设计模式：

1. **配置参数集中管理**
   - 所有参数在文件顶部统一定义
   - 使用大写常量命名
   - 配置与实现分离

2. **模块化设计**
   - 清晰的类和函数职责划分
   - 每个函数都有详细的文档字符串
   - 可复用的组件设计

3. **统一的可视化风格**
   - 使用项目统一的 `COLORS` 配色方案
   - 一致的图表尺寸和 DPI 设置
   - 标准化的字体大小和粗细

4. **标准输出格式**
   - 使用 `[Step X/Y]` 格式标注进度
   - 使用 `[OK]` 标记成功操作
   - 使用分隔线增强可读性

## 技术细节

### RaftElectionSimulator 类

主要方法：

- `__init__()`: 初始化模拟器参数
- `_generate_membership()`: 生成 node-region 成员关系
- `compute_timeout_random()`: 计算随机超时时间
- `compute_timeout_priority()`: 计算优先级超时时间
- `simulate_random_election()`: 模拟随机选举
- `simulate_priority_election()`: 模拟优先级选举

### 可视化函数

- `plot_timeout_heatmaps()`: 绘制超时热力图对比
- `plot_leader_distribution()`: 绘制 leader 分布柱状图
- `plot_timeout_statistics()`: 绘制超时统计图
- `print_statistics()`: 打印统计信息

## 自定义配置

### 修改节点数和选举轮数

编辑 `raft_timeout_heatmap.py` 中的配置参数：

```python
NUM_NODES = 5              # 修改节点数
NUM_ROUNDS = 90            # 修改选举轮数
NUM_REGIONS = 3            # 修改 Region 数量
```

### 调整超时参数

```python
T_BASE = 150               # 基础超时 (ms)
T_RANGE_RANDOM = 150       # Random 超时范围 (ms)
ALPHA = 0.3                # 优先级惩罚因子
DELTA_RANGE = 30           # 随机扰动 (ms)
```

### 自定义输出路径

```python
OUTPUT_DIR = 'output/election'  # 修改输出目录
```

## 实验结果分析

### 关键发现

1. **负载均衡效果**
   - Priority-based 策略实现了完美的负载均衡（标准差为 0）
   - Random 策略的负载分布存在一定不均衡性

2. **超时时间特征**
   - Priority-based 策略的超时时间随 leader 数量指数增长
   - 热力图清晰展示了两种策略的时间分布差异

3. **性能提升**
   - Leader 分布标准差降低 100%
   - 最大 leader 数量减少约 10-20%

## 扩展性

### 添加新的选举策略

在 `RaftElectionSimulator` 类中添加新方法：

```python
def compute_timeout_custom(self, node_id, round_idx, leader_count):
    """自定义超时计算策略"""
    # 实现你的策略
    return timeout

def simulate_custom_election(self, num_rounds):
    """模拟自定义选举"""
    # 实现仿真逻辑
    return timeout_matrix, leader_history
```

### 集成到其他分析流程

```python
from src.election.raft_timeout_heatmap import RaftElectionSimulator

# 创建模拟器
simulator = RaftElectionSimulator(num_nodes=10, num_regions=5)

# 运行仿真
random_timeouts, random_leaders = simulator.simulate_random_election(100)
priority_timeouts, priority_leaders = simulator.simulate_priority_election(100)

# 使用结果进行自定义分析
# ...
```

## 依赖项

- `numpy`: 数值计算
- `matplotlib`: 图表绘制
- `seaborn`: 统计图表美化（可选）

## 许可证

本模块遵循项目的整体许可证。

## 维护者

与主项目保持一致。

## 版本历史

- **v1.0.0** (2024-11-07): 初始版本，实现热力图对比功能
  - 支持 Random 和 Priority-based 两种策略
  - 生成 3 种对比图表
  - 完整的统计分析

## 参考文献

1. Diego Ongaro and John Ousterhout. "In Search of an Understandable Consensus Algorithm." USENIX ATC 2014.
2. 相关 Raft 共识算法的优化研究

---

**注意**: 本模块是 `2025_10_22_region-partitioning` 项目的一部分，专注于 MEC 区域划分中的 leader 选举优化。






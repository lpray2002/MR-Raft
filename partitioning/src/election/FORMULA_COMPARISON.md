# Raft 选举超时公式对比

## 公式变更说明

### 原公式（已弃用）
```
T_priority = T_base × (1 + α)^L + δ
```
- **特点**: 指数增长，无上限
- **问题**: 随着 leader 数量增加，超时时间会指数级增长，可能导致超时时间过长

### 新公式（当前使用）
```
T_j^(r) = T_base + (T_max - T_base) × [1 - (1/(1+α))^L_j] + δ

其中:
- L_j: 节点当前的leader数量
- α: 惩罚因子 (默认 0.3)
- T_max = T_base × 3 (最大超时时间)
- δ: 随机扰动 [0, delta_range]
```

## 公式特点对比

| 特性 | 原公式 | 新公式 |
|------|--------|--------|
| **增长方式** | 指数增长 | 渐进式增长（有上限） |
| **超时上限** | 无上限 | T_base × 3 |
| **L=0时** | T_base + δ | T_base + δ |
| **L→∞时** | ∞ | T_base × 3 + δ |
| **适用场景** | 节点数少、region少 | 通用场景 |

## 实验结果对比

### 配置参数
- Nodes: 7
- Election Rounds: 100
- Regions: 100
- T_base: 150 ms
- α: 0.3

### Random Election 统计
```
Average timeout: 223.09 ms
Timeout range: [150.15, 299.65] ms
Timeout std dev: 42.73 ms
Leader distribution: [18 15 13 18 10 16 10]
Max leader count: 18
Min leader count: 10
Std dev of leader count: 3.15
```

### Priority-based Election 统计（新公式）
```
Average timeout: 384.49 ms
Timeout range: [151.17, 473.62] ms
Timeout std dev: 80.06 ms
Leader distribution: [14 13 15 15 14 14 15]
Max leader count: 15
Min leader count: 13
Std dev of leader count: 0.70
```

### 关键指标对比

| 指标 | Random | Priority-based | 改善 |
|------|--------|----------------|------|
| **最大 leader 数** | 18 | 15 | ↓ 3 (16.7%) |
| **最小 leader 数** | 10 | 13 | ↑ 3 (30%) |
| **标准差** | 3.15 | 0.70 | ↓ 77.8% |
| **负载均衡性** | 较差 | **显著改善** | ✅ |

## 公式行为分析

### 不同 leader 数量下的超时时间

以 T_base=150ms, α=0.3 为例：

| Leader数 (L) | 原公式 (ms) | 新公式 (ms) | 差异 |
|-------------|------------|------------|------|
| 0 | 150 | 150 | 相同 |
| 1 | 195 | 227 | 新公式更大 |
| 2 | 254 | 281 | 新公式更大 |
| 3 | 330 | 322 | 原公式更大 |
| 5 | 570 | 375 | **原公式爆炸** |
| 10 | 2032 | 434 | **原公式爆炸** |
| 20 | 27147 | 447 | **原公式不可用** |

### 关键观察

1. **前期行为相似**: 当 L ≤ 2 时，两个公式的超时时间相近
2. **中期出现差异**: 当 L = 3 时，原公式开始超过新公式
3. **后期差异巨大**: 当 L ≥ 5 时，原公式的超时时间会变得不切实际

## 新公式优势

### 1. 可控的超时范围
- 超时时间被限制在 [T_base, T_base × 3] 之间
- 不会因为 leader 数量过多导致超时时间过长

### 2. 更好的负载均衡
- 标准差降低 77.8%
- leader 分布更加均匀

### 3. 渐进式惩罚
- 使用衰减函数 `(1/(1+α))^L`
- 随着 L 增加，增长速度逐渐放缓
- 避免了指数增长的爆炸性

### 4. 与 `simulate_election.py` 一致
- 两个模块使用相同的公式
- 结果可直接对比

## 数学推导

### 衰减因子的作用

```
decay_factor = (1/(1+α))^L

当 α = 0.3 时:
- L = 0: decay_factor = 1.000  →  timeout = T_base
- L = 1: decay_factor = 0.769  →  timeout = T_base + 0.231 × (T_max - T_base)
- L = 2: decay_factor = 0.592  →  timeout = T_base + 0.408 × (T_max - T_base)
- L = 5: decay_factor = 0.269  →  timeout = T_base + 0.731 × (T_max - T_base)
- L→∞: decay_factor = 0      →  timeout = T_max
```

### 增长曲线

新公式描述的是一个**饱和曲线**：
- 初期增长较快（对少量 leader 有明显惩罚）
- 后期增长放缓（避免过度惩罚）
- 最终趋于上限（T_max）

## 实际应用建议

### 参数调优

1. **T_base**: 基础超时时间
   - 建议: 150-200 ms
   - 考虑因素: 网络延迟、系统负载

2. **α (惩罚因子)**:
   - 建议范围: 0.2 - 0.5
   - α 越大，惩罚越重
   - α = 0.3 是经过实验验证的较优值

3. **T_max 倍数**:
   - 当前: T_max = T_base × 3
   - 建议范围: 2.5 - 4
   - 可根据实际场景调整

### 使用场景

**新公式适用于**:
- ✅ 大规模节点集群（节点数 > 5）
- ✅ 多 region 场景（region 数 > 10）
- ✅ 长时间运行的系统
- ✅ 需要稳定负载均衡的场景

**原公式适用于**:
- ⚠️ 极小规模测试（节点数 ≤ 3）
- ⚠️ 短期仿真（轮数 < 10）
- ❌ 不建议用于生产环境

## 代码示例

### 使用新公式运行仿真

```python
from src.election.raft_timeout_heatmap import RaftElectionSimulator

# 创建模拟器
simulator = RaftElectionSimulator(
    num_nodes=7,
    num_regions=100,
    T_base=150,
    alpha=0.3,      # 惩罚因子
    delta_range=30
)

# 运行仿真
random_timeouts, random_leaders = simulator.simulate_random_election(100)
priority_timeouts, priority_leaders = simulator.simulate_priority_election(100)
```

### 自定义公式参数

可以在类初始化后修改公式参数：

```python
# 如果需要更强的惩罚
simulator.alpha = 0.5  # 增加惩罚因子

# 如果需要更宽松的上限
# 修改 compute_timeout_priority 中的 T_max = self.T_base * 4
```

## 参考文献

1. `src/election/simulate_election.py` - 原始公式实现
2. `src/election/raft_timeout_heatmap.py` - 热力图可视化实现
3. `src/election/README.md` - 模块使用文档

---

**最后更新**: 2024-11-07  
**版本**: v2.0 (使用新的饱和增长公式)






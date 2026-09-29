#!/usr/bin/env bash
# 性能感知优先级选举实验编排脚本
# 5 节点异构 Docker（bridge 网络）× 3 策略 × 重复，逐 Region 启动。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# ---------------- 可调参数 ----------------
STRATEGIES=("random" "leader_count" "perf_no_static" "perf_load_only" "perf_no_leader" "perf_aware")
# 允许通过环境变量 STRATEGIES_OVERRIDE="perf_aware" 或 CLI --strategies 限定策略子集
if [[ -n "${STRATEGIES_OVERRIDE:-}" ]]; then
  read -r -a STRATEGIES <<< "$STRATEGIES_OVERRIDE"
fi
REPEAT="${REPEAT:-3}"
NUM_REGIONS="${NUM_REGIONS:-5}"            # Region 数量（每个 Region 含全部节点）
NUM_NODES="${NUM_NODES:-5}"                # 节点数量
REGION_INTERVAL="${REGION_INTERVAL:-15}"   # 逐 Region 启动间隔（秒）
DURATION="${DURATION:-140}"                # 客户端压测时长（秒）
CONCURRENCY="${CONCURRENCY:-20}"           # 每节点并发
OUTPUT_BASE="${OUTPUT_BASE:-$PROJECT_DIR/output/priority_election}"
IMAGE="multi-region-raft/priority-election:latest"
NET="pe-net"
RESOURCE_CONFIG="${RESOURCE_CONFIG:-A}"    # A | B | both

# 选举/惩罚参数
ELECTION_RTT="${ELECTION_RTT:-10}"
PENALTY_SCALE="${PENALTY_SCALE:-40}"
ALPHA="${ALPHA:-0.3}"
W1="${W1:-0.5}"
W2="${W2:-0.5}"
KAPPA="${KAPPA:-10.0}"
GAMMA="${GAMMA:-2.0}"
NET_BASELINE_BPS="${NET_BASELINE_BPS:-5e7}"
BETA1="${BETA1:-0.8}"; BETA2="${BETA2:-0.1}"; BETA3="${BETA3:-0.1}"
CHECK_QUORUM="${CHECK_QUORUM:-true}"

# ---------------- 拓扑常量 ----------------
# 节点列表由 NUM_NODES 动态生成（在 CLI 解析后 build_topology 前执行）
CPUS=(); MEMS=()   # 由 run_config 填充

# 节点 → 参与 Region / Region → 成员节点
declare -A NODE_REGIONS
declare -A REGION_NODES
declare -A NODE_REGIONS
declare -A REGION_NODES

build_topology() {
  local region_list=""
  for r in $(seq 1 "$NUM_REGIONS"); do
    region_list+="${r},"
  done
  region_list="${region_list%,}"
  for nid in "${NODE_IDS[@]}"; do
    NODE_REGIONS[$nid]="$region_list"
  done
  for r in $(seq 1 "$NUM_REGIONS"); do
    REGION_NODES[$r]="${NODE_IDS[*]}"
  done
}

CPUS=(); MEMS=()   # 由 run_config 填充

# ---------------- CLI 解析 ----------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repeat) REPEAT="$2"; shift 2 ;;
    --num-regions) NUM_REGIONS="$2"; shift 2 ;;
    --num-nodes) NUM_NODES="$2"; shift 2 ;;
    --interval) REGION_INTERVAL="$2"; shift 2 ;;
    --duration) DURATION="$2"; shift 2 ;;
    --concurrency) CONCURRENCY="$2"; shift 2 ;;
    --config) RESOURCE_CONFIG="$2"; shift 2 ;;
    --strategies) read -r -a STRATEGIES <<< "$2"; shift 2 ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
done

# 根据 NUM_NODES 动态生成节点列表和资源数组
NODE_IDS=($(seq 0 $((NUM_NODES - 1))))
# Config A 异构对抗：id 偶数=1核(弱)，id 奇数=2核(强)
CPUS_A=(); MEMS_A=()
for i in $(seq 0 $((NUM_NODES - 1))); do
  if (( i % 2 == 0 )); then CPUS_A+=("1.0"); MEMS_A+=("1g")
  else CPUS_A+=("2.0"); MEMS_A+=("1.5g"); fi
done
# Config B 纯结构（全 1.0 核）
CPUS_B=(); MEMS_B=()
for i in $(seq 1 "$NUM_NODES"); do
  CPUS_B+=("1.0"); MEMS_B+=("1g")
done

# 根据 NUM_REGIONS 生成拓扑
build_topology
echo "🗺️  拓扑：${#NODE_IDS[@]} 节点 × ${NUM_REGIONS} Region（每 Region 含全部节点）"

# ---------------- 工具函数 ----------------
# 构造某 Region 的成员环境变量值：raftNodeID:容器名:30000,...
build_members() {
  local region="$1"
  local out=""
  for nid in ${REGION_NODES[$region]}; do
    local raft_id=$(( region * 100 + nid ))
    out+="${raft_id}:pe-node-${nid}:30000,"
  done
  echo "${out%,}"   # 去掉末尾逗号
}

cleanup_containers() {
  for nid in "${NODE_IDS[@]}"; do
    docker rm -f "pe-node-${nid}" >/dev/null 2>&1 || true
  done
  # 等待容器真正消失（docker rm -f 是异步的，端口/卷释放需要时间）
  for _ in $(seq 1 30); do
    if [[ -z "$(docker ps -aq --filter "name=pe-node-" 2>/dev/null)" ]]; then
      break
    fi
    sleep 0.5
  done
  # Docker Desktop for Windows 端口释放滞后于容器删除，额外等待避免 -p bind 失败
  sleep 3
}
trap cleanup_containers EXIT

# 单次运行：启动 5 容器 + 客户端压测 + 收集
run_once() {
  local strategy="$1" run_dir="$2"
  # perf_no_static 在容器内使用 perf_aware 策略但 gamma=0（无静态项）
  local actual_strategy="$strategy"
  local actual_gamma="$GAMMA"
  if [[ "$strategy" == "perf_no_static" ]]; then
    actual_strategy="perf_aware"
    actual_gamma="0.0"
  fi
  # 不删 $run_dir 自身（Docker Desktop Windows 绑定挂载锁定后无法重建），只清内容
  mkdir -p "$run_dir"
  find "$run_dir" -mindepth 1 -delete 2>/dev/null || true
  cleanup_containers
  docker network create "$NET" >/dev/null 2>&1 || true

  local base_ts=$(( $(date +%s) + 10 ))   # 10s 后开始逐 Region 启动

  echo "  ▶ 启动 ${#NODE_IDS[@]} 节点容器 (strategy=$strategy)..."
  # 计算当前配置的最大算力（用于静态性能惩罚归一）
  local cmax=0
  for c in "${CPUS[@]}"; do
    if awk "BEGIN{exit !($c>$cmax)}"; then cmax=$c; fi
  done
  for nid in "${NODE_IDS[@]}"; do
    local grpc_host_port=$(( 50051 + nid ))
    # 静态性能惩罚 s=(Cmax-Cj)/Cmax ∈[0,1]，越弱越大；解决 R1 零负载盲选
    local static_pen
    static_pen=$(awk "BEGIN{printf \"%.4f\", ($cmax-${CPUS[$nid]})/$cmax}")
    # 该节点参与的每个 Region 的成员配置
    local member_envs=()
    IFS=',' read -ra regs <<< "${NODE_REGIONS[$nid]}"
    for r in "${regs[@]}"; do
      member_envs+=( -e "CLUSTER_MEMBERS_${r}=$(build_members "$r")" )
    done

    docker run -d --name "pe-node-${nid}" --network "$NET" \
      --cpus="${CPUS[$nid]}" --memory="${MEMS[$nid]}" \
      -p "${grpc_host_port}:50051" \
      -e MEC_NODE_ID="$nid" \
      -e MEC_NODE_NAME="pe-node-${nid}" \
      -e MEC_NODE_ADDRESS="pe-node-${nid}:30000" \
      -e GRPC_PORT=50051 \
      -e MEC_NODE_REGIONS="${NODE_REGIONS[$nid]}" \
      -e USE_MEMORY_SM=true \
      -e ELECTION_STRATEGY="$actual_strategy" \
      -e SEQUENTIAL_START=true \
      -e REGION_START_BASE_TS="$base_ts" \
      -e REGION_START_INTERVAL="$REGION_INTERVAL" \
      -e ELECTION_RTT="$ELECTION_RTT" \
      -e PENALTY_SCALE="$PENALTY_SCALE" \
      -e ELECTION_ALPHA="$ALPHA" \
      -e ELECTION_W1="$W1" -e ELECTION_W2="$W2" \
      -e ELECTION_KAPPA="$KAPPA" \
      -e ELECTION_GAMMA="$actual_gamma" \
      -e STATIC_PENALTY="$static_pen" \
      -e CPU_QUOTA="${CPUS[$nid]}" \
      -e NET_BASELINE_BPS="$NET_BASELINE_BPS" \
      -e BETA1="$BETA1" -e BETA2="$BETA2" -e BETA3="$BETA3" \
      -e CHECK_QUORUM="$CHECK_QUORUM" \
      -e METRICS_FILE="node_${nid}_metrics.jsonl" \
      -e ELECTION_EVENTS_FILE="node_${nid}_events.jsonl" \
      "${member_envs[@]}" \
      -v "$run_dir":/data \
      "$IMAGE" >/dev/null
  done

  # 构造客户端 NODE_CONFIGS（host 通过映射端口连接）
  local node_configs=""
  for nid in "${NODE_IDS[@]}"; do
    local grpc_host_port=$(( 50051 + nid ))
    node_configs+="${nid}:127.0.0.1:${grpc_host_port}:${NODE_REGIONS[$nid]}:${CONCURRENCY};"
  done
  node_configs="${node_configs%;}"

  # 等待至少一个节点 gRPC 就绪（gRPC 先于 region 启动，客户端可以立即开始压测）
  echo "  ▶ 等待节点 gRPC 就绪..."
  for nid in "${NODE_IDS[@]}"; do
    local port=$(( 50051 + nid ))
    for i in $(seq 1 30); do
      if timeout 2 bash -c "echo >/dev/tcp/127.0.0.1/$port" 2>/dev/null; then break; fi
      sleep 1
    done
  done
  echo "  ▶ 所有节点 gRPC 已就绪，启动客户端压测 ${DURATION}s..."
  NODE_CONFIGS="$node_configs" \
  DURATION_SECONDS="$DURATION" \
  PROPOSAL_SIZE=128 \
  OUTPUT_FILE="$run_dir/tps_samples.json" \
    "$PROJECT_DIR/bin/priority-election-client" || true

  # 收集 leader 分配（从各节点 metrics 末行的 leader_of）
  # 先通过 docker cp 拉取容器内文件，避免 Docker Desktop Windows 绑定挂载同步延迟丢数据
  echo "  ▶ 拉取节点数据文件..."
  for nid in "${NODE_IDS[@]}"; do
    for retry in 1 2 3; do
      err=$(docker cp "pe-node-${nid}:/data/node_${nid}_metrics.jsonl" "$run_dir/" 2>&1) && break
      echo "    [retry $retry] node_${nid}_metrics: $err"
      sleep 2
    done
    for retry in 1 2 3; do
      err=$(docker cp "pe-node-${nid}:/data/node_${nid}_events.jsonl" "$run_dir/" 2>&1) && break
      echo "    [retry $retry] node_${nid}_events: $err"
      sleep 2
    done
  done
  collect_leaders "$run_dir"

  echo "  ▶ 停止容器..."
  cleanup_containers

  # 清理 Dragonboat WAL 数据（每个 run 结束后立即清理，避免磁盘爆满）
  rm -rf "$run_dir"/pe-node-*
}

# 从 metrics jsonl 末尾提取各 Region 最终 leader，写 leader_assignment.json
collect_leaders() {
  local run_dir="$1"
  python - "$run_dir" <<'PY'
import sys, glob, json, os
run_dir = sys.argv[1]
region_leader = {}
for f in sorted(glob.glob(os.path.join(run_dir, "node_*_metrics.jsonl"))):
    rows = [json.loads(l) for l in open(f) if l.strip()]
    if not rows:
        continue
    last = rows[-1]
    nid = last["node_id"]
    for rid in last.get("leader_of", []):
        region_leader[str(rid)] = nid
json.dump(region_leader, open(os.path.join(run_dir, "leader_assignment.json"), "w"), indent=2)
PY
}

# 跑一整套资源配置（A 或 B）
run_config() {
  local cfg="$1"
  local config_name="config${cfg}_n${NUM_NODES}"
  if [[ "$cfg" == "A" ]]; then
    CPUS=("${CPUS_A[@]}"); MEMS=("${MEMS_A[@]}")
  else
    CPUS=("${CPUS_B[@]}"); MEMS=("${MEMS_B[@]}")
  fi
  echo "═══ Config $config_name  cpus=${CPUS[*]}  mems=${MEMS[*]} ═══"
  for s in "${STRATEGIES[@]}"; do
    for r in $(seq 1 "$REPEAT"); do
      echo "──[ config=$config_name strategy=$s run=$r ]──"
      run_once "$s" "$OUTPUT_BASE/${config_name}/comp_${s}/run_${r}"
    done
  done
}

# ---------------- 前置：构建镜像 + 客户端 ----------------
if [[ "${SKIP_BUILD:-false}" == "true" ]] && docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "🔨 跳过镜像构建（SKIP_BUILD=true 且镜像已存在）"
else
  echo "🔨 构建服务端镜像..."
  docker build -f "$PROJECT_DIR/Dockerfile.priority-election" -t "$IMAGE" "$PROJECT_DIR"
fi

echo "🔨 构建客户端二进制..."
mkdir -p "$PROJECT_DIR/bin"
( cd "$PROJECT_DIR" && go build -o bin/priority-election-client ./cmd/priority-election-client )

# ---------------- 主循环 ----------------
case "$RESOURCE_CONFIG" in
  A) run_config A ;;
  B) run_config B ;;
  both) run_config A; run_config B ;;
  *) echo "未知 RESOURCE_CONFIG: $RESOURCE_CONFIG"; exit 1 ;;
esac

echo "📊 绘图..."
python "$SCRIPT_DIR/plot_priority_election.py" --base "$OUTPUT_BASE" || true

echo "✓ 实验完成，输出位于 $OUTPUT_BASE"

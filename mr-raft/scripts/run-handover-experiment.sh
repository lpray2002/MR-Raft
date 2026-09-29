#!/bin/bash
#============================================================
# UAV Handover 一致性验证实验脚本
#
# 流程:
#   1. 构建 Docker 镜像
#   2. 启动集群（读取 cluster_config.yaml，用环境变量配置）
#   3. 等待集群就绪（Leader 通过 LEADER_REGIONS 自动转移）
#   4. 运行 Handover 客户端（多组实验）
#   5. 收集结果
#
# 使用方式:
#   ./run-handover-experiment.sh              # 运行默认实验
#   ./run-handover-experiment.sh --config /path/to/dir  # 指定配置目录
#   ./run-handover-experiment.sh clean        # 清理容器
#   ./run-handover-experiment.sh build        # 仅构建镜像
#============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_DIR="/Users/lpray/workspace/work/2025_10_22_region-partitioning"
CONFIG_DIR="${HANDOVER_CONFIG_DIR:-$PYTHON_DIR/output/handover_cluster}"
RESULTS_DIR="$PROJECT_DIR/output/handover_results"

NETWORK_NAME="handover_full_network"
SUBNET="172.44.0.0/16"
IMAGE_SERVER="multi-region-raft/mec-grpc-full:latest"
IMAGE_CLIENT="multi-region-raft/handover-client:latest"

# 实验参数（可被外部覆盖）
WAYPOINTS_LIST=(
    "0,4,8,9,12,9,8,4,0"    # S1: 全可验证环形路径
    "0,4,8,9,12,0"          # S2: 含一次跨 Region
    "0,4,8,4,0,4,8,4,0"    # S3: 快速来回切换
)
DWELL_LIST=(10 5 3)
SPEED=25
SEEDS=(42 123 456)

# 激活 conda 环境
eval "$(conda shell.bash hook 2>/dev/null)" && conda activate base 2>/dev/null

# 并发压测参数（从 Python 配置读取）
CONCURRENCY_LIST=($(python3 -c "
import sys; sys.path.insert(0, '$PYTHON_DIR')
from export_cluster_config import CONCURRENCY_LIST
print(' '.join(str(c) for c in CONCURRENCY_LIST))
"))

#============================================================
# 参数解析
#============================================================

while [[ $# -gt 0 ]]; do
    case "$1" in
        --config)
            CONFIG_DIR="$2"
            shift 2
            ;;
        --single)
            SINGLE_MODE=true
            shift
            ;;
        clean|build)
            ACTION="$1"
            shift
            ;;
        *)
            shift
            ;;
    esac
done

#============================================================
# 辅助函数
#============================================================

log() { echo "[$(date '+%H:%M:%S')] $*"; }

cleanup() {
    log "Cleaning up containers..."
    docker ps -a --filter "name=handover-" -q | xargs -r docker rm -f 2>/dev/null || true
    docker network rm "$NETWORK_NAME" 2>/dev/null || true
    # 清理数据 volume（避免旧实验数据残留导致 key 冲突）
    docker volume ls -q --filter "name=handover_data_" | xargs -r docker volume rm 2>/dev/null || true
}

build_images() {
    log "Building Docker images..."
    cd "$PROJECT_DIR"

    # 服务端镜像
    if ! docker image inspect "$IMAGE_SERVER" &>/dev/null; then
        docker build -f Dockerfile.server -t "$IMAGE_SERVER" .
    else
        log "  Server image exists, skipping build"
    fi

    # Handover 客户端镜像
    docker build -f Dockerfile.handover-client -t "$IMAGE_CLIENT" .
    log "  Images ready"
}

create_network() {
    docker network rm "$NETWORK_NAME" 2>/dev/null || true
    docker network create --driver bridge --subnet "$SUBNET" "$NETWORK_NAME"
    log "  Network $NETWORK_NAME created ($SUBNET)"
}

start_cluster() {
    local config_yaml="$1"
    log "Starting cluster from $config_yaml..."

    # 解析 YAML 并通过环境变量启动每个节点
    python3 -c "
import yaml, json, sys, subprocess, os

with open('$config_yaml') as f:
    cfg = yaml.safe_load(f)

nodes = cfg['mec_nodes']
regions_cfg = cfg['regions']

# 构建 leader_placement: region_id -> node_id
leader_placement = {}
lp_str = cfg.get('handover', {}).get('leader_placement', '')
if lp_str:
    for pair in lp_str.split(','):
        parts = pair.strip().split(':')
        if len(parts) == 2:
            leader_placement[int(parts[0])] = int(parts[1])

# 构建每个 region 的 CLUSTER_MEMBERS 环境变量
# 格式: raftNodeID:ip:port,...
# raftNodeID = regionID * 100 + nodeID
cluster_members = {}
for region in regions_cfg:
    rid = region['id']
    members_parts = []
    for member_nid in region['members']:
        # 找到该节点的 Raft 地址
        node_cfg = next(n for n in nodes if n['id'] == member_nid)
        raft_node_id = rid * 100 + member_nid
        members_parts.append(f'{raft_node_id}:{node_cfg[\"address\"]}')
    cluster_members[rid] = ','.join(members_parts)

# 启动每个节点
for node in nodes:
    nid = node['id']
    addr = node['address']
    ip = addr.split(':')[0]
    raft_port = addr.split(':')[1]
    api_port = node['api_port']
    node_regions = node['regions']

    # 构建环境变量
    env_vars = {
        'MEC_NODE_ID': str(nid),
        'MEC_NODE_NAME': f'mec-node-{nid}',
        'MEC_NODE_ADDRESS': addr,
        'GRPC_PORT': str(api_port),
        'MEC_NODE_REGIONS': ','.join(str(r) for r in node_regions),
        'MEC_NODE_DATA_DIR': f'/data/node-{nid}',
        'USE_MEMORY_SM': 'true',
    }

    # 设置 CLUSTER_MEMBERS_{rid} 环境变量
    for rid in node_regions:
        env_vars[f'CLUSTER_MEMBERS_{rid}'] = cluster_members[rid]

    # 设置 LEADER_REGIONS（该节点应为 leader 的 region 列表）
    leader_regions = [str(rid) for rid, lid in leader_placement.items() if lid == nid]
    if leader_regions:
        env_vars['LEADER_REGIONS'] = ','.join(leader_regions)

    # 构建 docker run 命令
    cmd = [
        'docker', 'run', '-d',
        '--name', f'handover-node-{nid}',
        '--network', '$NETWORK_NAME',
        '--ip', ip,
    ]

    # 添加环境变量
    for k, v in env_vars.items():
        cmd.extend(['-e', f'{k}={v}'])

    # volume 和镜像
    cmd.extend([
        '-v', f'handover_data_node{nid}:/data',
        '$IMAGE_SERVER',
    ])

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        lr_info = f' [Leader: R{env_vars.get(\"LEADER_REGIONS\", \"-\")}]' if 'LEADER_REGIONS' in env_vars else ''
        print(f'  ✓ Node {nid} started ({ip}:{api_port}, regions={node_regions}){lr_info}')
    else:
        print(f'  ✗ Node {nid} failed: {result.stderr.strip()}')
        sys.exit(1)
"
}

wait_cluster_ready() {
    log "Waiting for cluster to be ready (incl. leader transfer)..."
    local max_wait=90
    local waited=0

    # 找第一个节点的 gRPC 端口（从 YAML 中获取）
    local first_api_port
    first_api_port=$(python3 -c "
import yaml
with open('$CONFIG_DIR/cluster_config.yaml') as f:
    cfg = yaml.safe_load(f)
print(cfg['mec_nodes'][0]['api_port'])
")
    local first_node_id
    first_node_id=$(python3 -c "
import yaml
with open('$CONFIG_DIR/cluster_config.yaml') as f:
    cfg = yaml.safe_load(f)
print(cfg['mec_nodes'][0]['id'])
")

    while [ $waited -lt $max_wait ]; do
        if docker exec "handover-node-$first_node_id" sh -c "nc -z localhost $first_api_port" 2>/dev/null; then
            log "  Cluster ready after ${waited}s (gRPC port responding)"
            # 额外等待 leader transfer 完成（服务端会自动执行）
            sleep 8
            log "  Leader transfer window complete"
            return 0
        fi
        sleep 2
        waited=$((waited + 2))
    done

    log "  ⚠ Cluster not ready after ${max_wait}s"
    return 1
}

run_experiment() {
    local scenario_file="$1"
    local output_file="$2"
    local stale_flag="$3"

    local stale_arg=""
    if [ "$stale_flag" = "true" ]; then
        stale_arg="--stale"
    fi

    docker run --rm \
        --network "$NETWORK_NAME" \
        -v "$CONFIG_DIR:/configs:ro" \
        -v "$RESULTS_DIR:/results" \
        "$IMAGE_CLIENT" \
        --scenario="/configs/$(basename "$scenario_file")" \
        --output="/results/$(basename "$output_file")" \
        $stale_arg
}

#============================================================
# 主流程
#============================================================

main() {
    mkdir -p "$RESULTS_DIR"

    log "═══════════════════════════════════════════════════"
    log "  UAV Handover Consistency Experiment"
    log "═══════════════════════════════════════════════════"
    log "  Config dir: $CONFIG_DIR"
    log "  Results dir: $RESULTS_DIR"

    # 清理旧容器
    cleanup

    # 构建镜像
    build_images

    # 创建网络
    create_network

    # 单次模式：使用已有配置，遍历并发数组运行
    if [ "$SINGLE_MODE" = "true" ]; then
        if [ ! -f "$CONFIG_DIR/cluster_config.yaml" ]; then
            log "ERROR: $CONFIG_DIR must contain cluster_config.yaml"
            exit 1
        fi

        # 读取已有 scenario 的途径点（用于不同并发数下重新生成 scenario）
        local waypoints_str
        waypoints_str=$(python3 -c "
import json, os
sf = os.path.join('$CONFIG_DIR', 'scenario.json')
if os.path.exists(sf):
    with open(sf) as f:
        s = json.load(f)
    print(','.join(str(w) for w in s['config']['waypoints']))
else:
    print('')
" 2>/dev/null)

        local ticks_file="$CONFIG_DIR/timed_ticks.json"
        local ticks_arg=""
        if [ -f "$ticks_file" ]; then
            ticks_arg="--ticks $ticks_file"
        fi

        local exp_count=0
        for C in "${CONCURRENCY_LIST[@]}"; do
            log "━━━ Concurrency=$C ━━━"

            # 每轮清理 + 重启集群（确保无残留数据）
            cleanup
            create_network

            # 重新生成 scenario（只改 concurrency）
            if [ -n "$waypoints_str" ]; then
                cd "$PYTHON_DIR"
                log "  Regenerating scenario with concurrency=$C..."
                python3 export_cluster_config.py \
                    --waypoints "$waypoints_str" \
                    --speed "$SPEED" \
                    --dwell 0 \
                    --concurrency "$C" \
                    $ticks_arg \
                    --output-dir "$CONFIG_DIR" || {
                    log "  ERROR: export_cluster_config.py failed for concurrency=$C"
                    continue
                }
            fi

            start_cluster "$CONFIG_DIR/cluster_config.yaml"
            wait_cluster_ready

            # ReadIndex
            local result_name="result_c${C}_readindex.json"
            log "Running: $result_name"
            run_experiment "$CONFIG_DIR/scenario.json" "$result_name" "false"
            exp_count=$((exp_count + 1))

            # StaleRead
            result_name="result_c${C}_stale.json"
            log "Running: $result_name"
            run_experiment "$CONFIG_DIR/scenario.json" "$result_name" "true"
            exp_count=$((exp_count + 1))
        done

        cleanup

        log "═══════════════════════════════════════════════════"
        log "  $exp_count experiments completed (${#CONCURRENCY_LIST[@]} concurrency levels)"
        log "  Results in: $RESULTS_DIR/"
        log "═══════════════════════════════════════════════════"
        return
    fi

    # 多场景模式
    local exp_count=0
    for idx in "${!WAYPOINTS_LIST[@]}"; do
        local waypoints="${WAYPOINTS_LIST[$idx]}"
        local dwell="${DWELL_LIST[$idx]}"

        for seed in "${SEEDS[@]}"; do
            # 生成 scenario.json
            log "Generating scenario: waypoints=$waypoints, dwell=$dwell, seed=$seed"
            cd "$PYTHON_DIR"
            python3 export_cluster_config.py \
                --waypoints "$waypoints" \
                --speed "$SPEED" \
                --dwell "$dwell" \
                --seed "$seed" \
                --output-dir "$CONFIG_DIR" > /dev/null

            # 启动集群（每组实验重启以确保干净状态）
            cleanup
            create_network
            start_cluster "$CONFIG_DIR/cluster_config.yaml"
            wait_cluster_ready

            # 运行 READ_INDEX 模式
            local result_name="result_s${idx}_seed${seed}_readindex.json"
            log "Running experiment: $result_name"
            run_experiment "$CONFIG_DIR/scenario.json" "$result_name" "false"
            exp_count=$((exp_count + 1))

            # 运行 STALE_READ 模式
            result_name="result_s${idx}_seed${seed}_stale.json"
            log "Running experiment: $result_name"
            run_experiment "$CONFIG_DIR/scenario.json" "$result_name" "true"
            exp_count=$((exp_count + 1))
        done
    done

    # 清理
    cleanup

    log "═══════════════════════════════════════════════════"
    log "  All $exp_count experiments completed!"
    log "  Results in: $RESULTS_DIR/"
    log "═══════════════════════════════════════════════════"
}

# 支持单独运行子命令
case "${ACTION:-}" in
    clean)   cleanup ;;
    build)   build_images ;;
    *)       main ;;
esac

#!/bin/bash
#============================================================
# Native 模式运行 Handover 实验（无 Docker，直接进程）
#
# 用法:
#   bash scripts/run-handover-native.sh --single \
#       --config /root/region-partitioning/output/handover_cluster
#============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_DIR="/root/region-partitioning"
CONFIG_DIR="${HANDOVER_CONFIG_DIR:-$PYTHON_DIR/output/handover_cluster}"
RESULTS_DIR="$PROJECT_DIR/output/handover_results"

SERVER_BIN="$PROJECT_DIR/mec-node-grpc-full-topology"
CLIENT_BIN="$PROJECT_DIR/handover-client"

# 实验参数
SPEED=25
CONCURRENCY=100

# 延迟配置数组（毫秒）
# 0-60ms 按 2ms 拆分
DELAY_CONFIGS=(
    "0" "2" "4" "6" "8" "10" "12" "14" "16" "18" "20"
    "22" "24" "26" "28" "30" "32" "34" "36" "38" "40"
    "42" "44" "46" "48" "50" "52" "54" "56" "58" "60"
)

# 是否只输出 summary（不含逐条 records）
SUMMARY_ONLY="true"

# Raft 数据目录
RAFT_DATA_DIR="$PROJECT_DIR/runtime/raft-data"

#============================================================
# 参数解析
#============================================================
SINGLE_MODE="false"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --config)
            CONFIG_DIR="$2"
            shift 2
            ;;
        --single)
            SINGLE_MODE="true"
            shift
            ;;
        --use-shm)
            RAFT_DATA_DIR="/dev/shm/raft-data"
            shift
            ;;
        --summary-only)
            SUMMARY_ONLY="true"
            shift
            ;;
        --no-summary-only)
            SUMMARY_ONLY="false"
            shift
            ;;
        --delay-configs)
            IFS=';' read -ra DELAY_CONFIGS <<< "$2"
            shift 2
            ;;
        --concurrency)
            CONCURRENCY="$2"
            shift 2
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

# PID 数组，用于清理
declare -a SERVER_PIDS=()

cleanup() {
    log "Cleaning up server processes..."
    for pid in "${SERVER_PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
    done
    for pid in "${CLIENT_PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
    done
    SERVER_PIDS=()
    CLIENT_PIDS=()
    # 等待进程退出
    sleep 1
    # 强制清理残留
    pkill -9 -f "mec-node-grpc-full-topology" 2>/dev/null || true
    pkill -9 -f "handover-client" 2>/dev/null || true
    sleep 2
    # 清理数据目录
    rm -rf "$RAFT_DATA_DIR" 2>/dev/null || true
    rm -rf /dev/shm/raft-data 2>/dev/null || true
}

declare -a CLIENT_PIDS=()

trap cleanup EXIT

start_cluster() {
    local config_file="$1"
    log "Starting cluster from $config_file (native mode)..."

    # 解析 cluster_config.yaml 并启动节点
    local num_nodes
    num_nodes=$(python3 -c "
import yaml
with open('$config_file') as f:
    cfg = yaml.safe_load(f)
print(len(cfg['mec_nodes']))
")

    log "  Starting $num_nodes MEC nodes..."

    # 生成每个节点的环境变量并启动
    python3 << PYEOF > /tmp/autodl_node_envs.sh
import yaml

with open("$config_file") as f:
    cfg = yaml.safe_load(f)

nodes = cfg["mec_nodes"]
regions_cfg = cfg.get("regions", [])

# 构建 region -> members 映射 (raftNodeID:ip:port 格式)
# raftNodeID = regionID * 100 + nodeID
# 所有节点都在 localhost，端口从 address 字段提取
node_port_map = {}
for n in nodes:
    port = int(n["address"].split(":")[-1])
    node_port_map[n["id"]] = port

region_members_map = {}
for r in regions_cfg:
    rid = r["id"]
    members = r["members"]
    parts = []
    for nid in members:
        raft_node_id = rid * 100 + nid
        port = node_port_map[nid]
        parts.append(f"{raft_node_id}:127.0.0.1:{port}")
    region_members_map[rid] = ",".join(parts)

# Leader placement
leader_placement_str = cfg.get("handover", {}).get("leader_placement", "")
leader_map = {}
if leader_placement_str:
    for pair in leader_placement_str.split(","):
        rid_str, nid_str = pair.split(":")
        leader_map[int(rid_str)] = int(nid_str)

# 输出每个节点的启动命令
for n in nodes:
    nid = n["id"]
    raft_port = int(n["address"].split(":")[-1])
    api_port = n["api_port"]
    node_regions = n["regions"]

    env_parts = []
    env_parts.append(f"MEC_NODE_ID={nid}")
    env_parts.append(f"MEC_NODE_ADDRESS=127.0.0.1:{raft_port}")
    env_parts.append(f"GRPC_PORT={api_port}")
    env_parts.append(f"MEC_NODE_REGIONS={','.join(str(r) for r in node_regions)}")
    env_parts.append("USE_MEMORY_SM=true")
    env_parts.append(f"MEC_NODE_DATA_DIR=$RAFT_DATA_DIR/{raft_port}")
    env_parts.append(f"NETWORK_DELAY_US=$NETWORK_DELAY_US")
    env_parts.append("GOMAXPROCS=4")

    for rid in node_regions:
        if rid in region_members_map:
            env_parts.append(f"CLUSTER_MEMBERS_{rid}={region_members_map[rid]}")

    my_leader_regions = [str(rid) for rid, lnid in leader_map.items() if lnid == nid]
    if my_leader_regions:
        env_parts.append(f"LEADER_REGIONS={','.join(my_leader_regions)}")

    env_str = " ".join(env_parts)
    print(f"# Node {nid}")
    print(f"nohup env {env_str} $SERVER_BIN > $RESULTS_DIR/node_{nid}.log 2>&1 &")
    print(f"SERVER_PIDS+=(\$!)")
    print(f'log "    Node {nid} started (PID=\$!)"')
PYEOF

    source /tmp/autodl_node_envs.sh
    rm -f /tmp/autodl_node_envs.sh
}

wait_cluster_ready() {
    log "Waiting for cluster to be ready..."
    local max_wait=60
    local waited=0

    # 获取所有节点的 API 端口
    local all_ports
    all_ports=$(python3 -c "
import yaml
with open('$CONFIG_DIR/cluster_config.yaml') as f:
    cfg = yaml.safe_load(f)
print(' '.join(str(n['api_port']) for n in cfg['mec_nodes']))
")

    while [ $waited -lt $max_wait ]; do
        local all_ready=true
        for port in $all_ports; do
            if ! timeout 1 bash -c "echo > /dev/tcp/127.0.0.1/$port" 2>/dev/null && \
               ! nc -z 127.0.0.1 $port 2>/dev/null; then
                all_ready=false
                break
            fi
        done
        if [ "$all_ready" = true ]; then
            log "  All nodes ready after ${waited}s"
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

    mkdir -p "$RESULTS_DIR"

    local summary_arg=""
    if [ "$SUMMARY_ONLY" = "true" ]; then
        summary_arg="--summary-only"
    fi

    "$CLIENT_BIN" \
        --scenario="$scenario_file" \
        --output="$RESULTS_DIR/$output_file" \
        $summary_arg \
        $stale_arg || {
        log "  ⚠ Client exited with error (result may be partial)"
    }

    if [ -f "$RESULTS_DIR/$output_file" ]; then
        log "  ✓ Saved: $RESULTS_DIR/$output_file"
    else
        log "  ✗ No output file generated"
    fi
}

fix_scenario_addresses() {
    # 将 scenario.json 中的 Docker IP (172.44.x.x) 替换为 localhost
    local scenario_file="$1"
    python3 << PYEOF
import json

with open("$scenario_file") as f:
    s = json.load(f)

# 替换 node_addresses 中的 IP
new_addrs = {}
for nid, addr in s["node_addresses"].items():
    # 172.44.0.XX:43XXX -> 127.0.0.1:43XXX
    port = addr.split(":")[-1]
    new_addrs[nid] = f"127.0.0.1:{port}"
s["node_addresses"] = new_addrs

with open("$scenario_file", "w") as f:
    json.dump(s, f, indent=2)

print(f"  Fixed {len(new_addrs)} node addresses to localhost")
PYEOF
}

#============================================================
# 主流程
#============================================================
# 主流程
#============================================================
main() {
    log "╔══════════════════════════════════════════════════════════╗"
    log "║  Handover Experiment (Native Mode - No Docker)          ║"
    log "╚══════════════════════════════════════════════════════════╝"
    log "  Config: $CONFIG_DIR"
    log "  Concurrency: $CONCURRENCY"
    log "  Delay configs: ${DELAY_CONFIGS[*]} ms"
    log "  Data dir: $RAFT_DATA_DIR"

    mkdir -p "$RESULTS_DIR"

    if [ "$SINGLE_MODE" = "true" ]; then
        if [ ! -f "$CONFIG_DIR/cluster_config.yaml" ]; then
            log "ERROR: $CONFIG_DIR must contain cluster_config.yaml"
            exit 1
        fi

        # 读取途径点
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
        local total_delays=${#DELAY_CONFIGS[@]}

        for delay_ms in "${DELAY_CONFIGS[@]}"; do
            # 计算微秒延迟
            NETWORK_DELAY_US=$(python3 -c "print(int(float('${delay_ms}') * 1000))")
            export NETWORK_DELAY_US

            log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            log "  Delay = ${delay_ms}ms (${NETWORK_DELAY_US}us), Concurrency = $CONCURRENCY"
            log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

            # 完整清理（杀进程 + 删数据 + 等端口释放）
            cleanup
            mkdir -p "$RAFT_DATA_DIR"
            log "  Waiting for ports to release..."
            sleep 3

            # 修复地址为 localhost（首次运行时）
            if [ "$exp_count" -eq 0 ]; then
                fix_scenario_addresses "$CONFIG_DIR/scenario.json"
            fi

            # 启动集群
            start_cluster "$CONFIG_DIR/cluster_config.yaml"
            wait_cluster_ready
            sleep 5  # 额外等待确保所有节点 gRPC 服务就绪

            # ReadIndex 模式
            local result_name="result_d${delay_ms}ms_c${CONCURRENCY}_readindex.json"
            log "  Running: $result_name"
            run_experiment "$CONFIG_DIR/scenario.json" "$result_name" "false"
            exp_count=$((exp_count + 1))

            # 清理后重启集群跑 StaleRead
            cleanup
            mkdir -p "$RAFT_DATA_DIR"
            sleep 3

            start_cluster "$CONFIG_DIR/cluster_config.yaml"
            wait_cluster_ready
            sleep 5

            # StaleRead 模式
            result_name="result_d${delay_ms}ms_c${CONCURRENCY}_stale.json"
            log "  Running: $result_name"
            run_experiment "$CONFIG_DIR/scenario.json" "$result_name" "true"
            exp_count=$((exp_count + 1))
        done

        cleanup

        log "═══════════════════════════════════════════════════════"
        log "  $exp_count experiments completed (${total_delays} delay groups)"
        log "  Results in: $RESULTS_DIR/"
        log "═══════════════════════════════════════════════════════"
    fi
}

main

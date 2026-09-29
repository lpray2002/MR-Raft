#!/bin/bash
#============================================================
# 故障恢复实验 - Native 模式（无 Docker，直接进程）
#
# 实验 A-single（真实拓扑，单故障）:
#   bash scripts/run-fault-recovery.sh \
#       --config docker/configs/method_threshold_based.yaml \
#       --fault-node-id 1 \
#       --fault-delay 10 --fault-duration 5 \
#       --concurrency 50 --duration 40 --repeat 3
#
# 实验 A-seq（真实拓扑，多故障顺序注入）:
#   bash scripts/run-fault-recovery.sh \
#       --config docker/configs/method_threshold_based.yaml \
#       --fault-events "1:8:5:Follower,2:22:5:Leader,3:36:5:Mixed" \
#       --concurrency 50 --duration 55 --repeat 3
#
# 实验 B（合成集群，5节点 × N Region）:
#   bash scripts/run-fault-recovery.sh \
#       --synthetic --regions 3 \
#       --fault-node-id 0 \
#       --fault-delay 10 --fault-duration 5 \
#       --concurrency 50 --duration 40 --repeat 3
#============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

SERVER_BIN="$PROJECT_DIR/fault-recovery-server"
CLIENT_BIN="$PROJECT_DIR/grpc-client-full-topology"

# 默认参数
CONFIG_FILE=""
SYNTHETIC_MODE="false"
SYNTHETIC_REGIONS=3
FAULT_NODE_ID=0
FAULT_DELAY=45
FAULT_DURATION=10
CONCURRENCY=100
DURATION=75
PROPOSAL_SIZE=128
REPEAT=1
OUTPUT_DIR="$PROJECT_DIR/output/fault_recovery"
RAFT_DATA_DIR="$PROJECT_DIR/runtime/fault-recovery-data"
FORCE_LEADER=""
NETWORK_DELAY_US=0
FAULT_EVENTS=""
CPU_LIMIT=2
GOMAXPROCS=4
PROPOSE_TIMEOUT_MS=1000

#============================================================
# 参数解析
#============================================================
while [[ $# -gt 0 ]]; do
    case "$1" in
        --config)        CONFIG_FILE="$2"; shift 2 ;;
        --synthetic)     SYNTHETIC_MODE="true"; shift ;;
        --regions)       SYNTHETIC_REGIONS="$2"; shift 2 ;;
        --fault-node-id) FAULT_NODE_ID="$2"; shift 2 ;;
        --fault-delay)   FAULT_DELAY="$2"; shift 2 ;;
        --fault-duration) FAULT_DURATION="$2"; shift 2 ;;
        --concurrency)   CONCURRENCY="$2"; shift 2 ;;
        --duration)      DURATION="$2"; shift 2 ;;
        --proposal-size) PROPOSAL_SIZE="$2"; shift 2 ;;
        --repeat)        REPEAT="$2"; shift 2 ;;
        --output-dir)    OUTPUT_DIR="$2"; shift 2 ;;
        --force-leader)  FORCE_LEADER="$2"; shift 2 ;;
        --network-delay-us) NETWORK_DELAY_US="$2"; shift 2 ;;
        --fault-events)  FAULT_EVENTS="$2"; shift 2 ;;
        --cpu-limit)     CPU_LIMIT="$2"; shift 2 ;;
        --gomaxprocs)    GOMAXPROCS="$2"; shift 2 ;;
        --propose-timeout-ms) PROPOSE_TIMEOUT_MS="$2"; shift 2 ;;
        --use-shm)       RAFT_DATA_DIR="/dev/shm/fault-recovery-data"; shift ;;
        *) echo "Unknown option: $1"; shift ;;
    esac
done

#============================================================
# 辅助函数
#============================================================
log() { echo "[$(date '+%H:%M:%S')] $*"; }

declare -a SERVER_PIDS=()
CLIENT_PID=""
TARGET_PID=""

cleanup() {
    log "Cleaning up..."
    # 停止客户端
    if [ -n "$CLIENT_PID" ] && kill -0 "$CLIENT_PID" 2>/dev/null; then
        kill "$CLIENT_PID" 2>/dev/null || true
    fi
    # 停止所有服务端
    for pid in "${SERVER_PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
    done
    SERVER_PIDS=()
    CLIENT_PID=""
    TARGET_PID=""
    sleep 1
    pkill -9 -f "fault-recovery-server" 2>/dev/null || true
    pkill -9 -f "mec-node-grpc-full-topology" 2>/dev/null || true
    pkill -9 -f "grpc-client-full-topology" 2>/dev/null || true
    pkill -9 -f "fault-recovery-client" 2>/dev/null || true
    # 等待进程完全退出（用 _w 避免覆盖外层循环变量 i）
    for _w in $(seq 1 10); do
        if ! pgrep -f "fault-recovery-server" > /dev/null 2>&1; then
            break
        fi
        sleep 1
    done
    sleep 5
    # 等待 Raft(33xxx) 和 gRPC(43xxx) 端口完全释放
    for _p in $(seq 1 60); do
        if python3 -c "
import socket
for p in [33008, 43008, 33012, 43012]:
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(('127.0.0.1', p))
        s.close()
    except:
        s.close()
        exit(1)
" 2>/dev/null; then
            break
        fi
        sleep 1
    done
    rm -rf "$RAFT_DATA_DIR" 2>/dev/null || true
    # 清理可能残留的其他 raft 数据（如压测脚本留下的）
    rm -rf /dev/shm/raft-data 2>/dev/null || true
    rm -rf /dev/shm/fault-recovery-data 2>/dev/null || true
}

trap cleanup EXIT

#============================================================
# CPU 亲和性（taskset）
#============================================================
USE_TASKSET=false
if command -v taskset &>/dev/null; then
    if taskset -c 0 true 2>/dev/null; then
        USE_TASKSET=true
    fi
fi

NODE_IDX=0
get_cpu_range() {
    local total_cores
    total_cores=$(nproc 2>/dev/null || sysctl -n hw.ncpu)
    local start_core=$(( (NODE_IDX * CPU_LIMIT) % total_cores ))
    local end_core=$(( start_core + CPU_LIMIT - 1 ))
    [ $end_core -ge $total_cores ] && end_core=$((total_cores - 1))
    NODE_IDX=$((NODE_IDX + 1))
    echo "${start_core}-${end_core}"
}

#============================================================
# 合成集群配置生成
#============================================================
generate_synthetic_config() {
    local num_regions=$1
    local output_file=$2

    python3 << PYEOF > "$output_file"
import yaml, sys

num_regions = $num_regions
nodes = []
base_raft_port = 33000
base_api_port = 43000
all_region_ids = list(range(1, num_regions + 1))

for nid in range(5):
    nodes.append({
        'name': f'synthetic-node-{nid}',
        'id': nid,
        'address': f'127.0.0.1:{base_raft_port + nid}',
        'api_port': base_api_port + nid,
        'regions': all_region_ids
    })

regions = []
for rid in all_region_ids:
    members = list(range(5))
    regions.append({
        'id': rid,
        'name': f'Synthetic-Region-{rid}',
        'members': members
    })

config = {
    'mec_nodes': nodes,
    'regions': regions,
}

yaml.dump(config, sys.stdout, default_flow_style=False)
PYEOF
    log "  Generated synthetic config: 5 nodes x $num_regions regions -> $output_file"
}

#============================================================
# 从 YAML 配置生成节点启动命令并启动集群
#============================================================
start_cluster() {
    local config_file="$1"
    local leader_override="$2"
    NODE_IDX=0
    local run_dir="$3"

    log "Starting cluster from $config_file ..."

    mkdir -p "$RAFT_DATA_DIR"
    mkdir -p "$run_dir"

    python3 << PYEOF > /dev/shm/fault_recovery_node_envs.sh
import yaml

with open("$config_file") as f:
    cfg = yaml.safe_load(f)

nodes = cfg["mec_nodes"]
regions_cfg = cfg.get("regions", [])

# 从节点配置构建 region -> members 映射
region_to_nodes = {}
for n in nodes:
    for rid in n["regions"]:
        region_to_nodes.setdefault(rid, []).append(n["id"])

node_port_map = {}
for n in nodes:
    port = int(n["address"].split(":")[-1])
    node_port_map[n["id"]] = port

region_members_map = {}
for rid, node_ids in region_to_nodes.items():
    parts = []
    for nid in sorted(node_ids):
        raft_node_id = rid * 100 + nid
        port = node_port_map[nid]
        parts.append(f"{raft_node_id}:127.0.0.1:{port}")
    region_members_map[rid] = ",".join(parts)

# Leader placement override
leader_override_str = "$leader_override"
leader_map = {}
if leader_override_str:
    for pair in leader_override_str.split(","):
        if ":" in pair:
            rid_str, nid_str = pair.split(":")
            leader_map[int(rid_str)] = int(nid_str)

import os
total_cores = int(os.popen("nproc 2>/dev/null || sysctl -n hw.ncpu").read().strip())

for idx, n in enumerate(nodes):
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
    env_parts.append("GOMAXPROCS=$GOMAXPROCS")
    env_parts.append("PROPOSE_TIMEOUT_MS=$PROPOSE_TIMEOUT_MS")
    env_parts.append(f"REGION_STATS_FILE=$run_dir/node_{nid}_region_stats.jsonl")

    for rid in node_regions:
        if rid in region_members_map:
            env_parts.append(f"CLUSTER_MEMBERS_{rid}={region_members_map[rid]}")

    my_leader_regions = [str(rid) for rid, lnid in leader_map.items() if lnid == nid]
    if my_leader_regions:
        env_parts.append(f"LEADER_REGIONS={','.join(my_leader_regions)}")

    env_str = " ".join(env_parts)
    cpu_start = idx * $CPU_LIMIT % total_cores
    cpu_end = cpu_start + $CPU_LIMIT - 1
    if cpu_end >= total_cores:
        cpu_end = total_cores - 1
    cpu_range = f"{cpu_start}-{cpu_end}"
    print(f"# Node {nid}")
    print(f'if [ "\$USE_TASKSET" = true ]; then')
    print(f"  nohup env {env_str} taskset -c {cpu_range} \$SERVER_BIN > $run_dir/node_{nid}.log 2>&1 &")
    print(f"else")
    print(f"  nohup env {env_str} \$SERVER_BIN > $run_dir/node_{nid}.log 2>&1 &")
    print(f"fi")
    print(f"SERVER_PIDS+=(\$!)")
    print(f"echo \$! > $run_dir/node_{nid}.pid")
    # 记录故障目标节点的 PID（单故障模式兼容）
    print(f"if [ {nid} -eq $FAULT_NODE_ID ]; then TARGET_PID=\$!; TARGET_LOG=$run_dir/node_{nid}.log; fi")
    print(f'log "    Node {nid} started (PID=\$!, regions={node_regions})"')
PYEOF

    source /dev/shm/fault_recovery_node_envs.sh
    rm -f /dev/shm/fault_recovery_node_envs.sh
}

#============================================================
# 等待集群就绪
#============================================================
wait_cluster_ready() {
    local config_file="$1"
    log "Waiting for cluster to be ready..."
    local max_wait=120
    local waited=0

    local all_ports
    all_ports=$(python3 -c "
import yaml
with open('$config_file') as f:
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
        sleep 5
        waited=$((waited + 2))
    done

    log "  WARNING: Cluster not ready after ${max_wait}s"
    return 1
}

#============================================================
# 生成 NODE_CONFIGS 环境变量（给 fault-recovery-client 用）
#============================================================
generate_client_node_configs() {
    local config_file="$1"
    python3 -c "
import yaml
with open('$config_file') as f:
    cfg = yaml.safe_load(f)
parts = []
for n in cfg['mec_nodes']:
    nid = n['id']
    port = n['api_port']
    regions = ','.join(str(r) for r in n['regions'])
    parts.append(f'{nid}:127.0.0.1:{port}:{regions}:$CONCURRENCY')
print(';'.join(parts))
"
}

#============================================================
# 重启故障节点（保留数据目录）
#============================================================
restart_fault_node() {
    local config_file="$1"
    local leader_override="$2"
    local run_dir="$3"

    log "Restarting fault node $FAULT_NODE_ID (preserving data dir)..."

    python3 << PYEOF > /dev/shm/fault_recovery_restart.sh
import yaml

with open("$config_file") as f:
    cfg = yaml.safe_load(f)

nodes = cfg["mec_nodes"]
target = None
for n in nodes:
    if n["id"] == $FAULT_NODE_ID:
        target = n
        break

if not target:
    print('echo "ERROR: fault node $FAULT_NODE_ID not found in config"')
    exit(1)

# 重建 region -> members
region_to_nodes = {}
for n in nodes:
    for rid in n["regions"]:
        region_to_nodes.setdefault(rid, []).append(n["id"])

node_port_map = {}
for n in nodes:
    port = int(n["address"].split(":")[-1])
    node_port_map[n["id"]] = port

region_members_map = {}
for rid, node_ids in region_to_nodes.items():
    parts = []
    for nid in sorted(node_ids):
        raft_node_id = rid * 100 + nid
        port = node_port_map[nid]
        parts.append(f"{raft_node_id}:127.0.0.1:{port}")
    region_members_map[rid] = ",".join(parts)

leader_override_str = "$leader_override"
leader_map = {}
if leader_override_str:
    for pair in leader_override_str.split(","):
        if ":" in pair:
            rid_str, nid_str = pair.split(":")
            leader_map[int(rid_str)] = int(nid_str)

import os
total_cores = int(os.popen("nproc 2>/dev/null || sysctl -n hw.ncpu").read().strip())

nid = target["id"]
raft_port = int(target["address"].split(":")[-1])
api_port = target["api_port"]
node_regions = target["regions"]

env_parts = []
env_parts.append(f"MEC_NODE_ID={nid}")
env_parts.append(f"MEC_NODE_ADDRESS=127.0.0.1:{raft_port}")
env_parts.append(f"GRPC_PORT={api_port}")
env_parts.append(f"MEC_NODE_REGIONS={','.join(str(r) for r in node_regions)}")
env_parts.append("USE_MEMORY_SM=true")
env_parts.append(f"MEC_NODE_DATA_DIR=$RAFT_DATA_DIR/{raft_port}")
env_parts.append(f"NETWORK_DELAY_US=$NETWORK_DELAY_US")
env_parts.append("GOMAXPROCS=$GOMAXPROCS")
env_parts.append("PROPOSE_TIMEOUT_MS=$PROPOSE_TIMEOUT_MS")
env_parts.append(f"REGION_STATS_FILE=$run_dir/node_{nid}_region_stats_restart.jsonl")

for rid in node_regions:
    if rid in region_members_map:
        env_parts.append(f"CLUSTER_MEMBERS_{rid}={region_members_map[rid]}")

# 重启时不传 LEADER_REGIONS，避免触发 Leader Transfer 导致 TPS=0
# my_leader_regions = [str(rid) for rid, lnid in leader_map.items() if lnid == nid]
# if my_leader_regions:
#     env_parts.append(f"LEADER_REGIONS={','.join(my_leader_regions)}")

env_str = " ".join(env_parts)
cpu_start = nid * $CPU_LIMIT % total_cores
cpu_end = cpu_start + $CPU_LIMIT - 1
if cpu_end >= total_cores:
    cpu_end = total_cores - 1
cpu_range = f"{cpu_start}-{cpu_end}"
print(f'if [ "\$USE_TASKSET" = true ]; then')
print(f"  nohup env {env_str} taskset -c {cpu_range} \$SERVER_BIN > $run_dir/node_{nid}_restart.log 2>&1 &")
print(f"else")
print(f"  nohup env {env_str} \$SERVER_BIN > $run_dir/node_{nid}_restart.log 2>&1 &")
print(f"fi")
print(f"TARGET_PID=\$!")
print(f"SERVER_PIDS+=(\$!)")
print(f"TARGET_LOG=$run_dir/node_{nid}_restart.log")
PYEOF

    source /dev/shm/fault_recovery_restart.sh
    rm -f /dev/shm/fault_recovery_restart.sh
    log "  Fault node $FAULT_NODE_ID restarted (PID=$TARGET_PID)"
}

#============================================================
# 按 ID 杀死指定节点
#============================================================
kill_node_by_id() {
    local node_id="$1"
    local run_dir="$2"
    local pid_file="$run_dir/node_${node_id}.pid"
    if [ -f "$pid_file" ]; then
        local pid
        pid=$(cat "$pid_file")
        if kill -0 "$pid" 2>/dev/null; then
            kill -9 "$pid" 2>/dev/null || true
            log "  FAULT INJECTION: kill -9 node $node_id (PID=$pid)"
        else
            log "  WARNING: node $node_id (PID=$pid) not running"
        fi
    else
        log "  WARNING: PID file not found for node $node_id"
    fi
}

#============================================================
# 按 ID 重启指定节点（保留数据目录）
#============================================================
restart_node_by_id() {
    local node_id="$1"
    local config_file="$2"
    local leader_override="$3"
    local run_dir="$4"
    local restart_label="$5"

    log "  Restarting node $node_id (preserving data, label=$restart_label)..."

    python3 << PYEOF > /dev/shm/fault_recovery_restart_multi.sh
import yaml

with open("$config_file") as f:
    cfg = yaml.safe_load(f)

nodes = cfg["mec_nodes"]
target = None
for n in nodes:
    if n["id"] == $node_id:
        target = n
        break

if not target:
    print('echo "ERROR: node $node_id not found in config"')
    exit(1)

region_to_nodes = {}
for n in nodes:
    for rid in n["regions"]:
        region_to_nodes.setdefault(rid, []).append(n["id"])

node_port_map = {}
for n in nodes:
    port = int(n["address"].split(":")[-1])
    node_port_map[n["id"]] = port

region_members_map = {}
for rid, node_ids in region_to_nodes.items():
    parts = []
    for nid in sorted(node_ids):
        raft_node_id = rid * 100 + nid
        port = node_port_map[nid]
        parts.append(f"{raft_node_id}:127.0.0.1:{port}")
    region_members_map[rid] = ",".join(parts)

leader_override_str = "$leader_override"
leader_map = {}
if leader_override_str:
    for pair in leader_override_str.split(","):
        if ":" in pair:
            rid_str, nid_str = pair.split(":")
            leader_map[int(rid_str)] = int(nid_str)

leader_override_str = "$leader_override"
leader_map = {}
if leader_override_str:
    for pair in leader_override_str.split(","):
        if ":" in pair:
            rid_str, nid_str = pair.split(":")
            leader_map[int(rid_str)] = int(nid_str)

import os
total_cores = int(os.popen("nproc 2>/dev/null || sysctl -n hw.ncpu").read().strip())

nid = target["id"]
raft_port = int(target["address"].split(":")[-1])
api_port = target["api_port"]
node_regions = target["regions"]

env_parts = []
env_parts.append(f"MEC_NODE_ID={nid}")
env_parts.append(f"MEC_NODE_ADDRESS=127.0.0.1:{raft_port}")
env_parts.append(f"GRPC_PORT={api_port}")
env_parts.append(f"MEC_NODE_REGIONS={','.join(str(r) for r in node_regions)}")
env_parts.append("USE_MEMORY_SM=true")
env_parts.append(f"MEC_NODE_DATA_DIR=$RAFT_DATA_DIR/{raft_port}")
env_parts.append(f"NETWORK_DELAY_US=$NETWORK_DELAY_US")
env_parts.append("GOMAXPROCS=$GOMAXPROCS")
env_parts.append("PROPOSE_TIMEOUT_MS=$PROPOSE_TIMEOUT_MS")
env_parts.append(f"REGION_STATS_FILE=$run_dir/node_{nid}_region_stats_restart_$restart_label.jsonl")

for rid in node_regions:
    if rid in region_members_map:
        env_parts.append(f"CLUSTER_MEMBERS_{rid}={region_members_map[rid]}")

# 重启时不传 LEADER_REGIONS，避免触发 Leader Transfer 导致 TPS=0
# my_leader_regions = [str(rid) for rid, lnid in leader_map.items() if lnid == nid]
# if my_leader_regions:
#     env_parts.append(f"LEADER_REGIONS={','.join(my_leader_regions)}")

env_str = " ".join(env_parts)
log_suffix = "$restart_label"
cpu_start = nid * $CPU_LIMIT % total_cores
cpu_end = cpu_start + $CPU_LIMIT - 1
if cpu_end >= total_cores:
    cpu_end = total_cores - 1
cpu_range = f"{cpu_start}-{cpu_end}"
print(f'if [ "\$USE_TASKSET" = true ]; then')
print(f"  nohup env {env_str} taskset -c {cpu_range} \$SERVER_BIN > $run_dir/node_{nid}_restart_{log_suffix}.log 2>&1 &")
print(f"else")
print(f"  nohup env {env_str} \$SERVER_BIN > $run_dir/node_{nid}_restart_{log_suffix}.log 2>&1 &")
print(f"fi")
print(f"echo \$! > $run_dir/node_{nid}.pid")
print(f"SERVER_PIDS+=(\$!)")
PYEOF

    source /dev/shm/fault_recovery_restart_multi.sh
    rm -f /dev/shm/fault_recovery_restart_multi.sh
    log "  Node $node_id restarted"
}

#============================================================
# 多故障顺序实验（实验 A）
#============================================================
run_multi_fault_experiment() {
    local config_file="$1"
    local run_index="$2"
    local run_dir="$OUTPUT_DIR/run_${run_index}"

    mkdir -p "$run_dir"
    log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    log "  Run $run_index (multi-fault): $FAULT_EVENTS"
    log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    # 1. 清理 + 准备
    cleanup
    mkdir -p "$RAFT_DATA_DIR"
    sleep 5

    # 2. 启动集群
    start_cluster "$config_file" "$FORCE_LEADER" "$run_dir"
    wait_cluster_ready "$config_file"
    sleep 5

    # 3. 启动客户端（后台）
    local node_configs
    node_configs=$(generate_client_node_configs "$config_file")

    log "Starting fault-recovery-client (duration=${DURATION}s, concurrency=${CONCURRENCY})..."
    NODE_CONFIGS="$node_configs" \
    DURATION_SECONDS="$DURATION" \
    PROPOSAL_SIZE="$PROPOSAL_SIZE" \
    OUTPUT_FILE="$run_dir/tps_samples.json" \
    SINGLE_NODE_MODE="true" \
    nohup "$CLIENT_BIN" > "$run_dir/client.log" 2>&1 &
    CLIENT_PID=$!
    log "  Client started (PID=$CLIENT_PID)"

    # 4. 顺序执行多个故障事件
    local elapsed=0
    local fault_events_json="["
    local first_event=true

    IFS=',' read -ra EVENTS <<< "$FAULT_EVENTS"
    for event in "${EVENTS[@]}"; do
        IFS=':' read -r ev_node ev_delay ev_duration ev_role <<< "$event"

        # 等到该事件的触发时间
        local wait_for=$((ev_delay - elapsed))
        if [ $wait_for -gt 0 ]; then
            log "  Waiting ${wait_for}s until T=${ev_delay}s..."
            sleep "$wait_for"
        fi
        elapsed=$ev_delay

        # 故障注入
        local fault_epoch
        fault_epoch=$(date +%s)
        kill_node_by_id "$ev_node" "$run_dir"

        # 等待宕机时间
        log "  Node $ev_node (${ev_role}) down for ${ev_duration}s..."
        sleep "$ev_duration"
        elapsed=$((elapsed + ev_duration))

        # 重启
        local restart_epoch
        restart_epoch=$(date +%s)
        restart_node_by_id "$ev_node" "$config_file" "$FORCE_LEADER" "$run_dir" "$ev_role"

        # 收集事件 JSON
        if [ "$first_event" = true ]; then
            first_event=false
        else
            fault_events_json="${fault_events_json},"
        fi
        local restart_sec=$((ev_delay + ev_duration))
        fault_events_json="${fault_events_json}{\"node_id\":$ev_node,\"fault_sec\":$ev_delay,\"restart_sec\":$restart_sec,\"role\":\"$ev_role\",\"fault_epoch\":$fault_epoch,\"restart_epoch\":$restart_epoch}"
    done

    fault_events_json="${fault_events_json}]"

    # 5. 等待客户端完成
    log "  Waiting for client to finish..."
    wait "$CLIENT_PID" 2>/dev/null || true

    # 5.1 从服务端 region_stats 生成 tps_samples.json
    python3 "$SCRIPT_DIR/generate_tps_samples.py" --run-dir "$run_dir" 2>/dev/null || true

    # 6. 保存实验元信息
    cat > "$run_dir/experiment_meta.json" << METAEOF
{
    "multi_fault": true,
    "fault_events": $fault_events_json,
    "duration_sec": $DURATION,
    "concurrency": $CONCURRENCY,
    "proposal_size": $PROPOSAL_SIZE,
    "config_file": "$config_file",
    "force_leader": "$FORCE_LEADER",
    "synthetic_mode": $( [ "$SYNTHETIC_MODE" = "true" ] && echo "true" || echo "false" ),
    "run_index": $run_index,
    "network_delay_us": $NETWORK_DELAY_US
}
METAEOF

    log "  Run $run_index complete -> $run_dir/"
}

#============================================================
# 单次实验（单故障模式，向后兼容）
#============================================================
run_single_experiment() {
    local config_file="$1"
    local run_index="$2"
    local run_dir="$OUTPUT_DIR/run_${run_index}"

    mkdir -p "$run_dir"
    log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    log "  Run $run_index: fault_node=$FAULT_NODE_ID, delay=${FAULT_DELAY}s, down=${FAULT_DURATION}s"
    log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    # 1. 清理 + 准备
    cleanup
    mkdir -p "$RAFT_DATA_DIR"
    sleep 5

    # 2. 启动集群
    start_cluster "$config_file" "$FORCE_LEADER" "$run_dir"
    wait_cluster_ready "$config_file"
    sleep 5

    # 3. 启动客户端（后台）
    local node_configs
    node_configs=$(generate_client_node_configs "$config_file")

    log "Starting fault-recovery-client (duration=${DURATION}s, concurrency=${CONCURRENCY})..."
    NODE_CONFIGS="$node_configs" \
    DURATION_SECONDS="$DURATION" \
    PROPOSAL_SIZE="$PROPOSAL_SIZE" \
    OUTPUT_FILE="$run_dir/tps_samples.json" \
    SINGLE_NODE_MODE="true" \
    nohup "$CLIENT_BIN" > "$run_dir/client.log" 2>&1 &
    CLIENT_PID=$!
    log "  Client started (PID=$CLIENT_PID)"

    # 4. 等待 FAULT_DELAY 秒
    log "  Waiting ${FAULT_DELAY}s before fault injection..."
    sleep "$FAULT_DELAY"

    # 5. 故障注入 (kill -9)
    local fault_time
    fault_time=$(date +%s)
    if [ -n "$TARGET_PID" ] && kill -0 "$TARGET_PID" 2>/dev/null; then
        log "  FAULT INJECTION: kill -9 node $FAULT_NODE_ID (PID=$TARGET_PID)"
        kill -9 "$TARGET_PID" 2>/dev/null || true
    else
        log "  WARNING: Target PID ($TARGET_PID) not running!"
    fi

    # 6. 等待 FAULT_DURATION 秒
    log "  Node down for ${FAULT_DURATION}s..."
    sleep "$FAULT_DURATION"

    # 7. 重启故障节点（保留数据目录！）
    local restart_time
    restart_time=$(date +%s)
    restart_fault_node "$config_file" "$FORCE_LEADER" "$run_dir"

    # 8. 等待客户端完成
    log "  Waiting for client to finish..."
    wait "$CLIENT_PID" 2>/dev/null || true

    # 8.1 从服务端 region_stats 生成 tps_samples.json
    python3 "$SCRIPT_DIR/generate_tps_samples.py" --run-dir "$run_dir" 2>/dev/null || true

    # 9. 保存实验元信息
    cat > "$run_dir/experiment_meta.json" << METAEOF
{
    "fault_node_id": $FAULT_NODE_ID,
    "fault_delay_sec": $FAULT_DELAY,
    "fault_duration_sec": $FAULT_DURATION,
    "fault_time_epoch": $fault_time,
    "restart_time_epoch": $restart_time,
    "fault_time_sec": $FAULT_DELAY,
    "restart_time_sec": $((FAULT_DELAY + FAULT_DURATION)),
    "duration_sec": $DURATION,
    "concurrency": $CONCURRENCY,
    "proposal_size": $PROPOSAL_SIZE,
    "config_file": "$config_file",
    "force_leader": "$FORCE_LEADER",
    "synthetic_mode": $( [ "$SYNTHETIC_MODE" = "true" ] && echo "true" || echo "false" ),
    "run_index": $run_index,
    "network_delay_us": $NETWORK_DELAY_US
}
METAEOF

    log "  Run $run_index complete -> $run_dir/"
}

#============================================================
# 主流程
#============================================================
main() {
    log "╔══════════════════════════════════════════════════════════╗"
    log "║  Fault Recovery Experiment (Native Mode)                ║"
    log "╚══════════════════════════════════════════════════════════╝"

    # 检查二进制
    if [ ! -f "$SERVER_BIN" ]; then
        log "ERROR: Server binary not found: $SERVER_BIN"
        log "  Run: go build -o fault-recovery-server ./cmd/fault-recovery-server/"
        exit 1
    fi
    if [ ! -f "$CLIENT_BIN" ]; then
        log "ERROR: Client binary not found: $CLIENT_BIN"
        log "  Run: go build -o fault-recovery-client ./cmd/fault-recovery-client/"
        exit 1
    fi

    # 确定配置文件
    local effective_config="$CONFIG_FILE"

    if [ "$SYNTHETIC_MODE" = "true" ]; then
        log "  Mode: Synthetic (5 nodes x $SYNTHETIC_REGIONS regions)"
        effective_config="$OUTPUT_DIR/synthetic_config.yaml"
        mkdir -p "$OUTPUT_DIR"
        generate_synthetic_config "$SYNTHETIC_REGIONS" "$effective_config"

        # 合成模式：Node 0 是 leader of ceil(N/2) regions
        local leader_count=$(( (SYNTHETIC_REGIONS + 1) / 2 ))
        local leader_regions=""
        for ((r=1; r<=leader_count; r++)); do
            [ -n "$leader_regions" ] && leader_regions="${leader_regions},"
            leader_regions="${leader_regions}${r}:${FAULT_NODE_ID}"
        done
        FORCE_LEADER="$leader_regions"
        log "  Synthetic leader placement: $FORCE_LEADER"
    else
        if [ -z "$effective_config" ]; then
            log "ERROR: --config is required (or use --synthetic)"
            exit 1
        fi
        log "  Mode: Real topology ($effective_config)"
    fi

    local multi_fault_mode="false"
    if [ -n "$FAULT_EVENTS" ]; then
        multi_fault_mode="true"
        log "  Mode detail:    MULTI-FAULT sequential"
        log "  Fault events:   $FAULT_EVENTS"
    else
        log "  Fault node:     $FAULT_NODE_ID"
        log "  Fault delay:    ${FAULT_DELAY}s"
        log "  Fault duration: ${FAULT_DURATION}s"
    fi
    log "  Client duration: ${DURATION}s"
    log "  Concurrency:    $CONCURRENCY"
    log "  Force leader:   ${FORCE_LEADER:-none}"
    log "  Repeats:        $REPEAT"
    log "  Output:         $OUTPUT_DIR"

    mkdir -p "$OUTPUT_DIR"

    # 复制配置到输出目录
    cp "$effective_config" "$OUTPUT_DIR/config.yaml"

    for ((i=1; i<=REPEAT; i++)); do
        log ""
        log "════════════════════════════════════════════════════════"
        log "  REPEAT $i / $REPEAT"
        log "════════════════════════════════════════════════════════"
        if [ "$multi_fault_mode" = "true" ]; then
            run_multi_fault_experiment "$effective_config" "$i"
        else
            run_single_experiment "$effective_config" "$i"
        fi
    done

    cleanup

    log ""
    log "═══════════════════════════════════════════════════════"
    log "  All $REPEAT runs completed"
    log "  Results in: $OUTPUT_DIR/"
    log "═══════════════════════════════════════════════════════"
}

main

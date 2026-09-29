#!/bin/bash

#============================================================
# 全拓扑压测脚本（Native 模式 - 无 Docker）
# 
# 功能：
#   - 测试完整的15节点拓扑性能（直接进程，无需 Docker）
#   - 每个节点向其所有region发送请求
#   - 通过 taskset + GOMAXPROCS 限制每个进程的 CPU
#   - 适用于 AutoDL 等无 Docker 权限的服务器
#
# 用法：
#   ./test-full-topology-native.sh [-m method] [-c concurrency] [-d duration]
#
# 示例：
#   ./test-full-topology-native.sh                         # 使用默认配置
#   ./test-full-topology-native.sh -m threshold            # 只测试threshold方法
#   ./test-full-topology-native.sh -c "100" -d 90          # 所有节点100并发，90秒
#   ./test-full-topology-native.sh --cpu-limit 4           # 每进程限4核
#   ./test-full-topology-native.sh --use-shm               # AutoDL：数据放 /dev/shm
#   ./test-full-topology-native.sh --data-dir /path/to/data  # 自定义数据目录
#============================================================

set -e

#============================================================
# Python 命令检测
#============================================================
if command -v python3 &> /dev/null; then
    PYTHON_CMD="python3"
elif command -v python &> /dev/null; then
    PYTHON_CMD="python"
else
    echo "ERROR: Python not found"
    exit 1
fi

#============================================================
# 默认配置
#============================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Raft 运行时数据目录（默认磁盘；AutoDL 根分区紧张时用 --use-shm）
RAFT_DATA_DIR="$PROJECT_DIR/runtime/raft-data"

# 服务端和客户端二进制
SERVER_BIN="$PROJECT_DIR/mec-node-grpc-full-topology"
CLIENT_BIN="$PROJECT_DIR/grpc-client-full-topology"

# 测试方法
declare -A METHODS
METHODS["global"]="Global (1 Region)"
METHODS["neighbor"]="Neighbor (9 Regions)"
METHODS["clique"]="Clique (11 Regions)"
METHODS["threshold"]="Threshold (5 Regions)"

# 方法对应的配置文件
declare -A METHOD_CONFIG_FILES
METHOD_CONFIG_FILES["global"]="$PROJECT_DIR/docker/configs/method_global.yaml"
METHOD_CONFIG_FILES["neighbor"]="$PROJECT_DIR/docker/configs/method_neighbor_based.yaml"
METHOD_CONFIG_FILES["clique"]="$PROJECT_DIR/docker/configs/method_maximal_clique.yaml"
METHOD_CONFIG_FILES["threshold"]="$PROJECT_DIR/docker/configs/method_threshold_based.yaml"

# 方法对应的节点数和Region数
declare -A NODE_COUNTS
NODE_COUNTS["global"]=15
NODE_COUNTS["neighbor"]=15
NODE_COUNTS["clique"]=15
NODE_COUNTS["threshold"]=15

declare -A REGION_COUNTS
REGION_COUNTS["global"]=1
REGION_COUNTS["neighbor"]=9
REGION_COUNTS["clique"]=11
REGION_COUNTS["threshold"]=5

# 默认测试所有方法
METHODS_TO_TEST=("threshold" "neighbor" "clique" "global")

# 并发配置
CONCURRENCY_CONFIGS=(
    "0:30,1:30,2:30,3:30,4:30,5:30,6:30,7:30,8:30,9:30,10:30,11:30,12:30,13:30,14:30"
)

DEFAULT_DURATION=40
DEFAULT_PROPOSAL_SIZE=128
QPS_CONFIGS=(0)

# Leader放置策略
declare -A METHOD_LEADER_PLACEMENTS
METHOD_LEADER_PLACEMENTS["global"]="1:0"
METHOD_LEADER_PLACEMENTS["neighbor"]="1:12,2:2,3:11,4:12,5:12,6:12,7:1,8:11,9:11"
METHOD_LEADER_PLACEMENTS["clique"]="1:2,2:6,3:1,4:12,5:1,6:12,7:12,8:4,9:4,10:5,11:6"
METHOD_LEADER_PLACEMENTS["threshold"]="1:11,2:12,3:1,4:12,5:11"

# 状态机类型
USE_MEMORY_SM=true

# 资源限制（Native 模式）
CPU_LIMIT=2          # 每个进程限制的 CPU 核数
GOMAXPROCS=4         # Go 运行时最大线程数
MEMORY_LIMIT="1G"    # 仅记录，native 模式不强制内存限制
NETWORK_DELAY_MS=0   # 模拟网络延迟（毫秒），0=不模拟
API_DELAY_MS=0       # 模拟客户端到服务端延迟（毫秒），0=不模拟

# 延迟配置数组，格式: "raft_delay:api_delay"（毫秒），多组用空格分隔
# 0-2ms 按 0.2ms 增加，2-10ms 按 1ms 增加，10-30ms 按 2ms 增加
DELAY_CONFIGS=(
    "0:0"
    "0.2:0" "0.4:0" "0.6:0" "0.8:0" "1.0:0" "1.2:0" "1.4:0" "1.6:0" "1.8:0" "2.0:0"
    "3:0" "4:0" "5:0" "6:0" "7:0" "8:0" "9:0" "10:0"
    "12:0" "14:0" "16:0" "18:0" "20:0" "22:0" "24:0" "26:0" "28:0" "30:0"
)

#============================================================
# 参数解析
#============================================================

TEST_DURATION=$DEFAULT_DURATION
PROPOSAL_SIZE=$DEFAULT_PROPOSAL_SIZE

while [[ $# -gt 0 ]]; do
    case "$1" in
        -m|--method)
            METHODS_TO_TEST=("$2")
            shift 2
            ;;
        -c|--concurrency)
            IFS=';' read -ra CONCURRENCY_CONFIGS <<< "$2"
            shift 2
            ;;
        -d|--duration)
            TEST_DURATION="$2"
            shift 2
            ;;
        -p|--proposal-size)
            PROPOSAL_SIZE="$2"
            shift 2
            ;;
        --cpu-limit)
            CPU_LIMIT="$2"
            shift 2
            ;;
        --gomaxprocs)
            GOMAXPROCS="$2"
            shift 2
            ;;
        --network-delay)
            NETWORK_DELAY_MS="$2"
            shift 2
            ;;
        --api-delay)
            API_DELAY_MS="$2"
            shift 2
            ;;
        --delay-configs)
            IFS=';' read -ra DELAY_CONFIGS <<< "$2"
            shift 2
            ;;
        --use-shm)
            RAFT_DATA_DIR="/dev/shm/raft-data"
            shift
            ;;
        --data-dir)
            RAFT_DATA_DIR="$2"
            shift 2
            ;;
        -q|--qps)
            IFS=';' read -ra QPS_CONFIGS <<< "$2"
            shift 2
            ;;
        -l|--leader)
            DEFAULT_LEADER_PLACEMENT="$2"
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

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

print_info() { echo -e "${BLUE}ℹ️  $1${NC}"; }
print_success() { echo -e "${GREEN}✓ $1${NC}"; }
print_error() { echo -e "${RED}✗ $1${NC}"; }
print_step() { echo -e "${YELLOW}▶ $1${NC}"; }

log() { echo "[$(date '+%H:%M:%S')] $*"; }

# PID 管理
declare -a SERVER_PIDS=()
declare -a CLIENT_PIDS=()

cleanup() {
    print_step "清理进程..."
    for pid in "${CLIENT_PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
    done
    for pid in "${SERVER_PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
    done
    CLIENT_PIDS=()
    SERVER_PIDS=()
    sleep 1
    # 强制杀所有相关进程
    pkill -9 -f "mec-node-grpc-full-topology" 2>/dev/null || true
    pkill -9 -f "grpc-client-full-topology" 2>/dev/null || true
    sleep 2
}

trap cleanup EXIT

#============================================================
# 网络延迟管理（tc-delay.sh 优先，fallback 到应用层）
#============================================================

TC_DELAY_SCRIPT="$SCRIPT_DIR/tc-delay.sh"
USE_TC_DELAY=false

setup_network_delay() {
    local raft_delay=$1
    local api_delay=$2

    # 检查是否都为 0（用 Python 做浮点比较，兼容无 bc 环境）
    local skip=$($PYTHON_CMD -c "print(1 if float('${raft_delay}') == 0 and float('${api_delay}') == 0 else 0)" 2>/dev/null || echo "0")
    if [ "$skip" = "1" ]; then
        return
    fi

    # tc 只支持整数 ms，亚毫秒延迟只能用 delay_transport
    local raft_int=$($PYTHON_CMD -c "print(int(round(float('${raft_delay}'))))" 2>/dev/null || echo "0")
    local api_int=$($PYTHON_CMD -c "print(int(round(float('${api_delay}'))))" 2>/dev/null || echo "0")

    if [ "$raft_int" -gt 0 ] || [ "$api_int" -gt 0 ]; then
        if [ -x "$TC_DELAY_SCRIPT" ] && \
           sudo -n "$TC_DELAY_SCRIPT" setup --raft-delay "$raft_int" --api-delay "$api_int" 2>/dev/null; then
            USE_TC_DELAY=true
            print_success "  tc 延迟已设置: raft=${raft_int}ms, api=${api_int}ms"
            return
        fi
    fi

    USE_TC_DELAY=false
    if [ "$raft_delay" != "0" ] && [ "$raft_delay" != "0.0" ]; then
        print_info "  使用应用层延迟注入: raft=${raft_delay}ms"
    fi
}

teardown_network_delay() {
    if [ "$USE_TC_DELAY" = true ]; then
        sudo -n "$TC_DELAY_SCRIPT" teardown 2>/dev/null || true
        USE_TC_DELAY=false
    fi
}

# 确保退出时清理 tc 规则
cleanup_all() {
    teardown_network_delay
    cleanup
}
trap cleanup_all EXIT

# 计算 taskset CPU 范围
# 给每个进程分配不同的 CPU 核心范围，避免争抢
get_cpu_range() {
    local process_idx=$1
    local cores_per_process=$CPU_LIMIT
    local total_cores=$(nproc)
    
    local start_core=$(( (process_idx * cores_per_process) % total_cores ))
    local end_core=$(( start_core + cores_per_process - 1 ))
    if [ $end_core -ge $total_cores ]; then
        end_core=$((total_cores - 1))
    fi
    echo "${start_core}-${end_core}"
}

# 检测 taskset 是否可用（容器环境可能不允许设置 CPU 亲和性）
USE_TASKSET=false
if command -v taskset &>/dev/null; then
    if taskset -c 0 true 2>/dev/null; then
        USE_TASKSET=true
    fi
fi

#============================================================
# 启动集群
#============================================================

start_cluster() {
    local config_file="$1"
    local leader_placement="$2"
    local output_dir="$3"

    print_step "启动 MEC 集群 (native mode)..."

    # 用 Python 解析配置并生成启动命令
    local startup_script
    startup_script=$($PYTHON_CMD << PYEOF
import yaml, json

with open("$config_file") as f:
    cfg = yaml.safe_load(f)

nodes = cfg["mec_nodes"]
regions = cfg.get("regions", [])

# 从 nodes 的 regions 字段反推 region -> members 映射
region_member_ids = {}
node_port_map = {}
for n in nodes:
    port = int(n["address"].split(":")[-1])
    node_port_map[n["id"]] = port
    for rid in n["regions"]:
        region_member_ids.setdefault(rid, []).append(n["id"])

region_members_map = {}
for rid, member_ids in region_member_ids.items():
    parts = []
    for nid in member_ids:
        raft_node_id = rid * 100 + nid
        port = node_port_map[nid]
        parts.append(f"{raft_node_id}:127.0.0.1:{port}")
    region_members_map[rid] = ",".join(parts)

# Leader placement
leader_map = {}
leader_str = "$leader_placement"
if leader_str:
    for pair in leader_str.split(","):
        if ":" in pair:
            rid_str, nid_str = pair.split(":")
            leader_map[int(rid_str)] = int(nid_str)

# 输出每个节点的启动命令
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
    env_parts.append(f"USE_MEMORY_SM=$USE_MEMORY_SM")
    env_parts.append(f"MEC_NODE_DATA_DIR=$RAFT_DATA_DIR/{raft_port}")
    env_parts.append(f"GOMAXPROCS=$GOMAXPROCS")
    if "$USE_TC_DELAY" != "true":
        delay_ms_str = "$NETWORK_DELAY_MS"
        delay_us = int(float(delay_ms_str) * 1000) if delay_ms_str else 0
        env_parts.append(f"NETWORK_DELAY_US={delay_us}")
    else:
        env_parts.append("NETWORK_DELAY_US=0")

    for rid in node_regions:
        if rid in region_members_map:
            env_parts.append(f"CLUSTER_MEMBERS_{rid}={region_members_map[rid]}")

    my_leader_regions = [str(rid) for rid, lnid in leader_map.items() if lnid == nid]
    if my_leader_regions:
        env_parts.append(f"LEADER_REGIONS={','.join(my_leader_regions)}")

    env_str = " ".join(env_parts)
    print(f"{env_str}")
PYEOF
    )

    # 启动每个节点
    local idx=0
    SERVER_PIDS=()
    while IFS= read -r env_line; do
        local cpu_range=$(get_cpu_range $idx)
        local node_id=$(echo "$env_line" | grep -o "MEC_NODE_ID=[0-9]*" | cut -d= -f2)
        local log_file="$output_dir/node_${node_id}.log"

        if [ "$USE_TASKSET" = true ]; then
            env $env_line taskset -c "$cpu_range" "$SERVER_BIN" > "$log_file" 2>&1 &
        else
            env $env_line "$SERVER_BIN" > "$log_file" 2>&1 &
        fi
        SERVER_PIDS+=($!)
        idx=$((idx + 1))
    done <<< "$startup_script"

    print_info "  启动了 ${#SERVER_PIDS[@]} 个 MEC 节点 (CPU: ${CPU_LIMIT}核/进程, GOMAXPROCS=${GOMAXPROCS})"
}

wait_cluster_ready() {
    local config_file="$1"
    print_step "等待集群就绪..."

    local all_ports
    all_ports=$($PYTHON_CMD -c "
import yaml
with open('$config_file') as f:
    cfg = yaml.safe_load(f)
print(' '.join(str(n['api_port']) for n in cfg['mec_nodes']))
")

    local max_wait=60
    local waited=0
    while [ $waited -lt $max_wait ]; do
        local all_ready=true
        for port in $all_ports; do
            if ! timeout 1 bash -c "echo > /dev/tcp/127.0.0.1/$port" 2>/dev/null; then
                all_ready=false
                break
            fi
        done
        if [ "$all_ready" = true ]; then
            print_success "  集群就绪 (${waited}s)"
            # 等待 Leader 转移
            sleep 8
            print_success "  Leader 转移完成"
            return 0
        fi
        sleep 2
        waited=$((waited + 2))
    done

    print_error "  集群未就绪 (${max_wait}s 超时)"
    return 1
}

#============================================================
# 运行压测
#============================================================

run_benchmark() {
    local method="$1"
    local concurrency="$2"
    local target_qps="$3"
    local output_dir="$4"
    local raft_delay="${5:-0}"
    local api_delay="${6:-0}"
    local config_file="${METHOD_CONFIG_FILES[$method]}"
    local leader_placement="${METHOD_LEADER_PLACEMENTS[$method]}"

    if [ -n "$DEFAULT_LEADER_PLACEMENT" ]; then
        leader_placement="$DEFAULT_LEADER_PLACEMENT"
    fi

    local concurrency_safe=$(echo "$concurrency" | tr ':' '-' | tr ',' '_')
    local qps_label="q${target_qps}"
    local delay_label="d${raft_delay}r${api_delay}a"
    local run_dir="$output_dir/${method}-c${concurrency_safe}-${qps_label}-${delay_label}"
    mkdir -p "$run_dir"

    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    print_step "测试: ${METHODS[$method]} @ 并发=${concurrency} QPS=${target_qps} Delay=raft:${raft_delay}ms,api:${api_delay}ms"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    print_info "  配置: $config_file"
    print_info "  Leader: $leader_placement"
    print_info "  CPU限制: ${CPU_LIMIT}核/进程, GOMAXPROCS=${GOMAXPROCS}"
    print_info "  时长: ${TEST_DURATION}s"

    # 清理旧进程
    cleanup

    # 清理 Raft 数据目录（避免 bootstrap 冲突）
    rm -rf "$RAFT_DATA_DIR" 2>/dev/null || true
    rm -rf /dev/shm/raft-data 2>/dev/null || true
    rm -rf /tmp/mec-node-* 2>/dev/null || true
    rm -rf /data/mec-node-* /data/mec_node-* /data 2>/dev/null || true
    mkdir -p "$RAFT_DATA_DIR"
    echo "    [数据目录已清理: $RAFT_DATA_DIR]"

    # 等待端口释放（TIME_WAIT）
    print_step "等待端口释放..."
    local first_api_port
    first_api_port=$($PYTHON_CMD -c "
import yaml
with open('$config_file') as f:
    cfg = yaml.safe_load(f)
print(cfg['mec_nodes'][0]['api_port'])
")
    local port_wait=0
    while [ $port_wait -lt 30 ]; do
        if ! ss -tlnp 2>/dev/null | grep -q ":${first_api_port} " && \
           ! ss -tnp 2>/dev/null | grep -q ":${first_api_port} "; then
            break
        fi
        sleep 2
        port_wait=$((port_wait + 2))
    done
    if [ $port_wait -gt 0 ]; then
        print_info "    端口等待了 ${port_wait}s"
    fi

    # 设置网络延迟（tc 优先）
    setup_network_delay "$raft_delay" "$api_delay"

    # 启动集群
    start_cluster "$config_file" "$leader_placement" "$run_dir"
    wait_cluster_ready "$config_file" || return 1

    # 生成客户端启动参数
    print_step "启动压测客户端..."

    local client_configs
    client_configs=$($PYTHON_CMD << PYEOF
import yaml

with open("$config_file") as f:
    cfg = yaml.safe_load(f)

nodes = cfg["mec_nodes"]

# 解析并发配置
concurrency_str = "$concurrency"
node_concurrency = {}
if ':' in concurrency_str:
    for item in concurrency_str.split(','):
        parts = item.strip().split(':')
        nid = int(parts[0])
        c = int(parts[1])
        node_concurrency[nid] = c
else:
    default_c = int(concurrency_str)
    for n in nodes:
        node_concurrency[n["id"]] = default_c

target_qps = $target_qps

# 构建所有节点的 NODE_CONFIGS（分号分隔）
# 格式: nodeID:host:port:regions:concurrency:qps
configs = []
for n in nodes:
    nid = n["id"]
    c = node_concurrency.get(nid, 0)
    if c > 0:
        port = n["api_port"]
        regs = ",".join(str(r) for r in n["regions"])
        configs.append(f"{nid}:127.0.0.1:{port}:{regs}:{c}:{target_qps}")
print(";".join(configs))
PYEOF
    )

    # 单客户端模式：一个进程连接所有节点，不限 CPU
    CLIENT_PIDS=()
    local results_dir="$run_dir/results"
    mkdir -p "$results_dir"

    local client_log="$run_dir/client.log"
    local result_file="$results_dir/all_nodes_result.json"
    local wrapper="$run_dir/run_client.sh"

    cat > "$wrapper" << WRAPPER_EOF
#!/bin/bash
export NODE_CONFIGS="$client_configs"
export DURATION_SECONDS="$TEST_DURATION"
export PROPOSAL_SIZE="$PROPOSAL_SIZE"
export SINGLE_NODE_MODE="true"
export OUTPUT_FILE="$result_file"
exec "$CLIENT_BIN"
WRAPPER_EOF
    chmod +x "$wrapper"
    bash "$wrapper" > "$client_log" 2>&1 &
    CLIENT_PIDS+=($!)

    print_info "  启动了 1 个客户端进程（连接 ${#CONCURRENCY_CONFIGS[@]} 节点，不限 CPU）"

    # 等待客户端完成
    print_step "等待测试完成 (预计 ${TEST_DURATION}s)..."
    local wait_time=$((TEST_DURATION + 30))
    local elapsed=0

    while [ $elapsed -lt $wait_time ]; do
        local running=0
        for pid in "${CLIENT_PIDS[@]}"; do
            if kill -0 "$pid" 2>/dev/null; then
                running=$((running + 1))
            fi
        done

        if [ $running -eq 0 ]; then
            print_success "  测试完成（所有客户端已退出）"
            break
        fi

        sleep 5
        elapsed=$((elapsed + 5))
        printf "\r  已等待: ${elapsed}s / ${wait_time}s (剩余客户端: ${running})  "
    done
    echo ""

    # 停止 MEC 节点（触发统计报告输出）
    print_step "停止 MEC 节点..."
    for pid in "${SERVER_PIDS[@]}"; do
        kill -TERM "$pid" 2>/dev/null || true
    done
    sleep 3

    # 汇总结果
    print_step "汇总测试结果..."
    if ls "$results_dir"/*.json 1>/dev/null 2>&1; then
        $PYTHON_CMD << PYEOF
import json, glob, os

results_dir = "$results_dir"
files = sorted(glob.glob(os.path.join(results_dir, "*.json")))

total_ops = 0
total_success = 0
total_errors = 0
total_duration_ms = 0
latencies = []
tps_sum = 0

for f in files:
    try:
        with open(f) as fp:
            r = json.load(fp)
        # 结果可能是数组或对象
        if isinstance(r, list):
            for item in r:
                total_ops += item.get("total_requests", 0)
                total_success += item.get("success_requests", 0)
                total_errors += item.get("failed_requests", 0)
                if "avg_latency_ms" in item:
                    latencies.append(item["avg_latency_ms"] * 1000)  # ms -> us
                if "duration_seconds" in item:
                    total_duration_ms = max(total_duration_ms, int(item["duration_seconds"] * 1000))
                if "tps" in item:
                    tps_sum += item["tps"]
        elif isinstance(r, dict):
            total_ops += r.get("total_ops", r.get("total_requests", 0))
            total_success += r.get("success", r.get("success_requests", 0))
            total_errors += r.get("errors", r.get("failed_requests", 0))
            if "avg_latency_us" in r:
                latencies.append(r["avg_latency_us"])
            elif "avg_latency_ms" in r:
                latencies.append(r["avg_latency_ms"] * 1000)
            if "duration_ms" in r:
                total_duration_ms = max(total_duration_ms, r["duration_ms"])
            elif "duration_seconds" in r:
                total_duration_ms = max(total_duration_ms, int(r["duration_seconds"] * 1000))
            if "tps" in r:
                tps_sum += r["tps"]
    except:
        pass

if total_ops > 0:
    duration_s = total_duration_ms / 1000 if total_duration_ms > 0 else $TEST_DURATION
    tps = tps_sum if tps_sum > 0 else (total_success / duration_s if duration_s > 0 else 0)
    avg_lat = sum(latencies) / len(latencies) / 1000 if latencies else 0
    success_rate = total_success / total_ops * 100 if total_ops > 0 else 0

    print(f"")
    print(f"  ╔══════════════════════════════════════════════╗")
    print(f"  ║  结果: $method                               ║")
    print(f"  ╠══════════════════════════════════════════════╣")
    print(f"  ║  TPS:        {tps:>10.0f} ops/s              ║")
    print(f"  ║  总请求:     {total_ops:>10}                 ║")
    print(f"  ║  成功:       {total_success:>10} ({success_rate:.1f}%)     ║")
    print(f"  ║  失败:       {total_errors:>10}                 ║")
    print(f"  ║  平均延迟:   {avg_lat:>10.2f} ms              ║")
    print(f"  ╚══════════════════════════════════════════════╝")
    print(f"")

    # 保存汇总
    summary = {
        "method": "$method",
        "concurrency": "$concurrency",
        "target_qps": $target_qps,
        "raft_delay_ms": $raft_delay,
        "api_delay_ms": $api_delay,
        "duration_s": duration_s,
        "total_ops": total_ops,
        "success": total_success,
        "errors": total_errors,
        "tps": tps,
        "avg_latency_ms": avg_lat,
        "success_rate": success_rate,
        "cpu_limit": $CPU_LIMIT,
        "gomaxprocs": $GOMAXPROCS,
    }
    with open("$run_dir/summary.json", "w") as fp:
        json.dump(summary, fp, indent=2)
    print(f"  汇总保存到: $run_dir/summary.json")
else:
    print("  ⚠ 未找到有效结果")
PYEOF
    else
        print_error "  未找到结果文件"
    fi

    # 清理
    teardown_network_delay
    cleanup
    sleep 5

    return 0
}

#============================================================
# 主流程
#============================================================

main() {
    echo "╔════════════════════════════════════════════════════════════╗"
    echo "║       全拓扑压测 - Native 模式（无 Docker）                ║"
    echo "╚════════════════════════════════════════════════════════════╝"
    echo ""

    # 检查二进制
    if [ ! -f "$SERVER_BIN" ]; then
        print_step "编译服务端..."
        cd "$PROJECT_DIR"
        go build -o "$SERVER_BIN" ./cmd/mec-node-grpc-full-topology/
    fi
    if [ ! -f "$CLIENT_BIN" ]; then
        print_step "编译客户端..."
        cd "$PROJECT_DIR"
        go build -o "$CLIENT_BIN" ./cmd/grpc-client-full-topology/ 2>/dev/null || {
            print_error "客户端编译失败（可能不存在 cmd/grpc-client-full-topology/）"
            exit 1
        }
    fi
    print_success "二进制就绪"

    # 创建结果目录
    RESULTS_DIR="$PROJECT_DIR/benchmark-results/native-$(date +%Y%m%d_%H%M%S)"
    mkdir -p "$RESULTS_DIR"

    # 兼容旧参数：如果用了 --network-delay 或 --api-delay 但没用 --delay-configs
    local has_delay=$($PYTHON_CMD -c "print(1 if float('${NETWORK_DELAY_MS}') > 0 or float('${API_DELAY_MS}') > 0 else 0)" 2>/dev/null || echo "0")
    if [ "$has_delay" = "1" ]; then
        if [ "${DELAY_CONFIGS[0]}" = "0:0" ] && [ ${#DELAY_CONFIGS[@]} -eq 1 ]; then
            DELAY_CONFIGS=("${NETWORK_DELAY_MS}:${API_DELAY_MS}")
        fi
    fi

    print_info "测试配置:"
    print_info "  - 方法: ${METHODS_TO_TEST[*]}"
    print_info "  - 时长: ${TEST_DURATION}s"
    print_info "  - CPU限制: ${CPU_LIMIT}核/进程"
    print_info "  - GOMAXPROCS: ${GOMAXPROCS}"
    print_info "  - QPS: ${QPS_CONFIGS[*]} (0=不限制)"
    print_info "  - 并发: ${CONCURRENCY_CONFIGS[*]}"
    print_info "  - 延迟组: ${DELAY_CONFIGS[*]} (raft:api ms)"
    print_info "  - 数据目录: $RAFT_DATA_DIR"
    print_info "  - 结果: $RESULTS_DIR"
    echo ""

    local total_tests=0
    local success_tests=0

    for method in "${METHODS_TO_TEST[@]}"; do
        local config_file="${METHOD_CONFIG_FILES[$method]}"
        if [ ! -f "$config_file" ]; then
            print_error "配置文件不存在: $config_file (跳过 $method)"
            continue
        fi

        for concurrency in "${CONCURRENCY_CONFIGS[@]}"; do
            for qps in "${QPS_CONFIGS[@]}"; do
                for delay_cfg in "${DELAY_CONFIGS[@]}"; do
                    local raft_delay="${delay_cfg%%:*}"
                    local api_delay="${delay_cfg##*:}"
                    # 更新全局变量供 start_cluster 使用
                    NETWORK_DELAY_MS="$raft_delay"
                    total_tests=$((total_tests + 1))
                    if run_benchmark "$method" "$concurrency" "$qps" "$RESULTS_DIR" "$raft_delay" "$api_delay"; then
                        success_tests=$((success_tests + 1))
                    fi
                done
            done
        done
    done

    echo ""
    echo "═══════════════════════════════════════════════════════════"
    print_success "完成: $success_tests/$total_tests 测试成功"
    print_info "结果目录: $RESULTS_DIR"
    echo "═══════════════════════════════════════════════════════════"
}

main

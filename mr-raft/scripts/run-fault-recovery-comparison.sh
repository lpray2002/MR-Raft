#!/usr/bin/env bash
#============================================================
# 故障恢复对比实验：4 种分区算法，连续注入不同角色故障
#
# 用法:
#   bash scripts/run-fault-recovery-comparison.sh [--use-shm] [--repeat N]
#
# 每种方法注入 3 次故障（Follower → Light Leader → Heavy Leader），
# 展示分区粒度与故障影响面的 trade-off。
#============================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# ══════════════════════════════════════════════════════════════
# 统一实验参数
# ══════════════════════════════════════════════════════════════
DURATION=75
CONCURRENCY=30
REPEAT=3
CPU_LIMIT=2
GOMAXPROCS=4
NETWORK_DELAY_US=0
PROPOSE_TIMEOUT_MS=5000
PRE_STABLE=10
POST_STABLE=19
EXTRA_ARGS=""

# ── 各方法配置 ──
METHODS=("global" "threshold" "clique" "neighbor")

CONFIG_global="$PROJECT_DIR/docker/configs/method_global.yaml"
CONFIG_threshold="$PROJECT_DIR/docker/configs/method_threshold_based.yaml"
CONFIG_clique="$PROJECT_DIR/docker/configs/method_maximal_clique.yaml"
CONFIG_neighbor="$PROJECT_DIR/docker/configs/method_neighbor_based.yaml"

LEADER_global="1:0"
LEADER_threshold="1:11,2:12,3:1,4:12,5:11"
LEADER_clique="1:2,2:6,3:1,4:12,5:1,6:12,7:12,8:4,9:4,10:5,11:6"
LEADER_neighbor="1:12,2:2,3:11,4:12,5:12,6:12,7:1,8:11,9:11"

# 故障事件: "node_id:delay_sec:duration_sec:role"
# Global:    node12(Follower) → node5(Follower) → node0(Leader of 1R)
# Threshold: node2(Follower)  → node1(Leader 1R) → node12(Leader 2R)
# Clique:    node9(Follower)  → node6(Leader 2R) → node12(Leader 3R)
# Neighbor:  node0(Follower)  → node11(Leader 3R) → node12(Leader 4R)
EVENTS_global="12:40:3:Follower,5:50:3:Follower,0:60:3:Leader-1R"
EVENTS_threshold="2:40:3:Follower,1:50:3:Leader-1R,12:60:3:Leader-2R"
EVENTS_clique="9:40:3:Follower,6:50:3:Leader-2R,12:60:3:Leader-3R"
EVENTS_neighbor="0:40:3:Follower,11:50:3:Leader-3R,12:60:3:Leader-4R"

OUTPUT_BASE="$PROJECT_DIR/output/fault_recovery"

# ══════════════════════════════════════════════════════════════
# 参数解析
# ══════════════════════════════════════════════════════════════
while [[ $# -gt 0 ]]; do
    case "$1" in
        --use-shm)   EXTRA_ARGS="$EXTRA_ARGS --use-shm"; shift ;;
        --repeat)    REPEAT="$2"; shift 2 ;;
        --duration)  DURATION="$2"; shift 2 ;;
        --concurrency) CONCURRENCY="$2"; shift 2 ;;
        --network-delay-us) NETWORK_DELAY_US="$2"; shift 2 ;;
        *) shift ;;
    esac
done

# ══════════════════════════════════════════════════════════════
# 运行实验
# ══════════════════════════════════════════════════════════════
echo "============================================================"
echo " Fault Recovery Comparison Experiment"
echo " Duration=${DURATION}s  Concurrency=$CONCURRENCY  Repeat=$REPEAT"
echo " ProposeTimeout=${PROPOSE_TIMEOUT_MS}ms  CPU=$CPU_LIMIT  GOMAXPROCS=$GOMAXPROCS"
echo "============================================================"

for method in "${METHODS[@]}"; do
    config_var="CONFIG_${method}"
    leader_var="LEADER_${method}"
    events_var="EVENTS_${method}"

    echo ""
    echo "============================================================"
    echo "  Running: $method"
    echo "  Events:  ${!events_var}"
    echo "============================================================"

    bash "$SCRIPT_DIR/run-fault-recovery.sh" \
        --config "${!config_var}" \
        --force-leader "${!leader_var}" \
        --fault-events "${!events_var}" \
        --duration "$DURATION" \
        --concurrency "$CONCURRENCY" \
        --repeat "$REPEAT" \
        --cpu-limit "$CPU_LIMIT" \
        --gomaxprocs "$GOMAXPROCS" \
        --propose-timeout-ms "$PROPOSE_TIMEOUT_MS" \
        --network-delay-us "$NETWORK_DELAY_US" \
        --output-dir "$OUTPUT_BASE/comp_${method}" \
        $EXTRA_ARGS

    echo "  Done: $method"
    echo "  Waiting for port release..."
    sleep 5
    # 确保 Raft(33xxx) 和 gRPC(43xxx) 端口完全释放
    for _retry in $(seq 1 60); do
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
    sleep 3
done

# ══════════════════════════════════════════════════════════════
# 绘图（一次性生成所有图）
# ══════════════════════════════════════════════════════════════
echo ""
echo "============================================================"
echo " Generating all plots..."
echo "============================================================"

python3 "$SCRIPT_DIR/plot_fault_recovery.py" \
    --inputs "$OUTPUT_BASE/comp_global,$OUTPUT_BASE/comp_threshold,$OUTPUT_BASE/comp_clique,$OUTPUT_BASE/comp_neighbor" \
    --labels "Global,Threshold,Clique,Neighbor" \
    --pre-stable "$PRE_STABLE" \
    --post-stable "$POST_STABLE" \
    --output "$OUTPUT_BASE/figures/"

# ══════════════════════════════════════════════════════════════
# Per-Region TPS 提取与绘图
# ══════════════════════════════════════════════════════════════
echo ""
echo "============================================================"
echo " Extracting per-region TPS..."
echo "============================================================"

for method in "${METHODS[@]}"; do
    for run_dir in "$OUTPUT_BASE/comp_${method}"/run_*; do
        if [ -d "$run_dir" ]; then
            python3 "$SCRIPT_DIR/extract_region_tps.py" --run-dir "$run_dir"
        fi
    done
done

echo ""
echo "============================================================"
echo " Generating per-region plots..."
echo "============================================================"

python3 "$SCRIPT_DIR/plot_region_recovery.py" \
    --inputs "comp_global/run_1,comp_threshold/run_1,comp_clique/run_1,comp_neighbor/run_1" \
    --labels "Global,Threshold,Clique,Neighbor" \
    --base-dir "$OUTPUT_BASE" \
    --output "$OUTPUT_BASE/figures/"

echo ""
echo " All done. Figures saved to: $OUTPUT_BASE/figures/"

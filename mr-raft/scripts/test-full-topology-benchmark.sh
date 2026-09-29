#!/bin/bash

#============================================================
# 全拓扑压测脚本
# 
# 功能：
#   - 测试完整的15节点拓扑性能
#   - 每个节点向其所有region发送请求
#   - 所有压测客户端在一个容器中运行
#   - 自动计算有效TPS（去除重复计算）
#
# 用法：
#   ./test-full-topology-benchmark.sh [-m method] [-c concurrency] [-d duration]
#
# 示例：
#   ./test-full-topology-benchmark.sh                    # 使用默认配置
#   ./test-full-topology-benchmark.sh -m neighbor        # 只测试neighbor方法
#   ./test-full-topology-benchmark.sh -c 100 -d 90       # 自定义并发和时长
#============================================================

set -e

#============================================================
# 🔧 检测 Python 命令（跨平台兼容）
#============================================================
if command -v python3 &> /dev/null; then
    PYTHON_CMD="python3"
elif command -v python &> /dev/null; then
    PYTHON_CMD="python"
else
    echo "错误: 未找到 Python 3"
    exit 1
fi

#============================================================
# 🔧 测试配置区域
#============================================================

# 要测试的方法（可选值: global,neighbor,clique,threshold）
DEFAULT_METHODS="clique"  # 空=全部

# 并发配置数组（支持多次测试不同的并发分布）
# 每个元素是一次测试的并发配置，用分号分隔
# 格式1（所有节点相同）: "50" 表示所有节点50并发
# 格式2（不同节点不同并发）: "0:100,1:50,2:200,..." 表示node0=100并发，node1=50并发，node2=200并发
#
# 预定义的测试场景：
CONCURRENCY_CONFIGS=(
    # 场景1: 所有节点低并发（基线测试）
#    "0:0,1:0,2:200,3:0,4:200,5:200,6:0,7:0,8:0,9:0,10:200,11:200,12:0,13:0,14:0",
#    "0:1,1:1,2:1,3:1,4:1,5:1,6:1,7:1,8:1,9:1,10:1,11:1,12:1,13:1,14:1",
#    "0:2,1:2,2:2,3:2,4:2,5:2,6:2,7:2,8:2,9:2,10:2,11:2,12:2,13:2,14:2",
#    "0:3,1:3,2:3,3:3,4:3,5:3,6:3,7:3,8:3,9:3,10:3,11:3,12:3,13:3,14:3",
#    "0:4,1:4,2:4,3:4,4:4,5:4,6:4,7:4,8:4,9:4,10:4,11:4,12:4,13:4,14:4",
#    "0:5,1:5,2:5,3:5,4:5,5:5,6:5,7:5,8:5,9:5,10:5,11:5,12:5,13:5,14:5",
#    "0:6,1:6,2:6,3:6,4:6,5:6,6:6,7:6,8:6,9:6,10:6,11:6,12:6,13:6,14:6",
#    "0:8,1:8,2:8,3:8,4:8,5:8,6:8,7:8,8:8,9:8,10:8,11:8,12:8,13:8,14:8",
#    "0:10,1:10,2:10,3:10,4:10,5:10,6:10,7:10,8:10,9:10,10:10,11:10,12:10,13:10,14:10",
#    "0:12,1:12,2:12,3:12,4:12,5:12,6:12,7:12,8:12,9:12,10:12,11:12,12:12,13:12,14:12",
#    "0:14,1:14,2:14,3:14,4:14,5:14,6:14,7:14,8:14,9:14,10:14,11:14,12:14,13:14,14:14",
#    "0:16,1:16,2:16,3:16,4:16,5:16,6:16,7:16,8:16,9:16,10:16,11:16,12:16,13:16,14:16",
#    "0:18,1:18,2:18,3:18,4:18,5:18,6:18,7:18,8:18,9:18,10:18,11:18,12:18,13:18,14:18",
#    "0:20,1:20,2:20,3:20,4:20,5:20,6:20,7:20,8:20,9:20,10:20,11:20,12:20,13:20,14:20",
#    "0:22,1:22,2:22,3:22,4:22,5:22,6:22,7:22,8:22,9:22,10:22,11:22,12:22,13:22,14:22",
#    "0:24,1:24,2:24,3:24,4:24,5:24,6:24,7:24,8:24,9:24,10:24,11:24,12:24,13:24,14:24",
#    "0:26,1:26,2:26,3:26,4:26,5:26,6:26,7:26,8:26,9:26,10:26,11:26,12:26,13:26,14:26",
#    "0:28,1:28,2:28,3:28,4:28,5:28,6:28,9:28,10:28,11:28,12:28,13:28,14:28",
#    "0:30,1:30,2:30,3:30,4:30,5:30,6:30,7:30,8:30,9:30,10:30,11:30,12:30,13:30,14:30",
#    #"0:80,1:0,2:0,3:0,4:0,5:0,6:0,7:0,8:0,9:0,10:0,11:0,12:0,13:0,14:0",
#    "0:32,1:32,2:32,3:32,4:32,5:32,6:32,7:32,8:32,9:32,10:32,11:32,12:32,13:32,14:32",
#    "0:34,1:34,2:34,3:34,4:34,5:34,6:34,7:34,8:34,9:34,10:34,11:34,12:34,13:34,14:34",
#    "0:36,1:36,2:36,3:36,4:36,5:36,6:36,7:36,8:36,9:36,10:36,11:36,12:36,13:36,14:36",
#    "0:38,1:38,2:38,3:38,4:38,5:38,6:38,7:38,8:38,9:38,10:38,11:38,12:38,13:38,14:38",
#    "0:40,1:40,2:40,3:40,4:40,5:40,6:40,7:40,8:40,9:40,10:40,11:40,12:40,13:40,14:40",
#    "0:50,1:50,2:50,3:50,4:50,5:50,6:50,7:50,8:50,9:50,10:50,11:50,12:50,13:50,14:50",
#    "0:55,1:55,2:55,3:55,4:55,5:55,6:55,7:55,8:55,9:55,10:55,11:55,12:55,13:55,14:55",
#    "0:60,1:60,2:60,3:60,4:60,5:60,6:60,7:60,8:60,9:60,10:60,11:60,12:60,13:60,14:60",
#    "0:65,1:65,2:65,3:65,4:65,5:65,6:65,7:65,8:65,9:65,10:65,11:65,12:65,13:65,14:65",
#    "0:70,1:70,2:70,3:70,4:70,5:70,6:70,7:70,8:70,9:70,10:70,11:70,12:70,13:70,14:70",
#    "0:75,1:75,2:75,3:75,4:75,5:75,6:75,7:75,8:75,9:75,10:75,11:75,12:75,13:75,14:75",
#    "0:80,1:80,2:80,3:80,4:80,5:80,6:80,7:80,8:80,9:80,10:80,11:80,12:80,13:80,14:80",
#    "0:85,1:85,2:85,3:85,4:85,5:85,6:85,7:85,8:85,9:85,10:85,11:85,12:85,13:85,14:85",
#    "0:90,1:90,2:90,3:90,4:90,5:90,6:90,7:90,8:90,9:90,10:90,11:90,12:90,13:90,14:90",
#    "0:95,1:95,2:95,3:95,4:95,5:95,6:95,7:95,8:95,9:95,10:95,11:95,12:95,13:95,14:95",
    "0:100,1:100,2:100,3:100,4:100,5:100,6:100,7:100,8:100,9:100,10:100,11:100,12:100,13:100,14:100",
    # 场景3: 梯度负载（高中低）
    # "0:200,1:150,2:150,3:100,4:100,5:100,6:80,7:80,8:80,9:50,10:50,11:50,12:30,13:30,14:30"
    
    # 场景4: 热点节点（前3个节点高负载，其他低负载）
    # "0:200,1:200,2:200,3:30,4:30,5:30,6:30,7:30,8:30,9:30,10:30,11:30,12:30,13:30,14:30"
    
    # 场景5: 均衡负载（所有节点相同高并发）
    # "100"
)

# 兼容旧的配置变量（如果通过-c参数指定，会覆盖CONCURRENCY_CONFIGS）
DEFAULT_CONCURRENCY_CONFIG="0:1,1:1,2:1,3:1,4:1,5:1,6:1,7:1,8:1,9:1,10:1,11:1,12:1,13:1,14:1"

# 测试时长（秒）
DEFAULT_DURATION=60

# 请求数据大小（字节）
DEFAULT_PROPOSAL_SIZE=128

# QPS配置数组（0=不限制）
QPS_CONFIGS=(0)

# Leader放置策略（根据不同方法自动选择）
# 格式: "region_id:node_id,region_id:node_id,..."
# 留空表示让Raft自动选举
# 
# 为每个方法预定义Leader放置策略（每个节点最多一个leader）
# 这些策略基于verify_leader_placement.py的验证结果
declare -A METHOD_LEADER_PLACEMENTS
METHOD_LEADER_PLACEMENTS["global"]="1:0"  # Global方法只有1个Region
#METHOD_LEADER_PLACEMENTS["neighbor"]="1:0,2:2,3:11,4:1,5:3,6:9,7:8,8:10,9:4"  # 9个Region，负载均衡
#METHOD_LEADER_PLACEMENTS["clique"]="1:2,2:0,3:3,4:12,5:1,6:7,7:9,8:4,9:11,10:5,11:6"  # 11个Region，负载均衡
#METHOD_LEADER_PLACEMENTS["threshold"]="1:0,2:3,3:1,4:2,5:4"  # 5个Region

METHOD_LEADER_PLACEMENTS["neighbor"]="1:12,2:2,3:11,4:12,5:12,6:12,7:1,8:11,9:11"  # 9个Region
METHOD_LEADER_PLACEMENTS["clique"]="1:2,2:6,3:1,4:12,5:1,6:12,7:12,8:4,9:4,10:5,11:6"  # 11个Region
METHOD_LEADER_PLACEMENTS["threshold"]="1:11,2:12,3:1,4:12,5:11"  # 5个Region
# 默认Leader放置策略（如果通过-l参数指定，会覆盖上面的预定义策略）
DEFAULT_LEADER_PLACEMENT=""

# 状态机类型（true=内存，false=磁盘）
USE_MEMORY_SM=true

# 客户端容器模式（true=独立客户端容器，false=单个客户端容器）
DEFAULT_USE_INDEPENDENT_CLIENTS=false

# 容器资源限制
CONTAINER_CPU_LIMIT="2.0"
CONTAINER_MEMORY_LIMIT="1G"

# Go运行时最大线程数限制
DEFAULT_GOMAXPROCS=8

#============================================================

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# 打印函数
print_info() {
    echo -e "${BLUE}ℹ️  $1${NC}"
}

print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_error() {
    echo -e "${RED}✗ $1${NC}"
}

print_step() {
    echo -e "${YELLOW}▶ $1${NC}"
}

# 显示用法
show_usage() {
    cat << EOF
用法: $0 [选项]

选项:
    -m, --method METHOD               测试方法（global, neighbor, clique, threshold）
                                      默认: 测试所有方法
    -c, --concurrency CONCURRENCY     每个节点的并发数配置（分号分隔多次测试）
                                      格式1: "50;100;200" (所有节点相同)
                                      格式2: "node0:100,node1:50;node0:200,node1:100" (不同节点不同并发)
                                      如果不指定，使用CONCURRENCY_CONFIGS数组中的配置
                                      默认: 使用脚本中预定义的场景
    -d, --duration SECONDS            测试时长（秒）
                                      默认: $DEFAULT_DURATION
    -p, --proposal-size SIZE          提案数据大小（字节）
                                      默认: $DEFAULT_PROPOSAL_SIZE
    -l, --leader-placement PLACEMENT  Leader放置策略（格式: "1:0,2:1,3:2"）
                                      表示Region1→Node0, Region2→Node1, Region3→Node2
                                      如果不指定，每个方法使用预定义的负载均衡策略
                                      留空表示让Raft自动选举
                                      默认: 每个方法自动选择（见METHOD_LEADER_PLACEMENTS）
    -g, --gomaxprocs GOMAXPROCS      Go运行时最大CPU数限制
                                      默认: $DEFAULT_GOMAXPROCS
    --independent-clients             使用独立客户端容器模式（每个节点一个客户端容器）
                                      默认: 使用单个客户端容器（原始模式）
    --cpu-limit LIMIT                 容器CPU限制（例如: 2.0）
                                      默认: $CONTAINER_CPU_LIMIT
    --memory-limit LIMIT              容器内存限制（例如: 1G）
                                      默认: $CONTAINER_MEMORY_LIMIT
    -h, --help                        显示此帮助信息

可用方法:
    global          Global方法（15节点，1个region）
    neighbor        Neighbor-based方法（15节点，9个regions）
    clique          Maximal Clique方法（15节点，11个regions）
    threshold       Threshold-based方法（15节点，5个regions）

示例:
    # 基本用法（使用CONCURRENCY_CONFIGS数组中的所有场景）
    $0                                      # 使用预定义的并发场景
    $0 -m neighbor                          # 只测试neighbor方法，使用预定义场景
    
    # 单次测试（覆盖CONCURRENCY_CONFIGS）
    $0 -c "50"                              # 所有节点50并发
    $0 -c "0:100,1:50"                      # node0=100并发，node1=50并发
    
    # 多次测试不同并发（覆盖CONCURRENCY_CONFIGS）
    $0 -c "50;100;200"                      # 测试3次，每个节点分别50、100、200并发
    
    # 多次测试不同节点不同并发
    $0 -c "0:100,1:50;0:200,1:100"          # 测试2次，不同的节点并发配置
    
    # 15个节点完整配置示例（模拟不同负载）
    $0 -m neighbor -c "0:200,1:150,2:150,3:100,4:100,5:100,6:80,7:80,8:80,9:50,10:50,11:50,12:30,13:30,14:30" -d 120
    
    # 热点节点测试（node0高负载，其他低负载）
    $0 -c "0:500,1:50,2:50,3:50,4:50,5:50,6:50,7:50,8:50,9:50,10:50,11:50,12:50,13:50,14:50" -d 90
    
    # 其他配置
    $0 -d 90 -p 512                         # 测试时长90秒，提案大小512字节
    $0 --cpu-limit 4.0 --memory-limit 2G    # 自定义资源限制
    
    # Leader placement控制
    $0 -l "1:0,2:1,3:2"                     # 指定Region的leader位置

当前配置:
    DEFAULT_METHODS="$DEFAULT_METHODS"
    DEFAULT_CONCURRENCY_CONFIG="$DEFAULT_CONCURRENCY_CONFIG"
    DEFAULT_DURATION=$DEFAULT_DURATION
    DEFAULT_PROPOSAL_SIZE=$DEFAULT_PROPOSAL_SIZE
    QPS_CONFIGS=(${QPS_CONFIGS[*]})
    USE_MEMORY_SM=$USE_MEMORY_SM
    CONTAINER_CPU_LIMIT=$CONTAINER_CPU_LIMIT
    CONTAINER_MEMORY_LIMIT=$CONTAINER_MEMORY_LIMIT
    DEFAULT_GOMAXPROCS=$DEFAULT_GOMAXPROCS
EOF
    exit 0
}

# 解析命令行参数
TEST_METHODS=""
TEST_CONCURRENCY=""
TEST_DURATION_ENV=""
TEST_PROPOSAL_SIZE=""
TEST_LEADER_PLACEMENT=""
TEST_GOMAXPROCS_ENV=""
TEST_CPU_LIMIT=""
TEST_MEMORY_LIMIT=""
USE_INDEPENDENT_CLIENTS=""  # 将使用DEFAULT_USE_INDEPENDENT_CLIENTS的值

while [[ $# -gt 0 ]]; do
    case $1 in
        -m|--method)
            TEST_METHODS="$2"
            shift 2
            ;;
        -c|--concurrency)
            TEST_CONCURRENCY="$2"
            shift 2
            ;;
        -d|--duration)
            TEST_DURATION_ENV="$2"
            shift 2
            ;;
        -p|--proposal-size)
            TEST_PROPOSAL_SIZE="$2"
            shift 2
            ;;
        -l|--leader-placement)
            TEST_LEADER_PLACEMENT="$2"
            shift 2
            ;;
        -g|--gomaxprocs)
            TEST_GOMAXPROCS_ENV="$2"
            shift 2
            ;;
        --cpu-limit)
            TEST_CPU_LIMIT="$2"
            shift 2
            ;;
        --memory-limit)
            TEST_MEMORY_LIMIT="$2"
            shift 2
            ;;
        --independent-clients)
            USE_INDEPENDENT_CLIENTS="true"
            shift
            ;;
        -h|--help)
            show_usage
            ;;
        *)
            print_error "未知选项: $1"
            show_usage
            ;;
    esac
done

# 进入项目根目录
cd "$(dirname "$0")/.." || exit 1

# 定义测试方法
declare -A METHODS
METHODS["global"]="Global方法"
METHODS["neighbor"]="Neighbor-based方法"
METHODS["clique"]="Maximal Clique方法"
METHODS["threshold"]="Threshold-based方法"

declare -A METHOD_CONFIG_FILES
METHOD_CONFIG_FILES["global"]="docker/configs/method_global.yaml"
METHOD_CONFIG_FILES["neighbor"]="docker/configs/method_neighbor_based.yaml"
METHOD_CONFIG_FILES["clique"]="docker/configs/method_maximal_clique.yaml"
METHOD_CONFIG_FILES["threshold"]="docker/configs/method_threshold_based.yaml"

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

# 确定要测试的方法
if [ -n "$TEST_METHODS" ]; then
    IFS=',' read -ra METHODS_TO_TEST <<< "$TEST_METHODS"
elif [ -n "$DEFAULT_METHODS" ]; then
    IFS=',' read -ra METHODS_TO_TEST <<< "$DEFAULT_METHODS"
else
    METHODS_TO_TEST=(global neighbor clique threshold)
fi

# 解析并发配置
# 如果通过-c参数指定，则覆盖CONCURRENCY_CONFIGS数组
if [ -n "$TEST_CONCURRENCY" ]; then
    # 用户通过命令行指定了并发配置，解析为数组
    IFS=';' read -ra CONCURRENCY_CONFIGS <<< "$TEST_CONCURRENCY"
fi
# 否则使用脚本开头定义的CONCURRENCY_CONFIGS数组

# 确定测试时长
if [ -n "$TEST_DURATION_ENV" ]; then
    TEST_DURATION=$TEST_DURATION_ENV
else
    TEST_DURATION=$DEFAULT_DURATION
fi

# 确定提案大小
if [ -n "$TEST_PROPOSAL_SIZE" ]; then
    PROPOSAL_SIZE=$TEST_PROPOSAL_SIZE
else
    PROPOSAL_SIZE=$DEFAULT_PROPOSAL_SIZE
fi

# 确定Leader放置策略
if [ -n "$TEST_LEADER_PLACEMENT" ]; then
    LEADER_PLACEMENT=$TEST_LEADER_PLACEMENT
else
    LEADER_PLACEMENT=$DEFAULT_LEADER_PLACEMENT
fi

# 确定GOMAXPROCS
if [ -n "$TEST_GOMAXPROCS_ENV" ]; then
    DEFAULT_GOMAXPROCS="$TEST_GOMAXPROCS_ENV"
fi

# 确定CPU和内存限制
if [ -n "$TEST_CPU_LIMIT" ]; then
    CONTAINER_CPU_LIMIT=$TEST_CPU_LIMIT
fi

if [ -n "$TEST_MEMORY_LIMIT" ]; then
    CONTAINER_MEMORY_LIMIT=$TEST_MEMORY_LIMIT
fi

# 确定客户端容器模式
if [ -z "$USE_INDEPENDENT_CLIENTS" ]; then
    USE_INDEPENDENT_CLIENTS=$DEFAULT_USE_INDEPENDENT_CLIENTS
fi

# 创建结果目录
RESULTS_DIR="benchmark-results/full-topology-$(date +%Y%m%d_%H%M%S)"
mkdir -p "$RESULTS_DIR"

print_info "结果将保存到: $RESULTS_DIR"
echo ""

# 显示并发配置信息
print_step "并发配置场景"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "将测试 ${#CONCURRENCY_CONFIGS[@]} 个并发场景:"
for i in "${!CONCURRENCY_CONFIGS[@]}"; do
    echo "  场景$((i+1)): ${CONCURRENCY_CONFIGS[$i]}"
done
echo ""

cleanup_data_dirs() {
    local pattern=$1
    print_step "清理数据目录:  docker-data/${pattern}..."
    
    # 确保在项目根目录
    local project_root="$(cd "$(dirname "$0")/.." && pwd)"
    local data_dir="${project_root}/docker-data"
    
    if [ ! -d "$data_dir" ]; then
        print_info "  数据目录不存在，跳过清理"
        return 0
    fi
    
    # 查找匹配的目录
    local dirs_to_remove=$(find "$data_dir" -maxdepth 1 -type d -name "${pattern}" 2>/dev/null)
    
    if [ -z "$dirs_to_remove" ]; then
        print_info "  没有需要清理的目录"
        return 0
    fi
    
    print_info "  找到需要清理的目录:"
    echo "$dirs_to_remove" | while read dir; do
        echo "    - $(basename "$dir")"
    done
    
    # 尝试普通删除
    if rm -rf docker-data/${pattern} 2>/dev/null; then
        print_success "  数据目录已清理"
        return 0
    fi
    
    # 如果失败，尝试使用 sudo
    print_info "  需要 sudo 权限删除..."
    if sudo rm -rf docker-data/${pattern} 2>/dev/null; then
        print_success "  数据目录已清理 (使用 sudo)"
        return 0
    fi
    
    # 如果还是失败，尝试修改权限后删除
    print_info "  尝试修改权限后删除..."
    if sudo chown -R $(id -u):$(id -g) docker-data/${pattern} 2>/dev/null && \
       rm -rf docker-data/${pattern} 2>/dev/null; then
        print_success "  数据目录已清理 (修改权限后)"
        return 0
    fi
    
    print_error "  数据目录清理失败，请手动删除: docker-data/${pattern}"
    return 1
}

# 改进的 cleanup_all 函数
cleanup_all() {
    print_step "清理所有全拓扑测试容器和网络..."
    
    # 1. 停止所有相关容器
    local containers=$(docker ps -q --filter "name=^full-" 2>/dev/null || true)
    if [ -n "$containers" ]; then
        print_info "停止 $(echo $containers | wc -w) 个运行中的容器..."
        docker stop -t 3 $containers 2>/dev/null || true
        sleep 2
    fi
    
    # 2. 删除所有相关容器（包括已停止的）
    containers=$(docker ps -aq --filter "name=^full-" 2>/dev/null || true)
    if [ -n "$containers" ]; then
        print_info "删除 $(echo $containers | wc -w) 个容器..."
        docker rm -f $containers 2>/dev/null || true
        print_success "容器已清理"
    else
        print_info "没有需要清理的容器"
    fi
    
    # 3. 等待容器完全停止
    sleep 2
    
    # 4. 删除网络
    local networks=$(docker network ls --filter "name=_full_network" -q 2>/dev/null || true)
    if [ -n "$networks" ]; then
        print_info "删除网络..."
        docker network rm $networks 2>/dev/null || true
        print_success "网络已清理"
    fi
    
    # 5. 清理数据目录
    cleanup_data_dirs "full-*"
    
    # 6. 强制同步文件系统
    sync
    sleep 1
}

# 生成docker-compose文件
generate_docker_compose() {
    local method=$1
    local concurrency=$2
    local target_qps=$3
    local leader_placement=$4  # 新增：方法特定的leader placement
    local config_file="${METHOD_CONFIG_FILES[$method]}"
    
    # 将并发配置转换为文件名安全的格式（Windows不支持冒号）
    # 示例: "0:100,1:50" -> "0-100_1-50"
    local concurrency_safe=$(echo "$concurrency" | tr ':' '-' | tr ',' '_')
    
    local qps_label="q${target_qps}"
    local compose_file="$RESULTS_DIR/docker-compose-full-${method}-c${concurrency_safe}-${qps_label}.yaml"
    
    print_step "生成docker-compose配置: $method @ ${concurrency} 并发/节点"
    
    if [ ! -f "$config_file" ]; then
        print_error "配置文件不存在: $config_file"
        return 1
    fi
    
    print_info "  配置文件: $config_file"
    print_info "  每节点并发: $concurrency"
    print_info "  测试时长: $TEST_DURATION"
    print_info "  目标QPS: $([ "$target_qps" -eq 0 ] && echo "不限制" || echo "$target_qps")"
    print_info "  状态机: $([ "$USE_MEMORY_SM" = "true" ] && echo "内存" || echo "磁盘")"
    if [ -n "$leader_placement" ]; then
        print_info "  Leader放置: $leader_placement"
    else
        print_info "  Leader放置: 自动选举"
    fi
    
    if $PYTHON_CMD scripts/generate_docker_compose_full_topology.py \
        "$config_file" \
        "$concurrency" \
        "$TEST_DURATION" \
        "$target_qps" \
        "$PROPOSAL_SIZE" \
        "$USE_MEMORY_SM" \
        "$CONTAINER_CPU_LIMIT" \
        "$CONTAINER_MEMORY_LIMIT" \
        "$DEFAULT_GOMAXPROCS" \
        "$leader_placement" \
        "$USE_INDEPENDENT_CLIENTS" \
        > "$compose_file"; then
        
        if [ ! -s "$compose_file" ]; then
            print_error "生成的compose文件为空"
            return 1
        fi
        
        print_success "配置生成成功: $compose_file"
        return 0
    else
        print_error "配置生成失败"
        return 1
    fi
}

# 运行单个测试
run_test() {
    local method=$1
    local concurrency=$2
    local target_qps=$3
    local method_name="${METHODS[$method]}"
    local num_nodes=${NODE_COUNTS[$method]}
    local num_regions=${REGION_COUNTS[$method]}
    
    # 将并发配置转换为文件名安全的格式（Windows不支持冒号）
    local concurrency_safe=$(echo "$concurrency" | tr ':' '-' | tr ',' '_')
    
    # 确定该方法使用的Leader placement策略
    local method_leader_placement=""
    if [ -n "$LEADER_PLACEMENT" ]; then
        # 如果用户通过-l参数指定了，使用用户指定的
        method_leader_placement="$LEADER_PLACEMENT"
    else
        # 否则使用该方法的预定义策略
        method_leader_placement="${METHOD_LEADER_PLACEMENTS[$method]}"
    fi
    
    local qps_label="q${target_qps}"
    local compose_file="$RESULTS_DIR/docker-compose-full-${method}-c${concurrency_safe}-${qps_label}.yaml"
    local output_dir="$RESULTS_DIR/${method}_c${concurrency_safe}_${qps_label}"
    
    mkdir -p "$output_dir"
    
    local qps_display=$([ "$target_qps" -eq 0 ] && echo "不限制" || echo "$target_qps")
    print_step "开始测试: $method_name (${concurrency} 并发/节点, QPS=${qps_display})"
    print_info "  节点数: $num_nodes, Region数: $num_regions"
    if [ -n "$method_leader_placement" ]; then
        print_info "  Leader放置: $method_leader_placement"
    else
        print_info "  Leader放置: 自动选举"
    fi
    
    # 清理
    cleanup_all
    
    # 清理数据目录
    print_step "清理旧数据目录..."
    rm -rf docker-data/full-* 2>/dev/null || true
    sync
    
    # 创建results目录用于存放客户端结果（仅独立客户端模式需要）
    if [ "$USE_INDEPENDENT_CLIENTS" = "true" ]; then
        print_step "创建结果目录..."
        local results_dir="$RESULTS_DIR/results"
        rm -rf "$results_dir" 2>/dev/null || true
        mkdir -p "$results_dir"
    fi
    
    # 生成配置（传递方法特定的leader placement）
    if ! generate_docker_compose "$method" "$concurrency" "$target_qps" "$method_leader_placement"; then
        return 1
    fi
    
    # 创建数据目录
    print_step "创建数据目录..."
    if [ -f "$compose_file" ]; then
        grep "container_name:" "$compose_file" | awk '{print $2}' | while read container; do
            mkdir -p "docker-data/$container"
        done
    fi
    
    # 启动容器
    print_step "启动容器..."
    if ! docker-compose -f "$compose_file" up -d; then
        print_error "容器启动失败"
        return 1
    fi
    
    print_step "等待容器启动并稳定（5秒）..."
    sleep 5
    
    print_success "容器已启动"
    
    # 等待测试完成（等待所有客户端容器退出）
    print_step "等待测试完成（预计 ${TEST_DURATION}秒）..."
    local wait_time=$((TEST_DURATION + 30))
    local elapsed=0
    
    # 根据客户端模式选择不同的过滤条件
    local client_filter
    if [ "$USE_INDEPENDENT_CLIENTS" = "true" ]; then
        client_filter="name=full-.*-client-n"  # 独立客户端容器：full-{method}-client-n{id}
    else
        client_filter="name=full-.*-client"    # 单个客户端容器：full-{method}-client
    fi
    
    while [ $elapsed -lt $wait_time ]; do
        local running=$(docker ps --filter "$client_filter" --format "{{.Names}}" | wc -l)
        
        if [ "$running" -eq 0 ]; then
            print_success "测试已完成（所有客户端已退出）"
            break
        fi
        
        sleep 2
        elapsed=$((elapsed + 2))
        if [ "$USE_INDEPENDENT_CLIENTS" = "true" ]; then
            printf "\r  已等待: ${elapsed}s / ${wait_time}s (剩余客户端: ${running})"
        else
            printf "\r  已等待: ${elapsed}s / ${wait_time}s"
        fi
    done
    echo ""
    
    # 停止MEC节点
    print_step "停止MEC节点以输出统计报告..."
    local mec_containers=$(docker ps --filter "name=full-.*-n[0-9]" --format "{{.Names}}")
    if [ -n "$mec_containers" ]; then
        echo "$mec_containers" | while read container; do
            docker stop -t 1 "$container" 2>/dev/null || true
        done
        sleep 1
    fi
    
    # 收集日志
    print_step "收集测试日志..."
    docker ps -a --filter "name=full-" --format "{{.Names}}" | while read container; do
        docker logs "$container" > "$output_dir/${container}.log" 2>&1
    done
    
    # 汇总所有客户端的结果
    print_step "汇总测试结果..."
    
    # 等待结果文件写入完成
    sleep 2
    
    if [ "$USE_INDEPENDENT_CLIENTS" = "true" ]; then
        # 独立客户端模式：从results目录读取JSON文件并汇总
        local results_dir="$RESULTS_DIR/results"
        if [ ! -d "$results_dir" ]; then
            print_error "results目录不存在: $results_dir"
        else
            # 列出找到的结果文件
            local result_files=$(ls "$results_dir"/node_*_result.json 2>/dev/null | wc -l)
            print_info "找到 $result_files 个结果文件"
            
            if [ "$result_files" -gt 0 ]; then
                # 显示找到的文件
                ls -lh "$results_dir"/node_*_result.json
                
                # 汇总结果并保存到日志文件
                local summary_log="$output_dir/benchmark_summary.txt"
                print_info "汇总结果..."
                $PYTHON_CMD scripts/aggregate_results.py "$results_dir" "$output_dir/summary.json" | tee "$summary_log"
                
                # 显示汇总结果
                if [ -f "$output_dir/summary.json" ]; then
                    print_success "测试结果已汇总:"
                    print_success "  - JSON: $output_dir/summary.json"
                    print_success "  - 文本: $summary_log"
                fi
            else
                print_error "未找到客户端结果文件"
                print_info "检查客户端日志以排查问题..."
                print_info "结果目录: $results_dir"
                ls -la "$results_dir" 2>/dev/null || echo "results目录为空"
            fi
        fi
    else
        # 单个客户端模式：从客户端日志提取结果
        print_step "测试结果:"
        
        local client_log=$(ls "$output_dir"/full-*-client.log 2>/dev/null | head -1)
        local summary_log="$output_dir/benchmark_summary.txt"
        
        if [ -n "$client_log" ] && grep -q "全拓扑压测结果报告" "$client_log"; then
            print_success "客户端结果摘要:"
            echo ""
            # 提取结果并同时输出到控制台和文件
            grep -A 30 "全拓扑压测结果报告" "$client_log" | head -35 | tee "$summary_log"
            echo ""
            print_success "结果已保存到: $summary_log"
        else
            print_error "未找到客户端测试结果"
        fi
    fi
    
    # 停止剩余容器
    print_step "停止剩余容器..."
    docker-compose -f "$compose_file" down 2>/dev/null || true
    docker ps -a --filter "name=full-" -q | xargs -r docker rm -f 2>/dev/null || true
    
    # 清理数据目录
    rm -rf $RESULTS_DIR/docker-data/full-* 2>/dev/null || true
    
    print_step "等待系统稳定（10秒）..."
    sleep 10
    
    return 0
}

# 主流程
main() {
    echo "╔════════════════════════════════════════════════════════════╗"
    echo "║       全拓扑压测 - 方法对比测试                            ║"
    echo "╚════════════════════════════════════════════════════════════╝"
    echo ""
    
    print_info "测试配置:"
    print_info "  - 测试方法: ${METHODS_TO_TEST[*]}"
    print_info "  - 测试时长: ${TEST_DURATION}s"
    print_info "  - QPS配置: ${QPS_CONFIGS[*]} (0=不限制)"
    print_info "  - 状态机: $([ "$USE_MEMORY_SM" = "true" ] && echo "内存" || echo "磁盘")"
    print_info "  - 客户端模式: $([ "$USE_INDEPENDENT_CLIENTS" = "true" ] && echo "独立容器" || echo "单个容器")"
    print_info "  - 容器资源: CPU=${CONTAINER_CPU_LIMIT}核, 内存=${CONTAINER_MEMORY_LIMIT}"
    print_info "  - GoMaxProcs: ${DEFAULT_GOMAXPROCS}"
    print_info "  - 并发配置: ${CONCURRENCY_CONFIGS[*]}"
    echo ""
    
    print_info "各方法的节点和region数量："
    for method in "${METHODS_TO_TEST[@]}"; do
        local num_nodes=${NODE_COUNTS[$method]}
        local num_regions=${REGION_COUNTS[$method]}
        print_info "  - ${METHODS[$method]}: ${num_nodes} 节点, ${num_regions} regions"
    done
    echo ""
    
    # 检查gRPC代码
    print_step "检查gRPC代码..."
    if [ ! -f "proto/raftservice/raft_service.pb.go" ]; then
        print_error "gRPC代码未生成，请先运行: cd proto && sh gen.sh"
        exit 1
    fi
    print_success "gRPC代码已存在"
    echo ""
    
    # 构建镜像
    print_step "检查Docker镜像..."
    
    if ! docker images | grep -q "multi-region-raft/mec-grpc-full"; then
        print_info "构建MEC节点镜像..."
        if docker build -f Dockerfile.mec-grpc-full -t multi-region-raft/mec-grpc-full:latest .; then
            print_success "MEC节点镜像构建完成"
        else
            print_error "MEC节点镜像构建失败"
            exit 1
        fi
    else
        print_success "MEC节点镜像已存在"
    fi
    
    if ! docker images | grep -q "multi-region-raft/grpc-client-full"; then
        print_info "构建gRPC客户端镜像..."
        if docker build -f Dockerfile.grpc-client-full -t multi-region-raft/grpc-client-full:latest .; then
            print_success "gRPC客户端镜像构建完成"
        else
            print_error "gRPC客户端镜像构建失败"
            exit 1
        fi
    else
        print_success "gRPC客户端镜像已存在"
    fi
    echo ""
    
    # 初始清理
    print_step "执行初始环境清理..."
    cleanup_all
    echo ""
    
    # 依次测试
    local total_tests=$((${#METHODS_TO_TEST[@]} * ${#CONCURRENCY_CONFIGS[@]} * ${#QPS_CONFIGS[@]}))
    local test_num=1
    
    for method in "${METHODS_TO_TEST[@]}"; do
        for concurrency in "${CONCURRENCY_CONFIGS[@]}"; do
            for target_qps in "${QPS_CONFIGS[@]}"; do
                echo ""
                
                local qps_display=$([ "$target_qps" -eq 0 ] && echo "不限制" || echo "$target_qps")
                print_step "[$test_num/$total_tests] ${METHODS[$method]} @ ${concurrency} 并发/节点, QPS=${qps_display}"
                
                if run_test "$method" "$concurrency" "$target_qps"; then
                    print_success "测试完成"
                else
                    print_error "测试失败"
                fi
                
                test_num=$((test_num + 1))
                
                if [ $test_num -le $total_tests ]; then
                    print_info "等待5秒后开始下一个测试..."
                    sleep 5
                fi
            done
        done
    done
    
    print_success "所有测试完成！"
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "测试结果已保存到: $RESULTS_DIR"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}

# 运行主流程
main

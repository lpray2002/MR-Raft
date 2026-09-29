#!/bin/bash
#============================================================
# tc 网络延迟注入脚本（lo 接口，按端口分流）
#
# 用法:
#   ./tc-delay.sh setup --raft-delay 5 --api-delay 2
#   ./tc-delay.sh status
#   ./tc-delay.sh teardown
#
# 端口范围:
#   Raft:     33000-33014 (MEC 节点间共识通信)
#   gRPC API: 43000-43014 (客户端到服务端)
#
# 需要 root 权限 (sudo)
#============================================================

set -e

DEV="lo"
RAFT_PORT_START=33000
RAFT_PORT_END=33014
API_PORT_START=43000
API_PORT_END=43014

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[tc-delay]${NC} $*"; }
warn()  { echo -e "${YELLOW}[tc-delay]${NC} $*"; }
error() { echo -e "${RED}[tc-delay]${NC} $*" >&2; }

check_root() {
    if [ "$(id -u)" -ne 0 ]; then
        error "需要 root 权限，请用 sudo 运行"
        exit 1
    fi
}

add_port_filters() {
    local classid="$1"
    local port_start="$2"
    local port_end="$3"
    local prio="$4"

    for port in $(seq "$port_start" "$port_end"); do
        local hex_port=$(printf "0x%04x" "$port")
        # 匹配 dst port (TCP)
        tc filter add dev $DEV parent 1:0 protocol ip prio "$prio" \
            u32 match ip protocol 6 0xff \
            match ip dport "$port" 0xffff \
            flowid "$classid"
    done
}

do_setup() {
    local raft_delay=0
    local api_delay=0

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --raft-delay) raft_delay="$2"; shift 2 ;;
            --api-delay)  api_delay="$2"; shift 2 ;;
            *) shift ;;
        esac
    done

    if [ "$raft_delay" -eq 0 ] && [ "$api_delay" -eq 0 ]; then
        info "两个延迟都为 0，跳过 tc 设置"
        return 0
    fi

    check_root

    # 先清理已有规则
    tc qdisc del dev $DEV root 2>/dev/null || true

    # 创建 htb root qdisc，默认流量走 class 1:30（无延迟）
    tc qdisc add dev $DEV root handle 1: htb default 30

    # 默认 class（无延迟）— 必须有 rate，lo 用大值
    tc class add dev $DEV parent 1: classid 1:30 htb rate 10gbit
    tc qdisc add dev $DEV parent 1:30 handle 30: pfifo

    # Raft 延迟 class
    if [ "$raft_delay" -gt 0 ]; then
        tc class add dev $DEV parent 1: classid 1:10 htb rate 10gbit
        tc qdisc add dev $DEV parent 1:10 handle 10: netem delay "${raft_delay}ms"
        add_port_filters "1:10" "$RAFT_PORT_START" "$RAFT_PORT_END" 1
        info "Raft 延迟: ${raft_delay}ms (端口 ${RAFT_PORT_START}-${RAFT_PORT_END})"
    fi

    # API 延迟 class
    if [ "$api_delay" -gt 0 ]; then
        tc class add dev $DEV parent 1: classid 1:20 htb rate 10gbit
        tc qdisc add dev $DEV parent 1:20 handle 20: netem delay "${api_delay}ms"
        add_port_filters "1:20" "$API_PORT_START" "$API_PORT_END" 2
        info "API 延迟: ${api_delay}ms (端口 ${API_PORT_START}-${API_PORT_END})"
    fi

    info "tc 规则设置完成 (dev=$DEV)"
}

do_teardown() {
    check_root
    tc qdisc del dev $DEV root 2>/dev/null || true
    info "tc 规则已清除 (dev=$DEV)"
}

do_status() {
    echo "=== qdisc ==="
    tc qdisc show dev $DEV 2>/dev/null || echo "(无规则)"
    echo ""
    echo "=== class ==="
    tc class show dev $DEV 2>/dev/null || echo "(无规则)"
    echo ""
    echo "=== filter ==="
    tc filter show dev $DEV 2>/dev/null || echo "(无规则)"
}

case "${1:-}" in
    setup)
        shift
        do_setup "$@"
        ;;
    teardown)
        do_teardown
        ;;
    status)
        do_status
        ;;
    *)
        echo "用法: $0 {setup|teardown|status}"
        echo ""
        echo "  setup --raft-delay <ms> --api-delay <ms>"
        echo "  teardown"
        echo "  status"
        exit 1
        ;;
esac

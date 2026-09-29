#!/bin/bash
#============================================================
# AutoDL 一键部署 + 运行 Handover 实验
#
# 用法:
#   bash scripts/deploy-autodl.sh          # 部署 + 运行 + 拉取
#   bash scripts/deploy-autodl.sh deploy   # 仅部署代码
#   bash scripts/deploy-autodl.sh run      # 仅运行实验
#   bash scripts/deploy-autodl.sh fetch    # 仅拉取结果
#============================================================

set -e

# AutoDL 连接信息
AUTODL_HOST="connect.cqa1.seetacloud.com"
AUTODL_PORT=33030
AUTODL_USER="root"
AUTODL_PASS="6LkJsyNI/1Yy"

# 本地路径
LOCAL_GO_DIR="/Users/lpray/workspace/work/2025_10_23_multi-region-raft"
LOCAL_PY_DIR="/Users/lpray/workspace/work/2025_10_22_region-partitioning"

# 远程路径
REMOTE_GO_DIR="/root/multi-region-raft"
REMOTE_PY_DIR="/root/region-partitioning"
REMOTE_RESULTS="$REMOTE_GO_DIR/output/handover_results"

# 本地结果存放
LOCAL_RESULTS="$LOCAL_GO_DIR/output/handover_results_autodl"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

#------------------------------------------------------------
# 核心工具函数
#------------------------------------------------------------
_expect_rsync() {
    expect <<EXPECT_EOF
        set timeout 600
        spawn bash -c "rsync $@"
        expect {
            "*assword*" { send "$AUTODL_PASS\r"; exp_continue }
            eof {}
        }
        lassign [wait] pid spawnid os_error value
        exit \$value
EXPECT_EOF
}

_expect_ssh() {
    # 将脚本内容通过管道传给远程 bash
    local script_file="$1"
    expect <<EXPECT_EOF
        set timeout 1800
        spawn bash -c "cat $script_file | ssh -o StrictHostKeyChecking=no -p $AUTODL_PORT $AUTODL_USER@$AUTODL_HOST bash"
        expect {
            "*assword*" { send "$AUTODL_PASS\r"; exp_continue }
            eof {}
        }
        lassign [wait] pid spawnid os_error value
        exit \$value
EXPECT_EOF
}

run_remote() {
    # 用法: run_remote <<'SCRIPT' ... SCRIPT
    local tmpfile=$(mktemp /tmp/autodl_cmd_XXXXXXXX)
    cat > "$tmpfile"
    _expect_ssh "$tmpfile"
    local rc=$?
    rm -f "$tmpfile"
    return $rc
}

sync_to_remote() {
    # 用法: sync_to_remote <local> <remote> [excludes...]
    local src="$1" dst="$2"
    shift 2
    local args="-avz -e 'ssh -o StrictHostKeyChecking=no -p $AUTODL_PORT'"
    for ex in "$@"; do
        args="$args --exclude=$ex"
    done
    args="$args $src $AUTODL_USER@$AUTODL_HOST:$dst"
    _expect_rsync "$args"
}

sync_from_remote() {
    # 用法: sync_from_remote <remote> <local>
    local src="$1" dst="$2"
    _expect_rsync "-avz -e 'ssh -o StrictHostKeyChecking=no -p $AUTODL_PORT' $AUTODL_USER@$AUTODL_HOST:$src $dst"
}

#------------------------------------------------------------
# 部署
#------------------------------------------------------------
deploy() {
    log "═══ Deploying code to AutoDL ═══"

    log "Syncing Go project..."
    sync_to_remote "$LOCAL_GO_DIR/" "$REMOTE_GO_DIR/" .git output/handover_results vendor

    log "Syncing Python project..."
    sync_to_remote "$LOCAL_PY_DIR/" "$REMOTE_PY_DIR/" .git output/handover_figures __pycache__ .venv

    log "Installing dependencies..."
    run_remote <<'SCRIPT'
set -e
export PATH=$PATH:/usr/local/go/bin

# Docker
if ! command -v docker &>/dev/null; then
    echo ">>> Installing Docker (China mirror)..."
    rm -f /etc/apt/sources.list.d/docker.list
    apt-get -qq update >/dev/null
    DEBIAN_FRONTEND=noninteractive apt-get -y -qq install ca-certificates curl gnupg >/dev/null
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://mirrors.aliyun.com/docker-ce/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    UBUNTU_CODENAME=$(. /etc/os-release && echo "$VERSION_CODENAME")
    echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://mirrors.aliyun.com/docker-ce/linux/ubuntu $UBUNTU_CODENAME stable" > /etc/apt/sources.list.d/docker.list
    apt-get -qq update >/dev/null
    DEBIAN_FRONTEND=noninteractive apt-get -y -qq install docker-ce docker-ce-cli containerd.io >/dev/null
fi
systemctl start docker 2>/dev/null || service docker start 2>/dev/null || dockerd &
sleep 2
echo "Docker: $(docker --version)"

# Go
if ! command -v go &>/dev/null; then
    echo ">>> Installing Go..."
    wget -q https://mirrors.aliyun.com/golang/go1.21.13.linux-amd64.tar.gz -O /tmp/go.tar.gz || \
    wget -q https://golang.google.cn/dl/go1.21.13.linux-amd64.tar.gz -O /tmp/go.tar.gz
    rm -rf /usr/local/go && tar -C /usr/local -xzf /tmp/go.tar.gz
    echo 'export PATH=$PATH:/usr/local/go/bin' >> ~/.bashrc
fi
export PATH=$PATH:/usr/local/go/bin
export GOPROXY=https://goproxy.cn,direct
echo "Go: $(go version)"

# Python deps
pip install -q pyyaml networkx numpy matplotlib scipy 2>/dev/null || \
pip3 install -q pyyaml networkx numpy matplotlib scipy 2>/dev/null || \
python3 -m pip install -q pyyaml networkx numpy matplotlib scipy 2>/dev/null || \
{ apt-get -y -qq install python3-pip >/dev/null && pip3 install -q pyyaml networkx numpy matplotlib scipy; }

# Build Go
cd /root/multi-region-raft
echo ">>> Building Go binaries..."
go build ./cmd/mec-node-grpc-full-topology/
go build ./cmd/handover-client/

# Build Docker images
echo ">>> Building Docker images..."
docker build -f Dockerfile.server -t multi-region-raft/mec-grpc-full:latest . 2>&1 | tail -2
docker build -f Dockerfile.handover-client -t multi-region-raft/handover-client:latest . 2>&1 | tail -2

echo "✓ All dependencies ready!"
SCRIPT

    log "✓ Deploy complete"
}

#------------------------------------------------------------
# 运行实验
#------------------------------------------------------------
run_experiment() {
    log "═══ Running experiment on AutoDL ═══"

    run_remote <<'SCRIPT'
set -e
export PATH=$PATH:/usr/local/go/bin
cd /root/multi-region-raft

rm -rf output/handover_results
mkdir -p output/handover_results

echo "Starting experiment (native mode, no Docker)..."
bash scripts/run-handover-native.sh --single \
    --config /root/region-partitioning/output/handover_cluster

echo ""
echo "═══ Done! ═══"
ls -lh output/handover_results/
SCRIPT

    log "✓ Experiment complete"
}

#------------------------------------------------------------
# 拉取结果
#------------------------------------------------------------
fetch_results() {
    log "═══ Fetching results from AutoDL ═══"

    mkdir -p "$LOCAL_RESULTS"
    sync_from_remote "$REMOTE_RESULTS/" "$LOCAL_RESULTS/"

    log "✓ Results saved to: $LOCAL_RESULTS/"
    ls -la "$LOCAL_RESULTS/" 2>/dev/null | head -20

    log "Quick summary:"
    python3 -c "
import json, glob, os
files = sorted(glob.glob('$LOCAL_RESULTS/result_c*_readindex.json'),
               key=lambda f: int(f.split('result_c')[1].split('_')[0]))
for f in files:
    with open(f) as fp:
        s = json.load(fp)['summary']
    name = os.path.basename(f)
    c = s.get('concurrency', 1)
    print(f'  C={c:>3} TPS={s.get(\"actual_tps\",0):>6.0f} WrLat={s[\"avg_write_latency_us\"]/1000:.1f}ms Read={s[\"read_consistency_rate\"]*100:.2f}% HO={s.get(\"handover_consistency_rate\",1)*100:.2f}%')
" 2>/dev/null || true
}

#------------------------------------------------------------
# 主入口
#------------------------------------------------------------
check_deps() {
    command -v expect &>/dev/null || { log "ERROR: expect not found"; exit 1; }
}

check_deps

case "${1:-all}" in
    deploy)
        deploy
        ;;
    run)
        run_experiment
        ;;
    fetch)
        fetch_results
        ;;
    all)
        deploy
        run_experiment
        fetch_results
        log "═══ All done! ═══"
        log "Analyze: cd $LOCAL_PY_DIR && python analyze_consistency_results.py --results-dir $LOCAL_RESULTS"
        ;;
    *)
        echo "Usage: $0 {deploy|run|fetch|all}"
        exit 1
        ;;
esac

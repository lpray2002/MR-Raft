#!/bin/bash

# gRPC 代码生成脚本

set -e

echo "生成 gRPC 代码..."

# 确保安装了 protoc
if ! command -v protoc &> /dev/null; then
    echo "错误: protoc 未安装"
    echo "Windows: choco install protoc"
    echo "Linux:   sudo apt-get install -y protobuf-compiler"
    echo "macOS:   brew install protobuf"
    exit 1
fi

# 确保安装了 Go protoc 插件
if ! command -v protoc-gen-go &> /dev/null; then
    echo "安装 protoc-gen-go..."
    go install google.golang.org/protobuf/cmd/protoc-gen-go@latest
fi

if ! command -v protoc-gen-go-grpc &> /dev/null; then
    echo "安装 protoc-gen-go-grpc..."
    go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@latest
fi

# 返回项目根目录
cd "$(dirname "$0")/.."

# 创建输出目录
mkdir -p proto/raftservice

# 生成代码
protoc --go_out=. --go_opt=paths=source_relative \
       --go-grpc_out=. --go-grpc_opt=paths=source_relative \
       proto/raft_service.proto

echo "✓ gRPC 代码生成完成"
echo "  生成文件:"
echo "    - proto/raftservice/raft_service.pb.go"
echo "    - proto/raftservice/raft_service_grpc.pb.go"


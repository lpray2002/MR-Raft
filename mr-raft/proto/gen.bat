@echo off
REM gRPC 代码生成脚本 (Windows)

echo 生成 gRPC 代码...

REM 检查 protoc 是否安装
where protoc >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo 错误: protoc 未安装
    echo 请安装: choco install protoc
    echo 或下载: https://github.com/protocolbuffers/protobuf/releases
    exit /b 1
)

REM 安装 Go protoc 插件
where protoc-gen-go >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo 安装 protoc-gen-go...
    go install google.golang.org/protobuf/cmd/protoc-gen-go@latest
)

where protoc-gen-go-grpc >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo 安装 protoc-gen-go-grpc...
    go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@latest
)

REM 返回项目根目录
cd /d "%~dp0.."

REM 创建输出目录
if not exist "proto\raftservice" mkdir "proto\raftservice"

REM 生成代码
protoc --go_out=. --go_opt=paths=source_relative ^
       --go-grpc_out=. --go-grpc_opt=paths=source_relative ^
       proto/raft_service.proto

if %ERRORLEVEL% EQU 0 (
    echo ✓ gRPC 代码生成完成
    echo   生成文件:
    echo     - proto\raftservice\raft_service.pb.go
    echo     - proto\raftservice\raft_service_grpc.pb.go
) else (
    echo ✗ 代码生成失败
    exit /b 1
)


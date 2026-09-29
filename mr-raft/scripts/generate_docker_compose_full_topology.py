#!/usr/bin/env python3
"""
生成全拓扑压测的docker-compose配置文件
所有15个节点都参与压测，每个节点向其所有region发送请求

用法:
    python3 generate_docker_compose_full_topology.py <config_file> <concurrency_per_node> <duration_seconds> [target_qps] [proposal_size] [use_memory_sm] [cpu_limit] [memory_limit] [gomaxprocs]
    
参数说明:
    config_file           - 配置文件路径
    concurrency_per_node  - 每个节点的并发数
    duration_seconds      - 测试时长（秒）
    target_qps            - 目标QPS（可选，默认0表示不限制）
    proposal_size         - 提案数据大小（字节，可选，默认128）
    use_memory_sm         - 是否使用内存状态机（可选，true/false，默认false）
    cpu_limit             - 容器CPU限制（可选，默认2.0，表示2核）
    memory_limit          - 容器内存限制（可选，默认1G）
    gomaxprocs            - Go运行时最大CPU数限制（可选，默认0=不限制）
    
容器命名格式:
    MEC节点: full-{method}-n{node_id}
    客户端:  full-{method}-client-n{node_id}
"""

import yaml
import sys


def generate_docker_compose(config_file, concurrency_per_node, duration_seconds, target_qps=0, 
                           proposal_size=None, use_memory_sm=False, cpu_limit="2.0", memory_limit="1G", gomaxprocs="0", leader_placement="", independent_clients=False):
    """生成docker-compose配置
    
    concurrency_per_node 可以是:
    1. 单个数字: "100" - 所有节点使用相同并发
    2. 节点特定配置: "node0:100,node1:50,node2:200" - 不同节点不同并发
    
    independent_clients: 是否使用独立客户端容器模式
    - True: 每个节点一个独立的客户端容器
    - False: 所有节点共享一个客户端容器（原始模式）
    """
    
    # 读取配置（指定UTF-8编码）
    with open(config_file, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    
    # 提取配置
    mec_nodes = config.get('mec_nodes', [])
    regions = config.get('regions', [])
    benchmark = config.get('benchmark', {})
    method_name = benchmark.get('method_name', 'mec')
    
    # 提案大小：优先使用命令行参数，否则使用配置文件，最后使用默认值
    if proposal_size is None:
        proposal_size = benchmark.get('proposal_size', 128)
    else:
        proposal_size = int(proposal_size)
    
    # 解析leader placement配置
    # 格式: "1:0,2:1,3:2" 表示Region1→Node0, Region2→Node1, Region3→Node2
    region_leader_map = {}
    if leader_placement:
        for item in leader_placement.split(','):
            parts = item.strip().split(':')
            if len(parts) == 2:
                region_id = int(parts[0])
                node_id = int(parts[1])
                region_leader_map[region_id] = node_id
    
    # 解析并发配置
    node_concurrency = {}
    node_qps = {}
    default_concurrency = 10
    default_qps = int(target_qps) if target_qps else 0
    
    # 检查是否是节点特定配置（包含冒号）
    if ':' in str(concurrency_per_node):
        # 格式: "node0:100,node1:50" 或 "0:100,1:50"
        first_concurrency = None
        for item in str(concurrency_per_node).split(','):
            parts = item.strip().split(':')
            if len(parts) == 2:
                node_key = parts[0].strip()
                conc_value = int(parts[1])
                
                # 支持 "node0" 或 "0" 格式
                if node_key.startswith('node'):
                    node_id = int(node_key[4:])
                else:
                    node_id = int(node_key)
                
                node_concurrency[node_id] = conc_value
                
                # 记录第一个节点的并发数作为默认值
                if first_concurrency is None:
                    first_concurrency = conc_value
        
        # 使用第一个节点的并发数作为其他节点的默认值
        default_concurrency = first_concurrency if first_concurrency is not None else 10
    else:
        # 所有节点使用相同并发
        default_concurrency = int(concurrency_per_node)
    
    # 生成容器名称后缀
    sm_suffix = "mem" if use_memory_sm else "disk"
    
    # 为不同方法分配不同的子网
    subnet_map = {
        'global': '172.41.0.0/16',
        'neighbor': '172.42.0.0/16',
        'clique': '172.43.0.0/16',
        'threshold': '172.44.0.0/16',
    }
    subnet = subnet_map.get(method_name, '172.40.0.0/16')
    
    # 构建docker-compose配置
    compose = {
        'version': '3.8',
        'networks': {
            f'{method_name}_full_network': {
                'driver': 'bridge',
                'ipam': {
                    'config': [
                        {'subnet': subnet}
                    ]
                }
            }
        },
        'services': {}
    }
    
    # 为每个Region构建成员列表
    region_members = {}
    for region in regions:
        region_id = region['id']
        members = {}
        for node in mec_nodes:
            if region_id in node['regions']:
                raft_node_id = region_id * 100 + node['id']
                container_name = f'full-{method_name}-n{node["id"]}'
                port = node['address'].split(':')[-1]
                members[raft_node_id] = f'{container_name}:{port}'
        region_members[region_id] = members
    
    # gRPC端口基数
    grpc_port_base = 50051
    
    # 为每个MEC节点创建服务
    for idx, node in enumerate(mec_nodes):
        node_id = node['id']
        node_name = f'full-{method_name}-n{node_id}'
        
        # gRPC端口
        grpc_port = grpc_port_base + node_id
        
        # 分配固定IP
        ip_address = f'172.{subnet.split(".")[1]}.0.{10 + idx}'
        
        # Raft通信地址
        raft_port = node['address'].split(':')[-1]
        
        # 环境变量
        env_vars = [
            f'MEC_NODE_ID={node_id}',
            f'MEC_NODE_NAME={node_name}',
            f'MEC_NODE_ADDRESS={node_name}:{raft_port}',
            f'MEC_NODE_REGIONS={",".join(map(str, node["regions"]))}',
            f'MEC_NODE_DATA_DIR=/data',
            f'USE_MEMORY_SM={"true" if use_memory_sm else "false"}',
            f'GRPC_PORT={grpc_port}',
            f'GOMAXPROCS={gomaxprocs}',
        ]
        
        # 集群成员配置
        for region_id in node['regions']:
            members_str = ",".join([f"{nid}:{addr}" for nid, addr in region_members[region_id].items()])
            env_vars.append(f'CLUSTER_MEMBERS_{region_id}={members_str}')
        
        # Leader placement配置（如果指定）
        # 格式: LEADER_REGIONS=1,3,5 表示该节点应该成为Region 1,3,5的leader
        leader_regions = [str(r) for r in node['regions'] if region_leader_map.get(r) == node_id]
        if leader_regions:
            env_vars.append(f'LEADER_REGIONS={",".join(leader_regions)}')
        
        # 创建服务配置
        service = {
            'image': 'multi-region-raft/mec-grpc-full:latest',
            'container_name': node_name,
            'hostname': node_name,
            'networks': {
                f'{method_name}_full_network': {
                    'ipv4_address': ip_address
                }
            },
            'user': '0:0',
            'environment': env_vars,
            'volumes': [
                f'./docker-data/{node_name}:/data'
            ],
            'restart': 'no',
            'deploy': {
                'resources': {
                    'limits': {
                        'cpus': cpu_limit,
                        'memory': memory_limit
                    }
                }
            },
            'ulimits': {
                'nofile': {
                    'soft': 65536,
                    'hard': 65536
                },
                'nproc': {
                    'soft': 65536,
                    'hard': 65536
                }
            },
        }
        
        compose['services'][node_name] = service
    
    # 创建客户端容器
    if independent_clients:
        # 独立客户端模式：为每个节点创建独立的客户端容器
        client_ip_offset = 100
        for node in mec_nodes:
            node_id = node['id']
            node_conc = node_concurrency.get(node_id, default_concurrency)
            
            # 跳过并发为0的节点
            if node_conc < 1:
                continue
            
            client_name = f'full-{method_name}-client-n{node_id}'
            client_ip = f'172.{subnet.split(".")[1]}.1.{client_ip_offset + node_id}'
            
            node_host = f'full-{method_name}-n{node_id}'
            grpc_port = grpc_port_base + node_id
            regions_str = ','.join(map(str, node['regions']))
            node_q = node_qps.get(node_id, default_qps)
            
            # 单节点配置字符串
            node_config = f'{node_id}:{node_host}:{grpc_port}:{regions_str}:{node_conc}:{node_q}'
            
            # 客户端环境变量
            client_env = [
                f'NODE_CONFIGS={node_config}',
                f'DURATION_SECONDS={duration_seconds}',
                f'PROPOSAL_SIZE={proposal_size}',
                f'SINGLE_NODE_MODE=true',
                f'OUTPUT_FILE=/results/node_{node_id}_result.json',
            ]
            
            # 客户端服务配置
            client_service = {
                'image': 'multi-region-raft/grpc-client-full:latest',
                'container_name': client_name,
                'hostname': client_name,
                'networks': {
                    f'{method_name}_full_network': {
                        'ipv4_address': client_ip
                    }
                },
                'environment': client_env,
                'depends_on': [f'full-{method_name}-n{node_id}'],
                'restart': 'no',
                'volumes': [
                    './results:/results'
                ],
                'ulimits': {
                    'nofile': {'soft': 65536, 'hard': 65536},
                    'nproc': {'soft': 65536, 'hard': 65536}
                },
            }
            
            compose['services'][client_name] = client_service
    else:
        # 单个客户端模式（原始模式）：所有节点共享一个客户端容器
        client_name = f'full-{method_name}-client'
        client_ip = f'172.{subnet.split(".")[1]}.1.100'
        
        # 构建所有节点的配置字符串
        node_configs = []
        for node in mec_nodes:
            node_id = node['id']
            node_host = f'full-{method_name}-n{node_id}'
            grpc_port = grpc_port_base + node_id
            regions_str = ','.join(map(str, node['regions']))
            node_conc = node_concurrency.get(node_id, default_concurrency)
            node_q = node_qps.get(node_id, default_qps)
            node_configs.append(f'{node_id}:{node_host}:{grpc_port}:{regions_str}:{node_conc}:{node_q}')
        
        # 客户端环境变量
        client_env = [
            f'NODE_CONFIGS={";".join(node_configs)}',
            f'DURATION_SECONDS={duration_seconds}',
            f'PROPOSAL_SIZE={proposal_size}',
        ]
        
        # 客户端服务配置
        client_service = {
            'image': 'multi-region-raft/grpc-client-full:latest',
            'container_name': client_name,
            'hostname': client_name,
            'networks': {
                f'{method_name}_full_network': {
                    'ipv4_address': client_ip
                }
            },
            'environment': client_env,
            'depends_on': [f'full-{method_name}-n{node["id"]}' for node in mec_nodes],
            'restart': 'no',
            'ulimits': {
                'nofile': {'soft': 65536, 'hard': 65536},
                'nproc': {'soft': 65536, 'hard': 65536}
            },
        }
        
        compose['services'][client_name] = client_service
    
    # 输出YAML到stdout
    print(yaml.dump(compose, default_flow_style=False, sort_keys=False))


if __name__ == '__main__':
    if len(sys.argv) < 4:
        print(f"用法: {sys.argv[0]} <config_file> <concurrency_per_node> <duration_seconds> [target_qps] [proposal_size] [use_memory_sm] [cpu_limit] [memory_limit] [gomaxprocs] [leader_placement] [independent_clients]")
        print(f"")
        print(f"参数说明:")
        print(f"  config_file           - YAML配置文件路径")
        print(f"  concurrency_per_node  - 每个节点的并发数")
        print(f"  duration_seconds      - 测试时长（秒）")
        print(f"  target_qps            - 目标QPS（可选，默认0=不限制）")
        print(f"  proposal_size         - 提案数据大小（字节，可选，默认128）")
        print(f"  use_memory_sm         - 是否使用内存状态机（可选，true/false，默认false）")
        print(f"  cpu_limit             - 容器CPU限制（可选，默认2.0，表示2核）")
        print(f"  memory_limit          - 容器内存限制（可选，默认1G）")
        print(f"  gomaxprocs            - Go运行时最大CPU数限制（可选，默认0=不限制）")
        print(f"  leader_placement      - Leader放置策略（可选，格式: '1:0,2:1,3:2'，默认''=自动选举）")
        print(f"  independent_clients   - 是否使用独立客户端容器（可选，true/false，默认false）")
        print(f"")
        print(f"示例:")
        print(f"  # 每个节点100并发，不限制QPS，128字节提案")
        print(f"  {sys.argv[0]} docker/configs/method_neighbor_based.yaml 100 60")
        print(f"")
        print(f"  # 每个节点50并发，使用独立客户端容器")
        print(f"  {sys.argv[0]} docker/configs/method_neighbor_based.yaml 50 60 0 128 true 2.0 1G 0 '' true")
        sys.exit(1)
    
    config_file = sys.argv[1]
    concurrency_per_node = sys.argv[2]  # 保持字符串，支持 "100" 或 "node0:100,node1:50"
    duration_seconds = int(sys.argv[3])
    target_qps = int(sys.argv[4]) if len(sys.argv) > 4 else 0
    proposal_size = int(sys.argv[5]) if len(sys.argv) > 5 else None
    use_memory_sm = (sys.argv[6].lower() == 'true') if len(sys.argv) > 6 else False
    cpu_limit = sys.argv[7] if len(sys.argv) > 7 else "2.0"
    memory_limit = sys.argv[8] if len(sys.argv) > 8 else "1G"
    gomaxprocs = sys.argv[9] if len(sys.argv) > 9 else "0"
    leader_placement = sys.argv[10] if len(sys.argv) > 10 else ""
    independent_clients = (sys.argv[11].lower() == 'true') if len(sys.argv) > 11 else False
    
    # 调试输出
    print(f"# 调试信息: 接收到的参数", file=sys.stderr)
    print(f"#   config_file: {config_file}", file=sys.stderr)
    print(f"#   concurrency_per_node: {concurrency_per_node}", file=sys.stderr)
    print(f"#   duration_seconds: {duration_seconds}", file=sys.stderr)
    print(f"#   target_qps: {target_qps}", file=sys.stderr)
    print(f"#   proposal_size: {proposal_size}", file=sys.stderr)
    print(f"#   use_memory_sm: {use_memory_sm}", file=sys.stderr)
    print(f"#   cpu_limit: {cpu_limit}", file=sys.stderr)
    print(f"#   memory_limit: {memory_limit}", file=sys.stderr)
    print(f"#   gomaxprocs: {gomaxprocs}", file=sys.stderr)
    print(f"#   leader_placement: {leader_placement}", file=sys.stderr)
    print(f"#   independent_clients: {independent_clients}", file=sys.stderr)
    
    generate_docker_compose(config_file, concurrency_per_node, duration_seconds, target_qps, 
                           proposal_size, use_memory_sm, cpu_limit, memory_limit, gomaxprocs, leader_placement, independent_clients)

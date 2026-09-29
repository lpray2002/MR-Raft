"""拓扑统计信息收集工具"""

import networkx as nx


def collect_topology_stats(G):
    """
    收集拓扑特征统计
    
    Parameters:
    -----------
    G : nx.Graph
        网络拓扑图
    
    Returns:
    --------
    dict : 包含拓扑统计信息的字典
    """
    num_nodes = G.number_of_nodes()
    num_edges = G.number_of_edges()
    
    # 平均度
    degrees = [d for n, d in G.degree()]
    avg_degree = sum(degrees) / num_nodes if num_nodes > 0 else 0
    max_degree = max(degrees) if degrees else 0
    
    # 图密度
    density = nx.density(G)
    
    # 聚类系数
    clustering = nx.average_clustering(G)
    
    return {
        'nodes': num_nodes,
        'edges': num_edges,
        'avg_degree': avg_degree,
        'max_degree': max_degree,
        'density': density,
        'clustering': clustering
    }

"""生成图片的LaTeX表示"""


def generate_figure_latex(filename, caption, label, width='0.48\\textwidth', 
                          additional_info=None, subfigure=False):
    """
    生成单个图片的LaTeX代码
    
    Parameters:
    -----------
    filename : str
        图片文件名（相对路径）
    caption : str
        图片标题
    label : str
        图片标签（用于引用）
    width : str
        图片宽度，默认0.48\textwidth（适合双栏）
    additional_info : dict, optional
        额外信息，如 {'Nodes': 9, 'Edges': 11, 'Density': '2e-6'}
    subfigure : bool
        是否作为子图
    
    Returns:
    --------
    str : LaTeX代码
    """
    if subfigure:
        latex = []
        latex.append(f"\\begin{{subfigure}}[b]{{{width}}}")
        latex.append(f"    \\centering")
        latex.append(f"    \\includegraphics[width=\\textwidth]{{{filename}}}")
        
        # 构建caption
        caption_parts = [caption]
        if additional_info:
            info_str = ', '.join([f"{k}={v}" for k, v in additional_info.items()])
            caption_parts.append(f"({info_str})")
        
        latex.append(f"    \\caption{{{' '.join(caption_parts)}}}")
        latex.append(f"    \\label{{{label}}}")
        latex.append(f"\\end{{subfigure}}")
        
        return '\n'.join(latex)
    else:
        latex = []
        latex.append("\\begin{figure}[htbp]")
        latex.append("    \\centering")
        latex.append(f"    \\includegraphics[width={width}]{{{filename}}}")
        
        # 构建caption
        caption_parts = [caption]
        if additional_info:
            info_str = ', '.join([f"{k}={v}" for k, v in additional_info.items()])
            caption_parts.append(f"({info_str})")
        
        latex.append(f"    \\caption{{{' '.join(caption_parts)}}}")
        latex.append(f"    \\label{{{label}}}")
        latex.append("\\end{figure}")
        
        return '\n'.join(latex)


def generate_density_comparison_latex(densities, seeds, output_dir='output/density_comparison'):
    """
    生成三密度对比的LaTeX代码
    
    Parameters:
    -----------
    densities : list
        密度列表
    seeds : list
        随机种子列表
    output_dir : str
        图片输出目录
    
    Returns:
    --------
    str : LaTeX代码
    """
    latex = []
    latex.append("% ========================================")
    latex.append("% 密度对比 - 空间拓扑图")
    latex.append("% ========================================")
    latex.append("\\begin{figure*}[htbp]")
    latex.append("    \\centering")
    
    for idx, (density, seed) in enumerate(zip(densities, seeds)):
        density_str = f"{density:.0e}".replace('-0', '-').replace('+0', '')
        
        latex.append(f"    \\begin{{subfigure}}[b]{{0.32\\textwidth}}")
        latex.append(f"        \\centering")
        latex.append(f"        \\includegraphics[width=\\textwidth]{{{output_dir}/density_{density_str}_spatial.png}}")
        latex.append(f"        \\caption{{$\\lambda_{{bs}}={density_str}$, seed={seed}}}")
        latex.append(f"        \\label{{fig:spatial_{density_str}}}")
        latex.append(f"    \\end{{subfigure}}")
        if idx < len(densities) - 1:
            latex.append(f"    \\hfill")
    
    latex.append("    \\caption{Spatial topology at different base station densities}")
    latex.append("    \\label{fig:spatial_comparison}")
    latex.append("\\end{figure*}")
    latex.append("")
    
    # MEC拓扑图
    latex.append("% ========================================")
    latex.append("% 密度对比 - MEC拓扑图")
    latex.append("% ========================================")
    latex.append("\\begin{figure*}[htbp]")
    latex.append("    \\centering")
    
    for idx, (density, seed) in enumerate(zip(densities, seeds)):
        density_str = f"{density:.0e}".replace('-0', '-').replace('+0', '')
        
        latex.append(f"    \\begin{{subfigure}}[b]{{0.32\\textwidth}}")
        latex.append(f"        \\centering")
        latex.append(f"        \\includegraphics[width=\\textwidth]{{{output_dir}/density_{density_str}_mec_graph.png}}")
        latex.append(f"        \\caption{{$\\lambda_{{bs}}={density_str}$}}")
        latex.append(f"        \\label{{fig:mec_{density_str}}}")
        latex.append(f"    \\end{{subfigure}}")
        if idx < len(densities) - 1:
            latex.append(f"    \\hfill")
    
    latex.append("    \\caption{MEC topology graph at different base station densities}")
    latex.append("    \\label{fig:mec_comparison}")
    latex.append("\\end{figure*}")
    latex.append("")
    
    return '\n'.join(latex)


def generate_partition_strategy_latex(densities, strategies, output_dir='output/density_comparison'):
    """
    生成分区策略对比的LaTeX代码
    
    Parameters:
    -----------
    densities : list
        密度列表
    strategies : list
        策略名称列表，如 ['maximal_clique', 'threshold_tau4', 'neighbor_based', 'global']
    output_dir : str
        图片输出目录
    
    Returns:
    --------
    str : LaTeX代码
    """
    latex = []
    
    strategy_labels = {
        'maximal_clique': 'Maximal Clique',
        'neighbor_based': 'Neighbor-based',
        'global': 'Global',
    }
    
    for density in densities:
        density_str = f"{density:.0e}".replace('-0', '-').replace('+0', '')
        
        latex.append(f"% ========================================")
        latex.append(f"% 密度 {density_str} - 分区策略对比")
        latex.append(f"% ========================================")
        latex.append("\\begin{figure*}[htbp]")
        latex.append("    \\centering")
        
        for idx, strategy in enumerate(strategies):
            # 处理threshold策略的文件名
            if strategy.startswith('threshold_tau'):
                strategy_label = strategy.replace('_', ' ').replace('tau', 'τ=')
            else:
                strategy_label = strategy_labels.get(strategy, strategy)
            
            latex.append(f"    \\begin{{subfigure}}[b]{{0.24\\textwidth}}")
            latex.append(f"        \\centering")
            latex.append(f"        \\includegraphics[width=\\textwidth]{{{output_dir}/density_{density_str}_{strategy}.png}}")
            latex.append(f"        \\caption{{{strategy_label}}}")
            latex.append(f"        \\label{{fig:{density_str}_{strategy}}}")
            latex.append(f"    \\end{{subfigure}}")
            if idx < len(strategies) - 1:
                latex.append(f"    \\hfill")
        
        latex.append(f"    \\caption{{Partition strategies for $\\lambda_{{bs}}={density_str}$}}")
        latex.append(f"    \\label{{fig:partition_{density_str}}}")
        latex.append("\\end{figure*}")
        latex.append("")
    
    return '\n'.join(latex)


def generate_threshold_analysis_latex(output_dir='output/threshold_analysis'):
    """
    生成阈值分析曲线的LaTeX代码
    
    Parameters:
    -----------
    output_dir : str
        图片输出目录
    
    Returns:
    --------
    str : LaTeX代码
    """
    latex = []
    latex.append("% ========================================")
    latex.append("% 阈值分析曲线")
    latex.append("% ========================================")
    
    metrics = [
        ('threshold_avg_instances.png', 'Average instances per node', 'fig:threshold_avg_instances'),
        ('threshold_max_instances.png', 'Maximum instances per node', 'fig:threshold_max_instances'),
        ('threshold_avg_size.png', 'Average partition size', 'fig:threshold_avg_size'),
        ('threshold_max_size.png', 'Maximum partition size', 'fig:threshold_max_size'),
        ('threshold_num_partitions.png', 'Number of partitions', 'fig:threshold_num_partitions'),
        ('threshold_dual_axis.png', 'Trade-off between partition size and instances', 'fig:threshold_dual_axis'),
        ('threshold_composite_score.png', 'Composite score', 'fig:threshold_composite_score'),
    ]
    
    # 生成单图
    for filename, caption, label in metrics:
        latex.append("\\begin{figure}[htbp]")
        latex.append("    \\centering")
        latex.append(f"    \\includegraphics[width=0.7\\textwidth]{{{output_dir}/{filename}}}")
        latex.append(f"    \\caption{{{caption} vs. merge threshold}}")
        latex.append(f"    \\label{{{label}}}")
        latex.append("\\end{figure}")
        latex.append("")
    
    return '\n'.join(latex)


def generate_all_figures_latex(densities, seeds, threshold_strategies,
                               output_dir_base='output'):
    """
    生成所有图片的LaTeX代码
    
    Parameters:
    -----------
    densities : list
        密度列表
    seeds : list
        随机种子列表
    threshold_strategies : dict
        {density: threshold} 最优阈值字典
    output_dir_base : str
        输出目录基础路径
    
    Returns:
    --------
    str : 完整的LaTeX代码
    """
    latex = []
    latex.append("% ================================================================")
    latex.append("% 自动生成的图片LaTeX代码")
    latex.append("% 生成时间: " + "\\today")
    latex.append("% ================================================================")
    latex.append("")
    
    # 空间拓扑和MEC拓扑对比
    latex.append(generate_density_comparison_latex(densities, seeds, 
                                                   f'{output_dir_base}/density_comparison'))
    latex.append("")
    
    # 分区策略对比
    for density in densities:
        threshold = threshold_strategies.get(density, 4)
        strategies = ['maximal_clique', f'threshold_tau{threshold}', 'neighbor_based', 'global']
        latex.append(generate_partition_strategy_latex([density], strategies,
                                                       f'{output_dir_base}/density_comparison'))
    
    # 阈值分析曲线
    latex.append(generate_threshold_analysis_latex(f'{output_dir_base}/threshold_analysis'))
    
    return '\n'.join(latex)


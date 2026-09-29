"""LaTeX表格生成工具"""


def generate_topology_features_table(data, densities, single_column=True,
                                     table_width=r'\columnwidth'):
    """
    生成拓扑特征统计表格（LaTeX格式）
    
    Parameters:
    -----------
    data : dict
        {density: {'topology': stats, ...}}
    densities : list
        密度列表
    single_column : bool
        True 时使用单栏 table 与可配置宽度。
    table_width : str
        LaTeX 宽度表达式，用法同 \\includegraphics[width=...]。
    
    Returns:
    --------
    str : LaTeX表格代码
    """
    width = table_width if single_column else r'\textwidth'
    colspec = (
        "{>{\\centering\\arraybackslash}p{1.8cm}|"
        "*{6}{>{\\centering\\arraybackslash}X}}"
    )
    latex = []
    latex.append("\\begin{table}[!t]" if single_column else "\\begin{table*}")
    latex.append("\\centering")
    latex.append("\\caption{Topology Feature Statistics for Different Base Station Densities}")
    latex.append("\\label{tab:topology_features}")
    latex.append("\\scriptsize" if single_column else "\\footnotesize")
    latex.append("\\setlength{\\tabcolsep}{3pt}" if single_column
                 else "\\setlength{\\tabcolsep}{10pt}")
    latex.append("\\renewcommand{\\arraystretch}{1.05}" if single_column
                 else "\\renewcommand{\\arraystretch}{1.15}")
    if single_column:
        latex.append(f"% table width (adjust like \\includegraphics[width=...]): {width}")
    latex.append(f"\\begin{{tabularx}}{{{width}}}{colspec}")
    latex.append("\\hline")
    latex.append("\\textbf{$\\lambda_{bs}$} & \\textbf{Nodes} & \\textbf{Edges} & \\textbf{Avg Degree} & \\textbf{Max Degree} & \\textbf{Density} & \\textbf{Clustering} \\\\")
    latex.append("\\hline")
    
    # 添加数据行
    for density in densities:
        stats = data[density]['topology']
        # 处理字符串类型的密度标签（如'Real'）
        if isinstance(density, str):
            density_str = density
        else:
            density_str = f"{density:.0e}".replace('e-0', 'e-')
        latex.append(f"{density_str} & {stats['nodes']} & {stats['edges']} & "
                    f"{stats['avg_degree']:.2f} & {stats['max_degree']} & "
                    f"{stats['density']:.3f} & {stats['clustering']:.3f} \\\\")
    
    latex.append("\\hline")
    latex.append("\\end{tabularx}")
    latex.append("\\end{table}" if single_column else "\\end{table*}")
    
    return '\n'.join(latex)


def generate_partition_comparison_table(data, densities, single_column=False,
                                      table_width=r'\textwidth', alpha=0.4):
    """
    生成分区策略综合对比表格（LaTeX格式）
    
    Parameters:
    -----------
    data : dict
        {density: {'partitions': {strategy: stats, ...}, ...}}
    densities : list
        密度列表
    single_column : bool
        True 时使用单栏 table 与可配置宽度。
    table_width : str
        LaTeX 宽度表达式，用法同 \\includegraphics[width=...]。
    alpha : float
        综合评分权重 α，用于脚注。
    
    Returns:
    --------
    str : LaTeX表格代码
    """
    width = table_width if single_column else r'\textwidth'
    beta = 1.0 - alpha
    colspec = (
        "{>{\\centering\\arraybackslash}p{1.8cm}"
        ">{\\raggedright\\arraybackslash}X|"
        ">{\\centering\\arraybackslash}p{1.3cm}>"
        "{\\centering\\arraybackslash}p{1.3cm}>"
        "{\\centering\\arraybackslash}p{1.3cm}>"
        "{\\centering\\arraybackslash}p{1.3cm}>"
        "{\\centering\\arraybackslash}p{1.3cm}}"
    )
    latex = []
    latex.append("\\begin{table}[!t]" if single_column else "\\begin{table*}")
    latex.append("\\centering")
    latex.append("\\caption{Comprehensive Comparison of Partition Strategies Across Different Topologies}")
    latex.append("\\label{tab:partition_comparison_full}")
    latex.append("\\scriptsize" if single_column else "\\footnotesize")
    latex.append("\\setlength{\\tabcolsep}{3pt}" if single_column
                 else "\\setlength{\\tabcolsep}{8pt}")
    latex.append("\\renewcommand{\\arraystretch}{1.05}" if single_column
                 else "\\renewcommand{\\arraystretch}{1.1}")
    latex.append(f"% table width (adjust like \\includegraphics[width=...]): {width}")
    latex.append(f"\\begin{{tabularx}}{{{width}}}{colspec}")
    latex.append("\\hline")
    latex.append("\\textbf{$\\lambda_{bs}$} & \\textbf{Method} & \\textbf{Regions} & \\textbf{Avg Inst.} & \\textbf{Max Inst.} & \\textbf{Avg Size} & \\textbf{Max Size} \\\\")
    latex.append("\\hline")
    
    def _display_strategy_name(key):
        if key.startswith('SA ('):
            return key.replace('SA (', 'Simulated Annealing (', 1)
        return key

    def _ordered_strategy_keys(partitions):
        """按表格行顺序返回分区策略键名。"""
        keys = ['Global', 'Neighbor-based', 'Maximal Clique']
        keys.append(next(k for k in partitions if 'Greedy' in k))
        keys.append(next(
            k for k in partitions
            if k.startswith('SA (') or k.startswith('Simulated Annealing')
        ))
        keys.append(next(k for k in partitions if 'Threshold' in k))
        return keys

    for idx, density in enumerate(densities):
        # 处理字符串类型的密度标签（如'Real'）
        if isinstance(density, str):
            density_str = density
        else:
            density_str = f"{density:.0e}".replace('e-0', 'e-')
        partitions = data[density]['partitions']
        strategy_keys = _ordered_strategy_keys(partitions)
        n_strategies = len(strategy_keys)

        for row_idx, key in enumerate(strategy_keys):
            stats = partitions[key]
            name = _display_strategy_name(key)
            if row_idx == 0:
                latex.append(
                    f"\\multirow{{{n_strategies}}}{{*}}{{{density_str}}} & {name} & "
                    f"{stats['num_partitions']} & "
                    f"{stats['avg_instances']:.2f} & {stats['max_instances']} & "
                    f"{stats['avg_size']:.2f} & {stats['max_size']} \\\\"
                )
            else:
                latex.append(
                    f" & {name} & {stats['num_partitions']} & "
                    f"{stats['avg_instances']:.2f} & {stats['max_instances']} & "
                    f"{stats['avg_size']:.2f} & {stats['max_size']} \\\\"
                )
        
        # 添加分隔线（除了最后一个密度）
        if idx < len(densities) - 1:
            latex.append("\\cline{2-7}")
    
    latex.append("\\hline")
    latex.append(
        f"\\multicolumn{{7}}{{l}}{{\\tiny $^*$ Optimal threshold selected by composite "
        f"score minimization ($\\alpha{{=}}{alpha:g}$, $\\beta{{=}}{beta:g}$).}} \\\\"
        if single_column else
        f"\\multicolumn{{7}}{{l}}{{\\footnotesize $^*$ Optimal threshold selected by "
        f"composite score minimization ($\\alpha={alpha:g}$, $\\beta={beta:g}$).}} \\\\"
    )
    latex.append(
        "\\multicolumn{7}{l}{\\tiny Greedy and Simulated Annealing use the same "
        "$\\tau$ as a hard region-size upper bound.} \\\\"
        if single_column else
        "\\multicolumn{7}{l}{\\footnotesize Greedy and Simulated Annealing use the same "
        "$\\tau$ as a hard region-size upper bound.} \\\\"
    )
    latex.append("\\end{tabularx}")
    latex.append("\\end{table}" if single_column else "\\end{table*}")
    
    return '\n'.join(latex)


def _format_topology_label(label):
    """将拓扑标签格式化为 LaTeX 表格中的 λ_bs 显示形式。"""
    if label == 'Real':
        return 'Real'
    try:
        return f"{float(label):.0e}".replace('e-0', 'e-')
    except (TypeError, ValueError):
        return str(label)


def _format_density_for_caption(label):
    """将拓扑标签格式化为 caption 中的 $\\lambda_{bs}=...$ 形式。"""
    s = _format_topology_label(label)
    if s == 'Real':
        return 'real-world topology'
    if 'e' in s:
        mantissa, exponent = s.split('e', 1)
        return f"$\\lambda_{{bs}}={mantissa}\\times10^{{{exponent}}}$"
    return f"$\\lambda_{{bs}}={s}$"


def generate_overlap_sensitivity_table(results, single_column=True,
                                       table_width=r'\columnwidth',
                                       fixed_tau=None, alpha=0.4):
    """
    生成空间重叠率敏感性分析表格（LaTeX 格式）。

    Parameters
    ----------
    results : dict
        {topology_label: [row_dict, ...]}，row_dict 含 target_rho, rho, tau_star,
        num_partitions, avg_size, max_size, avg_instances, max_instances。
    single_column : bool
        True 时生成单栏 table（缩写表头、ρ/τ* 分列、密度写入 caption）；
        False 时生成双栏 table*（与 partition_comparison 版式一致）。
    table_width : str
        LaTeX 宽度表达式，用法同 \\includegraphics[width=...]，
        如 r'\\columnwidth'、r'0.9\\columnwidth'、r'\\textwidth'。
    fixed_tau : int or None
        固定阈值；None 表示表中为最优 τ*。
    alpha : float
        综合评分 S(τ)=α·Ī+β·R̄ 的 α（β=1-α），用于脚注。

    Returns
    -------
    str : LaTeX 表格代码
    """
    labels = list(results.keys())
    width = table_width if single_column else r'\textwidth'
    tau_col_header = r'\textbf{$\tau$}' if fixed_tau is not None else r'\textbf{$\tau^*$}'
    beta = 1.0 - alpha

    if single_column:
        density_caption = _format_density_for_caption(labels[0])
        tau_caption = (f", fixed $\\tau={fixed_tau}$"
                       if fixed_tau is not None else "")
        colspec = (
            "{>{\\centering\\arraybackslash}p{0.55cm}"
            ">{\\centering\\arraybackslash}p{0.5cm}|"
            "*{5}{>{\\centering\\arraybackslash}X}}"
        )
        latex = []
        latex.append("\\begin{table}[!t]")
        latex.append("\\centering")
        latex.append(
            f"\\caption{{Impact of Spatial Overlap Ratio $\\rho$ on "
            f"Threshold-based Partitioning ({density_caption}{tau_caption})}}"
        )
        latex.append("\\label{tab:overlap_sensitivity}")
        latex.append("\\scriptsize")
        latex.append("\\setlength{\\tabcolsep}{3pt}")
        latex.append("\\renewcommand{\\arraystretch}{1.05}")
        latex.append(f"% table width (adjust like \\includegraphics[width=...]): {width}")
        latex.append(f"\\begin{{tabularx}}{{{width}}}{colspec}")
        latex.append("\\hline")
        latex.append(
            f"\\textbf{{$\\rho$}} & {tau_col_header} & \\textbf{{Regions}} & "
            "\\textbf{Avg Inst.} & \\textbf{Max Inst.} & "
            "\\textbf{Avg Size} & \\textbf{Max Size} \\\\"
        )
        latex.append("\\hline")
        for label in labels:
            for row in results[label]:
                latex.append(
                    f"{row['rho']:.2f} & {row['tau_star']} & "
                    f"{row['num_partitions']} & "
                    f"{row['avg_instances']:.2f} & {row['max_instances']} & "
                    f"{row['avg_size']:.2f} & {row['max_size']} \\\\"
                )
        latex.append("\\hline")
        if fixed_tau is not None:
            latex.append(
                f"\\multicolumn{{7}}{{l}}{{\\tiny Fixed merge threshold "
                f"$\\tau={fixed_tau}$ across all $\\rho$ levels.}} \\\\"
            )
        else:
            latex.append(
                f"\\multicolumn{{7}}{{l}}{{\\tiny $^*$ $\\tau^*$ selected by composite "
                f"score ($\\alpha{{=}}{alpha:g}$, $\\beta{{=}}{beta:g}$).}} \\\\"
            )
        latex.append(
            "\\multicolumn{7}{l}{\\tiny "
            "$\\rho = 2|\\mathcal{E}|/(N_m(N_m{-}1))$ (measured); radii scaled on a "
            "fixed node set.} \\\\"
        )
        latex.append("\\end{tabularx}")
        latex.append("\\end{table}")
        return '\n'.join(latex)

    # 双栏 table*：与 partition_comparison 版式一致
    colspec = (
        "{>{\\centering\\arraybackslash}p{1.8cm}"
        ">{\\raggedright\\arraybackslash}X|"
        ">{\\centering\\arraybackslash}p{1.3cm}>"
        "{\\centering\\arraybackslash}p{1.3cm}>"
        "{\\centering\\arraybackslash}p{1.3cm}>"
        "{\\centering\\arraybackslash}p{1.3cm}>"
        "{\\centering\\arraybackslash}p{1.3cm}}"
    )

    latex = []
    latex.append("\\begin{table*}")
    latex.append("\\centering")
    latex.append("\\caption{Impact of Spatial Overlap Ratio $\\rho$ on "
                 "Threshold-based Partitioning Results}")
    latex.append("\\label{tab:overlap_sensitivity}")
    latex.append("\\footnotesize")
    latex.append("\\setlength{\\tabcolsep}{8pt}")
    latex.append("\\renewcommand{\\arraystretch}{1.1}")
    latex.append(f"% table width: {width}")
    latex.append(f"\\begin{{tabularx}}{{{width}}}{colspec}")
    latex.append("\\hline")
    latex.append(
        "\\textbf{$\\lambda_{bs}$} & \\textbf{$\\rho$} & \\textbf{Regions} & "
        "\\textbf{Avg Inst.} & \\textbf{Max Inst.} & "
        "\\textbf{Avg Size} & \\textbf{Max Size} \\\\"
    )
    latex.append("\\hline")

    n_rho = len(next(iter(results.values())))

    for idx, label in enumerate(labels):
        rows = results[label]
        density_str = _format_topology_label(label)
        for i, row in enumerate(rows):
            rho_label = (f"{row['rho']:.2f} "
                         f"($\\tau^*={row['tau_star']}^*$)")
            if i == 0:
                latex.append(
                    f"\\multirow{{{n_rho}}}{{*}}{{{density_str}}} & {rho_label} & "
                    f"{row['num_partitions']} & "
                    f"{row['avg_instances']:.2f} & {row['max_instances']} & "
                    f"{row['avg_size']:.2f} & {row['max_size']} \\\\"
                )
            else:
                latex.append(
                    f" & {rho_label} & {row['num_partitions']} & "
                    f"{row['avg_instances']:.2f} & {row['max_instances']} & "
                    f"{row['avg_size']:.2f} & {row['max_size']} \\\\"
                )
        if idx < len(labels) - 1:
            latex.append("\\cline{2-7}")

    latex.append("\\hline")
    latex.append(
        f"\\multicolumn{{7}}{{l}}{{\\footnotesize $^*$ Optimal threshold selected by "
        f"composite score minimization ($\\alpha={alpha:g}$, $\\beta={beta:g}$).}} \\\\"
    )
    latex.append(
        "\\multicolumn{7}{l}{\\footnotesize "
        "$\\rho = 2|\\mathcal{E}|/(N_m(N_m-1))$ (measured); coverage radii are uniformly "
        "scaled on a fixed node set.} \\\\"
    )
    latex.append("\\end{tabularx}")
    latex.append("\\end{table*}")

    return '\n'.join(latex)


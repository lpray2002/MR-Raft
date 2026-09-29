#!/usr/bin/env python3
"""
汇总所有客户端容器的压测结果
从results目录读取所有node_*_result.json文件，并生成汇总报告
"""

import json
import sys
import os
from pathlib import Path

def load_results(results_dir):
    """加载所有节点的结果文件"""
    results = []
    results_path = Path(results_dir)
    
    if not results_path.exists():
        print(f"❌ 结果目录不存在: {results_dir}")
        return results
    
    # 查找所有node_*_result.json文件
    for json_file in sorted(results_path.glob("node_*_result.json")):
        try:
            with open(json_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                # 每个文件包含一个节点的结果数组
                if isinstance(data, list) and len(data) > 0:
                    results.extend(data)
                    print(f"✓ 加载: {json_file.name}")
        except Exception as e:
            print(f"❌ 加载失败 {json_file.name}: {e}")
    
    return results

def print_summary(results):
    """打印汇总统计"""
    if not results:
        print("\n❌ 没有找到任何结果数据")
        return
    
    # 按节点ID排序
    results.sort(key=lambda x: x['node_id'])
    
    print("\n" + "="*80)
    print("压测结果汇总")
    print("="*80)
    
    # 单节点统计
    print("\n节点详细统计:")
    print(f"{'节点ID':<8} {'总请求':<12} {'成功':<12} {'失败':<10} {'TPS':<10} {'平均延迟(ms)':<15} {'P95(ms)':<10} {'P99(ms)':<10}")
    print("-"*80)
    
    total_requests = 0
    total_success = 0
    total_failed = 0
    total_tps = 0
    weighted_latency = 0
    
    for result in results:
        node_id = result['node_id']
        total_reqs = result['total_requests']
        success_reqs = result['success_requests']
        failed_reqs = result['failed_requests']
        tps = result['tps']
        avg_latency = result['avg_latency_ms']
        p95_latency = result['p95_latency_ms']
        p99_latency = result['p99_latency_ms']
        
        print(f"{node_id:<8} {total_reqs:<12} {success_reqs:<12} {failed_reqs:<10} "
              f"{tps:<10.0f} {avg_latency:<15.3f} {p95_latency:<10.3f} {p99_latency:<10.3f}")
        
        total_requests += total_reqs
        total_success += success_reqs
        total_failed += failed_reqs
        total_tps += tps
        weighted_latency += avg_latency * success_reqs
    
    # 总体统计
    print("-"*80)
    avg_latency_overall = weighted_latency / total_success if total_success > 0 else 0
    success_rate = (total_success / total_requests * 100) if total_requests > 0 else 0
    
    print(f"\n总体统计:")
    print(f"  总请求数:     {total_requests}")
    print(f"  成功请求:     {total_success}")
    print(f"  失败请求:     {total_failed}")
    print(f"  成功率:       {success_rate:.2f}%")
    print(f"  总TPS:        {total_tps:.0f}")
    print(f"  加权平均延迟: {avg_latency_overall:.3f} ms")
    
    print("\n" + "="*80)

def save_summary_json(results, output_file):
    """保存汇总结果为JSON"""
    if not results:
        return
    
    # 计算总体统计
    total_requests = sum(r['total_requests'] for r in results)
    total_success = sum(r['success_requests'] for r in results)
    total_failed = sum(r['failed_requests'] for r in results)
    total_tps = sum(r['tps'] for r in results)
    weighted_latency = sum(r['avg_latency_ms'] * r['success_requests'] for r in results)
    avg_latency_overall = weighted_latency / total_success if total_success > 0 else 0
    success_rate = (total_success / total_requests * 100) if total_requests > 0 else 0
    
    summary = {
        "nodes": results,
        "summary": {
            "total_requests": total_requests,
            "success_requests": total_success,
            "failed_requests": total_failed,
            "success_rate": success_rate,
            "total_tps": total_tps,
            "avg_latency_ms": avg_latency_overall
        }
    }
    
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    
    print(f"\n✅ 汇总结果已保存到: {output_file}")

def main():
    if len(sys.argv) < 2:
        print(f"用法: {sys.argv[0]} <results_dir> [output_json]")
        print(f"")
        print(f"参数说明:")
        print(f"  results_dir  - 结果文件目录（包含node_*_result.json文件）")
        print(f"  output_json  - 可选，汇总结果输出文件（默认：summary.json）")
        sys.exit(1)
    
    results_dir = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else "summary.json"
    
    print(f"📊 开始汇总结果...")
    print(f"  结果目录: {results_dir}")
    
    # 加载所有结果
    results = load_results(results_dir)
    
    if not results:
        print("\n❌ 没有找到任何结果文件")
        sys.exit(1)
    
    # 打印汇总
    print_summary(results)
    
    # 保存JSON
    save_summary_json(results, output_file)

if __name__ == '__main__':
    main()

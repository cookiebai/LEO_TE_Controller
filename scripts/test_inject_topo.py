#!/usr/bin/env python3
"""
测试脚本：模拟数据平面注入队列数据，验证 Lyapunov 引擎能正确读取

使用说明：
1. 先运行此脚本注入测试数据
2. 然后运行 lyapunov_solver.py 观察是否能读取到队列数据
"""

import redis
import time
import random
import json

r = redis.Redis(host='127.0.0.1', port=6379, decode_responses=True)

def inject_topology_with_capacity():
    """注入带有 capacity 字段的拓扑数据"""
    print("=" * 60)
    print("Step 1: 注入卫星网络拓扑数据（含 capacity 字段）")
    print("=" * 60)

    links = [
        # 核心卫星到核心节点
        ("sat1", "sat_core_1", 10.0, 1000.0, 15.0),  # src, dst, delay, capacity(Mbps), utilization
        ("sat2", "sat_core_1", 12.0, 1000.0, 25.0),
        ("sat3", "sat_core_1", 8.0, 1000.0, 10.0),

        # ISL 链路
        ("sat1", "sat2", 15.0, 500.0, 30.0),
        ("sat2", "sat3", 18.0, 500.0, 85.0),  # 拥塞链路！
        ("sat1", "sat3", 20.0, 500.0, 10.0),

        # 备用绕行路径
        ("sat2", "sat4", 14.0, 400.0, 20.0),
        ("sat4", "sat_core_1", 10.0, 600.0, 15.0),

        # 更多节点
        ("sat3", "sat5", 16.0, 400.0, 45.0),
        ("sat5", "sat_core_1", 11.0, 800.0, 30.0),
    ]

    for src, dst, delay, capacity, util in links:
        link_id = f"{src}_{dst}"
        r.hset(f"topo:link:{link_id}", mapping={
            "src": src,
            "dst": dst,
            "delay": delay,
            "capacity": capacity,
            "utilization": util,
            "status": "UP"
        })
        print(f"  注入链路: {src} -> {dst} (delay={delay}ms, cap={capacity}Mbps, util={util}%)")

    print(f"\n拓扑数据注入完成，共 {len(links)} 条链路\n")


def inject_queue_telemetry():
    """注入队列积压数据（模拟 queue_agent.py 的输出）"""
    print("=" * 60)
    print("Step 2: 注入队列积压数据（模拟数据平面遥测探针）")
    print("=" * 60)

    # 每个节点的队列数据
    # 格式: telemetry:queue:<node_id> -> {iface: backlog_bytes}
    queue_data = {
        "sat1": {
            "eth_sat2": 15000,      # sat1->sat2 队列: 15KB
            "eth_sat3": 5000,       # sat1->sat3 队列: 5KB
            "eth_sat_core_1": 20000,  # sat1->core 队列: 20KB
        },
        "sat2": {
            "eth_sat1": 8000,
            "eth_sat3": 95000,     # sat2->sat3 队列: 95KB (拥塞！)
            "eth_sat_core_1": 50000,  # sat2->core 队列: 50KB
            "eth_sat4": 12000,
        },
        "sat3": {
            "eth_sat1": 6000,
            "eth_sat2": 88000,     # sat3->sat2 队列: 88KB (拥塞！)
            "eth_sat5": 25000,
            "eth_sat_core_1": 15000,
        },
        "sat4": {
            "eth_sat2": 10000,
            "eth_sat_core_1": 8000,
        },
        "sat5": {
            "eth_sat3": 22000,
            "eth_sat_core_1": 18000,
        },
    }

    for node_id, ifaces in queue_data.items():
        queue_key = f"telemetry:queue:{node_id}"
        r.delete(queue_key)  # 先清除旧数据

        for iface, backlog in ifaces.items():
            r.hset(queue_key, iface, backlog)

        print(f"  节点 {node_id} 队列数据:")
        for iface, backlog in ifaces.items():
            print(f"    {iface}: {backlog} bytes")

    print(f"\n队列数据注入完成，共 {len(queue_data)} 个节点\n")


def inject_traffic_demands():
    """注入流量需求矩阵（模拟网络测量结果）"""
    print("=" * 60)
    print("Step 3: 注入流量需求矩阵")
    print("=" * 60)

    demands = [
        {"id": "flow_ctrl_1", "src": "sat1", "dst": "sat_core_1", "class": "class_1_urllc", "demand": 100},
        {"id": "flow_data_1", "src": "sat2", "dst": "sat_core_1", "class": "class_3_background", "demand": 300},
        {"id": "flow_data_2", "src": "sat3", "dst": "sat_core_1", "class": "class_2_embb", "demand": 200},
    ]

    # 存入 Redis 供 Lyapunov 引擎读取
    r.set("te:demands", json.dumps(demands))

    for d in demands:
        print(f"  流 {d['id']}: {d['src']} -> {d['dst']} ({d['class']}) demand={d['demand']}Mbps")

    print(f"\n流量需求注入完成，共 {len(demands)} 条流\n")


def verify_data():
    """验证数据注入结果"""
    print("=" * 60)
    print("Step 4: 验证数据注入")
    print("=" * 60)

    # 检查拓扑数据
    topo_keys = r.keys("topo:link:*")
    print(f"\n拓扑链路数: {len(topo_keys)}")
    for key in topo_keys[:5]:  # 只显示前5条
        data = r.hgetall(key)
        print(f"  {key}: {data}")

    # 检查队列数据
    queue_keys = r.keys("telemetry:queue:*")
    print(f"\n队列数据节点数: {len(queue_keys)}")
    for key in queue_keys:
        data = r.hgetall(key)
        print(f"  {key}: {data}")

    # 检查流量需求
    demands_str = r.get("te:demands")
    if demands_str:
        demands = json.loads(demands_str)
        print(f"\n流量需求数: {len(demands)}")


def simulate_queue_updates():
    """模拟动态队列更新（持续注入新数据）"""
    print("\n" + "=" * 60)
    print("开始模拟动态队列更新（5秒间隔，共 3 次）...")
    print("=" * 60)

    for i in range(3):
        print(f"\n--- 模拟更新 {i+1}/3 ---")

        # 更新 sat2->sat3 的队列（模拟动态拥塞）
        new_backlog = random.randint(50000, 150000)
        r.hset("telemetry:queue:sat2", "eth_sat3", new_backlog)
        r.hset("telemetry:queue:sat3", "eth_sat2", new_backlog)
        print(f"  sat2->sat3 队列更新: {new_backlog} bytes")

        time.sleep(5)


if __name__ == "__main__":
    print("\n" + "#" * 60)
    print("# 卫星网络 Lyapunov TE 数据注入测试")
    print("#" * 60 + "\n")

    # 1. 注入拓扑数据
    inject_topology_with_capacity()

    # 2. 注入队列数据
    inject_queue_telemetry()

    # 3. 注入流量需求
    inject_traffic_demands()

    # 4. 验证数据
    verify_data()

    print("\n" + "=" * 60)
    print("测试数据注入完成！")
    print("=" * 60)
    print("\n现在可以运行 lyapunov_solver.py 进行测试:")
    print("  cd /home/bai/LEO_TE_Controller")
    print("  python3 python_te_solver/lyapunov_solver.py")
    print("\n或者运行以下命令模拟动态队列更新:")
    print("  python3 -c 'import test_inject_topo; test_inject_topo.simulate_queue_updates()'")
    print("=" * 60 + "\n")

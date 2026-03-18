# scripts/test_inject_topo.py
import redis
import time

# 连接到本地 Redis
r = redis.Redis(host='127.0.0.1', port=6379, decode_responses=True)

print("开始向 Redis 注入模拟的卫星拓扑数据...")

# 1. 注入一条正常的链路 (利用率 30%)
r.hset("topo:link:sat1_sat2", mapping={
    "src": "sat1",
    "dst": "sat2",
    "delay": 15.0,
    "utilization": 30.0,
    "status": "UP"
})
print("已注入正常链路: sat1 -> sat2 (30%)")

# 2. 注入一条拥塞的链路 (利用率 95%，超过了我们设置的 80% 阈值)
# 我们假设这条链路的目的地就是我们要保护的核心节点 sat_core_1
r.hset("topo:link:sat2_sat_core_1", mapping={
    "src": "sat2",
    "dst": "sat_core_1",
    "delay": 20.0,
    "utilization": 95.0,  # 引发拥塞报警！
    "status": "UP"
})

# 3. 注入一条绕行的备用链路 (利用率 10%)
r.hset("topo:link:sat2_sat3", mapping={
    "src": "sat2",
    "dst": "sat3",
    "delay": 10.0,
    "utilization": 10.0,
    "status": "UP"
})
r.hset("topo:link:sat3_sat_core_1", mapping={
    "src": "sat3",
    "dst": "sat_core_1",
    "delay": 12.0,
    "utilization": 15.0,
    "status": "UP"
})

print("已注入拥塞链路及备用绕行拓扑！请观察 C++ 引擎的输出。")
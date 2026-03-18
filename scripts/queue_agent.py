#!/usr/bin/env python3
import os
import subprocess
import re
import time
import redis
import logging

# 环境变量：让探针知道自己是谁，以及 Redis 在哪
NODE_ID = os.environ.get('NODE_ID', 'unknown_sat')
REDIS_HOST = os.environ.get('REDIS_HOST', '192.168.1.100')
POLL_INTERVAL = float(os.environ.get('POLL_INTERVAL', '1.0')) # 采集频率，默认 1 秒

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
r = redis.Redis(host=REDIS_HOST, port=6379, decode_responses=True)

def get_interfaces():
    """获取所有非回环网卡"""
    ifaces = os.listdir('/sys/class/net/')
    return [i for i in ifaces if i != 'lo']

def collect_and_report():
    interfaces = get_interfaces()
    
    for iface in interfaces:
        try:
            # 执行 tc 命令
            cmd = f"tc -s qdisc show dev {iface}"
            output = subprocess.check_output(cmd.split(), text=True)
            
            # 正则匹配 backlog。tc 输出的典型格式： backlog 12345b 15p requeues 0
            # 我们提取字节数(b)或者包数(p)，Lyapunov 通常用字节数更精确
            match = re.search(r'backlog\s+(\d+)[bB]\s+(\d+)[pP]', output)
            
            backlog_bytes = 0
            if match:
                backlog_bytes = int(match.group(1))
            
            # 将数据上报给 Redis
            # 使用 Hash 结构: key 为 telemetry:queue:<node_id>, field 为接口名
            redis_key = f"telemetry:queue:{NODE_ID}"
            r.hset(redis_key, iface, backlog_bytes)
            
            # 也可以顺便更新一下时间戳，防止节点挂了控制器还在用老数据
            r.hset(redis_key, f"{iface}_timestamp", time.time())
            
        except Exception as e:
            logging.error(f"Failed to read interface {iface}: {e}")

if __name__ == '__main__':
    logging.info(f"Queue Monitor Agent started on {NODE_ID}. Connecting to Redis {REDIS_HOST}")
    while True:
        collect_and_report()
        time.sleep(POLL_INTERVAL)
#!/usr/bin/env python3
import sys
import json
import redis
import time

# 连接 Redis
r = redis.Redis(host='localhost', port=6379, decode_responses=True)

def process_bgp_message(line):
    try:
        msg = json.loads(line)
        # 只处理包含 BGP 路由更新的消息
        if msg.get('type') == 'update' and 'neighbor' in msg:
            update_data = msg['neighbor'].get('message', {}).get('update', {})
            
            # 解析 BGP-LS NLRI (这里以简化的 JSON 结构为例，实际 ExaBGP 格式会更深)
            # 提取节点和链路信息
            if 'attribute' in update_data and 'bgp-ls' in update_data['attribute']:
                ls_data = update_data['attribute']['bgp-ls']
                
                # 假设解析出了源、目的、利用率和延迟
                src = ls_data.get('local-node-id', 'unknown')
                dst = ls_data.get('remote-node-id', 'unknown')
                util = ls_data.get('te-metric', 0) # 借用 TE metric 表示利用率
                delay = ls_data.get('delay', 10)
                
                if src != 'unknown' and dst != 'unknown':
                    link_id = f"{src}_{dst}"
                    r.hset(f"topo:link:{link_id}", mapping={
                        "src": src,
                        "dst": dst,
                        "utilization": util,
                        "delay": delay,
                        "timestamp": time.time()
                    })
                    # 将节点加入一个集合，方便 C++ 知道总共有多少节点
                    r.sadd("topo:nodes", src)
                    r.sadd("topo:nodes", dst)
    except json.JSONDecodeError:
        pass
    except Exception as e:
        sys.stderr.write(f"Error parsing BGP-LS: {e}\n")

if __name__ == '__main__':
    # 死循环读取 ExaBGP 传来的标准输入
    while True:
        try:
            line = sys.stdin.readline()
            if not line:
                break
            process_bgp_message(line)
        except KeyboardInterrupt:
            break
#!/usr/bin/env python3
import subprocess
import re
import ipaddress
import redis
import time

r = redis.Redis(host='127.0.0.1', port=6379, decode_responses=True)

def get_containers():
    print("[1/3] 正在获取全网卫星和地面站容器列表...")
    output = subprocess.check_output(['docker', 'ps', '--format', '{{.Names}}'], text=True)
    return [n.strip() for n in output.split('\n') if n.strip() and ('Satellite' in n or 'GroundStation' in n)]

def sniff_topology():
    containers = get_containers()
    subnets = {} 
    
    print(f"[2/3] 正在潜入 {len(containers)} 个容器进行物理网段与网卡名绑定...")
    for container in containers:
        try:
            output = subprocess.check_output(['docker', 'exec', container, 'ip', '-4', 'addr', 'show'], text=True, stderr=subprocess.DEVNULL)
            # 按行解析，提取 IP 和 真实的网卡名(哈希值)
            for line in output.split('\n'):
                if 'inet ' in line:
                    parts = line.strip().split()
                    ip_str = parts[1]  # 例如 10.0.1.233/30
                    iface = parts[-1]  # 行末通常就是网卡名，例如 e2fbe4ef
                    
                    if ip_str.startswith('127.') or ip_str.startswith('172.'):
                        continue
                    
                    network = ipaddress.ip_network(ip_str, strict=False)
                    net_str = str(network)
                    
                    if net_str not in subnets:
                        subnets[net_str] = []
                    # 关键升级：把容器名和它的网卡名绑定存起来
                    subnets[net_str].append({'node': container, 'iface': iface})
        except Exception:
            pass

    print("[3/3] 开始向控制器大脑注入带网卡标识的真实拓扑图...")
    links_created = 0
    for key in r.keys("topo:link:*"): r.delete(key)
    
    for net_str, nodes in subnets.items():
        if len(nodes) == 2:
            n1, n2 = nodes[0], nodes[1]
            
            # 创建 n1 -> n2 的链路，并明确告诉大脑本地网卡叫什么 (local_iface)
            r.hset(f"topo:link:{n1['node']}_{n2['node']}", mapping={
                "src": n1['node'], "dst": n2['node'], "delay": 10.0, "capacity": 1000.0, "status": "UP",
                "local_iface": n1['iface'] # 灵魂注入！
            })
            
            # 创建 n2 -> n1 的链路
            r.hset(f"topo:link:{n2['node']}_{n1['node']}", mapping={
                "src": n2['node'], "dst": n1['node'], "delay": 10.0, "capacity": 1000.0, "status": "UP",
                "local_iface": n2['iface'] # 灵魂注入！
            })
            links_created += 2

    print("=" * 50)
    print(f"🎉 拓扑构建完毕！成功绑定 {links_created} 条带物理网卡标识的链路！")
    print("=" * 50)

if __name__ == '__main__':
    print("====== 动态拓扑实时嗅探器启动 ======")
    while True:
        try:
            sniff_topology()
            time.sleep(120)  # 每 2 分钟更新一次全网地图
        except KeyboardInterrupt:
            break
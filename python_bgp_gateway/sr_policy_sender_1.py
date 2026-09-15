import redis
import json
import time
import subprocess

r = redis.Redis(host='localhost', port=6379, decode_responses=True)

def execute_command(container_name, command):
    full_cmd = ["docker", "exec", container_name] + command.split()
    try:
        subprocess.run(full_cmd, capture_output=True, text=True, check=True)
        return True
    except subprocess.CalledProcessError:
        return False

def get_dest_ipv4(dst_node):
    """获取目标容器的 10.x.x.x IPv4 地址作为目标网段"""
    cmd = "ip -4 addr show | grep 'inet 10' | head -1 | awk '{print $2}' | cut -d'/' -f1"
    full_cmd = ["docker", "exec", dst_node, "sh", "-c", cmd]
    try:
        res = subprocess.run(full_cmd, capture_output=True, text=True, check=True)
        ip = res.stdout.strip()
        return ip if ip else None
    except:
        return None

def find_out_interface(src_node, next_node):
    """从 Redis 拓扑中查找两个相邻节点相连的本地网卡名"""
    # 遍历所有 topo:link 键，寻找匹配的源和目的
    keys = r.keys("topo:link:*")
    for key in keys:
        data = r.hgetall(key)
        if data and data.get('src') == src_node and data.get('dst') == next_node:
            return data.get('local_iface')
    return None

def send_hop_by_hop_ipv4_policy(policy):
    flow_id = policy.get('flow_id')
    path = policy.get('path')
    dst_node = path[-1]

    print("="*50)
    print(f"[下发中] 流 {flow_id} | 逐跳路由 | 终点: {dst_node}")
    print(f"完整路径: {' -> '.join(path)}")

    # 1. 自动获取目标节点的 IP
    dst_ip = get_dest_ipv4(dst_node)
    if not dst_ip:
        print(f"❌ 失败: 无法获取 {dst_node} 的 IPv4 地址")
        return

    # 2. 沿途下发静态路由
    for i in range(len(path) - 1):
        current_node = path[i]
        next_node = path[i+1]

        # 查找当前节点连接下一跳的网卡
        out_iface = find_out_interface(current_node, next_node)
        if not out_iface:
            print(f"⚠️ 警告: 未找到 {current_node} 到 {next_node} 的网卡信息")
            continue

        # 清理可能存在的旧路由并下发新路由 (直接指定出接口)
        execute_command(current_node, f"ip route del {dst_ip}")
        success = execute_command(current_node, f"ip route add {dst_ip} dev {out_iface}")

        if success:
            print(f"  ✅ 节点 {current_node:15s} : 注入前往 {dst_ip} 路由 -> {out_iface}")
        else:
            print(f"  ❌ 节点 {current_node:15s} : 路由下发失败")

    print("="*50)

if __name__ == '__main__':
    print("IPv4 Hop-by-Hop SDN 下发引擎启动...")
    while True:
        try:
            result = r.blpop("policy_queue", 0)
            if result:
                policy = json.loads(result[1])
                # 兼容旧版本格式过滤
                if 'path' in policy:
                    send_hop_by_hop_ipv4_policy(policy)
                else:
                    print(f"忽略缺失 path 字段的旧策略")
        except Exception as e:
            print(f"异常: {e}")
            time.sleep(1)
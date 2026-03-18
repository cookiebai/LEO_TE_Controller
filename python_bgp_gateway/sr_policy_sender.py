import redis
import json
import time
import subprocess

r = redis.Redis(host='localhost', port=6379, decode_responses=True)

def execute_command_in_container(container_name, command):
    """执行命令并返回成功与否及输出"""
    full_cmd = ["docker", "exec", container_name] + command.split()
    try:
        result = subprocess.run(full_cmd, capture_output=True, text=True, check=True)
        return True, result.stdout
    except subprocess.CalledProcessError as e:
        return False, e.stderr

def format_to_ipv6(node_id):
    """提取哈希并转换为标准 fd00:aaaa:bbbb::1"""
    clean_id = str(node_id).replace("Satellite_", "").replace("GroundStation_", "")
    clean_id = clean_id.replace("fd00:", "").replace("::1", "").replace(":", "").replace("_", "")
    if len(clean_id) == 8:
        return f"fd00:{clean_id[:4]}:{clean_id[4:]}::1"
    return f"fd00:{clean_id}::1"

def get_all_ifaces(container_name):
    """获取容器内所有非回环网卡名"""
    cmd = "ls /sys/class/net"
    success, out = execute_command_in_container(container_name, cmd)
    if success:
        return [iface.strip() for iface in out.split() if iface.strip() != 'lo']
    return []

def send_sr_policy_to_kernel(src, dst, sid_list):
    print("="*50)
    print(f"[执行中] 正在向 {src} 注入 SRv6 策略...")
    
    # 1. 准备地址
    real_dst_ip = format_to_ipv6(dst)
    target_prefix = f"{real_dst_ip}/128"
    
    # 2. 准备并修正 SID 序列
    # 去除重复和无效项，确保不含源节点自己
    src_ip = format_to_ipv6(src)
    clean_sids = []
    for s in sid_list:
        ip = format_to_ipv6(s)
        if ip != src_ip:
            clean_sids.append(ip)
    
    # 逻辑修正：Linux 内核要求 Segments 是按路径顺序排列的
    # 如果算法输出是 [Dst, ..., NextHop]，则需要反转
    if format_to_ipv6(dst) == clean_sids[0] and len(clean_sids) > 1:
        clean_sids = clean_sids[::-1]
    
    sids_str = ",".join(clean_sids)

    # 3. 暴力开启所有接口的 SRv6 能力
    ifaces = get_all_ifaces(src)
    for iface in ifaces:
        execute_command_in_container(src, f"sysctl -w net.ipv6.conf.{iface}.seg6_enabled=1")
    execute_command_in_container(src, "sysctl -w net.ipv6.conf.all.seg6_enabled=1")
    execute_command_in_container(src, "sysctl -w net.ipv6.conf.all.forwarding=1")

    # 4. 执行路由下发
    execute_command_in_container(src, f"ip -6 route del {target_prefix}")
    
    # 优先尝试使用搜到的第一个物理网卡（通常是 adff... 那张）
    main_iface = ifaces[0] if ifaces else None
    
    if main_iface:
        cmd = f"ip -6 route add {target_prefix} encap seg6 mode encap segs {sids_str} dev {main_iface}"
    else:
        cmd = f"ip -6 route add {target_prefix} encap seg6 mode encap segs {sids_str}"
    
    success, error = execute_command_in_container(src, cmd)
    
    if success:
        print(f"✅ 成功! 接口: {main_iface}")
        print(f"   目的: {target_prefix}")
        print(f"   路径: {sids_str}")
    else:
        print(f"❌ 失败: {error.strip()}")
        print(f"   执行命令: {cmd}")
    print("="*50)

if __name__ == '__main__':
    print("SRv6 真实下发引擎 (v3.0 强力版) 启动...")
    while True:
        try:
            result = r.blpop("policy_queue", 0)
            if result:
                policy = json.loads(result[1])
                send_sr_policy_to_kernel(policy['src'], policy['dst'], policy['sids'])
        except Exception as e:
            print(f"异常循环: {e}")
            time.sleep(1)
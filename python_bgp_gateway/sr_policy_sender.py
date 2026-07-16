import redis
import json
import time
import subprocess
import ipaddress
import hashlib

r = redis.Redis(host='localhost', port=6379, decode_responses=True)
POLICY_APPLIED_KEY = "te:policy:applied_signature"
SRV6_BASE_VERSION = 2

def execute_command_in_container(container_name, command):
    """执行命令并返回成功与否及输出"""
    full_cmd = ["docker", "exec", container_name, "sh", "-c", command]
    try:
        result = subprocess.run(full_cmd, capture_output=True, text=True, check=True)
        return True, result.stdout
    except subprocess.CalledProcessError as e:
        return False, e.stderr

def format_to_ipv6(node_id):
    """提取哈希并转换为标准 fd00:aaaa:bbbb::1"""
    value = str(node_id).strip()
    try:
        return str(ipaddress.IPv6Address(value))
    except ValueError:
        pass

    clean_id = str(node_id).replace("Satellite_", "").replace("GroundStation_", "")
    clean_id = clean_id.replace("fd00:", "").replace("::1", "").replace(":", "").replace("_", "")
    if len(clean_id) == 8:
        return f"fd00:{clean_id[:4]}:{clean_id[4:]}::1"
    return f"fd00:{clean_id}::1"

def get_dest_ipv4(dst_node):
    """获取目标容器的 10.x 数据面 IPv4 地址，供 IPv4 业务流匹配 SRv6 encap。"""
    cmd = "ip -4 -o addr show | awk '{print $4}' | grep '^10\\.' | cut -d'/' -f1 | head -1"
    success, out = execute_command_in_container(dst_node, cmd)
    ip = out.strip() if success else ""
    return ip or None

def find_out_interface(src_node, next_node):
    """从 topo:link 中查找 src->next 的本地出口网卡。"""
    for key in r.keys("topo:link:*"):
        data = r.hgetall(key)
        if data and data.get("src") == src_node and data.get("dst") == next_node:
            return data.get("local_iface")
    return None

def find_reverse_interface(node, prev_node):
    """查找 node 从 prev_node 收包时可能使用的本地接口。"""
    return find_out_interface(node, prev_node)

def get_all_ifaces(container_name):
    """获取容器内所有非回环网卡名"""
    cmd = "ls /sys/class/net"
    success, out = execute_command_in_container(container_name, cmd)
    if success:
        return [iface.strip() for iface in out.split() if iface.strip() != 'lo']
    return []

def build_sid_list(policy):
    """兼容求解器新格式 path 和旧格式 sids。Linux seg6 的 segs 使用正向路径。"""
    path = policy.get("path")
    if path and len(path) >= 2:
        return [format_to_ipv6(node) for node in path[1:]], path

    raw_sids = policy.get("sids") or []
    sids = [format_to_ipv6(s) for s in raw_sids]
    dst_ip = format_to_ipv6(policy.get("dst"))
    if len(sids) > 1 and sids[0] == dst_ip:
        sids = sids[::-1]
    path = [policy.get("src")] + raw_sids
    return sids, path

def policy_signature(policy, sids):
    payload = {
        "flow_id": policy.get("flow_id", ""),
        "src": policy.get("src", ""),
        "dst": policy.get("dst", ""),
        "path": policy.get("path", []),
        "sids": sids,
        "weight": round(float(policy.get("weight", 1.0)), 4),
        "srv6_base_version": SRV6_BASE_VERSION,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def enable_srv6(container_name):
    ifaces = get_all_ifaces(container_name)
    for iface in ifaces:
        execute_command_in_container(container_name, f"sysctl -w net.ipv6.conf.{iface}.seg6_enabled=1 >/dev/null")
        execute_command_in_container(container_name, f"sysctl -w net.ipv6.conf.{iface}.proxy_ndp=1 >/dev/null")
    execute_command_in_container(container_name, "sysctl -w net.ipv6.conf.all.seg6_enabled=1 >/dev/null")
    execute_command_in_container(container_name, "sysctl -w net.ipv6.conf.default.seg6_enabled=1 >/dev/null")
    execute_command_in_container(container_name, "sysctl -w net.ipv6.conf.all.forwarding=1 >/dev/null")
    execute_command_in_container(container_name, "sysctl -w net.ipv6.conf.all.proxy_ndp=1 >/dev/null")
    execute_command_in_container(container_name, "sysctl -w net.ipv6.conf.default.proxy_ndp=1 >/dev/null")
    return ifaces

def ensure_node_sid(container_name):
    """
    为容器补齐 SRv6 SID 身份地址。

    SID 放在 lo 上，避免绑定到单个动态链路接口；同时给每个数据面接口打开
    proxy_ndp，让相邻节点按 /128 dev 路由发往该 SID 时可以完成邻居发现。
    """
    sid = format_to_ipv6(container_name)
    ifaces = enable_srv6(container_name)

    success, out = execute_command_in_container(
        container_name,
        f"ip -6 addr show dev lo | grep -qw '{sid}'"
    )
    if not success:
        ok, err = execute_command_in_container(
            container_name,
            f"ip -6 addr add {sid}/128 dev lo 2>/dev/null || true"
        )
        if not ok:
            print(f"⚠️ {container_name} 添加 SID 地址失败: {err.strip()}")

    for iface in ifaces:
        execute_command_in_container(
            container_name,
            f"ip -6 neigh replace proxy {sid} dev {iface} 2>/dev/null || true"
        )

    return sid

def prepare_srv6_path(path):
    """沿路径准备 SRv6 基础数据面：节点 SID、NDP 代理、逐段 IPv6 SID 路由。"""
    ok = True
    sid_by_node = {}

    for node in path:
        sid_by_node[node] = ensure_node_sid(node)

    for idx in range(len(path) - 1):
        current = path[idx]
        next_node = path[idx + 1]
        out_iface = find_out_interface(current, next_node)
        next_sid = sid_by_node[next_node]

        if not out_iface:
            print(f"❌ 未找到 {current} -> {next_node} 的出口接口，无法安装 SID 路由")
            ok = False
            continue

        success, output = execute_command_in_container(
            current,
            f"ip -6 route replace {next_sid}/128 dev {out_iface}"
        )
        if success:
            print(f"  [srv6-base] {current}: {next_sid}/128 -> {out_iface}")
        else:
            print(f"❌ {current} 安装到下一 SID 的 IPv6 路由失败: {output.strip()}")
            ok = False

        # 确保下一跳节点在反向接口上也代理自己的 SID，帮助当前节点完成 NDP。
        ingress_iface = find_reverse_interface(next_node, current)
        if ingress_iface:
            execute_command_in_container(
                next_node,
                f"ip -6 neigh replace proxy {next_sid} dev {ingress_iface} 2>/dev/null || true"
            )

    return ok

def send_sr_policy_to_kernel(policy):
    src = policy["src"]
    dst = policy["dst"]
    sid_list, path = build_sid_list(policy)

    print("="*50)
    src_ip = format_to_ipv6(src)
    clean_sids = [sid for sid in sid_list if sid != src_ip]
    if not clean_sids:
        print(f"❌ 失败: 策略没有可用 SID，policy={policy}")
        print("="*50)
        return

    print(f"[执行中] 流 {policy.get('flow_id', '-')}: {src} -> {dst}")
    print(f"路径: {' -> '.join(path)}")

    base_ok = prepare_srv6_path(path)

    ifaces = enable_srv6(src)
    out_iface = find_out_interface(src, path[1]) if len(path) >= 2 else None
    if not out_iface and ifaces:
        out_iface = ifaces[0]
        print(f"⚠️ 未在 topo:link 找到第一跳出口，回退到 {out_iface}")

    sids_str = ",".join(clean_sids)
    commands = []

    dst_v6 = format_to_ipv6(dst)
    commands.append((f"{dst_v6}/128", f"ip -6 route replace {dst_v6}/128 encap seg6 mode encap segs {sids_str}" + (f" dev {out_iface}" if out_iface else "")))

    dst_v4 = get_dest_ipv4(dst)
    if dst_v4:
        commands.append((f"{dst_v4}/32", f"ip route replace {dst_v4}/32 encap seg6 mode encap segs {sids_str}" + (f" dev {out_iface}" if out_iface else "")))
    else:
        print(f"⚠️ 未找到 {dst} 的 10.x IPv4，跳过 IPv4 业务流 SRv6 encap 路由")

    ok = base_ok
    for target, cmd in commands:
        success, output = execute_command_in_container(src, cmd)
        if success:
            print(f"✅ 已安装 SRv6 encap 路由: {target} -> [{sids_str}] dev={out_iface or 'auto'}")
        else:
            ok = False
            print(f"❌ 路由下发失败: {target}")
            print(f"   命令: {cmd}")
            print(f"   错误: {output.strip()}")

    if ok:
        verify_cmd = "ip -6 route show | grep 'encap seg6' || true; ip route show | grep 'encap seg6' || true"
        _, routes = execute_command_in_container(src, verify_cmd)
        print("当前 SRv6 路由:")
        print(routes.strip() or "  (未查询到 encap seg6 路由)")
        flow_id = policy.get("flow_id")
        if flow_id:
            r.hset(POLICY_APPLIED_KEY, flow_id, policy_signature(policy, clean_sids))
            print(f"已记录策略生效标记: {POLICY_APPLIED_KEY}[{flow_id}]")
    print("="*50)

if __name__ == '__main__':
    print("SRv6 策略下发引擎启动...")
    while True:
        try:
            result = r.blpop("policy_queue", 0)
            if result:
                policy = json.loads(result[1])
                if "src" not in policy or "dst" not in policy:
                    print(f"忽略缺失 src/dst 的策略: {policy}")
                    continue
                send_sr_policy_to_kernel(policy)
        except Exception as e:
            print(f"异常循环: {e}")
            time.sleep(1)

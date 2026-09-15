import argparse
import redis
import json
import time
import subprocess
import ipaddress
import hashlib
from concurrent.futures import ThreadPoolExecutor

r = redis.Redis(host='localhost', port=6379, decode_responses=True)
POLICY_APPLIED_KEY = "te:policy:applied_signature"
POLICY_APPLIED_DETAILS_KEY = "te:policy:applied"
FLOW_DST_IP_KEY = "te:flow:dst_ip"
SRV6_BASE_VERSION = 4
MAX_SEGMENTS = 16

_prepared_nodes = set()
_sid_route_cache = {}
_topology_iface_cache = None

def execute_command_in_container(container_name, command):
    """执行命令并返回成功与否及输出"""
    full_cmd = ["docker", "exec", container_name, "sh", "-c", command]
    try:
        result = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
        return True, result.stdout
    except subprocess.CalledProcessError as e:
        return False, e.stderr
    except subprocess.TimeoutExpired:
        return False, "command timed out"

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


def format_to_transit_sid(node_id):
    """Use a separate SID namespace so base routes cannot be overwritten.

    fd00::/16 identifies a node and is the locally generated ping/encap
    target.  fd01::/16 is used only inside SRHs and by per-hop base routes.
    """
    identity = int(ipaddress.IPv6Address(format_to_ipv6(node_id)))
    transit = (0xFD01 << 112) | (identity & ((1 << 112) - 1))
    return str(ipaddress.IPv6Address(transit))

def get_node_10_ips(node):
    cmd = "ip -4 -o addr show | awk '{print $4}' | grep '^10\\.' | cut -d'/' -f1"
    success, out = execute_command_in_container(node, cmd)
    if not success:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]

def get_route_src_ip(node, dst_ip):
    cmd = f"ip route get {dst_ip} 2>/dev/null | sed -n 's/.* src \\([0-9.]*\\).*/\\1/p' | head -1"
    success, out = execute_command_in_container(node, cmd)
    return out.strip() if success else ""

def get_dest_ipv4(dst_node, src_node=None):
    """获取目标容器的业务 IPv4，尽量与 TrafficManager 选择的 receiver 地址一致。"""
    ips = get_node_10_ips(dst_node)
    if not ips:
        return None

    if src_node:
        for candidate_ip in ips:
            sender_src_ip = get_route_src_ip(src_node, candidate_ip)
            if not sender_src_ip.startswith("10."):
                continue

            receiver_reply_src_ip = get_route_src_ip(dst_node, sender_src_ip)
            if receiver_reply_src_ip == candidate_ip:
                return candidate_ip

    return ips[0]

def get_policy_dest_ipv4(policy):
    flow_id = policy.get("flow_id")
    if flow_id:
        try:
            selected_ip = r.hget(FLOW_DST_IP_KEY, flow_id)
            if selected_ip:
                return selected_ip
        except Exception:
            pass
    return get_dest_ipv4(policy["dst"], policy.get("src"))

def get_first_dest_ipv4(dst_node):
    """兼容旧调用：获取目标容器第一个 10.x 数据面 IPv4。"""
    cmd = "ip -4 -o addr show | awk '{print $4}' | grep '^10\\.' | cut -d'/' -f1 | head -1"
    success, out = execute_command_in_container(dst_node, cmd)
    ip = out.strip() if success else ""
    return ip or None

def find_out_interface(src_node, next_node):
    """从 topo:link 中查找 src->next 的本地出口网卡。"""
    global _topology_iface_cache
    if _topology_iface_cache is None:
        keys = list(r.scan_iter("topo:link:*"))
        pipe = r.pipeline(transaction=False)
        for key in keys:
            pipe.hgetall(key)
        _topology_iface_cache = {}
        for data in pipe.execute():
            if (
                data
                and data.get("status", "UP") == "UP"
                and data.get("src")
                and data.get("dst")
                and data.get("local_iface")
            ):
                _topology_iface_cache[
                    (data["src"], data["dst"])
                ] = data["local_iface"]
    return _topology_iface_cache.get((src_node, next_node))

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
        return [format_to_transit_sid(node) for node in path[1:]], path

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
        "srv6_base_version": SRV6_BASE_VERSION,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def enable_srv6(container_name):
    ifaces = get_all_ifaces(container_name)
    command = (
        "sysctl -w net.ipv6.conf.all.seg6_enabled=1 >/dev/null || exit 1; "
        "sysctl -w net.ipv6.conf.default.seg6_enabled=1 >/dev/null || exit 1; "
        "sysctl -w net.ipv6.conf.all.forwarding=1 >/dev/null || exit 1; "
        "sysctl -w net.ipv6.conf.all.proxy_ndp=1 >/dev/null || exit 1; "
        "sysctl -w net.ipv6.conf.default.proxy_ndp=1 >/dev/null || exit 1; "
        "for iface in $(ls /sys/class/net); do "
        "[ \"$iface\" = lo ] && continue; "
        "sysctl -w net.ipv6.conf.$iface.seg6_enabled=1 >/dev/null || exit 1; "
        "sysctl -w net.ipv6.conf.$iface.proxy_ndp=1 >/dev/null || exit 1; "
        "done"
    )
    success, output = execute_command_in_container(container_name, command)
    return success, ifaces, output

def ensure_node_sid(container_name):
    """
    为容器补齐 SRv6 SID 身份地址。

    SID 放在 lo 上，避免绑定到单个动态链路接口；同时给每个数据面接口打开
    proxy_ndp，让相邻节点按 /128 dev 路由发往该 SID 时可以完成邻居发现。
    """
    identity_sid = format_to_ipv6(container_name)
    sid = format_to_transit_sid(container_name)
    if container_name in _prepared_nodes:
        return True, sid

    command = (
        "sysctl -w net.ipv6.conf.all.seg6_enabled=1 >/dev/null || exit 1; "
        "sysctl -w net.ipv6.conf.default.seg6_enabled=1 >/dev/null || exit 1; "
        "sysctl -w net.ipv6.conf.all.forwarding=1 >/dev/null || exit 1; "
        "sysctl -w net.ipv6.conf.all.proxy_ndp=1 >/dev/null || exit 1; "
        "sysctl -w net.ipv6.conf.default.proxy_ndp=1 >/dev/null || exit 1; "
        # `ip addr show` canonicalizes zero-padded IPv6 groups (024d -> 24d),
        # so a textual grep followed by `addr add` is not idempotent.  Let
        # iproute2 perform the idempotent update directly.
        f"ip -6 addr replace {identity_sid}/128 dev lo || exit 1; "
        f"ip -6 addr replace {sid}/128 dev lo || exit 1; "
        "count=0; "
        "for iface in $(ls /sys/class/net); do "
        "[ \"$iface\" = lo ] && continue; "
        "sysctl -w net.ipv6.conf.$iface.seg6_enabled=1 >/dev/null || exit 1; "
        "sysctl -w net.ipv6.conf.$iface.proxy_ndp=1 >/dev/null || exit 1; "
        f"ip -6 neigh replace proxy {sid} dev $iface || exit 1; "
        "count=$((count+1)); "
        "done; "
        "echo $count"
    )
    success, output = execute_command_in_container(container_name, command)
    if not success:
        print(f"❌ {container_name} 配置 SID/NDP 失败: {output.strip()}")
        return False, sid

    _prepared_nodes.add(container_name)
    try:
        iface_count = int(output.strip())
    except ValueError:
        iface_count = 0
    print(
        f"  [srv6-node] {container_name}: identity={identity_sid}, "
        f"transit={sid}, ifaces={iface_count}"
    )
    return True, sid

def prepare_srv6_paths(paths):
    """批量准备节点 SID/NDP 与所有唯一逐跳 IPv6 基础路由。"""
    nodes = sorted({node for path in paths for node in path})
    with ThreadPoolExecutor(max_workers=4) as executor:
        node_results = list(executor.map(ensure_node_sid, nodes))
    ok = all(success for success, _ in node_results)

    edges = sorted({
        (current, next_node)
        for path in paths
        for current, next_node in zip(path[:-1], path[1:])
    })
    pending = []
    for current, next_node in edges:
        out_iface = find_out_interface(current, next_node)
        next_sid = format_to_transit_sid(next_node)
        if not out_iface:
            print(
                f"❌ 未找到 {current} -> {next_node} 的出口接口，"
                "无法安装 SID 路由"
            )
            ok = False
            continue
        cache_key = (current, next_node)
        desired = (next_sid, out_iface)
        if _sid_route_cache.get(cache_key) != desired:
            pending.append((
                current,
                next_node,
                next_sid,
                out_iface,
                cache_key,
                desired,
            ))

    def install_base(item):
        current, _, next_sid, out_iface, _, _ = item
        success, output = execute_command_in_container(
            current,
            f"ip -6 route replace {next_sid}/128 dev {out_iface}",
        )
        return item, success, output

    with ThreadPoolExecutor(max_workers=4) as executor:
        base_results = list(executor.map(install_base, pending))
    for item, success, output in base_results:
        current, _, next_sid, out_iface, cache_key, desired = item
        if success:
            print(f"  [srv6-base] {current}: {next_sid}/128 -> {out_iface}")
            _sid_route_cache[cache_key] = desired
        else:
            print(
                f"❌ {current} 安装到下一 SID 的 IPv6 路由失败: "
                f"{output.strip()}"
            )
            ok = False
    return ok


def prepare_srv6_path(path):
    """兼容单策略常驻模式。"""
    return prepare_srv6_paths([path])

def send_sr_policy_to_kernel(policy):
    src = policy["src"]
    dst = policy["dst"]
    sid_list, path = build_sid_list(policy)
    flow_id = policy.get("flow_id")
    if flow_id:
        r.hdel(POLICY_APPLIED_KEY, flow_id)
        r.hdel(POLICY_APPLIED_DETAILS_KEY, flow_id)

    print("="*50)
    src_ip = format_to_transit_sid(src)
    clean_sids = [sid for sid in sid_list if sid != src_ip]
    if not clean_sids:
        print(f"❌ 失败: 策略没有可用 SID，policy={policy}")
        print("="*50)
        return False
    if len(clean_sids) > MAX_SEGMENTS:
        print(f"❌ SID 深度 {len(clean_sids)} 超过限制 {MAX_SEGMENTS}")
        return False

    print(f"[执行中] 流 {policy.get('flow_id', '-')}: {src} -> {dst}")
    print(f"路径: {' -> '.join(path)}")

    base_ok = prepare_srv6_path(path)
    if not base_ok:
        print("❌ SRv6 基础路径准备失败，不记录 applied 标记")
        print("="*50)
        return False

    out_iface = find_out_interface(src, path[1]) if len(path) >= 2 else None
    if not out_iface:
        print(f"❌ 未在 topo:link 找到 {src} 的第一跳出口")
        return False

    sids_str = ",".join(clean_sids)
    commands = []

    dst_v6 = format_to_ipv6(dst)
    commands.append((f"{dst_v6}/128", f"ip -6 route replace {dst_v6}/128 encap seg6 mode encap segs {sids_str}" + (f" dev {out_iface}" if out_iface else "")))

    dst_v4 = get_policy_dest_ipv4(policy)
    if dst_v4:
        commands.append((f"{dst_v4}/32", f"ip route replace {dst_v4}/32 encap seg6 mode encap segs {sids_str}" + (f" dev {out_iface}" if out_iface else "")))
    else:
        print(f"⚠️ 未找到 {dst} 的 10.x IPv4，跳过 IPv4 业务流 SRv6 encap 路由")

    install_and_verify = ["set -e"]
    install_and_verify.extend(cmd for _, cmd in commands)
    for target, _ in commands:
        address = target.split("/")[0]
        if ":" in address:
            install_and_verify.append(
                f"ip -6 route get {address} | grep -q 'encap seg6'"
            )
        else:
            install_and_verify.append(
                f"ip route get {address} | grep -q 'encap seg6'"
            )
    success, output = execute_command_in_container(
        src,
        "; ".join(install_and_verify),
    )
    encap_ok = success
    if encap_ok:
        for target, _ in commands:
            print(
                f"✅ 已安装并核验 SRv6 encap 路由: {target} -> "
                f"[{sids_str}] dev={out_iface}"
            )
        if flow_id:
            applied = {
                "flow_id": flow_id,
                "src": src,
                "dst": dst,
                "path": path,
                "sids": clean_sids,
                "dst_ipv4": dst_v4,
                "dst_sid": dst_v6,
                "applied_at": time.time(),
                "version": SRV6_BASE_VERSION,
            }
            pipe = r.pipeline(transaction=True)
            pipe.hset(
                POLICY_APPLIED_KEY,
                flow_id,
                policy_signature(policy, clean_sids),
            )
            pipe.hset(
                POLICY_APPLIED_DETAILS_KEY,
                flow_id,
                json.dumps(applied, separators=(",", ":")),
            )
            pipe.execute()
            print(f"已记录策略生效标记: {POLICY_APPLIED_KEY}[{flow_id}]")
    else:
        print(f"❌ SRv6 encap 路由安装或核验失败: {output.strip()}")
    print("="*50)
    return encap_ok

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Install OpenSN SRv6 policies")
    parser.add_argument(
        "--drain-and-exit",
        action="store_true",
        help="exit after the policy queue stays empty for --idle-seconds",
    )
    parser.add_argument("--idle-seconds", type=float, default=2.0)
    args = parser.parse_args()

    print("SRv6 策略下发引擎启动...")
    if args.drain_and_exit:
        policies = []
        empty_since = None
        while True:
            result = r.blpop("policy_queue", timeout=1)
            if result:
                empty_since = None
                policy = json.loads(result[1])
                if "src" not in policy or "dst" not in policy:
                    print(f"忽略缺失 src/dst 的策略: {policy}")
                    continue
                policies.append(policy)
            else:
                empty_since = empty_since or time.monotonic()
                if time.monotonic() - empty_since >= args.idle_seconds:
                    break

        if not policies:
            raise SystemExit(0)

        paths = [build_sid_list(policy)[1] for policy in policies]
        print(
            f"[batch] policies={len(policies)}, "
            f"nodes={len({node for path in paths for node in path})}, "
            f"edges={len({edge for path in paths for edge in zip(path[:-1], path[1:])})}"
        )
        if not prepare_srv6_paths(paths):
            print("❌ 批量 SRv6 基础数据面准备失败")
            raise SystemExit(1)

        with ThreadPoolExecutor(max_workers=4) as executor:
            applied = list(executor.map(send_sr_policy_to_kernel, policies))
        if not all(applied):
            print(
                f"❌ 策略批量下发不完整: "
                f"success={sum(applied)}/{len(applied)}"
            )
            raise SystemExit(1)
        raise SystemExit(0)

    empty_since = None
    while True:
        try:
            result = r.blpop("policy_queue", 0)
            if result:
                empty_since = None
                policy = json.loads(result[1])
                if "src" not in policy or "dst" not in policy:
                    print(f"忽略缺失 src/dst 的策略: {policy}")
                    continue
                send_sr_policy_to_kernel(policy)
        except Exception as e:
            print(f"异常循环: {e}")
            time.sleep(1)

#!/usr/bin/env python3
"""Collect the SRv6 checklist evidence without running throughput phases."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from typing import Iterable
import urllib.request

import redis


ROOT = Path(__file__).resolve().parents[1]
LAB = ROOT.parent
OPEN = LAB / "OpenSN-Library"
PYTHON = LAB / ".venv/bin/python"
RESULTS = ROOT / "results" / "srv6-runs"
LOCK = LAB / "runtime" / "experiment.lock"
POLICY_KEYS = (
    "policy_queue",
    "te:policy:last_signature",
    "te:policy:desired",
    "te:policy:changed_at",
    "te:policy:applied_signature",
    "te:policy:applied",
)
CONTAINER_RE = re.compile(r"^(Satellite|GroundStation)_[0-9a-f]{8}$")


@dataclass
class CommandResult:
    command: str
    returncode: int
    stdout: str
    stderr: str
    seconds: float

    @property
    def combined(self) -> str:
        parts = []
        if self.stdout:
            parts.append(self.stdout.rstrip())
        if self.stderr:
            parts.append("[stderr]\n" + self.stderr.rstrip())
        return "\n".join(parts) or "（无输出）"


def display_command(argv: Iterable[object]) -> str:
    """Render argv readably; commands here contain no untrusted shell input."""
    import shlex

    return shlex.join(str(value) for value in argv)


def run_command(
    argv: Iterable[object],
    *,
    timeout: float = 120,
    cwd: Path | None = None,
) -> CommandResult:
    values = [str(value) for value in argv]
    started = time.monotonic()
    process = subprocess.Popen(
        values,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return CommandResult(
            display_command(values),
            process.returncode,
            stdout,
            stderr,
            time.monotonic() - started,
        )
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=3)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
        return CommandResult(
            display_command(values),
            124,
            stdout,
            stderr + f"\n命令在 {timeout:.0f} 秒后超时。",
            time.monotonic() - started,
        )


def run_shell(command: str, *, timeout: float = 120, cwd: Path | None = None) -> CommandResult:
    result = run_command(
        ["/bin/bash", "-o", "pipefail", "-c", command],
        timeout=timeout,
        cwd=cwd,
    )
    result.command = command
    return result


def require_ok(result: CommandResult) -> CommandResult:
    if result.returncode != 0:
        raise RuntimeError(
            f"命令失败（退出码 {result.returncode}）：{result.command}\n{result.combined}"
        )
    return result


def command_section(title: str, result: CommandResult) -> str:
    return "\n".join(
        [
            f"### {title}",
            "",
            "执行命令：",
            "",
            "~~~~bash",
            result.command,
            "~~~~",
            "",
            f"退出码：`{result.returncode}`；耗时：`{result.seconds:.3f}` 秒。",
            "",
            "~~~~text",
            result.combined,
            "~~~~",
            "",
        ]
    )


def plain_command_section(number: int, result: CommandResult) -> str:
    output_parts = []
    if result.stdout:
        output_parts.append(result.stdout.rstrip())
    if result.stderr:
        output_parts.append(result.stderr.rstrip())
    output = "\n".join(output_parts) or "（无输出）"
    return "\n".join(
        [
            f"### 命令 {number}",
            "",
            "~~~~bash",
            result.command,
            "~~~~",
            "",
            "~~~~text",
            output,
            "~~~~",
            "",
        ]
    )


def sanitize(text: str) -> tuple[str, list[str]]:
    """Remove credential-shaped values while retaining required network data."""
    findings: list[str] = []
    if re.search(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", text):
        findings.append("PEM 私钥块")
        text = re.sub(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            "[REDACTED PRIVATE KEY]",
            text,
            flags=re.DOTALL,
        )

    assignment = re.compile(
        r"(?im)\b(password|passwd|token|access[_-]?token|refresh[_-]?token|accessToken|refreshToken|api[_-]?key|secret)\b(\s*[:=]\s*)([^\s`'\"]+|['\"][^'\"]*['\"])",
    )
    if assignment.search(text):
        findings.append("凭据形态赋值")
        text = assignment.sub(lambda match: match.group(1) + match.group(2) + "[REDACTED]", text)

    bearer = re.compile(r"(?i)\b(authorization\s*:\s*bearer\s+)[A-Za-z0-9._~+/=-]+")
    if bearer.search(text):
        findings.append("Bearer 凭据")
        text = bearer.sub(r"\1[REDACTED]", text)

    jwt = re.compile(
        r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
    )
    if jwt.search(text):
        findings.append("JWT")
        text = jwt.sub("[REDACTED JWT]", text)

    uri_credentials = re.compile(
        r"(?i)\b([a-z][a-z0-9+.-]*://)([^/@\s:]+):([^/@\s]+)@"
    )
    if uri_credentials.search(text):
        findings.append("URI 内嵌凭据")
        text = uri_credentials.sub(r"\1[REDACTED]@", text)
    return text, findings


def redact_account_paths(text: str) -> str:
    """Keep useful project-relative evidence without exporting login names."""
    text = text.replace(str(ROOT / "python_bgp_gateway"), "<PROJECT_DIR>")
    text = text.replace(str(LAB), "<PROJECT_DIR>")
    return re.sub(r"/home/[A-Za-z0-9._-]+", "<HOME>", text)


def identity_sid(node: str) -> str:
    node_id = node.split("_", 1)[-1]
    return str(ipaddress.IPv6Address(f"fd00:{node_id[:4]}:{node_id[4:]}::1"))


def transit_sid(node: str) -> str:
    identity = int(ipaddress.IPv6Address(identity_sid(node)))
    return str(ipaddress.IPv6Address((0xFD01 << 112) | (identity & ((1 << 112) - 1))))


def api_instances() -> list[dict]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open("http://127.0.0.1:8080/api/instance/", timeout=20) as response:
        payload = json.load(response)
    if payload.get("code") != 0:
        raise RuntimeError(f"WebUI API 返回失败：{payload}")
    return payload.get("data") or []


def verify_scale(scale: str, expected_satellites: int) -> tuple[int, int]:
    match = re.fullmatch(r"([1-9][0-9]*)x([1-9][0-9]*)", scale)
    if not match:
        raise RuntimeError(f"非法规模：{scale}")
    orbits, per_orbit = map(int, match.groups())
    if orbits * per_orbit != expected_satellites:
        raise RuntimeError(
            f"规模 {scale} 与 expected-satellites={expected_satellites} 不一致"
        )
    instances = api_instances()
    satellites = [item for item in instances if item.get("type") == "Satellite"]
    expected = {(str(i), str(j)) for i in range(orbits) for j in range(per_orbit)}
    actual = {
        (
            item.get("extra", {}).get("OrbitIndex"),
            item.get("extra", {}).get("SatelliteIndex"),
        )
        for item in satellites
    }
    if len(satellites) != expected_satellites or actual != expected:
        raise RuntimeError(f"当前 WebUI 拓扑不是要求的 {scale}")
    return len(satellites), len(instances) - len(satellites)


def running_nodes() -> list[str]:
    result = require_ok(run_command(["docker", "ps", "--format", "{{.Names}}"], timeout=30))
    return sorted(name for name in result.stdout.splitlines() if CONTAINER_RE.fullmatch(name))


def wait_for_telemetry(
    client: redis.Redis,
    expected_nodes: set[str],
    process: subprocess.Popen[str] | None,
    timeout: float = 90,
) -> tuple[int, float]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"queue_monitor 提前退出，退出码 {process.returncode}")
        keys = list(client.scan_iter("telemetry:queue:*"))
        actual = {
            key.removeprefix("telemetry:queue:")
            for key in keys
            if client.type(key) == "hash"
        }
        updated = float(client.get("telemetry:queue:last_update_unix") or 0)
        age = time.time() - updated
        if expected_nodes <= actual and age < 10:
            return len(actual & expected_nodes), age
        time.sleep(1)
    raise RuntimeError("queue_monitor 在 90 秒内未生成完整的新鲜遥测")


def topology_links(client: redis.Redis) -> dict[tuple[str, str], dict[str, str]]:
    links: dict[tuple[str, str], dict[str, str]] = {}
    for key in client.scan_iter("topo:link:*"):
        data = client.hgetall(key)
        if data.get("src") and data.get("dst"):
            links[(data["src"], data["dst"])] = data
    return links


def choose_policy(policies: list[dict], requested_flow: str | None) -> dict:
    eligible = [policy for policy in policies if len(policy.get("path") or []) >= 3]
    if requested_flow:
        eligible = [policy for policy in eligible if policy.get("flow_id") == requested_flow]
        if not eligible:
            raise RuntimeError(f"没有找到可用策略：{requested_flow}")
    if not eligible:
        raise RuntimeError("没有包含中间节点的已下发 SRv6 策略")
    return sorted(eligible, key=lambda item: item.get("flow_id", ""))[0]


def capture_middle(
    policy: dict,
    mid: str,
    mid_iface: str,
    pcap_path: Path,
) -> tuple[list[CommandResult], CommandResult, CommandResult]:
    remote = "/tmp/srv6_middle.pcap"
    pid_file = "/tmp/srv6_middle_tcpdump.pid"
    run_command(["docker", "exec", mid, "rm", "-f", remote, pid_file], timeout=20)
    capture_script = (
        f"echo $$ > {pid_file}; "
        f"exec tcpdump -i {mid_iface} -nn -s 0 -w {remote} ip6"
    )
    capture_argv = [
        "docker",
        "exec",
        mid,
        "timeout",
        "-s",
        "INT",
        "-k",
        "5",
        "30",
        "sh",
        "-c",
        capture_script,
    ]
    started = time.monotonic()
    capture = subprocess.Popen(
        capture_argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        ready = run_command(
            [
                "docker",
                "exec",
                mid,
                "sh",
                "-c",
                f"test -s {pid_file} && kill -0 $(cat {pid_file})",
            ],
            timeout=5,
        )
        if ready.returncode == 0:
            break
        if capture.poll() is not None:
            stdout, stderr = capture.communicate()
            raise RuntimeError(f"tcpdump 启动失败：{stdout}\n{stderr}")
        time.sleep(0.2)
    else:
        raise RuntimeError("tcpdump 未在中间节点启动")

    ping6 = CommandResult("尚未执行", 1, "", "", 0)
    ping4 = CommandResult("尚未执行", 1, "", "", 0)
    stop = CommandResult("尚未执行", 1, "", "", 0)
    try:
        ping6 = run_command(
            [
                "docker",
                "exec",
                policy["src"],
                "ping",
                "-6",
                "-c",
                "5",
                "-W",
                "2",
                policy["dst_sid"],
            ],
            timeout=20,
        )
        ping4 = run_command(
            [
                "docker",
                "exec",
                policy["src"],
                "ping",
                "-c",
                "5",
                "-W",
                "2",
                policy["dst_ipv4"],
            ],
            timeout=20,
        )
        time.sleep(1)
    finally:
        stop = run_command(
            [
                "docker",
                "exec",
                mid,
                "sh",
                "-c",
                f"test ! -s {pid_file} || kill -INT $(cat {pid_file}) 2>/dev/null || true",
            ],
            timeout=10,
        )
        try:
            stdout, stderr = capture.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            capture.kill()
            stdout, stderr = capture.communicate(timeout=5)
    capture_result = CommandResult(
        display_command(capture_argv),
        capture.returncode or 0,
        stdout,
        stderr,
        time.monotonic() - started,
    )
    copy_result = run_command(["docker", "cp", f"{mid}:{remote}", pcap_path], timeout=30)
    run_command(["docker", "exec", mid, "rm", "-f", remote, pid_file], timeout=20)
    require_ok(copy_result)
    if not pcap_path.is_file() or pcap_path.stat().st_size <= 24:
        raise RuntimeError("中间节点 PCAP 为空或无效")
    return [capture_result, stop, copy_result], ping6, ping4


def inspect_mapping_commands(
    roles: dict[str, str],
    policy_path: list[str],
    links: dict[tuple[str, str], dict[str, str]],
) -> list[tuple[str, CommandResult]]:
    results: list[tuple[str, CommandResult]] = []
    for role, node in roles.items():
        results.append(
            (
                f"{role}容器的 PID、Docker network namespace 与网络模式",
                run_command(
                    [
                        "docker",
                        "inspect",
                        "--format",
                        "PID={{.State.Pid}} SandboxKey={{.NetworkSettings.SandboxKey}} NetworkMode={{.HostConfig.NetworkMode}}",
                        node,
                    ],
                    timeout=30,
                ),
            )
        )
        results.append(
            (
                f"{role}容器接口详情",
                run_command(["docker", "exec", node, "ip", "-d", "link"], timeout=30),
            )
        )

    relevant_host_ifaces: set[str] = set()
    relevant_bridges: set[str] = set()
    for current, nxt in zip(policy_path[:-1], policy_path[1:]):
        for edge in ((current, nxt), (nxt, current)):
            data = links.get(edge) or {}
            if data.get("host_iface"):
                relevant_host_ifaces.add(data["host_iface"])
            if data.get("local_iface"):
                relevant_bridges.add(data["local_iface"])
    for iface in sorted(relevant_bridges | relevant_host_ifaces):
        results.append(
            (
                f"宿主机相关 bridge/veth：{iface}",
                run_command(["ip", "-d", "link", "show", iface], timeout=20),
            )
        )
    return results


def path_markdown(
    policy: dict,
    links: dict[tuple[str, str], dict[str, str]],
) -> tuple[str, str]:
    path = policy["path"]
    diagram = " → ".join(
        f"**{node}**（源）" if index == 0 else
        f"**{node}**（采集中间节点）" if index == 1 else
        f"**{node}**（目的）" if index == len(path) - 1 else
        node
        for index, node in enumerate(path)
    )
    rows = [
        "| 跳 | 当前节点 | 下一节点 | 当前出口接口 | 当前宿主 veth | 下一节点收包接口 | 下一节点宿主 veth | 下一 transit SID |",
        "|---:|---|---|---|---|---|---|---|",
    ]
    for index, (current, nxt) in enumerate(zip(path[:-1], path[1:]), 1):
        forward = links.get((current, nxt), {})
        reverse = links.get((nxt, current), {})
        rows.append(
            "| {index} | `{current}` | `{nxt}` | `{out_iface}` | `{out_host}` | "
            "`{in_iface}` | `{in_host}` | `{sid}` |".format(
                index=index,
                current=current,
                nxt=nxt,
                out_iface=forward.get("local_iface", "N/A"),
                out_host=forward.get("host_iface", "N/A"),
                in_iface=reverse.get("local_iface", "N/A"),
                in_host=reverse.get("host_iface", "N/A"),
                sid=transit_sid(nxt),
            )
        )
    return diagram, "\n".join(rows)


def actual_configuration(policy: dict, links: dict[tuple[str, str], dict[str, str]]) -> str:
    path = policy["path"]
    sids = ",".join(policy["sids"])
    src_iface = links[(path[0], path[1])]["local_iface"]
    lines = [
        "源节点实际 SRH 封装命令（由下发器等价生成）：",
        "",
        "~~~~bash",
        f"docker exec {path[0]} ip -6 route replace {policy['dst_sid']}/128 encap seg6 mode encap segs {sids} dev {src_iface}",
        f"docker exec {path[0]} ip route replace {policy['dst_ipv4']}/32 encap seg6 mode encap segs {sids} dev {src_iface}",
        "~~~~",
        "",
        "路径节点公共配置模式：",
        "",
        "~~~~bash",
        "sysctl -w net.ipv6.conf.all.seg6_enabled=1",
        "sysctl -w net.ipv6.conf.default.seg6_enabled=1",
        "sysctl -w net.ipv6.conf.all.forwarding=1",
        "ip -6 addr replace <fd00-identity-SID>/128 dev lo",
        "ip -6 addr replace <fd01-transit-SID>/128 dev lo",
        "ip -6 route replace <next-transit-SID>/128 dev <out-iface>",
        "~~~~",
        "",
        "> 本项目没有配置 `seg6local` 或 `action End*` 路由。中间/目的节点使用 lo 上的 fd01 transit SID、逐跳 `/128 dev` 路由以及 Linux 内核 SRH 处理；因此 PDF 所列 `seg6local / End` 项的真实结果是“不适用/未配置”，而不是漏采集。",
    ]
    return "\n".join(lines)


def update_symlink(link: Path, target: Path) -> None:
    temporary = link.with_name(link.name + ".tmp")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(os.path.relpath(target, link.parent))
    os.replace(temporary, link)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="下发 SRv6 策略并按 PDF 清单采集 README/PCAP；不执行吞吐量测试。"
    )
    parser.add_argument("--scale", default="25x20")
    parser.add_argument("--expected-satellites", type=int, default=500)
    parser.add_argument("--flow-id", help="指定用于三节点取证的 flow；默认选择首条含中间节点的策略")
    parser.add_argument("--keep-policy", action="store_true", help="完成后保留源端 encap 路由和 Redis 策略")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    started_at = datetime.now().astimezone()
    run_tag = started_at.strftime("%Y%m%d_%H%M%S") + f"_{os.getpid()}"
    run_dir = RESULTS / run_tag
    run_dir.mkdir(parents=True, exist_ok=False)
    pcap_path = run_dir / "srv6_middle.pcap"
    readme_path = run_dir / "README.md"
    queue_log = run_dir / "queue_monitor.log"
    host_commands: list[tuple[str, CommandResult]] = []
    role_commands: dict[str, list[tuple[str, CommandResult]]] = {}
    capture_evidence: CommandResult | None = None
    grep_evidence: CommandResult | None = None
    queue_process: subprocess.Popen[str] | None = None
    queue_log_stream = None
    experiment = None
    client: redis.Redis | None = None
    cleanup_message = "尚未执行清理"
    failure: Exception | None = None
    interrupted = False
    validated_pcap = False
    required_failures: list[str] = []
    summary: dict[str, object] = {}

    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("已有部署或实验正在运行") from exc

        try:
            client = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=5)
            if client.ping() is not True:
                raise RuntimeError("Redis 未返回 PONG")
            nodes = running_nodes()
            satellites = sum(name.startswith("Satellite_") for name in nodes)
            ground = sum(name.startswith("GroundStation_") for name in nodes)
            if satellites != args.expected_satellites or ground != 2:
                raise RuntimeError(
                    f"拓扑节点不完整：Satellite={satellites}/{args.expected_satellites}, GroundStation={ground}/2"
                )
            api_satellites, api_ground = verify_scale(args.scale, args.expected_satellites)
            demands = json.loads(client.get("te:demands") or "[]")
            if len(demands) != 24:
                raise RuntimeError(f"流量需求不是 24 条：{len(demands)}")
            summary.update(
                scale=args.scale,
                satellites=satellites,
                ground_stations=ground,
                api_nodes=api_satellites + api_ground,
                demands=len(demands),
            )

            os.environ["LEO_TE_RUNTIME_DIR"] = str(run_dir)
            os.environ["OPEN_SN_DIR"] = str(OPEN)
            sys.path.insert(0, str(ROOT))
            import run_te_experiment as experiment_module

            experiment = experiment_module
            stale = experiment.stop_existing_controllers()
            summary["stopped_old_controller_pids"] = stale
            removed_before = experiment.clean_srv6_encap_routes()
            client.delete(*POLICY_KEYS)
            summary["old_encap_routes_removed"] = removed_before

            topo_result = run_command(
                [PYTHON, ROOT / "python_bgp_gateway/topo_sniffer.py", "--once"],
                timeout=240,
                cwd=ROOT / "python_bgp_gateway",
            )
            require_ok(topo_result)

            queue_log_stream = queue_log.open("w")
            queue_process = subprocess.Popen(
                [str(PYTHON), "-u", "tools/queue_monitor.py"],
                cwd=OPEN,
                stdout=queue_log_stream,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            telemetry_nodes, telemetry_age = wait_for_telemetry(client, set(nodes), queue_process)
            summary["telemetry_nodes"] = telemetry_nodes
            summary["telemetry_age_seconds_at_install"] = round(telemetry_age, 3)

            policy_state = experiment.install_te_snapshot(client, len(demands))
            ping_state = experiment.validate_policy_pings(client)
            summary["policy"] = {
                key: policy_state[key]
                for key in ("desired", "applied", "queued", "solver_elapsed_seconds", "sender_elapsed_seconds")
            }
            summary["all_policy_ping"] = ping_state
            if ping_state["srv6_ipv6_ping_ok"] != len(demands) or ping_state["srv6_ipv4_ping_ok"] != len(demands):
                raise RuntimeError(f"24 条策略 Ping 未全部通过：{ping_state}")

            policies = sorted(
                (json.loads(value) for value in client.hvals("te:policy:applied")),
                key=lambda item: item["flow_id"],
            )
            policy = choose_policy(policies, args.flow_id)
            path = policy["path"]
            src, mid, dst = path[0], path[1], path[-1]
            links = topology_links(client)
            mid_reverse = links.get((mid, src))
            if not mid_reverse or not mid_reverse.get("local_iface"):
                raise RuntimeError(f"无法确定中间节点 {mid} 从 {src} 收包的接口")
            mid_iface = mid_reverse["local_iface"]
            mid_host_iface = mid_reverse.get("host_iface", "N/A")
            summary.update(
                selected_flow=policy["flow_id"],
                source=src,
                middle=mid,
                destination=dst,
                middle_rx_iface=mid_iface,
                middle_host_iface=mid_host_iface,
                path=path,
                sids=policy["sids"],
                destination_sid=policy["dst_sid"],
                destination_ipv4=policy["dst_ipv4"],
            )

            capture_results, ping6, ping4 = capture_middle(policy, mid, mid_iface, pcap_path)
            capture_raw = capture_results[0]
            capture_evidence = CommandResult(
                f"tcpdump -i {mid_iface} -nn -s 0 -w srv6_middle.pcap 'ip6'",
                capture_raw.returncode,
                capture_raw.stdout,
                capture_raw.stderr,
                capture_raw.seconds,
            )
            if ping6.returncode != 0 or ping4.returncode != 0:
                raise RuntimeError("抓包期间的端到端 Ping 失败")

            pcap_decode = run_shell(
                f"tcpdump -nn -vv -r {display_command([pcap_path])} 'ip6'",
                timeout=60,
            )
            pcap_srh = run_shell(
                f"tcpdump -nn -vv -r {display_command([pcap_path])} 'ip6 protochain 43'",
                timeout=60,
            )
            if pcap_decode.returncode != 0:
                raise RuntimeError("PCAP 无法由 tcpdump 正常解码")
            srh_packets = len(re.findall(r"\bRT6\b", pcap_srh.stdout))
            if pcap_srh.returncode != 0 or srh_packets < 1:
                raise RuntimeError("PCAP 中未核验到 IPv6 SRH（Routing Header type 4）")
            validated_pcap = True
            summary["pcap_bytes"] = pcap_path.stat().st_size
            summary["pcap_sha256"] = hashlib.sha256(pcap_path.read_bytes()).hexdigest()
            summary["pcap_routing_header_decode_rc"] = pcap_srh.returncode
            summary["pcap_srh_packets"] = srh_packets

            host_commands = [
                ("共享宿主机 uname", run_command(["uname", "-a"], timeout=30)),
                ("共享宿主机 iproute2 版本", run_command(["ip", "-V"], timeout=30)),
                ("共享宿主机 IPv6 地址", run_command(["ip", "-6", "addr"], timeout=60)),
                ("共享宿主机全部详细 IPv6 路由", run_command(["ip", "-d", "-6", "route", "show", "table", "all"], timeout=60)),
                ("共享宿主机全部 IPv6 路由及统计", run_command(["ip", "-s", "-d", "-6", "route", "show", "table", "all"], timeout=60)),
                ("共享宿主机 IPv6 rule", run_command(["ip", "-6", "rule", "show"], timeout=30)),
                (
                    "共享宿主机 seg6 与 IPv6 forwarding sysctl",
                    run_shell("sysctl -a 2>/dev/null | grep -E 'seg6|ipv6.*forwarding'", timeout=120),
                ),
                ("共享宿主机 Docker 容器", run_command(["docker", "ps"], timeout=60)),
                ("共享宿主机命名 network namespace", run_command(["ip", "netns", "list"], timeout=30)),
                ("共享宿主机全部 link", run_command(["ip", "link"], timeout=120)),
            ]
            for title, result in host_commands:
                if result.returncode != 0:
                    required_failures.append(f"{title}: rc={result.returncode}")

            roles = {"源": src, "中间": mid, "目的": dst}
            for role, container in roles.items():
                container_commands = [
                    (
                        f"{role}节点容器 IPv6 地址",
                        run_command(["docker", "exec", container, "ip", "-6", "addr"], timeout=30),
                    ),
                    (
                        f"{role}节点容器全部详细 IPv6 路由",
                        run_command(
                            ["docker", "exec", container, "ip", "-d", "-6", "route", "show", "table", "all"],
                            timeout=30,
                        ),
                    ),
                    (
                        f"{role}节点容器 seg6 与 IPv6 forwarding sysctl",
                        run_shell(
                            f"docker exec {container} sysctl -a 2>/dev/null | grep -E 'seg6|ipv6.*forwarding'",
                            timeout=60,
                        ),
                    ),
                ]
                role_commands[role] = container_commands
                for title, result in container_commands:
                    if result.returncode != 0:
                        required_failures.append(f"{title}: rc={result.returncode}")

            project_config_dir = ROOT / "python_bgp_gateway"
            grep_command = (
                "grep -RniE 'seg6|seg6local|action End|End\\.|segs ' "
                f"{project_config_dir}"
            )
            grep_result = run_shell(
                grep_command,
                timeout=120,
            )
            grep_result.command = "grep -RniE 'seg6|seg6local|action End|End\\.|segs ' <PROJECT_DIR>"
            grep_evidence = grep_result
            if grep_result.returncode != 0:
                required_failures.append(f"项目 SRv6 配置脚本搜索: rc={grep_result.returncode}")

            if required_failures:
                raise RuntimeError("PDF 必需命令存在失败：" + "; ".join(required_failures))

        except KeyboardInterrupt:
            interrupted = True
            failure = RuntimeError("用户中断")
        except Exception as exc:  # Keep partial evidence and still restore transient state.
            failure = exc
        finally:
            if queue_process is not None and queue_process.poll() is None:
                try:
                    os.killpg(queue_process.pid, signal.SIGTERM)
                    queue_process.wait(timeout=10)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(queue_process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    try:
                        queue_process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
            if queue_log_stream is not None:
                queue_log_stream.close()
            capture_middle_node = summary.get("middle")
            if capture_middle_node:
                run_command(
                    [
                        "docker",
                        "exec",
                        str(capture_middle_node),
                        "sh",
                        "-c",
                        "test ! -s /tmp/srv6_middle_tcpdump.pid || "
                        "kill -INT $(cat /tmp/srv6_middle_tcpdump.pid) 2>/dev/null || true",
                    ],
                    timeout=10,
                )
                run_command(
                    [
                        "docker",
                        "exec",
                        str(capture_middle_node),
                        "rm",
                        "-f",
                        "/tmp/srv6_middle_tcpdump.pid",
                        "/tmp/srv6_middle.pcap",
                    ],
                    timeout=10,
                )
            if client is not None and not args.keep_policy:
                try:
                    removed = experiment.clean_srv6_encap_routes() if experiment else 0
                    client.delete(*POLICY_KEYS)
                    cleanup_message = (
                        f"已删除 {removed} 条源端 SRv6 encap 路由并清空本次 Redis 策略；"
                        "节点、拓扑、SID 和逐跳基础路由保留。"
                    )
                except Exception as cleanup_exc:
                    cleanup_message = f"清理失败：{cleanup_exc}"
                    if failure is None:
                        failure = cleanup_exc
            elif args.keep_policy:
                cleanup_message = "按 --keep-policy 要求保留 SRv6 encap 路由与 Redis 策略。"

    artifact_findings: list[str] = []
    for text_artifact in run_dir.glob("*.log"):
        raw = text_artifact.read_text(encoding="utf-8", errors="replace")
        cleaned, findings = sanitize(redact_account_paths(raw))
        artifact_findings.extend(findings)
        if cleaned != raw:
            text_artifact.write_text(cleaned, encoding="utf-8")
    if pcap_path.exists():
        strings_result = run_command(["strings", pcap_path], timeout=30)
        if strings_result.returncode != 0:
            failure = RuntimeError(
                "PCAP 敏感信息扫描失败，拒绝发布："
                f"rc={strings_result.returncode}"
            )
        _, pcap_findings = sanitize(strings_result.stdout + "\n" + strings_result.stderr)
        if pcap_findings:
            failure = RuntimeError(
                "PCAP 可打印内容命中敏感凭据形态，拒绝发布："
                + "、".join(sorted(set(pcap_findings)))
            )

    document_parts = ["# SRv6 按节点命令与输出", ""]
    command_number = 0

    def add_group(title: str, results: list[CommandResult]) -> None:
        nonlocal command_number
        if not results:
            return
        document_parts.extend([f"## {title}", ""])
        for result in results:
            command_number += 1
            document_parts.append(plain_command_section(command_number, result))

    add_group("源/中间/目的节点共用宿主机", [result for _, result in host_commands])
    add_group(
        f"源节点容器：{summary.get('source', '')}",
        [result for _, result in role_commands.get("源", [])],
    )
    if capture_evidence is not None:
        add_group(
            f"中间节点抓包：{summary.get('middle', '')}",
            [capture_evidence],
        )
    add_group(
        f"中间节点容器：{summary.get('middle', '')}",
        [result for _, result in role_commands.get("中间", [])],
    )
    add_group(
        f"目的节点容器：{summary.get('destination', '')}",
        [result for _, result in role_commands.get("目的", [])],
    )
    if grep_evidence is not None:
        add_group("SRv6 配置脚本主机", [grep_evidence])

    if failure is None and command_number != 21:
        failure = RuntimeError(f"PDF 命令块数量错误：{command_number}/21")
    if failure is not None:
        document_parts.extend(["## 执行错误", "", str(failure), ""])

    status = "失败" if failure else "成功"
    raw_document = redact_account_paths("\n".join(document_parts))
    document, findings = sanitize(raw_document)
    findings.extend(artifact_findings)
    readme_path.write_text(document, encoding="utf-8")

    RESULTS.mkdir(parents=True, exist_ok=True)
    if failure is None and validated_pcap:
        update_symlink(ROOT / "results" / "srv6-latest", run_dir)
        update_symlink(LAB / "SRv6_命令执行结果_README.md", readme_path)
        update_symlink(LAB / "srv6_middle.pcap", pcap_path)

    print(f"运行状态：{status}")
    print(f"README：{readme_path}")
    if validated_pcap:
        print(f"PCAP：{pcap_path}")
    if failure:
        print(f"失败原因：{failure}", file=sys.stderr)
        return 130 if interrupted else 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

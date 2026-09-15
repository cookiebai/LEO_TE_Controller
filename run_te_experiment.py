#!/usr/bin/env python3
"""Short, repeatable OpenSN baseline versus SRv6-TE experiment."""

import argparse
import hashlib
import json
import os
import signal
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import redis


ROOT = Path(__file__).resolve().parent
SOLVER = ROOT / "python_te_solver" / "lyapunov_solver.py"
SENDER = ROOT / "python_bgp_gateway" / "sr_policy_sender.py"
SNIFFER = ROOT / "python_bgp_gateway" / "topo_sniffer.py"
RESULT_DIR = Path(
    os.environ.get("LEO_TE_RUNTIME_DIR", "/tmp/leo_te_experiment")
)
CONTAINER_RESULT_DIR = "/share/user/te_experiment"
UDP_PROBE = ROOT / "udp_flow_probe.py"
CONTAINER_UDP_PROBE = "/tmp/leo_te_udp_flow_probe.py"
STANDARD_DIR = ROOT.parent / "OpenSN-Library" / "TopoConfigurators" / "Standard"
FROZEN_STANDARD_PID = None
CLEANUP_REQUIRED = False

POLICY_KEYS = (
    "policy_queue",
    "te:policy:last_signature",
    "te:policy:desired",
    "te:policy:changed_at",
    "te:policy:applied_signature",
    "te:policy:applied",
)


def run(args, *, cwd=None, timeout=120, check=True):
    result = subprocess.run(
        [str(value) for value in args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(map(str, args))}\n"
            f"{result.stdout}\n{result.stderr}"
        )
    return result


def satellite_containers():
    result = run(
        ["docker", "ps", "--filter", "name=Satellite", "--format", "{{.Names}}"],
        timeout=20,
    )
    return [line for line in result.stdout.splitlines() if line]


def stop_existing_controllers():
    current_pid = os.getpid()
    result = run(["ps", "-eo", "pid=,args="], check=False)
    stopped = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) != 2:
            continue
        pid = int(parts[0])
        command = parts[1]
        if pid == current_pid:
            continue
        if (
            "lyapunov_solver.py" not in command
            and "sr_policy_sender.py" not in command
        ):
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            stopped.append(pid)
        except ProcessLookupError:
            pass
    if stopped:
        time.sleep(1)
    return stopped


def freeze_standard():
    """Freeze only this project's Standard; never signal unrelated main.py."""
    global FROZEN_STANDARD_PID
    candidates = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "cmdline").read_bytes().split(b"\0")
            if not any(arg.decode(errors="replace") in ("main.py", str(STANDARD_DIR / "main.py"))
                       for arg in command):
                continue
            if (entry / "cwd").resolve() != STANDARD_DIR.resolve():
                continue
            candidates.append(int(entry.name))
        except (OSError, ProcessLookupError):
            continue
    if len(candidates) > 1:
        raise RuntimeError(f"multiple Standard processes found: {candidates}")
    if not candidates:
        return {"pid": None, "state": "not_running"}
    pid = candidates[0]
    status = Path(f"/proc/{pid}/status").read_text()
    state = next(line for line in status.splitlines() if line.startswith("State:"))
    if "T" in state.split()[1]:
        return {"pid": pid, "state": "already_frozen"}
    os.kill(pid, signal.SIGSTOP)
    FROZEN_STANDARD_PID = pid
    print(f"froze Standard PID {pid}; will resume on exit", flush=True)
    return {"pid": pid, "state": "frozen_by_runner"}


def resume_standard():
    global FROZEN_STANDARD_PID
    if FROZEN_STANDARD_PID is not None:
        try:
            os.kill(FROZEN_STANDARD_PID, signal.SIGCONT)
            print(f"resumed Standard PID {FROZEN_STANDARD_PID}", flush=True)
        except ProcessLookupError:
            pass
        FROZEN_STANDARD_PID = None


def validate_host_resources(args):
    meminfo = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        name, value = line.split(":", 1)
        meminfo[name] = int(value.split()[0])
    available_mib = meminfo.get("MemAvailable", 0) / 1024.0
    pressure_path = Path("/proc/pressure/memory")
    full_stall_percent = 0.0
    if pressure_path.exists():
        for line in pressure_path.read_text().splitlines():
            if line.startswith("full "):
                fields = dict(field.split("=") for field in line.split()[1:])
                full_stall_percent = float(fields["avg10"])
    if (
        available_mib < args.min_host_available_mib
        or full_stall_percent > args.max_host_memory_stall_percent
    ):
        raise RuntimeError(
            "host cannot support a valid stable experiment: "
            f"MemAvailable={available_mib:.1f} MiB "
            f"(required >= {args.min_host_available_mib:.1f}), "
            f"memory full-stall avg10={full_stall_percent:.2f}% "
            f"(required <= {args.max_host_memory_stall_percent:.2f}%). "
            "Free memory or increase VM RAM before retrying."
        )
    return {
        "available_mib": available_mib,
        "memory_full_stall_avg10_percent": full_stall_percent,
    }


def wait_for_host_resources(args):
    """Wait briefly for transient memory pressure to settle between phases."""
    wait_seconds = max(0.0, float(args.resource_wait_seconds))
    deadline = time.monotonic() + wait_seconds
    last_error = None
    while True:
        try:
            return validate_host_resources(args)
        except RuntimeError as exc:
            last_error = exc
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise last_error
        delay = min(5.0, remaining)
        print(
            "host resources are temporarily busy; "
            f"waiting {delay:.0f}s before retry "
            f"({remaining:.0f}s remaining): {last_error}",
            flush=True,
        )
        time.sleep(delay)


def clean_container_srv6_routes(container):
    command = (
        "count=0; "
        "for target in $(ip -o route show | "
        "awk '/encap seg6/ {print $1}'); do "
        "ip route del \"$target\" 2>/dev/null && count=$((count+1)); "
        "done; "
        "for target in $(ip -6 -o route show | "
        "awk '/encap seg6/ {print $1}'); do "
        "ip -6 route del \"$target\" 2>/dev/null && count=$((count+1)); "
        "done; "
        "remaining=$(ip -o route show | awk '/encap seg6/ {n++} END {print n+0}'); "
        "remaining6=$(ip -6 -o route show | awk '/encap seg6/ {n++} END {print n+0}'); "
        "[ \"$remaining\" -eq 0 ] && [ \"$remaining6\" -eq 0 ] || exit 1; "
        "echo $count"
    )
    result = run(
        ["docker", "exec", container, "sh", "-c", command],
        check=True,
        timeout=20,
    )
    try:
        return int(result.stdout.strip())
    except ValueError as exc:
        raise RuntimeError(f"invalid SRv6 cleanup response from {container}") from exc


def clean_srv6_encap_routes():
    containers = satellite_containers()
    with ThreadPoolExecutor(max_workers=16) as executor:
        return sum(executor.map(clean_container_srv6_routes, containers))


def kill_container_iperf(container):
    command = (
        "pkill -KILL -f '[i]perf3 -c' 2>/dev/null || true; "
        "pkill -KILL -f '[i]perf3 -s' 2>/dev/null || true; "
        "pkill -KILL -f '[l]eo_te_udp_flow_probe.py' 2>/dev/null || true"
    )
    run(
        ["docker", "exec", container, "sh", "-c", command],
        timeout=15,
        check=False,
    )


def kill_all_iperf():
    containers = satellite_containers()
    with ThreadPoolExecutor(max_workers=16) as executor:
        list(executor.map(kill_container_iperf, containers))


def container_ospf_health(container):
    command = (
        "routes=$(ip route show proto ospf | wc -l); "
        "if ps -C ospfd -o stat= 2>/dev/null | "
        "grep -qv '^[[:space:]]*Z'; then live=1; else live=0; fi; "
        "printf '%s %s\\n' \"$live\" \"$routes\""
    )
    result = run(
        ["docker", "exec", container, "sh", "-c", command],
        timeout=15,
        check=False,
    )
    try:
        live, routes = (int(value) for value in result.stdout.split())
    except (ValueError, TypeError):
        return {"container": container, "live": 0, "routes": 0}
    return {"container": container, "live": live, "routes": routes}


def repair_container_ospf(container):
    command = (
        "stamp=$(date +%s); "
        "if ! ps -C ospfd -o stat= 2>/dev/null | "
        "grep -qv '^[[:space:]]*Z'; then "
        "[ -e /var/run/frr/ospfd.pid ] && "
        "mv /var/run/frr/ospfd.pid /var/run/frr/ospfd.pid.stale.$stamp; "
        "[ -S /var/run/frr/ospfd.vty ] && "
        "mv /var/run/frr/ospfd.vty /var/run/frr/ospfd.vty.stale.$stamp; "
        "/usr/lib/frr/ospfd -d -F traditional -A 127.0.0.1 || exit 1; "
        "sleep 1; "
        "vtysh -f /etc/frr/batch.txt || exit 1; "
        "fi"
    )
    run(["docker", "exec", container, "sh", "-c", command], timeout=20)


def ensure_ospf_health(min_routes=100, timeout_seconds=60):
    containers = satellite_containers()
    if not containers:
        raise RuntimeError("no running Satellite containers")

    def scan():
        with ThreadPoolExecutor(max_workers=4) as executor:
            return list(executor.map(container_ospf_health, containers))

    health = scan()
    dead = [item["container"] for item in health if not item["live"]]
    if dead:
        print(f"repairing dead ospfd on {dead}", flush=True)
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(repair_container_ospf, dead))

    deadline = time.monotonic() + timeout_seconds
    while True:
        health = scan()
        unhealthy = [
            item for item in health
            if not item["live"] or item["routes"] < min_routes
        ]
        if not unhealthy:
            print(
                f"OSPF health passed: {len(health)}/{len(containers)} "
                f"nodes, min_routes={min(item['routes'] for item in health)}",
                flush=True,
            )
            return health
        if time.monotonic() >= deadline:
            raise RuntimeError(f"OSPF health did not converge: {unhealthy}")
        print(f"waiting for OSPF: unhealthy={len(unhealthy)}", flush=True)
        time.sleep(5)


def select_reachable_destination(flow):
    result = run(
        [
            "docker",
            "exec",
            flow["dst"],
            "sh",
            "-c",
            (
                "ip -4 -o addr show up | awk '$4 ~ /^10\\./ "
                "{split($4,a,\"/\"); print a[1]}'"
            ),
        ],
        timeout=15,
        check=False,
    )
    candidates = [line.strip() for line in result.stdout.splitlines() if line]
    for candidate in candidates:
        route = run(
            [
                "docker",
                "exec",
                flow["src"],
                "ip",
                "route",
                "get",
                candidate,
            ],
            timeout=10,
            check=False,
        )
        if (
            route.returncode != 0
            or " dev eth0" in route.stdout
            or "src 10." not in route.stdout
        ):
            continue
        ping = run(
            [
                "docker",
                "exec",
                flow["src"],
                "ping",
                "-c",
                "1",
                "-W",
                "2",
                candidate,
            ],
            timeout=5,
            check=False,
        )
        if ping.returncode == 0:
            return candidate
    raise RuntimeError(
        f"no OSPF-reachable receiver IPv4 for {flow['id']} "
        f"({flow['src']} -> {flow['dst']}), candidates={candidates}"
    )


def build_flow_specs(redis_client):
    demands = json.loads(redis_client.get("te:demands") or "[]")
    specs = []
    for flow in demands:
        flow_id = flow["id"]
        try:
            index = int(flow_id.rsplit("_", 1)[-1])
        except ValueError as exc:
            raise RuntimeError(f"unexpected flow id: {flow_id}") from exc
        specs.append(
            {
                **flow,
                "port": 5201 + index,
            }
        )
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(select_reachable_destination, flow): flow
            for flow in specs
        }
        for future in as_completed(futures):
            flow = futures[future]
            flow["dst_ip"] = future.result()
            redis_client.hset("te:flow:dst_ip", flow["id"], flow["dst_ip"])
    return sorted(specs, key=lambda item: item["id"])


def validate_topology_snapshot(redis_client, demands):
    keys = sorted(redis_client.scan_iter("topo:link:*"))
    nodes = set()
    pipe = redis_client.pipeline(transaction=False)
    for key in keys:
        pipe.hgetall(key)
    links = pipe.execute()
    for link in links:
        src, dst = link.get("src"), link.get("dst")
        if src:
            nodes.add(src)
        if dst:
            nodes.add(dst)
    expected_nodes = {
        endpoint
        for demand in demands
        for endpoint in (demand["src"], demand["dst"])
    }
    missing = sorted(expected_nodes - nodes)
    running = set(
        run(["docker", "ps", "--format", "{{.Names}}"], timeout=20).stdout.splitlines()
    )
    stale_nodes = sorted(nodes - running)
    if len(keys) < 200 or missing or stale_nodes:
        raise RuntimeError(
            "incomplete topology snapshot: "
            f"links={len(keys)}, nodes={len(nodes)}, "
            f"missing_demand_endpoints={missing}, stale_nodes={stale_nodes}"
        )
    return {
        "links": len(keys),
        "nodes": len(nodes),
        "up_links": sum(link.get("status", "UP") == "UP" for link in links),
        "down_links": sum(link.get("status", "UP") != "UP" for link in links),
        "snapshot_sha256": hashlib.sha256(
            json.dumps(links, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "last_update_unix": redis_client.get("topo:last_update_unix"),
        "_node_names": sorted(nodes),
    }


def phase_paths(phase, flow_id):
    prefix = f"{CONTAINER_RESULT_DIR}/{phase}_{flow_id}"
    return {
        "server": f"{prefix}_server.json",
        "server_error": f"{prefix}_server.err",
        "client": f"{prefix}_client.json",
        "client_error": f"{prefix}_client.err",
    }


def deploy_udp_probe(containers):
    def deploy(container):
        result = run(
            [
                "docker",
                "exec",
                container,
                "mkdir",
                "-p",
                CONTAINER_RESULT_DIR,
            ],
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"failed to prepare UDP result directory in {container}: "
                f"{result.stderr}"
            )

        result = run(
            ["docker", "cp", UDP_PROBE, f"{container}:{CONTAINER_UDP_PROBE}"],
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"failed to deploy UDP probe to {container}: {result.stderr}"
            )

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(deploy, containers))


def start_phase_server(phase, flow, protocol, start_at, duration):
    paths = phase_paths(phase, flow["id"])
    if protocol == "udp-native":
        command = (
            f"python3 {CONTAINER_UDP_PROBE} server "
            f"--port {flow['port']} --start-file {start_at} "
            f"--duration {duration} --output {paths['server']} "
            f"2> {paths['server_error']}"
        )
    else:
        command = (
            f"mkdir -p {CONTAINER_RESULT_DIR}; "
            f"iperf3 -s -1 -p {flow['port']} -i 1 --json "
            f"> {paths['server']} 2> {paths['server_error']}"
        )
    result = run(
        ["docker", "exec", "-d", flow["dst"], "sh", "-c", command],
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"failed to start server for {flow['id']}: {result.stderr}"
        )


def start_phase_client(phase, flow, duration, protocol, start_at):
    paths = phase_paths(phase, flow["id"])
    bandwidth_kbps = max(1, int(float(flow["demand"])))
    if protocol == "udp-native":
        command = (
            f"python3 {CONTAINER_UDP_PROBE} client "
            f"--destination {flow['dst_ip']} --port {flow['port']} "
            f"--source-port {flow['port'] + 10000} "
            f"--rate-kbps {bandwidth_kbps} --payload-bytes 1000 "
            f"--start-file {start_at} --duration {duration} "
            f"--output {paths['client']} 2> {paths['client_error']}"
        )
    else:
        protocol_args = "-u -l 1000" if protocol == "udp" else "-M 1200"
        command = (
            f"iperf3 -c {flow['dst_ip']} -p {flow['port']} "
            f"-t {duration} -P 1 -i 1 -b {bandwidth_kbps}K "
            f"{protocol_args} --json "
            f"> {paths['client']} 2> {paths['client_error']}"
        )
    result = run(
        ["docker", "exec", "-d", flow["src"], "sh", "-c", command],
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"failed to start client for {flow['id']}: {result.stderr}"
        )


def read_container_file(container, path):
    result = run(
        ["docker", "exec", container, "sh", "-c", f"cat {path} 2>/dev/null"],
        timeout=20,
        check=False,
    )
    return result.stdout


def parse_server_intervals(flow, content, args):
    try:
        document = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid receiver JSON for {flow['id']}") from exc
    if document.get("error"):
        raise RuntimeError(
            f"receiver error for {flow['id']}: {document['error']}"
        )

    if args.protocol == "udp-native":
        samples = [
            float(value) for value in document.get("intervals_mbps", [])
        ]
    else:
        samples = []
        for interval in document.get("intervals", []):
            summary = interval.get("sum") or interval.get("sum_received")
            if not summary or "bits_per_second" not in summary:
                continue
            samples.append(float(summary["bits_per_second"]) / 1_000_000.0)
    samples = samples[args.warmup_samples:]
    if args.max_samples > 0:
        samples = samples[-args.max_samples:]
    if len(samples) < args.min_samples:
        raise RuntimeError(
            f"{flow['id']} has only {len(samples)} stable receiver intervals"
        )
    return samples


def parse_client_stats(flow, content):
    try:
        document = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid sender JSON for {flow['id']}") from exc
    duration = float(document.get("duration", 0))
    sent_packets = int(document.get("sent_packets", 0))
    payload_bytes = int(document.get("payload_bytes", 0))
    target_packets = int(document.get("target_packets", 0))
    if duration <= 0 or sent_packets <= 0 or payload_bytes <= 0:
        raise RuntimeError(f"incomplete sender statistics for {flow['id']}")
    return {
        "sent_packets": sent_packets,
        "target_packets": target_packets,
        "sent_mbps": sent_packets * payload_bytes * 8.0 / duration / 1_000_000.0,
        "sent_ratio": (
            sent_packets / target_packets if target_packets > 0 else 0.0
        ),
    }


def summarize_phase(phase, flow_samples):
    common = min(len(values) for values in flow_samples.values())
    aligned = {
        flow_id: values[-common:]
        for flow_id, values in flow_samples.items()
    }
    total_series = [
        sum(values[index] for values in aligned.values())
        for index in range(common)
    ]
    mean = statistics.fmean(total_series)
    cv = statistics.pstdev(total_series) / mean if mean > 0 else 0.0
    return {
        "phase": phase,
        "active_flows": len(aligned),
        "samples_per_flow": common,
        "total_mean_mbps": mean,
        "total_median_mbps": statistics.median(total_series),
        "total_min_mbps": min(total_series),
        "total_max_mbps": max(total_series),
        "total_cv": cv,
        "total_series_mbps": total_series,
        "flows": {
            flow_id: {
                "mean_mbps": statistics.fmean(values),
                "median_mbps": statistics.median(values),
                "min_mbps": min(values),
                "max_mbps": max(values),
                "cv": (
                    statistics.pstdev(values) / statistics.fmean(values)
                    if statistics.fmean(values) > 0
                    else 0.0
                ),
                "samples": len(values),
            }
            for flow_id, values in aligned.items()
        },
    }


def run_external_phase(redis_client, mode, cycle, args, flows):
    wait_for_host_resources(args)
    phase = f"{mode}_{cycle}_{int(time.time())}"
    if len(flows) != args.expected_flows:
        raise RuntimeError(
            f"expected {args.expected_flows} flows, found {len(flows)}"
        )

    redis_client.set("te:mode", mode)
    kill_all_iperf()
    duration = max(10, int(args.measure_seconds))
    if args.protocol == "udp-native":
        deploy_udp_probe(
            {flow["src"] for flow in flows}
            | {flow["dst"] for flow in flows}
        )
    # Docker's exec API can take an unpredictable amount of time to create 48
    # detached processes. Native UDP workers therefore wait on a shared-file
    # barrier that is released only after every worker has been launched.
    if args.protocol == "udp-native":
        start_token = f"{CONTAINER_RESULT_DIR}/{phase}.start"
    else:
        start_token = time.time() + 30.0
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(
            executor.map(
                lambda flow: start_phase_server(
                    phase,
                    flow,
                    args.protocol,
                    start_token,
                    duration,
                ),
                flows,
            )
        )
    time.sleep(2)
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(
            executor.map(
                lambda flow: start_phase_client(
                    phase,
                    flow,
                    duration,
                    args.protocol,
                    start_token,
                ),
                flows,
            )
        )
    if args.protocol == "udp-native":
        start_at = time.time() + 5.0
        release_container = min(
            {flow["src"] for flow in flows}
            | {flow["dst"] for flow in flows}
        )
        local_start_file = RESULT_DIR / f"{phase}.start"
        local_start_file.write_text(f"{start_at:.6f}")
        release = run(
            [
                "docker",
                "cp",
                str(local_start_file),
                f"{release_container}:{start_token}",
            ],
            timeout=20,
            check=False,
        )
        if release.returncode != 0:
            raise RuntimeError(
                f"failed to release UDP start barrier: {release.stderr}"
            )
    else:
        start_at = start_token
    print(
        f"[{mode}_{cycle}] 24 synchronized receiver-side sessions, "
        f"protocol={args.protocol}, duration={duration}s",
        flush=True,
    )
    if args.protocol == "udp-native":
        time.sleep(max(0.0, start_at + duration + 2.0 - time.time()))
    else:
        time.sleep(duration + 10)

    flow_samples = {}
    client_stats = {}
    errors = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(
                read_container_file,
                flow["dst"],
                phase_paths(phase, flow["id"])["server"],
            ): flow
            for flow in flows
        }
        for future in as_completed(futures):
            flow = futures[future]
            try:
                flow_samples[flow["id"]] = parse_server_intervals(
                    flow,
                    future.result(),
                    args,
                )
            except Exception as exc:
                errors.append(str(exc))
    if args.protocol == "udp-native":
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {
                executor.submit(
                    read_container_file,
                    flow["src"],
                    phase_paths(phase, flow["id"])["client"],
                ): flow
                for flow in flows
            }
            for future in as_completed(futures):
                flow = futures[future]
                try:
                    client_stats[flow["id"]] = parse_client_stats(
                        flow,
                        future.result(),
                    )
                except Exception as exc:
                    errors.append(str(exc))
    kill_all_iperf()
    if errors:
        raise RuntimeError(
            f"{len(errors)} receiver sessions failed: " + "; ".join(errors)
        )

    result = summarize_phase(phase, flow_samples)
    if client_stats:
        actual_sent_mbps = sum(
            stats["sent_mbps"] for stats in client_stats.values()
        )
        nominal_sent_mbps = (
            sum(max(1, int(float(flow["demand"]))) for flow in flows) / 1000.0
        )
        sent_load_ratio = (
            actual_sent_mbps / nominal_sent_mbps
            if nominal_sent_mbps > 0
            else 0.0
        )
        result.update(
            {
                "nominal_sent_mbps": nominal_sent_mbps,
                "actual_sent_mbps": actual_sent_mbps,
                "sent_load_ratio": sent_load_ratio,
                "sender_flows": client_stats,
            }
        )
        result["min_flow_sent_ratio"] = min(
            stats["sent_ratio"] for stats in client_stats.values()
        )
    output_path = RESULT_DIR / f"{mode}_{cycle}.json"
    output_path.write_text(json.dumps(result, indent=2, sort_keys=True))
    if client_stats and (
        result["sent_load_ratio"] < args.min_sent_load_ratio
        or result["min_flow_sent_ratio"] < args.min_sent_load_ratio
    ):
        raise RuntimeError(
            f"{phase} sender load invalid: aggregate "
            f"{result['sent_load_ratio'] * 100:.2f}%, worst flow "
            f"{result['min_flow_sent_ratio'] * 100:.2f}%; "
            f"required {args.min_sent_load_ratio * 100:.2f}%. See {output_path}"
        )
    sender_summary = (
        f"sent={result['actual_sent_mbps']:.2f}/"
        f"{result['nominal_sent_mbps']:.2f} Mbps "
        if client_stats
        else ""
    )
    print(
        f"[{mode}_{cycle}] mean={result['total_mean_mbps']:.2f} Mbps "
        f"median={result['total_median_mbps']:.2f} Mbps "
        f"CV={result['total_cv'] * 100:.2f}% "
        f"{sender_summary}"
        f"flows={result['active_flows']}",
        flush=True,
    )
    return result


def install_te_snapshot(redis_client, expected_flows):
    demands = json.loads(redis_client.get("te:demands") or "[]")
    validate_topology_snapshot(redis_client, demands)
    redis_client.delete(*POLICY_KEYS)
    sender_log = RESULT_DIR / "sender.log"
    solver_log = RESULT_DIR / "solver.log"
    solver_started = time.monotonic()
    solver = run(
        [sys.executable, SOLVER, "--once"],
        cwd=SOLVER.parent,
        timeout=120,
    )
    solver_elapsed = time.monotonic() - solver_started
    with solver_log.open("a") as output:
        output.write(solver.stdout)
        output.write(solver.stderr)

    sender_started = time.monotonic()
    with sender_log.open("a") as sender_output:
        sender = subprocess.Popen(
            [
                sys.executable,
                str(SENDER),
                "--drain-and-exit",
                "--idle-seconds",
                "2",
            ],
            cwd=SENDER.parent,
            stdout=sender_output,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            sender_returncode = sender.wait(timeout=180)
        except subprocess.TimeoutExpired as exc:
            sender.terminate()
            try:
                sender.wait(timeout=5)
            except subprocess.TimeoutExpired:
                sender.kill()
                sender.wait(timeout=5)
            raise RuntimeError(
                f"SRv6 sender timed out after 180s; see {sender_log}"
            ) from exc
        if sender_returncode != 0:
            raise RuntimeError(
                f"SRv6 sender exited with {sender_returncode}; see {sender_log}"
            )
    sender_elapsed = time.monotonic() - sender_started

    desired = redis_client.hlen("te:policy:desired")
    applied = redis_client.hlen("te:policy:applied_signature")
    queued = redis_client.llen("policy_queue")
    if desired != expected_flows or applied != expected_flows or queued != 0:
        raise RuntimeError(
            f"incomplete policy install: desired={desired}, "
            f"applied={applied}, queue={queued}, expected={expected_flows}"
        )
    policies = sorted(
        (
            json.loads(value)
            for value in redis_client.hvals("te:policy:applied")
        ),
        key=lambda policy: policy["flow_id"],
    )
    return {
        "desired": desired,
        "applied": applied,
        "queued": queued,
        "solver_elapsed_seconds": solver_elapsed,
        "sender_elapsed_seconds": sender_elapsed,
        "solver_output": solver.stdout,
        "solver_log": str(solver_log),
        "sender_log": str(sender_log),
        "items": policies,
    }


def ping_policy(policy, attempts=3):
    source = policy["src"]
    sid = policy.get("dst_sid") or policy["sids"][-1]
    destination_v4 = policy.get("dst_ipv4")
    v6_ok = False
    v4_ok = destination_v4 is None
    for _ in range(attempts):
        if not v6_ok:
            v6 = run(
                [
                    "docker",
                    "exec",
                    source,
                    "ping",
                    "-6",
                    "-c",
                    "1",
                    "-W",
                    "2",
                    sid,
                ],
                timeout=6,
                check=False,
            )
            v6_ok = v6.returncode == 0
        if destination_v4 and not v4_ok:
            v4 = run(
                [
                    "docker",
                    "exec",
                    source,
                    "ping",
                    "-c",
                    "1",
                    "-W",
                    "2",
                    destination_v4,
                ],
                timeout=6,
                check=False,
            )
            v4_ok = v4.returncode == 0
        if v6_ok and v4_ok:
            break
    return policy["flow_id"], v6_ok, v4_ok


def validate_policy_pings(redis_client, max_rounds=3):
    policies = [
        json.loads(value)
        for value in redis_client.hvals("te:policy:applied")
    ]
    state = {
        policy["flow_id"]: {"v6": False, "v4": False}
        for policy in policies
    }
    rounds = 0
    for validation_round in range(1, max_rounds + 1):
        pending = [
            policy for policy in policies
            if not state[policy["flow_id"]]["v6"]
            or not state[policy["flow_id"]]["v4"]
        ]
        if not pending:
            break
        if validation_round > 1:
            print(
                f"retrying SRv6 ping validation: pending={len(pending)}",
                flush=True,
            )
            time.sleep(3)
        rounds = validation_round
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [
                executor.submit(ping_policy, policy, 2)
                for policy in pending
            ]
            for future in as_completed(futures):
                flow_id, current_v6, current_v4 = future.result()
                state[flow_id]["v6"] |= current_v6
                state[flow_id]["v4"] |= current_v4
    v6_failed = sorted(
        flow_id for flow_id, value in state.items() if not value["v6"]
    )
    v4_failed = sorted(
        flow_id for flow_id, value in state.items() if not value["v4"]
    )
    return {
        "policies": len(policies),
        "validation_rounds": rounds,
        "srv6_ipv6_ping_ok": len(policies) - len(v6_failed),
        "srv6_ipv4_ping_ok": len(policies) - len(v4_failed),
        "srv6_ipv6_ping_failed": v6_failed,
        "srv6_ipv4_ping_failed": v4_failed,
    }


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycles", type=int, default=2)
    parser.add_argument("--expected-flows", type=int, default=24)
    parser.add_argument("--measure-seconds", type=float, default=24)
    parser.add_argument("--warmup-samples", type=int, default=2)
    parser.add_argument("--max-samples", type=int, default=8)
    parser.add_argument("--min-samples", type=int, default=5)
    parser.add_argument(
        "--protocol",
        choices=("udp-native", "udp", "tcp"),
        default="udp-native",
        help="measurement transport; udp-native avoids iperf3 TCP-control loss",
    )
    parser.add_argument("--target-low-percent", type=float, default=17)
    parser.add_argument("--target-high-percent", type=float, default=23)
    parser.add_argument(
        "--max-improvement-stddev-percent",
        type=float,
        default=3.0,
        help="maximum allowed population standard deviation across cycles",
    )
    parser.add_argument(
        "--min-sent-load-ratio",
        type=float,
        default=0.98,
        help="minimum actual/nominal UDP sender load for a valid phase",
    )
    parser.add_argument("--max-phase-cv-percent", type=float, default=5.0)
    parser.add_argument("--max-paired-sent-difference-percent", type=float, default=1.0)
    parser.add_argument("--min-host-available-mib", type=float, default=512.0)
    parser.add_argument("--max-host-memory-stall-percent", type=float, default=5.0)
    parser.add_argument(
        "--resource-wait-seconds",
        type=float,
        default=90.0,
        help=(
            "maximum time to wait for transient memory pressure to settle "
            "at resource gates"
        ),
    )
    parser.add_argument(
        "--demand-total-kbps",
        type=float,
        default=0.0,
        help=(
            "idempotently normalize all Redis demands to this aggregate "
            "offered load; zero keeps the published rates"
        ),
    )
    parser.add_argument(
        "--result",
        type=Path,
        default=RESULT_DIR / "result.json",
    )
    parser.add_argument(
        "--keep-existing-controller",
        action="store_true",
        help="refuse to stop existing solver/sender (baseline will usually be invalid)",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help=(
            "return success after a complete valid run even when improvement "
            "is outside the target range; target_met remains unchanged"
        ),
    )
    args = parser.parse_args()
    if args.cycles < 1 or args.expected_flows < 1:
        parser.error("cycles and expected-flows must be positive")
    if args.measure_seconds < 10 or args.warmup_samples < 0 or args.min_samples < 1:
        parser.error("invalid measurement duration or sample counts")
    available_samples = int(args.measure_seconds) - args.warmup_samples
    if args.max_samples > 0:
        available_samples = min(available_samples, args.max_samples)
    if available_samples < args.min_samples:
        parser.error("measurement window cannot provide min-samples")
    if not 0 < args.min_sent_load_ratio <= 1:
        parser.error("min-sent-load-ratio must be in (0, 1]")
    if args.target_low_percent > args.target_high_percent:
        parser.error("target-low-percent must not exceed target-high-percent")
    if args.resource_wait_seconds < 0:
        parser.error("resource-wait-seconds must not be negative")
    return args


def write_checkpoint(args, experiment, cycles, complete=False):
    improvements = [cycle["improvement_percent"] for cycle in cycles]
    phase_stable = bool(cycles) and all(
        cycle[phase]["total_cv"] * 100 <= args.max_phase_cv_percent
        for cycle in cycles
        for phase in ("baseline", "te")
    )
    paired_load_valid = bool(cycles) and all(
        abs(cycle["te"]["actual_sent_mbps"] / cycle["baseline"]["actual_sent_mbps"] - 1)
        * 100
        <= args.max_paired_sent_difference_percent
        for cycle in cycles
        if args.protocol == "udp-native"
    )
    result = {
        "experiment": experiment,
        "cycles": cycles,
        "status": "completed" if complete else "in_progress",
        "completed_cycles": len(cycles),
        "mean_improvement_percent": (
            statistics.fmean(improvements) if improvements else None
        ),
        "improvement_stddev_percent": (
            statistics.pstdev(improvements) if len(improvements) > 1 else 0.0
        ),
        "target_low_percent": args.target_low_percent,
        "target_high_percent": args.target_high_percent,
        "max_improvement_stddev_percent": args.max_improvement_stddev_percent,
        "max_phase_cv_percent": args.max_phase_cv_percent,
        "max_paired_sent_difference_percent": args.max_paired_sent_difference_percent,
        "all_cycles_in_target": bool(cycles) and all(
            args.target_low_percent <= improvement <= args.target_high_percent
            for improvement in improvements
        ),
        "phase_stable": phase_stable,
        "paired_load_valid": paired_load_valid,
    }
    result["improvement_stable"] = (
        result["improvement_stddev_percent"]
        <= args.max_improvement_stddev_percent
    )
    result["target_met"] = (
        complete
        and len(cycles) == args.cycles
        and result["all_cycles_in_target"]
        and result["improvement_stable"]
        and phase_stable
        and paired_load_valid
    )
    args.result.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.result.with_name(args.result.name + ".tmp")
    temporary.write_text(json.dumps(result, indent=2, sort_keys=True))
    temporary.replace(args.result)
    return result


def main(args=None):
    global CLEANUP_REQUIRED
    args = parse_args() if args is None else args
    host_resources = wait_for_host_resources(args)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    redis_client = redis.Redis(
        host="127.0.0.1",
        port=6379,
        decode_responses=True,
        socket_timeout=5,
    )
    redis_client.ping()
    CLEANUP_REQUIRED = True
    redis_client.set("te:traffic:external_control", "1")
    standard_state = freeze_standard()
    time.sleep(6)
    kill_all_iperf()
    ospf_health = ensure_ospf_health()

    if not args.keep_existing_controller:
        stopped = stop_existing_controllers()
        if stopped:
            print(f"stopped existing controller PIDs: {stopped}", flush=True)

    demands = json.loads(redis_client.get("te:demands") or "[]")
    if len(demands) != args.expected_flows:
        raise RuntimeError(
            f"expected {args.expected_flows} demands, found {len(demands)}"
        )
    input_offered_load_kbps = sum(
        max(0.0, float(demand["demand"])) for demand in demands
    )
    if args.demand_total_kbps > 0:
        if input_offered_load_kbps <= 0:
            raise RuntimeError("cannot normalize an empty offered load")
        factor = args.demand_total_kbps / input_offered_load_kbps
        for demand in demands:
            demand["demand"] = round(
                max(0.0, float(demand["demand"])) * factor,
                6,
            )
        redis_client.set(
            "te:demands",
            json.dumps(demands, separators=(",", ":")),
        )
    offered_load_kbps = sum(
        max(0.0, float(demand["demand"])) for demand in demands
    )
    print(
        f"offered load={offered_load_kbps:.3f} Kbps "
        f"flows={len(demands)}",
        flush=True,
    )
    snapshot = run(
        [sys.executable, SNIFFER, "--once"],
        cwd=SNIFFER.parent,
        timeout=180,
    )
    print(snapshot.stdout, end="", flush=True)
    topology_state = validate_topology_snapshot(redis_client, demands)
    topology_nodes = set(topology_state.pop("_node_names"))
    telemetry_node_names = {
        key.removeprefix("telemetry:queue:")
        for key in redis_client.scan_iter("telemetry:queue:*")
    }
    missing_telemetry = sorted(topology_nodes - telemetry_node_names)
    telemetry_nodes = len(topology_nodes & telemetry_node_names)
    if missing_telemetry:
        raise RuntimeError(
            f"missing controller input: links={topology_state['links']}, "
            f"telemetry_nodes={telemetry_nodes}, "
            f"missing_telemetry={missing_telemetry}"
        )
    telemetry_updated = float(
        redis_client.get("telemetry:queue:last_update_unix") or "0"
    )
    if time.time() - telemetry_updated > 10:
        raise RuntimeError("queue telemetry is stale; start queue_monitor.py first")

    cycles = []
    experiment = {
        "protocol": args.protocol,
        "cycles": args.cycles,
        "expected_flows": args.expected_flows,
        "measure_seconds": args.measure_seconds,
        "warmup_samples": args.warmup_samples,
        "max_samples": args.max_samples,
        "min_samples": args.min_samples,
        "input_offered_load_kbps": input_offered_load_kbps,
        "offered_load_kbps": offered_load_kbps,
        "demand_rates_kbps": sorted({float(demand["demand"]) for demand in demands}),
        "topology": topology_state,
        "telemetry_nodes": telemetry_nodes,
        "started_unix": time.time(),
        "standard": standard_state,
        "scenario": redis_client.hgetall("te:experiment:scenario"),
        "host_resources_at_start": host_resources,
        "ospf": {
            "nodes": len(ospf_health),
            "min_routes": min(item["routes"] for item in ospf_health),
            "max_routes": max(item["routes"] for item in ospf_health),
        },
        "report_only": args.report_only,
    }
    write_checkpoint(args, experiment, cycles)
    for cycle in range(1, args.cycles + 1):
        deleted = clean_srv6_encap_routes()
        redis_client.delete(*POLICY_KEYS)
        print(f"[baseline_{cycle}] deleted {deleted} SRv6 encap routes", flush=True)
        flows = build_flow_specs(redis_client)
        baseline = run_external_phase(
            redis_client,
            "baseline",
            cycle,
            args,
            flows,
        )
        experiment["current_cycle"] = {
            "cycle": cycle,
            "status": "baseline_complete",
            "baseline": baseline,
        }
        write_checkpoint(args, experiment, cycles)

        wait_for_host_resources(args)
        policy_state = install_te_snapshot(redis_client, args.expected_flows)
        ping_state = validate_policy_pings(redis_client)
        experiment["current_cycle"].update(
            {
                "status": "policy_and_ping_complete",
                "policy": policy_state,
                "ping": ping_state,
            }
        )
        write_checkpoint(args, experiment, cycles)
        if (
            ping_state["srv6_ipv6_ping_ok"] != args.expected_flows
            or ping_state["srv6_ipv4_ping_ok"] != args.expected_flows
        ):
            raise RuntimeError(f"SRv6 ping validation failed: {ping_state}")
        te = run_external_phase(redis_client, "te", cycle, args, flows)
        improvement = (
            (te["total_mean_mbps"] - baseline["total_mean_mbps"])
            / baseline["total_mean_mbps"]
            * 100.0
        )
        cycles.append(
            {
                "cycle": cycle,
                "baseline": baseline,
                "te": te,
                "improvement_percent": improvement,
                "policy": policy_state,
                "ping": ping_state,
            }
        )
        experiment.pop("current_cycle", None)
        write_checkpoint(args, experiment, cycles)
        print(
            f"[cycle {cycle}] baseline={baseline['total_mean_mbps']:.2f} "
            f"TE={te['total_mean_mbps']:.2f} Mbps "
            f"improvement={improvement:.2f}%",
            flush=True,
        )

    experiment["elapsed_seconds"] = time.time() - experiment["started_unix"]
    result = write_checkpoint(args, experiment, cycles, complete=True)
    print(
        f"mean improvement={result['mean_improvement_percent']:.2f}% "
        f"stddev={result['improvement_stddev_percent']:.2f}pp "
        f"target_met={result['target_met']}",
        flush=True,
    )
    print(f"result: {args.result}", flush=True)
    return 0 if result["target_met"] or args.report_only else 4


if __name__ == "__main__":
    args = parse_args()
    try:
        exit_code = main(args)
    except KeyboardInterrupt:
        print("experiment interrupted; completed-cycle checkpoints are preserved", file=sys.stderr)
        exit_code = 130
    except (RuntimeError, subprocess.TimeoutExpired, redis.RedisError) as exc:
        print(f"experiment failed: {exc}", file=sys.stderr)
        exit_code = 1
    finally:
        if CLEANUP_REQUIRED:
            try:
                kill_all_iperf()
            except Exception as exc:
                print(f"traffic cleanup failed: {exc}", file=sys.stderr)
            finally:
                resume_standard()
    sys.exit(exit_code)

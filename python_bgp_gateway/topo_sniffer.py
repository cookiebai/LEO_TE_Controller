#!/usr/bin/env python3
import argparse
import ipaddress
import os
import re
import subprocess
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import redis


REDIS_HOST = os.getenv("TE_REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("TE_REDIS_PORT", "6379"))
POLL_INTERVAL_SECONDS = float(os.getenv("TOPO_SNIFF_INTERVAL_SECONDS", "30"))
DEFAULT_DELAY_MS = float(os.getenv("TE_DEFAULT_DELAY_MS", "10"))
DEFAULT_CAPACITY_MBPS = float(os.getenv("TE_DEFAULT_CAPACITY_MBPS", "16"))

r = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    decode_responses=True,
    socket_timeout=3.0,
)


def run_command(args, timeout=10):
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def get_containers():
    output = run_command(
        ["docker", "ps", "--format", "{{.Names}}"],
        timeout=15,
    )
    if output.returncode != 0:
        raise RuntimeError(output.stderr.strip() or "docker ps failed")
    return [
        name.strip()
        for name in output.stdout.splitlines()
        if name.strip()
        and ("Satellite_" in name or "GroundStation_" in name)
    ]


def get_host_interface_map():
    """Return host ifindex -> interface name."""
    result = run_command(["ip", "-o", "link", "show"], timeout=15)
    mapping = {}
    if result.returncode != 0:
        return mapping

    for line in result.stdout.splitlines():
        match = re.match(r"^\s*(\d+):\s+([^:@]+)(?:@[^:]+)?:", line)
        if match:
            mapping[int(match.group(1))] = match.group(2)
    return mapping


def get_bridge_port_status():
    """Return host bridge-port forwarding state keyed by interface name."""
    result = run_command(["bridge", "link", "show"], timeout=15)
    if result.returncode != 0:
        return {}
    status = {}
    for line in result.stdout.splitlines():
        iface_match = re.match(r"^\s*\d+:\s+([^:@]+)(?:@[^:]+)?:", line)
        state_match = re.search(r"\bstate\s+(\S+)", line)
        if iface_match and state_match:
            status[iface_match.group(1)] = (
                state_match.group(1).lower() == "forwarding"
            )
    return status


def parse_rate_mbps(value, unit):
    multipliers = {
        "": 1e-6,
        "K": 1e-3,
        "M": 1.0,
        "G": 1000.0,
        "T": 1_000_000.0,
    }
    return float(value) * multipliers.get(unit.upper(), 1e-6)


def parse_delay_ms(value, unit):
    multipliers = {
        "us": 0.001,
        "ms": 1.0,
        "s": 1000.0,
    }
    return float(value) * multipliers.get(unit.lower(), 1.0)


def get_tc_link_parameters():
    """Read all host qdiscs once and return per-interface delay/capacity."""
    result = run_command(["tc", "qdisc", "show"], timeout=20)
    parameters = defaultdict(dict)
    if result.returncode != 0:
        return parameters

    for line in result.stdout.splitlines():
        dev_match = re.search(r"\bdev\s+(\S+)", line)
        if not dev_match:
            continue
        iface = dev_match.group(1)

        delay_match = re.search(r"\bdelay\s+([\d.]+)(us|ms|s)\b", line)
        if delay_match:
            parameters[iface]["delay"] = parse_delay_ms(
                delay_match.group(1),
                delay_match.group(2),
            )

        rate_match = re.search(
            r"\brate\s+([\d.]+)([KMGT]?)(?:bit|bps)\b",
            line,
            flags=re.IGNORECASE,
        )
        if rate_match:
            parameters[iface]["capacity"] = parse_rate_mbps(
                rate_match.group(1),
                rate_match.group(2),
            )

    return parameters


def get_container_interfaces(
    container,
    host_interface_map,
    bridge_port_status,
):
    result = run_command(
        [
            "docker",
            "exec",
            container,
            "sh",
            "-c",
            (
                "ip -4 -o addr show up | "
                "while read -r index iface family ip_prefix rest; do "
                "iface=${iface%%@*}; "
                "[ \"$iface\" = lo ] && continue; "
                "read -r iflink < /sys/class/net/$iface/iflink || continue; "
                "printf '%s\\t%s\\t%s\\n' \"$iface\" \"$ip_prefix\" \"$iflink\"; "
                "done"
            ),
        ],
        timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip()
            or f"docker exec returned {result.returncode}"
        )

    interfaces = []
    seen = set()
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        iface, ip_prefix, iflink_raw = parts
        if iface == "lo" or ip_prefix.startswith(("127.", "172.")):
            continue
        if (iface, ip_prefix) in seen:
            continue
        seen.add((iface, ip_prefix))

        host_iface = ""
        try:
            host_iface = host_interface_map.get(int(iflink_raw), "")
        except ValueError:
            pass

        interfaces.append(
            {
                "node": container,
                "iface": iface,
                "host_iface": host_iface,
                "ip_prefix": ip_prefix,
                "forwarding": bridge_port_status.get(host_iface, True),
            }
        )
    return interfaces


def sniff_topology():
    containers = get_containers()
    host_interface_map = get_host_interface_map()
    bridge_port_status = get_bridge_port_status()
    tc_parameters = get_tc_link_parameters()
    subnets = defaultdict(list)
    scan_failures = []

    workers = max(1, int(os.getenv("TOPO_SNIFF_WORKERS", "4")))
    with ThreadPoolExecutor(max_workers=min(workers, max(1, len(containers)))) as executor:
        futures = {
            executor.submit(
                get_container_interfaces,
                container,
                host_interface_map,
                bridge_port_status,
            ): container
            for container in containers
        }
        for future in as_completed(futures):
            try:
                endpoints = future.result()
            except Exception as exc:
                scan_failures.append(futures[future])
                print(
                    f"[topology] {futures[future]} scan failed: {exc}",
                    flush=True,
                )
                continue
            if futures[future].startswith("Satellite_") and not endpoints:
                scan_failures.append(futures[future])
                print(
                    f"[topology] {futures[future]} returned no data interfaces",
                    flush=True,
                )
                continue
            for endpoint in endpoints:
                network = ipaddress.ip_network(
                    endpoint["ip_prefix"],
                    strict=False,
                )
                subnets[str(network)].append(endpoint)

    links = []
    for endpoints in subnets.values():
        if len(endpoints) != 2:
            continue
        left, right = endpoints
        for src, dst in ((left, right), (right, left)):
            shaping = tc_parameters.get(src["host_iface"], {})
            links.append(
                {
                    "src": src["node"],
                    "dst": dst["node"],
                    "delay": shaping.get("delay", DEFAULT_DELAY_MS),
                    "capacity": shaping.get(
                        "capacity",
                        DEFAULT_CAPACITY_MBPS,
                    ),
                    "status": "UP",
                    "local_iface": src["iface"],
                    "host_iface": src["host_iface"],
                }
            )
            if not src["forwarding"] or not dst["forwarding"]:
                links[-1]["status"] = "DOWN"

    old_keys = list(r.scan_iter("topo:link:*"))
    old_count = len(old_keys)
    demand_endpoints = set()
    try:
        import json

        for demand in json.loads(r.get("te:demands") or "[]"):
            demand_endpoints.update((demand["src"], demand["dst"]))
    except (TypeError, ValueError, KeyError):
        pass
    link_nodes = {
        endpoint
        for link in links
        for endpoint in (link["src"], link["dst"])
    }
    missing_endpoints = sorted(demand_endpoints - link_nodes)
    too_small = old_count > 0 and len(links) < int(old_count * 0.9)
    if scan_failures or too_small or missing_endpoints:
        print(
            "[topology] rejected incomplete snapshot: "
            f"links={len(links)} previous={old_count} "
            f"scan_failures={scan_failures} "
            f"missing_demand_endpoints={missing_endpoints}",
            flush=True,
        )
        return False

    pipe = r.pipeline(transaction=True)
    if old_keys:
        pipe.delete(*old_keys)
    for link in links:
        key = f"topo:link:{link['src']}_{link['dst']}"
        pipe.hset(key, mapping=link)
    pipe.set("topo:last_update_unix", f"{time.time():.6f}")
    pipe.execute()

    capacities = [link["capacity"] for link in links]
    delays = [link["delay"] for link in links]
    up_count = sum(link["status"] == "UP" for link in links)
    capacity_text = (
        f"{min(capacities):.2f}-{max(capacities):.2f} Mbps"
        if capacities
        else "n/a"
    )
    delay_text = (
        f"{min(delays):.2f}-{max(delays):.2f} ms"
        if delays
        else "n/a"
    )
    print(
        f"[topology] containers={len(containers)} directed_links={len(links)} "
        f"up={up_count} down={len(links) - up_count} "
        f"capacity={capacity_text} delay={delay_text}",
        flush=True,
    )
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--once",
        action="store_true",
        help="publish one validated topology snapshot and exit",
    )
    args = parser.parse_args()
    print("====== OpenSN topology sniffer started ======", flush=True)
    if args.once:
        return 0 if sniff_topology() else 2
    while True:
        started = time.monotonic()
        try:
            sniff_topology()
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(f"[topology] refresh failed: {exc}", flush=True)

        elapsed = time.monotonic() - started
        time.sleep(max(0.2, POLL_INTERVAL_SECONDS - elapsed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

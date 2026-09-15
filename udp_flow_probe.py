#!/usr/bin/env python3
"""Small dependency-free UDP offered-load probe for OpenSN containers."""

import argparse
import json
import socket
import time
from pathlib import Path


def wait_until(timestamp):
    while True:
        remaining = timestamp - time.time()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.02))


def wait_for_start(args):
    if args.start_file:
        start_file = Path(args.start_file)
        while True:
            try:
                return float(start_file.read_text().strip())
            except (FileNotFoundError, ValueError):
                time.sleep(0.02)
    if args.start_at is None:
        raise ValueError("either --start-at or --start-file is required")
    return args.start_at


def run_server(args):
    start_at = wait_for_start(args)
    wait_until(start_at)
    samples = [0] * args.duration
    packets = [0] * args.duration
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    sock.bind(("0.0.0.0", args.port))
    sock.settimeout(0.2)
    end_at = start_at + args.duration
    while time.time() < end_at:
        try:
            payload, _ = sock.recvfrom(65535)
        except socket.timeout:
            continue
        index = int(time.time() - start_at)
        if 0 <= index < args.duration:
            samples[index] += len(payload)
            packets[index] += 1
    sock.close()

    result = {
        "start_at": start_at,
        "duration": args.duration,
        "intervals_mbps": [value * 8.0 / 1_000_000.0 for value in samples],
        "packets": packets,
        "total_bytes": sum(samples),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, separators=(",", ":")))


def run_client(args):
    start_at = wait_for_start(args)
    payload = bytes(args.payload_bytes)
    packets_per_second = args.rate_kbps * 1000.0 / (8.0 * len(payload))
    total_packets = round(packets_per_second * args.duration)
    # Ten-millisecond bursts stay well below OpenSN's 32-KiB TBF burst and
    # substantially reduce scheduler wakeups across the 24 source containers.
    batch = max(1, min(16, round(packets_per_second / 100.0)))
    spacing = batch / packets_per_second
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)
    sock.bind(("0.0.0.0", args.source_port))
    wait_until(start_at)
    end_at = start_at + args.duration
    next_send = start_at
    sent = 0
    while sent < total_packets:
        wait_until(next_send)
        now = time.time()
        if now >= end_at:
            break
        current_batch = min(batch, total_packets - sent)
        for _ in range(current_batch):
            sock.sendto(payload, (args.destination, args.port))
            sent += 1
        next_send += spacing
        # Keep the original pacing epoch after a deschedule. Resetting it to
        # "now" silently lowers the offered load and makes A/B phases
        # incomparable. Small bounded batches let the socket/qdisc catch up.
    sock.close()
    if args.output:
        Path(args.output).write_text(
            json.dumps(
                {
                    "start_at": start_at,
                    "duration": args.duration,
                    "sent_packets": sent,
                    "target_packets": total_packets,
                    "payload_bytes": len(payload),
                    "sent_ratio": sent / total_packets if total_packets else 1.0,
                },
                separators=(",", ":"),
            )
        )


def parse_args():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="role", required=True)

    server = subparsers.add_parser("server")
    server.add_argument("--port", type=int, required=True)
    server.add_argument("--start-at", type=float)
    server.add_argument("--start-file")
    server.add_argument("--duration", type=int, required=True)
    server.add_argument("--output", required=True)

    client = subparsers.add_parser("client")
    client.add_argument("--destination", required=True)
    client.add_argument("--port", type=int, required=True)
    client.add_argument("--source-port", type=int, required=True)
    client.add_argument("--rate-kbps", type=float, required=True)
    client.add_argument("--payload-bytes", type=int, default=1000)
    client.add_argument("--start-at", type=float)
    client.add_argument("--start-file")
    client.add_argument("--duration", type=int, required=True)
    client.add_argument("--output")
    release = subparsers.add_parser("release")
    release.add_argument("--start-file", required=True)
    release.add_argument("--start-at", type=float, required=True)
    return parser.parse_args()


if __name__ == "__main__":
    parsed = parse_args()
    if parsed.role == "release":
        output = Path(parsed.start_file)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(f"{parsed.start_at:.6f}")
    elif parsed.role == "server":
        run_server(parsed)
    else:
        run_client(parsed)

"""Measure baseline/TE on the user's EXISTING topology; no topology/qdisc writes."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time
import uuid
import yaml

from integration.controller import (atomic_json, command, discover_nodes, docker_read,
                                    validate_demands)
from integration.lsdb import coordinates
from integration.manage import snapshot


def destination(node):
    row, col = coordinates(node)
    return f'fc00:0:{row:x}::{col:x}'


def state_fingerprint(graph, queues):
    metrics = sorted((r['src'], r['dst'], r['capacity_mbps'], r['delay_ms']) for r in queues)
    identities = sorted((node, data['end_sid'], data['loopback']) for node, data in graph.nodes(data=True))
    raw = json.dumps([identities, sorted(graph.edges), metrics], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def summarize_phase(clients, servers, warmup, duration, demands):
    if set(clients) != {f['id'] for f in demands} or set(servers) != set(clients):
        raise ValueError('Missing sender or receiver evidence for some business flows')
    totals = [0.0] * duration
    offered = [0.0] * duration
    flows = {}
    for flow in demands:
        client, server = clients[flow['id']], servers[flow['id']]
        for result in (client, server):
            if len(result['bytes_per_second']) < warmup + duration:
                raise ValueError('Probe output does not cover the full measurement window')
        sent = [v * 8 / 1e6 for v in client['bytes_per_second'][warmup:warmup + duration]]
        received = [v * 8 / 1e6 for v in server['bytes_per_second'][warmup:warmup + duration]]
        if client['send_errors']:
            raise ValueError(f'{flow["id"]}: sender reported {client["send_errors"]} send errors')
        for i in range(duration):
            totals[i] += received[i]
            offered[i] += sent[i]
        flows[flow['id']] = {'actual_sent_mbps': statistics.mean(sent),
                             'received_mbps': statistics.mean(received),
                             'configured_mbps': float(flow['demand']) / 1000}
    mean = statistics.mean(totals)
    cv_percent = statistics.pstdev(totals) / mean * 100 if mean else None
    configured = sum(float(f['demand']) for f in demands) / 1000
    actual = statistics.mean(offered)
    max_flow_error = max(abs(f['actual_sent_mbps'] - f['configured_mbps']) /
                         f['configured_mbps'] * 100 for f in flows.values())
    return {'received_mbps': mean, 'actual_sent_mbps': actual,
            'configured_sent_mbps': configured,
            'offered_error_percent': abs(actual - configured) / configured * 100,
            'max_flow_offered_error_percent': max_flow_error,
            'cv_percent': cv_percent, 'intervals_mbps': totals,
            'offered_intervals_mbps': offered, 'flows': flows}


def comparison(baseline, te, max_cv=5, max_pair_difference=1, max_offered_error=2):
    paired = abs(baseline['actual_sent_mbps'] - te['actual_sent_mbps']) / max(
        baseline['actual_sent_mbps'], te['actual_sent_mbps'], 1e-9) * 100
    valid = (baseline['received_mbps'] > 0 and
             baseline['cv_percent'] is not None and te['cv_percent'] is not None and
             baseline['cv_percent'] <= max_cv and te['cv_percent'] <= max_cv and
             paired <= max_pair_difference and
             max(baseline['offered_error_percent'], te['offered_error_percent']) <= max_offered_error and
             max(baseline.get('max_flow_offered_error_percent', 0),
                 te.get('max_flow_offered_error_percent', 0)) <= max_offered_error)
    improvement = ((te['received_mbps'] / baseline['received_mbps'] - 1) * 100
                   if baseline['received_mbps'] > 0 else None)
    return {'valid': valid, 'baseline': baseline, 'te': te,
            'paired_offered_difference_percent': paired, 'improvement_percent': improvement}


def owned_filename(flow):
    token = hashlib.sha256(('leo_te_' + flow['id']).encode()).hexdigest()[:24]
    return f'/path.cmd/leo-te-{token}.json'


def withdraw(demands, nodes, timeout=60):
    # Only delete this exact demand set's controller-generated commands.
    for flow in demands:
        container = nodes[flow['src']]['container']
        command(['docker', 'exec', container, 'rm', '-f', '--', owned_filename(flow)])
    deadline = time.monotonic() + timeout
    while True:
        remaining = []
        for flow in demands:
            route = command(['docker', 'exec', nodes[flow['src']]['container'], 'ip', '-6',
                             'route', 'get', destination(flow['dst'])])
            if 'encap seg6' in route:
                remaining.append(flow['id'])
        if not remaining:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(f'Baseline still has SRv6 policy routes: {remaining}. '
                               'Stop other controllers and inspect foreign /path.cmd commands')
        time.sleep(2)


def assert_routes(demands, nodes, te):
    routes = {}
    for flow in demands:
        route = command(['docker', 'exec', nodes[flow['src']]['container'], 'ip', '-6',
                         'route', 'get', destination(flow['dst'])])
        if ('encap seg6' in route) != te:
            raise RuntimeError(f'{flow["id"]}: route mode changed during throughput measurement')
        routes[flow['id']] = route
    return routes


def phase(args, demands, nodes, output, mode):
    output.mkdir(parents=True, exist_ok=True)
    routes = assert_routes(demands, nodes, mode == 'te')
    atomic_json(output / 'routes-before.json', routes)
    remote = '/tmp/leo-te-throughput-' + uuid.uuid4().hex
    involved = {nodes[f[k]]['container'] for f in demands for k in ('src', 'dst')}
    for container in sorted(involved):
        command(['docker', 'exec', container, 'mkdir', '-p', remote])
        command(['docker', 'cp', '/app/bin/udp6-probe', container + ':' + remote + '/probe'])
    total_duration = args.warmup + args.duration
    # All processes bind before a shared epoch. Start delay also covers N execs.
    start_at = time.time() + max(5, len(demands) * .15 + 3)
    processes = []
    records = []
    try:
        for index, flow in enumerate(demands):
            port = args.port_base + index
            for role, node in [('server', flow['dst']), ('client', flow['src'])]:
                filename = remote + '/' + flow['id'] + '-' + role + '.json'
                cmd = ['docker', 'exec', nodes[node]['container'], remote + '/probe', role,
                       '--port', str(port), '--duration', str(total_duration),
                       '--start-at', str(start_at), '--output', filename]
                if role == 'client':
                    cmd += ['--dst', destination(flow['dst']), '--source-port', str(args.port_base + 1000 + index),
                            '--rate-kbps', str(flow['demand']), '--payload', str(args.payload)]
                log = (output / (flow['id'] + '-' + role + '.log')).open('w')
                process = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
                processes.append((process, log))
                records.append((flow, role, nodes[node]['container'], filename))
        if time.time() >= start_at:
            raise RuntimeError('Probe startup missed the common epoch; measurement refused')
        print(f'{mode}: {len(demands)} flows, warmup={args.warmup}s, measurement={args.duration}s', flush=True)
        # Capture native samples while offered traffic is active. This is proof
        # of queue maintenance, not a fabricated queue derived from throughput.
        until_sample = start_at + args.warmup + max(1, args.duration / 2)
        while time.time() < until_sample:
            time.sleep(min(1, until_sample - time.time()))
        _, graph, warnings, queues = snapshot(nodes, args.queue_max_age)
        atomic_json(output / 'queues-during-load.json', {'read_at': time.time(), 'links': queues})
        for process, log in processes:
            process.wait(timeout=max(5, start_at + total_duration + 8 - time.time()))
            log.close()
            if process.returncode:
                raise RuntimeError('A traffic probe failed; see raw flow logs')
        clients, servers = {}, {}
        for flow, role, container, filename in records:
            result = docker_read(container, filename)
            atomic_json(output / (flow['id'] + '-' + role + '.json'), result)
            (clients if role == 'client' else servers)[flow['id']] = result
        atomic_json(output / 'routes-after.json', assert_routes(demands, nodes, mode == 'te'))
        return summarize_phase(clients, servers, args.warmup, args.duration, demands), state_fingerprint(graph, queues)
    finally:
        for container in involved:
            # Kill only probe processes whose argv contain this unique run path.
            subprocess.run(['docker', 'exec', container, 'pkill', '-f', remote + '/probe'],
                           capture_output=True)
            subprocess.run(['docker', 'exec', container, 'rm', '-rf', '--', remote], capture_output=True)
        for process, log in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            log.close()


def write_report(output, result):
    atomic_json(output / 'result.json', result)
    lines = ['# 原拓扑 IPv6 UDP 吞吐量对比', '',
             f'状态：{result["status"]}；所有数据来自接收端实际 payload 计数。', '',
             '| 周期 | Baseline Mbps | TE Mbps | 变化 % | 样本有效 |',
             '|---|---:|---:|---:|---|']
    for i, item in enumerate(result['cycles'], 1):
        improvement = item['improvement_percent']
        value = f'{improvement:.2f}' if improvement is not None else 'N/A'
        lines.append(f'| {i} | {item["baseline"]["received_mbps"]:.3f} | '
                     f'{item["te"]["received_mbps"]:.3f} | {value} | {item["valid"]} |')
    if result.get('mean_improvement_percent') is not None:
        lines += ['', f'平均变化：{result["mean_improvement_percent"]:.2f}%；'
                  '不预设提升目标，零提升或负提升原样记录。']
    if result.get('error'):
        lines += ['', '错误：' + result['error']]
    lines += ['', '测量保持拓扑、链路容量/时延、需求矩阵及发送负载一致；'
              '未修改 qdisc、未创建拓扑。原始发送/接收、队列采样和路由回读保存在各周期目录。']
    (output / 'README.md').write_text('\n'.join(lines) + '\n')


def main():
    parser = argparse.ArgumentParser(description='在课题1原拓扑上实测 Baseline/TE 吞吐量，不创建拓扑')
    parser.add_argument('--demands', required=True)
    parser.add_argument('--containers', nargs='+')
    parser.add_argument('--output', default='/output/throughput')
    parser.add_argument('--cycles', type=int, default=3)
    parser.add_argument('--duration', type=int, default=30)
    parser.add_argument('--warmup', type=int, default=10)
    parser.add_argument('--payload', type=int, default=1000)
    parser.add_argument('--port-base', type=int, default=31000)
    parser.add_argument('--queue-max-age', type=float)
    parser.add_argument('--max-cv-percent', type=float, default=5)
    parser.add_argument('--max-paired-offered-difference-percent', type=float, default=1)
    parser.add_argument('--max-offered-error-percent', type=float, default=2)
    parser.add_argument('--ack-timeout', type=float, default=90)
    parser.add_argument('--keep-policy', action='store_true')
    parser.add_argument('--withdraw-only', action='store_true')
    args = parser.parse_args()
    if args.cycles < 1 or args.duration < 5 or args.warmup < 0 or not 1 <= args.payload <= 1000:
        parser.error('cycles>=1, duration>=5, warmup>=0, payload in 1..1000 required')
    nodes = discover_nodes(args.containers)
    config = yaml.safe_load((Path(__file__).resolve().parents[1] / 'config/controller.yaml').read_text())
    demands = json.loads(Path(args.demands).read_text())
    if args.withdraw_only:
        # Rollback must remain possible even when queue telemetry has stopped.
        validate_demands(demands, nodes, config['lyapunov_engine']['traffic_classes'])
        withdraw(demands, nodes, args.ack_timeout)
        print('Withdrawn only the policies matching this demand file', flush=True)
        return
    # Validate only after current topology/queue preflight succeeds.
    _, graph, _, queues = snapshot(nodes, args.queue_max_age)
    validate_demands(demands, graph, config['lyapunov_engine']['traffic_classes'])
    if args.port_base < 1024 or args.port_base + 1000 + len(demands) >= 65536:
        parser.error('Traffic port range is invalid')
    output = Path(args.output) / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
    output.mkdir(parents=True, exist_ok=True)
    atomic_json(output / 'demands.json', demands)
    initial = state_fingerprint(graph, queues)
    result = {'status': 'in_progress', 'topology_fingerprint': initial, 'cycles': [],
              'measurement': {'protocol': 'IPv6 UDP payload', 'duration': args.duration,
                              'warmup': args.warmup, 'configured_cycles': args.cycles}}
    write_report(output, result)
    print(f'Result directory: {output}', flush=True)
    try:
        for index in range(args.cycles):
            cycle_dir = output / f'cycle-{index + 1}'
            withdraw(demands, nodes, args.ack_timeout)
            _, graph, _, queues = snapshot(nodes, args.queue_max_age)
            if state_fingerprint(graph, queues) != initial:
                raise RuntimeError('Topology/capacity/delay changed before baseline')
            baseline, signature = phase(args, demands, nodes, cycle_dir / 'baseline', 'baseline')
            if signature != initial:
                raise RuntimeError('Topology/capacity/delay changed during baseline')
            solver_output = cycle_dir / 'controller'
            invocation = [sys.executable, '-m', 'integration.controller', '--demands', args.demands,
                          '--output', str(solver_output), '--once', '--apply',
                          '--ack-timeout', str(args.ack_timeout)]
            if args.containers:
                invocation += ['--containers'] + args.containers
            if args.queue_max_age:
                invocation += ['--queue-max-age', str(args.queue_max_age)]
            # Runs the same installed control-plane entrypoint and checks its
            # native SRv6 acceptance before starting the TE measurement.
            console = command(invocation, timeout=args.ack_timeout + 180)
            (solver_output / 'controller.log').write_text(console)
            te, signature = phase(args, demands, nodes, cycle_dir / 'te', 'te')
            if signature != initial:
                raise RuntimeError('Topology/capacity/delay changed during TE')
            item = comparison(baseline, te, args.max_cv_percent,
                              args.max_paired_offered_difference_percent, args.max_offered_error_percent)
            result['cycles'].append(item)
            write_report(output, result)
            change = f'{item["improvement_percent"]:.2f}%' if item['improvement_percent'] is not None else 'N/A'
            print(f'cycle={index + 1} baseline={baseline["received_mbps"]:.3f}Mbps '
                  f'te={te["received_mbps"]:.3f}Mbps change={change} '
                  f'valid={item["valid"]}', flush=True)
        result['status'] = 'valid' if all(c['valid'] for c in result['cycles']) else 'invalid_measurement'
        improvements = [c['improvement_percent'] for c in result['cycles'] if c['improvement_percent'] is not None]
        result['mean_improvement_percent'] = statistics.mean(improvements) if improvements else None
        write_report(output, result)
    except BaseException as exc:
        result['status'] = 'interrupted' if isinstance(exc, KeyboardInterrupt) else 'failed'
        result['error'] = str(exc)
        write_report(output, result)
        raise
    finally:
        if not args.keep_policy:
            try:
                withdraw(demands, nodes, args.ack_timeout)
            except Exception as exc:
                result['cleanup_error'] = str(exc)
                write_report(output, result)
    if result['status'] != 'valid':
        raise SystemExit(1)


if __name__ == '__main__':
    main()

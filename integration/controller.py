"""Single-container integration runtime. Default output is a reviewable plan."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time

import redis
import yaml

from integration.lsdb import (attach_metrics, coordinates, parse_lsdb, port_to,
                              positive, qdisc_metrics)
from integration.native_queues import collect_native_queues
from python_te_solver.lyapunov_solver import LyapunovSolver


STOP = False


def command(args, stdin=None, timeout=20):
    result = subprocess.run(args, input=stdin, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{args[:4]}: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def docker_read(container, path):
    return json.loads(command(['docker', 'exec', container, 'cat', path]))


def live_document(container):
    """Prefer live FRR, with a checked, freshly exported native-file fallback."""
    document = docker_read(container, '/lsdb.json')
    try:
        live = json.loads(command(['docker', 'exec', container, 'vtysh', '-c',
                                   'show isis database detail json']))
        parse_lsdb(dict(document, lsdb=live))
        document['lsdb'] = live
        document['topology_readback'] = 'live-vtysh'
    except (ValueError, RuntimeError, KeyError, TypeError) as exc:
        timestamp = document.get('queues', {}).get('timestamp', 0)
        period = document.get('queue_poll_s', 7)
        if not (-5 <= time.time() - float(timestamp) <= 2 * float(period)):
            raise ValueError(f'{container}: live FRR read failed ({exc}); native LSDB '
                             'is not a fresh queue-tick export; collect raw diagnostics') from exc
        parse_lsdb(document)  # Never replace an unsupported topology with guessed edges.
        document['topology_readback'] = 'fresh-native-lsdb'
        document['live_readback_warning'] = str(exc)
    return document


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def discover_nodes(selected=None):
    containers = selected or command(['docker', 'ps', '--format', '{{.Names}}']).splitlines()

    def inspect(container):
        try:
            # Unified images preserve the original node's /cfg.sh even when
            # running as a controller. Never mistake that snapshot for a node.
            try:
                role = command(['docker', 'exec', container, 'cat', '/run/leo-te-role']).strip()
            except RuntimeError:
                role = ''
            if role == 'control':
                return None
            config = docker_read(container, '/cfg.sh')
            coordinates(config['name'])
            return config['name'], {'container': container, 'config': config}
        except (RuntimeError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
            return None

    nodes = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        for item in executor.map(inspect, containers):
            if item:
                name, info = item
                if name in nodes:
                    raise ValueError(f"Two running containers claim {name}; select a single topology")
                nodes[name] = info
    if not nodes:
        raise ValueError('No running task-1 nodes with JSON /cfg.sh and Lrrcc name found')
    return nodes


def read_qdisc(container, iface):
    # Interface arguments are passed directly, without shell interpolation.
    if not re.fullmatch(r'[a-zA-Z0-9_.:-]+', iface):
        raise ValueError(f'Unsupported interface name: {iface}')
    prefix = ['docker', 'exec', container] if container else []
    records = json.loads(command(prefix + ['tc', '-j', '-s', 'qdisc', 'show', 'dev', iface]))
    text = command(prefix + ['tc', 'qdisc', 'show', 'dev', iface])
    result = qdisc_metrics(records, text)
    result['source'] = f'{container or "host"}:{iface}:root'
    return result


def collect_metrics(graph, nodes):
    """Prefer source-container TX queue, else host peer on destination side."""
    def collect(edge):
        src, dst = edge
        errors = []
        # Container src eth TX corresponds to src->dst.
        # Host-side veth at DESTINATION transmits into dst, also src->dst.
        candidates = []
        try:
            port = port_to(nodes[src]['config'], dst)
            candidates.append((nodes[src]['container'], port['eth']))
        except (KeyError, ValueError) as exc:
            errors.append(str(exc))
        try:
            port = port_to(nodes[dst]['config'], src)
            candidates.append((None, port['veth']))
        except (KeyError, ValueError) as exc:
            errors.append(str(exc))
        for container, iface in candidates:
            try:
                result = read_qdisc(container, iface)
                result.update(src=src, dst=dst, timestamp=time.time())
                return result
            except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
                errors.append(str(exc))
        raise ValueError(f'{src}->{dst}: cannot measure qdisc: {errors}')

    with ThreadPoolExecutor(max_workers=8) as executor:
        return list(executor.map(collect, graph.edges))


def validate_demands(raw, graph, classes):
    if not isinstance(raw, list) or not raw:
        raise ValueError('Demands must be a nonempty JSON list; no synthetic demands in live mode')
    seen_ids, seen_routes = set(), set()
    for flow in raw:
        flow_id = flow['id']
        if not isinstance(flow_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', flow_id):
            raise ValueError(f'Invalid flow id: {flow_id}')
        pair = (flow['src'], flow['dst'])
        if flow_id in seen_ids or pair in seen_routes:
            raise ValueError('Native tuner installs one route per (src,dst); duplicate flow/pair unsupported')
        if pair[0] == pair[1] or any(n not in graph for n in pair):
            raise ValueError(f'Invalid or SRv6-unready demand endpoints: {pair}')
        if flow['class'] not in classes:
            raise ValueError(f'Unknown traffic class: {flow["class"]}')
        positive(flow['demand'], 'demand kbps')
        seen_ids.add(flow_id)
        seen_routes.add(pair)
    return raw


def make_native_policy(flow_id, path, priority):
    if len(path) < 2:
        raise ValueError('Policy path must contain at least two nodes')
    for node in path:
        coordinates(node)
    # Exact fields consumed by frr-auto66.sh:parse_one().
    return {'biz_id': f'leo_te_{flow_id}', 'src': path[0], 'dst': path[-1],
            'mode': 'end', 'priority': priority, 'via': path[1:]}


def install_commands(policies, nodes, timeout):
    submitted = {}
    for policy in policies:
        container = nodes[policy['src']]['container']
        # Check the existing tuner; the controller does not start/reconfigure FRR.
        command(['docker', 'exec', container, 'pgrep', '-f', r'[a]uto66(-lite)?\.sh --tuner'])
        token = hashlib.sha256(policy['biz_id'].encode()).hexdigest()[:24]
        filename = f'/path.cmd/leo-te-{token}.json'
        script = ('set -eu; mkdir -p /path.cmd; '
                  'cat > "$1.tmp"; mv "$1.tmp" "$1"')
        command(['docker', 'exec', '-i', container, 'sh', '-c', script, 'leo-te', filename],
                stdin=json.dumps(policy) + '\n')
        submitted[policy['biz_id']] = (container, time.time(), filename)
    deadline = time.monotonic() + timeout
    feedback = {}
    pending = set(submitted)
    while pending and time.monotonic() < deadline and not STOP:
        for biz in list(pending):
            container, started, _ = submitted[biz]
            try:
                status = docker_read(container, '/path-tuner.status').get(biz, {})
                installed_at = datetime.fromisoformat(status.get('installed_at', '')).timestamp()
                # Native timestamps have second precision. Permit truncation only.
                if installed_at < int(started):
                    continue
                if status.get('status') == 'ok' and status.get('route') == 'seg6' and status.get('ping') == 'pass':
                    feedback[biz] = status
                    pending.remove(biz)
            except (ValueError, RuntimeError, TypeError):
                continue
        if pending:
            time.sleep(1)
    if pending:
        raise RuntimeError(f'Native path acceptance timed out: {sorted(pending)}; '
                           'inspect /path-tuner.status and /auto-debug.log on source nodes')
    return feedback


def topology_summary(document, graph, warnings):
    return {'source_self': document.get('self'), 'source_updated_at': document.get('updated_at'),
            'nodes': len(graph), 'directed_links': graph.number_of_edges(),
            'srv6_ready_nodes': sum(bool(d.get('srv6_ready')) for _, d in graph.nodes(data=True)),
            'warnings': warnings, 'node_details': dict(graph.nodes(data=True)),
            'links': [dict(src=u, dst=v, **d) for u, v, d in graph.edges(data=True)]}


def cycle(args, solver, output):
    if getattr(args, 'node_map', None):
        nodes = json.loads(Path(args.node_map).read_text())
        for node, info in nodes.items():
            coordinates(node)
            if info['config']['name'] != node:
                raise ValueError('Explicit node-map identity mismatch')
    else:
        nodes = {} if args.lsdb_file else discover_nodes(args.containers)
    if args.lsdb_file:
        document = json.loads(Path(args.lsdb_file).read_text())
    else:
        reference = args.lsdb_container or nodes[sorted(nodes)[0]]['container']
        document = live_document(reference)
    graph, warnings = parse_lsdb(document)
    if getattr(args, 'restrict_topology', False):
        graph = graph.subgraph(nodes).copy()
    summary = topology_summary(document, graph, warnings)
    atomic_json(output / 'topology.json', summary)
    if args.inspect:
        if args.inspect_queues:
            if not nodes:
                raise ValueError('--inspect-queues requires running nodes')
            records = collect_native_queues(graph, nodes, command, args.queue_max_age)
            atomic_json(output / 'queues.json', {'read_at': time.time(), 'links': records})
            print(f'Native queues: {sum(r.get("queue_bytes") is not None for r in records)}/{len(records)} measurable', flush=True)
        print(f'Topology: nodes={len(graph)}, directed_links={graph.number_of_edges()}, '
              f'SRv6-ready={summary["srv6_ready_nodes"]}', flush=True)
        return
    usable = graph.subgraph([n for n, d in graph.nodes(data=True) if d['srv6_ready']]).copy()
    if not usable:
        raise ValueError('No SRv6-capable topology available')
    if nodes:
        missing = set(usable) - nodes.keys()
        if missing:
            raise ValueError(f'LSDB nodes have no running /cfg.sh mapping: {sorted(missing)}')
    if args.metrics_file:
        metrics_document = json.loads(Path(args.metrics_file).read_text())
        records = metrics_document['links']
        if not args.offline_demo:
            timestamp = positive(metrics_document['timestamp'], 'metrics timestamp')
            if not (-5 <= time.time() - timestamp <= args.metrics_max_age):
                raise ValueError('Metrics file is stale or timestamp is in the future')
            records = [dict(record, timestamp=timestamp) for record in records]
    elif args.offline_demo:
        records = []
    elif args.metrics_source == 'lsdb':
        records = collect_native_queues(usable, nodes, command, args.queue_max_age)
        atomic_json(output / 'queues.json', {'read_at': time.time(), 'links': records})
    else:
        records = collect_metrics(usable, nodes)
    missing_metrics = attach_metrics(usable, records, args.capacity_mbps, args.delay_ms,
                                    allow_missing=args.offline_demo)
    flows = validate_demands(json.loads(Path(args.demands).read_text()), usable, solver.classes)
    results = solver.solve_lyapunov_mcf(usable, flows) or {}
    if set(results) != {f['id'] for f in flows}:
        raise ValueError('Some requested flows have no installable path; refusing partial policy set')
    policies = [make_native_policy(flow_id, routes[0]['path'], args.priority)
                for flow_id, routes in sorted(results.items())]
    # Native shell derives a node SID for every hop; check all advertised SIDs
    # through parse_lsdb(), and respect the solver's configured SID depth.
    if any(len(p['via']) > solver.max_sid_depth for p in policies):
        raise ValueError('Computed policy exceeds maximum SID depth')
    plan = {'generated_at': datetime.now(timezone.utc).isoformat(),
            'mode': 'apply' if args.apply else 'dry-run', 'offline_demo': args.offline_demo,
            'missing_metrics_with_demo_defaults': missing_metrics,
            'demands_unit': 'kbps', 'policies': policies,
            'link_metrics': [dict(src=u, dst=v, **d) for u, v, d in usable.edges(data=True)]}
    atomic_json(output / 'plan.json', plan)
    atomic_json(output / 'acceptance.json', {'status': 'pending' if args.apply else 'dry-run',
                                          'generated_at': plan['generated_at']})
    for policy in policies:
        atomic_json(output / 'path.cmd' / policy['src'] / (policy['biz_id'] + '.json'), policy)
    solver.r.set('integration:latest_plan', json.dumps(plan))
    if args.apply:
        feedback = install_commands(policies, nodes, args.ack_timeout)
        atomic_json(output / 'acceptance.json', {'status': 'accepted',
                    'generated_at': plan['generated_at'], 'native_status': feedback})
    print(f'Completed: mode={plan["mode"]}, policies={len(policies)}, '
          f'offline_demo={args.offline_demo}, output={output}', flush=True)


def main():
    parser = argparse.ArgumentParser(description='课题1/3 LSDB → Lyapunov TE → 原生 path.cmd')
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--lsdb-file', help='Read a mounted /lsdb.json snapshot')
    source.add_argument('--lsdb-container', help='Read /lsdb.json from this running node (default: discover)')
    parser.add_argument('--inspect', action='store_true', help='Only normalize and report topology')
    parser.add_argument('--inspect-queues', action='store_true', help='Also inspect native queues from every node')
    parser.add_argument('--containers', nargs='+', help='Restrict discovery to existing container names')
    parser.add_argument('--node-map', help='Explicit checked node/container/interface mapping from the field adapter')
    parser.add_argument('--restrict-topology', action='store_true',
                        help='Use only live advertised edges between explicitly selected containers')
    parser.add_argument('--metrics-source', choices=['lsdb', 'qdisc'], default='lsdb',
                        help='Default: native per-node /lsdb.json queues (no host-veth guessing)')
    parser.add_argument('--queue-max-age', type=float, help='Native queues age limit; default 2 x queue_poll_s')
    parser.add_argument('--demands', help='Business demands JSON; demand is in kbps')
    parser.add_argument('--metrics-file', help='Explicit normalized link metrics; otherwise read qdisc')
    parser.add_argument('--metrics-max-age', type=float, default=10)
    parser.add_argument('--capacity-mbps', type=float, help='Explicit fallback only; LSDB metric is not bandwidth')
    parser.add_argument('--delay-ms', type=float, help='Explicit fallback only; LSDB metric is not delay')
    parser.add_argument('--offline-demo', action='store_true', help='File-only dry-run with marked 16Mbps/10ms/0B defaults')
    parser.add_argument('--apply', action='store_true', help='Write native commands to source nodes and await acceptance')
    parser.add_argument('--priority', type=int, default=10)
    parser.add_argument('--ack-timeout', type=float, default=60)
    parser.add_argument('--interval', type=float, default=5)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--output', default='/output')
    parser.add_argument('--config', default=str(Path(__file__).resolve().parents[1] / 'config/controller.yaml'))
    args = parser.parse_args()
    if args.inspect_queues:
        args.inspect = True
    if not args.inspect and not args.demands:
        parser.error('--demands is required to compute real business routes')
    if args.offline_demo and (not args.lsdb_file or args.apply):
        parser.error('--offline-demo requires --lsdb-file and cannot be combined with --apply')
    if args.apply and (args.lsdb_file or args.inspect):
        parser.error('--apply requires a live Docker source and cannot be used with --inspect')
    if args.lsdb_file and not (args.inspect or args.offline_demo or args.metrics_file):
        parser.error('File snapshots require --metrics-file or explicit --offline-demo')
    if args.priority < 0 or args.interval <= 0 or args.ack_timeout <= 0:
        parser.error('priority must be nonnegative; interval and ack-timeout must be positive')
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)

    def stop(signum, frame):
        global STOP
        STOP = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    # One embedded Redis per controller, Unix socket only: no host port collision.
    with tempfile.TemporaryDirectory(prefix='leo-te-') as runtime:
        socket_path = str(Path(runtime) / 'redis.sock')
        server = subprocess.Popen(['redis-server', '--port', '0', '--unixsocket', socket_path,
                                   '--unixsocketperm', '700', '--save', '', '--appendonly', 'no',
                                   '--dir', runtime, '--loglevel', 'warning'])
        try:
            client = redis.Redis(unix_socket_path=socket_path)
            for _ in range(100):
                if server.poll() is not None:
                    raise RuntimeError('Embedded Redis exited during startup')
                try:
                    client.ping()
                    break
                except redis.ConnectionError:
                    time.sleep(.05)
            else:
                raise RuntimeError('Embedded Redis startup timeout')
            config = yaml.safe_load(Path(args.config).read_text())
            config['redis']['unix_socket_path'] = socket_path
            config_path = Path(runtime) / 'controller.yaml'
            config_path.write_text(yaml.safe_dump(config))
            solver = LyapunovSolver(config_path)
            while not STOP:
                try:
                    cycle(args, solver, output)
                    atomic_json(output / 'health.json', {'status': 'ok', 'timestamp': time.time()})
                except Exception as exc:
                    atomic_json(output / 'health.json', {'status': 'error', 'error': str(exc),
                                                       'timestamp': time.time()})
                    print(f'Integration error: {exc}', flush=True)
                    if args.once:
                        raise
                if args.once:
                    break
                until = time.monotonic() + args.interval
                while time.monotonic() < until and not STOP:
                    time.sleep(.2)
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()


if __name__ == '__main__':
    main()

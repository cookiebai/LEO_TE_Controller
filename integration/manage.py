"""Manage collectors on existing task-1/3 nodes; never create a topology."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import time

import networkx as nx

from integration.controller import atomic_json, command, discover_nodes, docker_read, topology_summary, live_document
from integration.lsdb import parse_lsdb
from integration.native_queues import collect_native_queues


VENDOR = Path(__file__).parent / 'vendor/queue-20261027'


def install_scripts(info):
    container = info['container']
    sources = {'auto66-lite.sh': VENDOR / 'auto66-lite.sh',
               'queue-probe-original.sh': VENDOR / 'queue-probe.sh',
               'queue-probe.sh': Path(__file__).parent / 'queue-probe.sh',
               'queue-probe.py': Path(__file__).parent / 'queue_probe.py'}
    for name, source in sources.items():
        path = '/' + name
        script = ('set -eu; if [ -f "$1" ] && [ ! -e "$1.before-20261027" ]; then '
                  'cp -p "$1" "$1.before-20261027"; fi; '
                  'cat > "$1.tmp"; chmod 755 "$1.tmp"; mv "$1.tmp" "$1"')
        command(['docker', 'exec', '-i', container, 'sh', '-c', script, 'leo-install', path],
                stdin=source.read_text())
    return container


def snapshot(nodes, max_age=None):
    reference = nodes[sorted(nodes)[0]]['container']
    document = live_document(reference)
    graph, warnings = parse_lsdb(document)
    queues = collect_native_queues(graph, nodes, command, max_age)
    missing = [record for record in queues if record.get('queue_bytes') is None or
               record.get('capacity_mbps') is None or record.get('delay_ms') is None]
    if missing:
        raise ValueError(f'Native metrics missing on {len(missing)} links: {missing[:3]}')
    return document, graph, warnings, queues


def main():
    parser = argparse.ArgumentParser(description='只管理课题1/3已存在的拓扑与采集脚本；不创建节点或链路')
    parser.add_argument('action', choices=['install', 'start', 'ensure-qdisc', 'status', 'wait', 'snapshot'])
    parser.add_argument('--containers', nargs='+')
    parser.add_argument('--output', default='/output')
    parser.add_argument('--timeout', type=float, default=240)
    parser.add_argument('--expected-nodes', type=int)
    parser.add_argument('--queue-max-age', type=float)
    parser.add_argument('--rate', help='Explicit example rate for a previously empty/noqueue root, e.g. 16mbit')
    parser.add_argument('--delay', help='Explicit example propagation delay, e.g. 10ms')
    args = parser.parse_args()
    nodes = discover_nodes(args.containers)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    atomic_json(output / 'nodes.json', nodes)
    if args.expected_nodes is not None and len(nodes) != args.expected_nodes:
        raise ValueError(f'Expected {args.expected_nodes} existing nodes, discovered {len(nodes)}')
    if args.action == 'ensure-qdisc':
        if not args.rate or not args.delay:
            parser.error('ensure-qdisc requires explicit --rate and --delay')
        import re
        if not re.fullmatch(r'[0-9.]+[kKmMgG]?bit', args.rate) or not re.fullmatch(r'[0-9.]+(us|ms|s)', args.delay):
            parser.error('Invalid tc rate or delay')
        changes = []
        for node, info in sorted(nodes.items()):
            for port in info['config']['ports'].values():
                if not port.get('linkid') or port['eth'] in ('eth0', 'lo'):
                    continue
                prefix = ['docker', 'exec', info['container']]
                iface = port['eth']
                records = json.loads(command(prefix + ['tc', '-j', 'qdisc', 'show', 'dev', iface]))
                existing = [q for q in records if q.get('root') and q['kind'] != 'noqueue']
                if existing:
                    changes.append({'node': node, 'interface': iface, 'action': 'preserved-existing-root'})
                    continue
                command(prefix + ['tc', 'qdisc', 'add', 'dev', iface, 'root', 'handle', '1:',
                                  'tbf', 'rate', args.rate, 'burst', '32kb', 'latency', '100ms'])
                command(prefix + ['tc', 'qdisc', 'add', 'dev', iface, 'parent', '1:1', 'handle', '10:',
                                  'netem', 'delay', args.delay, 'limit', '1000'])
                changes.append({'node': node, 'interface': iface, 'action': 'created-on-empty-root',
                                'rate': args.rate, 'delay': args.delay})
            print(f'qdisc checked: {node}', flush=True)
        atomic_json(output / 'qdisc-actions.json', changes)
        return
    if args.action in ('install', 'start'):
        def process(item):
            node, info = item
            install_scripts(info)
            if args.action == 'start':
                # Avoid two writers/watchers. Only the vendor's legacy processes
                # are stopped; FRR, topology links and qdisc remain in place.
                command(['docker', 'exec', info['container'], 'bash', '/auto66-lite.sh', 'stop-old'])
                result = command(['docker', 'exec', info['container'], 'bash', '/auto66-lite.sh'], timeout=90)
                (output / f'{node}-start.log').write_text(result)
            print(f'{args.action}: {node} ({info["container"]})', flush=True)
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(process, sorted(nodes.items())))
        return
    if args.action == 'status':
        for node, info in sorted(nodes.items()):
            try:
                tail = command(['docker', 'exec', info['container'], 'tail', '-c', '3500', '/lsdb.json'])
                from integration.native_queues import decode_tail, validate_native_queues
                records = validate_native_queues(decode_tail(tail), node, info['config'], max_age=args.queue_max_age)
                print(f'{node}: queues={sum(r["queue_bytes"] is not None for r in records)}/{len(records)}, '
                      f'age={max(r["age_seconds"] for r in records):.1f}s')
            except Exception as exc:
                print(f'{node}: NOT READY: {exc}')
        return
    deadline = time.monotonic() + args.timeout
    while True:
        try:
            document, graph, warnings, queues = snapshot(nodes, args.queue_max_age)
            if set(graph) != set(nodes):
                raise ValueError('Live LSDB membership differs from discovered existing nodes')
            if not all(data['srv6_ready'] for _, data in graph.nodes(data=True)):
                raise ValueError('Some nodes have not advertised SRv6 End SID')
            if not nx.is_strongly_connected(graph):
                raise ValueError('Live advertised topology is not strongly connected')
            atomic_json(output / 'topology.json', topology_summary(document, graph, warnings))
            atomic_json(output / 'queues.json', {'read_at': time.time(), 'links': queues})
            print(f'READY: existing_nodes={len(graph)}, directed_links={graph.number_of_edges()}, '
                  f'fresh_measured_queues={len(queues)}', flush=True)
            return
        except Exception as exc:
            if args.action == 'snapshot' or time.monotonic() >= deadline:
                raise
            print(f'Waiting for existing topology/queues: {exc}', flush=True)
            time.sleep(5)


if __name__ == '__main__':
    main()

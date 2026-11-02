"""Same queues protocol as task 1, with exact tc JSON counters under load."""
from datetime import datetime
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time


def read_json(args):
    result = subprocess.run(args, text=True, capture_output=True, timeout=5, check=True)
    return json.loads(result.stdout)


def qdisc_values(qdiscs):
    roots = [q for q in qdiscs if q.get('root')]
    if len(roots) != 1:
        return {'measured': False, 'queue_bytes': None, 'reason': 'multiple-or-missing-root-qdiscs'}
    root = roots[0]
    kind = root['kind']
    if kind in ('noqueue', 'clsact', 'ingress', 'mq') or 'backlog' not in root:
        return {'measured': False, 'queue_bytes': None, 'reason': 'not-measurable-kind-or-no-backlog'}
    rates, delays = [], []
    for qdisc in qdiscs:
        options = qdisc.get('options', {})
        if not isinstance(options, dict):
            continue
        rate = options.get('rate64', options.get('rate'))
        if isinstance(rate, dict):
            rate = rate.get('rate')
        if isinstance(rate, (int, float)) and rate > 0:
            # iproute2 TBF/netem JSON rate is BYTES per second.
            rates.append(rate * 8 / 1e6)
        delay = options.get('delay')
        if isinstance(delay, dict):
            delay = delay.get('delay')
        if qdisc['kind'] == 'netem' and isinstance(delay, (int, float)):
            # iproute2 5.15 in the supplied Ubuntu22.04 node outputs seconds.
            delays.append(delay * 1000)
    return {'measured': True, 'queue_bytes': int(root['backlog']),
            'qlen_packets': root.get('qlen'), 'drops_total': root.get('drops'),
            'overlimits_total': root.get('overlimits'),
            'capacity_mbps': min(rates) if rates else None,
            'delay_ms': sum(delays) if delays else None,
            'qdisc': f'{kind} {root.get("handle", "")} Sent {root.get("bytes", 0)}b/{root.get("packets", 0)}p'}


def sample(config, qdiscs, interfaces, timestamp=None):
    node = config['name']
    links = []
    by_iface = {}
    for qdisc in qdiscs:
        by_iface.setdefault(qdisc.get('dev'), []).append(qdisc)
    by_link = {item['ifname']: item for item in interfaces}
    for port in config['ports'].values():
        if not port.get('linkid') or port['eth'] in ('lo', 'eth0'):
            continue
        peers = {name for link in port['linkid'] for name in re.findall(r'L\d{4}', link) if name != node}
        if len(peers) != 1:
            raise ValueError(f'Cannot identify exactly one peer for {node}:{port["eth"]}')
        iface = port['eth']
        result = qdisc_values(by_iface.get(iface, []))
        result.update(src=node, dst=peers.pop(), interface=iface,
                      carrier='up' if 'LOWER_UP' in by_link.get(iface, {}).get('flags', []) else 'down',
                      source=os.environ.get('QPROBE_SOURCE', 'task1-queue-probe-json'))
        links.append(result)
    if not links:
        raise ValueError('No business link with peer found in /cfg.sh')
    timestamp = time.time() if timestamp is None else timestamp
    return {'node': node, 'namespace': 'container:' + socket.gethostname(),
            'netns_inode': str(os.stat('/proc/self/ns/net').st_ino),
            'timestamp': timestamp, 'sampled_at': datetime.fromtimestamp(timestamp).astimezone().isoformat(),
            'units': {'queue_bytes': 'bytes', 'qlen_packets': 'packets',
                      'drops_total': 'cumulative packets', 'overlimits_total': 'cumulative events',
                      'capacity_mbps': 'Mbps', 'delay_ms': 'ms'},
            'links': links, 'links_total': len(links),
            'links_measured': sum(link['measured'] for link in links)}


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ('-h', '--help', 'help'):
        print('queue-probe.sh --once: exact tc JSON counters, same native queues contract')
        return
    if len(sys.argv) > 1 and sys.argv[1] != '--once':
        raise SystemExit('Only --once is supported (no argument = self-check)')
    config = json.loads(Path(os.environ.get('QPROBE_CFG', '/cfg.sh')).read_text())
    qdiscs = read_json(['tc', '-j', '-s', 'qdisc', 'show'])
    interfaces = read_json(['ip', '-j', 'link', 'show'])
    result = sample(config, qdiscs, interfaces)
    if len(sys.argv) > 1:
        print(json.dumps(result, ensure_ascii=False, separators=(',', ':')))
    else:
        print(f'node={result["node"]}, measurable={result["links_measured"]}/{result["links_total"]}')
        for link in result['links']:
            print(json.dumps(link, ensure_ascii=False))


if __name__ == '__main__':
    main()

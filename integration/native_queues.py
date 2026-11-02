"""Consume the exact queues tail supplied in 队列采集交付.zip."""
from concurrent.futures import ThreadPoolExecutor
import json
import re
import time

from integration.lsdb import positive, port_to


def decode_tail(text):
    # auto66-lite atomically replaces /lsdb.json and always writes queues LAST.
    # Read just 16 KiB per node instead of transferring its full ~620 KiB LSDB.
    matches = list(re.finditer(r'"queue_poll_s"\s*:\s*(\d+)\s*,\s*"queues"\s*:', text))
    if not matches:
        raise ValueError('No native queues tail; start /auto66-lite.sh and /queue-probe.sh')
    match = matches[-1]
    queues, _ = json.JSONDecoder().raw_decode(text[match.end():].lstrip())
    return {'queue_poll_s': int(match.group(1)), 'queues': queues}


def validate_native_queues(document, node, config=None, now=None, max_age=None):
    queues = document['queues']
    if queues.get('node') != node:
        raise ValueError(f'{node}: queues belong to {queues.get("node")}')
    period = positive(document['queue_poll_s'], 'queue_poll_s')
    timestamp = positive(queues['timestamp'], 'queue timestamp')
    now = time.time() if now is None else now
    age = now - timestamp
    limit = 2 * period if max_age is None else positive(max_age, 'queue max age')
    if not (-5 <= age <= limit):
        raise ValueError(f'{node}: stale/future native queues: age={age:.2f}s, limit={limit}s')
    records, seen = [], set()
    for link in queues['links']:
        src, dst = link['src'], link['dst']
        if src != node or src == dst or (src, dst) in seen:
            raise ValueError(f'{node}: duplicate or invalid queue direction {src}->{dst}')
        seen.add((src, dst))
        if config is not None:
            expected = port_to(config, dst)['eth']
            if link['interface'] != expected:
                raise ValueError(f'{src}->{dst}: queue interface differs from /cfg.sh')
        record = dict(link, timestamp=timestamp, queue_poll_s=period,
                      age_seconds=age, namespace=queues.get('namespace'),
                      netns_inode=queues.get('netns_inode'),
                      source=f'lsdb.queues:{link.get("source", "task1-queue-probe")}')
        if link.get('measured') is not True or link.get('carrier', 'up') != 'up':
            record['queue_bytes'] = None
            record['missing_reason'] = link.get('reason', 'carrier-down-or-unmeasured')
        elif link.get('queue_bytes') is None:
            raise ValueError(f'{src}->{dst}: measured=true but queue_bytes is null')
        else:
            record['queue_bytes'] = positive(link['queue_bytes'], 'queue bytes', zero=True)
        for key in ('capacity_mbps', 'delay_ms'):
            if record.get(key) is not None:
                record[key] = positive(record[key], key, zero=key == 'delay_ms')
        records.append(record)
    if int(queues['links_total']) != len(records):
        raise ValueError(f'{node}: inconsistent links_total')
    return records


def collect_native_queues(graph, nodes, run_command, max_age=None):
    def read(node):
        if node not in nodes:
            raise ValueError(f'No running node container for {node}')
        info = nodes[node]
        tail = run_command(['docker', 'exec', info['container'], 'tail', '-c', '16384', '/lsdb.json'])
        return validate_native_queues(decode_tail(tail), node, info['config'], max_age=max_age)

    with ThreadPoolExecutor(max_workers=8) as executor:
        batches = list(executor.map(read, sorted(graph.nodes)))
    records = [record for batch in batches for record in batch
               if graph.has_edge(record['src'], record['dst'])]
    expected = set(graph.edges)
    actual = {(record['src'], record['dst']) for record in records}
    if expected != actual:
        raise ValueError(f'Native queue coverage incomplete: missing={sorted(expected - actual)[:8]}')
    # Collection itself takes time. Validate freshness at the END as well.
    now = time.time()
    for record in records:
        limit = max_age if max_age is not None else 2 * record['queue_poll_s']
        if now - record['timestamp'] > limit:
            raise ValueError(f'Queue expired during collection: {record["src"]}->{record["dst"]}')
    return records

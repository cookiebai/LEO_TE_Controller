"""Temporarily pin verified point-to-point peers for frozen-topology A/B tests.

Both phases use the same neighbor map. No route, rate, delay, or loss setting
is changed. A durable undo manifest supports cleanup and interrupted-run recovery.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import json
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import lab

ACTIVE = ROOT.parent / 'runtime/fixed-neighbors-active.json'


def execute(args, **kwargs):
    result = subprocess.run(args, text=True, capture_output=True, timeout=60, **kwargs)
    if result.returncode:
        raise RuntimeError(f'{args[:4]}: {result.stderr}')
    return result.stdout


def save(path, data):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, indent=2))
    temporary.replace(path)


def inventory(node):
    output = execute(['docker', 'exec', node, 'sh', '-c', 'ip -j address show; ip -j neigh show'])
    addresses, neighbors = map(json.loads, output.splitlines())
    return {'interfaces': {item['ifname']: item for item in addresses}, 'neighbors': neighbors}


def peer_entries(links, inventories):
    indexed = {(link['src'], link['dst']): link for link in links}
    plan = {name: [] for name in inventories}
    for link in links:
        if link.get('status') != 'UP':
            raise RuntimeError('Cannot pin a non-UP topology')
        src, dst, dev = link['src'], link['dst'], link['local_iface']
        if not re.fullmatch(r'[0-9a-f]{8}', dev):
            raise RuntimeError('Unexpected experiment interface: ' + dev)
        reverse = indexed.get((dst, src))
        if reverse is None:
            raise RuntimeError('Missing reverse link')
        local = inventories[src]['interfaces'][dev]
        peer = inventories[dst]['interfaces'][reverse['local_iface']]
        mac = peer['address']
        if not re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}', mac):
            raise RuntimeError('Invalid peer MAC')
        networks = [ipaddress.ip_interface(f"{a['local']}/{a['prefixlen']}").network
                    for a in local['addr_info'] if a['family'] == 'inet' and a['local'].startswith('10.')]
        addresses = [a['local'] for a in peer['addr_info']
                     if (a['family'] == 'inet' and any(ipaddress.ip_address(a['local']) in net for net in networks))
                     or (a['family'] == 'inet6' and a['scope'] == 'link')]
        if not any(ipaddress.ip_address(a).version == 4 for a in addresses):
            raise RuntimeError('Peer does not share the directly connected IPv4 subnet')
        addresses += [a['local'] for a in inventories[dst]['interfaces']['lo']['addr_info']
                      if a['family'] == 'inet6' and a['local'].startswith('fd01:')]
        previous = {(n['dev'], n['dst']): n for n in inventories[src]['neighbors']}
        for address in sorted(set(addresses)):
            address = str(ipaddress.ip_address(address))
            plan[src].append({'dev': dev, 'dst': address, 'lladdr': mac,
                              'previous': previous.get((dev, address))})
    return plan


def batch(node, commands):
    if commands:
        execute(['docker', 'exec', '-i', node, 'ip', '-batch', '-'], input='\n'.join(commands) + '\n')


def apply_node(item):
    node, entries = item
    batch(node, [f"neigh replace {e['dst']} lladdr {e['lladdr']} nud permanent dev {e['dev']}" for e in entries])
    current = {(n['dev'], n['dst']): n for n in json.loads(execute(['docker', 'exec', node, 'ip', '-j', 'neigh', 'show']))}
    for entry in entries:
        actual = current.get((entry['dev'], entry['dst']), {})
        if actual.get('lladdr') != entry['lladdr'] or 'PERMANENT' not in actual.get('state', []):
            raise RuntimeError('Neighbor pin verification failed: ' + node)


def restore_commands(entries, current):
    now = {(n['dev'], n['dst']): n for n in current}
    commands = []
    for entry in entries:
        actual = now.get((entry['dev'], entry['dst']), {})
        # Do not overwrite an independently changed neighbor or recreate deleted entries.
        if actual.get('lladdr') != entry['lladdr'] or 'PERMANENT' not in actual.get('state', []):
            continue
        old = entry['previous']
        if old and old.get('lladdr'):
            state = 'permanent' if 'PERMANENT' in old.get('state', []) else 'stale'
            router = ' router' if 'router' in old else ''
            commands.append(f"neigh replace {entry['dst']} lladdr {old['lladdr']} nud {state} dev {entry['dev']}{router}")
        else:
            commands.append(f"neigh del {entry['dst']} dev {entry['dev']}")
    return commands


def restore(path):
    data = json.loads(path.read_text())
    lab.verify_ownership()
    def one(item):
        node, entries = item
        inspected = subprocess.run(['docker', 'inspect', '--format', '{{.Id}}', node],
                                   capture_output=True, text=True, timeout=30)
        if inspected.returncode:
            if 'No such object' in inspected.stderr:
                return  # Deleted containers also deleted their neighbor namespaces.
            raise RuntimeError(inspected.stderr)
        if inspected.stdout.strip() != data['container_ids'][node]:
            raise RuntimeError('Container identity changed; refusing neighbor restore: ' + node)
        current = json.loads(execute(['docker', 'exec', node, 'ip', '-j', 'neigh', 'show']))
        batch(node, restore_commands(entries, current))
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(one, data['nodes'].items()))
    data.update(status='restored', restored_unix=time.time())
    save(path, data)
    if ACTIVE.exists() and json.loads(ACTIVE.read_text())['manifest'] == str(path):
        ACTIVE.unlink()
    print('Restored pre-experiment neighbor configuration: ' + str(path), flush=True)


def pin(path):
    lab.verify_ownership()
    if ACTIVE.exists():
        restore(Path(json.loads(ACTIVE.read_text())['manifest']))
    client = lab.redis_client()
    links = [client.hgetall(key) for key in sorted(client.scan_iter('topo:link:*'))]
    names = sorted({link[key] for link in links for key in ('src', 'dst')})
    if not names or any(not re.fullmatch(r'(Satellite|GroundStation)_[0-9a-f]{8}', n) for n in names):
        raise RuntimeError('Missing or unexpected topology nodes')
    if set(names) != set(lab.nodes()):
        raise RuntimeError('Snapshot does not match running lab nodes')
    inspected = json.loads(execute(['docker', 'inspect', *names]))
    identities = {item['Name'].lstrip('/'): item['Id'] for item in inspected}
    with ThreadPoolExecutor(max_workers=16) as pool:
        inventories = dict(zip(names, pool.map(inventory, names)))
    plan = peer_entries(links, inventories)
    data = {'mode': 'fixed-peer-neighbors-for-both-phases', 'status': 'prepared',
            'prepared_unix': time.time(), 'container_ids': identities, 'nodes': plan}
    save(path, data)
    save(ACTIVE, {'manifest': str(path)})  # Durable undo intent before the first mutation.
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(apply_node, plan.items()))
    data.update(status='pinned', pinned_unix=time.time())
    save(path, data)
    print(f'Fixed peer neighbors: {len(names)} nodes, {sum(map(len, plan.values()))} entries; undo={path}', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('pin', 'restore'))
    parser.add_argument('manifest', type=Path)
    args = parser.parse_args()
    path = args.manifest.resolve()
    if not path.is_relative_to((ROOT / 'results/runs').resolve()):
        parser.error('Undo manifest must be under this lab results/runs')
    if args.action == 'pin':
        pin(path)
    elif path.exists():
        restore(path)

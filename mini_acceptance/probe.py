"""Read existing nodes and explicit local topology, without a global gate."""
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import json
import re
import subprocess

import networkx as nx

from integration.lsdb import coordinates, parse_lsdb


def call(args, text=None, timeout=25, check=True):
    result = subprocess.run(args, input=text, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f'{args}: {result.stderr.strip() or result.stdout.strip()}')
    return result.stdout if check else result


def inside(container, args, text=None, timeout=25):
    return call(['docker', 'exec'] + (['-i'] if text is not None else []) + [container] + args,
                text=text, timeout=timeout)


def address(node, sid=False):
    row, col = coordinates(node)
    return f'fc00:{row:x}:{col:x}::' if sid else f'fc00:0:{row:x}::{col:x}'


def end_route(routes, sid):
    target = ipaddress.IPv6Address(sid)
    for route in routes:
        encap = route.get('encap')
        action = route.get('action')
        if isinstance(encap, dict):
            action = encap.get('action', action)
            encap = encap.get('type')
        if encap != 'seg6local' or action != 'End':
            continue
        try:
            if target in ipaddress.IPv6Network(route['dst'], strict=False):
                return route
        except ValueError:
            pass
    return None


def normalize(config, document, interfaces, routes):
    by_name = {link['ifname']: link for link in interfaces}
    identities = set()
    for link in interfaces:
        if link['ifname'] == 'lo':
            for a in link.get('addr_info', []):
                try:
                    groups = ipaddress.IPv6Address(a['local']).exploded.split(':')
                    if a.get('prefixlen') == 128 and groups[:2] == ['fc00', '0000']:
                        node = f'L{int(groups[2],16):02d}{int(groups[-1],16):02d}'
                        coordinates(node)
                        identities.add(node)
                except (ValueError, KeyError):
                    pass
    if len(identities) != 1:
        raise ValueError(f'Need one live task-1 loopback identity, got {sorted(identities)}')
    node = next(iter(identities))
    parsed, error = None, None
    try:
        parsed, _ = parse_lsdb(document)
    except (ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
        error = str(exc)
    peers = {}
    if document.get('self') == node:
        for pair, values in document.get('link_addr_pair', {}).items():
            parts = pair.split('->')
            if len(parts) == 2 and parts[0] == node:
                peers[parts[1]] = {'interface': values.get('iface'), 'source': 'lsdb.link_addr_pair'}
        for neighbor in document.get('neighbors', []):
            name = neighbor.get('name', '')
            if re.fullmatch(r'L\d{4}', name):
                peers.setdefault(name, {'interface': neighbor.get('interface', '').split('@')[0],
                                        'source': 'lsdb.neighbors'})
    if parsed is not None and node in parsed:
        for _, peer, attrs in parsed.out_edges(node, data=True):
            if attrs.get('local_iface'):
                peers.setdefault(peer, {'interface': attrs['local_iface'], 'source': 'lsdb.lsp+neighbors'})
    # cfg provides interface mapping only when a real LSDB advertisement exists.
    if config.get('name') == node:
        for mac, port in config.get('ports', {}).items():
            iface = port.get('eth')
            if by_name.get(iface, {}).get('address', '').lower() != mac.lower():
                continue
            names = {n for link in port.get('linkid', []) for n in re.findall(r'L\d{4}', link)}
            if node not in names or len(names) != 2:
                continue
            peer = next(iter(names-{node}))
            if parsed is not None and parsed.has_edge(node, peer):
                peers.setdefault(peer, {'interface': iface, 'source': 'lsdb.lsp+verified-cfg'})
    active, ports = {}, {}
    for peer, info in peers.items():
        if peer == node:
            continue
        try:
            coordinates(peer)
        except ValueError:
            continue
        iface = info['interface']
        link = by_name.get(iface, {})
        if 'LOWER_UP' not in link.get('flags', []):
            continue
        active[peer] = info
        mac = link.get('address', iface)
        ports[mac] = {'eth': iface, 'linkid': [''.join(sorted((node, peer)))]}
    return {'node': node, 'loopback': address(node), 'sid': address(node, True),
            'end_route': end_route(routes, address(node, True)),
            'peers': active, 'config': {'name': node, 'ports': ports},
            'queue_document': {'queue_poll_s': document.get('queue_poll_s'), 'queues': document.get('queues')},
            'full_lsdb_parsed': parsed is not None, 'full_lsdb_error': error,
            'lsdb_self': document.get('self')}


def scan(prefix=None, names=None, lsdb_path='/lsdb.json'):
    names = names or call(['docker', 'ps', '--format', '{{.Names}}']).splitlines()
    if prefix:
        names = [name for name in names if name.startswith(prefix)]

    def inspect(name):
        try:
            role = inside(name, ['sh', '-c', 'cat /run/leo-te-role 2>/dev/null || true']).strip()
            if role == 'control':
                return {'container': name, 'excluded': 'controller role'}
            interfaces = json.loads(inside(name, ['ip', '-j', '-6', 'addr', 'show']))
            try:
                config = json.loads(inside(name, ['cat', '/cfg.sh']))
            except (ValueError, RuntimeError):
                config = {}
            document = json.loads(inside(name, ['cat', lsdb_path]))
            routes = json.loads(inside(name, ['ip', '-j', '-6', 'route', 'show', 'table', 'all']))
            data = normalize(config, document, interfaces, routes)
            data.update(container=name, lsdb_path=lsdb_path)
            data['container_id'] = json.loads(call(['docker', 'inspect', name]))[0]['Id']
            if not data['peers']:
                data['excluded'] = 'no live explicit LSDB neighbor/interface pair'
            elif not data['end_route']:
                data['excluded'] = 'kernel has no matching seg6local End route'
            return data
        except Exception as exc:
            return {'container': name, 'excluded': str(exc)}

    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(inspect, names))
    groups = {}
    for row in rows:
        if not row.get('excluded'):
            groups.setdefault(row['node'], []).append(row)
    nodes, ambiguous = {}, {}
    for node, group in groups.items():
        if len(group) == 1:
            nodes[node] = group[0]
        else:
            ambiguous[node] = [row['container'] for row in group]
    graph = observed_graph(nodes)
    return nodes, graph, {'candidates': rows, 'ambiguous': ambiguous,
                          'usable_nodes': len(nodes), 'observed_directed_edges': graph.number_of_edges()}


def observed_graph(nodes):
    graph = nx.DiGraph()
    for node, info in nodes.items():
        graph.add_node(node, loopback=info['loopback'], end_sid=info['sid'], srv6_ready=bool(info['end_route']))
    for src, info in nodes.items():
        for dst, port in info['peers'].items():
            if dst != src and dst in nodes and src in nodes[dst]['peers']:
                graph.add_edge(src, dst, interface=port['interface'], topology_source=port['source'])
    return graph


def select_groups(graph, count, explicit=None):
    if explicit:
        if len(explicit) not in (2, 3) or len(set(explicit)) != len(explicit):
            raise ValueError('Select two or three different logical nodes')
        part = graph.subgraph(explicit)
        if len(part) != len(explicit) or not nx.is_strongly_connected(part):
            raise ValueError('Selected nodes have no connected reciprocal observed subgraph; use automatic pick')
        return [explicit]
    groups = []
    if count == 3:
        for a in sorted(graph):
            for b in sorted(graph.successors(a)):
                for c in sorted(graph.successors(b)):
                    if b != a and c not in (a,b):
                        groups.append([a, b, c])
    # A real existing pair is a useful fallback when no working triple exists.
    groups += [[a, b] for a, b in sorted(graph.edges) if a < b]
    return groups


def ping_rows(nodes, count=2, size=56):
    evidence = []
    for src in nodes:
        for dst in nodes:
            if src == dst:
                continue
            result = call(['docker', 'exec', nodes[src]['container'], 'ping', '-6', '-n',
                           '-c', str(count), '-W', '2', '-s', str(size), nodes[dst]['loopback']],
                          check=False, timeout=count*3+5)
            evidence.append({'src': src, 'dst': dst, 'container': nodes[src]['container'],
                             'returncode': result.returncode, 'output': result.stdout+result.stderr})
    return evidence

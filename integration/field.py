"""Self-contained field controller: existing nodes only, step-by-step operation."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid

import networkx as nx
import yaml

from integration.controller import (atomic_json, command, docker_read, live_document,
                                    validate_demands, topology_summary)
from integration.lsdb import coordinates, parse_lsdb
from integration.native_queues import collect_native_queues, decode_tail, validate_native_queues
from integration.benchmark import (destination, state_fingerprint, summarize_phase,
                                   comparison, withdraw, assert_routes)

ROOT = Path(os.environ.get('LEO_OUTPUT', '/output'))
APP_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = APP_ROOT / 'integration/fixtures/demands.weixing.example.json'


def read(path, default=None):
    return json.loads(Path(path).read_text()) if Path(path).exists() else default


@contextmanager
def operation():
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT / '.operation.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another operation is running; wait for it to finish')
        yield


def canonical_config(config, document, links):
    """Read identity/ports from live state, never infer a grid or interface order."""
    loopbacks = set()
    for link in links:
        if link['ifname'] != 'lo':
            continue
        for address in link.get('addr_info', []):
            try:
                ip=ipaddress.IPv6Address(address['local'])
                groups=ip.exploded.split(':')
                if address.get('prefixlen') == 128 and groups[:2] == ['fc00','0000']:
                    node=f'L{int(groups[2],16):02d}{int(groups[-1],16):02d}'
                    coordinates(node);loopbacks.add(node)
            except (KeyError,ValueError):
                continue
    if len(loopbacks)>1:
        raise ValueError('Multiple task-1 loopback identities in one container')
    node=next(iter(loopbacks)) if loopbacks else document.get('self') or config.get('name')
    coordinates(node)
    if config.get('name') == node and config.get('ports'):
        return config, 'cfg+live-loopback' if loopbacks else 'cfg'
    # A copied image can retain another node's cfg. Only use observed native
    # pairs/neighbors and actual MACs; never synthesize four grid neighbors.
    by_name={link['ifname']:link for link in links}
    ports={}
    if document.get('self') == node:
        for pair, values in document.get('link_addr_pair',{}).items():
            names=pair.split('->')
            iface=values.get('iface')
            if len(names)==2 and names[0]==node and iface in by_name:
                coordinates(names[1]);mac=by_name[iface].get('address',iface)
                ports[mac]={'eth':iface,'linkid':[''.join(sorted(names))]}
        if not ports:
            try:
                graph,_=parse_lsdb(document)
                for src,dst,data in graph.out_edges(node,data=True):
                    iface=data.get('local_iface')
                    if iface in by_name:
                        mac=by_name[iface].get('address',iface)
                        ports[mac]={'eth':iface,'linkid':[''.join(sorted((src,dst)))]}
            except (ValueError,KeyError,nx.NetworkXError):
                pass
    if not ports:
        raise ValueError(f'{node}: stale/missing cfg and no explicit live neighbor/interface mapping')
    return {'name':node,'ports':ports}, 'native-lsdb-pairs+actual-interfaces'


def candidates(selected=None):
    cfg = read(ROOT / 'selection.json', {})
    names = selected or cfg.get('containers') or command(['docker', 'ps', '--format', '{{.Names}}']).splitlines()
    if cfg.get('prefix') and selected is None:
        names = [name for name in names if name.startswith(cfg['prefix'])]

    def inspect(container):
        try:
            try:
                if command(['docker', 'exec', container, 'cat', '/run/leo-te-role']).strip() == 'control':
                    return None
            except RuntimeError:
                pass
            try:config = docker_read(container, '/cfg.sh')
            except (ValueError,RuntimeError):config = {}
            try:document = docker_read(container, '/lsdb.json')
            except (ValueError,RuntimeError):document = {}
            links = json.loads(command(['docker', 'exec', container, 'ip', '-j', '-6', 'addr', 'show']))
            config, source = canonical_config(config,document,links)
            by_name = {link['ifname']: link for link in links}
            up, matched = 0, 0
            for mac, port in config['ports'].items():
                link = by_name.get(port['eth'], {})
                if port.get('linkid') and 'LOWER_UP' in link.get('flags', []):
                    up += 1
                    matched += link.get('address', '').lower() == mac.lower()
            return {'container': container, 'config': config, 'business_up': up,
                    'mac_matched_up': matched, 'identity_source':source}
        except (RuntimeError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        found = [item for item in pool.map(inspect, names) if item]
    grouped = {}
    for item in found:
        grouped.setdefault(item['config']['name'], []).append(item)
    result, ambiguous, ignored = {}, {}, []
    for node, group in grouped.items():
        healthy = [item for item in group if item['business_up'] > 0]
        if len(healthy) == 1:
            result[node] = healthy[0]
            ignored += [item['container'] for item in group if item is not healthy[0]]
        elif len(healthy) > 1:
            # A unique cfg/MAC-consistent active node can exclude a stale clone.
            consistent = [item for item in healthy if item['mac_matched_up'] == item['business_up']]
            if len(consistent) == 1:
                result[node] = consistent[0]
                ignored += [item['container'] for item in group if item is not consistent[0]]
            else:
                ambiguous[node] = group
        else:
            ignored += [item['container'] for item in group]
    atomic_json(ROOT / 'discovery.json', {'nodes': result, 'ambiguous': ambiguous, 'ignored': ignored})
    if ambiguous:
        summary = {node: [item['container'] for item in group] for node, group in ambiguous.items()}
        raise ValueError(f'Multiple active networks claim the same identities: {summary}. '
                         'Run leo-te select --prefix <actual-container-prefix> or '
                         'leo-te select --containers <actual names>. See /output/discovery.json')
    if len(result) < 2:
        raise ValueError(f'Only {len(result)} active nodes identified from /cfg.sh and business interfaces; '
                         'need at least two, not exactly 66. See /output/discovery.json')
    atomic_json(ROOT / 'nodes.json', result)
    return result


def state(nodes=None, metrics=True):
    nodes = candidates() if nodes is None else nodes
    reference = nodes[sorted(nodes)[0]]['container']
    document = live_document(reference)
    full, warnings = parse_lsdb(document)
    graph = full.subgraph([n for n in nodes if n in full and full.nodes[n]['srv6_ready']]).copy()
    components = sorted(nx.strongly_connected_components(graph), key=lambda c: (-len(c), sorted(c)))
    if not components or len(components[0]) < 2:
        raise ValueError('No mutually reachable pair with live advertised SRv6 End SIDs; check owner auto/IS-IS')
    graph = graph.subgraph(components[0]).copy()
    usable = {node: nodes[node] for node in graph}
    summary = topology_summary(document, graph, warnings)
    summary.update(discovered_nodes=len(nodes), advertised_nodes=len(full),
                   topology_readback=document.get('topology_readback'),
                   excluded_nodes=sorted(set(nodes)-set(graph)))
    atomic_json(ROOT / 'topology.json', summary)
    queues = []
    if metrics:
        queues = collect_native_queues(graph, usable, command)
        missing = [q for q in queues if any(q.get(k) is None for k in ('queue_bytes', 'capacity_mbps', 'delay_ms'))]
        if missing:
            raise ValueError(f'{len(missing)} directions have missing qdisc metrics, e.g. {missing[:2]}. '
                             'Run leo-te queues prepare --rate 16mbit --delay 10ms, then leo-te check')
        atomic_json(ROOT / 'queues.json', {'read_at': time.time(), 'links': queues})
    return usable, graph, queues


def demands_for(graph):
    path = ROOT / 'demands.json'
    demands = read(path)
    if demands is None:
        demands = [f for f in read(EXAMPLE) if f['src'] in graph and f['dst'] in graph]
        reason = 'available subset of the declared 6-flow example'
        if not demands:
            pairs = [(len(nx.shortest_path(graph, s, d)), s, d) for s in graph for d in graph if s != d]
            _, src, dst = max(pairs)
            demands = [{'id': 'available_demo', 'src': src, 'dst': dst,
                        'class': 'class_2_embb', 'demand': 1000}]
            reason = 'one reachable available-node demo at 1 Mbps; no promise of congestion/improvement'
        atomic_json(path, demands)
        atomic_json(ROOT / 'demands-origin.json', {'reason': reason, 'unit': 'kbps'})
        print('DEMO demands:', reason, flush=True)
    classes = yaml.safe_load((APP_ROOT / 'config/controller.yaml').read_text())['lyapunov_engine']['traffic_classes']
    validate_demands(demands, graph, classes)
    return demands


def check(ping=True):
    print(command(['docker', 'version', '--format', 'Client={{.Client.Version}} API={{.Client.APIVersion}} Server={{.Server.Version}} API={{.Server.APIVersion}}']).strip())
    nodes, graph, queues = state()
    demands = demands_for(graph)
    if ping:
        for flow in demands:
            command(['docker', 'exec', nodes[flow['src']]['container'], 'ping', '-6', '-c', '1',
                     '-W', '3', destination(flow['dst'])])
    print(f'READY: discovered/usable={len(read(ROOT / "nodes.json"))}/{len(nodes)}, '
          f'directed_links={graph.number_of_edges()}, fresh_queues={len(queues)}, flows={len(demands)}', flush=True)
    print(json.dumps(demands, ensure_ascii=False, indent=2), flush=True)
    return nodes, graph, queues, demands


def queue_prepare(args):
    if not re.fullmatch(r'[0-9.]+[kKmMgG]?bit', args.rate) or not re.fullmatch(r'[0-9.]+(us|ms|s)', args.delay):
        raise ValueError('Use an explicit rate/delay, e.g. --rate 16mbit --delay 10ms')
    nodes = candidates()
    actions=[]
    wrapper = '#!/bin/bash\nexport QPROBE_CFG=/leo-te-queue-cfg.json\nexec /leo-te-queue-probe "$@"\n'
    for node, info in sorted(nodes.items()):
        container = info['container']
        for port in info['config']['ports'].values():
            iface=port['eth']
            if not port.get('linkid') or iface in ('lo','eth0'):continue
            q=json.loads(command(['docker','exec',container,'tc','-j','qdisc','show','dev',iface]))
            if any(row.get('root') and row['kind']!='noqueue' for row in q):
                actions.append({'node':node,'interface':iface,'action':'preserved-existing-root'})
            else:
                command(['docker','exec',container,'tc','qdisc','add','dev',iface,'root','handle','1:',
                         'tbf','rate',args.rate,'burst','32kb','latency','100ms'])
                command(['docker','exec',container,'tc','qdisc','add','dev',iface,'parent','1:1','handle','10:',
                         'netem','delay',args.delay,'limit','1000'])
                actions.append({'node':node,'interface':iface,'action':'created-on-empty-root','rate':args.rate,'delay':args.delay})
            atomic_json(ROOT/'qdisc-actions.json',actions)
        command(['docker','exec','-i',container,'sh','-c','cat > /leo-te-queue-cfg.json'],stdin=json.dumps(info['config']))
        command(['docker', 'cp', '/app/bin/leo-te-queue-probe', container + ':/leo-te-queue-probe.tmp'])
        command(['docker', 'exec', '-i', container, 'sh', '-c',
                 'set -eu; chmod 755 /leo-te-queue-probe.tmp; mv /leo-te-queue-probe.tmp /leo-te-queue-probe; '
                 'if [ -f /queue-probe.sh ] && [ ! -e /queue-probe.sh.before-leo-te-field ]; then '
                 'cp -p /queue-probe.sh /queue-probe.sh.before-leo-te-field; fi; '
                 'cat > /queue-probe.sh.leo-tmp; chmod 755 /queue-probe.sh.leo-tmp; '
                 'mv /queue-probe.sh.leo-tmp /queue-probe.sh'], stdin=wrapper)
        print(f'queues prepared: {node} ({container}); native auto remains LSDB writer', flush=True)
    print('Waiting for native auto queue-tick exports (up to 45s)...', flush=True)
    deadline = time.time()+45
    while True:
        try:
            usable, graph, records = state(nodes)
            if any('leo-te-backup-tc-json' not in q['source'] for q in records):
                raise ValueError('Native auto has not yet published the backup collector samples')
            print(f'QUEUES READY: {len(usable)} usable nodes, {len(records)} native LSDB directions', flush=True)
            return
        except Exception as exc:
            if time.time() >= deadline:
                raise ValueError(f'Queues not ready: {exc}. Ask owner to start auto66-lite.sh on every node; '
                                 'backup did not start a competing LSDB writer') from exc
            print('Waiting:', exc, flush=True)
            time.sleep(3)


def queue_restore():
    for node, info in sorted(candidates().items()):
        container = info['container']
        wrapper = '#!/bin/bash\nexport QPROBE_CFG=/leo-te-queue-cfg.json\nexec /leo-te-queue-probe "$@"\n'
        current = command(['docker','exec',container,'cat','/queue-probe.sh'])
        if current != wrapper:
            raise ValueError(f'{container}: collector changed externally; refusing to overwrite it')
        command(['docker','exec',container,'sh','-c',
                 'set -eu; test -f /queue-probe.sh.before-leo-te-field; '
                 'cp -p /queue-probe.sh.before-leo-te-field /queue-probe.sh.leo-tmp; '
                 'mv /queue-probe.sh.leo-tmp /queue-probe.sh'])
        print('Restored original collector:',node,container,flush=True)
    print('Root qdiscs retained; only the backed-up collector was restored',flush=True)


def session(active=True):
    current = read(ROOT / 'session.json')
    if not current:
        raise ValueError('No session; first run leo-te baseline')
    if active and (current.get('stage') not in ('starting', 'baseline_measured', 'tuned', 'te_measured') or
                   time.time() >= current['start_at'] + current['max_seconds']):
        raise ValueError('Session inactive/expired; run leo-te abort, then start a new baseline')
    return current


def save_session(current):
    atomic_json(ROOT / 'session.json', current)
    atomic_json(Path(current['output']) / 'session.json', current)


def evidence(current, name, value):
    atomic_json(Path(current['output']) / name, value)


def probe_records(current):
    def fetch(record):
        return record, docker_read(record['container'], record['output'])
    with ThreadPoolExecutor(max_workers=8) as pool:
        pairs = list(pool.map(fetch, current['records']))
    clients, servers = {}, {}
    for record, result in pairs:
        if abs(result['start_at'] - current['start_at']) > .01:
            raise ValueError('Traffic probe belongs to another epoch')
        (clients if record['role'] == 'client' else servers)[record['flow_id']] = result
    return clients, servers


def assert_traffic(current):
    clients, servers = probe_records(current)
    now = time.time()
    for result in list(clients.values()) + list(servers.values()):
        if result['finished'] or not (-2 <= now-result['sampled_at'] <= 6):
            raise ValueError('A continuous traffic probe stopped or stopped updating; abort and re-run baseline')
    return clients, servers


def window(current, mode, offset, duration):
    end = current['start_at'] + offset + duration
    if end + 3 >= current['start_at'] + current['max_seconds']:
        raise ValueError('Not enough session lifetime left; abort and start a new baseline')
    routes = assert_routes(current['demands'], current['nodes'], mode == 'te')
    evidence(current, mode + '/routes-before.json', routes)
    midpoint = current['start_at'] + offset + duration/2
    for target in (midpoint, end+1.3):
        while time.time() < target:
            time.sleep(min(1, target-time.time()))
        _, graph, queues = state(current['nodes'])
        if state_fingerprint(graph, queues) != current['fingerprint']:
            raise ValueError('Topology, capacity, delay or End SID changed during measurement')
        evidence(current, mode + ('/queues-mid.json' if target == midpoint else '/queues-end.json'), queues)
    clients, servers = assert_traffic(current)
    for result in list(clients.values()) + list(servers.values()):
        if result['completed_seconds'] < offset+duration or result['finished']:
            raise ValueError('Probe has not covered the complete window, or has stopped; see diagnose')
    summary = summarize_phase(clients, servers, offset, duration, current['demands'])
    evidence(current, mode + '/probe-clients.json', clients)
    evidence(current, mode + '/probe-servers.json', servers)
    evidence(current, mode + '/routes-after.json', assert_routes(current['demands'], current['nodes'], mode == 'te'))
    evidence(current, mode + '/summary.json', dict(summary, window_start=offset, duration=duration))
    return summary


def stop_probes(current):
    remote = current['remote']
    if not re.fullmatch(r'/tmp/leo-te-field-[0-9a-f]{32}', remote):
        raise ValueError('Refusing cleanup of an invalid session path')
    script = ('for f in "$1"/*.pid; do [ -f "$f" ] || continue; read p < "$f"; '
              'case "$p" in *[!0-9]*|"") continue;; esac; '
              'if [ -r "/proc/$p/cmdline" ] && grep -Fq -- "$1/probe" "/proc/$p/cmdline"; '
              'then kill -TERM "$p" 2>/dev/null || true; fi; done')
    for container in sorted({r['container'] for r in current.get('records', [])}):
        subprocess.run(['docker', 'exec', container, 'sh', '-c', script, 'leo-stop', remote], capture_output=True)
    time.sleep(.3)
    final=Path(current['output'])/'probe-final'
    final.mkdir(parents=True,exist_ok=True)
    for record in current.get('records',[]):
        for suffix in ('json','log'):
            filename=record['output'] if suffix=='json' else record['output'][:-5]+'.log'
            run=subprocess.run(['docker','exec',record['container'],'cat',filename],capture_output=True,text=True)
            if run.returncode==0:
                (final/(record['flow_id']+'-'+record['role']+'.'+suffix)).write_text(run.stdout)
    for container in sorted({r['container'] for r in current.get('records', [])}):
        subprocess.run(['docker', 'exec', container, 'rm', '-rf', '--', remote], capture_output=True)


def abort(current=None):
    current = session(False) if current is None else current
    stop_probes(current)
    withdraw(current['demands'], current['nodes'], 90)
    current['stage'] = 'stopped'
    save_session(current)
    print('Stopped this session probes and withdrew only its own policies', flush=True)


def baseline(args):
    old = read(ROOT / 'session.json')
    if old and old.get('stage') not in ('stopped', 'complete', 'failed'):
        raise ValueError('A session already exists; use leo-te abort before starting another baseline')
    nodes, graph, queues, demands = check()
    withdraw(demands, nodes, 90)
    assert_routes(demands, nodes, False)
    output = ROOT / 'runs' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8])
    output.mkdir(parents=True)
    start = time.time() + max(60, len(demands)*3)
    remote = '/tmp/leo-te-field-' + uuid.uuid4().hex
    current = {'stage': 'starting', 'start_at': start, 'max_seconds': args.max_seconds,
               'remote': remote, 'output': str(output), 'demands': demands, 'nodes': nodes,
               'fingerprint': state_fingerprint(graph, queues), 'records': [], 'duration': args.duration}
    save_session(current)
    evidence(current, 'demands.json', demands)
    involved = {nodes[f[key]]['container'] for f in demands for key in ('src','dst')}
    for container in sorted(involved):
        command(['docker', 'exec', container, 'mkdir', '-p', remote])
        command(['docker', 'cp', '/app/bin/field-udp6-probe', container+':'+remote+'/probe'])
    # Start the common epoch only after binary distribution has completed.
    start = time.time()+max(20, len(demands)*3)
    current['start_at'] = start
    save_session(current)
    for index, flow in enumerate(demands):
        for role, node in [('server', flow['dst']), ('client', flow['src'])]:
            filename = remote + '/' + flow['id'] + '-' + role
            record = {'container': nodes[node]['container'], 'role': role, 'flow_id': flow['id'],
                      'output': filename+'.json'}
            current['records'].append(record)
            save_session(current)  # Preserve ledger before any launch can fail.
            invocation = [remote+'/probe', role, '--port', str(args.port_base+index),
                          '--duration', str(args.max_seconds), '--start-at', str(start),
                          '--output', record['output']]
            if role == 'client':
                invocation += ['--dst', destination(flow['dst']), '--source-port', str(args.port_base+1000+index),
                               '--rate-kbps', str(flow['demand']), '--payload', '1000']
            script = 'p=$1; l=$2; shift 2; echo $$ > "$p"; exec "$@" > "$l" 2>&1'
            command(['docker', 'exec', '-d', record['container'], 'sh', '-c', script, 'leo-probe',
                     filename+'.pid', filename+'.log'] + invocation)
    if time.time() >= start:
        raise ValueError('Probe launch missed common epoch; refuse measurement')
    print(f'BASELINE: {len(demands)} continuous flows, warmup={args.warmup}s, measure={args.duration}s', flush=True)
    current['baseline'] = window(current, 'baseline', args.warmup, args.duration)
    current['stage'] = 'baseline_measured'
    save_session(current)
    print(f'BASELINE measured: {current["baseline"]["received_mbps"]:.3f} Mbps; traffic STILL RUNNING. Next: leo-te tune', flush=True)


def algorithm(nodes, output, apply=True):
    node_map=Path(output)/'node-map.json'
    atomic_json(node_map,nodes)
    invocation = [sys.executable, '-m', 'integration.controller', '--demands', str(ROOT/'demands.json'),
                  '--output', str(output), '--once', '--ack-timeout', '90', '--restrict-topology',
                  '--node-map',str(node_map)]
    if apply:
        invocation += ['--apply']
    output.mkdir(parents=True, exist_ok=True)
    with (output/'controller.log').open('w') as log:
        process = subprocess.Popen(invocation, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in process.stdout:
            print(line, end='', flush=True);log.write(line);log.flush()
        if process.wait():
            raise RuntimeError(f'Algorithm/apply failed; see {output}/controller.log')


def verify_plan_routes(nodes, output):
    plan=read(Path(output)/'plan.json')
    verified={}
    for policy in plan['policies']:
        rowcol=[coordinates(n) for n in policy['via'] if n!=policy['src']]
        expected=[ipaddress.IPv6Address(f'fc00:{r:x}:{c:x}::') for r,c in rowcol]
        r,c=coordinates(policy['dst']);dst=ipaddress.IPv6Address(f'fc00:{r:x}:{c:x}::')
        if dst not in expected:expected.append(dst)
        route=command(['docker','exec',nodes[policy['src']]['container'],'ip','-6','route','get',destination(policy['dst'])])
        match=re.search(r'encap seg6 mode encap segs\s+\d+\s*\[([^]]+)\]',route)
        actual=[ipaddress.IPv6Address(s) for s in match.group(1).split()] if match else []
        if actual!=expected:
            atomic_json(Path(output)/'route-mismatch.json',{'policy':policy,'expected':[str(x) for x in expected],'route':route})
            raise ValueError(f'{policy["biz_id"]}: kernel SID list differs from computed plan; see route-mismatch.json')
        verified[policy['biz_id']]={'container':nodes[policy['src']]['container'],
                                  'expected_sids':[str(x) for x in expected],'kernel_route':route,'matched':True}
    atomic_json(Path(output)/'kernel-plan-verification.json',verified)
    return verified


def tune():
    current = session()
    if current['stage'] != 'baseline_measured':
        raise ValueError('tune requires a measured baseline with traffic still running')
    assert_traffic(current)
    _, graph, queues = state(current['nodes'])
    if state_fingerprint(graph, queues) != current['fingerprint']:
        raise ValueError('Topology or link parameters changed since baseline')
    evidence(current, 'controller/queues-before-algorithm.json', queues)
    # Same sender/receiver processes stay alive throughout algorithm and ACK.
    algorithm(current['nodes'], Path(current['output'])/'controller')
    verify_plan_routes(current['nodes'],Path(current['output'])/'controller')
    assert_traffic(current)
    evidence(current, 'controller/routes-after-apply.json', assert_routes(current['demands'], current['nodes'], True))
    current['stage'] = 'tuned';current['tuned_at'] = time.time();save_session(current)
    print('TUNED: native ACK=ok/seg6/pass and kernel routes verified; traffic STILL RUNNING. Next: leo-te te', flush=True)


def measure_te(args):
    current = session()
    if current['stage'] != 'tuned':
        raise ValueError('First run leo-te tune and receive native ACKs')
    assert_traffic(current)
    offset = math.ceil(time.time()-current['start_at']) + args.settle
    print(f'TE: settle={args.settle}s, measurement={current["duration"]}s; same continuous offered load', flush=True)
    current['te'] = window(current, 'te', offset, current['duration'])
    current['stage'] = 'te_measured';save_session(current)
    print(f'TE measured: {current["te"]["received_mbps"]:.3f} Mbps. Next: leo-te compare', flush=True)


def compare():
    current = session()
    if current['stage'] != 'te_measured':
        raise ValueError('Need both measured windows; first complete baseline, tune and te')
    result = comparison(current['baseline'], current['te'])
    result.update(status='valid' if result['valid'] else 'invalid_measurement',
                  measurement='same IPv6 UDP streams; receiver application payload Mbps',
                  output=current['output'], demands=current['demands'])
    evidence(current, 'result.json', result)
    change = result['improvement_percent']
    text = (f'# 现场原网络吞吐量对比\n\n状态：{result["status"]}\n\n'
            f'Baseline：{result["baseline"]["received_mbps"]:.3f} Mbps\n\n'
            f'TE：{result["te"]["received_mbps"]:.3f} Mbps\n\n'
            f'变化：{change:.2f}%\n\n' if change is not None else '# 无有效Baseline，不能计算提升\n')
    text += '同一批流量在算法计算和SRv6下发期间持续运行；原始队列、发送/接收计数、路径和回执随本目录保存。零/负变化如实记录。\n'
    (Path(current['output'])/'README.md').write_text(text)
    print(text, flush=True)
    try:abort(current)
    except Exception as exc:
        result['cleanup_error']=str(exc);evidence(current,'result.json',result)
        raise
    current['stage'] = 'complete';save_session(current)
    atomic_json(ROOT/'latest-result.json',{'kind':'throughput','output':current['output'],'completed_at':time.time()})
    print('Result:', current['output'], flush=True)
    if not result['valid']:
        raise ValueError('Measurement invalid; inspect CV and actual offered-load errors; do not claim improvement')


def smoke(apply):
    nodes, graph, queues, demands = check()
    output = ROOT/'smoke'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
    if apply:withdraw(demands,nodes,90)
    algorithm(nodes, output, apply)
    if apply:
        verify_plan_routes(nodes,output)
        evidence_data = assert_routes(demands, nodes, True)
        atomic_json(output/'kernel-routes.json', evidence_data)
        atomic_json(ROOT/'latest-result.json',{'kind':'smoke','output':str(output),'completed_at':time.time()})
        print('SMOKE PASS: algorithm ran, native ACKs accepted, kernel SID lists match computed plan', flush=True)
        print('Policies retained for inspection. To restore native routes: leo-te withdraw', flush=True)
    else:
        print('PLAN READY (not applied):', output, flush=True)


def diagnose():
    output = ROOT/'diagnostics'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
    output.mkdir(parents=True)
    for name, invocation in [('docker-version', ['docker','version']), ('docker-ps',['docker','ps','--no-trunc'])]:
        run = subprocess.run(invocation, capture_output=True, text=True)
        (output/(name+'.txt')).write_text(run.stdout+run.stderr)
    names = command(['docker','ps','--format','{{.Names}}']).splitlines()
    for index, container in enumerate(names):
        try:
            config = docker_read(container,'/cfg.sh');coordinates(config['name'])
        except Exception:
            continue
        dest = output/f'{index:03d}-{config["name"]}';dest.mkdir()
        atomic_json(dest/'identity.json', {'container':container,'config':config})
        for label, invocation in [('lsdb',['cat','/lsdb.json']),
                                  ('frr-json',['vtysh','-c','show isis database detail json']),
                                  ('interfaces',['ip','-j','link','show']),
                                  ('qdisc',['tc','-s','qdisc','show']),
                                  ('qdisc-json',['tc','-j','-s','qdisc','show']),
                                  ('auto-processes',['ps','-ef']),
                                  ('auto-log',['tail','-n','80','/auto-debug.log']),
                                  ('path-status',['cat','/path-tuner.status'])]:
            run = subprocess.run(['docker','exec',container]+invocation,capture_output=True,text=True,timeout=30)
            (dest/(label+'.txt')).write_text(run.stdout+run.stderr)
    print('Diagnostics saved:', output, flush=True)


def results():
    latest=read(ROOT/'latest-result.json')
    if not latest:raise ValueError('No completed evidence yet; first complete smoke or compare')
    error=read(ROOT/'last-error.json',{})
    if error.get('time',0)>latest['completed_at']:
        print('A newer operation failed; these are the previous completed results. Latest error:',error,flush=True)
    path=Path(latest['output'])
    print('Evidence directory:',path,flush=True)
    if latest['kind']=='smoke':
        ack=read(path/'acceptance.json')
        verified=read(path/'kernel-plan-verification.json')
        print('Native acceptance:',ack['status'],flush=True)
        for biz,item in verified.items():
            status=ack['native_status'][biz]
            print(biz,item['container'],status['status'],status['route'],status['ping'],
                  'SIDs='+','.join(item['expected_sids']),'kernel_matches='+str(item['matched']),flush=True)
    else:print((path/'README.md').read_text(),flush=True)


def serve():
    ROOT.mkdir(parents=True, exist_ok=True)
    Path('/run/leo-te-role').write_text('control\n')
    print('LEO TE controller ready. No topology created. Commands: leo-te help, queues, check, smoke, baseline, tune, te, compare.', flush=True)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    while True:
        time.sleep(60)


def main():
    parser = argparse.ArgumentParser(description='现场控制器：只操作既有网络；不要求容器命名或恰好66节点')
    sub = parser.add_subparsers(dest='action')
    for name in ('serve','help','nodes','check','tune','compare','status','results','abort','withdraw','diagnose','plan','smoke'):
        sub.add_parser(name)
    select = sub.add_parser('select')
    group=select.add_mutually_exclusive_group(required=True)
    group.add_argument('--containers',nargs='+');group.add_argument('--prefix');group.add_argument('--auto',action='store_true')
    demand=sub.add_parser('demands')
    demand_group=demand.add_mutually_exclusive_group()
    demand_group.add_argument('--stdin',action='store_true');demand_group.add_argument('--reset',action='store_true')
    queue=sub.add_parser('queues');queue.add_argument('mode',choices=['prepare','status','restore'],default='status',nargs='?')
    queue.add_argument('--rate',default='16mbit');queue.add_argument('--delay',default='10ms')
    for name in ('baseline','run'):
        p=sub.add_parser(name);p.add_argument('--warmup',type=int,default=20);p.add_argument('--duration',type=int,default=30)
        p.add_argument('--max-seconds',type=int,default=1200);p.add_argument('--port-base',type=int,default=31000)
        if name=='run':p.add_argument('--cycles',type=int,default=1);p.add_argument('--settle',type=int,default=20)
    te=sub.add_parser('te');te.add_argument('--settle',type=int,default=20)
    args=parser.parse_args()
    if args.action in (None,'help'):
        parser.print_help();return
    if args.action=='serve':serve();return
    ROOT.mkdir(parents=True,exist_ok=True)
    with operation():
        try:
            if args.action=='select':
                old=read(ROOT/'session.json',{})
                if old.get('stage') in ('starting','baseline_measured','tuned','te_measured'):
                    raise ValueError('Abort active session before changing selection')
                atomic_json(ROOT/'selection.json',{'containers':args.containers,'prefix':args.prefix})
                print('Selection saved. Run leo-te check');return
            if args.action=='demands':
                if args.stdin or args.reset:
                    if read(ROOT/'session.json',{}).get('stage') in ('starting','baseline_measured','tuned','te_measured'):
                        raise ValueError('Abort active session before changing demands')
                if args.reset:
                    (ROOT/'demands.json').unlink(missing_ok=True)
                    (ROOT/'demands-origin.json').unlink(missing_ok=True)
                    print('Demo demands reset. Run leo-te check to choose only available endpoints. Existing policies were not deleted.');return
                if args.stdin:
                    raw=json.load(sys.stdin);_,g,_=state(metrics=False)
                    classes=yaml.safe_load((APP_ROOT / 'config/controller.yaml').read_text())['lyapunov_engine']['traffic_classes']
                    validate_demands(raw,g,classes);atomic_json(ROOT/'demands.json',raw)
                print(json.dumps(read(ROOT/'demands.json'),ensure_ascii=False,indent=2));return
            if args.action=='status':
                current=read(ROOT/'session.json',{'stage':'not_started'})
                print(json.dumps({'stage':current['stage'],'flows':len(current.get('demands',[])),
                      'result_dir':current.get('output'),
                      'baseline_mbps':current.get('baseline',{}).get('received_mbps'),
                      'te_mbps':current.get('te',{}).get('received_mbps'),
                      'error':current.get('error')},ensure_ascii=False,indent=2));return
            if args.action=='results':results();return
            if args.action=='diagnose':diagnose();return
            if args.action=='nodes':
                try:candidates()
                except ValueError as exc:print(exc,flush=True)
                discovery=read(ROOT/'discovery.json',{})
                for node,info in sorted(discovery.get('nodes',{}).items()):
                    print(node,info['container'],'business_up='+str(info['business_up']),flush=True)
                for node,group in sorted(discovery.get('ambiguous',{}).items()):
                    for info in group:print('AMBIGUOUS',node,info['container'],'business_up='+str(info['business_up']),flush=True)
                print('IGNORED:',discovery.get('ignored',[]),flush=True);return
            if args.action=='queues':
                if args.mode in ('prepare','restore'):
                    if read(ROOT/'session.json',{}).get('stage') in ('starting','baseline_measured','tuned','te_measured'):
                        raise ValueError('Abort active measurement before modifying qdisc/collector')
                    if args.mode=='prepare':queue_prepare(args)
                    else:queue_restore()
                else:
                    nodes=candidates()
                    for node,info in sorted(nodes.items()):
                        try:
                            tail=command(['docker','exec',info['container'],'tail','-c','16384','/lsdb.json'])
                            q=validate_native_queues(decode_tail(tail),node,info['config'])
                            print(node,info['container'],'measured',sum(r['queue_bytes'] is not None for r in q),'/',len(q),flush=True)
                        except Exception as exc:print(node,info['container'],'NOT READY:',exc,flush=True)
                return
            if args.action=='check':check();return
            if args.action=='abort':abort();return
            if args.action=='withdraw':
                current=read(ROOT/'session.json',{})
                nodes=current.get('nodes') or candidates()
                flows=read(ROOT/'demands.json')
                if not flows:raise ValueError('No demands recorded')
                withdraw(flows,nodes,90);print('Own policies withdrawn');return
            if args.action in ('smoke','plan'):
                if read(ROOT/'session.json',{}).get('stage') in ('starting','baseline_measured','tuned','te_measured'):
                    raise ValueError('Use tune during an active throughput session; smoke/plan are separate checks')
                smoke(args.action=='smoke');return
            if args.action in ('baseline','run'):
                if args.duration<5 or args.warmup<0 or args.max_seconds<120 or args.max_seconds>3600 or args.port_base<1024 or args.port_base>64000:
                    raise ValueError('duration>=5, warmup>=0, max-seconds=120..3600, port-base=1024..64000 required')
                if args.action=='baseline':baseline(args);return
                if args.cycles<1 or args.cycles>10:raise ValueError('cycles=1..10 required')
                for cycle in range(args.cycles):
                    print(f'Cycle {cycle+1}/{args.cycles}',flush=True)
                    baseline(args);tune();measure_te(args);compare()
                return
            if args.action=='tune':tune();return
            if args.action=='compare':compare();return
            if args.action=='te':
                if args.settle<0:raise ValueError('settle must be nonnegative')
                measure_te(args);return
            raise ValueError(f'Unhandled command: {args.action}')
        except BaseException as exc:
            atomic_json(ROOT/'last-error.json',{'time':time.time(),'action':args.action,'error':str(exc)})
            if args.action in ('baseline','tune','te','run'):
                current=read(ROOT/'session.json',{})
                if current.get('stage') in ('starting','baseline_measured','tuned','te_measured'):
                    try:abort(current)
                    except Exception as cleanup:print('Cleanup error:',cleanup,flush=True)
                    current['stage']='failed';current['error']=str(exc);save_session(current)
            raise


if __name__=='__main__':
    try:main()
    except (Exception,KeyboardInterrupt) as exc:
        print(f'ERROR: {exc}\nNext: leo-te diagnose (preserves raw evidence)',file=sys.stderr,flush=True)
        raise SystemExit(1)

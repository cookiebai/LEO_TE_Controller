#!/usr/bin/env python3
"""Local constellation lifecycle. Keep the original WebUI alive throughout measurement."""
import argparse
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
LAB = ROOT.parent
OPEN = LAB / 'OpenSN-Library'
RUNTIME = LAB / 'runtime'
PYTHON = LAB / '.venv/bin/python'
ENV = dict(os.environ, NO_PROXY='127.0.0.1,localhost', no_proxy='127.0.0.1,localhost',
           PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION='python', TMPDIR=str(LAB / 'tmp'))
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def command(args, **kwargs):
    values = [str(a) for a in args]
    result = subprocess.run(values, env=ENV, text=True, capture_output=True,
                            check=False, timeout=kwargs.pop('timeout', 120), **kwargs)
    if result.returncode != 0:
        raise RuntimeError(f'command failed ({result.returncode}): {" ".join(values)}\n'
                           f'{result.stdout}\n{result.stderr}')
    return result.stdout.strip()


def api(path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request('http://127.0.0.1:8080/api/' + path, data=data,
                                 headers={'Content-Type': 'application/json'})
    with HTTP.open(req, timeout=30) as response:
        result = json.load(response)
    if result.get('code') != 0:
        raise RuntimeError(result)
    return result.get('data')


def nodes(all_nodes=False):
    names = command(['docker', 'ps', *(['-a'] if all_nodes else []), '--format', '{{.Names}}']).splitlines()
    return [n for n in names if re.fullmatch(r'(Satellite|GroundStation)_[0-9a-f]{8}', n)]


def wait_for(description, check, seconds=180):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        try:
            if check():
                print(description + ': OK', flush=True)
                return
        except RuntimeError:
            raise
        except Exception as exc:
            last = str(exc)
        time.sleep(2)
    raise RuntimeError(f'{description}: timeout; {last}')


def parse_scale(value):
    match = re.fullmatch(r'([1-9][0-9]*)x([1-9][0-9]*)', value.lower())
    if not match:
        raise argparse.ArgumentTypeError('规模格式应为 12x10、18x20 或 20x25')
    orbits, per_orbit = map(int, match.groups())
    return value.lower(), orbits, per_orbit


def complete_nodes(satellites):
    names = nodes()
    return sum(n.startswith('Satellite_') for n in names) == satellites and sum(n.startswith('GroundStation_') for n in names) == 2


def verify_ownership():
    names = nodes(True)
    if names:
        for item in json.loads(command(['docker', 'inspect', *names])):
            if not any(m.get('Destination') == '/share' and
                       Path(m['Source']).resolve().is_relative_to(LAB.resolve())
                       for m in item['Mounts']):
                raise RuntimeError('发现其他目录的实验节点，拒绝操作: ' + item['Name'])
    for name in ('opensn_node_daemon', 'opensn_etcd'):
        if has_container(name):
            item = json.loads(command(['docker', 'inspect', name]))[0]
            if (item['Config'].get('Labels') or {}).get('opensn.lab') != str(LAB):
                raise RuntimeError('服务没有本目录归属标签，拒绝操作: ' + name)


def redis_client():
    import redis
    return redis.Redis(decode_responses=True, socket_timeout=5)


def has_container(name):
    return name in command(['docker', 'ps', '--format', '{{.Names}}']).splitlines()


def tune_scale_network(satellites):
    if satellites <= 120:
        return
    # Each satellite has roughly four ARP neighbors. Linux's default global
    # neighbor threshold (1024) is exhausted around 250 nodes and manifests as
    # sendmsg ENOBUFS, broken ARP, and fragmented OSPF domains.
    threshold = max(8192, satellites * 8)
    settings = {
        'net.ipv4.neigh.default.gc_thresh1': threshold // 4,
        'net.ipv4.neigh.default.gc_thresh2': threshold // 2,
        'net.ipv4.neigh.default.gc_thresh3': threshold,
        'net.core.netdev_max_backlog': 20000,
        'net.core.netdev_budget': 1200,
        'net.core.netdev_budget_usecs': 8000,
    }
    for key, value in settings.items():
        command(['docker', 'run', '--rm', '--privileged', '--network', 'host',
                 '--entrypoint', 'sysctl', 'realssd/satellite-router', '-w', f'{key}={value}'])
    print(f'大规模网络内核参数已设置：neighbor_gc_thresh3={threshold}', flush=True)


def start_web(scale, read_only=False, work=None):
    if has_container('opensn_node_daemon'):
        wait_for('API', lambda: api('platform/status') is None, 30)
        return
    if not (OPEN / 'daemon/share/static/dist/build/index.html').is_file():
        raise RuntimeError('缺少前端构建。先在 OpenSN-Library 执行 make web-node-daemon（Node >=22）。')
    iface = json.loads(command(['ip', '-j', 'route', 'show', 'default']))[0]['dev']
    work = work or RUNTIME / 'web-recovery'
    work.mkdir(parents=True, exist_ok=True)
    command(['docker', 'run', '-d', '--rm', '--name', 'opensn_node_daemon', '--privileged',
             '--network', 'host', '--pid', 'host', '--label', 'opensn.lab=' + str(LAB),
             '-e', 'INTERFACE=' + iface, '-e', 'ETCD_ADDR=127.0.0.1',
             '-e', 'OPENSN_WEB_ONLY=' + ('1' if read_only else '0'),
             '-e', 'GOMEMLIMIT=512MiB', '-e', 'GOGC=50',
             '-v', f'{LAB}:{LAB}', '-v', '/var/run/docker.sock:/var/run/docker.sock',
             '-w', work, '--entrypoint', OPEN / 'NodeDaemon', 'ubuntu:22.04',
             ROOT / f'config/experiment_daemon_{scale.replace("x", "_")}.json', 'experiment'])
    wait_for('API', lambda: api('platform/status') is None, 30)


def verify_web(expected_nodes):
    with HTTP.open('http://127.0.0.1:8080/', timeout=10) as response:
        html = response.read().decode()
    if '<div id="root">' not in html:
        raise RuntimeError('8080 没有提供 React 前端')
    actual = len(api('instance/'))
    if actual != expected_nodes:
        raise RuntimeError(f'前端 API 节点数为 {actual}，预期 {expected_nodes}')
    print(f'WebUI: http://127.0.0.1:8080/ ({expected_nodes} nodes)', flush=True)


def verify_scale(scale):
    _, orbits, per_orbit = parse_scale(scale)
    satellites = [item for item in api('instance/') if item.get('type') == 'Satellite']
    expected = {(str(i), str(j)) for i in range(orbits) for j in range(per_orbit)}
    actual = {(item.get('extra', {}).get('OrbitIndex'),
               item.get('extra', {}).get('SatelliteIndex')) for item in satellites}
    if len(satellites) != orbits * per_orbit or actual != expected:
        raise RuntimeError(f'当前轨道/卫星索引不是 {scale}；总节点数相同也不能混用拓扑，请显式 --rebuild。')


def rebuild_cleanup():
    """Remove only nodes whose bind mounts belong to this lab and their exact bridges."""
    old = nodes(True)
    if old:
        inspected = json.loads(command(['docker', 'inspect', *old]))
        for item in inspected:
            if not any(m.get('Destination') == '/share' and
                       Path(m['Source']).is_relative_to(LAB) for m in item['Mounts']):
                raise RuntimeError('发现其他目录的 OpenSN 节点，拒绝重建: ' + item['Name'])
    bridges = []
    if has_container('opensn_etcd'):
        import etcd3
        client = etcd3.client(host='127.0.0.1', timeout=5)
        saved = {meta.key.decode(): value.decode() for value, meta in client.get_all()}
        backup = RUNTIME / ('etcd-before-rebuild-' + datetime.now().strftime('%Y%m%d_%H%M%S') + '.json')
        backup.write_text(json.dumps(saved, indent=2))
        ids = {json.loads(value)['link_id'] for key, value in saved.items()
               if re.fullmatch(r'/node_0/links/[0-9a-f]{8}', key)}
        for item in json.loads(command(['ip', '-j', '-d', 'link', 'show'])):
            if item['ifname'] in ids and item.get('linkinfo', {}).get('info_kind') == 'bridge':
                bridges.append(item['ifname'])
    if has_container('opensn_node_daemon'):
        command(['docker', 'stop', '--timeout', '10', 'opensn_node_daemon'])
    if old:
        command(['docker', 'rm', '-f', *old], timeout=max(120, len(old)))
    if bridges:
        # Single helper container, explicit validated bridge names; no glob deletion.
        code = 'import subprocess,sys; [subprocess.run(["ip","link","del",n],check=True) for n in sys.argv[1:]]'
        command(['docker', 'run', '--rm', '--privileged', '--network', 'host',
                 '--entrypoint', 'python3', 'realssd/satellite-router', '-c', code, *bridges],
                timeout=max(120, len(bridges)))
    if has_container('opensn_etcd'):
        command(['docker', 'stop', '--timeout', '10', 'opensn_etcd'])
    print(f'Rebuilt lab resources: removed {len(old)} nodes, {len(bridges)} bridges; results retained.', flush=True)


def prepare(scale, satellites, rebuild=False):
    slug = scale.replace('x', '_')
    topology_path = OPEN / f'example/test_topologies/topology_config_{slug}.json'
    daemon_path = ROOT / f'config/experiment_daemon_{slug}.json'
    if not topology_path.is_file() or not daemon_path.is_file():
        raise RuntimeError(f'缺少 {scale} 配置，请先运行 scripts/generate_scale_configs.py {scale}')
    verify_ownership()
    client = redis_client()
    client.ping()
    tune_scale_network(satellites)
    if rebuild:
        rebuild_cleanup()
    if complete_nodes(satellites) and len(json.loads(client.get('te:demands') or '[]')) == 24:
        start_web(scale, read_only=True)
        verify_web(satellites + 2)
        verify_scale(scale)
        return
    if nodes(True):
        raise RuntimeError('发现未完成的旧部署。查看日志后运行 ./lab.sh prepare --rebuild 重建本目录实验。')
    if has_container('opensn_etcd'):
        raise RuntimeError('已有 etcd 但没有完整数据面，请使用 prepare --rebuild 显式重建。')
    command(['docker', 'run', '-d', '--rm', '--name', 'opensn_etcd',
             '--label', 'opensn.lab=' + str(LAB), '-p', '2379:2379', 'realssd/opensn_etcd'])
    work = RUNTIME / ('deployment-' + datetime.now().strftime('%Y%m%d_%H%M%S'))
    start_web(scale, work=work)
    api('emulation/update', json.loads((OPEN / 'example/test_topologies/emu_config.json').read_text()))
    api('emulation/topology', json.loads(topology_path.read_text()))
    api('emulation/start', {})
    deploy_timeout = max(300, satellites * 3)
    wait_for(f'{satellites} Satellite + 2 GroundStation', lambda: complete_nodes(satellites), deploy_timeout)
    for pattern in ('topo:link:*', 'telemetry:queue:*', 'te:policy:*'):
        keys = list(client.scan_iter(pattern))
        if keys:
            client.delete(*keys)
    client.delete('te:demands', 'te:flow:dst_ip', 'policy_queue', 'topo:last_update_unix', 'te:mode')
    client.set('te:traffic:external_control', '1')
    standard_env = dict(ENV, PYTHONPATH=str(OPEN / 'TopoConfigurators'), ADDR='127.0.0.1', PORT='8080',
                        OPENSN_FIXED_TIME='2026-09-14T16:25:00')
    with (work / 'standard.log').open('w') as log:
        process = subprocess.Popen([str(PYTHON), '-u', 'main.py'], cwd=OPEN / 'TopoConfigurators/Standard',
                                   env=standard_env, stdout=log, stderr=subprocess.STDOUT)
        try:
            def ready():
                if process.poll() is not None:
                    raise RuntimeError(f'Standard exited: {process.returncode}; see {log.name}')
                return len(json.loads(client.get('te:demands') or '[]')) == 24
            wait_for('24 demands', ready, max(180, satellites))
            settle = max(60, min(300, satellites // 2))
            print(f'等待 OSPF 收敛 {settle} 秒；NodeDaemon 保持运行。', flush=True)
            time.sleep(settle)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    command([PYTHON, ROOT / 'python_bgp_gateway/topo_sniffer.py', '--once'], timeout=180)
    sys.path.insert(0, str(ROOT))
    import run_te_experiment as experiment
    experiment.reconfigure_all_ospf()
    print('等待全量 OSPF 配置传播 60 秒。', flush=True)
    time.sleep(60)
    experiment.ensure_ospf_health(
        min_routes=max(100, satellites * 4),
        timeout_seconds=max(120, satellites // 2),
    )
    verify_web(satellites + 2)
    verify_scale(scale)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        'action',
        choices=['prepare', 'status', 'run', 'benchmark', 'calibrate'],
    )
    parser.add_argument('--scale', type=parse_scale, default=parse_scale('12x10'))
    parser.add_argument('--rebuild', action='store_true')
    parser.add_argument('--load', type=float)
    parser.add_argument('--cycles', type=int, default=1)
    parser.add_argument('--seconds', type=int)
    args = parser.parse_args()
    scale, orbits, per_orbit = args.scale
    satellites = orbits * per_orbit
    fixed_neighbors = ENV.get('TE_FIXED_NEIGHBORS', '1' if satellites >= 500 else '0')
    if fixed_neighbors not in ('0', '1'):
        parser.error('TE_FIXED_NEIGHBORS 必须为 0 或 1')
    RUNTIME.mkdir(exist_ok=True)
    (LAB / 'tmp').mkdir(exist_ok=True)
    if args.action == 'calibrate':
        env = dict(ENV, TE_SCALE=scale, TE_FIXED_NEIGHBORS=fixed_neighbors)
        if args.seconds is not None:
            env['TE_CALIBRATION_MEASURE_SECONDS'] = str(args.seconds)
        if args.load is not None:
            if not 20000 <= args.load <= 250000:
                parser.error('校准初始负载必须在 20000–250000 Kbps')
            env['TE_CALIBRATION_INITIAL_LOAD_KBPS'] = str(round(args.load))
        os.execve(PYTHON, [str(PYTHON), str(ROOT / 'scripts/calibrate.py')], env)
    if args.action == 'run':
        verify_ownership()
        if not complete_nodes(satellites):
            raise RuntimeError(f'当前运行节点不是 {scale}（应有 {satellites}+2），请先 prepare --scale {scale}')
        verify_scale(scale)
        verify_web(satellites + 2)
        collector = ROOT / 'scripts/collect_srv6_evidence.py'
        os.execve(
            PYTHON,
            [
                str(PYTHON),
                str(collector),
                '--scale',
                scale,
                '--expected-satellites',
                str(satellites),
            ],
            ENV,
        )
    if args.action == 'benchmark':
        calibration_run = args.load is not None
        verify_ownership()
        if not complete_nodes(satellites):
            raise RuntimeError(f'当前运行节点不是 {scale}（应有 {satellites}+2），请先 prepare --scale {scale}')
        verify_scale(scale)
        frozen = ROOT / f'config/experiment_{scale.replace("x", "_")}_frozen.json'
        if args.load is None:
            if not frozen.exists():
                parser.error('尚未冻结参数，请使用 --load 指定校准负载（Kbps）')
            profile = json.loads(frozen.read_text())
            if not profile.get('formal_passed'):
                raise RuntimeError('冻结参数尚未通过正式验证')
            if bool(profile.get('fixed_neighbors', False)) != (fixed_neighbors == '1'):
                raise RuntimeError('邻居控制模式与冻结配置不匹配，请重新校准。')
            if args.seconds is None:
                args.seconds = profile['formal_seconds']
            with (RUNTIME / 'experiment.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                command([PYTHON, ROOT / 'python_bgp_gateway/topo_sniffer.py', '--once'], timeout=180)
                sys.path.insert(0, str(ROOT))
                import run_te_experiment as experiment
                client = redis_client()
                topology = experiment.validate_topology_snapshot(client, json.loads(client.get('te:demands') or '[]'))
            if topology['snapshot_sha256'] != profile['topology_sha256']:
                raise RuntimeError('拓扑与冻结参数不匹配，请先 ./lab.sh calibrate 重新校准。')
            args.load = profile['offered_load_kbps']
        if args.seconds is None:
            args.seconds = 60 if satellites >= 500 else 25
        verify_web(satellites + 2)
        env = dict(ENV, OPEN_SN_DIR=str(OPEN), PYTHON_BIN=str(PYTHON),
                   TE_DEMAND_TOTAL_KBPS=str(args.load), TE_CYCLES=str(args.cycles),
                   TE_MEASURE_SECONDS=str(args.seconds), TE_MIN_AVAILABLE_MIB='1024', TE_REQUIRE_WEB='1',
                   TE_TARGET_LOW_PERCENT='14', TE_TARGET_HIGH_PERCENT='16', TE_RESOURCE_WAIT_SECONDS='180',
                   TE_REPORT_ONLY='1' if calibration_run else '0',
                   TE_FIXED_NEIGHBORS=fixed_neighbors,
                   TE_SCALE=scale, TE_EXPECTED_SATELLITES=str(satellites),
                   TE_EXPECTED_GROUND_STATIONS='2', TE_EXPECTED_NODES=str(satellites + 2))
        os.execve(ROOT / 'run_full_experiment.sh', [str(ROOT / 'run_full_experiment.sh')], env)
    elif args.action == 'prepare':
        with (RUNTIME / 'experiment.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            prepare(scale, satellites, args.rebuild)
    else:
        print('Running nodes:', len(nodes()))
        print('Demands:', len(json.loads(redis_client().get('te:demands') or '[]')))
        verify_web(satellites + 2)
        verify_scale(scale)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(1)

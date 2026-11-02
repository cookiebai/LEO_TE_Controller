"""Run task 1's supplied cfg-driven deployer, without generating any cfg."""
import argparse
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import uuid

from integration import task1_deployer as original


VENDOR = Path(__file__).parent / 'vendor/deploy-20261027'
LABEL = 'leo.te.deployment'
MAC = re.compile(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$')
INTERFACE = re.compile(r'[A-Za-z0-9_.-]{1,15}$')


def call(args, check=True):
    result = subprocess.run(args, text=True, capture_output=True)
    if check and result.returncode:
        raise RuntimeError(f'{args}: {result.stderr.strip() or result.stdout.strip()}')
    return result.stdout.strip()


def validate_configs(nodes, links, spares, mgmt):
    """Validate fields before passing them to the vendor shell-based runner."""
    all_veth = []
    for name, item in nodes.items():
        if not re.fullmatch(r'L0[1-6](?:0[1-9]|1[01])', name):
            raise ValueError(f'Node outside the supplied lite grid: {name}')
        interfaces = set()
        cfg = item['cfg']
        entries = [(mac, port) for mac, port in cfg['ports'].items()]
        entries.append((cfg['man']['mac'], cfg['man']))
        for mac, port in entries:
            iface, veth = port['eth'], port['veth']
            if not MAC.fullmatch(mac) or int(mac[:2], 16) & 1:
                raise ValueError(f'Invalid unicast MAC: {mac}')
            if not INTERFACE.fullmatch(iface) or not INTERFACE.fullmatch(veth):
                raise ValueError(f'Invalid interface names: {iface}, {veth}')
            if iface in interfaces:
                raise ValueError(f'{name}: interface used twice: {iface}')
            interfaces.add(iface)
            all_veth.append(veth)
        ipaddress.IPv4Address(cfg['man']['ip'])
    if len(set(all_veth)) != len(all_veth):
        raise ValueError('Provided cfg reuses an endpoint veth name')
    for linkid, endpoints in links.items():
        if len(endpoints) != 2 or ''.join(sorted(e[0] for e in endpoints)) != linkid:
            raise ValueError(f'Invalid endpoint pairing in supplied cfg: {linkid}')


def main():
    parser = argparse.ArgumentParser(description='使用一键部署包的原始cfg与部署器，不生成替代拓扑')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--plan', action='store_true')
    action.add_argument('--apply', action='store_true')
    action.add_argument('--verify', action='store_true')
    action.add_argument('--undo', action='store_true')
    parser.add_argument('--with-lite', action='store_true')
    parser.add_argument('--image', default=os.environ.get('LEO_TE_IMAGE', 'leo-te-integrated:full-20261027'))
    parser.add_argument('--output', default='/output/deployment')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_./:@-]+', args.image):
        parser.error('Invalid Docker image reference')
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    statefile = output / 'deploy_state.json'
    original.SUDO = ''  # The wrapper runs as root in the host net/pid namespaces.
    original.STATE = str(statefile)
    if args.undo:
        state = json.loads(statefile.read_text())
        owner = state['deployment_id']
        # Validate EVERY remaining object before deleting anything.
        for container in state['containers']:
            raw = call(['docker', 'inspect', container], check=False)
            if not raw:
                continue
            info = json.loads(raw)[0]
            if info['Id'] != state['container_ids'].get(container) or (info['Config']['Labels'] or {}).get(LABEL) != owner:
                raise ValueError(f'Refusing to remove a replaced/unowned container: {container}')
        for iface in state.get('host_ends', []):
            raw = call(['ip', '-j', 'link', 'show', 'dev', iface], check=False)
            if raw and json.loads(raw)[0].get('ifalias') != 'leo-te:' + owner:
                raise ValueError(f'Refusing to remove an unowned host interface: {iface}')
        original.undo()
        return
    nodes = original.load_cfgs(str(VENDOR / 'cfg'))
    links, spares, mgmt, errors = original.parse_ports(nodes)
    if errors:
        raise ValueError('; '.join(errors))
    validate_configs(nodes, links, spares, mgmt)
    errors = original.check_names(nodes, links, spares, mgmt, applying=args.apply)
    if errors:
        raise ValueError('; '.join(errors))
    plan = {'cfg_source': '一键部署包.rar/cfg (unchanged)', 'nodes': len(nodes),
            'business_links': len(links), 'spare_ports': len(spares), 'management_ports': len(mgmt),
            'image': args.image,
            'cfg_sha256': {name: hashlib.sha256(Path(item['file']).read_bytes()).hexdigest()
                           for name, item in sorted(nodes.items())},
            'links': {link: endpoints for link, endpoints in sorted(links.items())}}
    (output / 'plan.json').write_text(json.dumps(plan, indent=2) + '\n')
    print(f'Supplied cfg: {len(nodes)} nodes, {len(links)} business links, '
          f'{len(spares)} spare ports, {len(mgmt)} management ports', flush=True)
    if args.verify:
        original.verify(nodes, links)
        return
    if not args.apply:
        print('Dry-run only. No nodes, links, or cfg files were created.', flush=True)
        return
    if statefile.exists():
        raise ValueError(f'Existing deployment state at {statefile}; inspect it before applying again')
    if os.geteuid() != 0:
        raise ValueError('Deployment requires the privileged host-net/host-pid wrapper')
    owner = uuid.uuid4().hex
    container_ids = {}
    original_run = original.run
    latest_state = None

    def save(state):
        nonlocal latest_state
        state['host_ends'] = list(dict.fromkeys(state.get('host_ends', [])))
        state.update(deployment_id=owner, container_ids=dict(container_ids),
                     image=args.image, cfg_sha256=plan['cfg_sha256'],
                     recorded_at=datetime.now(timezone.utc).isoformat())
        temporary = statefile.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n')
        temporary.replace(statefile)
        latest_state = state

    def create_node(name, image):
        # Explicit node mode keeps the supplied FRR startup, rather than the
        # integrated image's controller/help entrypoint.
        cid = call(['docker', 'run', '-d', '--name', name, '--hostname', name,
                    '--label', LABEL + '=' + owner, '--privileged', '--cap-add', 'NET_ADMIN',
                    '--network', 'none', '--sysctl', 'net.ipv6.conf.all.disable_ipv6=0',
                    '--sysctl', 'net.ipv6.conf.all.forwarding=1', image, 'node'])
        container_ids[name] = cid
        return cid

    def run(text, check=True):
        result = original_run(text, check=check)
        words = shlex.split(text)
        if words[:3] == ['ip', 'link', 'add']:
            devs = [words[3]]
            if 'peer' in words:
                devs.append(words[words.index('peer') + 2])
            for dev in devs:
                call(['ip', 'link', 'set', 'dev', dev, 'alias', 'leo-te:' + owner])
            # Record endpoints before any namespace move can fail. Preserve the
            # vendor state object, so its later saves also retain this ledger.
            if latest_state is not None:
                latest_state['host_ends'].extend(devs)
                save(latest_state)
        return result

    original.save, original.docker_run, original.run = save, create_node, run
    original.apply(nodes, links, spares, mgmt, args.image,
                   str(VENDOR / 'auto66-lite.sh'), str(Path(__file__).parent / 'queue-probe.sh'))
    if args.with_lite:
        original.start_lite(nodes)
    print(f'Deployment state: {statefile}', flush=True)


if __name__ == '__main__':
    main()

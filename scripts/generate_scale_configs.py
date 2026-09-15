#!/usr/bin/env python3
"""Generate deterministic polar-grid topology and daemon configs for scale tests."""
import argparse
import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OPEN = ROOT.parent / 'OpenSN-Library'
TOPOLOGY_DIR = OPEN / 'example/test_topologies'


def checksum(line):
    return sum(int(c) if c.isdigit() else 1 if c == '-' else 0 for c in line) % 10


def tle(orbit, satellite, orbits, per_orbit):
    # Match the already-qualified 12x10 constellation's epoch/inclination/motion.
    phase = 360 / per_orbit * satellite + 360 / per_orbit / orbits * orbit
    longitude = 180 * orbit / orbits
    line1 = '1 00000U 23666A   26017.74871964  .00000000  00000-0 00000000 0 0000'
    line2 = f'2 00000  90.0000 {longitude:08.4f} 0000011   0.0000 {phase:8.4f} {13.1507:11.8f}00000'
    return line1 + str(checksum(line1)), line2 + str(checksum(line2))


def generate(orbits, per_orbit):
    instances = []
    for orbit in range(orbits):
        for satellite in range(per_orbit):
            line1, line2 = tle(orbit, satellite, orbits, per_orbit)
            instances.append({'type': 'Satellite', 'extra': {
                'TLE_0': f'NODE_{orbit}_{satellite}', 'TLE_1': line1, 'TLE_2': line2,
                'Area': '0.0.0.0', 'OrbitIndex': str(orbit),
                'SatelliteIndex': str(satellite)}, 'device_info': {}})
    # Keep ground stations identical across scale trials for a fair comparison.
    reference = json.loads((TOPOLOGY_DIR / 'topology_config_12_10.json').read_text())
    instances.extend(copy.deepcopy(reference['instances'][-2:]))
    links = []
    for orbit in range(orbits):
        for satellite in range(per_orbit):
            here = orbit * per_orbit + satellite
            peers = [orbit * per_orbit + (satellite + 1) % per_orbit]
            if orbit < orbits - 1:
                peers.append((orbit + 1) * per_orbit + satellite)
            for peer in peers:
                links.append({'end_indexes': [here, peer], 'type': 'vlink',
                              'init_parameter': {}, 'address_infos': [{}, {}], 'extra': {}})
    return {'instances': instances, 'links': links}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('scales', nargs='+')
    args = parser.parse_args()
    daemon_template = json.loads((ROOT / 'config/experiment_daemon_12_10.json').read_text())
    for scale in args.scales:
        try:
            orbits, per_orbit = (int(value) for value in scale.lower().split('x'))
        except (ValueError, TypeError):
            parser.error(f'invalid scale: {scale}; expected e.g. 18x20')
        if orbits < 2 or per_orbit < 3:
            parser.error('scale must have at least 2 orbits and 3 satellites per orbit')
        topology = generate(orbits, per_orbit)
        slug = f'{orbits}_{per_orbit}'
        topology_path = TOPOLOGY_DIR / f'topology_config_{slug}.json'
        topology_path.write_text(json.dumps(topology, separators=(',', ':')) + '\n')
        daemon = copy.deepcopy(daemon_template)
        daemon['app']['instance_capacity'] = orbits * per_orbit + 32
        daemon['app']['parallel_num'] = min(8, max(2, orbits * per_orbit // 60))
        daemon_path = ROOT / f'config/experiment_daemon_{slug}.json'
        daemon_path.write_text(json.dumps(daemon, indent=2) + '\n')
        print(f'{scale}: satellites={orbits * per_orbit}, instances={len(topology["instances"])}, '
              f'links={len(topology["links"])}, daemon_parallel={daemon["app"]["parallel_num"]}')


if __name__ == '__main__':
    main()

"""Independently recompute scale-run acceptance from recorded measurements."""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def audit(directory, scale):
    provenance = json.loads((directory / 'provenance.json').read_text())
    if provenance.get('parameters', {}).get('TE_SCALE') != scale:
        return None
    result_path = directory / 'result.json'
    data = json.loads(result_path.read_text()) if result_path.exists() else {}
    exit_path = directory / 'exit_status.json'
    exit_data = json.loads(exit_path.read_text()) if exit_path.exists() else {}
    web_path = directory / 'frontend_health.jsonl'
    web = [json.loads(line) for line in web_path.read_text().splitlines()] if web_path.exists() else []
    cycles = []
    for cycle in data.get('cycles', []):
        baseline, te = cycle['baseline'], cycle['te']
        improvement = (te['total_mean_mbps'] / baseline['total_mean_mbps'] - 1) * 100
        paired = abs(te['actual_sent_mbps'] / baseline['actual_sent_mbps'] - 1) * 100
        policy, ping = cycle['policy'], cycle['ping']
        cycles.append({
            'cycle': cycle['cycle'], 'baseline_mbps': baseline['total_mean_mbps'],
            'te_mbps': te['total_mean_mbps'], 'improvement_percent': improvement,
            'baseline_cv_percent': baseline['total_cv'] * 100,
            'te_cv_percent': te['total_cv'] * 100, 'paired_sent_difference_percent': paired,
            'pass': (14 <= improvement <= 16 and paired <= 1
                     and all(phase['total_cv'] <= .05 and phase['sent_load_ratio'] >= .98
                             and phase['min_flow_sent_ratio'] >= .98 for phase in (baseline, te))
                     and policy['desired'] == policy['applied'] == 24 and policy['queued'] == 0
                     and ping['srv6_ipv4_ping_ok'] == ping['srv6_ipv6_ping_ok'] == 24),
        })
    topology = data.get('experiment', {}).get('topology', {})
    satellites = int(scale.split('x')[0]) * int(scale.split('x')[1])
    fixed_neighbors = provenance['parameters'].get('TE_FIXED_NEIGHBORS') == '1'
    neighbor_path = directory / 'fixed_neighbors.json'
    neighbor_data = json.loads(neighbor_path.read_text()) if neighbor_path.exists() else {}
    neighbors_restored = (not fixed_neighbors or
                          (neighbor_data.get('status') == 'restored'
                           and neighbor_data.get('pinned_unix') is not None
                           and len(neighbor_data.get('nodes', {})) == satellites + 2))
    eligible = (data.get('status') == 'completed' and data.get('target_met') is True
                and exit_data.get('exit_code') == 0 and bool(cycles)
                and all(cycle['pass'] for cycle in cycles)
                and topology.get('nodes') == satellites + 2 and topology.get('down_links') == 0
                and neighbors_restored
                and bool(web) and all(sample.get('ok') is True for sample in web))
    return {'directory': str(directory), 'scale': scale,
            'load_kbps': provenance['parameters'].get('TE_DEMAND_TOTAL_KBPS'),
            'status': data.get('status'), 'exit_code': exit_data.get('exit_code'),
            'topology_sha256': topology.get('snapshot_sha256'),
            'web_checks': len(web), 'web_failures': sum(not item.get('ok') for item in web),
            'fixed_neighbors': fixed_neighbors, 'neighbors_restored': neighbors_restored,
            'cycles': cycles, 'independent_pass': eligible,
            'three_cycle_pass': eligible and len(cycles) >= 3}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scale', default='25x20')
    args = parser.parse_args()
    rows = []
    for directory in sorted((ROOT / 'results/runs').iterdir()):
        if (directory / 'provenance.json').exists():
            row = audit(directory, args.scale)
            if row is not None:
                rows.append(row)
    print(json.dumps(rows, ensure_ascii=False, indent=2))

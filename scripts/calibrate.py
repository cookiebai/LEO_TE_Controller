"""Bounded real offered-load search, independent confirmation, then frozen 3-cycle validation."""
from datetime import datetime
import fcntl
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
DEST = None
trials = []
reference_hash = None
SCALE = os.environ.get('TE_SCALE', '12x10')
MEASURE_SECONDS = int(os.environ.get('TE_CALIBRATION_MEASURE_SECONDS',
    '60' if math.prod(map(int, SCALE.split('x'))) >= 500 else '25'))


def run_directory(log_text):
    # Never attribute an unrelated/stale results/latest to a failed launch.
    match = re.search(r'本次运行目录：([^\n\r]+)', log_text)
    if not match:
        raise RuntimeError('Trial did not start; inspect its log (possibly lock/preflight failure)')
    path = Path(match.group(1).strip()).resolve()
    if path.parent != (ROOT / 'results/runs').resolve():
        raise RuntimeError('Unexpected trial directory: ' + str(path))
    return path


def trial(load, cycles=1, seconds=20):
    global reference_hash
    argv = [str(ROOT / 'lab.sh'), 'run', '--scale', SCALE, '--load', str(load),
            '--cycles', str(cycles), '--seconds', str(seconds)]
    print(f'Calibration command: {" ".join(argv)}', flush=True)
    log_path = DEST / f'trial-{len(trials)+1}.log'
    with log_path.open('w') as log:
        result = subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT)
    run_dir = run_directory(log_path.read_text())
    result_path = run_dir / 'result.json'
    data = json.loads(result_path.read_text()) if result_path.exists() else {}
    row = {'load_kbps': load, 'cycles': cycles, 'seconds': seconds, 'directory': str(run_dir),
           'exit_code': result.returncode, 'mean_improvement_percent': data.get('mean_improvement_percent'),
           'target_met': data.get('target_met'), 'phase_stable': data.get('phase_stable'),
           'paired_load_valid': data.get('paired_load_valid')}
    trials.append(row)
    (DEST / 'trials.json').write_text(json.dumps(trials, indent=2))
    print(json.dumps(row), flush=True)
    if result.returncode != 0 or data.get('status') != 'completed' or not data.get('phase_stable') or not data.get('paired_load_valid'):
        raise RuntimeError('Invalid trial; inspect saved logs before continuing. No frozen profile published.')
    topo_hash = data['experiment']['topology']['snapshot_sha256']
    if reference_hash is not None and topo_hash != reference_hash:
        raise RuntimeError('Topology changed across calibration trials')
    reference_hash = topo_hash
    return data


def calibrate():
    if not 20 <= MEASURE_SECONDS <= 300:
        raise RuntimeError('Calibration phase duration must be 20–300 seconds')
    load = int(os.environ.get('TE_CALIBRATION_INITIAL_LOAD_KBPS', '90000'))
    if not 20000 <= load <= 250000:
        raise RuntimeError('Initial calibration load must be 20000–250000 Kbps')
    subprocess.run([str(ROOT / 'lab.sh'), 'prepare', '--scale', SCALE], check=True)
    lower = upper = None
    for _ in range(12):
        data = trial(load, seconds=MEASURE_SECONDS)
        improvement = data['mean_improvement_percent']
        if 14 <= improvement <= 16:
            confirmed = trial(load, seconds=MEASURE_SECONDS)
            if confirmed['target_met']:
                profile = {'scale': SCALE, 'offered_load_kbps': load, 'target_low_percent': 14, 'target_high_percent': 16,
                           'topology_sha256': reference_hash, 'fixed_time': '2026-09-14T16:25:00',
                           'formal_cycles': 3, 'formal_seconds': MEASURE_SECONDS, 'calibration_directory': str(DEST),
                           'fixed_neighbors': os.environ.get('TE_FIXED_NEIGHBORS', '0') == '1',
                           'formal_passed': False}
                # Freeze before formal validation. Do not tune using formal validation outcomes.
                (DEST / 'frozen-before-validation.json').write_text(json.dumps(profile, indent=2))
                formal = trial(load, cycles=3, seconds=MEASURE_SECONDS)
                if not formal['target_met']:
                    raise RuntimeError('Formal validation failed; frozen parameters and all results retained, no automatic retuning.')
                profile.update(formal_passed=True, formal_result=trials[-1]['directory'])
                target = ROOT / f'config/experiment_{SCALE.replace("x", "_")}_frozen.json'
                target.write_text(json.dumps(profile, indent=2) + '\n')
                print(f'Qualified frozen profile: {target}', flush=True)
                return
            improvement = confirmed['mean_improvement_percent']
        if improvement < 14:
            lower = load
        elif improvement > 16:
            upper = load
        if lower is not None and upper is not None:
            if lower >= upper:
                raise RuntimeError('Nonmonotonic search bracket; diagnose instead of blindly tuning.')
            load = round((lower + upper) / 2)
        else:
            load = round(load * (1.2 if improvement < 14 else .85))
        if not 20000 <= load <= 250000:
            raise RuntimeError('Search exceeded configured 20–250 Mbps offered-load range')
    raise RuntimeError('No stable working point within 12 trials; all trials retained')


def main():
    global DEST
    runtime = ROOT.parent / 'runtime'
    runtime.mkdir(exist_ok=True)
    with (runtime / 'calibration.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('已有校准任务正在运行') from None
        DEST = ROOT / 'results/calibration' / (SCALE + '-' + datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
        DEST.mkdir(parents=True, exist_ok=False)
        try:
            calibrate()
        except (Exception, KeyboardInterrupt) as exc:
            (DEST / 'incomplete.txt').write_text(str(exc) + '\nSee trials.json and trial logs.\n')
            raise


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as exc:
        print(f'Calibration incomplete: {exc}; handoff: {DEST}', file=sys.stderr)
        sys.exit(1)

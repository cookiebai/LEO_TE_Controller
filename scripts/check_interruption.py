"""Opt-in integration check: interrupt a real baseline; keep all evidence."""
from datetime import datetime
import json
import os
from pathlib import Path
import signal
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT.parent / 'runtime' / ('interruption-check-' + datetime.now().strftime('%Y%m%d_%H%M%S'))
DEST.mkdir()
log_path = DEST / 'console.log'
with log_path.open('w') as log:
    process = subprocess.Popen([str(ROOT / 'lab.sh'), 'run', '--load', '90000', '--seconds', '25'],
                               stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        deadline = time.monotonic() + 180
        while '[baseline_1] 24 synchronized receiver-side sessions' not in log_path.read_text():
            if process.poll() is not None:
                raise RuntimeError('Experiment exited before interruption; inspect console.log')
            if time.monotonic() > deadline:
                raise RuntimeError('Timed out waiting for baseline traffic')
            time.sleep(1)
        time.sleep(7)  # The common start barrier releases traffic after five seconds.
        os.killpg(process.pid, signal.SIGINT)
        rc = process.wait(timeout=90)
        from calibrate import run_directory
        run_dir = run_directory(log_path.read_text())
        status = json.loads((run_dir / 'exit_status.json').read_text())
        data = json.loads((run_dir / 'result.json').read_text())
        report = {'directory': str(run_dir), 'returncode': rc, 'exit_status': status,
                  'result_status': data['status'], 'target_met': data['target_met']}
        (DEST / 'report.json').write_text(json.dumps(report, indent=2))
        assert rc == 130 and status['status'] == 'interrupted', report
        assert data['status'] != 'completed' and not data['target_met'], report
        print(json.dumps(report, indent=2))
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=90)

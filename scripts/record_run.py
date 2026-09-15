"""Write run provenance and an exit marker even when preflight fails."""
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time


def output(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    return result.stdout.strip()


def main():
    destination = Path(sys.argv[1])
    root = Path(__file__).resolve().parents[1]
    phase = sys.argv[2]
    if phase == 'start':
        repos = {}
        for repo in (root, root.parent / 'OpenSN-Library'):
            repos[repo.name] = {'commit': output('git', '-C', str(repo), 'rev-parse', 'HEAD'),
                                'status': output('git', '-C', str(repo), 'status', '--short')}
            (destination / (repo.name + '.patch')).write_text(output('git', '-C', str(repo), 'diff'))
            for name in output('git', '-C', str(repo), 'ls-files', '--others', '--exclude-standard').splitlines():
                if Path(name).parts[0] in ('results', 'runtime', '.cache'):
                    continue
                source = repo / name
                if source.suffix in ('.py', '.sh', '.go', '.json', '.md') and source.is_file():
                    saved = destination / 'untracked-source' / repo.name / name
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, saved)
        data = {'started_unix': time.time(), 'python': sys.version, 'kernel': platform.release(),
                'repos': repos, 'parameters': {k: v for k, v in os.environ.items()
                    if k.startswith('TE_') or k in ('OPEN_SN_DIR', 'PYTHON_BIN')},
                'docker_images': output('docker', 'image', 'ls', '--no-trunc', '--format', '{{.Repository}}:{{.Tag}} {{.ID}}')}
        (destination / 'provenance.json').write_text(json.dumps(data, indent=2))
    else:
        rc = int(phase)
        data = {'finished_unix': time.time(), 'exit_code': rc,
                'status': 'finished' if rc == 0 else 'interrupted' if rc in (130, 143) else 'failed'}
        (destination / 'exit_status.json').write_text(json.dumps(data, indent=2))
        if not (destination / 'summary.txt').exists():
            (destination / 'summary.txt').write_text(f"运行{data['status']}，退出码 {rc}。详情见 console.log；未完成的运行不构成目标验收。\n")


if __name__ == '__main__':
    main()

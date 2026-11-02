import json
from pathlib import Path
import subprocess
import time
import uuid

root = Path(__file__).resolve().parent
image = "leo-te-controller:merged-frr-20261030"
created = []
results = {"image_id": subprocess.check_output(["docker", "image", "inspect", image, "--format", "{{.Id}}"], text=True).strip()}


def call(args, **kwargs):
    return subprocess.run(args, text=True, capture_output=True, **kwargs)


try:
    for mode in ("controller", "node"):
        name = "leo-merged-" + mode + "-audit-" + uuid.uuid4().hex[:8]
        args = ["docker", "run", "-d", "--name", name, "--network", "none"]
        if mode == "node":
            args += ["--cap-add", "NET_ADMIN", "--cap-add", "NET_RAW", "--cap-add", "SYS_ADMIN"]
        else:
            args += ["--cap-drop", "ALL"]
        args += [image] + (["node"] if mode == "node" else [])
        cid = call(args, check=True).stdout.strip()
        created.append(cid)
        marker = "FRR Processes:" if mode == "node" else "LEO TE controller ready"
        for attempt in range(100):
            logs = call(["docker", "logs", cid]).stdout
            if marker in logs:
                break
            state = call(["docker", "inspect", "--format", "{{.State.Running}}", cid]).stdout.strip()
            if state != "true":
                raise RuntimeError(mode + " stopped: " + logs)
            time.sleep(.2)
        else:
            raise RuntimeError(mode + " failed to start: " + logs)
        if mode == "controller":
            extra = call(["docker", "exec", cid, "leo-te", "help"], check=True).stdout
        else:
            extra = call(["docker", "exec", cid, "/bin/sh", "-c", "pgrep -x zebra && pgrep -x isisd && test ! -e /run/leo-te-role && vtysh -c 'show version'"], check=True).stdout
            results["node_controller_marker_removed"] = True
        (root / (mode + "-startup.log")).write_text(logs + "\n" + extra)
        results[mode + "_startup"] = "PASS"
        if mode == "node":
            script = """import pathlib,subprocess,json
paths=[p for p in pathlib.Path('/usr/lib/frr').rglob('*') if p.is_file()]+[pathlib.Path('/usr/bin/vtysh')]
checked=[];bad=[]
for p in paths:
 with p.open('rb') as f: magic=f.read(4)
 if magic!=b'\\x7fELF':continue
 r=subprocess.run(['ldd',str(p)],capture_output=True,text=True)
 checked.append(str(p))
 if 'not found' in r.stdout+r.stderr or r.returncode:bad.append({'path':str(p),'output':r.stdout+r.stderr})
print(json.dumps({'checked':checked,'failures':bad},indent=2))
assert not bad
"""
            output = call(["docker", "exec", cid, "python3", "-c", script], check=True).stdout
            (root / "frr-library-validation.json").write_text(output)
            results["frr_elf_dependency_check"] = "PASS"
            results["frr_elf_objects_checked"] = len(json.loads(output)["checked"])
finally:
    for cid in reversed(created):
        call(["docker", "rm", "-f", cid], check=True)
(root / "runtime-validation.json").write_text(json.dumps(results, indent=2))
print(json.dumps(results, indent=2))

import hashlib
import json
from pathlib import Path
import subprocess
import tarfile

root = Path(__file__).resolve().parent
image = "leo-te-controller:merged-frr-20261030"
cid = subprocess.check_output(["docker", "create", "--network", "none", "--entrypoint", "/bin/sh", image], text=True).strip()
try:
    subprocess.run(["docker", "export", "-o", str(root / "merged-rootfs.tar"), cid], check=True)
finally:
    subprocess.run(["docker", "rm", cid], check=True, stdout=subprocess.DEVNULL)
records = {}
with tarfile.open(root / "merged-rootfs.tar") as archive:
    for member in archive:
        name = member.name.removeprefix("./").lstrip("/").rstrip("/")
        if not name:
            continue
        record = dict(type=member.type.decode(), mode=member.mode, uid=member.uid,
                      gid=member.gid, linkname=member.linkname, size=member.size)
        if member.isfile() or member.islnk():
            record["sha256"] = hashlib.file_digest(archive.extractfile(member), "sha256").hexdigest()
        records[name] = record
(root / "merged-inventory.json").write_text(json.dumps(records, indent=2))
manifest = json.loads((root / "context/merge-manifest.json").read_text())
volatile = set(manifest["volatile_paths"])
exceptions = set(manifest["runtime_integration_files"])
conflicts = set(manifest["conflicting_paths"])


def equivalent(left, right):
    if left["type"] == "5":
        return right["type"] == "5"
    if left.get("sha256") is not None:
        return left["sha256"] == right.get("sha256") and left["mode"] == right["mode"]
    return all(left[key] == right[key] for key in ("type", "mode", "linkname"))


failures = []
counts = {}
for source in ("controller", "node"):
    counts[source] = 0
    for name, record in manifest[source + "_files"].items():
        if name in volatile:
            continue
        use_backup = name in exceptions or (source == "node" and name in conflicts)
        target = f"opt/leo-te-merge/originals/{source}/{name}" if use_backup else name
        if target not in records or not equivalent(record, records[target]):
            failures.append(dict(source=source, path=name, checked=target))
        counts[source] += 1
apps = [name for name, record in manifest["controller_files"].items()
        if name.startswith("app/") and record.get("sha256")]
proof = dict(
    image_id=subprocess.check_output(["docker", "image", "inspect", image, "--format", "{{.Id}}"], text=True).strip(),
    controller_entries_checked=counts["controller"], node_entries_checked=counts["node"],
    controller_app_regular_files=len(apps),
    controller_app_unchanged=all(equivalent(manifest["controller_files"][name], records[name]) for name in apps),
    node_scripts_unchanged={name: equivalent(manifest["node_files"][name], records[name])
                            for name in ("auto66-lite.sh", "queue-probe.sh", "start-frr.sh")},
    runtime_system_files=sorted(exceptions), conflicting_paths_backed_up=len(conflicts),
    failures=failures,
)
(root / "file-preservation-validation.json").write_text(json.dumps(proof, indent=2))
print(json.dumps(proof, indent=2))
assert not failures

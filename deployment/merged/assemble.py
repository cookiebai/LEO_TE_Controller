"""Prepare an additive merge without replacing the controller application."""
import copy
import hashlib
import json
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parent
CONTEXT = ROOT / "context"
CONTEXT.mkdir(exist_ok=True)
BACKUP = "opt/leo-te-merge/originals"
RUNTIME_FILES = {"etc/passwd", "etc/group", "etc/shadow", "etc/gshadow", "etc/ld.so.cache", "var/cache/ldconfig/aux-cache"}
VOLATILE = {".dockerenv", "etc/hostname", "etc/hosts", "etc/resolv.conf"}


def normalize(name):
    return name.removeprefix("./").lstrip("/").rstrip("/")


def inventory(archive):
    records = {}
    for member in archive:
        name = normalize(member.name)
        if not name:
            continue
        record = dict(type=member.type.decode(), mode=member.mode,
                      uid=member.uid, gid=member.gid, linkname=member.linkname,
                      size=member.size)
        if member.isfile() or member.islnk():
            record["sha256"] = hashlib.file_digest(archive.extractfile(member), "sha256").hexdigest()
        records[name] = record
    return records


def same(left, right):
    if left["type"] == right["type"] == "5":
        return True
    return all(left.get(key) == right.get(key)
               for key in ("type", "mode", "sha256", "linkname"))


def add(output, source, member, name):
    info = copy.copy(member)
    info.name = name
    # Materialize hard links so relocated backups cannot refer to active files.
    if member.isfile() or member.islnk():
        stream = source.extractfile(member)
        if member.islnk():
            info.type = tarfile.REGTYPE
            info.linkname = ""
            info.size = len(stream.read())
            stream.seek(0)
        output.addfile(info, stream)
    else:
        output.addfile(info)


with tarfile.open(ROOT / "controller-rootfs.tar") as controller, tarfile.open(ROOT / "node-rootfs.tar") as node:
    original = inventory(controller)
    incoming = inventory(node)
    conflicts = {name for name, record in incoming.items()
                 if name in original and not same(record, original[name]) and name not in VOLATILE}
    additions = {name for name in incoming if name not in original and name not in VOLATILE}
    # Refuse directory/symlink substitutions; those need an explicit merge design.
    for name in conflicts:
        if "5" in (original[name]["type"], incoming[name]["type"]):
            raise RuntimeError(f"Directory conflict: {name}")
    protected = conflicts | RUNTIME_FILES
    with tarfile.open(CONTEXT / "node-payload.tar", "w") as output:
        for member in node.getmembers():
            name = normalize(member.name)
            if name in additions:
                add(output, node, member, name)
            if name in protected:
                add(output, node, member, f"{BACKUP}/node/{name}")
        for member in controller.getmembers():
            name = normalize(member.name)
            if name in protected:
                add(output, controller, member, f"{BACKUP}/controller/{name}")
    manifest = dict(
        controller_image="leo-te-controller:field-20261029",
        node_image="frr-auto66-lite:new-20261029",
        controller_files=original, node_files=incoming,
        added_paths=sorted(additions), conflicting_paths=sorted(conflicts),
        runtime_integration_files=sorted(RUNTIME_FILES),
        volatile_paths=sorted(VOLATILE),
        backup_prefix="/" + BACKUP,
        policy="Controller paths retained; node-only paths added verbatim. Both conflicting originals backed up. FRR account entries appended and loader cache regenerated.",
    )
    (CONTEXT / "merge-manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    print(json.dumps(dict(additions=len(additions), conflicts=len(conflicts), payload_bytes=(CONTEXT / "node-payload.tar").stat().st_size)))

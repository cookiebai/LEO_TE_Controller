"""Add only the FRR accounts required by the unchanged node programs."""
from pathlib import Path

backup = Path("/opt/leo-te-merge/originals/node")
for relative, names in {
    "etc/passwd": {"frr"},
    "etc/group": {"frr", "frrvty"},
    "etc/shadow": {"frr"},
    "etc/gshadow": {"frr", "frrvty"},
}.items():
    target = Path("/") / relative
    current = target.read_text()
    existing = {line.split(":", 1)[0] for line in current.splitlines()}
    source = (backup / relative).read_text().splitlines()
    selected = [line for line in source if line.split(":", 1)[0] in names]
    if {line.split(":", 1)[0] for line in selected} != names:
        raise RuntimeError(f"Source FRR account missing: {relative}")
    if existing & names:
        raise RuntimeError(f"Unexpected existing FRR account: {relative}")
    if relative in ("etc/passwd", "etc/group"):
        identifiers = {line.split(":")[2] for line in current.splitlines() if line}
        if any(line.split(":")[2] in identifiers for line in selected):
            raise RuntimeError(f"FRR numeric identity conflict: {relative}")
    with target.open("a") as stream:
        if current and not current.endswith("\n"):
            stream.write("\n")
        stream.write("\n".join(selected) + "\n")

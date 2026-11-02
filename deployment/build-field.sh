#!/usr/bin/env bash
# Development-side build only. The field site loads the finished archive.
set -euo pipefail
LEO_SOURCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
LEO_FIELD_BUILD_DIR=${1:-"$LEO_SOURCE_DIR/../integration-artifacts/field-20261029-build"}
LEO_FIELD_IMAGE=${LEO_FIELD_IMAGE:-leo-te-controller:field-20261029}
mkdir -p "$LEO_FIELD_BUILD_DIR"
LEO_FIELD_BUILD_DIR=$(cd -- "$LEO_FIELD_BUILD_DIR" && pwd -P)
cd "$LEO_SOURCE_DIR"
gcc -O2 -Wall -Wextra -Werror -static integration/field_probe.c -lm -o "$LEO_FIELD_BUILD_DIR/field-udp6-probe"
CGO_ENABLED=0 go build -trimpath -ldflags='-s -w' -o "$LEO_FIELD_BUILD_DIR/leo-te-queue-probe" integration/field_queue_probe.go
python3 - "$LEO_SOURCE_DIR" "$LEO_FIELD_BUILD_DIR" <<'PY'
from pathlib import Path
import hashlib,shutil,sys,tarfile,urllib.request
r,b=map(Path,sys.argv[1:]);archive=b/'docker-27.5.1.tgz'
if not archive.exists():
    with urllib.request.urlopen('https://download.docker.com/linux/static/stable/x86_64/docker-27.5.1.tgz',timeout=90) as src,archive.open('wb') as out:
        shutil.copyfileobj(src,out)
with archive.open('rb') as stream:
    digest=hashlib.sha256()
    for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
    assert digest.hexdigest()=='4f798b3ee1e0140eab5bf30b0edc4e84f4cdb53255a429dc3bbae9524845d640','Docker CLI download checksum mismatch'
with tarfile.open(archive) as source:
    (b/'docker').write_bytes(source.extractfile('docker/docker').read())
(b/'docker').chmod(0o755)
license=b/'DOCKER-CLI-LICENSE'
if not license.exists():
    with urllib.request.urlopen('https://raw.githubusercontent.com/docker/cli/v27.5.1/LICENSE',timeout=30) as source:
        license.write_bytes(source.read())
c=b/'context'
if c.exists():shutil.rmtree(c)
(c/'app/integration').mkdir(parents=True);(c/'app/tests').mkdir();(c/'bin').mkdir()
for name in ['__init__.py','controller.py','lsdb.py','native_queues.py','manage.py','benchmark.py','field.py','queue_probe.py','udp6_probe.c','field_probe.c','field_queue_probe.go']:
    shutil.copy2(r/'integration'/name,c/'app/integration'/name)
for name in ['fixtures','vendor/queue-20261027']:
    shutil.copytree(r/'integration'/name,c/'app/integration'/name)
for name in ['python_te_solver','config']:
    shutil.copytree(r/name,c/'app'/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
for name in ['test_integration.py','test_native_queues.py','test_field.py']:
    shutil.copy2(r/'tests'/name,c/'app/tests'/name)
for name in ['field-udp6-probe','leo-te-queue-probe']:shutil.copy2(b/name,c/'bin'/name)
for name in ['docker','DOCKER-CLI-LICENSE']:shutil.copy2(b/name,c/name)
shutil.copy2(r/'Dockerfile.field',c/'Dockerfile')
shutil.copy2(r/'README_FIELD_20261029.md',c/'app/README.md')
PY
docker build --network none -t "$LEO_FIELD_IMAGE" "$LEO_FIELD_BUILD_DIR/context"
docker run --rm --network none --entrypoint python3 "$LEO_FIELD_IMAGE" -W ignore::ResourceWarning -m unittest discover -s /app/tests -v

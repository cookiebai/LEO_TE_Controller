#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
TE_WORK_DIR=${LEO_WORK_DIR:-$SCRIPT_DIR}
TE_IMAGE=${LEO_TE_IMAGE:-leo-te-integrated:full-20261027}
mkdir -p "$TE_WORK_DIR/results"
TE_RUNTIME_FLAGS=(--network none)
if [[ ${1:-} == deploy ]]; then
    TE_RUNTIME_FLAGS=(--privileged --network host --pid host --env TINI_SUBREAPER=1)
fi
exec docker run --rm "${TE_RUNTIME_FLAGS[@]}" \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$TE_WORK_DIR:/work" \
    -v "$TE_WORK_DIR/results:/output" \
    "$TE_IMAGE" "$@"

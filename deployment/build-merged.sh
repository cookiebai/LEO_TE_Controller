#!/usr/bin/env bash
# Rebuild the additive merge using dedicated, generated build files.
set -euo pipefail
LEO_SOURCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
LEO_MERGE_DIR=${1:-"$LEO_SOURCE_DIR/../integration-artifacts/merged-single-rebuild"}
mkdir -p "$LEO_MERGE_DIR"
LEO_MERGE_DIR=$(cd -- "$LEO_MERGE_DIR" && pwd -P)
if [[ "$LEO_MERGE_DIR" == / || "$LEO_MERGE_DIR" == "$LEO_SOURCE_DIR" || "$LEO_MERGE_DIR" == "$LEO_SOURCE_DIR/deployment/merged" ]]; then
    echo 'Choose a dedicated build directory.' >&2
    exit 1
fi
LEO_EXPORT_CONTAINER=
cleanup() {
    if [[ -n "$LEO_EXPORT_CONTAINER" ]]; then
        docker rm "$LEO_EXPORT_CONTAINER" >/dev/null
    fi
}
trap cleanup EXIT
mkdir -p "$LEO_MERGE_DIR/context"
cp "$LEO_SOURCE_DIR"/deployment/merged/*.py "$LEO_MERGE_DIR/"
for LEO_CONTEXT_FILE in Dockerfile setup-runtime.py leo-te-merged-entrypoint; do
    cp "$LEO_SOURCE_DIR/deployment/merged/context/$LEO_CONTEXT_FILE" "$LEO_MERGE_DIR/context/"
done
export_rootfs() {
    LEO_EXPORT_CONTAINER=$(docker create --network none --entrypoint /bin/sh "$1")
    docker export -o "$LEO_MERGE_DIR/$2-rootfs.tar" "$LEO_EXPORT_CONTAINER"
    docker rm "$LEO_EXPORT_CONTAINER" >/dev/null
    LEO_EXPORT_CONTAINER=
}
export_rootfs leo-te-controller:field-20261029 controller
export_rootfs frr-auto66-lite:new-20261029 node
python3 "$LEO_MERGE_DIR/assemble.py"
docker build --network none -t leo-te-controller:merged-frr-20261030 "$LEO_MERGE_DIR/context"

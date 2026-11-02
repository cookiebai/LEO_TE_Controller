#!/usr/bin/env bash
set -euo pipefail
# Keep the task-1 invocation and JSON shape. Structured counters avoid the
# vendor text parser's inability to handle all tc size presentation formats.
if command -v python3 >/dev/null && [[ -f /queue-probe.py ]]; then
    exec python3 /queue-probe.py "$@"
fi
exec bash /queue-probe-original.sh "$@"

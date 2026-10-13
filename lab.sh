#!/usr/bin/env bash
set -euo pipefail
script_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$script_root/../.venv/bin/python" "$script_root/scripts/lab.py" "$@"

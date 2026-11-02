#!/usr/bin/env bash
set -euo pipefail
cd /app
mkdir -p /run
case "${1:---help}" in
    node) echo node > /run/leo-te-role ;;
    *) echo control > /run/leo-te-role ;;
esac
case "${1:---help}" in
    node)
        shift
        # Keep task-1/3 startup. Their platform owns topology and interfaces.
        exec /root/entrypoint.sh "$@"
        ;;
    manage)
        shift
        exec python3 -m integration.manage "$@"
        ;;
    deploy)
        shift
        exec python3 -m integration.deploy "$@"
        ;;
    controller)
        shift
        exec python3 -m integration.controller "$@"
        ;;
    benchmark)
        shift
        exec python3 -m integration.benchmark "$@"
        ;;
    --help|-h|help)
        cat <<'EOF'
LEO TE task-1/3 integrated image (2026-10-27)
  node                    Original task-1/3 node startup
  manage --help           Install/start collectors on the EXISTING topology
  deploy --help           Original task-1 cfg deployment package (host net/pid required)
  controller --help       LSDB queues -> Lyapunov -> native SRv6 commands
  benchmark --help        Repeated IPv6 UDP baseline/TE throughput comparison
Uses the supplied cfg unchanged. No generated replacement topology. No OpenSN.
EOF
        ;;
    *) exec python3 -m integration.controller "$@" ;;
esac

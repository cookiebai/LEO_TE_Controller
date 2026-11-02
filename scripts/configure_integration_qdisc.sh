#!/usr/bin/env bash
# Run in the namespace that owns the real link TX queue.
set -euo pipefail
usage() {
    cat <<'EOF'
Usage: configure_integration_qdisc.sh IFACE [--apply] [--rate 16mbit]
       [--burst 32kb] [--latency 100ms] [--delay 10ms]
Default: inspect only. --apply adds TBF only to a noqueue/empty root.
Existing managed root qdiscs are preserved; change them in the platform.
--delay optionally adds a netem child; do not duplicate platform link delay.
EOF
}
if [[ $# -eq 0 || "$1" == --help ]]; then usage; exit 0; fi
iface=$1; shift
apply=0; rate=16mbit; burst=32kb; latency=100ms; delay=
while [[ $# -gt 0 ]]; do
    case "$1" in
        --apply) apply=1; shift ;;
        --rate|--burst|--latency|--delay)
            [[ $# -ge 2 ]] || { usage; exit 2; }
            case "$1" in
                --rate) rate=$2 ;; --burst) burst=$2 ;;
                --latency) latency=$2 ;; --delay) delay=$2 ;;
            esac
            shift 2 ;;
        *) usage; exit 2 ;;
    esac
done
ip link show dev "$iface" >/dev/null
tc -s qdisc show dev "$iface"
[[ $apply -eq 1 ]] || exit 0
if tc qdisc show dev "$iface" | awk '$0 ~ / root / && $2 != "noqueue" { found=1 } END { exit !found }'; then
    echo "Existing root qdisc found; retain it and configure through its owning platform." >&2
    exit 1
fi
tc qdisc add dev "$iface" root handle 1: tbf rate "$rate" burst "$burst" latency "$latency"
if [[ -n "$delay" ]]; then
    tc qdisc add dev "$iface" parent 1:1 handle 10: netem delay "$delay" limit 1000
fi
tc -j -s qdisc show dev "$iface"

#!/usr/bin/env bash
#
# One-command OpenSN baseline -> SRv6-TE experiment.
#
# Default:
#   ./run_full_experiment.sh
#
# Stable two-cycle run:
#   ./run_full_experiment.sh --cycles 2
#
# Supports both the 6x11 (66+2) and 12x10 (120+2) topologies. All persistent
# data is written below results/runs/<timestamp>/ and the
# results/latest symlink always points at the newest run. Transient probes,
# shared flow files, Python bytecode, SRv6 routes, Redis policy state, and the
# queue monitor owned by this script are cleaned on every exit.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPEN_SN_DIR="${OPEN_SN_DIR:-$(dirname "$SCRIPT_DIR")/OpenSN-Library}"
LAB_ROOT="$(dirname "$SCRIPT_DIR")"
PYTHON_BIN="${PYTHON_BIN:-$LAB_ROOT/.venv/bin/python}"
TMP_RESULT_DIR="$SCRIPT_DIR/results/tmp"
export NO_PROXY="127.0.0.1,localhost,${NO_PROXY:-}"
export no_proxy="$NO_PROXY"
export TMPDIR="$LAB_ROOT/tmp"
mkdir -p "$TMPDIR" "$SCRIPT_DIR/results" "$LAB_ROOT/runtime"

CYCLES="${TE_CYCLES:-1}"
MEASURE_SECONDS="${TE_MEASURE_SECONDS:-20}"
DEMAND_TOTAL_KBPS="${TE_DEMAND_TOTAL_KBPS:-76400}"
MIN_AVAILABLE_MIB="${TE_MIN_AVAILABLE_MIB:-350}"
MAX_MEMORY_STALL_PERCENT="${TE_MAX_MEMORY_STALL_PERCENT:-5}"
RESOURCE_WAIT_SECONDS="${TE_RESOURCE_WAIT_SECONDS:-120}"
TARGET_LOW_PERCENT="${TE_TARGET_LOW_PERCENT:-17}"
TARGET_HIGH_PERCENT="${TE_TARGET_HIGH_PERCENT:-23}"
REPORT_ARGS=()
if [[ "${TE_REPORT_ONLY:-0}" == 1 ]]; then
    REPORT_ARGS+=(--report-only)
fi
KEEP_POLICY=0

usage() {
    cat <<'EOF'
用法：
  ./run_full_experiment.sh
  ./run_full_experiment.sh --cycles 2
  ./run_full_experiment.sh --keep-policy

选项：
  --cycles N      测量周期数，默认 1
  --keep-policy   结束后保留 SRv6 路由和 Redis 策略，默认会清理
  -h, --help      显示帮助

常用环境变量：
  TE_MEASURE_SECONDS=20
  TE_DEMAND_TOTAL_KBPS=76400
  TE_MIN_AVAILABLE_MIB=350
  TE_MAX_MEMORY_STALL_PERCENT=5
  TE_RESOURCE_WAIT_SECONDS=120
  TE_TARGET_LOW_PERCENT=17
  TE_TARGET_HIGH_PERCENT=23
  PYTHON_BIN=/path/to/venv/bin/python
EOF
}

while (($#)); do
    case "$1" in
        --cycles)
            if (($# < 2)); then
                echo "错误：--cycles 后需要一个正整数" >&2
                exit 2
            fi
            CYCLES="$2"
            shift 2
            ;;
        --keep-policy)
            KEEP_POLICY=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "错误：未知参数 $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if ! [[ "$CYCLES" =~ ^[1-9][0-9]*$ ]]; then
    echo "错误：cycles 必须是正整数，当前值为 $CYCLES" >&2
    exit 2
fi

exec 9>"$LAB_ROOT/runtime/experiment.lock"
if ! flock -n 9; then
    echo "错误：已经有实验或部署正在运行" >&2
    exit 1
fi

RUN_TAG="$(date +%Y%m%d_%H%M%S)_$$"
RUNS_DIR="$SCRIPT_DIR/results/runs"
RUN_DIR="$RUNS_DIR/$RUN_TAG"
RUNTIME_DIR="$RUN_DIR/runtime"
RESULT_PATH="$RUN_DIR/result.json"
CONSOLE_LOG="$RUN_DIR/console.log"
SUMMARY_PATH="$RUN_DIR/summary.txt"
QUEUE_LOG="$RUN_DIR/queue_monitor.log"
RAW_ARCHIVE="$RUN_DIR/raw_flow_files.tar.gz"
PYCACHE_DIR="$RUN_DIR/pycache"
QUEUE_PID=""
WEB_PID=""
CLEANUP_DONE=0

mkdir -p "$RUNTIME_DIR"
ln -sfn "$RUN_DIR" "$SCRIPT_DIR/results/latest"
# Keep the log pipe alive while the parent handles terminal Ctrl+C / TERM.
exec > >(trap '' INT TERM; exec tee -a "$CONSOLE_LOG") 2>&1

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

archive_and_clear_shared_flow_files() {
    local archive_path="$1"
    local container
    container="$(
        docker ps --filter name=Satellite --format '{{.Names}}' |
        head -n 1
    )"
    if [[ -z "$container" ]]; then
        return 0
    fi
    if docker exec "$container" sh -c \
        'test -d /share/user/te_experiment &&
         find /share/user/te_experiment -mindepth 1 -print -quit |
         grep -q .' >/dev/null 2>&1; then
        docker exec "$container" \
            tar -C /share/user/te_experiment -cf - . |
            gzip -1 > "$archive_path"
    fi
    docker exec "$container" sh -c \
        'if test -d /share/user/te_experiment; then
             find /share/user/te_experiment -mindepth 1 -delete;
         fi' >/dev/null 2>&1 || true
}

archive_and_clear_old_tmp() {
    if [[ ! -d "$TMP_RESULT_DIR" ]]; then
        return 0
    fi
    if find "$TMP_RESULT_DIR" -mindepth 1 -print -quit | grep -q .; then
        tar -C "$TMP_RESULT_DIR" -czf \
            "$RUN_DIR/recovered_tmp_before_run.tar.gz" .
    fi
    find "$TMP_RESULT_DIR" -mindepth 1 -delete
}

remove_exited_experiment_containers() {
    local stale_containers=()
    mapfile -t stale_containers < <(
        {
            docker ps -aq \
                --filter status=exited --filter name=Satellite_
            docker ps -aq \
                --filter status=exited --filter name=GroundStation_
        } | sort -u
    )
    if ((${#stale_containers[@]})); then
        docker rm "${stale_containers[@]}" >/dev/null 2>&1 || true
        log "已删除 ${#stale_containers[@]} 个退出状态的旧实验容器"
    fi
}

cleanup() {
    local original_rc=$?
    if ((CLEANUP_DONE)); then
        return
    fi
    CLEANUP_DONE=1
    trap - EXIT INT TERM
    set +e

    log "开始归档和清理实验临时资源"

    if [[ -n "$WEB_PID" ]]; then
        if ((original_rc == 0)) && ! kill -0 "$WEB_PID" 2>/dev/null; then
            log "错误：网页健康检查进程提前退出"
            original_rc=1
        fi
        kill "$WEB_PID" 2>/dev/null
        wait "$WEB_PID" 2>/dev/null
        if grep -q '"ok": false' "$RUN_DIR/frontend_health.jsonl"; then
            log "错误：测量期间网页健康检查失败，参见 frontend_health.jsonl"
            original_rc=1
        fi
    fi

    if [[ -n "$QUEUE_PID" ]] && kill -0 "$QUEUE_PID" 2>/dev/null; then
        kill "$QUEUE_PID" 2>/dev/null
        wait "$QUEUE_PID" 2>/dev/null
        log "已停止本次启动的 queue_monitor，PID=$QUEUE_PID"
    fi

    PYTHONPYCACHEPREFIX="$PYCACHE_DIR" \
    LEO_TE_RUNTIME_DIR="$RUNTIME_DIR" \
    "$PYTHON_BIN" - "$SCRIPT_DIR" "$KEEP_POLICY" <<'PY'
import redis
import sys
from concurrent.futures import ThreadPoolExecutor

root, keep_policy = sys.argv[1], int(sys.argv[2])
sys.path.insert(0, root)
import run_te_experiment as experiment

try:
    experiment.kill_all_iperf()
    experiment.stop_existing_controllers()
except Exception as exc:
    print(f"[cleanup] 瞬态流量进程清理警告: {exc}")

def remove_probe(container):
    experiment.run(
        ["docker", "exec", container, "rm", "-f",
         experiment.CONTAINER_UDP_PROBE],
        timeout=10,
        check=False,
    )

with ThreadPoolExecutor(max_workers=8) as executor:
    list(executor.map(remove_probe, experiment.satellite_containers()))

if not keep_policy:
    try:
        deleted = experiment.clean_srv6_encap_routes()
        client = redis.Redis(
            host="127.0.0.1", port=6379,
            decode_responses=True, socket_timeout=5,
        )
        client.delete(*experiment.POLICY_KEYS)
        client.delete("te:mode")
        print(f"[cleanup] 已删除 {deleted} 条 SRv6 encap 路由并清空策略状态")
    except Exception as exc:
        print(f"[cleanup] SRv6/策略清理警告: {exc}")
else:
    print("[cleanup] 按 --keep-policy 要求保留 SRv6 路由和策略")
PY

    if ! "$PYTHON_BIN" "$SCRIPT_DIR/scripts/fixed_neighbors.py" restore "$RUN_DIR/fixed_neighbors.json"; then
        log "错误：固定邻居恢复失败；保留恢复记录，不能验收为成功"
        original_rc=1
    fi

    archive_and_clear_shared_flow_files "$RAW_ARCHIVE"
    find "$TMP_RESULT_DIR" -mindepth 1 -delete 2>/dev/null
    rm -rf "$PYCACHE_DIR"
    remove_exited_experiment_containers

    "$PYTHON_BIN" "$SCRIPT_DIR/scripts/record_run.py" "$RUN_DIR" "$original_rc"

    log "长期数据保留在 $RUN_DIR"
    log "临时文件、探针和本次队列监控已释放"
    exit "$original_rc"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

for command in docker redis-cli grep gzip tar flock; do
    if ! command -v "$command" >/dev/null 2>&1; then
        log "错误：缺少命令 $command"
        exit 1
    fi
done
if [[ ! -x "$PYTHON_BIN" ]] && ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    log "错误：Python不可执行：$PYTHON_BIN"
    exit 1
fi

log "本次运行目录：$RUN_DIR"
"$PYTHON_BIN" "$SCRIPT_DIR/scripts/record_run.py" "$RUN_DIR" start
if [[ "${TE_REQUIRE_WEB:-0}" == 1 ]]; then
    "$PYTHON_BIN" -u "$SCRIPT_DIR/scripts/watch_frontend.py" > "$RUN_DIR/frontend_health.jsonl" 9>&- &
    WEB_PID=$!
fi
log "检查 Redis 和 OpenSN 容器"

if [[ "$(redis-cli --raw ping 2>/dev/null || true)" != "PONG" ]]; then
    log "错误：Redis 未运行"
    exit 1
fi

SATELLITES="$(
    docker ps --filter name=Satellite --format '{{.Names}}' | wc -l
)"
GROUND_STATIONS="$(
    docker ps --filter name=GroundStation --format '{{.Names}}' | wc -l
)"
EXPECTED_SATELLITES="${TE_EXPECTED_SATELLITES:-120}"
EXPECTED_GROUND_STATIONS="${TE_EXPECTED_GROUND_STATIONS:-2}"
if [[ "$SATELLITES" -ne "$EXPECTED_SATELLITES" ]] ||
   [[ "$GROUND_STATIONS" -ne "$EXPECTED_GROUND_STATIONS" ]]; then
    log "错误：OpenSN 节点不完整，Satellite=$SATELLITES/$EXPECTED_SATELLITES，GroundStation=$GROUND_STATIONS/$EXPECTED_GROUND_STATIONS"
    exit 1
fi
EXPECTED_TELEMETRY_NODES=$((SATELLITES + GROUND_STATIONS))
log "识别到拓扑：Satellite=$SATELLITES，GroundStation=$GROUND_STATIONS"

DEMANDS="$(
    "$PYTHON_BIN" - <<'PY'
import json
import redis
r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
print(len(json.loads(r.get("te:demands") or "[]")))
PY
)"
if [[ "$DEMANDS" -ne 24 ]]; then
    log "错误：Redis 流量需求为 $DEMANDS/24，需要先运行 Standard 初始化"
    exit 1
fi

log "归档并清除上一次遗留的临时测量文件"
archive_and_clear_old_tmp
archive_and_clear_shared_flow_files \
    "$RUN_DIR/recovered_raw_before_run.tar.gz"

log "采集真实拓扑，供首次队列遥测使用"
"$PYTHON_BIN" -u "$SCRIPT_DIR/python_bgp_gateway/topo_sniffer.py" --once

if [[ "${TE_FIXED_NEIGHBORS:-0}" == 1 ]]; then
    log "为固定拓扑 A/B 测量临时固定真实对端邻居（两阶段相同，退出时恢复）"
    "$PYTHON_BIN" "$SCRIPT_DIR/scripts/fixed_neighbors.py" pin "$RUN_DIR/fixed_neighbors.json"
fi

log "确保只运行一个本次专用的 queue_monitor"
mapfile -t OLD_QUEUE_PIDS < <(
    "$PYTHON_BIN" - "$OPEN_SN_DIR" <<'PY'
from pathlib import Path
import sys
target = (Path(sys.argv[1]) / 'tools/queue_monitor.py').resolve()
for entry in Path('/proc').iterdir():
    if not entry.name.isdigit():
        continue
    try:
        args = [v.decode() for v in (entry / 'cmdline').read_bytes().split(b'\0') if v]
        if args and Path(args[0]).name.startswith('python'):
            cwd = (entry / 'cwd').resolve()
            if any((cwd / arg).resolve() == target for arg in args[1:] if arg.endswith('.py')):
                print(entry.name)
    except (OSError, ValueError, RuntimeError):
        continue
PY
)
for pid in "${OLD_QUEUE_PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
done
for pid in "${OLD_QUEUE_PIDS[@]}"; do
    for _ in {1..20}; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.1
    done
done

(
    cd "$OPEN_SN_DIR"
    exec "$PYTHON_BIN" -u tools/queue_monitor.py 9>&-
) >>"$QUEUE_LOG" 2>&1 &
QUEUE_PID=$!
log "queue_monitor PID=$QUEUE_PID"

"$PYTHON_BIN" - "$QUEUE_PID" "$EXPECTED_TELEMETRY_NODES" <<'PY'
import redis
import os
import sys
import time

pid = int(sys.argv[1])
expected_nodes = int(sys.argv[2])
r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
deadline = time.monotonic() + 30
expected = set(__import__('subprocess').check_output(
    ['docker', 'ps', '--format', '{{.Names}}'], text=True).splitlines())
expected = {name for name in expected if name.startswith(('Satellite_', 'GroundStation_'))}
while time.monotonic() < deadline:
    try:
        os.kill(pid, 0)
        updated = float(r.get("telemetry:queue:last_update_unix") or 0)
        actual = {key.removeprefix('telemetry:queue:') for key in r.scan_iter('telemetry:queue:*')
                  if r.type(key) == 'hash'}
        nodes = len(actual & expected)
        if nodes == expected_nodes and expected <= actual and time.time() - updated < 10:
            print(f"队列遥测就绪：nodes={nodes}, age={time.time() - updated:.2f}s")
            raise SystemExit(0)
    except redis.RedisError:
        pass
    time.sleep(1)
raise SystemExit(f"queue_monitor PID={pid} 在30秒内未生成新鲜遥测")
PY

export LEO_TE_RUNTIME_DIR="$RUNTIME_DIR"
export PYTHONPYCACHEPREFIX="$PYCACHE_DIR"

log "启动完整 baseline -> 求解 -> SRv6下发 -> Ping -> TE 流程"
set +e
"$PYTHON_BIN" -u "$SCRIPT_DIR/run_te_experiment.py" \
    --cycles "$CYCLES" \
    --protocol udp-native \
    --demand-total-kbps "$DEMAND_TOTAL_KBPS" \
    --measure-seconds "$MEASURE_SECONDS" \
    --warmup-samples 2 \
    --max-samples 0 \
    --min-samples 10 \
    --target-low-percent "$TARGET_LOW_PERCENT" \
    --target-high-percent "$TARGET_HIGH_PERCENT" \
    --max-improvement-stddev-percent 3 \
    --max-phase-cv-percent 5 \
    --min-sent-load-ratio 0.98 \
    --max-paired-sent-difference-percent 1 \
    --min-host-available-mib "$MIN_AVAILABLE_MIB" \
    --max-host-memory-stall-percent "$MAX_MEMORY_STALL_PERCENT" \
    --resource-wait-seconds "$RESOURCE_WAIT_SECONDS" \
    "${REPORT_ARGS[@]}" \
    --result "$RESULT_PATH"
RUN_RC=$?
set -e

if [[ -f "$RESULT_PATH" ]]; then
    "$PYTHON_BIN" - "$RESULT_PATH" <<'PY' | tee "$SUMMARY_PATH"
import json
import sys

path = sys.argv[1]
with open(path) as stream:
    data = json.load(stream)

print("\n========== 实验结果 ==========")
print("状态:", data.get("status"))
print("已完成周期:", data.get("completed_cycles"))
print("平均提升:", data.get("mean_improvement_percent"))
print("提升标准差:", data.get("improvement_stddev_percent"))
print("阶段稳定:", data.get("phase_stable"))
print("配对负载有效:", data.get("paired_load_valid"))
target_low = data.get("target_low_percent")
target_high = data.get("target_high_percent")
print(f"达到{target_low}%-{target_high}%目标:", data.get("target_met"))
current = data.get("experiment", {}).get("current_cycle")
if current:
    print("中断阶段:", current.get("status"))
for cycle in data.get("cycles", []):
    baseline = cycle["baseline"]
    te = cycle["te"]
    policy = cycle["policy"]
    ping = cycle["ping"]
    print(f"\n周期 {cycle['cycle']}:")
    print(f"  Baseline: {baseline['total_mean_mbps']:.3f} Mbps, "
          f"CV={baseline['total_cv'] * 100:.3f}%")
    print(f"  TE:       {te['total_mean_mbps']:.3f} Mbps, "
          f"CV={te['total_cv'] * 100:.3f}%")
    print(f"  提升:     {cycle['improvement_percent']:.3f}%")
    print(f"  策略:     {policy['desired']}/"
          f"{policy['applied']}/{policy['queued']}")
    print(f"  Ping:     IPv6 {ping['srv6_ipv6_ping_ok']}/"
          f"{ping['policies']}, IPv4-over-SRv6 "
          f"{ping['srv6_ipv4_ping_ok']}/{ping['policies']}")
print("\n完整 JSON:", path)
PY
else
    log "未生成结果 JSON，请查看 $CONSOLE_LOG"
fi

exit "$RUN_RC"

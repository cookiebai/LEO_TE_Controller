#!/bin/bash
# LEO_TE_Controller 完整启动脚本
#
# 功能：
# 1. 启动 Redis（如果未运行）
# 2. 启动宿主机队列监控 (queue_monitor.py)
# 3. 启动 C++ 保底 TE 引擎
# 4. 启动 BGP-LS 监听网关
# 5. 启动 SRv6 策略下发器
# 6. 启动 Lyapunov 数学优化引擎
#
# 使用方法：
#   ./run_all.sh          # 前台运行所有服务
#   ./run_all.sh start    # 后台运行所有服务
#   ./run_all.sh stop     # 停止所有服务

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

REDIS_HOST="${REDIS_HOST:-127.0.0.1}"
REDIS_PORT="${REDIS_PORT:-6379}"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

start_redis() {
    log "检查 Redis..."
    if redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" ping >/dev/null 2>&1; then
        log "Redis 已在运行 ($REDIS_HOST:$REDIS_PORT)"
    else
        log "启动 Redis..."
        if command -v redis-server &>/dev/null; then
            redis-server --daemonize yes --host "$REDIS_HOST" --port "$REDIS_PORT"
            sleep 1
            log "Redis 已启动"
        else
            log "错误: Redis 未安装"
            exit 1
        fi
    fi
}

start_queue_monitor() {
    log "启动宿主机队列监控..."
    cd "$PROJECT_ROOT/OpenSN-Library"
    nohup /usr/bin/python3 tools/queue_monitor.py \
        --redis-host "$REDIS_HOST" \
        --redis-port "$REDIS_PORT" \
        > /tmp/queue_monitor.log 2>&1 &
    log "队列监控已启动 (PID: $!)"
}

start_cpp_engine() {
    log "启动 C++ 快速保底 TE 引擎..."
    if [ -f "$PROJECT_ROOT/LEO_TE_Controller/build/te_engine" ]; then
        nohup "$PROJECT_ROOT/LEO_TE_Controller/build/te_engine" \
            > /tmp/cpp_te.log 2>&1 &
        log "C++ 引擎已启动 (PID: $!)"
    else
        log "警告: C++ 引擎未构建，跳过"
    fi
}

start_bgp_gateway() {
    log "启动 BGP-LS 监听网关..."
    cd "$PROJECT_ROOT/LEO_TE_Controller"
    nohup /usr/bin/python3 python_bgp_gateway/bgp_ls_receiver.py \
        > /tmp/bgp_rx.log 2>&1 &
    log "BGP 网关已启动 (PID: $!)"
}

start_sr_policy() {
    log "启动 SRv6 策略下发器..."
    cd "$PROJECT_ROOT/LEO_TE_Controller"
    nohup /usr/bin/python3 python_bgp_gateway/sr_policy_sender.py \
        > /tmp/sr_tx.log 2>&1 &
    log "SR 策略下发器已启动 (PID: $!)"
}

start_lyapunov() {
    log "启动 Lyapunov 数学优化引擎..."
    cd "$PROJECT_ROOT/LEO_TE_Controller/python_te_solver"
    nohup /usr/bin/python3 lyapunov_solver.py \
        > /tmp/lyapunov.log 2>&1 &
    log "Lyapunov 引擎已启动 (PID: $!)"
}

stop_all() {
    log "停止所有服务..."

    # 停止 Python 进程
    pkill -f "queue_monitor.py" && log "队列监控已停止" || true
    pkill -f "bgp_ls_receiver.py" && log "BGP 网关已停止" || true
    pkill -f "sr_policy_sender.py" && log "SR 策略下发器已停止" || true
    pkill -f "lyapunov_solver.py" && log "Lyapunov 引擎已停止" || true

    # 停止 C++ 进程
    pkill -f "te_engine" && log "C++ 引擎已停止" || true

    log "所有服务已停止"
}

status() {
    log "检查服务状态..."

    echo ""
    echo "=== Redis ==="
    redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" ping 2>/dev/null || echo "未运行"

    echo ""
    echo "=== Python 服务 ==="
    ps aux | grep -E "queue_monitor|bgp_ls|sr_policy|lyapunov" | grep -v grep || echo "无运行中的服务"

    echo ""
    echo "=== C++ 服务 ==="
    ps aux | grep "te_engine" | grep -v grep || echo "无运行中的服务"

    echo ""
    echo "=== 日志文件 ==="
    for logfile in /tmp/queue_monitor.log /tmp/bgp_rx.log /tmp/sr_tx.log /tmp/lyapunov.log /tmp/cpp_te.log; do
        if [ -f "$logfile" ]; then
            echo "$logfile: $(wc -l < "$logfile") 行"
        fi
    done
}

usage() {
    echo "用法: $0 [命令]"
    echo ""
    echo "命令:"
    echo "  start   后台启动所有服务"
    echo "  stop    停止所有服务"
    echo "  status  查看服务状态"
    echo "  (无)    前台运行所有服务"
    echo ""
    echo "环境变量:"
    echo "  REDIS_HOST  Redis 主机 (默认: 127.0.0.1)"
    echo "  REDIS_PORT  Redis 端口 (默认: 6379)"
}

case "${1:-}" in
    start)
        start_redis
        start_queue_monitor
        sleep 1
        start_bgp_gateway
        start_sr_policy
        start_lyapunov
        log "所有服务已启动"
        log "查看日志: tail -f /tmp/{queue_monitor,lyapunov}.log"
        ;;
    stop)
        stop_all
        ;;
    status)
        status
        ;;
    *)
        usage
        echo ""
        log "启动所有服务..."
        start_redis
        start_queue_monitor
        sleep 1
        start_bgp_gateway
        start_sr_policy
        start_lyapunov
        log "所有服务已启动 (前台运行，按 Ctrl+C 停止)"

        # 等待中断信号
        trap "stop_all; exit 0" INT TERM
        wait
        ;;
esac

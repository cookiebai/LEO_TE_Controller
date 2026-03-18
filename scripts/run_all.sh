#!/bin/bash
# scripts/run_all.sh

echo "启动 Redis..."
./start_redis.sh

echo "启动 C++ 快速保底 TE 引擎..."
nohup ../build/te_engine > /tmp/cpp_te.log 2>&1 &

echo "启动 BGP-LS 监听网关..."
nohup python3 ../python_bgp_gateway/bgp_ls_receiver.py > /tmp/bgp_rx.log 2>&1 &

echo "启动 SRv6 策略下发器..."
nohup python3 ../python_bgp_gateway/sr_policy_sender.py > /tmp/sr_tx.log 2>&1 &

# 【新增】启动核心 Lyapunov 数学引擎
echo "启动 Lyapunov 数学优化引擎..."
nohup python3 ../python_te_solver/lyapunov_solver.py > /tmp/lyapunov.log 2>&1 &

echo "所有服务已启动！"
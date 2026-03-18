#!/bin/bash

# 1. 启动 Redis
bash start_redis.sh
sleep 2

# 2. 编译并启动 C++ 核心引擎 (后台运行)
cd ../
mkdir -p build && cd build
cmake .. && make
./te_engine &
TE_PID=$!

# 3. 启动 Python SRv6 下发服务 (后台运行)
cd ../python_bgp_gateway
python3 sr_policy_sender.py &
SENDER_PID=$!

# 4. 启动 ExaBGP 接收 BGP-LS (前台运行，方便看日志)
echo "启动 BGP 监听网关..."
env exabgp.tcp.port=179 exabgp exabgp_conf.ini

# 捕获 Ctrl+C 退出时清理后台进程
trap "kill $TE_PID $SENDER_PID; exit" INT
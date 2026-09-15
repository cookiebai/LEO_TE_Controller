# OpenSN + SRv6-TE 完整运行步骤

更新日期：2026-09-15

## 使用说明

- 如果电脑或虚拟机刚刚重启，从第 1 步开始执行。
- 如果 66 个 Satellite 和 2 个 GroundStation 已经运行，直接执行第 7 步。
- 每一个代码块都应完整复制执行。
- 不要再单独启动 `topo_sniffer.py`、`queue_monitor.py`、
  `lyapunov_solver.py`、`sr_policy_sender.py` 或 `calc_throughput.py`。
  第 7 步的一键脚本已经包含这些功能。

## 1. 启动依赖服务

```bash
sudo systemctl start docker
sudo systemctl start redis-server

docker rm -f opensn_etcd 2>/dev/null || true

docker run -d --rm \
  --name opensn_etcd \
  -p 2379:2379 \
  realssd/opensn_etcd
```

检查 Redis：

```bash
redis-cli ping
```

正常输出：

```text
PONG
```

## 2. 清理上次重启后留下的旧节点

> 只有电脑或虚拟机重启后才执行本步骤。

```bash
docker rm -f opensn_node_daemon 2>/dev/null || true

{
  docker ps -aq --filter name=Satellite_
  docker ps -aq --filter name=GroundStation_
} | sort -u | xargs -r docker rm -f
```

## 3. 启动 NodeDaemon

```bash
TE_RUNTIME_DIR=$(mktemp -d /tmp/opensn-te-runtime.XXXXXX)

docker run -d --rm \
  --name opensn_node_daemon \
  --privileged \
  --network host \
  --pid host \
  -v /home/bai:/home/bai \
  -v "$TE_RUNTIME_DIR:$TE_RUNTIME_DIR" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -w "$TE_RUNTIME_DIR" \
  --entrypoint /home/bai/OpenSN-Library/NodeDaemon \
  ubuntu:22.04 \
  /home/bai/LEO_TE_Controller/config/experiment_daemon.json \
  experiment
```

等待约 5 秒，然后检查：

```bash
curl -sS http://127.0.0.1:8080/api/platform/status
```

返回 `"code":0` 表示 NodeDaemon 正常。

## 4. 上传拓扑配置

```bash
cd /home/bai/OpenSN-Library

curl -sS -X POST \
  http://127.0.0.1:8080/api/emulation/update \
  -H 'Content-Type: application/json' \
  --data-binary @example/test_topologies/emu_config.json

curl -sS -X POST \
  http://127.0.0.1:8080/api/emulation/topology \
  -H 'Content-Type: application/json' \
  --data-binary @example/test_topologies/topology_config_6_11.json

curl -sS -X POST \
  http://127.0.0.1:8080/api/emulation/start
```

三个请求都应返回：

```json
{"code":0,"message":"Success"}
```

等待节点启动：

```bash
sleep 60

docker ps --filter name=Satellite --format '{{.Names}}' | wc -l
docker ps --filter name=GroundStation --format '{{.Names}}' | wc -l
```

正常输出：

```text
66
2
```

## 5. 清理 Redis 旧实验状态

不要执行 `redis-cli flushall`。它会删除整个 Redis 数据库，容易误删其他数据。

只清理本实验的旧策略和运行状态：

```bash
redis-cli del \
  policy_queue \
  te:policy:last_signature \
  te:policy:desired \
  te:policy:changed_at \
  te:policy:applied_signature \
  te:policy:applied \
  te:mode \
  te:demands \
  te:flow:dst_ip

redis-cli set te:traffic:external_control 1
```

## 6. 启动 Standard 初始化仿真

```bash
cd /home/bai/OpenSN-Library/TopoConfigurators/Standard

nohup env \
  PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python \
  PYTHONPATH=/home/bai/OpenSN-Library/TopoConfigurators \
  ADDR=127.0.0.1 \
  PORT=8080 \
  OPENSN_FIXED_TIME=2026-09-14T16:25:00 \
  python3 -u main.py \
  > /tmp/standard.log 2>&1 &

STANDARD_PID=$!
echo "Standard PID=$STANDARD_PID"
```

等待 Standard 写入 24 条需求并让 OSPF 收敛：

```bash
sleep 90
```

检查需求数量：

```bash
redis-cli --raw get te:demands |
python3 -c 'import json,sys; print(len(json.load(sys.stdin)))'
```

正常输出：

```text
24
```

停止 Standard 和 NodeDaemon，释放内存：

```bash
kill "$STANDARD_PID" 2>/dev/null || true
wait "$STANDARD_PID" 2>/dev/null || true
docker stop --time 10 opensn_node_daemon
```

检查节点仍然运行：

```bash
docker ps --filter name=Satellite --format '{{.Names}}' | wc -l
docker ps --filter name=GroundStation --format '{{.Names}}' | wc -l
```

正常输出：

```text
66
2
```

## 7. 运行完整实验

以下一条命令会自动执行：

```text
拓扑嗅探
→ 队列数据上报
→ 清理旧 SRv6 路由
→ Baseline 吞吐量测量
→ 控制面路径计算
→ SRv6 策略下发
→ 24/24 IPv6 SID Ping
→ 24/24 IPv4-over-SRv6 Ping
→ TE 吞吐量测量
→ 计算提升百分比
→ 保存数据
→ 释放临时资源
```

执行：

```bash
/home/bai/LEO_TE_Controller/run_full_experiment.sh
```

单周期一般需要 2～4 分钟。

如果需要执行两周期稳定性测试：

```bash
/home/bai/LEO_TE_Controller/run_full_experiment.sh --cycles 2
```

## 8. 查看实验结果

查看最近一次实验的中文汇总：

```bash
cat /home/bai/LEO_TE_Controller/results/latest/summary.txt
```

查看完整运行日志：

```bash
less /home/bai/LEO_TE_Controller/results/latest/console.log
```

查看完整 JSON 数据：

```bash
python3 -m json.tool \
  /home/bai/LEO_TE_Controller/results/latest/result.json |
less
```

所有数据保存在：

```text
/home/bai/LEO_TE_Controller/results/latest/
```

主要文件：

```text
result.json
summary.txt
console.log
queue_monitor.log
runtime/baseline_1.json
runtime/te_1.json
runtime/solver.log
runtime/sender.log
raw_flow_files.tar.gz
```

## 9. 平时再次运行

只要电脑没有重启，并且节点数量仍然是 `66+2`，以后不需要再次执行
第 1～6 步。

直接执行：

```bash
/home/bai/LEO_TE_Controller/run_full_experiment.sh
```

## 10. 实验结束后的自动清理

第 7 步执行结束或报错退出时，一键脚本都会自动：

- 停止队列监控和流量探针。
- 停止残留求解器和策略下发器。
- 清除 SRv6 encap 路由。
- 清除 Redis 中的策略状态。
- 压缩保存原始流数据。
- 删除 Python 临时缓存。
- 删除退出状态的旧实验容器。

正在运行的 66 个 Satellite 和 2 个 GroundStation 会保留，方便下一次
继续一键运行。

如果需要在实验结束后保留 SRv6 策略用于检查：

```bash
/home/bai/LEO_TE_Controller/run_full_experiment.sh --keep-policy
```

## 最简流程

电脑或虚拟机重启后：

```text
1. 启动 Docker、Redis、etcd
2. 清理旧节点
3. 启动 NodeDaemon
4. 上传拓扑
5. 清理旧 TE 状态
6. 启动 Standard 并等待 90 秒
7. 执行 run_full_experiment.sh
8. 查看 results/latest/summary.txt
```

电脑没有重启：

```bash
/home/bai/LEO_TE_Controller/run_full_experiment.sh
```

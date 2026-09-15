# LEO TE + OpenSN SRv6 实验说明

## 一键运行（推荐）

当 66 个 Satellite、2 个 GroundStation、Redis 和 etcd 已启动并且
`te:demands` 中已有 24 条需求时，只需执行：

```bash
cd /home/bai/LEO_TE_Controller
./run_full_experiment.sh
```

两周期稳定性测试：

```bash
./run_full_experiment.sh --cycles 2
```

脚本自动检查环境、去重并启动队列监控、等待短时内存压力恢复、执行
baseline、控制面求解、SRv6 下发、双栈 Ping 和 TE 测量。每次结果统一
保存在 `results/runs/<时间戳>/`，`results/latest` 永远指向最近一次。
退出时会归档原始流文件，清理探针、临时 Python 缓存、SRv6 路由和
Redis 策略，并停止本次队列监控；同时只删除退出状态的旧实验容器，
不会删除当前运行的 66+2 个节点。需要在结束后现场检查策略时使用
`./run_full_experiment.sh --keep-policy`。

## 当前交接：2026-09-14（以本节及当前代码为准）

### 2026-09-15 单轮全流程验证

低并发完整环境和增强后的 runner 已连续完整跑通两次。最终留档结果：

```text
result_20260915_fullflow_with_paths.json
Baseline 54.91 Mbps，CV 0.30%，sent 76.39/76.39 Mbps
TE       76.36 Mbps，CV 0.35%，sent 76.38/76.39 Mbps
提升     39.07%
策略     desired=24, applied=24, queue=0
Ping     IPv6 SID=24/24, IPv4-over-SRv6=24/24
```

`target_met=false` 仅因为 39.07% 高于当前 17%–23% 目标；所有运行和
数据有效性门槛均通过，`--report-only` 因而返回退出码 0。此结果证明
流程可运行，不是“稳定约 20%”的最终验收。完整 JSON 现包含每流逐秒
数据、sender 统计、24 条 policy/path/SID 和求解器原始输出。

本次恢复了 3 个启动后 `ospfd` 僵死的节点。runner 现在会自动扫描
66 个卫星：仅对没有存活 ospfd 的节点保留 stale PID/socket、原地重启、
加载已有 FRR batch，并等待所有节点至少 100 条 OSPF 路由。SRv6 Ping
改为 4 并发及最多 3 轮退避，只重试失败策略，仍严格要求最终 24/24。

后面的 7 月记录为历史归档，不代表当前环境已经完成验收，历史 PID 不可直接使用。
当前 `controller.yaml` 为 `max_k_paths=24`；求解器使用全局单路径 min-max 模型。

### 当前真实状态

- 9 月 11 日修复 UDP 节拍后，76.4 Mbps 总供给负载下得到过有效单轮
  **21.90% 和 22.13%**；各阶段总吞吐 CV 约 0.18%–0.68%。
- 最后两轮确认被主动中断，只完成第 1 轮，尚无不中断的多轮最终验收结果。
- 9 月 14 日重建过 66 Satellite + 2 GroundStation，但随后机器发生重启。
  当前没有运行中的 OpenSN 实验容器、NodeDaemon、Standard 或队列监控。
- 本次启动期间发生严重内存压力；最新检查可用内存不足 100 MiB、swap
  耗尽，内核 16:58 有 OOM kill 记录。必须先释放内存或扩大 VM 内存，
  不能把这种状态下的吞吐下降作为有效 baseline。
- **当前目标未完成，不能宣称每次已经稳定提升约 20%。**

### 本次脚本改进

1. UDP 保留固定发包时间轴，不因调度暂停而静默丢掉应发包数。
2. 验证聚合及最差单流的实际/目标发送比例，默认必须 >=98%。
3. 验证配对实际供给负载差异，默认 <=1%；最终吞吐仍统计接收端。
4. 每一轮必须独立处于 17%–23%，不允许只用平均值隐藏越界周期。
5. 阶段 CV 默认 <=5%；轮间提升标准差默认 <=3pp。
6. 每轮完成后原子保存检查点。`status=in_progress` 及
   `target_met=false` 表示整次验收尚未完成，不是最终成功结果。
7. baseline 清理后再次核验 IPv4/IPv6 encap route 均为零。
8. 运行前刷新一次拓扑、拒绝旧节点快照和过期队列遥测。
9. 自动冻结当前项目的 Standard，退出时只恢复本次冻结的进程。
10. `--help` 和参数解析失败不再触发流量清理。
11. 增加主机资源保护，默认要求 MemAvailable >=512 MiB，内存
    full-stall avg10 <=5%；资源不足时在实验写操作前拒绝运行。
12. 采集每个节点的 IPv4 地址只执行一次 `ip`，默认采集并发降至 4。

新增的 `config/experiment_daemon.json` 关闭非测量必需的后台 monitor、
将节点创建并发降至 4；不改变 68 节点数量、拓扑或链路限速。
Standard 新增可选 `OPENSN_FIXED_TIME`，固定几何时刻及故障矩阵初始
时间步，默认动态模式不变。固定场景下得到的结果只适用于该场景，
不是任意日期、负载或拓扑都能提升约 20% 的保证。

### 恢复顺序与命令

先检查资源：

```bash
free -h
cat /proc/pressure/memory
docker ps --format '{{.Names}} {{.Status}}'
```

释放内存后，恢复 OpenSN 依赖、NodeDaemon 和拓扑。依赖启动命令及 API
见第 3 节，但不要直接执行会删除已有容器的依赖启动脚本。

新低并发 NodeDaemon 启动方案（已按源码检查，尚待资源恢复后运行验收）：

```bash
te_runtime_dir=$(mktemp -d /tmp/opensn-te-runtime.XXXXXX)
docker run --rm --privileged --network host --pid host \
  -v /home/bai:/home/bai \
  -v "$te_runtime_dir:$te_runtime_dir" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -w "$te_runtime_dir" \
  --entrypoint /home/bai/OpenSN-Library/daemon/opensn-daemon/NodeDaemon \
  ubuntu:22.04 \
  /home/bai/LEO_TE_Controller/config/experiment_daemon.json experiment
```

额外的 `experiment` 参数是为了兼容现有二进制只在 argc>2 时读取指定
配置的行为。运行数据在临时目录，不删除或覆盖原项目 runtime 数据。

启动 Standard 前禁止其自行生成背景 iperf：

```bash
redis-cli set te:traffic:external_control 1
cd /home/bai/OpenSN-Library/TopoConfigurators/Standard
env PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python \
  PYTHONPATH=/home/bai/OpenSN-Library/TopoConfigurators \
  ADDR=127.0.0.1 PORT=8080 \
  OPENSN_FIXED_TIME=2026-09-14T16:25:00 \
  python3 -u main.py
```

该新快照尚未完成吞吐校准；不要把 9 月 11 日的 76.4 Mbps 结果直接
当成此快照的最终证据。等待 66+2 容器、24 条当前需求及 OSPF 收敛。

另外一个终端保持队列采集运行：

```bash
cd /home/bai/OpenSN-Library
python3 -u tools/queue_monitor.py
```

先跑单轮以确定新快照下的有效供给负载，再保持相同配置跑 2–3 轮；
以下 76.4 Mbps 是上次校准点，当前复验的起始值：

```bash
cd /home/bai/LEO_TE_Controller
PYTHONPYCACHEPREFIX=/tmp/te_pycache python3 -u run_te_experiment.py \
  --cycles 2 --protocol udp-native --demand-total-kbps 76400 \
  --measure-seconds 20 --warmup-samples 2 --max-samples 12 --min-samples 10 \
  --target-low-percent 17 --target-high-percent 23 \
  --max-improvement-stddev-percent 3 --max-phase-cv-percent 5 \
  --min-sent-load-ratio 0.98 --max-paired-sent-difference-percent 1 \
  --result results/result_final.json
```

成功必须同时满足 `status=completed`、全部周期 24/24 策略、24/24 双栈
ping、`all_cycles_in_target=true`、`phase_stable=true`、
`paired_load_valid=true`、`improvement_stable=true`、`target_met=true`。
退出码 4 表示测量完成但未达到目标，1 表示运行/有效性错误，130 表示中断。
不可修改吞吐统计公式或筛掉越界轮次来获得 20%。

回归测试：

```bash
PYTHONPYCACHEPREFIX=/tmp/te_pycache python3 -m unittest discover -s tests -v
```

剩余工作：资源恢复后验证新启动方案，完成全量 OSPF/24 流 SRv6 健康
检查，验证策略切换后真实数据路径，完成连续多轮短时吞吐验收并保存
原始 JSON。检查点、资源保护等单元测试不能代替这些实网验收。

## 0. 2026-07-24 完成状态

### 0.1 最终配置与验收结果

2026-07-24 已完成控制面硬门槛和两次独立吞吐确认。最终配置为：

```yaml
max_k_paths: 6
capacity_rho: 0.90
congestion_penalty: 3.45
```

测量使用 `udp-native` 接收端逐秒统计，每阶段 32 秒，丢弃前 4 个
预热样本，保留 20 个样本。两次有效周期如下：

| 周期 | baseline | baseline CV | TE | TE CV | 提升 |
|---|---:|---:|---:|---:|---:|
| p3.45-A | 77.9092 Mbit/s | 2.632% | 92.6568 Mbit/s | 4.154% | 18.929% |
| p3.45-B | 76.2580 Mbit/s | 2.585% | 89.0828 Mbit/s | 4.721% | 16.818% |

汇总结果：

- 平均提升：**17.873%**，落入 17%-23% 验收区间。
- 周期间提升总体标准差：**1.056 个百分点**，小于 3pp。
- 两周期四个阶段均为 24/24 active flows，阶段总吞吐 CV 均小于 5%。
- 每周期均通过 `desired=24`、`applied=24`、`queue=0`。
- 每周期均通过 IPv6 SRv6 ping 24/24 和 IPv4-over-SRv6 ping 24/24。
- 第二周期单独提升为 16.818%，比单周期下限低 0.182pp；最终口径按
  README 约定的多周期平均提升和周期间标准差判定。

原始结果文件：

- `/tmp/leo_te_experiment/result_cycle1_p345.json`
- `/tmp/leo_te_experiment/result_confirm_p345.json`

复现实验命令：

```bash
PYTHONPYCACHEPREFIX=/tmp/te_pycache \
python3 -u /home/bai/LEO_TE_Controller/run_te_experiment.py \
  --cycles 2 \
  --protocol udp-native \
  --measure-seconds 32 \
  --warmup-samples 4 \
  --max-samples 20 \
  --min-samples 16 \
  --target-low-percent 17 \
  --target-high-percent 23 \
  --result /tmp/leo_te_experiment/result_final.json
```

### 0.2 本次新增修复

1. 修复 `TrafficManager` 复用上一轮仿真的 `te:flow:dst_ip`：
   缓存地址现在必须属于当前 receiver 的实际 `10.x` 地址，否则删除并重算。
2. 修复求解器的量纲错误：队列字节数先按链路容量换算为串行化时延
   （毫秒），再与链路时延组合，不再把原始 bytes 直接加到 ms。
3. 修复三个 FRR/OSPF 异常节点并完成全量健康检查；66/66 Satellite
   均有存活的 `ospfd` 和至少 100 条 OSPF 路由。
4. 当前稳定拓扑为 68 个容器、246 条有向链路，其中 240 UP、6 DOWN。
5. 调参样本（包括 p3.30 的失稳周期和 p4 的 26.00% 过强周期）仅用于
   路径阈值定标，未计入上述最终汇总。

### 0.3 运行结束要求

结束实验时应删除残留 SRv6 encap route 和瞬态 policy key，并恢复被冻结的
Standard 进程。`te:traffic:external_control=1` 可继续保留，使 Standard
只维护需求、不自行启动 iperf。

本次收尾后的实际状态：

- Standard PID 27907 已 `SIGCONT` 恢复；NodeDaemon 和 queue monitor 正常运行。
- `te:policy:desired=0`、`te:policy:applied_signature=0`、`policy_queue=0`。
- 全部 Satellite 中 IPv6/IPv4 `encap seg6` route 均为 0。
- `udp_flow_probe.py` 残留进程为 0，`te:traffic:external_control=1`。

## 0. 2026-07-23 交接状态（明天从这里继续）

> 本节是当前最高优先级的运行记录。后面的章节保留了早期方案和背景，
> 其中基于长期 iperf 日志打标的流程仅用于兼容；正式实验优先使用
> `run_te_experiment.py` 的同步、接收端、逐秒采样流程。

### 0.1 今天停止时的安全状态

- Standard 已退出；恢复 `SIGCONT` 后遇到一次 etcd connection timeout 并退出。
- `queue_monitor.py` 与 `topo_sniffer.py` 均已停止。
- 明天需重新启动 Standard 和 queue monitor；topo sniffer 建议先执行
  `--once`，正式验证期间不要让拓扑继续变化。
- 没有运行 `lyapunov_solver.py`、`sr_policy_sender.py` 或实验 runner。
- `te:traffic:external_control=1`，Standard 只写需求，不自行管理 iperf。
- 已杀掉残留 iperf/native UDP probe。
- 已删除 48 条中断预检遗留的 `encap seg6` route。
- 已清空以下瞬态策略状态：
  - `policy_queue=0`
  - `te:policy:desired=0`
  - `te:policy:applied_signature=0`
  - `te:policy:applied=0`
- 因此当前环境是安全的 baseline 起点，不是一个有效 TE 状态。

### 0.2 今天已经完成的代码改造

控制面数据流现在按以下框架实现：

```text
OpenSN bridge/tc
    -> topo_sniffer.py（真实容量、时延、UP/DOWN）
    -> Redis topo:link:*

host tc backlog/drop
    -> queue_monitor.py
    -> Redis telemetry:queue:*

TrafficManager
    -> Redis te:demands + te:flow:dst_ip

lyapunov_solver.py --once
    -> 单路径、可实际下发的 TE policy
    -> Redis te:policy:desired + policy_queue

sr_policy_sender.py --drain-and-exit
    -> 节点 SID / 每跳 SID route / IPv4-over-SRv6 encap
    -> Redis te:policy:applied*

run_te_experiment.py
    -> baseline 清理
    -> 24 流同步接收端采样
    -> 一次性求解与下发
    -> 24 条 IPv6 + IPv4-over-SRv6 ping
    -> TE 同步采样
    -> 增益、CV、重复周期标准差
```

已完成的关键修改：

1. `python_bgp_gateway/topo_sniffer.py`
   - 不再硬编码 1000 Mbit/s、10 ms。
   - 从 host veth 的 qdisc 读取真实 16 Mbit/s 和 2-14 ms 时延。
   - 写入 `local_iface`、`host_iface`、容量、时延、状态。
   - 拒绝容器扫描失败、需求端点缺失或链路数突降的残缺快照。
   - 新增 `--once`。
   - 已加入 host bridge `forwarding/disabled` 状态识别；今天最后一次快照为：
     - 68 个容器
     - 246 条有向链路
     - 216 UP
     - 30 DOWN

2. `tools/queue_monitor.py`
   - 改为读取 host veth 的真实 `tc -s qdisc`。
   - 不再读取容器内始终为 `noqueue` 的伪队列。
   - 将 backlog、drop 增量映射回节点接口。

3. `python_te_solver/lyapunov_solver.py`
   - 不再发布无法由单条内核 route 表达的分数多路径。
   - 按需求从大到小进行确定性单路径选择。
   - 使用时延、队列和投影链路利用率惩罚。
   - policy queue 使用原子替换，避免 sender 追旧策略。
   - 支持 `--once`，循环模式已有 sleep，不再满 CPU 空转。

4. `python_bgp_gateway/sr_policy_sender.py`
   - 支持 `--drain-and-exit`。
   - 缓存已准备节点与每跳 SID route。
   - 只有基础 SID route、encap route 和内核核验都成功才写 applied。
   - 修复 IPv6 规范化导致的非幂等错误：
     - `fd00:024d:...` 会显示成 `fd00:24d:...`
     - 已改用 `ip -6 addr replace`
   - SID 命名空间已分离：
     - `fd00:`：节点身份 SID、ping/encap 目标
     - `fd01:`：SRH 转发 SID和逐跳基础 route
   - 分离原因：防止某节点的 encap route 覆盖另一个策略需要的逐跳 SID route。

5. `TopoConfigurators/Standard/traffic_manager.py`
   - 写入并复用每条 flow 的实际 receiver IPv4。
   - 监听 applied policy 变化并支持重启业务。
   - 支持 `te:traffic:external_control=1`，让正式 runner 独占流量进程。

6. `run_te_experiment.py`
   - baseline 前并行删除所有旧 encap route。
   - 严格要求 24/24 flows，少一条就拒绝结果。
   - 自动选择 OSPF 双向可达的 receiver IPv4。
   - baseline 和 TE 使用相同 flow/IP/速率。
   - 读取接收端逐秒数据，输出 mean、median、min/max、CV 和 JSON。
   - 严格检查：
     - 完整拓扑
     - desired=24
     - applied=24
     - queue=0
     - IPv6 SRv6 ping=24
     - IPv4-over-SRv6 ping=24
   - 默认使用 `udp-native`，避免 iperf3 UDP 的 TCP 控制信道被拥塞打断。

7. `udp_flow_probe.py`
   - 无第三方依赖。
   - 24 个 receiver 使用共同绝对开始时间。
   - receiver 每秒统计实际收到的 payload bytes。
   - sender 按需求速率节拍发送，避免 iperf3 UDP 控制信道问题。

8. `/home/bai/calc_throughput.py`
   - 旧日志工具也已改成 receiver-side 多样本统计。
   - 支持 mark、warmup、样本上下限、active flow 门槛和 JSON。
   - 正式对比仍优先用 `run_te_experiment.py`。

### 0.3 今天的真实测试结果

以下结果必须按“中间证据”理解，尚未达到最终验收：

| 测量/验证 | 结果 | 结论 |
|---|---:|---|
| TCP baseline | 66.74 Mbit/s，24/24，CV 20.64% | 完整但不稳定，不采用 |
| iperf3 UDP baseline | 8/24 成功 | TCP 控制信道失败，不采用 |
| native UDP baseline | 51.93 Mbit/s，24/24，CV 7.80% | 当前最可信 baseline，仍应继续降 CV |
| v3 SID（同一命名空间）ping | IPv6 6/24，IPv4 9/24 | route 覆盖冲突，不通过 |
| v4 SID（fd00/fd01 分离，仍含假 UP 边）ping | IPv6 10/24，IPv4 15/24 | 有改善，但仍不通过 |
| 控制面安装完整性预检 | desired=24、applied=24、queue=0 | 安装链路通过，但不能替代 ping |
| `d97596eb` 修复后 | 119 条 OSPF route；双向 3/3 ping；约 145 ms | 节点恢复 |

尚未得到有效的 baseline 与 TE 成对增益数据。原因是最后一个关键问题
（bridge disabled 链路被误标为 UP）刚修复，稳定拓扑上的 24 策略重装在今天停止时被主动中断。

### 0.4 今天遇到的问题与根因

1. **真实链路容量不是 1000 Mbit/s**
   - host qdisc 是 16 Mbit/s TBF，原采集值使求解器完全误判利用率。

2. **容器内队列不是实际瓶颈队列**
   - 容器看到 `noqueue`，真实 backlog/drop 在 host veth。

3. **`d97596eb` FRR/OSPF 进程僵死**
   - 容器 PID 1 不回收子进程，旧 FRR restart 脚本延迟杀掉新进程。
   - 单容器 restart 又导致自定义 veth 消失。
   - 已精确重建四个 veth、IP 和原 qdisc：
     - `e3dadfa2`
     - `6ba3ab6a`
     - `e3f261ec`
     - `f97ca5a6`

4. **拓扑扫描高峰产生半张快照**
   - Docker exec 并发时曾只采到 118 条链路。
   - 现在残缺快照会被拒绝，不再覆盖上一份完整快照。

5. **bridge disabled 被误认为 UP**
   - 容器接口仍是 UP，但 host bridge port 已是 `state disabled`。
   - 求解器因此选到物理不可用路径。
   - 已修复采集器；最后一次稳定快照是 216 UP / 30 DOWN。

6. **SID 地址幂等检查错误**
   - IPv6 零填充规范化使文本 grep 失败。
   - `addr add` 随后报 `address already assigned`。
   - 已改为 `addr replace`。

7. **同一 SID route 的角色冲突**
   - 逐跳基础 route 和源节点 encap route 使用相同 `fd00` 目标。
   - 后装策略覆盖先装基础 route。
   - 已用 fd00 身份 / fd01 转发命名空间分离。

8. **iperf3 UDP 不适合作为当前主测工具**
   - UDP 数据流启动后，iperf3 TCP 控制连接大量报：
     - `unable to read from stream socket: Resource temporarily unavailable`
   - 已增加 receiver-side native UDP probe。

9. **TCP 多流在当前 TBF/高 RTT 下振荡严重**
   - 12 个尾部样本的总吞吐在约 45-99 Mbit/s 波动。
   - 因此不能仅增加 TCP 窗口后宣称结果稳定。

### 0.5 明天的首要验收顺序

不要直接追求 20%。先严格按下面的门槛推进：

1. 冻结 Standard 拓扑演进。
2. 发布一次 246-link、包含 UP/DOWN 的稳定拓扑快照。
3. 清理所有旧 encap route 和策略状态。
4. 单次求解并完整安装 24 条 policy。
5. 必须先达到：
   - desired=24
   - applied=24
   - queue=0
   - IPv6 SRv6 ping=24/24
   - IPv4-over-SRv6 ping=24/24
6. 以上通过后才跑 baseline→TE。
7. 第一轮确认真实增益方向；再根据真实结果调需求和目标函数。
8. 最后跑 2-3 个短周期，目标：
   - 每阶段 20-24 秒
   - 24/24 flows
   - 单阶段总吞吐 CV 最好 <=5%
   - 平均提升 17%-23%
   - 周期间提升标准差最好 <=3 个百分点

### 0.6 明天直接执行的命令

#### A. 健康检查

```bash
ps -ef | grep -E \
  'NodeDaemon|python3 -u main.py|topo_sniffer.py|queue_monitor.py|lyapunov_solver.py|sr_policy_sender.py|run_te_experiment.py' \
  | grep -v grep

docker ps --filter name=Satellite --format '{{.Names}}' | wc -l

redis-cli get te:traffic:external_control
redis-cli --scan --pattern 'topo:link:*' | wc -l
redis-cli --scan --pattern 'telemetry:queue:*' | wc -l
redis-cli get te:demands
```

今天停止时底层状态：

- NodeDaemon 正常运行。
- `http://127.0.0.1:8080/api/platform/status` 返回 `code=0`。
- 66 个 Satellite 容器仍在运行；另有 2 个 GroundStation 容器。
- FRR/OSPF 数据面仍在。

若 Standard 和 queue monitor 不在进程列表中，分别启动。不要再将三个
服务塞进同一个 `wait` shell，避免一个子进程退出时连带终止其他服务。

终端 1：

```bash
cd /home/bai/OpenSN-Library/TopoConfigurators/Standard
env PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python \
  PYTHONPATH=/home/bai/OpenSN-Library/TopoConfigurators \
  ADDR=127.0.0.1 \
  PORT=8080 \
  python3 -u main.py 2>&1 |
tee /tmp/standard.log
```

终端 2：

```bash
cd /home/bai/OpenSN-Library
python3 -u tools/queue_monitor.py 2>&1 |
tee /tmp/queue_monitor.log
```

先不要启动持续运行的 topo sniffer；按 C 节执行一次 `--once` 即可。

检查故障节点：

```bash
docker exec Satellite_d97596eb sh -c '
  ps -eo stat,comm,args | grep -E "watchfrr|zebra|ospfd|staticd" | grep -v grep
  ip -4 -o addr show | grep " 10\\."
  ip route | grep "proto ospf" | wc -l
  ping -c 3 -W 2 10.0.1.74
'
```

#### B. 冻结动态拓扑

先找到 Standard 的 Python PID：

```bash
STANDARD_PID=$(
  ps -eo pid=,args= |
  awk '$0 ~ /python3 -u main.py/ && $0 !~ /awk/ {print $1; exit}'
)
echo "$STANDARD_PID"
kill -STOP "$STANDARD_PID"
ps -p "$STANDARD_PID" -o pid,stat,etime,args
```

`STAT` 中应包含 `T`。实验完成或退出前必须恢复：

```bash
kill -CONT "$STANDARD_PID"
```

#### C. 发布一次稳定拓扑快照

```bash
python3 -u \
  /home/bai/LEO_TE_Controller/python_bgp_gateway/topo_sniffer.py \
  --once
```

期望类似：

```text
containers=68 directed_links=246 up=216 down=30
```

检查 Redis：

```bash
redis-cli --scan --pattern 'topo:link:*' |
while read -r key; do
  redis-cli --raw hget "$key" status
done |
sort |
uniq -c
```

#### D. 清理到 baseline 状态

```bash
PYTHONPATH=/home/bai/LEO_TE_Controller python3 - <<'PY'
import redis
import run_te_experiment as experiment

r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
experiment.kill_all_iperf()
print("deleted encap:", experiment.clean_srv6_encap_routes())
print("deleted keys:", r.delete(*experiment.POLICY_KEYS))
r.set("te:traffic:external_control", "1")
PY
```

确认：

```bash
redis-cli hlen te:policy:desired
redis-cli hlen te:policy:applied_signature
redis-cli llen policy_queue
```

三者应均为 0。

#### E. 只做控制面 + SRv6 预检

```bash
cd /home/bai/LEO_TE_Controller/python_te_solver
python3 -u lyapunov_solver.py --once \
  > /tmp/leo_te_experiment/preflight_solver.log 2>&1

cd /home/bai/LEO_TE_Controller/python_bgp_gateway
python3 -u sr_policy_sender.py \
  --drain-and-exit --idle-seconds 3 \
  > /tmp/leo_te_experiment/preflight_sender.log 2>&1
```

检查完整性：

```bash
redis-cli hlen te:policy:desired
redis-cli hlen te:policy:applied_signature
redis-cli llen policy_queue

tail -n 80 /tmp/leo_te_experiment/preflight_solver.log
tail -n 80 /tmp/leo_te_experiment/preflight_sender.log
```

期望：

```text
desired = 24
applied = 24
queue = 0
```

执行 48 个真实 ping：

```bash
PYTHONPATH=/home/bai/LEO_TE_Controller python3 - <<'PY'
import redis
import run_te_experiment as experiment

r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
print(experiment.validate_policy_pings(r))
PY
```

必须得到：

```text
{
  'policies': 24,
  'srv6_ipv6_ping_ok': 24,
  'srv6_ipv4_ping_ok': 24
}
```

如果没有 24/24，不要开始吞吐实验。逐流检查：

```bash
redis-cli --raw hgetall te:policy:applied
docker exec <src> ip -6 route | grep 'encap seg6'
docker exec <src> ip route | grep 'encap seg6'
docker exec <hop> ip -6 route get <next_fd01_sid>
docker exec <src> ping -6 -c 3 -W 3 <dst_fd00_sid>
docker exec <src> ping -c 3 -W 3 <dst_ipv4>
```

同时确认失败路径的每条边在 Redis 均为 UP：

```bash
redis-cli hgetall 'topo:link:<src>_<dst>'
```

#### F. 跑一轮正式 baseline→TE

预检通过后先再次清理，runner 会自行完成 baseline 清理和重装：

```bash
PYTHONPYCACHEPREFIX=/tmp/te_pycache \
python3 -u /home/bai/LEO_TE_Controller/run_te_experiment.py \
  --cycles 1 \
  --protocol udp-native \
  --measure-seconds 20 \
  --warmup-samples 2 \
  --max-samples 12 \
  --min-samples 10 \
  --target-low-percent 17 \
  --target-high-percent 23 \
  --result /tmp/leo_te_experiment/result_cycle1.json
```

查看结果：

```bash
python3 -m json.tool \
  /tmp/leo_te_experiment/result_cycle1.json |
less
```

#### G. 调稳定性和 20% 增益

第一轮不要为了“正好 20%”修改统计公式。按真实结果处理：

- 如果 CV >5%：
  - 将 `--measure-seconds` 调到 24；
  - `--max-samples` 调到 16；
  - 保持 topology freeze；
  - 检查是否有 OSPF 邻居变化或 probe 进程启动不齐。
- 如果增益明显低于 17%：
  - 检查 TE 路径是否真的避开 baseline 热点；
  - 检查 solver 的 projected utilization；
  - 再调整 `traffic.json` 的 offered load 或
    `controller.yaml` 的 congestion penalty。
- 如果增益明显高于 23%：
  - 先确认 baseline 不是异常掉流；
  - 再减小 offered load 或降低拥塞惩罚。
- 任何调参后都必须重跑完整 baseline→TE，不能复用旧 baseline。

最终重复验证：

```bash
PYTHONPYCACHEPREFIX=/tmp/te_pycache \
python3 -u /home/bai/LEO_TE_Controller/run_te_experiment.py \
  --cycles 2 \
  --protocol udp-native \
  --measure-seconds 24 \
  --warmup-samples 2 \
  --max-samples 16 \
  --min-samples 12 \
  --target-low-percent 17 \
  --target-high-percent 23 \
  --result /tmp/leo_te_experiment/result_final.json
```

结束时恢复 Standard：

```bash
kill -CONT "$STANDARD_PID"
```

### 0.7 明天建议继续实现的代码项

1. 把 topology freeze/resume 集成进 `run_te_experiment.py` 的
   `try/finally`，避免人工忘记 `SIGCONT`。
2. runner 在 baseline 前主动执行 `topo_sniffer.py --once`。
3. sender 将同一节点的 sysctl、SID 和 proxy NDP 配置进一步批量化，
   将首次 24 policy 安装从约 1-2 分钟压缩到 30 秒左右。
4. 输出每个失败 ping 的 flow ID、第一条失败 edge 和 route-get 证据。
5. 在最终结果 JSON 增加：
   - topology UP/DOWN 计数
   - policy path
   - ping 失败明细
   - 每阶段 CV 门槛和 pass/fail
6. 等 24/24 ping 与首轮真实增益完成后，再把最终数值写回本 README。

本文档用于说明当前两个项目的整体实验目标、运行流程、控制算法介入逻辑、SRv6 下发逻辑、吞吐量量化方法，以及后续继续优化时应遵守的实验框架。

## 1. 项目边界

本实验由两个项目共同完成：

- `/home/bai/OpenSN-Library`
  - 负责拓扑生成、容器化卫星网络、链路状态变化、iperf 流量生成、队列遥测。
  - 主要相关文件：
    - `TopoConfigurators/Standard/main.py`
    - `TopoConfigurators/Standard/traffic_manager.py`
    - `TopoConfigurators/Standard/traffic.json`
    - `tools/queue_monitor.py`
    - `link_failure/link_connectivity_matrix.csv`

- `/home/bai/LEO_TE_Controller`
  - 负责拓扑嗅探、Lyapunov TE 求解、SRv6 策略下发。
  - 主要相关文件：
    - `python_bgp_gateway/topo_sniffer.py`
    - `python_te_solver/lyapunov_solver.py`
    - `python_bgp_gateway/sr_policy_sender.py`
    - `config/controller.yaml`

额外量化脚本：

- `/home/bai/calc_throughput.py`
  - 负责读取接收端 iperf server 日志，统计实际送达吞吐量。

## 2. 实验目标

实验目标不是让所有流在 baseline 阶段天然跑满，而是验证：

1. 未启用控制算法时，流量按基础 OSPF 转发，有一定接收吞吐，但受拥塞、链路变化、路径选择影响，不能充分利用全网资源。
2. 启用 Lyapunov TE 算法后，控制面根据拓扑、链路容量、队列状态和流量需求，计算更合适的路径。
3. `sr_policy_sender.py` 将 TE 路径以 SRv6/IPv6 方式下发到容器内核。
4. 下发成功后，业务 IPv4 流通过 `encap seg6` 路由进入 SRv6 隧道。
5. 量化时统计接收端吞吐量，而不是发送端吞吐量。
6. 最终目标是算法介入后接收吞吐量相比 baseline 稳定提升约 20%，
   当前正式验收区间为 17%-23%。

## 3. 正确启动流程

### 3.1 启动依赖服务

```bash
cd /home/bai/OpenSN-Library
bash daemon/scripts/start_depend_service.sh
```

### 3.2 启动 NodeDaemon

```bash
cd /home/bai/OpenSN-Library/daemon/opensn-daemon
sudo ./NodeDaemon
```

### 3.3 上传配置和拓扑

在本机虚拟机中优先使用 `127.0.0.1:8080`，避免 `192.168.200.129:8080` 被代理返回 502。

```bash
cd /home/bai/OpenSN-Library

curl -sS -X POST http://127.0.0.1:8080/api/emulation/update \
  -H 'Content-Type: application/json' \
  --data-binary @example/test_topologies/emu_config.json

curl -sS -X POST http://127.0.0.1:8080/api/emulation/topology \
  -H 'Content-Type: application/json' \
  --data-binary @example/test_topologies/topology_config_6_11.json

curl -sS -X POST http://127.0.0.1:8080/api/emulation/start
```

### 3.4 启动 Standard 拓扑配置器和流量管理

```bash
cd /home/bai/OpenSN-Library/TopoConfigurators/Standard

nohup env PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python \
  PYTHONPATH=/home/bai/OpenSN-Library/TopoConfigurators \
  ADDR=127.0.0.1 PORT=8080 \
  python3 -u main.py > /tmp/standard.log 2>&1 &
```

`PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python` 用于避免当前环境里的 protobuf/etcd3 descriptor 兼容问题。

### 3.5 启动拓扑嗅探

```bash
python3 /home/bai/LEO_TE_Controller/python_bgp_gateway/topo_sniffer.py
```

该脚本将当前容器拓扑写入 Redis，例如：

- `topo:link:<src>_<dst>`
- `src`
- `dst`
- `delay`
- `capacity`
- `status`
- `local_iface`

### 3.6 启动队列遥测

```bash
python3 /home/bai/OpenSN-Library/tools/queue_monitor.py
```

该脚本读取 host veth 的 `tc -s qdisc show`，写入 Redis：

- `telemetry:queue:<container>`

求解器使用这些队列数据作为拥塞指标。

## 4. Baseline 测量流程

baseline 阶段不要启动：

- `lyapunov_solver.py`
- `sr_policy_sender.py`

也不要保留旧的 SRv6 策略状态。

建议清理：

```bash
redis-cli del policy_queue
redis-cli del te:policy:last_signature
redis-cli del te:policy:applied_signature
redis-cli del te:flow:dst_ip
```

等待 Standard 启动 iperf 流后，打 baseline 测量标记：

```bash
sudo python3 /home/bai/calc_throughput.py --mark baseline
```

等待一段业务自然运行时间后，统计 baseline 阶段新增的接收端吞吐：

```bash
sudo python3 /home/bai/calc_throughput.py --since-mark baseline
```

注意：当前量化脚本正式实验推荐使用 `--mark/--since-mark`，不要使用固定时间窗口作为主要依据。

## 5. 控制算法阶段流程

### 5.1 启动 Lyapunov 求解器

```bash
cd /home/bai/LEO_TE_Controller/python_te_solver
python3 lyapunov_solver.py
```

求解器主要逻辑：

1. 从 Redis 读取拓扑：
   - `topo:link:*`
2. 从 Redis 读取队列状态：
   - `telemetry:queue:<node>`
3. 从 Redis 读取流量需求：
   - `te:demands`
4. 构建有向图 `G`
5. 为每条流计算 K 条候选路径
6. 通过 Lyapunov 风格代价和投影利用率惩罚选择可下发的单路径
7. 当前内核下发是单条静态 SRv6 route，因此每条 flow 只发布一条路径
8. 将策略写入 Redis 队列：
   - `policy_queue`

为避免队列爆炸，当前求解器已经做了两类防抖：

- 策略签名只看真正影响内核路由的字段：
  - `src`
  - `dst`
  - `path`
- 不再因为 `weight` 微小变化重复入队
- 同一个 flow 在 `policy_queue` 中只保留最新策略

### 5.2 启动 SRv6 下发器

```bash
cd /home/bai/LEO_TE_Controller/python_bgp_gateway
python3 sr_policy_sender.py
```

下发器主要逻辑：

1. 从 `policy_queue` 读取策略。
2. 将节点名转换为 SRv6 SID：
   - `Satellite_96fba8f3` -> `fd00:96fb:a8f3::1`
3. 为路径上的容器开启 SRv6 基础配置：
   - `net.ipv6.conf.all.forwarding=1`
   - `net.ipv6.conf.all.seg6_enabled=1`
   - 每个接口 `seg6_enabled=1`
   - 每个节点在 `lo` 上添加自己的 `fd00:*::1/128` SID
4. 为每跳安装 IPv6 SID 路由：
   - `ip -6 route replace <next_sid>/128 dev <out_iface>`
5. 在源节点安装真正影响业务流的 encap 路由：
   - IPv6 SID route：
     - `ip -6 route replace <dst_sid>/128 encap seg6 mode encap segs ... dev <out_iface>`
   - IPv4 业务 route：
     - `ip route replace <dst_10_ip>/32 encap seg6 mode encap segs ... dev <out_iface>`
6. 下发成功后写入：
   - `te:policy:applied_signature`

当前特别注意：

- `traffic_manager.py` 会把 iperf 实际使用的目的 IPv4 写入 Redis：
  - `te:flow:dst_ip`
- `sr_policy_sender.py` 优先读取 `te:flow:dst_ip` 来安装 IPv4 encap route。
- 这样可以避免 receiver 有多个 10.x 地址时，两边选择不一致，导致 iperf 没命中 SRv6 route。

### 5.3 控制算法阶段测量

算法阶段开始时打标记：

```bash
sudo python3 /home/bai/calc_throughput.py --mark te
```

启动求解器和 sender 后，等待下发完成：

```bash
redis-cli hlen te:flow:dst_ip
redis-cli hlen te:policy:applied_signature
redis-cli llen policy_queue
```

理想状态：

```text
te:flow:dst_ip 接近 24
te:policy:applied_signature 接近 24
policy_queue 为 0
```

然后统计控制算法阶段新增的接收吞吐：

```bash
sudo python3 /home/bai/calc_throughput.py --since-mark te
```

## 6. 量化脚本设计原则

量化脚本必须满足：

1. 统计接收端吞吐量，而不是发送端吞吐量。
2. 不使用固定时间窗口作为主要实验依据。
3. 不把旧日志中的历史吞吐算入新阶段。
4. baseline 和 TE 阶段应分别打标记，统计标记之后新增日志。

当前脚本支持：

```bash
sudo python3 /home/bai/calc_throughput.py --mark baseline
sudo python3 /home/bai/calc_throughput.py --since-mark baseline

sudo python3 /home/bai/calc_throughput.py --mark te
sudo python3 /home/bai/calc_throughput.py --since-mark te
```

临时排查历史日志时可以使用：

```bash
sudo python3 /home/bai/calc_throughput.py --include-stale
```

正式对比不建议使用 `--include-stale`。

## 7. 当前关键 Redis Key

拓扑：

```text
topo:link:*
```

队列遥测：

```text
telemetry:queue:<container>
```

流量需求：

```text
te:demands
```

TrafficManager 实际 iperf 目标地址：

```text
te:flow:dst_ip
```

求解器策略去重状态：

```text
te:policy:last_signature
```

待下发策略队列：

```text
policy_queue
```

已成功安装 SRv6 策略：

```text
te:policy:applied_signature
```

## 8. SRv6 是否真正生效的验证方法

### 8.1 查看 applied 数量

```bash
redis-cli hlen te:policy:applied_signature
redis-cli llen policy_queue
```

### 8.2 查看源节点 IPv4 encap route

以某条 flow 的源节点为例：

```bash
docker exec <src_container> ip route | grep 'encap seg6'
```

应该能看到类似：

```text
10.0.x.x encap seg6 mode encap segs ... dev <iface>
```

### 8.3 查看源节点 IPv6 encap route

```bash
docker exec <src_container> ip -6 route | grep 'encap seg6'
```

应该能看到类似：

```text
fd00:xxxx:xxxx::1 encap seg6 mode encap segs ... dev <iface>
```

### 8.4 查看 SRv6 基础配置

```bash
docker exec <container> sysctl net.ipv6.conf.all.forwarding
docker exec <container> sysctl net.ipv6.conf.all.seg6_enabled
docker exec <container> ip -6 addr show dev lo
```

期望：

```text
net.ipv6.conf.all.forwarding = 1
net.ipv6.conf.all.seg6_enabled = 1
lo 上存在 fd00:*::1/128
```

### 8.5 验证 iperf 目标 IP 是否命中 SRv6 route

从 client 日志查看实际目标：

```bash
docker exec <src_container> tail -n 50 /share/user/iperf_logs/<flow>_client.log
```

例如：

```text
client restart -> 10.0.1.129
```

然后检查源节点：

```bash
docker exec <src_container> ip route get 10.0.1.129
```

如果命中 SRv6，应看到：

```text
encap seg6 mode encap segs ...
```

## 9. 已发现并修复的问题

### 9.1 SRv6 下发原本不是完整 IPv6/SRv6

原问题：

- 下发逻辑更像 IPv4 route，不是真正 SRv6。

当前修复：

- 增加 `fd00:*::1/128` SID。
- 开启 `seg6_enabled` 和 IPv6 forwarding。
- 安装 `ip -6 route ... encap seg6 ...`。
- 安装 IPv4 over SRv6 route：
  - `ip route replace <dst_10_ip>/32 encap seg6 ...`

### 9.2 吞吐量原本统计发送端

原问题：

- 发送端吞吐不能代表真实送达。

当前修复：

- `/home/bai/calc_throughput.py` 只读 `*_server.log`。
- 统计接收端 `bits/sec`。

### 9.3 policy_queue 重复堆积

原问题：

- 求解器每轮重复 `rpush`。
- 同一 flow 的旧策略和新策略混在队列里。
- sender 追旧策略，导致下发状态滞后。

当前修复：

- 策略签名不再包含不影响内核 route 的 `weight` 抖动。
- 每个 flow 在队列中只保留最新策略。

### 9.4 TrafficManager 和 sender 选择的目标 IPv4 不一致

原问题：

- receiver 有多个 10.x 地址。
- TrafficManager 实际打一个 IP。
- sender 可能给另一个 IP 安装 encap route。
- 结果 iperf 不命中 SRv6。

当前修复：

- TrafficManager 写入：
  - `te:flow:dst_ip[flow_id] = actual_dest_ip`
- sender 优先按该 IP 下发 IPv4 SRv6 encap route。

### 9.5 固定时间窗口导致吞吐统计为 0

原问题：

- 只看最近 N 秒日志。
- 算法下发慢或流量重启慢时，日志可能被判定过期。

当前修复：

- 使用 `--mark/--since-mark` 按实验阶段新增日志统计。

## 10. 当前仍需关注的问题

### 10.1 iperf3 UDP 控制信道不稳定

现象：

- SRv6 ping 可以通。
- TCP iperf over SRv6 可以通。
- 但 UDP iperf3 在部分 SRv6 路径上可能出现：
  - `unable to read from stream socket`
  - `unable to send control message`
  - `Bad file descriptor`

原因推测：

- iperf3 即使测 UDP，也依赖 TCP 控制连接。
- SRv6 封装、MTU、多流拥塞、链路切换可能导致控制信道不稳定。

后续方向：

- 降低 UDP 单流速率。
- 减小 UDP payload，例如 `-l 800` 或更低。
- 使用更长 duration，避免下发时流已经结束。
- 考虑切换到 TCP 流进行主实验，或引入 iperf2/其他 UDP 测量方式。

### 10.2 sender 下发速度较慢

原因：

- 每条策略会执行大量 `docker exec`。
- 每条路径都可能重复配置 SID、sysctl、proxy NDP、SID route。

后续优化：

- 对已配置节点做缓存。
- 对已安装 SID route 做缓存。
- 对每个容器批量执行多条命令，减少 `docker exec` 次数。

### 10.3 baseline 和 TE 阶段必须隔离

要求：

- baseline 不应保留旧 SRv6 route。
- TE 阶段不应使用旧日志。

建议：

- 每轮完整实验前重建拓扑容器，或至少确认源节点没有旧 `encap seg6` route。
- 每轮阶段开始前清理 Redis 状态：

```bash
redis-cli del policy_queue
redis-cli del te:policy:last_signature
redis-cli del te:policy:applied_signature
redis-cli del te:flow:dst_ip
```

## 11. 推荐实验框架

### 11.1 完整实验前准备

1. 启动 OpenSN 依赖服务。
2. 启动 NodeDaemon。
3. 上传 emu config 和 topology config。
4. 启动 emulation。
5. 启动 Standard。
6. 启动 topo_sniffer。
7. 启动 queue_monitor。
8. 确认 Redis 有拓扑：

```bash
redis-cli keys 'topo:link:*' | head
```

9. 确认 Redis 有流量需求：

```bash
redis-cli get te:demands
```

### 11.2 baseline 阶段

```bash
redis-cli del policy_queue te:policy:last_signature te:policy:applied_signature te:flow:dst_ip

sudo python3 /home/bai/calc_throughput.py --mark baseline

# 等待 baseline 阶段产生足够接收端日志

sudo python3 /home/bai/calc_throughput.py --since-mark baseline
```

记录：

- baseline 总接收吞吐
- baseline 活跃流数
- baseline 各流吞吐

### 11.3 TE 阶段

```bash
sudo python3 /home/bai/calc_throughput.py --mark te
```

启动：

```bash
cd /home/bai/LEO_TE_Controller/python_te_solver
python3 lyapunov_solver.py
```

```bash
cd /home/bai/LEO_TE_Controller/python_bgp_gateway
python3 sr_policy_sender.py
```

等待：

```bash
redis-cli hlen te:flow:dst_ip
redis-cli hlen te:policy:applied_signature
redis-cli llen policy_queue
```

统计：

```bash
sudo python3 /home/bai/calc_throughput.py --since-mark te
```

记录：

- TE 总接收吞吐
- TE 活跃流数
- TE 各流吞吐
- applied 策略数量
- policy_queue 是否清空

### 11.4 计算提升

```text
提升比例 = (TE接收吞吐 - baseline接收吞吐) / baseline接收吞吐 * 100%
```

目标：

```text
17% - 23%，中心目标约 20%
```

## 12. 后续修改必须遵守的原则

1. 不要让 baseline 自然跑满，否则无法体现控制算法作用。
2. 不要统计发送端吞吐量。
3. 不要用旧日志对比新实验。
4. 不要让 TrafficManager 和 sender 分别猜 receiver IP。
5. 不要让求解器无限重复推送相同策略。
6. TE 下发必须是 SRv6/IPv6，不应退化成普通 IPv4 route。
7. 每次判断算法是否有效，至少同时检查：
   - `te:policy:applied_signature`
   - `policy_queue`
   - 源节点 `ip route | grep 'encap seg6'`
   - iperf client 日志
   - iperf server 接收日志

## 13. 快速排查命令

查看控制进程：

```bash
ps -ef | grep -E 'NodeDaemon|main.py|topo_sniffer.py|queue_monitor.py|lyapunov_solver.py|sr_policy_sender.py'
```

查看下发进度：

```bash
redis-cli hlen te:flow:dst_ip
redis-cli hlen te:policy:applied_signature
redis-cli llen policy_queue
```

查看 Standard 日志：

```bash
tail -f /tmp/standard.log
```

查看某源节点 SRv6 route：

```bash
docker exec <src_container> ip route | grep 'encap seg6'
docker exec <src_container> ip -6 route | grep 'encap seg6'
```

查看某 flow client 日志：

```bash
docker exec <src_container> tail -n 80 /share/user/iperf_logs/<flow>_client.log
```

查看某 flow server 日志：

```bash
docker exec <dst_container> tail -n 80 /share/user/iperf_logs/<flow>_server.log
```

查看量化脚本帮助：

```bash
python3 /home/bai/calc_throughput.py --help
```

# 2026-10-27：原课题1/3镜像、LSDB队列和控制器集成运行指南

> 已收到“一键部署包.rar”并补齐原cfg部署入口。最新完整流程和镜像以 [README_FULL_DEPLOY_20261027.md](README_FULL_DEPLOY_20261027.md) 为准；下文保留收到RAR之前的阶段记录。

## 对本次四点需求的确认

1. **新镜像完整继承 `weixing1020.tar` 的节点文件系统**，保留 FRR、原 `/cfg.sh`、原 `/lsdb.json`、原 `/frr-auto66.sh` 和原节点启动脚本，再加入控制器算法、新队列脚本及吞吐量测试工具。镜像名：`leo-te-integrated:queue-20261027`。
2. `/auto66-lite.sh` 和 `/queue-probe.sh` 已内置；前者持续维护 LSDB 并默认每 7 秒更新顶层 `queues`。控制器默认直接读取此字段。已有可测 qdisc 时无需另外启动队列采集进程；未配置 qdisc 时只能得到缺测标记，需要先按真实链路参数配置。
3. **不创建、不改造原拓扑，不使用 OpenSN**。控制器、脚本管理器及吞吐量工具通过同机 Docker socket 接入课题1已经启动的节点。它们没有创建容器、创建链路、生成网格或调用 OpenSN 的功能。
4. 以下给出启动顺序和可执行命令。但**原课题1“开启全网拓扑”的启动器尚未提供**，当前无法替你给出该步骤的真实命令。`docker load` 导入的是镜像，不能恢复宿主机上原有的全网容器/veth接线。需补充原课题1启动命令/脚本路径，后面的采集、求解、下发和测试均已配好。

## 原拓扑依据和边界

对 `weixing1020.tar` 的只读检查：归档仅有一个镜像，原入口 `/root/entrypoint.sh`；包含 L0305 的 `/cfg.sh`、`/frr-auto66.sh` 和 `/lsdb.json`。其中历史 LSDB 为 49 个可识别节点、129 条有向链路。没有找到全网容器创建、宿主机veth接线或Compose启动文件。原入口只启动FRR并保活。

新ZIP中 `lsdb_L0305.json` 是另一个时刻的参考快照，有66个节点、264条有向链路、66个End SID。**这些样例只用于解析测试，程序不把样例当作现场拓扑，也不据此另造网络。现场一律读取运行节点的实时IS-IS LSDB。**

一个节点的LSDB可以包含全网拓扑，但其 `queues.links` 只包含**本节点的4个发送方向**。因此只启动L0305的脚本，只能得到L0305的队列；要用于全网算法，需要在所有参与节点启动维护脚本，控制器再汇总各节点的尾部。

新镜像包含所有节点所需的软件和原拓扑信息；实际每个节点仍是课题1平台启动的独立容器。算法控制器建议单独启动一个容器。可以使用同一新镜像启动不同角色，**无需Docker-in-Docker**。

## 0. 交付文件与导入

本交付目录包含：

- `leo-te-integrated-queue-20261027.tar.gz`：新集成镜像，Linux amd64。
- `run.sh`：控制器/采集管理/测试的统一启动包装。
- `demands.json`：6条示例IPv6业务需求，使用提供样例已有的节点；可改为真实业务。
- `README_QUEUE_INTEGRATION_20261027.md`：本文。
- `queue-vendor/`：原ZIP内容，脚本逐字节保留。
- `验证报告.md`、`evidence/`：验证记录；没有伪造全网吞吐量结果。
- `SHA256SUMS`：完整交付文件校验。

在交付目录执行：

```bash
sha256sum -c SHA256SUMS
docker load -i leo-te-integrated-queue-20261027.tar.gz
chmod +x run.sh
docker run --rm leo-te-integrated:queue-20261027 --help
```

新镜像已包含原镜像内容，导入新镜像后不需要再导入原镜像才能运行。重建源码时才需要两个构建基底镜像。

## 1. 用课题1原入口启动其原有拓扑

**此步骤使用课题1原平台/原启动脚本，不调用本项目历史 `lab.sh`，不调用 OpenSN。**

需要课题1补充：原来从空机启动L0101～L0611全网节点和veth的命令，以及对应拓扑配置位置。归档中未找到这个启动器；本文不会编造一个替代拓扑。

两种接入方式均支持：

- 保留已经运行的原 `weixing1020` 节点：后续 `manage install/start` 会把新采集脚本安装进去，随后直接对接，无需重建这些节点。
- 课题1用新镜像启动节点：在其原启动器中将节点镜像改为 `leo-te-integrated:queue-20261027`，节点容器命令设为 `node`，其余原有的容器、接线、配置写入流程不变。如果原启动器显式覆盖entrypoint，继续使用其原节点启动方式；脚本和FRR仍位于原路径。

节点需保持课题1原有的网络管理权限、IPv6 forwarding和`seg6_enabled`设置。原版FRR需要`SYS_ADMIN`等能力；不能仅配置`NET_ADMIN`就假定其FRR已正常启动。控制器不需要privileged。

确认现场已启动：

```bash
docker ps --format '{{.Names}}\t{{.Image}}'
docker exec L0305 cat /cfg.sh
docker exec L0305 vtysh -c 'show isis neighbor'
```

示例命令使用容器名L0305；若平台容器名不同，替换为实际名称。程序按 `/cfg.sh` 的 `name` 识别节点，支持容器名与逻辑节点名不同。

## 2. 安装并启动所有节点的LSDB/队列维护

在交付目录执行：

```bash
# 只安装：备份已有同名脚本，不重建节点或修改接线
./run.sh manage install --expected-nodes 66

# 安装并启动：停止旧版auto66 watcher，避免两套脚本同时写LSDB
./run.sh manage start --expected-nodes 66
```

第二条内部执行供应方的 `stop-old` 和 `bash /auto66-lite.sh`，保留FRR、原拓扑及已有qdisc。每个节点的启动回显保存在 `results/Lxxxx-start.log`。不需要再次独立启动queue-probe常驻进程。

等价的单节点手动操作是：

```bash
docker exec L0305 bash /auto66-lite.sh stop-old
docker exec L0305 bash /auto66-lite.sh
docker exec L0305 bash /auto66-lite.sh status
```

如果现场实际不是66个节点，先向课题1确认，不要用示例数量补造节点。可以去掉`--expected-nodes 66`，但完整性仍以实际实时LSDB与已运行节点一致为准。若有两套同名逻辑节点并存，可在命令尾部使用`--containers 实际容器名1 实际容器名2 ...`限定对象。

## 3. 检查qdisc；缺失时才按链路参数补齐

先检查实际发送方向：

```bash
docker exec L0305 bash /auto66-lite.sh qshow all
docker exec L0305 bash /queue-probe.sh --once
```

如果已经有平台设置的TBF/netem队列，保留它们，直接读。若业务接口是noqueue，以下命令会**只对原节点中尚无已管理root的业务接口添加队列**，已有root不覆盖：

```bash
# 16mbit/10ms来自交付示例；仅当这就是本次试验约定值时使用
./run.sh manage ensure-qdisc --expected-nodes 66 --rate 16mbit --delay 10ms
```

该命令配置TBF（burst 32KB、latency 100ms）和netem（delay为参数、limit 1000包），记录 `results/qdisc-actions.json`。参数应以课题1实际链路配置为准。它不创建或重连链路。

供应方也提供了以下原生命令，**会删除并覆盖原root**，只有确实需要重置该接口时使用：

```bash
# 默认仅预览
docker exec L0305 bash /auto66-lite.sh qset eth1 rate=16mbit burst=32kbit lat=100ms delay=10ms limit=1000
# 明确要覆盖当前root时才加 --yes
docker exec L0305 bash /auto66-lite.sh qset eth1 rate=16mbit burst=32kbit lat=100ms delay=10ms limit=1000 --yes
```

qset/qclear只改tc，不负责产生业务流量。低负载时 `queue_bytes=0, measured=true` 正常；`measured=false`或`queue_bytes=null`是缺测，不能当成空队列。

## 4. 等待LSDB、队列和SRv6能力就绪

```bash
./run.sh manage wait --expected-nodes 66 --timeout 240
./run.sh manage status
./run.sh controller --inspect --inspect-queues --once
```

就绪检查读取原拓扑的实时LSP，检查发现的节点和LSDB成员一致、图连通、End SID已宣告、参与有向链路有新鲜队列/容量/时延。不会通过猜网格来补边。输出：

- `results/topology.json`：实时拓扑和能力。
- `results/queues.json`：按 `src→dst` 汇总的原生队列数据。
- `results/nodes.json`：实际容器与 `/cfg.sh` 对应关系。

人工读取L0305尾部：

```bash
docker exec L0305 tail -c 4000 /lsdb.json
```

默认每7秒采样；超过`2 × queue_poll_s`（默认14秒）拒绝参与求解。只认原生 `queues.links[].queue_bytes`，不把drops/overlimits或delay当作积压，也不读取宿主机备用veth来代替此包的业务队列。每个节点默认仅传输16KB尾部，避免每轮将66份完整LSDB传回控制器。

修改**单节点**采样周期可用供应方入口：

```bash
docker exec L0305 bash -c 'echo 7 > /run/queue-poll'
```

全网1秒采样在供应方环境曾造成高CPU，本文保持7秒，不自动提频。

## 5. 准备算法所需业务需求

交付目录已有 `demands.json`，例如：

```json
[
  {"id":"row3_a","src":"L0301","dst":"L0306","class":"class_2_embb","demand":4000},
  {"id":"row3_b","src":"L0302","dst":"L0306","class":"class_2_embb","demand":4000}
]
```

`demand`单位kbps；容量Mbps、物理时延ms、积压bytes。完整示例有6条4Mbps业务，总发送负载24Mbps，用于在已有链路上观察拥塞与绕行，**没有生成或改变拓扑**。按真实业务修改节点和速率；需求不能从LSDB推导。

现有原生路径执行器按源/目的安装一条路由；同一`(src,dst)`不能放多条独立需求，控制器会拒绝。示例使用原生支持的目的IPv6 loopback；IPv4/任意业务网段不在本版执行器的安装接口中。

## 6. 读取原生队列并求解，先检查策略

```bash
./run.sh controller --demands /work/demands.json --once
```

查看：

```bash
cat results/plan.json
```

默认仅输出方案；算法使用现有Lyapunov/SciPy全局单路径求解器，输入来自真实LSDB与 `queues`。`plan.json`保留积压、容量、时延、采样时间、接口和来源。`results/health.json`报告本轮成功/失败；失败时旧plan可能保留，不能把旧文件当作新结果。

## 7. 下发SRv6并检查回执

```bash
./run.sh controller --demands /work/demands.json --once --apply --ack-timeout 90
cat results/acceptance.json
```

控制器原子写入**源节点** `/path.cmd/leo-te-<业务ID摘要>.json`。原生lite执行器读取该文件、安装 `encap seg6` 路由并Ping目的loopback。只有新回执同时为 `status=ok, route=seg6, ping=pass` 才记录accepted。

单节点现场查看：

```bash
docker exec L0301 cat /path-tuner.status
docker exec L0301 ip -6 route get fc00:0:3::6
```

程序支持旧auto66和新auto66-lite的执行器进程匹配。新执行器仍使用node End SID，本文不声称它支持严格End.X逐跳。

持续读取、求解与下发：

```bash
docker run -d --name leo-te-controller-queue20261027 --restart unless-stopped --network none \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD:/work" -v "$PWD/results:/output" \
  leo-te-integrated:queue-20261027 controller \
  --demands /work/demands.json --apply --interval 5 --ack-timeout 90

docker logs -f leo-te-controller-queue20261027
```

`--interval 5`为每轮完成后的等待时间。队列过期/缺测时停止该轮下发并保留已有策略，日志中说明原因。控制器镜像虽保留原 `/cfg.sh`，通过角色标识避免将自己的历史快照误识别为L0305节点。

## 8. 在同一原拓扑上实测吞吐量

**停止持续控制器，避免它在Baseline阶段重新下发策略：**

```bash
docker stop leo-te-controller-queue20261027
```

如果未启动持续控制器，跳过上面这条。然后运行：

```bash
./run.sh benchmark --demands /work/demands.json \
  --cycles 3 --warmup 10 --duration 30 --ack-timeout 90
```

每周期依次执行：

1. 只撤销本需求文件对应的控制器策略，等待原生执行器删除路由，核实目的走原生IS-IS。
2. 同步启动IPv6 UDP发送与接收，预热10秒，再记录30秒Baseline接收端payload吞吐量。
3. 读取新鲜原生队列，调用同一个算法控制器下发SRv6，等待原生Ping验收。
4. 以同一需求、同一发送速率和payload重新测量TE，保留原始流量和队列证据。
5. 检查拓扑、End SID、容量/时延指纹未改变，检查两阶段实际发送负载一致、每条流接近约定速率、接收总量是否稳定。

测试工具会向涉及节点临时复制一个静态链接的UDP6探针，不要求原节点安装Python/iperf。默认payload 1000字节；吞吐量单位是接收的应用payload Mbps，区别于物理链路线上比特率。测试不改qdisc，不创建拓扑，不启动其他业务，不强制调出某个提升百分比。

默认有效性阈值：阶段吞吐量CV≤5%、Baseline/TE实际发送负载差≤1%、实际发送与约定速率误差≤2%（总量及每条流）。不符合时标记`invalid_measurement`并返回非零状态；零提升和负提升原样记录。阶段干扰可能需要延长`--duration`，不要直接将无效结果解释为算法提升。

结果目录由命令打印，对应宿主机：

```text
results/throughput/<时间戳>/
  README.md                  各周期接收吞吐量、变化比例、有效性
  result.json                全部测量和判定
  demands.json               本次需求副本
  cycle-1/
    baseline/                逐流发送/接收、路由、负载期间原生队列
    controller/              算法plan、native acceptance、控制器日志
    te/                      同上，TE阶段
  cycle-2/ ...
```

默认测试结束后撤销本次策略，回到原生路由；用`--keep-policy`保留。不会删除其他课题的策略文件；如果Baseline仍被其他控制器/策略接管，会报错而不伪造Baseline。

## 9. 停止和撤销

停止持续控制器不会自动撤销已经安装的策略。需要恢复原生路由时：

```bash
./run.sh benchmark --demands /work/demands.json --withdraw-only
```

该操作无需队列新鲜，仅撤销此需求集对应的`leo-te-*.json`，不删除其他业务。若另有策略覆盖同一目的，会提示仍有SRv6路由，不会强行清空它们。

节点采集器无需随测试停止。暂停某个节点的维护：

```bash
docker exec L0305 bash /auto66-lite.sh stop
```

这会停止lite watcher/tuner/LSDB维护，不删除原拓扑，也不主动停止FRR。恢复用`bash /auto66-lite.sh`。不要将该操作当作策略撤销的替代。

## 10. 已验证与尚未验证

- 已验证ZIP完整66节点拓扑样例的解析、原生4方向队列口径、7秒周期/14秒过期、缺失不冒充0、源接口匹配和控制器角色排除。
- 已在一个隔离单节点中执行原脚本，LSDB队列时间戳持续更新，两次样本间隔约7.12秒；实际配置的dummy出口返回measured=true，其他不存在的接口保持null/false。该测试没有建立额外网络拓扑。
- 已在隔离容器loopback测试静态UDP6探针，发送目标完成、无发送错误、接收计数验证通过。
- **未启动或生成全网拓扑，未执行全网吞吐量对比，也没有本次全网提升数字。** 仍需课题1原启动器/运行现场，才能实测第8步。

参数参考：[TBF](https://man7.org/linux/man-pages/man8/tc-tbf.8.html)、[netem](https://man7.org/linux/man-pages/man8/tc-netem.8.html)。原供应方字段和脚本说明见 `queue-vendor/README.md`。

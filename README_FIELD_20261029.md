# 明天现场联调：一个控制器镜像，使用课题方现成网络

更新：2026-10-29。镜像 `leo-te-controller:field-20261029`，文件 `leo-te-controller-20261029.tar.gz`。

**所有下列命令都在能执行docker的宿主机终端输入。不要先进入L0305等节点的bash。**

## 1. 先明确分工和验收目标

| 内容 | 由谁做 |
|---|---|
| 创建现场节点容器、接线、启动网络 | 课题方 |
| 各节点执行原auto66-lite.sh，维护IS-IS/SRv6及LSDB | 课题方 |
| 启动本镜像，读现有拓扑/队列，调用项目Lyapunov算法并写原path.cmd接口 | 本控制器 |
| 如现场没有队列配置，配置16Mbit/s、10ms并安装备用采集入口 | 本镜像的queues prepare命令 |
| 先验收算法和实际下发，再按需对比吞吐量 | 按本文操作 |

**明天先做到第7步的SMOKE PASS。** 这代表算法已计算、原执行器回执ok/seg6/pass、内核实际SID列表与算法方案一致。Ping验收连通性，UDP接收端计数用于验收吞吐量。

本镜像启动一个独立控制器，不带节点cfg、不创建拓扑，不使用OpenSN。控制器通过一条启动命令中的Docker socket挂载访问已有节点，容器间无需额外建立业务网络。

容器实际名字可以有前后缀或使用其他命名；识别依据是实际IPv6 loopback、原cfg、LSDB身份和真实业务接口。原auto的逻辑节点ID仍使用其Lrrcc/SID约定。节点数只要求有至少两个可用、互通、支持SRv6的节点，允许在现有网络的可用互通子网上先测。程序不会推算或生成缺失的拓扑边。

## 2. 导入镜像

用文件管理器把本次镜像文件复制到宿主机“主目录/联调”文件夹。无需解压，不再搜索光盘挂载路径，不需要旧交付目录或run.sh。

```bash
cd "$HOME/联调"
docker load -i leo-te-controller-20261029.tar.gz
```

应看到 `Loaded image: leo-te-controller:field-20261029`。若你的镜像放在其他目录，先cd到那个真实目录，再执行docker load。

先确认课题方节点已经运行：

```bash
docker ps --format '{{.Names}}  {{.Image}}'
```

Docker需要管理员权限时，给docker命令前加sudo，例如 `sudo docker load -i ...`；保持原终端和工作目录，不必切换到另一个节点容器。

## 3. 只启动这一个控制器

下面整段是一条命令，可以整段复制：

```bash
docker run -d --name leo-te-control --network none \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v leo-te-field-results:/output \
  leo-te-controller:field-20261029
```

不用自行安装Python、Redis、iperf或解压工具；计算和测量工具都在镜像中。`leo-te-field-results`是Docker管理的结果卷，配置和结果留在卷里；不依赖宿主机旧results目录。

```bash
docker logs leo-te-control
docker exec leo-te-control leo-te help
```

应看到 `LEO TE controller ready`。这表示控制器已启动，后面还需执行队列和网络检查。启动不会自动打流或下发路径。

若提示容器名已存在，先查看，勿直接删除现场容器：

```bash
docker ps -a --filter name=leo-te-control
```

确认为本镜像的已停止控制器时，用 `docker start leo-te-control` 继续；若是其他镜像或旧测试，先确认归属，不要覆盖。

## 4. 识别现场节点，处理今天的重复claim

```bash
docker exec leo-te-control leo-te nodes
```

输出类似 `L0305 实际容器名 business_up=4`。数量不要求恰好66。没有真实业务接口的旧镜像快照会在IGNORED中列出；控制器自身也会跳过。

如果有两个健康网络都声明同一个逻辑节点，会列出 `AMBIGUOUS` 及实际容器名，避免选错。只有遇到这种情况，才执行下面其中一种选择方式，替换为现场真实值：

```bash
# 例：只选实际容器名以sat-开头的这一套网络
docker exec leo-te-control leo-te select --prefix sat-

# 或直接列出要使用的实际容器名（至少两个，有几个就列几个）
docker exec leo-te-control leo-te select --containers 实际容器A 实际容器B 实际容器C
```

没有AMBIGUOUS时不用输入上述示例。恢复自动发现用：

```bash
docker exec leo-te-control leo-te select --auto
```

程序还会核对真实loopback身份。如果镜像内遗留其他节点的cfg，会从原LSDB的显式link_addr_pair/邻居接口和实际MAC建立只读映射，保留节点原cfg；无可核实映射时会报错并保留诊断。

## 5. 补齐队列配置与采集（现场没有qdisc时执行）

已经确认本次示例参数：业务出口16Mbit/s、10ms；已有root队列保留。

```bash
docker exec leo-te-control leo-te queues prepare --rate 16mbit --delay 10ms
```

等待命令执行完。它会：

1. 根据当前选定的真实节点/业务接口配置TBF和netem，只对空/noqueue根队列添加，记录qdisc-actions.json。
2. 在节点中安装静态备用采集器，读取tc JSON的真实整数backlog，不依赖节点Python，也不依赖人类可读的KB/MB排版。
3. 原 `/queue-probe.sh` 备份为 `/queue-probe.sh.before-leo-te-field`，新入口继续由原auto调用。单独的只读采集配置放在 `/leo-te-queue-cfg.json`，原 `/cfg.sh` 保留。
4. 等原auto每7秒把本节点发送队列追加到 `/lsdb.json` 最后的queues字段。**原auto仍是LSDB写入者，没有另起一个抢写LSDB的后台进程。**

成功标志：`QUEUES READY: N usable nodes, M native LSDB directions`。N、M以现场可用拓扑为准。

如果已有root但没有可读的rate或netem时延，程序会保留它并报告缺测，需要现场确认该口参数；不会用IGP metric冒充容量或时延。

如果提示原auto没有发布队列：请课题方确认各节点运行的是本次队列包里的auto66-lite.sh，并且每个被测节点都启动了。此命令负责配置/采集，沿用原auto维护全网，不代替课题方建网。

只查看队列状态：

```bash
docker exec leo-te-control leo-te queues status
```

## 6. 验证拓扑、队列、Ping和业务需求

```bash
docker exec leo-te-control leo-te check
```

成功标志：`READY`，并打印实际可用节点数、有向链路数、新鲜队列数、业务流数和需求表。只保留真实广告中的可用互通子网；被排除节点记在topology.json。

数据口径：队列bytes、容量Mbps、时延ms；默认7秒采样，超过两个周期视为过期。queue_bytes=0且measured=true是有效空队列；null/缺测不作为0。

第一次运行会明确生成演示需求：有条件时使用原6条示例、每条4Mbps；只有部分端点可用时取可用子集。如果这6条完全不可用，则从当前真实互通子网选择一对端点、1Mbps作基本验收，并在输出中说明。它只选业务端点，不生成网络。

```bash
docker exec leo-te-control leo-te demands
```

请先看清源、目的和速率。最大可用流量低于链路能力时，吞吐量可能已接近全送达；首要算法/下发验收仍可进行。

## 7. 明天优先完成：运行算法并验证真实下发

先只求解，看程序是否可运行：

```bash
docker exec leo-te-control leo-te plan
```

然后计算并实际下发：

```bash
docker exec leo-te-control leo-te smoke
```

等待原auto执行器处理，成功标志：

```text
SMOKE PASS: algorithm ran, native ACKs accepted, kernel SID lists match computed plan
```

查看逐条证据：

```bash
docker exec leo-te-control leo-te results
```

每条应有 `ok seg6 pass`、`kernel_matches=True` 和具体SID列表。这里既检查源节点新回执，也回读内核路由中的实际SID顺序，避免只看到一个旧seg6路由就判断成功。算法方案、原生回执、实际容器/节点映射和内核路由都保存到结果卷。

smoke后保留本次策略，方便查看。要恢复原生路由：

```bash
docker exec leo-te-control leo-te withdraw
```

**做到SMOKE PASS并能查看逐条结果，就完成了你确认的明天首要目标。** 接下来有时间再做第8步。

## 8. 完整吞吐量对比：按四条命令逐步操作

先暂停其他业务打流和自动调路程序。原auto需要继续运行，它负责LSDB和路径执行。控制器默认只在你输入命令时计算，未开启自动反复调路。

### 8.1 启动模拟流量，测Baseline

```bash
docker exec leo-te-control leo-te baseline --warmup 20 --duration 30
```

命令会撤销本需求集的本控制器策略，确认原生路由；分发静态IPv6 UDP探针，同一时刻开始持续发送。预热20秒后测30秒，以接收端实际payload字节计算Mbps。

看到 `BASELINE measured ... traffic STILL RUNNING` 后，该命令已经返回，但**节点中的同一批流量仍在继续**。不要停止控制器或打流进程。

### 8.2 保持负载，读取实时队列，运行算法并下发

```bash
docker exec leo-te-control leo-te tune
```

这条就是“在打流期间真正运行算法调优”的命令。先读取新鲜LSDB队列和容量/时延，调用当前Lyapunov求解器，再把原约定JSON写入各源节点/path.cmd，等待ok/seg6/pass，并核对内核SID列表。

成功标志 `TUNED ... traffic STILL RUNNING`。本步骤不重启发送端/接收端。

### 8.3 测调优后的吞吐量

```bash
docker exec leo-te-control leo-te te --settle 20
```

同一批流量先等待20秒稳定，再测30秒。排除调路过渡期，保持业务需求、发送速率、容量、时延和拓扑一致。成功标志 `TE measured ...`。

### 8.4 汇总前后结果并结束本次测试

```bash
docker exec leo-te-control leo-te compare
docker exec leo-te-control leo-te results
```

compare输出Baseline、TE、变化百分比及valid/invalid_measurement，随后停止本次探针并撤销本次策略，恢复原生路由。原网络、qdisc及auto队列维护保留。

默认有效性：接收总吞吐CV≤5%、前后实际发送负载差≤1%、发送负载与需求的误差≤2%（总量及单流），并核对测量期间拓扑、容量、时延、SID没有变化。无效就报错并保存原始数据；零或负提升照实输出。Ping结果不能代替此吞吐量指标。

本次流量最长运行20分钟；请连续完成四步。任一步失败会尽力停止本次流量、撤销本次策略，保留错误与原始记录。需要手动中止：

```bash
docker exec leo-te-control leo-te abort
```

可选：熟悉逐步流程后，用一条命令自动完成一周期或三周期：

```bash
docker exec leo-te-control leo-te run --cycles 1 --warmup 20 --duration 30 --settle 20
# 正式重复测试时把--cycles改为3
```

## 9. 今天的area/areas错误或其他错误，如何收集证据

```bash
docker exec leo-te-control leo-te status
docker exec leo-te-control leo-te diagnose
```

diagnose保存Docker版本/容器列表，以及节点原始LSDB、FRR JSON、真实接口、qdisc原文/JSON、auto进程/日志和原生回执。无需你在几十万字节JSON中手工找错误。

新版支持标准areas、相同结构的area和单一/default VRF包装。FRR未就绪、返回error或其他未识别结构时，会打印实际key；若原LSDB是新鲜的auto导出且结构有效，会使用该原生导出。**今天只有“类似area错误”的描述，没有完整报错，不能断定就是队列未检测到。**

导出所有结果和诊断：

```bash
mkdir -p "$HOME/联调结果"
docker cp leo-te-control:/output/. "$HOME/联调结果/"
```

把这份结果交给我，包含具体错误和原始数据。不要为排错重建课题方网络或删除其他节点。

## 10. 更换演示需求、恢复采集器与停止

切换网络/需求前先完成compare或abort，撤销旧需求的策略。更换可用子网后要重新生成演示需求时：

```bash
docker exec leo-te-control leo-te demands --reset
docker exec leo-te-control leo-te check
```

若需指定业务，可从宿主机标准输入提交JSON；单位kbps，替换为nodes中实际显示的逻辑ID：

```bash
docker exec -i leo-te-control leo-te demands --stdin <<'JSON'
[{"id":"demo1","src":"L0305","dst":"L0306","class":"class_2_embb","demand":1000}]
JSON
```

恢复备用采集器之前备份的原入口（不删除现有qdisc）：

```bash
docker exec leo-te-control leo-te queues restore
```

控制器无需运行时，先abort/withdraw确认测试已结束，再停本容器：

```bash
docker stop leo-te-control
```

结果卷保留。此交付不包含创建、接线或回收现场网络的命令。

## 11. 版本、已确认事项和验证边界

- 现场拥有Docker和网络环境，节点由课题方手动启动；各节点执行队列包auto66-lite.sh。
- 采用独立控制器、一次socket挂载；以真实接口/身份发现节点，允许部分可用子网。
- 同意备用16Mbit/s、10ms，已有root保留；IPv6 UDP模拟流量，沿用原path.cmd、回执与loopback约定。
- 首要验收是算法运行与真实SRv6下发，吞吐量对比随后执行。
- 现场Docker版本和原area报错全文未知，启动后由check/diagnose实测记录。镜像内采用可协商API的Docker CLI，参见[Docker官方API版本说明](https://docs.docker.com/reference/api/engine/)。具体本地验证结果见交付的验证记录。
- 本地实测不能替代现场验收，也不保证不同拓扑、硬件和负载下固定提升比例；按本次原始计数和有效性输出结论。

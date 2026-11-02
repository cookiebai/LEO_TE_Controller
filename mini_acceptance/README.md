# 独立小镜像：在一个测试容器里验收原网2～3节点

镜像：`leo-te-mini:20261029`，文件：`leo-te-mini-20261029.tar.gz`。

本测试独立于主控制器。使用现场已经运行的节点及原auto，不建新拓扑，不使用OpenSN。所有`mini`工具都在这个小容器内，原业务节点不用安装Python或算法依赖。通过一次Docker socket挂载访问原节点。

## 一、宿主机只做这几步

先由课题方启动原节点和auto66-lite.sh。把小镜像文件复制到宿主机“主目录/联调/测试”目录后，在宿主机执行：

```bash
cd "$HOME/联调/测试"
docker load -i leo-te-mini-20261029.tar.gz
docker run -d --name leo-te-mini --network none -v /var/run/docker.sock:/var/run/docker.sock -v leo-te-mini-results:/data leo-te-mini:20261029
docker exec -it leo-te-mini bash
```

无需先解压、联网安装或导入主控制器镜像。后面的命令全部在`mini-test:`提示符这个测试容器里输入。Docker命令若需要管理员权限，在宿主机给它们前面加sudo。

“单个容器”是一个测试控制容器：它调度原来已经存在的2～3个业务节点发Ping、读数据、装策略，不是在控制器里假造节点。控制器使用`--network none`，用`mini ping`或`mini node`从真实业务节点发包。

## 二、最快验收：输入一条命令

在测试容器内：

```bash
mini run --count 2 --repair
```

它会自动选择原网中一对有真实双向邻接、内核有End能力、相互Ping可通的节点。只验这两个节点之间的链路队列，不要求另外64个节点或全网队列都正常。

`--repair`表示选中链路的队列缺失时，按已约定示例16Mbit/s、10ms补空/noqueue根队列，保留已有root；给所选节点安装备用采集入口，仍由原auto写回LSDB。已有有效数据时直接使用，不主动重装采集器。

最后应看到：

```text
MINI PASS: 原拓扑 -> 实测队列 -> 项目算法 -> 实际内核SID -> 相互Ping
下发方式：native；队列来源：native-lsdb；原生回执：ok / seg6 / pass
```

**MINI PASS才表示原生完整链路通过。** 策略会保留供查看。看清后撤销：

```bash
mini verify
mini status
mini undo --restore-queues
```

undo仅撤本测试策略、恢复本测试备份的采集入口。配置过的实验qdisc保留，原网络和auto继续运行。`mini status`会区分当前策略和历史验收结果。

## 三、逐步手动查清楚每一项

如果想更清楚地查看过程，先确认上次已undo，然后按以下命令逐条执行。

### 1. 扫描真实节点

```bash
mini scan
mini pick --count 3
mini topo
```

pick最多选择三个已有相邻节点；没有合适三节点时会选真实两节点。数量不强制66，容器实际名称可以任意命名。它依据真实loopback、内核End路由、原LSDB邻接和接口在位信息识别。

`mini topo`会显示类似：

```text
A = L0101 / 实际容器名 / loopback=fc00:0:1::1 / SID=fc00:1:1::
B = L0102 / 实际容器名 / loopback=fc00:0:1::2 / SID=fc00:1:2::
C = L0103 / 实际容器名 / loopback=fc00:0:1::3 / SID=fc00:1:3::
TOPOLOGY PASS ...
```

以实际输出为准；A/B/C是方便操作的别名。只有两个节点时没有C。

若全网LSP的area/areas包装解析失败，但原LSDB里的本地link_addr_pair/邻居信息可读，会明确使用双方都有的真实本地邻接，并注明全网解析错误。不会按网格规律补边，也不会把“仅Ping可达”当成一条物理直连边。

### 2. 相互Ping

自动检查所有所选节点之间的方向：

```bash
mini ping --count 3
```

自己从A向B、从B向A发Ping，不用手填地址：

```bash
mini node A -- ping -6 -c 3 "$(mini ip B)"
mini node B -- ping -6 -c 3 "$(mini ip A)"
```

若有C：

```bash
mini node A -- ping -6 -c 3 "$(mini ip C)"
mini node C -- ping -6 -c 3 "$(mini ip A)"
```

需要进入原节点手动看时：

```bash
mini node A -- bash
```

现在你在真实A节点内部，可以用`ip -6 addr show`、`tc -s qdisc show`、`tail -c 4000 /lsdb.json`。输入`exit`回到`mini-test:`后再执行其他mini命令。

### 3. 读取原LSDB末尾的真实队列

```bash
mini queues
```

输出每条被测方向的接口、queue字节、capacity Mbps、delay ms和采样年龄。末尾应为`LSDB QUEUE PASS`。详细原始采样：

```bash
cat /data/queues.json
mini node A -- tail -c 4000 /lsdb.json
mini node A -- tc -s qdisc show
```

queue=0且measured=true是实测空队列，Ping流量小的时候很常见；它不是缺测。可以在Ping前后对比tc的Sent计数，确认真实包经过了这个接口。null、接口对不上、过期样本均不能通过。

若队列缺失，执行备用配置：

```bash
mini fix-queues --rate 16mbit --delay 10ms
mini queues
```

只在所选链路的空根队列上加TBF+netem，已有root保留。备用采集器无节点Python依赖，入口先备份，再原子安装；原auto仍是LSDB唯一写入者。出现`REPAIR PASS`表示已经等到**安装之后**由原auto写回的备用新样本。若已有root无法提供rate/时延，保留它并报出缺项，不填默认假值。

### 4. 运行真实项目算法，先不下发

```bash
mini plan
cat /data/plan.json
```

应看到`ALGORITHM PASS`、计算路径和via节点。它调用项目当前的`LyapunovSolver.solve_lyapunov_mcf`，输入是刚读到的局部拓扑与实测队列。默认显式演示需求为1000kbps，源为A、目的为最后一个节点；这是测试需求，不是从队列推导的业务速率。计算无需外部Redis服务。

### 5. 真正下发，再回读验证

```bash
mini apply
mini verify
cat /data/acceptance.json
```

apply前会重读数据并重新求解，部署最新方案。它把自有JSON写入源节点原/path.cmd，等待原执行器返回新biz_id的ok/seg6/pass，再核对**实际内核SID顺序和算法方案一致**，最后相互Ping。

只打印方案、只写出JSON、只发现一个旧seg6路由，都不能得到MINI PASS。未取得回执会明确失败，并保留实际状态供doctor排查。

如果还想在原节点自己看内核路由（目标随当前选择自动取到）：

```bash
# 两节点测试时，A到B
mini node A -- ip -6 route get "$(mini ip B)"
# 三节点测试时，A到C
mini node A -- ip -6 route get "$(mini ip C)"
```

应能看到`encap seg6 ... [具体SID列表]`，列表与plan.json一致。

### 6. 撤销本测试

```bash
mini undo --restore-queues
```

它检查容器ID、策略内容/路由SID、采集入口归属后再操作；不清空其他/path.cmd，不删除原节点、不重建链路、不泛删目的路由。

## 四、两个明确标注的备用方式

### A. 原LSDB队列维护有问题，先直接读真实内核队列

```bash
mini queues --direct
mini plan --direct-queues
mini apply --direct-queues
mini verify
```

仍使用真实tc数据，不把缺失填0。输出是`MINI EXPLICIT FALLBACK PASS`，并标明queue_source=direct-tc、lsdb_queues=false；**它证明算法/下发可执行，但不能证明原LSDB队列已维护成功。** 原LSDB的问题请保留doctor证据。

### B. 原/path.cmd执行器不工作，明确选择直接内核下发

先撤掉上次自有策略，确认完成后再选备选：

```bash
mini undo
mini apply --kernel
mini verify
```

这一方式仍先读真实数据、运行同一算法，再在原源节点直接**新增**专属proto196低metric路由。它用add而非replace，已有同源目的的其他策略或已有seg6路由会拒绝覆盖。实际SID回读和Ping仍须通过。

输出`MINI EXPLICIT FALLBACK PASS`，apply_mode=kernel、native_ack=null：**证明真实内核路由已安装并可达，原执行器回执未验收。** 这种方式不能冒充课题一执行器成功。

两个问题同时存在时可明确组合：

```bash
mini apply --kernel --direct-queues
```

需要内核支持SRv6且原节点已有End能力。软件不能通过捏造数据或回执，保证任意未配置现场都成功。撤销仍使用`mini undo`，仅删除SID/dev/metric仍与本测试一致的自有路由。

## 五、怎么选择特定的现有节点

先undo并恢复上次入口，再用实际容器名或唯一逻辑ID选择：

```bash
mini undo --restore-queues
mini pick --nodes L0305 L0306
```

也可选三个已有相邻节点：

```bash
mini pick --nodes L0305 L0306 L0307
```

这里是原网中的示例；如果实际容器名有前后缀，请填真实名称。手选节点不是相邻互通子网时会提示缺少中继，推荐改自动pick，而不会虚构一条边。

多套健康网络声明相同ID时，用真实名称指定，或限制实际容器名前缀：

```bash
mini scan --prefix 真实网络前缀
mini pick --count 2
```

去掉前缀限制：`mini scan --prefix ''`。原LSDB路径不同可用`mini scan --lsdb-path /真实容器内路径`，默认/lsdb.json。

## 六、失败时看哪里、如何导出

测试容器内：

```bash
mini status
mini doctor
```

doctor采集所选节点原始LSDB、cfg、IPv6地址、路由表、FRR JSON、qdisc、auto进程和日志、原回执。扫描阶段的排除/重名原因在`/data/discovery.json`；选节点互Ping尝试在`/data/pick-attempts.json`。

退出回到宿主机后导出：

```bash
exit
mkdir -p "$HOME/小测试结果"
docker cp leo-te-mini:/data/. "$HOME/小测试结果/"
```

下次回到同一测试容器：`docker exec -it leo-te-mini bash`。结果命名卷与主控制器分开。本测试不要求将主交付目录挂入容器。

测试结束先`mini undo --restore-queues`，再exit到宿主机执行`docker stop leo-te-mini`。不要同时让主算法和小测试控制同一对源目的；检测到已有策略时会明确拒绝覆盖。

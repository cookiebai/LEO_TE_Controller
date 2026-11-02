> **2026-10-28光盘离线版更新**：现场缺少解压工具时，请使用旁边的 `delivery-20261028-offline` 完整目录。先读该目录的 `README_OFFLINE_20261028.md`，执行 `prepare-offline.sh` 直接导入镜像并复制到硬盘，再按该目录同名完整说明第2～9步操作。离线版镜像标签为 `leo-te-integrated:full-offline-20261028`；本文下方保留10月27日原版步骤。不要混用两个交付目录的run.sh与镜像标签。

# 原课题1/3完整部署、队列感知TE与吞吐量测试

更新：2026-10-27。最新镜像：`leo-te-integrated:full-20261027`。

## 本版包含什么

一个Linux amd64镜像同时包含：

1. 完整继承的 `weixing1020.tar` 节点环境、FRR和原始文件。
2. 新队列包的原版 `/auto66-lite.sh`，以及保持同一queues格式的结构化tc采集入口 `/queue-probe.sh`；供应方原采集器完整保留为 `/queue-probe-original.sh`。
3. `一键部署包.rar` 中的66份原始cfg和原部署器。
4. 控制面的Lyapunov全局单路径算法、原生LSDB队列读取、SRv6策略与回执接口。
5. 三周期Baseline/TE实测工具和静态UDP6探针。

先构建/导入已更新代码的镜像，再从此镜像创建节点并按原cfg接线；接线及cfg写入完成后，才启动维护脚本。**不使用OpenSN、不生成替代cfg、不另设计拓扑。**

运行时仍是66个独立节点容器，加按需启动的控制器/部署管理容器。它们复用同一个镜像；无需Docker-in-Docker或额外下载节点镜像。

本版补齐了上一版缺少的原部署入口。上一版 `queue-20261027` 和独立控制器版继续保留，但本次完整试验请使用 **full-20261027**。

## 0. 当前这台机器的状态

本次已使用部署包实建66节点，原cfg逐字节写入；节点名为L0101～L0611。当前部署状态在本交付目录：

```text
results/deployment/deploy_state.json
```

**当前主机无需重复执行 `deploy --apply`**，可从第4步检查现场，然后直接运行第7～9步。重复部署会拒绝覆盖同名容器和已有部署状态。

要在另一台干净机器从零复现，按第1～9步顺序执行。移交到新机器时不要带本机的`results/`运行状态；镜像、run.sh、demands.json、文档与原包即可。`evidence/`是只读证据，可以一并移交。

## 1. 导入新镜像

在交付目录执行：

```bash
sha256sum -c SHA256SUMS
docker load -i leo-te-integrated-full-20261027.tar.gz
chmod +x run.sh
docker run --rm leo-te-integrated:full-20261027 --help
```

前提：Linux Docker Engine、Docker使用权限、足够的CPU/内存。部署时`run.sh`会给**部署助手**配置privileged、host网络和host PID，用于执行原部署器的veth/netns命令。普通控制器和测量助手不需要这些权限；节点权限沿用原部署包。

## 2. 先核对原接线表，不创建对象

```bash
./run.sh deploy --plan
```

必须看到：

```text
66 nodes, 132 business links, 198 spare ports, 66 management ports
```

cfg是镜像内 `integration/vendor/deploy-20261027/cfg/` 的原始文件。端口名、MAC、linkid、管理IP和备用口均来自它们；没有调用供应方源码中附带的`--gen-cfg`功能，该选项不对外开放。

原部署器只作一处必要持久化补丁：每创建一个容器就保存状态。集成适配层补充了节点入口`node`、容器label/ID核验、接口所有权alias和原子状态写入；原cfg及接线算法未更换。

## 3. 从空机建立原网络并启动维护

```bash
./run.sh deploy --apply --with-lite
```

顺序是：

1. 从已经包含新代码的镜像创建66个节点，使用原节点FRR入口。
2. 调用课题1原部署器，按cfg配对132条业务veth，两端分别移入对应节点netns。
3. 按cfg恢复198个备用口、66个管理口的名称/MAC/IP及载波语义。
4. 原样复制每个节点的cfg和两个已更新脚本。
5. 拉起所有节点的auto66-lite，维护IS-IS、SRv6、LSDB与队列。

状态和接线计划保存在 `results/deployment/`。中途失败也保留已建对象清单；不清空其他课题或其他部署。完整IS-IS收敛需要等待约60～180秒。

**已有原节点、只需更新采集脚本时**，不要重复建网：

```bash
./run.sh manage start --expected-nodes 66
```

它会安装两个新脚本并使用供应方`stop-old`避免双watcher，不重新接线。对本版刚执行过`deploy --apply --with-lite`的环境，无需再执行一次。

## 4. 配置并检查真实发送队列

原部署包默认只建网络，不安装限速队列。原队列包只采集已有qdisc；noqueue会报告缺测。

本次对比试验采用交付示例的16Mbps、10ms，执行：

```bash
./run.sh manage ensure-qdisc --expected-nodes 66 --rate 16mbit --delay 10ms
```

仅在没有已管理root的业务口添加TBF+netem；已有root保留，动作记录在`results/qdisc-actions.json`。它不修改接线、地址或FRR。链路参数应按实际实验约定调整，不能把示例值当成物理链路测量。

检查某一节点：

```bash
docker exec L0305 bash /auto66-lite.sh qshow all
docker exec L0305 bash /queue-probe.sh --once
docker exec L0305 tail -c 4000 /lsdb.json
```

`queues.links`是**本节点发送方向**的积压，单位bytes；capacity是Mbps、delay是ms，drops和overlimits为累计计数，不代替瞬时积压。宿主机备用veth不能代替此部署中两端都在节点netns里的业务队列。

## 5. 等待全网数据就绪

```bash
./run.sh manage wait --expected-nodes 66 --timeout 240
./run.sh controller --inspect --inspect-queues --once
./run.sh deploy --verify
```

严格检查应得到：

```text
existing_nodes=66, directed_links=264, fresh_measured_queues=264
```

检查内容：实时LSDB节点与实际运行节点一致、强连通、End SID已宣告、每条参与链路都有新鲜积压/容量/时延。输出为`results/topology.json`、`results/queues.json`和`results/nodes.json`。

供应方默认7秒刷新队列。控制器读取每个节点的LSDB尾部，超过两个`queue_poll_s`（默认14秒）或缺测就停止该轮下发，**不会将缺失当作0**。一个节点的LSDB有全网拓扑，但queues只有它自己的4个出口；程序会汇总66份队列。

无需另开queue-probe常驻进程。单节点维护命令仍然是：

```bash
docker exec L0305 bash /auto66-lite.sh
```

它维护L0305，不能代替其他65个节点启动维护。

## 6. 业务需求文件

交付的`demands.json`含6条示例业务，合计24Mbps，源/目的均为原cfg已有节点。例如：

```json
{"id":"row3_a","src":"L0301","dst":"L0306","class":"class_2_embb","demand":4000}
```

`demand`单位kbps。可按实际业务修改；需求不能从LSDB自动推导。同一`(src,dst)`只接受一条需求，因为课题1执行器按目的安装单条路由。

本版使用原生支持的IPv6 loopback业务目标：`L0306 → fc00:0:3::6`，`L0611 → fc00:0:6::b`。它没有扩展IPv4、任意业务网段、五元组分类或严格End.X接口。

## 7. 求解并下发一次

```bash
# 先只输出方案
./run.sh controller --demands /work/demands.json --once
cat results/plan.json

# 再实际下发
./run.sh controller --demands /work/demands.json --once --apply --ack-timeout 90
cat results/acceptance.json
```

控制器默认直接读取新LSDB的`queues`，使用现有Lyapunov全局单路径求解器，将策略原子写入**源节点**`/path.cmd/leo-te-<摘要>.json`。只有原执行器的新回执同时为`status=ok、route=seg6、ping=pass`才记录accepted。

```bash
docker exec L0301 cat /path-tuner.status
docker exec L0301 ip -6 route get fc00:0:3::6
```

`plan.json`保留真实队列、容量、时延、接口、采样时间和来源；`health.json`报告最近一轮状态，判断文件时间，避免把旧plan当作最新结果。

## 8. 需要持续调优时

```bash
docker run -d --name leo-te-controller-full20261027 --restart unless-stopped --network none \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD:/work" -v "$PWD/results:/output" \
  leo-te-integrated:full-20261027 controller \
  --demands /work/demands.json --apply --interval 5 --ack-timeout 90

docker logs -f leo-te-controller-full20261027
```

每轮完成后等待5秒。所有节点维护脚本持续更新数据；过期/缺测时不会继续用无效队列调优。停止控制器不会自动删除已有策略。

## 9. 三周期吞吐量对比

如果第8步的持续控制器在运行，先停它，避免它在Baseline阶段重新下发：

```bash
docker stop leo-te-controller-full20261027
```

若只执行过第7步的`--once`，无需这条。然后：

```bash
./run.sh benchmark --demands /work/demands.json \
  --cycles 3 --warmup 10 --duration 30 --ack-timeout 90
```

每周期：撤销本需求集对应策略 → 核实原生IS-IS路由 → 同步发送IPv6 UDP（预热10秒、测量30秒）→ 算法读取新鲜队列并下发SRv6 → 验收Ping → 以相同需求和发送速率重测TE。

工具从接收端实际payload字节计算Mbps，不用配置速率冒充吞吐量；同时记录发送端实际负载、每秒样本、测量中原生队列和路由前后回读。静态探针会临时复制到涉及节点，不依赖原节点安装Python/iperf。

默认有效性要求：接收总吞吐量CV≤5%、A/B实际发送负载差≤1%、实际发送与约定负载误差≤2%（总量和每条流）；拓扑、End SID、容量和时延指纹不得改变。不满足就标记`invalid_measurement`，不伪装成功。零/负提升原样记录，吞吐量是IPv6 UDP应用payload，不能直接等同TCP或物理链路线速。

结果目录由命令打印，对应：

```text
results/throughput/<时间戳>/
  README.md                三周期结果表
  result.json              逐阶段指标及有效性
  demands.json             本次需求
  cycle-1/baseline/         发送、接收、路由、队列原始证据
  cycle-1/controller/       算法方案、SRv6回执、日志
  cycle-1/te/               TE原始证据
  cycle-2/ ...
  cycle-3/ ...
```

默认结束时撤销这份需求的策略，恢复原生路由；需要保留用`--keep-policy`。其他课题策略不会被清空；若有外部策略导致Baseline仍走SRv6，会报错而不冒充原生Baseline。

## 10. 撤销策略、停止维护、回收网络

只恢复本业务集原生路由：

```bash
./run.sh benchmark --demands /work/demands.json --withdraw-only
```

暂停某一节点的维护（不删除拓扑、不主动停止FRR）：

```bash
docker exec L0305 bash /auto66-lite.sh stop
```

确实要移除本次部署时：

```bash
./run.sh deploy --undo
```

回收依赖`results/deployment/deploy_state.json`，先核验容器ID、label和宿主接口alias，再调用原部署器回收。容器被其他人替换、接口不属于本部署时拒绝删除。**保留状态文件**；不要手工全量删除所有Docker对象，也不要把这份状态拿到另一台机器回收。

## 原始交付与适配源代码

`task1-deploy/`保留RAR原件内容，`queue-vendor/`保留原ZIP内容；两份auto66-lite和queue-probe完全一致。镜像内的`deploy`入口使用这66份cfg，不猜邻接、不生成新cfg。

本次严格就绪、cfg/MAC核验、SRv6回执和吞吐量原始记录见交付目录`evidence/`与`验证报告.md`。历史“缺少启动入口”的结论已由本次提供的RAR补齐；后续以本文为准。

全网加压时曾遇到供应方采样字段缺失而中止测量。最终版本采集器改用`tc -j -s`提供的整数backlog及计数，避免依赖人类可读文本的单位/排版；仍每7秒由原auto66-lite维护同一个LSDB尾部，缺失与过期检查没有放宽。原probe脚本及原包仍保留可核对。

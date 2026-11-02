# 链路发送队列采集 · 交付说明

日期：2026-10-27。对应《发给课题1/3：链路 qdisc 配置与队列积压采集要求》(2026-10-21)。
环境：66 节点(6×11)FRR+SRv6 容器仿真，节点名 `L0101`~`L0611`。

## 0. 文件夹里有什么

| 文件 | 是什么 | 放到哪 |
|---|---|---|
| `auto66-lite.sh` | 节点自动化脚本，内含队列采集调度 + `qshow`/`qset`/`qclear` 子命令 | 每个节点容器内 `/auto66-lite.sh` |
| `queue-probe.sh` | 单容器队列采集器，只读、一次一采、JSON 打标准输出 | 每个节点容器内 `/queue-probe.sh` |
| `lsdb_L0305_含队列示例.json` | 真实节点的完整 `/lsdb.json`(621 KB)，尾部带队列段 | 参考 |
| `lsdb_L0305_队列尾部.json` | 上面那份的尾部截取，只有队列，1.7 KB | **先看这个** |

两脚本与现网 66 个容器内的文件逐字节一致：`auto66-lite.sh` md5 `60b3ed7d8709191fd44be168755c11ac`，`queue-probe.sh` md5 `6bfdfa7b613a4ebc652a22a6651afa49`。

**不需要重做镜像。** 队列数据不在镜像层里，是容器内脚本每 7 秒写进 `/lsdb.json` 尾部的。若在别的机器上新起容器，把上面两个脚本 `docker cp` 进容器即可(容器需 `CAP_NET_ADMIN` + iproute2，现有镜像已具备)。

## 1. 队列数据在哪一个字段

出口只有一个：容器内 `/lsdb.json`，顶层 `queues` 段。

```
/lsdb.json
├── self / loopback / locator / neighbors / link_addr_pair   ← 原有拓扑与编址
├── lsdb                                                     ← FRR IS-IS 数据库原文(含 SRv6 子 TLV)
├── updated_at, trigger, queue_poll_s                        ← 本段是第几次导出、周期
└── queues                                                   ← 发送队列(本文重点)
```

顶层还有 `queue_poll_s: 7`，即队列刷新周期秒数；改周期用容器环境变量 `AUTO_QPOLL`。

## 2. 字段口径

`queues` 头部：

| 字段 | 含义 |
|---|---|
| `node` / `namespace` | 本节点名 / 命名空间标识，形如 `container:L0305` |
| `netns_inode` | 网络命名空间 inode(`4026532720`)，可用来和 `docker inspect` / `nsenter` 对齐 |
| `timestamp` | Unix 采样时刻，秒带小数(容器与宿主同时钟) |
| `sampled_at` | 同一时刻的可读形式，便于人眼核对 |
| `links_measured` / `links_total` | 本节点几个口采到了真值 / 共几个业务口 |

`links[]` 每条 = **一个发送方向**：

| 字段 | 单位/含义 |
|---|---|
| `src` `dst` `interface` | 源节点 / 对端节点 / **本节点容器内的出口接口**；`src→dst` 有向，A→B 与 B→A 各在其源节点里各记一条 |
| `queue_bytes` | 瞬时排队字节 = `tc` 的 `backlog`，**停手即回 0** |
| `qlen_packets` | 瞬时排队包数 = `tc` 的 `qlen` |
| `drops_total` | 累计丢包，只增不减，**不能当积压用** |
| `overlimits_total` | 累计超速令牌事件。TBF 拥塞时先涨这个，`dropped` 可能一直为 0 |
| `capacity_mbps` | 由根队列 `rate` 解析(多 rate 取最小)，即这条链路当前被限速到的容量 |
| `delay_ms` | 该口所有 netem `delay` 之和。TBF 的 `latency` **不算时延**，只用于算队列上限 |
| `qdisc` | 根队列类型 + 累计 `Sent 字节/包数`，用于确认业务流量真的过了这个队列 |
| `carrier` | 该口链路层是否在位(`up`) |
| `measured` | `true`=真值；`false`=没采到，看 `reason` |
| `reason` | `not-measurable-kind`(根队列是 noqueue 等不可测) / `no-backlog-field` / 多个根队列不采 |
| `source` | `task1-queue-probe`，课题三若转发请改成 `task3` |

**缺失 ≠ 0。** 不可测的链路一律 `queue_bytes: null` + `measured: false` + `reason`，绝不拿 noqueue 的零值冒充实测量。消费方请照此实现过期/缺失判定：`timestamp` 超过 2 个 `queue_poll_s` 视为陈旧，`measured:false` 视为缺测而不是"队列空"。

## 3. 实测示例(2026-10-27，L0305，eth1 正在被打满)

```json
{"src":"L0305","dst":"L0306","interface":"eth1",
 "queue_bytes":292400,"qlen_packets":200,"drops_total":0,"overlimits_total":33252,
 "capacity_mbps":16.0000,"delay_ms":10.000,
 "qdisc":"tbf 1: Sent 16853116b/11574p","carrier":"up",
 "measured":true,"source":"task1-queue-probe"}
```

同一份导出里，另外三个没有负载的口 `queue_bytes: 0` 但 `measured: true`(装了队列、只是当时不忙)；停流后 `tc` 回读 `backlog 0b 0p`、累计 `Sent 29292086 bytes 20086 pkt`、`overlimits 58217`。

这满足原要求 §4 的验收 1 和 2：`qdisc` 里的 `Sent 字节/包` 随业务增长、超瓶颈时能采到非零 `backlog`/`qlen`，且 `drops` 为 0 而 `overlimits` 上涨(只看 drops 会漏判拥塞)。

## 4. 三种读法

```bash
# A. 读文件(推荐，零侵入)：容器内 /lsdb.json 的 queues 段
docker exec L0305 tail -c 2600 /lsdb.json

# B. 现场采一次，只读、不落盘、不动网络
docker exec L0305 bash /queue-probe.sh --once

# C. 看 tc 原文
docker exec L0305 bash /auto66-lite.sh qshow all      # 或 qshow eth1
```

## 5. 队列怎么造出来的(课题四要造、要撤都用这个)

```bash
# 预览，什么都不改
docker exec L0305 bash /auto66-lite.sh qset eth1 rate=16mbit burst=32kbit lat=100ms delay=10ms limit=1000
# 真落地(本节点四个业务口全装)
docker exec L0305 bash /auto66-lite.sh qset all  rate=16mbit burst=32kbit lat=100ms delay=10ms limit=1000 --yes
# 撤销，拆回默认 noqueue
docker exec L0305 bash /auto66-lite.sh qclear all --yes
```

装的是 `root tbf(rate/burst/latency)` + `parent 1:1 netem(delay/limit)`，与原要求 §3 示例同构。`qset`/`qclear` **只动 `tc`**，不碰 FRR、路由、地址，每次真落地都记进容器 `/auto-debug.log`(`QSET-APPLY` / `QCLEAR-APPLY`)。

注意两点：

1. `qset` 会先 `tc qdisc del dev X root` 再加——**会覆盖平台已配好的根队列**。若某条链路平台已有限速，请不要对它用 `qset`，直接按 §4 读它的统计即可(采集器读现有根队列，不重建、不与子队列相加)。需要"非 noqueue 就拒绝覆盖"的保护，说一声我们加。
2. 双向要分头配：`L0305→L0306` 在 L0305 容器内 `eth1` 配；`L0306→L0305` 要在 L0306 容器内它自己那个口配。

## 6. 本环境的取数位置(与原要求 §1 的唯一差别)

原要求里"目的节点对应的宿主机 veth 出口也算向 dst 发送"这条路，在本环境**不存在**：业务 veth 对**两端都在容器命名空间内**。2026-10-27 实测，L0305 四个业务口 `iflink` = 109/106/111/156，在宿主根命名空间里都查不到对应接口；宿主侧 264 个 `ve_*` 全是备用口(198)与管理口宿主端(66)。

结论：**队列只能在源节点容器内读**，`docker exec` 或读容器内 `/lsdb.json` 都行；请不要指望从宿主机 `tc` 读到业务队列，也不要拿宿主 `ve_*` 的统计顶替。方向仍按原要求的正确那条走——源节点容器内出口 = `src→dst`，接口与对端关系用 `/cfg.sh` 的 `ports[*].linkid` 判定(采集器就是这么做的)，不按接口名猜。

## 7. 归谁做

| 事项 | 归属 |
|---|---|
| 队列采集、口径、写进 `/lsdb.json` | **课题一，已完成** |
| 队列/时延/容量造出来、撤销(`qset`/`qclear` 已提供) | 需要造的一方调用，本侧不主动改现网 |
| 把 `queues` 转发进 Redis、字段单位与保留策略 | **课题三**(现要求 §5) |
| IS-IS 邻接、SRv6 下发、`/path.cmd` 批量策略 | 课题一，已在跑 |
| 灌负载流量以产生可观测积压 | 负载生成侧(课题一提供工具，不代跑) |

本侧不做：上报/HTTP/告警、把队列写进 IS-IS LSP 或路由、改课题三/课题四的 docker 与集成代码。

## 8. 待你们确认的三件事

1. **采样周期**：现 7 秒(全网 66 节点同时采)。原要求建议 1 秒——我们试过 1 秒档，66 节点同时采样把宿主 CPU 顶到约 97%。要 1 秒请改为**只对被测瓶颈链路**加密，或给出可接受的资源上限。
2. **容量/时延来源**：现在 `capacity_mbps`/`delay_ms` 是从队列本身解析的。装了 qdisc 才有值，没装的就是 `null`。若需要全量链路的物理时延/容量台账，应由平台链路参数表提供，不宜由队列反推。
3. **AF_XDP 提醒仍然有效**：本镜像含 XDP/AF_XDP。我们这条链路上 IS-IS 报文与 ping/TCP 业务走内核栈，`qdisc` 的 `Sent` 计数随业务增长已实测(见 §3)；若你们有用户态直接发包的业务，那部分不经过 egress qdisc，需从实际用户态发送队列另补统计。

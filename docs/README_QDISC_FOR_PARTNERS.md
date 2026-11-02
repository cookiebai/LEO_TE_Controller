# 发给课题1/3：链路 qdisc 配置与队列积压采集要求

日期：2026-10-21。目的：为课题四控制面提供**每条有向链路真实发送队列积压**。节点镜像依据 `weixing1020.tar`。

## 需要提供的数据

每条 `src→dst` 链路需有：源/目的节点名（`L0101` 等）、真实发送接口及所在命名空间、`backlog_bytes`、采样 Unix 时间；同时提供链路容量 Mbps、时延 ms。建议起始采样周期 1 秒，控制器求解周期 5 秒，再依据联调资源调整。`backlog_bytes` 为瞬时排队字节，`qlen` 为瞬时包数，`drops` 为累计丢包计数，三者不能互换。

目前镜像中的 `/lsdb.json` 只有拓扑/路由能力，未包含队列、物理时延、容量；`link_data.csv` 只有 delay 记录。**开启 qdisc 不会自动把队列写入 LSDB/Redis**，必须采集和上报。当前课题四适配器已支持同机通过 Docker 和宿主机 `tc` 直接读取 qdisc，不要求立刻修改课题3 Redis。

## 1. 先确认队列位置与方向

在节点中查看（替换容器名和数据接口）：

```bash
NODE=实际节点容器名
IFACE=eth1
docker exec "$NODE" cat /cfg.sh
docker exec "$NODE" ip -d link show dev "$IFACE"
docker exec "$NODE" tc -s qdisc show dev "$IFACE"
docker exec "$NODE" tc -j -s qdisc show dev "$IFACE"
```

如果业务接口是 `noqueue`，继续在宿主机检查平台创建的 veth：

```bash
HOST_IFACE=平台实际veth名
ip -d link show dev "$HOST_IFACE"
tc -s qdisc show dev "$HOST_IFACE"
tc -j -s qdisc show dev "$HOST_IFACE"
```

**方向规则**：源节点容器内出口队列代表 `src→dst`；目的节点对应的宿主机 veth 出口，也代表向 `dst` 发送。源节点的宿主机 veth 出口通常是向源节点接收的方向，不能直接标成源节点发送队列。

`/cfg.sh` 的 JSON `ports[*]` 包含 `eth`、`veth`、`linkid`，例如 `linkid=["L0205L0305"]`。请用链路两端映射定位，不要按接口名猜方向。管理接口（通常 eth0）、lo、未使用端口不配置业务限速。

## 2. 已有平台限速/时延 qdisc 的情况

如果已经有 TBF/HTB/netem 等根队列，**保留现有层级**，直接读统计。请由平台现有链路管理代码调整参数，避免后台配置器下一轮将人工命令覆盖。

无需为了“开启统计”重建已有 qdisc：

```bash
tc -j -s qdisc show dev "$HOST_IFACE"
```

当前课题四适配器读取单个根 qdisc 的 backlog。根统计已包括其子队列时，不能再把 root 与 child backlog 相加。若使用 `mq` 等多队列、并行链路或多个瓶颈，需要另行约定归一化统计，不可简单合计。

## 3. 只有 noqueue 时配置发送限速

下面以 **16 Mbit/s、32 KB burst、100 ms TBF latency** 为示例，具体值应替换为平台链路参数。TBF 的 `latency` 用于计算队列限制，**不是链路传播时延**；有 netem 子队列时还需由子队列限制实际排队。仅安装 FIFO 并不能保证产生可观测积压，需要实际瓶颈限速和足够负载。

在节点容器的数据出口配置，节点需具备 `CAP_NET_ADMIN`，并安装 iproute2：

```bash
NODE=实际节点容器名
IFACE=eth1
docker exec "$NODE" tc qdisc add dev "$IFACE" root handle 1: \
  tbf rate 16mbit burst 32kb latency 100ms
docker exec "$NODE" tc -j -s qdisc show dev "$IFACE"
```

或者，在宿主机实际承载该方向的 veth 配置（与前一方案选择正确位置，避免双重限速）：

```bash
HOST_IFACE=平台实际veth名
sudo tc qdisc add dev "$HOST_IFACE" root handle 1: \
  tbf rate 16mbit burst 32kb latency 100ms
sudo tc -j -s qdisc show dev "$HOST_IFACE"
```

若平台尚未模拟传播时延，可在此 TBF 下添加 netem（示例 10 ms；已有时延时不重复添加）：

```bash
sudo tc qdisc add dev "$HOST_IFACE" parent 1:1 handle 10: \
  netem delay 10ms limit 1000
sudo tc -s qdisc show dev "$HOST_IFACE"
```

`limit 1000` 是 netem 排队包数上限，可能取代 TBF 原内部 FIFO 的排队限制；应按实际链路速率、带宽时延积和实验目标设置。双向链路要分别配置两端真正的发送方向。

也可使用随附脚本，它默认只检查，遇到已有非 noqueue 根队列会拒绝覆盖：

```bash
# 在项目目录运行；示例只给指定容器的指定数据接口新增队列
docker cp scripts/configure_integration_qdisc.sh "$NODE":/tmp/configure_qdisc.sh
docker exec "$NODE" bash /tmp/configure_qdisc.sh "$IFACE"
docker exec "$NODE" bash /tmp/configure_qdisc.sh "$IFACE" \
  --apply --rate 16mbit --burst 32kb --latency 100ms
```

## 4. 验证统计覆盖真实业务

```bash
# 无负载时记录，再运行平台业务流量，持续查看同一个接口
docker exec "$NODE" tc -s qdisc show dev "$IFACE"
docker exec "$NODE" tc -j -s qdisc show dev "$IFACE"
```

验收要求：

1. 真实业务经过时，所选 qdisc 的 `bytes/packets` 计数增长。
2. 受控提高发送负载到瓶颈速率以上，能采到非零 `backlog`/`qlen` 或累计 drops 的变化。低负载 backlog=0 合理，但不能用 noqueue 的零值冒充实测队列。
3. 上报 `src,dst,interface,namespace,backlog_bytes,timestamp`，每个方向单独标识。时间同步，明确数据过期规则；采集失败时标记缺失，不能伪装为 0。
4. 原有 IS-IS 邻接、课题3探测及课题四 SRv6 loopback Ping 仍正常。

镜像含 XDP/AF_XDP：需核实业务是否经过 Linux egress qdisc。AF_XDP 用户态发送可能绕过普通队列；不能仅凭已挂载 TBF 就断言其积压覆盖用户态业务。请以实际业务负载下 qdisc 计数增长验证；不经过时需从实际用户态发送队列补充统计。依据：[Linux AF_XDP 文档](https://www.kernel.org/doc/html/latest/networking/af_xdp.html)。

## 5. 若由课题3统一写入 Redis

请董科研给出 Redis 地址、端口、访问方式，以及每个字段的单位、方向、时间戳、保留策略。推荐以明确单位输出可原子更新的归一化快照，由课题四 `--metrics-file` 接收：

```json
{
  "timestamp": 1792540800.0,
  "links": [
    {"src":"L0101","dst":"L0102","queue_bytes":1500,"capacity_mbps":16,"delay_ms":9.2,"source":"task3"}
  ]
}
```

这是**建议的新增桥接格式**，不是声称现有 Redis 已包含这些字段。时间戳换成当前采样时间，覆盖所有参与链路。现有 `data:ts0:10310:10410:delay` 中的节点编码、delay 单位、单向/往返含义需由课题3说明；不得把 delay 数值当成 backlog。

## 6. 撤销本次人工新增配置

只有确认是本次在原 `noqueue` 上人工新增的根队列，且平台要求恢复时，才执行对应位置的命令：

```bash
# 节点容器中的本次新增根队列
docker exec "$NODE" tc qdisc del dev "$IFACE" root

# 或宿主机中的本次新增根队列
sudo tc qdisc del dev "$HOST_IFACE" root
```

删除 root 会连同其子队列一并删除，不能用于撤销原有平台配置。原有 qdisc 的恢复应由平台重新应用保存的链路配置。

参数参考：[tc](https://man7.org/linux/man-pages/man8/tc.8.html)、[TBF](https://man7.org/linux/man-pages/man8/tc-tbf.8.html)、[netem](https://man7.org/linux/man-pages/man8/tc-netem.8.html)。

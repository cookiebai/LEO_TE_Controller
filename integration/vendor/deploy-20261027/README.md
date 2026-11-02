# 一键部署包 · 66 节点现场形状仿真环境

日期：2026-10-27。给课题四/集成方：**一条命令建出整套环境**，接口连接关系完全以 `cfg.sh` 为准。

## 1. 里面有什么

| 文件 | 作用 |
|---|---|
| `deploy.sh` | 入口(封装 `deploy_from_cfg.py`) |
| `deploy_from_cfg.py` | **cfg 驱动**的建网器：读 cfg → 建容器 → 接 veth → 原样写回 cfg → 拉起节点 |
| `cfg/` | 66 份 `/cfg.sh`，就是我们这套环境正在用的那一份(6×11 环网，132 条业务链路) |
| `auto66-lite.sh` | 节点自动化(IS-IS+SRv6 组网、`/lsdb.json` 导出、队列采集与 `qset/qclear`) md5 `60b3ed7d8709191fd44be168755c11ac` |
| `queue-probe.sh` | 单节点发送队列采集器 md5 `6bfdfa7b613a4ebc652a22a6651afa49` |

## 2. 一条命令

```bash
bash deploy.sh                    # 干跑：打印接线计划，不动任何东西
bash deploy.sh --apply --with-lite    # 真建：容器+veth+写cfg+起节点
bash deploy.sh --verify           # 体检：邻居数/载波/端到端 ping/队列尾部
bash deploy.sh --undo             # 回收：只删本工具建过的对象
```

前提：宿主机有 docker、`sudo`(免密最好)、python3、iproute2，并且镜像已在本地
(`docker load -i weixing1020.tar`)。镜像名不是默认的 `frr-srv6-qw:latest` 就加 `IMG=你的名字`。
建完 IS-IS 收敛约 60~180 秒，别在 60 秒内判定失败。

## 3. cfg 是唯一事实源(这是本次的重点)

以前我们自己的复刻器是"按内置 6×11 拓扑建网，再反过来生成 cfg"。**现在这个工具反过来**：
你给什么 cfg，它建什么网，`cfg.sh` 一个字节都不改，落进容器后 md5 与你手上原件一致。

cfg 里每一项都被照用：

| cfg 字段 | 落地方式 |
|---|---|
| `name` | 容器名、节点身份(必须是 `L` + 4 位数字，X 1~6 / Y 1~11) |
| `ports[*].eth` | 容器内接口名，改名成它 |
| `ports` 的键(MAC) | 该接口的真实 MAC，建 veth 时就设成这个值 |
| `ports[*].linkid` | 两个节点名按字典序拼接的串。**相同 linkid 的两端 = 一条链路**，各进自己容器的 netns(两端都不留在宿主) |
| `linkid: []` | 备用口：一端进容器，一端留宿主并置 down，容器内看到 `NO-CARRIER` |
| `man.mac/eth/ip` | 管理口 eth0，地址按 `ip/16` 配，宿主端用 `man.veth` 的名字 |

不自洽会当场列出来并停止(不会留下半成品)：某条 linkid 只有一端(对端 cfg 缺文件)、一端超过两、接口名或宿主侧接口名重复、容器名已被占用。

**要用你们平台自己导出的 cfg**，就这样取：

```bash
mkdir -p mycfg && for c in $(sudo docker ps --format '{{.Names}}'); do
  sudo docker exec $c cat /cfg.sh | tr -d '\n' > mycfg/$c.cfg.sh
done
CFGDIR=mycfg bash deploy.sh            # 先干跑核对条数对不对
```

## 4. 建完怎么验

```bash
bash deploy.sh --verify
sudo docker exec L0305 vtysh -c 'show isis neighbor'      # 每节点应有 4 个 Up
sudo docker exec L0305 ping6 -c 3 fc00:0:1::1             # 环回格式 fc00:0:<环>::<位>，两位都是十六进制
sudo docker exec L0305 tail -c 2600 /lsdb.json            # 末尾 queues 段
sudo docker exec L0305 bash /auto66-lite.sh qset all rate=16mbit burst=32kbit lat=100ms delay=10ms --yes
```

队列默认是空的：不装 qdisc 的链路在 `queues` 里是 `queue_bytes:null` + `measured:false`，**那是"没测"不是 0**。造队列和读法见上一个包《队列采集交付/README.md》。

## 5. 已经实测到哪一步(说实话)

在 VM 上做了 4 节点环的小样(L0901~L0904，独立于现网 66 台)：

- 接线表解析：4 份 cfg → 4 条业务链路、4 备用口、4 管理口，配对无误；
- `cfg.sh` 原样写入：容器内外 md5 逐份一致(`a56ed5d5…`/`00e04957…`)；
- MAC 照 cfg 落地：`eth1=02:dd:cc:bb:aa:18`、`eth2=…:20`、`eth5=…:3c`、`eth0=…:5a`，四个值与 cfg 的键完全相同；
- 口名与载波：业务口 `UP/LOWER_UP`(两端都在各自 netns 里)，备用口 `NO-CARRIER`(宿主端 down)，符合现场语义；
- lite 能在建好的容器里读表起跑(`/auto-debug.log` 有 STEP0 linkmap=cfg)；
- 回收：`--undo` 删掉 4 容器 + 16 个宿主侧接口，靠状态文件，不做任何"按名前缀全删"。

再拿现网 66 份真 cfg 做解析核对：**66 节点 / 132 条业务链路 / 198 备用口 / 66 管理口**，与今天恢复后实测的 264 个邻居(每条链路两端各计一次)完全对上。

**没做的一件事**：小样没跑 IS-IS 端到端。原因是 lite 的拓扑门只认 X≤6、Y≤11(现场 66 节点规格)，测试用的 `L09xx` 被门判为越界——这是设计行为，不是接线失败；而 VM 上 66 个合法网格名已被现网环境占用，我们没有动它。**这套工具在干净机器上对 66 份 cfg 直接 `--apply` 才是完整路径**。如果要在现 VM 上停 4 台做真网验证，需要先批准，我们再动手。

## 6. 边界

本包只负责"把 cfg 变成交换好的容器网络 + 把节点拉起来 + 出数据"。不做：上报 Redis/HTTP、改你们侧代码、把队列写进 IS-IS 或路由。`--undo` 之外的清理动作请自己确认对象归属。

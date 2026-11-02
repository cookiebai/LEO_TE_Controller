# 课题四控制面 Docker 联调说明

> 最新完整部署、原生队列、SRv6及吞吐量实测流程见 [完整部署指南](README_FULL_DEPLOY_20261027.md)，使用 `leo-te-integrated:full-20261027`。

> 2026-10-27 新队列包与原镜像集成版本请看 [新版逐步运行指南](README_QUEUE_INTEGRATION_20261027.md)。下面保留2026-10-21独立控制器版本说明；新版默认读取原生LSDB的queues。

日期：2026-10-21。适配依据：用户提供的 `weixing1020.tar`，不依赖 `dky-node.tar.gz`。

## 已完成的接口

```text
运行中的课题1/3节点 /cfg.sh + /lsdb.json + FRR实时LSDB
                         ↓
IS-IS拓扑解析 + 实际链路qdisc积压/容量/时延
                         ↓
现有 Lyapunov 全局单路径求解器（Python/SciPy MILP）
                         ↓
课题1原生 /path.cmd/*.json → 课题1路径执行器
                         ↓
/path-tuner.status（route=seg6、ping=pass）
```

镜像内包含控制面源码、Python 依赖、编译后的 C++ 引擎、内置 Redis 和 Docker CLI。联调入口使用当前实验的 Python 求解器；旧 C++ 示例引擎保留但不启动（其中有示例目的节点 `sat_core_1`）。内置 Redis 只监听私有 Unix socket，无需另开 Redis 容器，无宿主机 Redis 端口冲突。算法本身未更换；增加了 Unix socket 连接配置。

同一台 Linux 主机、同一 Docker 引擎部署。控制器通过 Docker socket 访问已经运行的节点，**不会创建卫星拓扑或自动启动/重配节点 FRR**。只读分析和实际下发使用不同命令。

## 原始镜像检查结论

| 项目 | 实际结果 |
|---|---|
| 格式与架构 | Docker/OCI 镜像归档，Linux amd64，归档没有 RepoTag |
| 镜像 ID | `sha256:a75aea1079394729d5c00ecc98b7ad63d0a823d5933257601d635a3e656d430b` |
| 拓扑 | 根目录 `/lsdb.json`，FRR IS-IS Level-2 JSON |
| 本节点 | `L0305`，loopback `fc00:0:3::5/128`，wrapper locator `fc00:3:5::/48` |
| 静态快照时间 | `2026-10-19T07:41:06+00:00`，只是归档中保存的历史状态 |
| 快照规模 | 49 个可识别节点、129 条有向链路、46 个有宣告 End SID 的节点 |
| 缺少 End SID 的节点 | `L0203`、`L0210`、`L0305`；求解时排除，不凭 wrapper locator 假定能力就绪 |
| 节点身份映射 | `/cfg.sh` 实际内容是 JSON，含 `name`、`ports[*].eth/veth/linkid` |
| 路径接收 | `/frr-auto66.sh` 扫描 `/path.cmd/*.json`，约 5 秒一轮，同源目的选最高 priority |
| 路径反馈 | `/path-tuner.status`，包含安装状态、route 类型、Ping 结果 |
| 队列字段 | 原始 LSDB **没有** backlog、容量或物理时延；IGP metric=10 不能当作这些量 |
| 遥测 CSV | `link_data.csv` 共 767 条，全是 `data:ts0:<src>:<dst>:delay`；值为 `时间戳,测量值`，没有队列字段 |

静态快照已保存在 `integration/fixtures/weixing1020-lsdb.json`，便于离线复测。实际读取时先取 `/lsdb.json` wrapper，再执行同一节点的 `vtysh -c 'show isis database detail json'` 更新其内部 LSDB，避免把镜像中冻结的快照当成运行现场。wrapper 的 `updated_at` 由对方脚本在拓扑变化时更新，**不是周期心跳**。

## 1. 构建与离线交付

```bash
cd /home/lenovo/bxr_lab/LEO_TE_Controller
docker build -t leo-te-controller:integration-20261021 .
docker run --rm leo-te-controller:integration-20261021 --help

# 如需重新导出
docker save leo-te-controller:integration-20261021 | gzip > leo-te-controller-integration-20261021.tar.gz
sha256sum leo-te-controller-integration-20261021.tar.gz
```

接收方导入：

```bash
docker load -i leo-te-controller-integration-20261021.tar.gz
```

已提供的项目镜像与课题1/3镜像是两个镜像：**给归档命名或导入镜像本身，不会产生运行中的拓扑**。课题1仍需用自己的平台建立 veth、写入 `/cfg.sh` 并启动其 FRR/自动化脚本。控制器随后读取运行节点。

节点侧还需满足其原有 FRR 权限与内核配置：此版 zebra/isisd 请求 `CAP_SYS_ADMIN`（仅 `NET_ADMIN` 不足以启动其 FRR），且需要 IPv6 forwarding 和数据接口 `seg6_enabled=1`。普通容器的 `/proc/sys` 可能只读，应由课题1平台在建节点时正确设置 sysctl。此要求属于课题1/3节点，**控制器自身不需要 SYS_ADMIN 或 privileged**。

## 2. 离线检查及算法冒烟测试

```bash
cd /home/lenovo/bxr_lab/LEO_TE_Controller
mkdir -p deployment/output

# 只解析随原镜像提供的真实LSDB，不需要Docker socket
docker run --rm -v "$PWD/deployment/output:/output" \
  leo-te-controller:integration-20261021 \
  --lsdb-file /app/integration/fixtures/weixing1020-lsdb.json --inspect --once

# 演示求解：真实快照 + 明确标注的示例需求与假设链路参数
docker run --rm -v "$PWD/deployment/output:/output" \
  leo-te-controller:integration-20261021 \
  --lsdb-file /app/integration/fixtures/weixing1020-lsdb.json \
  --demands /app/integration/fixtures/demands.example.json \
  --offline-demo --once
```

`--offline-demo` 明确标记：缺省容量 16 Mbps、时延 10 ms、队列 0 B；这些是**演示假设**，不是课题1/3的实测数据。该模式禁止 `--apply`。

## 3. 现场只读检查

课题1完成拓扑启动后：

```bash
cd /home/lenovo/bxr_lab/LEO_TE_Controller
docker run --rm --network host \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD/deployment/output:/output" \
  leo-te-controller:integration-20261021 --inspect --once
```

默认发现所有 `/cfg.sh` 中有 `Lrrcc` 身份的运行容器；容器名可以是平台生成的其他名称。`--lsdb-container 实际容器名` 可指定 LSDB 读取节点。发现同一节点身份被两个容器占用时拒绝运行，避免混用两套拓扑。

## 4. 准备业务需求

创建 `deployment/demands.json`，例如：

```json
[
  {"id":"business_1","src":"L0101","dst":"L0103","class":"class_1_urllc","demand":1000},
  {"id":"business_2","src":"L0102","dst":"L0104","class":"class_3_background","demand":4000}
]
```

这里 `demand` 单位为 **kbps**，链路容量为 **Mbps**，时延为 **ms**，队列为 **bytes**。业务量应由联调需求/业务发生器提供，不能从 LSDB 推导。没有需求时要求显式提供文件，不沿用旧脚本随机生成流量的行为。`class` 可取 `class_1_urllc`、`class_2_embb`、`class_3_background`。

对方执行器按 `(src,dst)` 安装一条路由，当前拒绝同一源目的对应多条独立业务需求。若确需分别优化，双方须增加按业务分类/策略路由接口。

## 5. 真实数据求解，先输出方案

```bash
cd /home/lenovo/bxr_lab/LEO_TE_Controller
docker run --rm --network host \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD/deployment/demands.json:/input/demands.json:ro" \
  -v "$PWD/deployment/output:/output" \
  leo-te-controller:integration-20261021 \
  --demands /input/demands.json --once
```

现场模式读取实际 qdisc：优先源节点本地出口；如为 `noqueue`，尝试目的节点 `/cfg.sh` 给出的宿主机 veth（宿主机向目的节点发送，方向才是 `src→dst`）。读取根 qdisc 的 `backlog`，不叠加子 qdisc，避免重复计数。读取 TBF/netem 文本中的 rate/delay；未能测得的字段不默认为 0。多队列根 `mq` 暂不支持，需要显式归一化指标。

若 qdisc 只有容量、没有时延，可增加双方确认的 `--delay-ms 数值`；若容量也由平台外部给定，可用 `--capacity-mbps 数值`。这些参数只填缺失值，**不会覆盖实测值**；队列缺失仍拒绝计算。qdisc 配置详见 [README_QDISC_FOR_PARTNERS.md](docs/README_QDISC_FOR_PARTNERS.md)。

也支持 `--metrics-file /input/metrics.json`，格式：

```json
{
  "timestamp": 1792540800.0,
  "links": [
    {"src":"L0101","dst":"L0102","capacity_mbps":16,"delay_ms":9.2,"queue_bytes":1500,"source":"partner-adapter"}
  ]
}
```

示例时间戳必须替换为当前采样 Unix 秒，且需覆盖参与计算的所有有向链路。默认允许最大 10 秒年龄，时钟超前最多 5 秒，使用 `--metrics-max-age` 调整。文件应通过临时文件加 rename 原子更新。当前未把 CSV 的 `delay` 自动映射进来，因为其单位、单向/往返含义和节点号编码未在归档中定义；这也不能代替队列采样。

## 6. 实际下发和反馈

审核 `deployment/output/plan.json` 后，在第5步命令最后加入 `--apply`。去掉 `--once` 可持续运行，默认每轮完成后等待 5 秒。也可使用 `deployment/compose.yaml`：先准备 `demands.json`，运行 `docker compose -f deployment/compose.yaml up -d`；默认只输出方案。确认后在 compose 的 command 追加 `--apply`。

下发内容完全匹配原生格式：

```json
{"biz_id":"leo_te_business_1","src":"L0101","dst":"L0103","mode":"end","priority":10,"via":["L0102","L0103"]}
```

写入**源节点** `/path.cmd/leo-te-<业务ID摘要>.json`，先写临时文件再 rename，执行器不会读到半份 JSON。不会把全网策略文件放进所有节点。只检查现有 tuner，不自动启动对方脚本；tuner 未启动时提示失败。对方脚本通常由其平台执行 `bash /frr-auto66.sh start`，请由课题1按现有部署流程启动。

`acceptance.json` 只有在反馈同时满足 `status=ok`、`route=seg6`、`ping=pass` 且安装时间属于本次提交后才标记 `accepted`；单纯写出 JSON 或安装命令返回成功不算验收。默认等待 60 秒，受对方约 5 秒扫描和 20 秒冷却影响。该反馈验证的是目的 loopback 的 Ping，不证明吞吐量、所有业务地址或严格逐跳行为。

## 7. 输出、停止与策略撤销

| 输出 | 含义 |
|---|---|
| `topology.json` | 拓扑、宣告能力、原始 IGP metric、解析告警 |
| `plan.json` | 本轮方案、指标来源、是否为离线演示 |
| `path.cmd/Lrrcc/*.json` | 可复核的原生策略副本；现场实际文件名采用摘要 |
| `acceptance.json` | 当前方案的现场反馈；dry-run 不标记生效 |
| `health.json` | 最近一轮成功/失败及时间；失败时旧 plan 可能仍保留，须结合本文件判断 |

`docker stop 控制器容器名` 停止计算。**停止控制器不会撤销节点中已存在的策略**。连续模式中输入失败会停止该轮下发并保留已安装路由；撤销需求、业务改名和拓扑长期失效时，需要删除相应源节点 `/path.cmd/leo-te-*.json` 文件，对方执行器下一轮撤销其路由。不要删除其他课题的策略文件。输出目录中也可能保留以前生成的策略副本，以当前 `plan.json` 为准。

Docker socket 用于读取节点文件、运行 `tc`/`vtysh`，在 `--apply` 时写入源节点命令文件；这是同机适配的访问边界。宿主机网络模式用于只读采集宿主机 veth 的 `tc` 数据。控制器无需 `--privileged`，qdisc 配置由节点/平台拥有者执行。

## 8. 联调前仍需对齐的事实

这些缺项无法通过镜像打包自动补出：

1. **运行现场**：课题1平台启动拓扑的步骤、节点数量、哪些节点已宣告 End SID。归档静态快照只有 49 个节点，不能用“6×11”直接补齐边。
2. **业务对象**：原生执行器安装的是目的 `fc00:0:r::c/128` 的 IPv6 路由。当前没有 IPv4-over-SRv6、任意业务网段、五元组或分流接口，真实业务必须选用此目标或由课题1扩展执行器。
3. **容量、时延和队列**：真实限速 qdisc 所在命名空间、接口和方向，链路速率、时延、队列上限及采样周期。要测到真实业务经过的队列。
4. **课题3遥测**：若后续采用 Redis，请董科研提供 Redis 地址/访问方式、节点号到 `Lrrcc` 映射、delay 单位和含义，以及新增 backlog 字段/单位/方向/时间戳。当前 CSV 不能支持队列感知 TE。
5. **数据路径**：镜像存在 XDP/AF_XDP，需确认业务走 Linux/SRv6 转发还是用户态转发。qdisc 计数与控制策略必须覆盖同一业务路径，不能用探测包数值代替业务队列。
6. **规模与路径语义**：对方脚本硬编码 6×11、只用 node End SID；其 `endx` 分支未组装宣告的 End.X SID。本适配使用 `mode=end`，不声称严格 End.X 逐跳或现有 25×20 规模可直接下发。

## 验证命令

```bash
docker run --rm --entrypoint python3 leo-te-controller:integration-20261021 \
  -m unittest discover -s tests -p 'test_integration.py' -v
```

原项目 `./lab.sh` 及历史实验入口保留。现场 Docker 联调使用本文的新入口，避免启动旧 OpenSN 部署脚本和旧 `fd00:` 下发器。

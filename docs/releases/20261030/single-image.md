# 控制器与新版 FRR 合成一个镜像

交付文件：`leo-te-controller-merged-frr-20261030.tar.gz`。

导入后的唯一新镜像名称：`leo-te-controller:merged-frr-20261030`。

镜像 ID：`sha256:a52974a5d032f181a845ea68aabf33e2bb060ff03ddc17584afef00cd04d6cb9`。

本次以原控制器为基础，将课题一新版 FRR 镜像的文件实际合入同一个文件系统。归档只包含一个镜像条目；此前的 `leo-te-bundle-20261030.tar.gz` 是双镜像归档，这次请使用上面的新文件。

## 1. 原内容如何保留

两份原始 `.tar.gz` 文件均保留不变。

- 控制器 `/app` 下 37 个原文件全部保持原路径、原内容，包括 Lyapunov 算法、拓扑/队列读取、下发验证、配置、探针和测试文件。
- 课题一 `/auto66-lite.sh`、`/queue-probe.sh`、`/start-frr.sh` 三个脚本保持原内容；FRR 程序和缺少的依赖库合入原路径。
- 原镜像中存在 715 处内容或模式不同的同路径项目，主要为系统工具、库和系统配置。默认运行环境保留控制器版本，双方原件同时保存在下面目录，按原路径组织：

```text
/opt/leo-te-merge/originals/controller/
/opt/leo-te-merge/originals/node/
```

同一个文件系统的同一路径只能有一个生效版本。为了让原 FRR 程序正常运行，仅对以下系统文件做了必要的兼容配置：

```text
/etc/passwd
/etc/group
/etc/shadow
/etc/gshadow
/etc/ld.so.cache
/var/cache/ldconfig/aux-cache
```

前四项追加原 FRR 账户/组条目，后两项由 `ldconfig` 重新生成。它们的原件也按上述目录留存。未改写算法或课题一脚本，没有安装新版本软件包。

完整路径、哈希与冲突清单在镜像内 `/opt/leo-te-merge/merge-manifest.json`。

## 2. 一次导入一个新镜像

所有下列 Docker 命令在宿主机终端输入。现场文件位于其他位置时，先进入其实际目录：

```bash
cd /home/lenovo/bxr_lab/联调
docker load -i leo-te-controller-merged-frr-20261030.tar.gz
docker image inspect leo-te-controller:merged-frr-20261030 --format '{{.Id}}'
```

无需手工解压。应看到：

```text
Loaded image: leo-te-controller:merged-frr-20261030
```

本次归档不再导入两个原镜像标签。如果此前已经导入过原镜像，Docker 仍会保留那些已有标签，不影响这份新镜像的使用。

## 3. 继续作为控制器使用

默认启动行为与原控制器相同：等待手动检查和运行算法，不自动创建网络、打流或下发路径。现场节点继续沿用课题方已接线的拓扑和 auto 维护流程。

先查看是否存在同名容器：

```bash
docker ps -a --filter name=leo-te-control
```

没有同名容器时启动：

```bash
docker run -d --name leo-te-control --network none \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v leo-te-field-results:/output \
  leo-te-controller:merged-frr-20261030
```

如果同名容器已经存在，先核对其镜像及归属。`docker start` 会继续使用旧容器创建时的镜像，不会自动切换为本次新镜像；不要为换镜像直接删除尚在测试的控制器。可使用另一个明确的新容器名，并相应替换后续命令中的 `leo-te-control`。

然后继续[现场运行指南](../../../README_FIELD_20261029.md) 第 4 步及后续命令：

```bash
docker logs leo-te-control
docker exec leo-te-control leo-te help
docker exec leo-te-control leo-te nodes
docker exec leo-te-control leo-te queues status
```

缺少 qdisc 参数或有效队列时，按原约定执行：

```bash
docker exec leo-te-control leo-te queues prepare --rate 16mbit --delay 10ms
```

检查通过后运行算法并实际下发：

```bash
docker exec leo-te-control leo-te check
docker exec leo-te-control leo-te plan
docker exec leo-te-control leo-te smoke
docker exec leo-te-control leo-te results
```

验收仍为 `SMOKE PASS`、新回执的 `ok seg6 pass` 和 `kernel_matches=True`。完整吞吐量测试仍按[现场运行指南](../../../README_FIELD_20261029.md) 执行 `baseline → tune → te → compare`。

## 4. 同一个镜像也可供节点部署使用

如果课题方要以本镜像创建业务节点，沿用其已有接线、节点身份和 `/cfg.sh` 配置，使用同一镜像名称，并将容器命令设为 `node`：

```text
镜像：leo-te-controller:merged-frr-20261030
命令：node
```

该模式通过新增的薄入口调用原封不动的 `/start-frr.sh`，启动 zebra 和 isisd，并移除控制器专用的发现排除标记。创建节点时仍需课题方提供网络管理权限和原拓扑接线。

`node` 模式启动 FRR 后，auto 脚本仍按课题方原流程逐节点运行；本次未改变其默认启动行为。导入镜像不会更新已有节点容器、重建拓扑或自动生成 `/cfg.sh`、运行时 `/lsdb.json`。现场已就绪的原节点可以继续供新控制器读取，无需因为导入本镜像而全部重建。

## 5. 本次验证

- 文件清单对比：核对控制器 15,586 个、节点 4,124 个条目，排除 Docker 运行时文件；原件按原路径或冲突备份路径均可找到，校验无失败。
- 控制器 37 个应用文件、三个课题一脚本内容一致。
- 控制器 50 项单元测试全部通过。
- 默认控制器实际启动通过，`leo-te help` 正常。
- 隔离网络内实际通过原 `/start-frr.sh` 启动 zebra、isisd，`vtysh` 可用；节点模式正确去除控制器标记。
- 检查 28 个 FRR ELF 程序及模块，动态库全部可解析。
- 导出归档仅包含一个镜像，重新导入后新镜像 ID 一致。

现场原有节点、队列、策略均未修改。本次验证确认合并内容和运行环境；现场实时队列、实际路径下发及吞吐量仍按上述验收步骤测试。

验证报告：`单镜像整合_20261030_验证.json`。校验文件：`leo-te-controller-merged-frr-20261030.SHA256SUMS`。

# 两份原镜像合成一个交付文件

交付文件：`leo-te-bundle-20261030.tar.gz`。

这是可以直接 `docker load` 的多镜像归档，一份文件同时包含下面两个原镜像；没有修改镜像内文件、镜像标签、默认启动入口或依赖，也没有重新构建镜像。

| 用途 | 导入后的镜像名称 | 原镜像 ID |
|---|---|---|
| 算法控制器 | `leo-te-controller:field-20261029` | `sha256:563adbf61556c6f83323a0a90ffd62e011544fc9b24353fd30fcc2d850724a12` |
| 课题一 FRR 节点 | `frr-auto66-lite:new-20261029` | `sha256:37ef9dd6e917dc5888a9faf5ff1063a79e42ede859200f62503fc23a668e79e1` |

**一份文件、一次导入；导入后仍是两个镜像，分别启动节点与控制器。** 这保留了两份镜像的原内容和运行方式，没有将两个文件系统合成一个新镜像或让一个容器同时承担两种角色。

原来的两份 `.tar.gz` 文件保留不变。本文件可替代它们作为搬运和离线导入材料。

## 离线导入

将合包文件复制到宿主机，进入其真实所在目录。以下为当前本机路径：

```bash
cd /home/lenovo/bxr_lab/联调
docker load -i leo-te-bundle-20261030.tar.gz
```

无需手工解压，无需解压软件。Docker 需要管理员权限时，在 Docker 命令前加 `sudo`。

检查两个镜像都存在：

```bash
docker image inspect leo-te-controller:field-20261029 --format '{{.Id}}'
docker image inspect frr-auto66-lite:new-20261029 --format '{{.Id}}'
```

应与上表两个完整 ID 一致。合包验证记录为 `双镜像合包_20261030_验证.json`；校验文件为 `leo-te-bundle-20261030.SHA256SUMS`。

## 后续执行

课题方继续按其部署流程创建、接线并启动节点，在被测节点执行 `/auto66-lite.sh`。导入镜像不会替换已有容器，也不会自动创建网络。

控制器继续按同目录[现场运行指南](../../../README_FIELD_20261029.md) 操作：将其第 2 步的镜像导入命令换成上面的合包导入命令；第 3 步及后续步骤保持原命令，仍使用 `leo-te-controller:field-20261029`。

没有已存在的同名控制器时，启动命令仍为：

```bash
docker run -d --name leo-te-control --network none \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v leo-te-field-results:/output \
  leo-te-controller:field-20261029
```

然后按[现场运行指南](../../../README_FIELD_20261029.md) 执行 `nodes`、队列准备/检查、`check`、`plan`、`smoke` 和 `results`。已存在同名容器时，先核对归属及镜像再继续，不要重复创建或直接删除。

合包验证只确认原镜像完整保留且能够导入；网络、实时队列和实际 SRv6 下发仍按[现场运行指南](../../../README_FIELD_20261029.md) 的现场验收步骤检查。

# 2026-10-30 单镜像整合构建源码

这里收录原构建工作目录中的脚本和 Dockerfile。大体积 rootfs、镜像归档、自动生成的文件清单在本地构建目录中生成。

准备两个已有镜像：`leo-te-controller:field-20261029`、`frr-auto66-lite:new-20261029`。后者由课题方交付。需要 Docker 和 Python 3.11+（脚本使用 `hashlib.file_digest`）。在仓库根目录执行：

```bash
deployment/build-merged.sh
```

默认构建目录为仓库旁的 `integration-artifacts/merged-single-rebuild`。可将一个专用目录作为第一个参数；构建过程会更新该目录里的中间文件。脚本只导出原镜像、组装上下文并构建新镜像，不启动业务节点。

```bash
deployment/build-merged.sh /absolute/path/to/dedicated-build-dir
python3 /absolute/path/to/dedicated-build-dir/validate_files.py
python3 /absolute/path/to/dedicated-build-dir/validate_runtime.py
```

文件验证会导出新镜像并逐条比较文件；运行验证会创建临时隔离容器，检查控制器和 FRR 启动以及动态库，结束后回收这些临时容器。FRR 启动验证需要宿主机支持 Docker 的 `NET_ADMIN`、`NET_RAW`、`SYS_ADMIN` 能力。

`assemble.py` 保留控制器路径，将节点独有文件加入原路径，对冲突文件备份双方原件。`context/setup-runtime.py` 追加 FRR 账户，Dockerfile 重建动态库缓存。原始镜像 ID 标签和 [历史验证记录](../../docs/releases/20261030/) 对应 2026-10-30 的交付，重新构建后的镜像 ID 应以实际结果为准。

重新导出：

```bash
docker save leo-te-controller:merged-frr-20261030 | gzip > leo-te-controller-merged-frr-20261030.tar.gz
sha256sum leo-te-controller-merged-frr-20261030.tar.gz
```

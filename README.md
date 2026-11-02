# LEO TE Controller

面向低轨卫星网络的 SRv6 流量工程控制器。使用实际拓扑、链路容量、时延和队列数据，调用 Lyapunov 单路径求解器，输出路径并核对原生执行器回执及内核 SID。

## 选择运行入口

| 使用场景 | 说明与入口 |
| --- | --- |
| 现场已有课题方网络，运行控制器 | [现场运行指南](README_FIELD_20261029.md)，镜像 `leo-te-controller:field-20261029` |
| 一个镜像同时提供控制器和 FRR 节点运行模式 | [2026-10-30 单镜像整合](docs/releases/20261030/single-image.md)，镜像 `leo-te-controller:merged-frr-20261030` |
| 用现有网络中的 2～3 个节点做局部验收 | [小规模验收工具](mini_acceptance/README.md) |
| 按交付的 66 节点 cfg 部署完整网络 | [完整部署指南](README_FULL_DEPLOY_20261027.md) |
| 在 OpenSN 上复现实验 | [实验说明](README_TE_EXPERIMENT.md)、[完整运行手册](README_FULL_RUN_GUIDE.md)、[12×10 手册](12x10手动运行指南.md)、[25×20 手册](25x20手动运行指南.md) |

现场控制器读取已存在的节点；启动控制器不会自动建立拓扑。OpenSN 的实验环境见独立仓库 [cookiebai/workspace](https://github.com/cookiebai/workspace/tree/server-handoff-20260915)。

## 近期更新

- 2026-10-21：LSDB 解析、实测 qdisc 指标、原生 `path.cmd` 下发与回执检查，支持 Redis Unix socket。
- 2026-10-27：原生队列采集、交付 cfg 部署、Baseline/TE 接收端吞吐量对比。
- 2026-10-29：现场节点识别、队列准备、内核 SID 核对、独立小规模验收工具与 C/Go 探针。
- 2026-10-30：控制器与 FRR 单镜像整合、冲突原件保留、文件与启动验证。
- 2026-11-02：整理源码和文档入口，收录镜像整合构建源码，修复本地运行时的配置与示例路径。

历史接口细节见 [早期集成说明](README_INTEGRATION.md)、[原生队列说明](README_QUEUE_INTEGRATION_20261027.md) 和 [合作方 qdisc 约定](docs/README_QDISC_FOR_PARTNERS.md)。镜像整合的历史验证报告见 [发布目录](docs/releases/20261030/)。报告中的通过结果对应当时归档；当前源码的测试不代表已重新完成现场吞吐量验收。

## 从源码检查

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m unittest mini_acceptance.test_mini -v
.venv/bin/python -m integration.controller --help
```

构建基础镜像：

```bash
docker build -t leo-te-controller:integration-20261021 .
```

现场镜像构建使用 `deployment/build-field.sh`，需要 GCC、Go、Docker 和基础镜像；首次构建会下载并校验 Docker CLI。单镜像整合的重建步骤见 [构建说明](deployment/merged/README.md)。

## 目录

| 目录 | 内容 |
| --- | --- |
| `integration/` | 现场控制器、拓扑与队列适配、部署与吞吐量工具 |
| `python_te_solver/`、`cpp_te_engine/` | Python 算法与 C++ 引擎 |
| `mini_acceptance/` | 现有小规模子网验收 |
| `deployment/` | 镜像、Compose 与整合构建支持 |
| `tests/` | 控制器与实验回归测试 |
| `docs/` | 接口说明和发布验证记录 |

运行输出、构建上下文、镜像归档与 Python 缓存由 `.gitignore` 排除。`build/` 编译产物和 `results/console_manual_*.log` 终端日志已停止跟踪；本地镜像与实验原始记录保留。

# 2026-11-02 源码整理验证

本次将 2026-10-21 至 2026-10-30 的控制器、队列、部署和验收源码纳入版本控制，并补充仓库首页及镜像整合构建入口。

修复 `integration.field`、`integration.benchmark`、`integration.controller`、`mini_acceptance.cli` 中的配置/示例路径：按模块所在位置解析仓库根目录，兼容源码目录与镜像内 `/app` 布局。

## 本次检查

| 检查 | 结果 |
| --- | --- |
| `python -m unittest discover -s tests` | 91 项通过 |
| `python -m unittest mini_acceptance.test_mini` | 21 项通过 |
| 两个 C 探针的 GCC 语法检查，含 `-Wall -Wextra -Werror` | 通过 |
| `go test integration/field_queue_probe.go` | 编译通过；该文件没有 Go 测试用例 |
| Python AST 与 Shell 语法检查 | 通过 |
| 使用合成 rootfs 验证整合脚本 | 新增文件、冲突双方备份与运行时文件排除均通过 |
| `git diff --check` | 通过 |

Python 测试使用本地已有的 Python 3.11 虚拟环境。测试命令见[仓库首页](../README.md)。本次未重建完整交付镜像、部署网络或重测现场吞吐量。2026-10-30 的镜像验证保存在[独立发布目录](releases/20261030/)，与本次源码检查分开记录。

## 清理范围

经确认，停止跟踪 `build/` 下 38 个编译产物，以及 `results/console_manual_20260915_131334.log`、`results/console_manual_20260915_131616.log` 两份终端日志。对应本地文件与历史 Git 提交保留。

原始实验结果、镜像归档、旧版源码和供应方脚本保留。生成的构建文件与镜像归档通过忽略规则排除。

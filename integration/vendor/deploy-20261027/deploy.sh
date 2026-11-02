#!/bin/bash
# 一键起环境: 建容器 + 按 cfg 接线 + 原样写 cfg + 起 lite + 体检
#
#   bash deploy.sh                 干跑, 只出接线计划(不动任何东西)
#   bash deploy.sh --apply         真建
#   bash deploy.sh --apply --with-lite   真建并拉起每个节点
#   bash deploy.sh --verify        体检(邻居/载波/端到端 ping/队列尾部)
#   bash deploy.sh --undo          只回收本工具建过的对象(靠 deploy_state.json)
#
#   CFGDIR=/路径/cfg  bash deploy.sh --apply   改成用你自己那批 cfg
#   IMG=frr-srv6-qw:latest  镜像名
set -e
cd "$(dirname "$0")"
CFG=${CFGDIR:-cfg}
IMG=${IMG:-frr-srv6-qw:latest}
PY=${PY:-python3}

command -v "$PY" >/dev/null || { echo "缺 python3"; exit 1; }
command -v docker >/dev/null || { echo "缺 docker"; exit 1; }
[ -d "$CFG" ] || { echo "找不到 cfg 目录: $CFG"; exit 1; }

exec $PY deploy_from_cfg.py --cfg-dir "$CFG" --image "$IMG" \
     --lite auto66-lite.sh --probe queue-probe.sh "$@"

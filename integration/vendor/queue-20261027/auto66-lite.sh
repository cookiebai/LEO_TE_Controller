#!/bin/bash
# =====================================================================
# frr-auto66-lite.sh — 66 节点(6×11 Torus)自动化【只认 /cfg.sh 的精简版】
#
# 与 frr-auto66.sh 的区别: 去掉一切"自适应/猜测"逻辑。接线表写了什么就做什么,
# 接线表没写的口(备用口、无对端口)一律不碰。方向不再按 eth 序号猜。
#
# 现场用法(容器内 root, 一条命令):
#     bash /root/frr-auto66-lite.sh
# 自动完成:
#   1) 认身份(/cfg.sh 的 name -> hostname -> $NODE_ID -> 人工热补文件)
#   2) 补齐空基底缺的进程(mgmtd/zebra/isisd), 只写内存不写 frr.conf
#   3) 配 IS-IS 主干 + SRv6 locator + End SID
#   4) 逐口读 ports[*].linkid 定对端, 两端必然落进同一个 /64
#   5) 链路/地址自愈(平台 admin down 冲刷掉的全局地址每轮重放补回)
#   6) /lsdb.json 导出: 拓扑事件驱动, 队列尾段(/queue-probe.sh 在位时)每 QP 秒刷新
#   7) /path.cmd/*.json -> SRv6 seg6 源路由 + 回执
#
# 子命令: status | gate | report | stop | restart | stop-old | qshow | qset | qclear
# 开关: AUTO_CFGSH(接线表路径, 默认 /cfg.sh)、AUTO_IFACES(强制指定业务口)、
#       AUTO_QPOLL(队列尾部刷新秒数, 默认 7; 运行中可 echo 3 > /run/queue-poll 直接改)
#
# 兜底: /cfg.sh 里没有 ports(我方仿真平台的 69 B 占位版就是这种)时, 退回平台既定
#       规律 eth1=列+1 eth2=列-1 eth3=行-1 eth4=行+1, 并在日志里说明用了兜底。
# =====================================================================

SELF=$(readlink -f "$0" 2>/dev/null || echo "$0")
MODE=${1:-start}

H=5        # 主循环周期(秒)
D=10       # 幽灵接口判定阈值(秒)
C=60       # 同接口滚链冷却(秒)
N=300      # 滚链限次后的静默窗口(秒)
G=600      # 守护进程稳定运行多久后崩溃计数放松(秒)
B=30       # 整栈重启后的静默(秒)
LP=30      # LSDB 轮询周期(秒)
QSH=${AUTO_QUEUE_SH:-/queue-probe.sh}   # 队列采集器; 文件在位才往 /lsdb.json 尾部附 queues
QP=${AUTO_QPOLL:-7}    # 有队列时刷新尾部的秒数(队列是瞬时值, 不能只跟着 LSP 变化走)
# 运行中可改的口子: 每 tick 重读这个文件, 不用重启角色。例: echo 3 > /run/queue-poll
QPFILE=${AUTO_QPOLL_FILE:-/run/queue-poll}
PC=5       # 路径策略扫描周期(秒)
COOL=20    # 同 (src,dst) 签名冷却(秒)

LOGF=${AUTO_LOG:-/var/log/frr/auto66-lite.log}
DBGF=${AUTO_DBG:-/auto-debug.log}
# 非 root 或只读根目录时退到 /tmp, 免得 dbg 每次调用都往 stderr 喷 Permission denied
: >> "$DBGF" 2>/dev/null || DBGF=/tmp/auto66-lite-debug.log
: >> "$LOGF" 2>/dev/null || LOGF=/tmp/auto66-lite.log
IDFILE=/etc/frr-auto/id
IDLOCK=/var/lib/frr-auto/identity
LSDBF=/lsdb.json
# 拓扑本体缓存: `show isis database detail json` 一次 620 KB, 是导出里最贵的一步。
# 队列 ticks 复用上次本体、只重写尾部, 否则 66 台会把宿主 CPU 打满(实测 load 95)。
BODYF=${AUTO_LSDB_BODY:-/run/lsdb-body.part}
PCDIR=/path.cmd
PSTAT=/path-tuner.status
STATEF=/auto66.state
EXCLUDE="${AUTO_EXCLUDE:-eth0}"
IFACES="${AUTO_IFACES:-}"
CFGSH="${AUTO_CFGSH:-/cfg.sh}"

# 占位版 cfg.sh(没有 ports)时的兜底规律, 与我方平台建网脚本一致
FALLBACK_IFACES="eth1 eth2 eth3 eth4"

if command -v ping6 >/dev/null 2>&1; then PING6=ping6
elif command -v ping >/dev/null 2>&1 && ping -6 -c1 -W1 ::1 >/dev/null 2>&1; then PING6="ping -6"
else PING6=""; fi

dbg() { echo "[$(date '+%T')] $*" >> $DBGF 2>/dev/null; }
say() { echo "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }
in_grid() { [ "$1" -ge 1 ] 2>/dev/null && [ "$1" -le 6 ] 2>/dev/null && [ "$2" -ge 1 ] 2>/dev/null && [ "$2" -le 11 ] 2>/dev/null; }

# =====================================================================
# 名字 -> (X,Y): 身份、对端、SID 派生共用一份规则
#   r1n11 / R01N01 / row1col11 / x1y11 > l1-1 / l01.02 > l0101 / l111
#   loose 另接受人工写法 1,11 / 1-11；越出 6×11 一律拒绝
# =====================================================================
X_OUT=""; Y_OUT=""; XY_RULE=""; XY_OOR=""
pair2() {
  local g
  g=$(printf '%s' "$1" | grep -oE '[0-9]{1,3}')
  set -- $g
  [ $# -ge 2 ] || return 1
  X_OUT=$((10#$1)); Y_OUT=$((10#$2))
  in_grid $X_OUT $Y_OUT
}
split_digits() {
  local d=$1 k x y
  case "$d" in ''|*[!0-9]*) return 1 ;; esac
  for k in 1 2; do
    x=${d:0:$k}; y=${d:$k}
    [ ${#y} -ge 1 ] && [ ${#y} -le 2 ] || continue
    X_OUT=$((10#$x)); Y_OUT=$((10#$y))
    in_grid $X_OUT $Y_OUT && return 0
  done
  XY_OOR=$d
  return 1
}
xy_of() {
  local t=$1 mode=${2:-strict} m
  XY_RULE=""; XY_OOR=""
  t=$(printf '%s' "$t" | tr 'A-Z' 'a-z' | tr -d ' \r\t')
  [ -n "$t" ] || return 1
  if [ "$mode" = loose ]; then
    m=$(printf '%s' "$t" | grep -oE '^[0-9]{1,2}[-_.,][0-9]{1,2}$')
    if [ -n "$m" ]; then
      pair2 "$m" && { XY_RULE="explicit-pair"; return 0; }
      XY_OOR=$m; return 1
    fi
    m=$(printf '%s' "$t" | grep -oE '^[0-9]{2,4}$')
    if [ -n "$m" ]; then
      split_digits "$m" && { XY_RULE="explicit-digits"; return 0; }
      return 1
    fi
  fi
  for m in $(printf '%s' "$t" | grep -oE 'r0*[0-9]{1,2}[^a-z0-9]*n0*[0-9]{1,2}|row[^a-z0-9]*0*[0-9]{1,2}[^a-z0-9]*col[^a-z0-9]*0*[0-9]{1,2}|ring[^a-z0-9]*0*[0-9]{1,2}[^a-z0-9]*node[^a-z0-9]*0*[0-9]{1,2}|(^|[^a-z0-9])x[^a-z0-9]*0*[0-9]{1,2}[^a-z0-9]*y[^a-z0-9]*0*[0-9]{1,2}'); do
    pair2 "$m" && { XY_RULE="labeled-pair"; return 0; }
    XY_OOR=$m
  done
  [ -n "$XY_OOR" ] && return 1
  m=$(printf '%s' "$t" | grep -oE '(^|[^a-z0-9])l0*[0-9]{1,2}[-_.:]0*[0-9]{1,2}' | head -1)
  if [ -n "$m" ]; then
    pair2 "$m" && { XY_RULE="l-separated"; return 0; }
    XY_OOR=$m; return 1
  fi
  m=$(printf '%s' "$t" | grep -oE '(^|[^a-z0-9])l[0-9]{2,4}' | head -1)
  if [ -n "$m" ]; then
    m=$(printf '%s' "$m" | sed 's/[^0-9]//g')
    split_digits "$m" && { XY_RULE="l-digits"; return 0; }
    return 1
  fi
  return 1
}

# ---- 编址派生 ----
hexn() { local v=$(printf '%x' $1); [ "$v" = 0 ] && v=; printf '%s' "$v"; }
hex4() { printf '%04x' $1; }
derive_addr() {
  MY_X=$X_OUT; MY_Y=$Y_OUT
  X_HEX=$(hexn $MY_X); Y_HEX=$(hexn $MY_Y)
  NAME=$(printf 'L%02d%02d' $MY_X $MY_Y)
  LOOPBACK="fc00:0:${X_HEX}::${Y_HEX}/128"
  NET="49.0001.$(hex4 $MY_X).$(hex4 $MY_Y).00"
  LOCATOR="fc00:${X_HEX}:${Y_HEX}::/48"
  LOCATOR_ADDR="fc00:${X_HEX}:${Y_HEX}::"
}
lo_of()  { xy_of "$1" || return 1; echo "fc00:0:$(hexn $X_OUT)::$(hexn $Y_OUT)"; }
sid_of() { xy_of "$1" || return 1; echo "fc00:$(hexn $X_OUT):$(hexn $Y_OUT)::"; }
net_of() { xy_of "$1" || return 1; echo "49.0001.$(hex4 $X_OUT).$(hex4 $Y_OUT).00"; }

# =====================================================================
# 接线表 /cfg.sh: {"man":{...},"name":"L0305","ports":{"<MAC>":{"eth","linkid","veth"}}}
#   ports 以 MAC 为键; linkid 是两节点名字典序拼接的单元素数组("L0305L0306");
#   linkid=[] 表示该口无对端(备用/下行口) -> 不配址、不挂 IS-IS
# =====================================================================
keyed_value() {
  local f=$1 k v low
  [ -r "$f" ] || return 1
  low=$(tr 'A-Z' 'a-z' < "$f" 2>/dev/null | tr -d '\r')
  [ -n "$low" ] || return 1
  for k in name node_id nodeid node_name nodename hostname label id; do
    v=$(printf '%s\n' "$low" | grep -oE "\"$k\"[[:space:]]*:[[:space:]]*\"?[^\",}[:space:]]+" | head -1 | sed -E 's/^[^:]*:[[:space:]]*//; s/^"//')
    [ -n "$v" ] && { printf '%s\n' "$v"; return 0; }
    v=$(printf '%s\n' "$low" | grep -oE "(^|[[:space:];])$k[[:space:]]*=[[:space:]]*\"?[^\"[:space:];]+" | head -1 | sed -E 's/^[^=]*=[[:space:]]*//; s/^"//')
    [ -n "$v" ] && { printf '%s\n' "$v"; return 0; }
  done
  return 1
}

ID_SRC=""; ID_CONFLICT=""; CFG_SELF=""; CFG_NAME=""
LINKMAP="fallback"; LM_LOG=1
CFG_LINKS=""; declare -A PEER_X PEER_Y EXPECT

cfg_link_lines() {
  local s
  [ -r "$CFGSH" ] || return 1
  s=$(tr -d ' \r\t\n' < "$CFGSH" 2>/dev/null)
  printf '%s' "$s" | grep -oE '"eth":"eth[0-9]+","linkid":\["[^"]+"\]' |
    sed -E 's/"eth":"(eth[0-9]+)","linkid":\["([^"]*)"\]/\1 \2/'
}

# 逐口登记对端坐标: linkid 里除本节点外的另一个名字就是对端。
# 表里的自身份与已确定身份不符 -> 判定为整镜像复制/上一节点残留的陈旧文件, 整张表弃用。
linkmap_init() {
  local e lid tk best n sx=$MY_X sy=$MY_Y
  PEER_X=(); PEER_Y=(); EXPECT=()
  CFG_LINKS=$(cfg_link_lines)
  CFG_SELF=""; CFG_NAME=""; SELF_FROM_LINKS=""
  LINKMAP=fallback
  [ -n "$CFG_LINKS" ] || { [ $LM_LOG = 1 ] && dbg "STEP0 $CFGSH 无 ports/linkid -> 用兜底口表($FALLBACK_IFACES)"; LM_LOG=0; return 0; }
  # 本节点身份 = 接线表里被点名次数最多的名字(每条链路两端都点名, 只有本节点被 4 条同时点名)
  best=0
  for tk in $(printf '%s\n' "$CFG_LINKS" | grep -oE 'L[0-9]{4}' | sort -u); do
    n=$(printf '%s\n' "$CFG_LINKS" | grep -c -- "$tk")
    [ $n -gt $best ] && { best=$n; SELF_FROM_LINKS=$tk; }
  done
  [ "${best:-0}" -le 1 ] && SELF_FROM_LINKS=$(keyed_value "$CFGSH" | tr 'a-z' 'A-Z')
  CFG_NAME=$SELF_FROM_LINKS
  if [ -n "$CFG_NAME" ] && xy_of "$CFG_NAME"; then CFG_SELF=$(printf 'L%02d%02d' $X_OUT $Y_OUT); else CFG_SELF=""; fi
  X_OUT=$sx; Y_OUT=$sy
  # 逐口登记对端坐标: linkid 里除本节点外的另一个名字就是对端
  while read -r e lid; do
    [ -n "$e" ] || continue
    for tk in $(printf '%s' "$lid" | grep -oE 'L[0-9]{4}'); do
      [ "$(printf '%s' "$tk" | tr 'a-z' 'A-Z')" = "$CFG_NAME" ] && continue
      xy_of "$tk" || continue
      PEER_X[$e]=$X_OUT; PEER_Y[$e]=$Y_OUT
    done
  done <<EOF
$CFG_LINKS
EOF
  X_OUT=$sx; Y_OUT=$sy
  if [ -n "$MY_X" ] && [ -n "$CFG_SELF" ] && [ "$CFG_SELF" != "$NAME" ]; then
    LINKMAP=fallback-stale; PEER_X=(); PEER_Y=()
    [ $LM_LOG = 1 ] && dbg "STEP0 $CFGSH 自称 $CFG_SELF 与本节点 $NAME 冲突 -> 弃用其链路表"; LM_LOG=0
    return 0
  fi
  LINKMAP=cfg
  [ $LM_LOG = 1 ] && { dbg "STEP0 linkmap=cfg self=${CFG_NAME:-?} 逐口对端=$(for e in $(printf '%s\n' "$CFG_LINKS" | awk '{print $1}'); do printf '%s(%s,%s) ' $e ${PEER_X[$e]:-?} ${PEER_Y[$e]:-?}; done)"; LM_LOG=0; }
}

# 链路两端地址由两端坐标唯一确定: 行向 fc00:9000、列向 fc00:1000, 靠 1 的一端用 ::1。
# 只有真四邻格才配址 —— 两个坐标都不同说明 linkid 说的不是直连链路, 硬推的 /64
# 会和别的口撞段(现场"地址看着都对但直连不通"的成因), 宁可不配。
link_addr_pair() {
  local px=$1 py=$2 sx sy k
  if [ "$py" -eq "$MY_Y" ] && [ "$px" -ne "$MY_X" ]; then
    [ $px -eq $(( (MY_X-2+6)%6+1 )) ] || [ $px -eq $(( MY_X%6+1 )) ] || return 1
    if [ $px -eq $(( MY_X % 6 + 1 )) ]; then k="${X_HEX}:${Y_HEX}"; sx="::1"
    else k="$(hexn $px):${Y_HEX}"; sx="::2"; fi
  elif [ "$px" -eq "$MY_X" ] && [ "$py" -ne "$MY_Y" ]; then
    [ $py -eq $(( (MY_Y-2+11)%11+1 )) ] || [ $py -eq $(( MY_Y%11+1 )) ] || return 1
    if [ $py -eq $(( MY_Y % 11 + 1 )) ]; then k="${X_HEX}:${Y_HEX}"; sx="::1"
    else k="${X_HEX}:$(hexn $py)"; sx="::2"; fi
  else return 1; fi
  case "$sx" in ::1) sy="::2" ;; *) sy="::1" ;; esac
  if [ "$py" -eq "$MY_Y" ]; then printf 'fc00:9000:%s%s fc00:9000:%s%s' "$k" "$sx" "$k" "$sy"
  else printf 'fc00:1000:%s%s fc00:1000:%s%s' "$k" "$sx" "$k" "$sy"; fi
}

# 兜底口表: 与平台建网脚本一致的 eth 方向(eth1=列+1 eth2=列-1 eth3=行-1 eth4=行+1)
fallback_addr() {
  local if=$1 r c
  case "$if" in
    eth1) echo "fc00:1000:${X_HEX}:${Y_HEX}::1" ;;
    eth2) c=$(( (MY_Y-2+11)%11+1 )); echo "fc00:1000:${X_HEX}:$(hexn $c)::2" ;;
    eth3) r=$(( (MY_X-2+6)%6+1 ));   echo "fc00:9000:$(hexn $r):${Y_HEX}::2" ;;
    eth4) echo "fc00:9000:${X_HEX}:${Y_HEX}::1" ;;
    *) return 1 ;;
  esac
}

expect_addr() {
  local if=$1 px py w
  case "$LINKMAP" in
    cfg)
      px=${PEER_X[$if]:-}; py=${PEER_Y[$if]:-}
      [ -n "$px" ] || return 1
      w=$(link_addr_pair $px $py | awk '{print $1}')
      [ -n "$w" ] || return 1
      printf '%s' "$w" ;;
    *) fallback_addr "$if" ;;
  esac
}
peer_ip() { local p; p=$(expect_addr "$1") || return 1
  case "$p" in
    *::1) printf '%s' "${p%::1}::2" ;;
    *::2) printf '%s' "${p%::2}::1" ;;
    *) return 1 ;;
  esac; }
has_carrier() { ip -br link show dev "$1" 2>/dev/null | grep -q 'LOWER_UP'; }
if_addrs()    { ip -o -6 addr show dev "$1" 2>/dev/null | awk '$4 ~ /^fc00/{printf "%s ", $4}'; }

# 业务口 = 接线表里点名且有对端的口; 兜底时用固定口表。备用口(无 linkid)天然不在这里。
data_ifaces() {
  local i out="" src=""
  if [ "$LINKMAP" = cfg ]; then
    src=$(printf '%s\n' "$CFG_LINKS" | awk '{print $1}')
  else
    src=${IFACES:-$FALLBACK_IFACES}
  fi
  [ -n "$IFACES" ] && src=$IFACES
  for i in $src; do
    ip -o link show dev "$i" >/dev/null 2>&1 || continue
    case " $EXCLUDE " in *" $i "*) continue ;; esac
    out="$out $i"
  done
  printf '%s\n' $out
}

resolve_identity() {
  local raw
  # hostname 先于接线表: 整镜像复制/dock 复用会让容器里的 /cfg.sh 还是上一个节点的名字,
  # 这时以 --hostname 为准并弃用那张陈旧的表(见 linkmap_init 的 fallback-stale)
  if [ -r "$IDFILE" ]; then raw=$(head -1 "$IDFILE" 2>/dev/null); [ -n "$raw" ] && xy_of "$raw" loose && { ID_SRC="$IDFILE"; derive_addr; ID_CONFLICT=""; linkmap_init; return 0; }; fi
  if [ -n "${NODE_ID:-}" ] && xy_of "$NODE_ID" loose; then ID_SRC="env NODE_ID"; derive_addr; ID_CONFLICT=""; linkmap_init; return 0; fi
  if xy_of "$(hostname 2>/dev/null)"; then
    ID_SRC="hostname($(hostname))"; derive_addr; linkmap_init
    if [ "$LINKMAP" = fallback-stale ]; then
      ID_CONFLICT="hostname=$NAME 与 $CFGSH(自称 $CFG_SELF) 不一致 -> 已弃用其链路表, 改按兜底口表配址"
      dbg "STEP1 ID-CONFLICT $ID_CONFLICT"
    fi
    return 0
  fi
  raw=$(keyed_value "$CFGSH" 2>/dev/null)
  if [ -n "$raw" ] && xy_of "$raw"; then ID_SRC="$CFGSH name='$raw'"; derive_addr; linkmap_init; return 0; fi
  linkmap_init
  if [ -n "$SELF_FROM_LINKS" ] && xy_of "$SELF_FROM_LINKS"; then ID_SRC="$CFGSH 链路表($SELF_FROM_LINKS)"; derive_addr; linkmap_init; return 0; fi
  if [ -n "${NODE_NAME:-}" ] && xy_of "$NODE_NAME"; then ID_SRC="env NODE_NAME"; derive_addr; return 0; fi
  if [ -r "$IDLOCK" ]; then raw=$(head -1 "$IDLOCK" 2>/dev/null); [ -n "$raw" ] && xy_of "$raw" loose && { ID_SRC="$IDLOCK(cached, 兜底)"; derive_addr; linkmap_init; return 0; }; fi
  return 1
}

role_identity() { resolve_identity; }

id_banner() {
  echo "[lite] 身份无法确定 -> 不下发任何接口或路由配置"
  echo "[lite]   已尝试: $IDFILE, \$NODE_ID, hostname($(hostname 2>/dev/null)), $CFGSH, \$NODE_NAME, $IDLOCK"
  [ -n "$XY_OOR" ] && echo "[lite]   被拒候选 '$XY_OOR': 本版本只支持 6×11 网格(轨道 01-06, 序号 01-11)"
  echo "[lite]   补写本机身份即可自动接管(不用重启容器):"
  echo "[lite]     mkdir -p /etc/frr-auto && echo L0101 > /etc/frr-auto/id"
}

# =====================================================================
# FRR 拉起与主干配置(空基底镜像 /etc/frr 为空、且没有 mgmtd)
# =====================================================================
ensure_frr() {
  [ -f /etc/frr/frr.conf ] || : > /etc/frr/frr.conf
  mkdir -p /var/run/frr /var/log/frr /var/lib/frr 2>/dev/null
  chown -R frr:frr /var/run/frr /var/log/frr /var/lib/frr 2>/dev/null
  pgrep -x mgmtd >/dev/null || { [ -x /usr/lib/frr/mgmtd ] && /usr/lib/frr/mgmtd -d -A 127.0.0.1 >/dev/null 2>&1; sleep 2; }
  pgrep -x zebra >/dev/null || { /usr/lib/frr/zebra -d -f /etc/frr/frr.conf -A ::1 >/dev/null 2>&1; sleep 2; }
  pgrep -x isisd >/dev/null || { /usr/lib/frr/isisd -d -f /etc/frr/frr.conf -A ::1 >/dev/null 2>&1; sleep 2; }
}

# isisd 对幽灵状态接口滚链会崩溃: 必须先解绑 IP 再动链路
safe_reattach() {
  vtysh -c "configure terminal" -c "interface $1" -c "no ipv6 router isis SRv6" -c "end" 2>/dev/null
  ip link set $1 down 2>/dev/null; sleep 1; ip link set $1 up 2>/dev/null; sleep 1
  vtysh -c "configure terminal" -c "interface $1" -c "ipv6 router isis SRv6" -c "isis network point-to-point" -c "end" 2>/dev/null
}

enable_seg6() {
  for d in /proc/sys/net/ipv6/conf/*/seg6_enabled; do echo 1 > "$d" 2>/dev/null; done
  ip -6 route replace local ${LOCATOR_ADDR}/128 dev lo 2>/dev/null
  ip -6 route replace $LOCATOR encap seg6local action End dev lo 2>/dev/null
}

assign_link_addr() {
  local if=$1 want a
  want=$(expect_addr "$if") || return 0
  [ -n "$want" ] || return 0
  EXPECT[$if]=$want
  ip -6 addr replace ${want}/64 dev "$if" nodad 2>/dev/null
  for a in $(if_addrs "$if"); do
    case "$a" in fc00:9000:*|fc00:1000:*) ;; *) continue ;; esac
    [ "${a%/*}" = "$want" ] && continue
    ip -6 addr del "$a" dev "$if" 2>/dev/null
    dbg "ADDR $if: 清掉与接线表不符的残留地址 $a(应为 $want)"
  done
}

apply_core() {
  vtysh <<EOF
configure terminal
interface lo
ipv6 address $LOOPBACK
ipv6 router isis SRv6
exit
router isis SRv6
net $NET
is-type level-2-only
topology ipv6-unicast
exit
segment-routing
 srv6
  locators
   locator LOC
    prefix $LOCATOR block-len 32 node-len 16 func-bits 16
   exit
  exit
 exit
exit
router isis SRv6
segment-routing on
segment-routing srv6
 locator LOC
exit
exit
end
EOF
  enable_seg6
  for _if in $(data_ifaces); do assign_link_addr $_if; done
  echo "[lite] 主干配置已下发(只写内存): lo=$LOOPBACK net=$NET locator=$LOCATOR"
  dbg "STEP3 core config: $NAME lo=$LOOPBACK net=$NET locator=$LOCATOR linkmap=$LINKMAP"
}

write_state() {
  local now; now=$(date +%s)
  [ $(( now - GTICK )) -ge 10 ] || return 0
  GTICK=$now
  { echo "state=run"
    echo "identity=$NAME src=$ID_SRC linkmap=$LINKMAP"
    echo "isis_net=$NET lo=${LOOPBACK%/*} locator=$LOCATOR"
    echo "data_links=$(data_ifaces | tr '\n' ' ')"
    echo "isis_neighbors=$(vtysh -c 'show isis neighbor' 2>/dev/null | awk '/Up/' | wc -l)"
    echo "updated=$(date -Iseconds)"
  } > $STATEF 2>/dev/null
  return 0
}
GTICK=0

# =====================================================================
# LSDB 导出(邻居表变化秒级触发 + LSP hash 轮询兜底)
# =====================================================================
lsp_hash() { vtysh -c 'show isis database' 2>/dev/null | grep -E '^[A-Z]' | md5sum | cut -d' ' -f1; }

# 队列采集器在位就取一次瞬时快照, 压成一行附到 /lsdb.json 末尾
# 超时要跟着采样周期收紧: 采样 1 秒时若还允许 5 秒, 一次慢采集就会把后面的 tick 全堵死
queue_tail() {
  local q to
  [ -f "$QSH" ] || return 0
  to=$(( $(qpoll_now) + 2 ))
  [ "$to" -gt 8 ] && to=8
  [ "$to" -lt 3 ] && to=3
  q=$(timeout "$to" bash "$QSH" --once 2>/dev/null | tr -d '\n')
  [ -n "$q" ] || return 0
  printf '  "queues": %s' "$q"
}

# 当前采样周期: 运行中改文件即可, 不必重启角色; 文件缺失或写了非数字都退回启动时的 QP
qpoll_now() {
  local v
  v=$(head -1 "$QPFILE" 2>/dev/null | tr -dc '0-9')
  [ -n "$v" ] || v=$QP
  printf '%s' "$v"
}

# 本体 = self/loopback/locator/neighbors/link_addr_pair/lsdb, 末尾不带右花括号。
# 只在拓扑触发时重建; `show isis database detail json` 是这里最贵的一步。
build_body() {
  local neigh first=1 n ifc peer local_a remote_a sz0 sz1
  {
    echo '{'
    echo "  \"self\": \"$NAME\","
    echo "  \"loopback\": \"$LOOPBACK\","
    echo "  \"locator\": \"$LOCATOR\","
    echo '  "neighbors": ['
    neigh=$(vtysh -c 'show isis neighbor' 2>/dev/null | awk '/Up/{print $1, $2}')
    while read -r n ifc; do
      [ -z "$n" ] && continue
      [ $first -eq 0 ] && echo ','
      printf '    {"name":"%s","interface":"%s"}' "$n" "$ifc"
      first=0
    done <<< "$neigh"
    echo ''
    echo '  ],'
    # 有向链路的直连地址: 集成方按 src->dst 取源端发送口与两端 /64, 不必再猜 eth 方向
    echo '  "link_addr_pair": {'
    first=1
    for ifc in $(data_ifaces); do
      [ -n "${PEER_X[$ifc]:-}" ] || continue
      peer=$(printf 'L%02d%02d' ${PEER_X[$ifc]} ${PEER_Y[$ifc]})
      local_a=$(expect_addr "$ifc"); remote_a=$(peer_ip "$ifc")
      [ -n "$local_a" ] && [ -n "$remote_a" ] || continue
      [ $first -eq 0 ] && echo ','
      printf '    "%s->%s": {"iface":"%s","local":"%s","remote":"%s"}' \
        "$NAME" "$peer" "$ifc" "$local_a" "$remote_a"
      first=0
    done
    echo ''
    echo '  },'
    echo '  "lsdb": '
    sz0=$(wc -c < "${BODYF}.tmp" 2>/dev/null)
    vtysh -c 'show isis database detail json' 2>/dev/null | sed 's/^/  /'
    sz1=$(wc -c < "${BODYF}.tmp" 2>/dev/null)
    # 空 lsdb 不能算成功本体, 否则后面每轮都拼出一份废 JSON
    [ "$sz0" != "$sz1" ] || return 1
  } > "${BODYF}.tmp" 2>/dev/null
}

export_lsdb() {
  local reason=$1 qt
  [ -n "$NAME" ] || return 0
  if [ "$reason" != "queue-tick" ] || [ ! -s "$BODYF" ]; then
    build_body && mv -f "${BODYF}.tmp" "$BODYF" 2>/dev/null
  fi
  [ -s "$BODYF" ] || { dbg "LSDB export skipped (no body, $reason)"; return 0; }
  qt=$(queue_tail)
  {
    cat "$BODYF"
    printf ',\n  "updated_at": "%s",\n  "trigger": "%s"' "$(date -Iseconds)" "$reason"
    if [ -n "$qt" ]; then
      # 把当前采样周期也写进数据里, 读侧不必猜我们多久刷一次; queues 仍保持最后一项
      printf ',\n  "queue_poll_s": %s' "$(qpoll_now)"
      printf ',\n%s' "$qt"
    fi
    echo ''
    echo '}'
  } > "${LSDBF}.tmp" 2>/dev/null
  # 原子替换: 读侧(课题三/四)永远只会看到完整文件, 不会读到写了一半的 JSON
  mv -f "${LSDBF}.tmp" "$LSDBF" 2>/dev/null
  dbg "LSDB exported ($reason, $(wc -c < $LSDBF 2>/dev/null) B)"
}

run_lsdb() {
  local last_lsp last_neigh cur_lsp cur_neigh acc=0 qts=0 qp wait
  last_lsp=$(lsp_hash)
  last_neigh=$(vtysh -c 'show isis neighbor' 2>/dev/null | awk '/Up/{print $1,$2}' | md5sum)
  dbg "lsdb role started (name=$NAME poll=${LP}s queue=$([ -f "$QSH" ] && echo "${QP}s" || echo off))"
  while true; do
    qp=$(qpoll_now)
    # 采集器在位时把睡眠切到"离下一次队列 tick 还差多久", 上限仍是 3 秒一档(邻居核验节奏不变);
    # 采集器不在位则完全退回原来的 3 秒定轮。
    wait=3
    if [ -f "$QSH" ]; then
      wait=$(( qp - (SECONDS - qts) ))
      [ "$wait" -gt 3 ] && wait=3
      [ "$wait" -lt 1 ] && wait=1
    fi
    sleep "$wait"
    acc=$(( acc + wait ))
    cur_neigh=$(vtysh -c 'show isis neighbor' 2>/dev/null | awk '/Up/{print $1,$2}' | md5sum)
    if [ "$cur_neigh" != "$last_neigh" ]; then
      sleep 2
      export_lsdb link-event
      last_neigh=$cur_neigh
      last_lsp=$(lsp_hash)
      qts=$SECONDS
      acc=0
      continue
    fi
    # 队列是瞬时值: 采集器在位时按 QP 定频重写尾部(复用拓扑本体, 不重采 620 KB 的 vtysh JSON)
    if [ -f "$QSH" ] && [ $(( SECONDS - qts )) -ge "$qp" ]; then
      qts=$SECONDS
      export_lsdb queue-tick
      # 注意: 这里不清 acc —— 队列 tick 比 LP 密的时候, 一清就再把 LSP 兜底推后,
      # AUTO_QPOLL=7 < LP=30 时那个 30 秒兜底会永远不触发(2026-10-26 实测到并修掉)
      continue
    fi
    if [ "$acc" -ge "$LP" ]; then
      acc=0
      cur_lsp=$(lsp_hash)
      if [ -n "$cur_lsp" ] && [ "$cur_lsp" != "$last_lsp" ]; then
        export_lsdb poll
        last_lsp=$cur_lsp
      fi
    fi
  done
}

# =====================================================================
# SRv6 路径下发: 扫 /path.cmd/*.json, 同 (src,dst) 取 priority 最高
# =====================================================================
declare -A FILE_HASHES ROUTE_SIGS ROUTE_SIG_TIME ACTIVE_ROUTES BIZ_STATUS

parse_one() {
  local f=$1 biz src dst mode pri via
  [ -r "$f" ] || return 1
  biz=$(grep -oE '"biz_id"[[:space:]]*:[[:space:]]*"[^"]+"' "$f" | head -1 | sed 's/.*:[[:space:]]*"\([^"]*\)".*/\1/')
  src=$(grep -oE '"src"[[:space:]]*:[[:space:]]*"[^"]+"' "$f" | head -1 | sed 's/.*:[[:space:]]*"\([^"]*\)".*/\1/')
  dst=$(grep -oE '"dst"[[:space:]]*:[[:space:]]*"[^"]+"' "$f" | head -1 | sed 's/.*:[[:space:]]*"\([^"]*\)".*/\1/')
  mode=$(grep -oE '"mode"[[:space:]]*:[[:space:]]*"[^"]+"' "$f" | head -1 | sed 's/.*:[[:space:]]*"\([^"]*\)".*/\1/')
  pri=$(grep -oE '"priority"[[:space:]]*:[[:space:]]*[0-9]+' "$f" | head -1 | sed 's/.*:[[:space:]]*//')
  via=$(grep -oE '"via"[[:space:]]*:[[:space:]]*\[[^]]+\]' "$f" | head -1 | sed 's/.*\[//; s/\].*//; s/"//g; s/,/ /g; s/^[[:space:]]*//; s/[[:space:]]*$//')
  mode=${mode:-end}; pri=${pri:-1}
  [ -n "$biz" ] && [ -n "$src" ] && [ -n "$dst" ] && [ -n "$via" ] || return 1
  echo "$biz $src $dst $mode $pri $via"
}

get_iface_to() {
  local target=$1 t ifc net
  t=$(printf '%s' "$target" | tr 'A-Z' 'a-z')
  ifc=$(vtysh -c 'show isis neighbor' 2>/dev/null | awk -v t="$t" 'tolower($1)==t {print $2}' | head -1 | sed 's/@.*//')
  [ -n "$ifc" ] && { echo "$ifc"; return 0; }
  net=$(net_of "$target") || return 1
  ifc=$(vtysh -c 'show isis neighbor detail' 2>/dev/null | grep -B4 -F "$net" | awk '/Interface:/ {gsub(/@.*/,"",$2); print $2}' | head -1)
  [ -n "$ifc" ] && { echo "$ifc"; return 0; }
  return 1
}

write_status() {
  local tmp=${PSTAT}.tmp biz first=1
  echo '{' > "$tmp"
  for biz in "${!BIZ_STATUS[@]}"; do
    [ $first -eq 0 ] && echo ',' >> "$tmp"
    printf '  "%s": %s' "$biz" "${BIZ_STATUS[$biz]}" >> "$tmp"
    first=0
  done
  echo '' >> "$tmp"; echo '}' >> "$tmp"
  mv "$tmp" "$PSTAT" 2>/dev/null
}

apply_path() {
  local biz=$1 src=$2 dst=$3 mode=$4
  shift 4
  local hops=("$@") dst_lo dst_sid sids="" first_next="" node sid ifc dev segs rkind
  dst_lo=$(lo_of "$dst"); dst_sid=$(sid_of "$dst")
  if [ -z "$dst_lo" ] || [ -z "$dst_sid" ]; then
    dbg "path $biz: ERROR 无法解析终点 '$dst'"
    BIZ_STATUS[$biz]='{"status":"failed","error":"cannot resolve dst"}'; return 1
  fi
  for node in "${hops[@]}"; do
    [ "$node" = "$src" ] && continue
    sid=$(sid_of "$node")
    if [ -z "$sid" ]; then
      dbg "path $biz: ERROR 无法解析途经点 '$node'"
      BIZ_STATUS[$biz]='{"status":"failed","error":"cannot resolve hop '"$node"'"}'; return 1
    fi
    [ -z "$first_next" ] && first_next=$node
    sids="${sids}${sids:+ }$sid"
  done
  case " $sids " in *"$dst_sid"*) ;; *) sids="${sids}${sids:+ }$dst_sid" ;; esac

  dev=""
  if [ -n "$first_next" ]; then
    ifc=$(get_iface_to "$first_next")
    [ -z "$ifc" ] && ifc=$(ip -6 route get "$(sid_of "$first_next")" 2>/dev/null | grep -oE 'dev [a-z0-9@]+' | awk '{print $2}' | head -1 | sed 's/@.*//')
    if [ -z "$ifc" ] && [ "$mode" = "endx" ]; then
      dbg "path $biz: ERROR 严格模式需要通往 $first_next 的接口"
      BIZ_STATUS[$biz]='{"status":"failed","error":"no interface to '"$first_next"'"}'; return 1
    fi
    dev=${ifc:-}
  fi
  if [ -z "$dev" ]; then
    dev=$(ip -6 route get "$dst_lo" 2>/dev/null | grep -oE 'dev [a-z0-9@]+' | awk '{print $2}' | head -1 | sed 's/@.*//')
  fi
  if [ -z "$dev" ] || [ "$dev" = "lo" ]; then
    dbg "path $biz: ERROR 无通往 ${first_next:-$dst} 的物理出接口(dev=$dev) -> 拒发, 避免 lo 黑洞"
    BIZ_STATUS[$biz]='{"status":"failed","error":"no egress dev"}'; return 1
  fi

  segs=$(echo "$sids" | tr ' ' ',')
  dbg "path $biz: install ${dst_lo}/128 segs=$segs dev=$dev metric=10"
  if ip -6 route replace ${dst_lo}/128 encap seg6 mode encap segs $segs metric 10 dev $dev 2>>$DBGF; then
    ACTIVE_ROUTES[${dst_lo}/128]=$biz
    rkind=$(ip -6 route get "$dst_lo" 2>/dev/null | grep -c "encap seg6")
    [ "$rkind" -gt 0 ] && rkind=seg6 || rkind=isis
    if $PING6 -c 2 -W 1 "$dst_lo" >/dev/null 2>&1; then
      BIZ_STATUS[$biz]=$(printf '{"status":"ok","installed_at":"%s","ping":"pass","route":"%s","dev":"%s"}' "$(date -Iseconds)" "$rkind" "$dev")
    else
      BIZ_STATUS[$biz]=$(printf '{"status":"ok","installed_at":"%s","ping":"fail","route":"%s","dev":"%s"}' "$(date -Iseconds)" "$rkind" "$dev")
    fi
  else
    dbg "path $biz: ERROR ip route 下发失败"
    BIZ_STATUS[$biz]='{"status":"failed","error":"route command failed"}'
  fi
}

remove_route() {
  ip -6 route del "$1" metric 10 2>/dev/null || ip -6 route del "$1" 2>/dev/null
  dbg "path $2: 策略已撤, 删除 SRv6 路由 $1"
  BIZ_STATUS[$2]=$(printf '{"status":"removed","removed_at":"%s"}' "$(date -Iseconds)")
  unset ACTIVE_ROUTES["$1"]
}

tuner_tick() {
  local f fname cur parsed biz src dst mode pri via key old_pri val rest sig_hash now dst_key route_biz found k2
  declare -A SEEN CAND
  for f in "$PCDIR"/*.json; do
    [ -f "$f" ] || continue
    fname=$(basename "$f"); SEEN[$fname]=1
    cur=$(md5sum "$f" 2>/dev/null | awk '{print $1}'); [ -n "$cur" ] || continue
    parsed=$(parse_one "$f") || { dbg "path $fname: 字段不全, 跳过"; continue; }
    read -r biz src dst mode pri via <<< "$parsed"
    key="${src},${dst}"
    if [ -z "${CAND[$key]}" ]; then
      CAND[$key]="${pri}|${biz}|${mode}|${via}"; FILE_HASHES[$fname]=$cur
    else
      old_pri=${CAND[$key]%%|*}
      [ "$pri" -gt "$old_pri" ] && { CAND[$key]="${pri}|${biz}|${mode}|${via}"; FILE_HASHES[$fname]=$cur; }
    fi
  done
  for key in "${!CAND[@]}"; do
    val=${CAND[$key]}
    pri=${val%%|*}; rest=${val#*|}
    biz=${rest%%|*}; rest=${rest#*|}
    mode=${rest%%|*}; via=${rest#*|}
    sig_hash=$(echo -n "${key},${via}" | md5sum | awk '{print $1}')
    now=$(date +%s)
    if [ "${ROUTE_SIGS[$key]}" = "$sig_hash" ] && [ $(( now - ${ROUTE_SIG_TIME[$key]:-0} )) -lt $COOL ]; then continue; fi
    apply_path "$biz" "${key%%,*}" "${key##*,}" "$mode" $via
    ROUTE_SIGS[$key]=$sig_hash; ROUTE_SIG_TIME[$key]=$now
  done
  for dst_key in "${!ACTIVE_ROUTES[@]}"; do
    route_biz=${ACTIVE_ROUTES[$dst_key]}; found=0
    for k2 in "${!CAND[@]}"; do
      rest=${CAND[$k2]}; rest=${rest#*|}
      biz=${rest%%|*}
      [ "$biz" = "$route_biz" ] && { found=1; break; }
    done
    [ $found -eq 0 ] && remove_route "$dst_key" "$route_biz"
  done
  for fname in "${!FILE_HASHES[@]}"; do [ -z "${SEEN[$fname]}" ] && unset FILE_HASHES["$fname"]; done
  write_status
}

run_tuner() {
  mkdir -p "$PCDIR" 2>/dev/null
  dbg "tuner role started, watching $PCDIR/"
  while true; do
    tuner_tick
    sleep $PC
  done
}

# =====================================================================
# 逐数据口取证: route get 出现 via fe80:: = 本机全局地址不在了或两端不同 /64
# =====================================================================
link_audit() {
  local if want pip cur rt dr q
  for if in $(data_ifaces); do
    want=$(expect_addr "$if"); pip=$(peer_ip "$if")
    cur=$(if_addrs "$if")
    if [ -z "$want" ]; then
      printf '  %s 不配址(接线表对端(%s,%s)不在本节点四邻格内) 实际=[%s]\n' \
        "$if" "${PEER_X[$if]:-?}" "${PEER_Y[$if]:-?}" "${cur:-无}"
      continue
    fi
    rt=$(ip -6 route get "$pip" 2>/dev/null | head -1)
    q=$(tc -s qdisc show dev "$if" 2>/dev/null | head -1)
    dr=$(printf '%s' "$q" | grep -oE 'backlog [0-9]+ [0-9]+b' | head -1)
    case "$rt" in
      *'via '*|'') v="异常: 需要下一跳或不可达(本机地址丢失/两端不同 /64)" ;;
      *) v="正常: 本口直连" ;;
    esac
    printf '  %s 期望=%s 实际=[%s] 对端=%s carrier=%s %s\n' \
      "$if" "${want:-无映射}" "${cur:-无}" "${pip:-?}" "$(has_carrier $if && echo 在 || echo 无)" "${dr:-}"
    printf '     选路=%s -> %s\n' "${rt:-不可达}" "$v"
  done
  return 0
}

verify_once() {
  sleep 60
  local n lo
  dbg "STEP6 verification start"
  {
    echo "==== [verify] $(date '+%F %T') $NAME ===="
    ip -6 addr show dev lo 2>/dev/null | grep fc00
    vtysh -c 'show isis neighbor' 2>/dev/null
    vtysh -c 'show segment-routing srv6 locator' 2>/dev/null
    link_audit
    for n in $(vtysh -c 'show isis neighbor' 2>/dev/null | awk '/Up/{print $1}' | sort -u); do
      lo=$(lo_of "$n") || { echo "$n: 名称无法解析环回"; continue; }
      echo -n "ping $n ($lo): "
      $PING6 -c 2 -W 1 "$lo" 2>&1 | tail -1
    done
  } >> $DBGF 2>&1
  dbg "STEP6 verification done"
}

# =====================================================================
# 主守护: 定身份 -> 拉进程 -> 配主干 -> 起子角色 -> watcher 循环
# =====================================================================
run_daemon() {
  mkdir -p /var/log/frr /var/run/frr 2>/dev/null
  exec 200>/var/run/.auto66.lock
  if ! flock -n 200; then
    for lt in $(seq 1 10); do sleep 3; flock -n 200 && break; done
    flock -n 200 || echo "[lite] WARN: 锁被残留进程持有, 仍然接管(逻辑幂等)"
  fi
  echo "==== [lite] daemon start $(date '+%F %T') host=$(hostname) ===="

  local BN=0
  while ! resolve_identity; do
    if [ $BN -eq 0 ] || [ $(( BN % 12 )) -eq 0 ]; then
      id_banner
      id_banner > /proc/1/fd/1 2>/dev/null || true
    fi
    BN=$((BN+1)); sleep 5
  done
  mkdir -p "$(dirname $IDLOCK)" 2>/dev/null && echo "$NAME" > "$IDLOCK" 2>/dev/null
  echo "[lite] 本机 $NAME 来自 $ID_SRC (规则 $XY_RULE) -> lo=$LOOPBACK net=$NET locator=$LOCATOR 链路映射=$LINKMAP"
  dbg "STEP1 identity: $NAME src=$ID_SRC rule=$XY_RULE lo=$LOOPBACK net=$NET locator=$LOCATOR linkmap=$LINKMAP"

  ensure_frr
  sysctl -w net.ipv6.conf.all.forwarding=1 >/dev/null 2>&1
  sysctl -w net.ipv6.conf.all.keep_addr_on_down=1 >/dev/null 2>&1
  local READY=0 i
  for i in $(seq 1 60); do
    pgrep -x zebra >/dev/null && pgrep -x isisd >/dev/null && { READY=1; break; }
    ensure_frr; sleep 1
  done
  [ $READY -eq 1 ] || echo "[lite] WARN: 60s 后仍未见 zebra/isisd, 继续尝试"

  if ip -6 addr show dev lo | grep -q "fc00:0:${X_HEX}::${Y_HEX}" \
     && vtysh -c "show isis interface detail" 2>/dev/null | grep -q "Interface: lo"; then
    echo "[lite] 主干配置已存在, 跳过"
  else
    apply_core
  fi
  dbg "STEP4 watcher started (H=$H D=$D C=$C N=$N G=$B) excluded=[$EXCLUDE]"

  verify_once &
  export_lsdb boot

  local PREV="" WAIT CN D_ PID ET
  declare -A CRASHN GHOSTSEEN BOUNCEAT BOUNCECNT ADDAT
  while true; do
    for D_ in mgmtd zebra isisd; do
      PID=$(pgrep -x $D_ | head -1)
      if [ -n "$PID" ]; then
        ET=$(ps -o etimes= -p "$PID" 2>/dev/null | tr -d ' ')
        [ "${ET:-0}" -gt "$G" ] && CRASHN[$D_]=0
        continue
      fi
      CRASHN[$D_]=$(( ${CRASHN[$D_]:-0} + 1 )); CN=${CRASHN[$D_]}
      if [ $CN -le 1 ]; then WAIT=3; elif [ $CN -le 3 ]; then WAIT=$((30 * CN)); else WAIT=300; fi
      echo "[lite] 守护进程 $D_ 不在(第 $CN 次) -> 暂停 ${WAIT}s 后重启"
      sleep $WAIT
      pgrep -x $D_ >/dev/null && continue
      case $D_ in
        mgmtd) /usr/lib/frr/mgmtd -d -A 127.0.0.1 ;;
        zebra) /usr/lib/frr/zebra -d -f /etc/frr/frr.conf -A ::1 ;;
        isisd) /usr/lib/frr/isisd -d -f /etc/frr/frr.conf -A ::1 ;;
      esac
    done
    pgrep -f "auto66-lite.sh --tuner" >/dev/null || { bash "$SELF" --tuner >> $LOGF 2>&1 & }
    pgrep -f "auto66-lite.sh --lsdb"  >/dev/null || { bash "$SELF" --lsdb  >> $LOGF 2>&1 & }

    # 每轮重读接线表: 平台改了 /cfg.sh 不需要重启容器或重启自动化
    linkmap_init
    ALL=$(data_ifaces)
    write_state
    if [ -z "$ALL" ]; then
      echo "[lite] 内核里还没有本节点的数据口(平台尚未接线), $H 秒后重试"
      PREV=""; sleep $H; continue
    fi

    for IF in $ALL; do
      ip link set $IF up 2>/dev/null
      assign_link_addr $IF
    done
    IFSCOPE=" $(printf '%s\n' $ALL eth1 eth2 eth3 eth4 | sort -u | tr '\n' ' ')"
    RUNCFG=$(vtysh -c "show running-config" 2>/dev/null)
    for SEC in $(echo "$RUNCFG" | awk '/^interface /{print $2}'); do
      [ "$SEC" = "lo" ] && continue
      echo "$ALL" | grep -qx -- "$SEC" && continue
      case "$IFSCOPE" in *" $SEC "*) ;; *) continue ;; esac
      echo "[lite] 清除内核已不存在的接口配置段 [$SEC]"
      vtysh -c "configure terminal" -c "no interface $SEC" -c "end" 2>/dev/null
    done
    if pgrep -x isisd >/dev/null; then
      DET=$(vtysh -c "show isis interface detail" 2>/dev/null)
      if ! echo "$DET" | grep -q "Interface: lo"; then
        echo "[lite] isisd 配置丢失(进程重启过?) -> 重放主干配置"
        apply_core; DET=$(vtysh -c "show isis interface detail" 2>/dev/null)
      fi
      UP=$(echo "$DET" | awk '/Interface: /{gsub(/,/,""); if ($4=="Up") print $2}')
      KNOWN=$(echo "$DET" | awk '/Interface: /{gsub(/,/,""); print $2}')
      NOW=$(date +%s)
      for IF in $ALL; do
        has_carrier $IF || continue
        echo " $PREV " | grep -q " $IF " || continue
        if echo "$UP" | grep -qx -- "$IF"; then
          unset "GHOSTSEEN[$IF]"; unset "BOUNCEAT[$IF]"; unset "BOUNCENT[$IF]"; unset "ADDAT[$IF]"
          continue
        fi
        if echo "$KNOWN" | grep -qx -- "$IF"; then
          [ -z "${GHOSTSEEN[$IF]:-}" ] && GHOSTSEEN[$IF]=$NOW && continue
          [ $(( NOW - GHOSTSEEN[$IF] )) -lt "$D" ] && continue
          BC=${BOUNCECNT[$IF]:-0}
          if [ -n "${BOUNCEAT[$IF]:-}" ]; then
            if [ $BC -ge 3 ] && [ $(( NOW - BOUNCEAT[$IF] )) -lt "$N" ]; then continue; fi
            if [ $BC -lt 3 ] && [ $(( NOW - BOUNCEAT[$IF] )) -lt "$C" ]; then continue; fi
          fi
          [ $BC -ge 3 ] && BOUNCECNT[$IF]=0
          BOUNCEAT[$IF]=$NOW; BOUNCECNT[$IF]=$(( BC + 1 ))
          echo "[lite] $IF 幽灵段(未 Up) -> 安全重挂 #${BOUNCECNT[$IF]}"
          safe_reattach $IF
        else
          if [ -z "${ADDAT[$IF]:-}" ]; then
            echo "[lite] 接管 $IF (plain add)"
            dbg "STEP5 attaching $IF (plain add)"
            vtysh -c "configure terminal" -c "interface $IF" -c "ipv6 router isis SRv6" -c "isis network point-to-point" -c "end" 2>/dev/null
            ADDAT[$IF]=$NOW
          elif [ $(( NOW - ADDAT[$IF] )) -ge "$D" ]; then
            BC=${BOUNCECNT[$IF]:-0}
            if [ $BC -ge 3 ]; then
              echo "[lite] $IF 挂过仍看不见(zebra 丢了 IF_NEW) -> 整栈重启"
              pkill -x isisd 2>/dev/null; pkill -x zebra 2>/dev/null
              CRASHN[isisd]=0; CRASHN[zebra]=0; BOUNCECNT[$IF]=0; ADDAT[$IF]=$(( NOW + B ))
              sleep 3
            else
              echo "[lite] $IF 已 add 但 isisd 看不见 -> 滚链唤醒 #$((BC+1))"
              safe_reattach $IF; ADDAT[$IF]=$NOW; BOUNCECNT[$IF]=$(( BC + 1 ))
            fi
          fi
        fi
        enable_seg6
      done
    fi
    PREV=$(echo $ALL)
    sleep $H
  done
}

# =====================================================================
# 队列装配子命令: 只动 tc, 不碰 FRR/路由/地址。默认 dry-run, 带 --yes 才落地。
# 给集成方省事用 —— 装完 tbf 后 /lsdb.json 尾部那个口就出实值, 不必自己手写 tc。
# =====================================================================
q_expand() {   # 参数: 接口名或 all -> 输出实际可操作的接口
  # 诊断一律走 stderr: 调用方用 $(q_expand ...) 取值, 打到 stdout 会被吃掉
  local t out="" d
  for t in "$@"; do
    if [ "$t" = all ]; then
      for d in $(data_ifaces); do out="$out $d"; done
    elif [ "$t" = lo ]; then
      echo "  跳过 lo(队列不装在环回上)" >&2
    elif ip -o link show dev "$t" >/dev/null 2>&1; then
      out="$out $t"
    else
      echo "FATAL: 看不到接口 $t" >&2; return 1
    fi
  done
  [ -n "$out" ] || { echo "没有可操作的口(查 $CFGSH 是否在位、口是否 Up)" >&2; return 1; }
  printf '%s\n' $out
}

cmd_qshow() {
  have tc || { say "FATAL: 容器内没有 tc"; return 1; }
  local ifs d
  ifs=$(q_expand "${@:-all}") || return 1
  for d in $ifs; do
    echo "== $d  对端linkid=$(printf '%s\n' "$CFG_LINKS" | awk -v e="$d" '$1==e{print $2}')  应有本机地址=$(expect_addr "$d" 2>/dev/null | awk '{print $1}')"
    tc -s qdisc show dev "$d" 2>/dev/null | sed 's/^/    /'
  done
  say "读队列值不用这条命令: /lsdb.json 尾部 queues 每 ${QP}s 刷一次, 或 bash $QSH --once"
}

cmd_qset() {
  have tc || { say "FATAL: 容器内没有 tc"; return 1; }
  local a target="" rate=16mbit burst=32kbit lat=100ms delay="" limit=1000 yes="" ifs d k v
  for a in "$@"; do
    case "$a" in
      --yes) yes=1 ;;
      *=*) k=${a%%=*}; v=${a#*=}
           case "$k" in
             rate) rate=$v ;; burst) burst=$v ;; lat|latency) lat=$v ;;
             delay) delay=$v ;; limit) limit=$v ;;
             *) say "  忽略未知参数 $a (可用: rate burst lat delay limit)" ;;
           esac ;;
      *) [ -z "$target" ] && target=$a || say "  忽略多余参数 $a" ;;
    esac
  done
  [ -n "$target" ] || { say "用法: bash $SELF qset <eth|all> [rate=16mbit] [burst=32kbit] [lat=100ms] [delay=10ms] [limit=1000] --yes"; return 1; }
  ifs=$(q_expand "$target") || return 1
  for d in $ifs; do
    echo "== $d  现状: $(tc -s qdisc show dev $d 2>/dev/null | head -1)"
    printf '    tc qdisc del dev %s root\n' "$d"
    printf '    tc qdisc add dev %s root handle 1: tbf rate %s burst %s latency %s\n' "$d" "$rate" "$burst" "$lat"
    [ -n "$delay" ] && printf '    tc qdisc add dev %s parent 1:1 handle 10: netem delay %s limit %s\n' "$d" "$delay" "$limit"
    if [ -z "$yes" ]; then
      say "    预览模式, 什么都没改 —— 确认无误后在行尾加 --yes"
      dbg "QSET-DRY $d rate=$rate burst=$burst lat=$lat delay=${delay:-无} limit=$limit"
      continue
    fi
    tc qdisc del dev "$d" root 2>/dev/null
    tc qdisc add dev "$d" root handle 1: tbf rate "$rate" burst "$burst" latency "$lat" 2>&1 | sed 's/^/    /'
    if [ -n "$delay" ]; then
      tc qdisc add dev "$d" parent 1:1 handle 10: netem delay "$delay" limit "$limit" 2>&1 | sed 's/^/    /'
    fi
    echo "    装后: $(tc -s qdisc show dev $d 2>/dev/null | head -2 | tr '\n' ' ')"
    say "    下一个队列 tick(≤${QP}s)起, /lsdb.json 尾部 $d 会出 queue_bytes/capacity_mbps/delay_ms 实值"
    dbg "QSET-APPLY $d rate=$rate burst=$burst lat=$lat delay=${delay:-无} limit=$limit"
  done
}

cmd_qclear() {
  have tc || { say "FATAL: 容器内没有 tc"; return 1; }
  local a target="" yes="" ifs d
  for a in "$@"; do
    case "$a" in --yes) yes=1 ;; *) [ -z "$target" ] && target=$a ;; esac
  done
  ifs=$(q_expand "${target:-all}") || return 1
  for d in $ifs; do
    printf '== %s  现状: %s\n' "$d" "$(tc -s qdisc show dev $d 2>/dev/null | head -1)"
    printf '    tc qdisc del dev %s root\n' "$d"
    if [ -z "$yes" ]; then
      say "    预览模式, 什么都没改 —— 加 --yes 才真拆"
      dbg "QCLEAR-DRY $d"
      continue
    fi
    tc qdisc del dev "$d" root 2>/dev/null
    say "    已拆回默认队列: $(tc -s qdisc show dev $d 2>/dev/null | head -1)"
    dbg "QCLEAR-APPLY $d"
  done
}

# =====================================================================
# 前台子命令
# =====================================================================
usage() {
  cat <<EOF
用法: bash $SELF [start|status|gate|report|stop|restart|stop-old]
  (无参数 = start)  后台常驻: 读 /cfg.sh -> 认身份 -> 配 IS-IS/SRv6 -> 逐口按 linkid 配址
                    -> 链路/地址自愈 -> /lsdb.json 导出 -> /path.cmd SRv6 下发
  status   状态(身份来源/链路映射/逐链路取证/LSP 查重)
  gate     预检接线表(只读): 每个业务口的对端与应有地址
  report   一键诊断包 -> /frr-report.txt
  stop     停本自动化(不动 zebra/isisd 与已下发配置)
  restart  停掉再重启
  stop-old 停掉容器里原有的 auto-config/path-tuner/lsdb-monitor/auto66
  qshow [eth|all]      只读: 打每个业务口的 tc -s qdisc 原文(默认 all)
  qset <eth|all> [rate=16mbit] [burst=32kbit] [lat=100ms] [delay=10ms] [limit=1000] [--yes]
                       给出口装 tbf(带 delay 再挂 netem 子队列)。不加 --yes 只预览
  qclear [<eth|all>] [--yes]   拆回默认队列(noqueue)。不加 --yes 只预览
  注: qset/qclear 只动 tc, 不碰 FRR/路由/地址; 每次真落地都记进 /auto-debug.log
开关: AUTO_CFGSH=$CFGSH  AUTO_IFACES=$IFACES  AUTO_EXCLUDE=$EXCLUDE
EOF
}

precheck() {
  [ "$(id -u)" = 0 ] || { say "FATAL: 需要 root(容器内 docker exec 默认即 root)"; return 1; }
  local c miss=""
  for c in vtysh ip sed awk grep md5sum flock pgrep pkill; do have "$c" || miss="$miss $c"; done
  [ -n "$miss" ] && { say "FATAL: 容器内缺命令:$miss"; return 1; }
  [ -x /usr/lib/frr/zebra ] && [ -x /usr/lib/frr/isisd ] || { say "FATAL: 看不到 FRR 二进制, 这不是 FRR 容器"; return 1; }
  return 0
}

running_daemon() { pgrep -f 'auto66-lite.sh --daemon' | head -1; }

front_start() {
  precheck || exit 1
  if [ -n "$(running_daemon)" ]; then
    say "自动化已在运行(PID $(running_daemon)), 直接看状态:"
  else
    if pgrep -f '/auto-config.sh' >/dev/null || pgrep -f 'auto66.sh --daemon' >/dev/null; then
      say "WARN: 容器里还有旧版自动化在跑, 会形成双 watcher。先执行: bash $SELF stop-old"
    fi
    mkdir -p /var/log/frr
    setsid nohup bash "$SELF" --daemon >> $LOGF 2>&1 < /dev/null &
    say "已后台拉起自动化(PID $!)"
    sleep 8
  fi
  cmd_status
  say "详细日志: $LOGF 与 $DBGF"
}

front_stop() {
  pkill -f 'auto66-lite.sh --daemon' 2>/dev/null
  pkill -f 'auto66-lite.sh --tuner'  2>/dev/null
  pkill -f 'auto66-lite.sh --lsdb'   2>/dev/null
  say "已停止自动化进程(不动 zebra/isisd, 不动已下发的配置与网络)"
}

dup_net() {
  local n
  [ -n "$MY_X" ] || { echo "身份未定, 无法判重"; return 1; }
  # 这个镜像的 isisd 按主机名形态打印 LSP ID(例 L0305.00-00), 老口径只按 NET 十六进制找会永远打不中
  n=$(vtysh -c 'show isis database' 2>/dev/null | awk -v me="$NAME" '
      $1 ~ /\.00-00$/ { id=$1; sub(/\.00-00$/, "", id); if (id == me) c++ } END { print c+0 }')
  case $n in
    1) echo "唯一($NAME.00-00)"; return 0 ;;
    0) echo "!! 库内无本机 LSP $NAME.00-00 -> 未洪泛(isisd 刚重启/配置未生效/无邻居)"; return 1 ;;
    *) echo "!! 库内 $n 条 $NAME.00-00 -> 有其它节点用了同一身份(NET 冲突), 本节点会被域内丢弃"; return 1 ;;
  esac
}

cmd_status() {
  local pid lo nn fwd
  pid=$(running_daemon)
  echo "----- auto66-lite status $(date -u '+%FT%TZ') host=$(hostname) uptime=$(cut -d' ' -f1 /proc/uptime 2>/dev/null) -----"
  if role_identity; then
    echo "  身份/编址 : $NAME (X=$MY_X Y=$MY_Y) lo=$LOOPBACK  来源=$ID_SRC"
    echo "  IS-IS NET : $NET   locator=$LOCATOR   链路映射=$LINKMAP"
    [ -n "$ID_CONFLICT" ] && echo "  身份冲突  : $ID_CONFLICT"
  else
    echo "  身份/编址 : 未确定 -> 自动化不下发任何配置(见 $LOGF)"
  fi
  echo "  主进程    : ${pid:-未运行}"
  echo "  子进程    : tuner=$(pgrep -f 'auto66-lite.sh --tuner' | head -1) lsdb=$(pgrep -f 'auto66-lite.sh --lsdb' | head -1)"
  echo "  守护进程  : zebra=$(pgrep -x zebra|head -1) isisd=$(pgrep -x isisd|head -1) mgmtd=$(pgrep -x mgmtd|head -1)"
  lo=$(ip -6 addr show dev lo 2>/dev/null | grep -oE 'fc00:0:[0-9a-f]+::[0-9a-f]+' | head -1)
  echo "  环回      : ${lo:-未配置}"
  fwd=$(sysctl -n net.ipv6.conf.all.forwarding 2>/dev/null)
  echo "  forwarding: ${fwd:-?} (应为 1)"
  nn=$(vtysh -c 'show isis neighbor' 2>/dev/null | awk '/Up/' | wc -l)
  echo "  IS-IS 邻居: $nn 个 (6×11 满编 4; 少于 4 属接线未完)"
  echo "  LSP 自检  : $(dup_net)"
  echo "  状态快照  : $(tr '\n' ' ' < $STATEF 2>/dev/null)"
  echo "  逐链路取证:"
  link_audit | sed 's/^/  /'
  echo "  lsdb.json : $([ -f $LSDBF ] && echo "$(wc -c < $LSDBF) B @ $(stat -c %y $LSDBF 2>/dev/null | cut -d. -f1)" || echo 未生成)"
  echo "  path.cmd  : $(ls $PCDIR/*.json 2>/dev/null | wc -l) 条策略, 回执 $([ -f $PSTAT ] && echo 有 || echo 无)"
  echo "  日志尾:"; tail -5 $LOGF 2>/dev/null | sed 's/^/    /'
}

cmd_gate() {
  local i miss="" w
  role_identity || { say "FATAL: 认不出本机身份(hostname=$(hostname)), 写 $IDFILE 或修 /cfg.sh"; exit 1; }
  say "本机 $NAME ($MY_X,$MY_Y), 身份来源=$ID_SRC, 链路映射=$LINKMAP"
  if [ "$LINKMAP" = cfg ]; then
    say "接线表($CFGSH) 逐口对端与应有地址:"
    for i in $(data_ifaces); do
      w=$(expect_addr $i)
      if [ -z "$w" ]; then
        printf '    %s -> 对端(%s,%s) 不在本节点四邻格内 -> 不配址\n' \
          "$i" "${PEER_X[$i]:-?}" "${PEER_Y[$i]:-?}"
      else
        printf '    %s -> 对端(%s,%s)  本机=%s  对端=%s\n' "$i" "${PEER_X[$i]}" "${PEER_Y[$i]}" "$w" "$(peer_ip $i)"
      fi
    done
    for i in $(printf '%s\n' "$CFG_LINKS" | awk '{print $1}'); do
      ip -o link show dev "$i" >/dev/null 2>&1 || miss="$miss $i"
    done
    [ -n "$miss" ] && say "  接线表点名但内核没有的口(平台尚未接线):$miss"
  else
    say "未采用接线表(原因见 $DBGF 的 STEP0 行), 用兜底口表: eth1=列+1 eth2=列-1 eth3=行-1 eth4=行+1"
    say "  兜底方向若与实际接线相反, 两端会落进不同 /64(直连 ping 不通)。现场以 /cfg.sh 为准"
  fi
  say "内核全部接口(无 linkid 的备用/下行口一律不动):"
  ip -br link show 2>/dev/null | sed 's/^/    /'
  say "当前邻居表:"
  vtysh -c 'show isis neighbor' 2>/dev/null | sed 's/^/    /'
  say "LSP 自检: $(dup_net)"
  say "逐链路取证:"
  link_audit
}

cmd_report() {
  local out=/frr-report.txt
  {
    echo "===== auto66-lite report $(date -u '+%FT%TZ') host=$(hostname) ====="
    echo "--- identity ---"; echo "cache=$IDLOCK:$(cat $IDLOCK 2>/dev/null) idfile=$(cat $IDFILE 2>/dev/null) NODE_ID=${NODE_ID:-} NODE_NAME=${NODE_NAME:-}"
    role_identity && echo "resolved=$NAME src=$ID_SRC linkmap=$LINKMAP conflict=$ID_CONFLICT" || echo "resolved=none"
    echo "--- state file ---"; cat $STATEF 2>/dev/null
    echo "--- cfg.sh ---"; cat "$CFGSH" 2>/dev/null | cut -c1-900
    echo "--- processes ---"; ps -eo pid,etime,cmd 2>/dev/null | grep -E 'zebra|isisd|mgmtd|auto66' | grep -v grep
    echo "--- sysctl ---"; for k in all.forwarding all.keep_addr_on_down all.disable_ipv6 eth0.accept_dad eth0.dad_transmits; do printf '%s=%s\n' "$k" "$(sysctl -n net.ipv6.conf.$k 2>/dev/null)"; done
    echo "--- lo ---"; ip -6 addr show dev lo 2>/dev/null | grep inet6
    echo "--- links ---"; ip -br link show 2>/dev/null
    echo "--- all ipv6 addrs ---"; ip -o -6 addr show 2>/dev/null | grep -v '^1 '
    echo "--- link audit ---"; link_audit
    echo "--- neigh (all) ---"; ip -6 neigh show 2>/dev/null
    echo "--- qdisc ---"; for i in $(data_ifaces); do tc -s qdisc show dev $i 2>/dev/null; done
    echo "--- isis ---"; vtysh -c 'show isis neighbor' 2>/dev/null
    vtysh -c 'show isis interface detail' 2>/dev/null | grep -E 'Interface:|IPv6|metric'
    echo "--- lsp self ---"; dup_net
    echo "--- srv6 ---"; vtysh -c 'show segment-routing srv6 locator' 2>/dev/null
    echo "--- routes ---"; ip -6 route show 2>/dev/null | head -40
    echo "--- seg6 routes ---"; ip -6 route show 2>/dev/null | grep seg6 | head -20
    echo "--- lsdb.json ---"; [ -f $LSDBF ] && echo "$(wc -c < $LSDBF) bytes @ $(stat -c %y $LSDBF 2>/dev/null)"
    echo "--- path status ---"; cat $PSTAT 2>/dev/null
    echo "--- log tail ---"; tail -60 $LOGF 2>/dev/null
    echo "--- debug tail ---"; tail -60 $DBGF 2>/dev/null
  } > $out 2>&1
  say "诊断包已写到 $out, 取回: docker cp <容器>:$out ."
}

cmd_stop_old() {
  pkill -f '/lsdb-monitor.sh' 2>/dev/null
  pkill -f '/path-tuner.sh' 2>/dev/null
  pkill -f '/auto-config.sh' 2>/dev/null
  pkill -f 'auto66.sh --daemon' 2>/dev/null
  pkill -f 'auto66.sh --tuner'  2>/dev/null
  pkill -f 'auto66.sh --lsdb'   2>/dev/null
  say "已停掉容器里原有的 auto-config/path-tuner/lsdb-monitor/auto66(如果存在)"
}

case "$MODE" in
  start|-h|--help|help) usage; [ "$MODE" = start ] && front_start ;;
  --daemon) run_daemon ;;
  --tuner)  role_identity || exit 1; run_tuner ;;
  --lsdb)   role_identity || exit 1; run_lsdb ;;
  stop)     front_stop ;;
  restart)  front_stop; sleep 2; front_start ;;
  status)   cmd_status ;;
  gate)     cmd_gate ;;
  report)   cmd_report ;;
  stop-old) cmd_stop_old ;;
  qshow)    role_identity || exit 1; shift; cmd_qshow  "$@" ;;
  qset)     role_identity || exit 1; shift; cmd_qset   "$@" ;;
  qclear)   role_identity || exit 1; shift; cmd_qclear "$@" ;;
  *)        usage ;;
esac

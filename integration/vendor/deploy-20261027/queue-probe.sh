#!/bin/bash
# =====================================================================
# queue-probe.sh — 单容器发送队列采集器(容器内 root 运行)
#
# 依据集成方《发给课题1/3：链路 qdisc 配置与队列积压采集要求》(2026-10-21):
#   * 方向: 源节点容器内的出口队列才代表 src->dst, 不按接口名猜, 用 /cfg.sh 的 linkid 定位
#   * 单位: backlog(字节) / qlen(包) / drops(累计) 三者不可互换, 每个字段自带单位后缀
#   * 缺失 != 0: 没有可测根队列时写 null + 原因, 绝不拿 noqueue 的零值冒充实测
#   * 只读根队列: 根统计已含子队列, 不再与 child 相加
#   * 管理口 eth0、lo、无对端的备用口一律不采
#
# 本脚本只做数据生产。上报/写 Redis/接口暴露由课题三负责, 不在本脚本范围内。
# 出口口径(2026-10-26 定): 只有 --once 这一个用法。队列由 frr-auto66-lite.sh 定时调
# `--once` 取回, 作为 "queues" 段附在 /lsdb.json 末尾 —— 课题三读时延的那一个文件就
# 顺带读到队列, 不用起常驻进程, 也不用解析 tc 文本。
# 刷新频率不在本脚本里, 由 lite 的 AUTO_QPOLL(默认 7 秒)与容器内 /run/queue-poll 决定,
# 实际生效值写在 /lsdb.json 顶层 queue_poll_s 字段里。
# 若沿用集成方 scripts/queue_agent.py 的 Redis 形状, 从 queues.links 里取:
#   HSET telemetry:queue:<node>  <interface>=queue_bytes  <interface>_timestamp=timestamp
#
# 采集开销(2026-10-26 重写): 一次 --once 只 fork 5 次 —— 读接线表 1 次 awk,
# `ip -br link show` + `tc -s qdisc show` + 1 次 awk 解析全部业务口, stat 1 次。
# 旧写法逐口 `tc -s qdisc show dev X` 再串 20 个 grep/awk, 四口一趟约 80 次 fork、
# 0.73~0.86 秒, 是 1 秒采样的唯一拦路项。
#
# 用法(容器内):
#   bash /queue-probe.sh --once    采一次, JSON 打到标准输出(只读, 不落盘、不改网络)
#   bash /queue-probe.sh           不带参数=自检容器里 tc/awk 等依赖在不在
# 环境变量: QPROBE_CFG=/cfg.sh
# =====================================================================

SELF=${BASH_SOURCE[0]}
MODE=${1:-check}
CFG=${QPROBE_CFG:-/cfg.sh}
SRC_TAG=${QPROBE_SOURCE:-task1-queue-probe}

say() { echo "$*"; }

# ---- 一次 awk 读懂接线表: 首行 "ME <节点>", 之后每行 "LINK <eth> <对端>" ----
# linkid 里除本节点外的另一个名字就是对端; 只有名字里带 Lxxxx 的接线才认。
parse_cfg() {
  [ -r "$CFG" ] || return 1
  awk '
    { s = s $0 }
    END {
      gsub(/[ \t\r\n]/, "", s)
      me = ""
      if (match(s, /"name":"[^"]+"/)) me = substr(s, RSTART + 8, RLENGTH - 9)
      if (me == "" && match(s, /"linkid":\["[^"]*"\]/)) {
        t = substr(s, RSTART, RLENGTH)
        if (match(t, /L[0-9][0-9][0-9][0-9]/)) me = toupper(substr(t, RSTART, RLENGTH))
      }
      me = toupper(me)
      printf "ME %s\n", me
      rest = s
      while (match(rest, /"eth":"eth[0-9]+","linkid":\["[^"]*"\]/)) {
        tok = substr(rest, RSTART, RLENGTH); rest = substr(rest, RSTART + RLENGTH)
        e = tok; sub(/^"eth":"/, "", e); sub(/",.*$/, "", e)
        lid = tok; sub(/^.*"linkid":\["/, "", lid); sub(/"\]$/, "", lid)
        peer = ""; t = lid
        while (match(t, /L[0-9][0-9][0-9][0-9]/)) {
          k = toupper(substr(t, RSTART, 5)); t = substr(t, RSTART + 5)
          if (k != me) peer = k
        }
        if (peer != "") printf "LINK %s %s\n", e, peer
      }
    }' "$CFG"
}

# ---- 一次 ip + 一次 tc + 一次 awk 把所有业务口读完 ----
# 输出每行 "<eth>|<queue_bytes>|<qlen>|<drops>|<overlimits>|<capacity>|<delay>|<qdisc>|<reason>|<carrier>"
# 判类顺序与集成方 lsdb.py 一致: 没根队列 -> 根队列属排除集 -> 多个根 -> 没有 backlog 字段 -> 可测。
# 排除集照抄 lsdb.py:127(noqueue clsact ingress mq), pfifo_fast 属可测。
probe_snapshot() {
  { echo '@@LINK'
    ip -br link show 2>/dev/null
    echo '@@TC'
    tc -s qdisc show 2>/dev/null
  } | awk -v ifcs="$1" '
    function rate2mbps(tok,   v, u) {
      v = tok + 0; u = tok
      sub(/^[0-9.]+/, "", u); sub(/[bB][iI][tT]$/, "", u); u = toupper(u)
      if (u == "K") return v / 1000
      if (u == "G") return v * 1000
      if (u == "T") return v * 1000000
      if (u == "M") return v
      return v / 1000000
    }
    function delay2ms(tok,   v, u) {
      v = tok + 0; u = tok; sub(/^[0-9.]+/, "", u)
      if (u == "us") return v / 1000
      if (u == "s")  return v * 1000
      return v
    }
    BEGIN { n = split(ifcs, want, " "); for (i = 1; i <= n; i++) w[want[i]] = 1 }
    /^@@LINK$/ { sec = 1; next }
    /^@@TC$/   { sec = 2; next }
    sec == 1 { d0 = $1; sub(/@.*/, "", d0)
               if (d0 != "IFACE" && (d0 in w)) car[d0] = ($0 ~ /LOWER_UP/) ? "up" : "down"; next }
    sec == 2 {
      if ($1 == "qdisc") {
        cur = ""
        for (i = 1; i < NF; i++) if ($i == "dev") { if ($(i + 1) in w) cur = $(i + 1); break }
        if (cur == "") next
        seen[cur] = 1
        if ($0 ~ /(^|[^[:alnum:]_])root([^[:alnum:]_]|$)/) {
          nr[cur]++
          if (nr[cur] == 1) { kd[cur] = $2; hd[cur] = $3 }
        }
      } else if (cur == "") next
      low = tolower($0)
      if (match(low, /rate [0-9.]+[kmgt]?bit/)) {
        v = rate2mbps(substr($0, RSTART + 5, RLENGTH - 5))
        if (!(cur in cap) || v < cap[cur]) cap[cur] = v
      }
      if (match(low, /delay [0-9.]+(us|ms|s)/)) dl[cur] += delay2ms(substr($0, RSTART + 6, RLENGTH - 6))
      if (!(cur in dp) && match(low, /dropped [0-9]+/))    dp[cur] = substr($0, RSTART + 8,  RLENGTH - 8)
      if (!(cur in ov) && match(low, /overlimits [0-9]+/)) ov[cur] = substr($0, RSTART + 11, RLENGTH - 11)
      if (!(cur in bl) && match(low, /backlog [0-9]+b/)) {
        bl[cur] = substr($0, RSTART + 8, RLENGTH - 9)
        for (i = 1; i <= NF; i++) if ($i ~ /^[0-9]+p$/) { qp[cur] = substr($i, 1, length($i) - 1); break }
      }
      if (!(cur in sb) && index(low, "sent") > 0) {
        for (i = 2; i <= NF; i++) {
          if ($i == "bytes") sb[cur] = $(i - 1)
          if ($i == "pkt")   sp[cur] = $(i - 1)
        }
      }
      next
    }
    END {
      for (i = 1; i <= n; i++) {
        d = want[i]; c = (d in car) ? car[d] : "down"
        if (!(d in seen)) { printf "%s|null|null|null|null|null|null|none|iface-missing-or-no-tc|down\n", d; continue }
        k = (d in kd) ? kd[d] : ""
        if (k == "")      { printf "%s|null|null|null|null|null|null|none|no-root-qdisc|%s\n", d, c; continue }
        if (k == "noqueue" || k == "clsact" || k == "ingress" || k == "mq")
                          { printf "%s|null|null|null|null|null|null|%s %s|not-measurable-kind|%s\n", d, k, hd[d], c; continue }
        if (nr[d] != 1)   { printf "%s|null|null|null|null|null|null|%s|multiple-root-qdiscs|%s\n", d, k, c; continue }
        if (!(d in bl))   { printf "%s|null|null|null|null|null|null|%s %s|no-backlog-field|%s\n", d, k, hd[d], c; continue }
        printf "%s|%s|%s|%s|%s|%s|%s|%s %s Sent %sb/%sp|ok|%s\n", d, bl[d],
          (d in qp) ? qp[d] : "null", (d in dp) ? dp[d] : "null", (d in ov) ? ov[d] : "null",
          (d in cap) ? sprintf("%.4f", cap[d]) : "null", (d in dl) ? sprintf("%.3f", dl[d]) : "null",
          k, hd[d], (d in sb) ? sb[d] : 0, (d in sp) ? sp[d] : 0, c
      }
    }'
}

sample_once() {
  local me="" ifcs="" e p k v line ts iso ino up btime="" kk vv first=1 m=0 n=0 f2 f3 f4 f5 f6 f7 f8 f9 f10
  local -A QBY QLP DPP OVP CAP DLY QDS RSN CAR
  local LINKS=()
  line=$(parse_cfg) || { say "FATAL: 读不到接线表 $CFG"; return 1; }
  while read -r k v; do
    case "$k" in
      ME)   me=$v ;;
      LINK) ifcs="$ifcs${ifcs:+ }${v%% *}"; LINKS+=("$v") ;;
    esac
  done <<EOF
$line
EOF
  [ -n "$me" ] || { say "FATAL: $CFG 里认不出本节点身份(需要 name 或 linkid)"; return 1; }
  [ ${#LINKS[@]} -gt 0 ] || { say "FATAL: $CFG 里没有带对端的业务口"; return 1; }

  while IFS='|' read -r e f2 f3 f4 f5 f6 f7 f8 f9 f10; do
    [ -n "$e" ] || continue
    QBY[$e]=$f2; QLP[$e]=$f3; DPP[$e]=$f4; OVP[$e]=$f5; CAP[$e]=$f6; DLY[$e]=$f7
    QDS[$e]=$f8; RSN[$e]=$f9; CAR[$e]=$f10
  done <<EOF
$(probe_snapshot "$ifcs")
EOF

  read -r up _ < /proc/uptime
  while read -r kk vv; do [ "$kk" = btime ] && btime=$vv; done < /proc/stat
  if [ -n "$btime" ]; then ts=$(( btime + ${up%%.*} )).${up#*.}; else ts=$(date +%s.%N); fi
  printf -v iso '%(%Y-%m-%dT%H:%M:%S%z)T' -1
  ino=$(stat -L -c %i /proc/self/ns/net 2>/dev/null)

  printf '{\n'
  printf '  "node": "%s",\n' "$me"
  printf '  "namespace": "container:%s",\n' "${HOSTNAME:-$(hostname)}"
  printf '  "netns_inode": "%s",\n' "$ino"
  printf '  "timestamp": %s,\n' "$ts"
  printf '  "sampled_at": "%s",\n' "$iso"
  printf '  "units": {"queue_bytes":"瞬时排队字节(停手即回 0)","qlen_packets":"瞬时排队包数","drops_total":"累计丢包(只增不减, 不可当 backlog)","overlimits_total":"累计超速令牌事件(TBF 拥塞时先涨这个, dropped 可能一直为 0)","capacity_mbps":"根队列 rate 解析(多 rate 取 min)","delay_ms":"该口所有 delay 计数之和(lat 不算时延)"},\n'
  printf '  "links": [\n'
  for v in "${LINKS[@]}"; do
    e=${v%% *}
    n=$((n + 1))
    [ $first -eq 0 ] && printf ',\n'
    first=0
    if [ -z "${QBY[$e]:-}" ] || [ "${QBY[$e]}" = "null" ]; then
      # 缺失不等于 0: 集成方明令不能用 noqueue 的零值冒充实测队列
      printf '    {"src":"%s","dst":"%s","interface":"%s","queue_bytes":null,"qlen_packets":null,"drops_total":null,"overlimits_total":null,"capacity_mbps":null,"delay_ms":null,"qdisc":"%s","measured":false,"reason":"%s","source":"%s"}' \
        "$me" "${v#* }" "$e" "${QDS[$e]}" "${RSN[$e]}" "$SRC_TAG"
    else
      m=$((m + 1))
      printf '    {"src":"%s","dst":"%s","interface":"%s","queue_bytes":%s,"qlen_packets":%s,"drops_total":%s,"overlimits_total":%s,"capacity_mbps":%s,"delay_ms":%s,"qdisc":"%s","carrier":"%s","measured":true,"source":"%s"}' \
        "$me" "${v#* }" "$e" "${QBY[$e]}" "${QLP[$e]}" "${DPP[$e]}" "${OVP[$e]}" "${CAP[$e]}" "${DLY[$e]}" \
        "${QDS[$e]}" "${CAR[$e]}" "$SRC_TAG"
    fi
  done
  printf '\n  ],\n'
  printf '  "links_measured": %s,\n' "$m"
  printf '  "links_total": %s\n' "$n"
  printf '}\n'
}

usage() { awk '/^# =====/{c++; next} c == 1' "$SELF"; }

# 不带参数 = 自检: 依赖齐不齐、接线表能不能认出本节点、每个业务口的根队列是什么类
self_check() {
  local c missing="" cfg me="" ifcs="" e v snap d rest q1 q2 q3 q4 q5 q6 qd rs car
  local -A REC PEER
  local ORDER=()
  for c in tc ip awk stat; do
    command -v "$c" >/dev/null 2>&1 || missing="$missing $c"
  done
  [ -n "$missing" ] && { say "FATAL: 容器内缺命令:$missing"; return 1; }
  cfg=$(parse_cfg) || { say "FATAL: 读不到接线表 $CFG"; return 1; }
  while read -r c v; do
    case "$c" in
      ME)   me=$v ;;
      LINK) e=${v%% *}; PEER[$e]=${v#* }; ORDER+=("$e"); ifcs="$ifcs${ifcs:+ }$e" ;;
    esac
  done <<EOF
$cfg
EOF
  [ -n "$me" ] || { say "FATAL: $CFG 里认不出本节点身份(需要 name 或 linkid)"; return 1; }
  say "本节点: $me    接线表: $CFG"
  say "业务口与根队列(采不到实值的那几个会在 queues 里写 null+reason):"
  snap=$(probe_snapshot "$ifcs")
  while IFS='|' read -r d rest; do REC[$d]=$rest; done <<EOF
$snap
EOF
  for e in "${ORDER[@]}"; do
    IFS='|' read -r q1 q2 q3 q4 q5 q6 qd rs car <<< "${REC[$e]-}"
    printf '  %-6s -> %-6s  qdisc=%-16s carrier=%-5s %s\n' "$e" "${PEER[$e]}" "$qd" "$car" \
      "$([ "${q1:-null}" = null ] && printf 'reason=%s' "$rs" || printf '实测 backlog=%sb %sp' "$q1" "$q2")"
  done
}

case "$MODE" in
  --once) sample_once ;;
  -h|--help|help) usage ;;
  ""|check) self_check ;;
  *) say "本脚本只有 --once 一个用法(不带参数=自检)"; usage; exit 2 ;;
esac

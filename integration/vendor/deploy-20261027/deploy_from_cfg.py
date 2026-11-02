#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
deploy_from_cfg.py — 以 /cfg.sh 为唯一事实源的一键部署器(现场形状 66 节点仿真)

与 gen_field_env.py 的方向相反: 那个是"按内置拓扑建网再写 cfg", 本脚本是"按你手上
的 cfg 建网, cfg 一个字节都不改"。cfg 说的接口名、MAC、对端 linkid、管理 IP 全部照
落地, 因此部署完容器内 `/cfg.sh` 的 md5 与你提供的原件完全一致。

    cfg 目录里每份文件 = 一个节点, 形状与现场镜像内那一份相同:
        {"man":{"mac","eth":"eth0","veth","ip"}, "name":"L0305",
         "ports":{ "<mac>": {"eth":"eth1","linkid":["L0305L0306"],"veth"}, ... }}

    linkid 是两个节点名按字典序拼接、无分隔符。同一条链路在两端的 cfg 里各记一次,
    linkid 相同 -> 建一对 veth, 两端分别落进两个容器的 netns 并按各自 cfg 改名。
    linkid 为空 -> 备用口, 一端进容器、一端留宿主并置 down。

用法(默认只出计划, 不动任何东西):
    python3 deploy_from_cfg.py --cfg-dir cfg/                 # 干跑核对
    python3 deploy_from_cfg.py --cfg-dir cfg/ --apply         # 真建
    python3 deploy_from_cfg.py --cfg-dir cfg/ --verify        # 建完体检
    python3 deploy_from_cfg.py --undo                          # 只回收本脚本建的东西

现场只有一份 cfg 或一份都没有时, 先按 6x11 环网生成一套形状一致的 cfg:
    python3 deploy_from_cfg.py --gen-cfg cfg/ --rings 6 --cols 11
"""
import argparse
import json
import os
import random
import re
import subprocess
import sys

SUDO = os.environ.get("SUDO", "sudo")
STATE = os.environ.get("DEPLOY_STATE", "deploy_state.json")
NODE_RE = re.compile(r"^L\d{4}$")
NAME_RE = re.compile(r"^L\d{4}$")


def run(cmd, check=True):
    p = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if p.returncode != 0 and check:
        raise SystemExit("命令失败(exit=%d): %s\n%s" % (
            p.returncode, cmd, p.stdout.decode("utf-8", "replace").strip()))
    return p.stdout.decode("utf-8", "replace").strip()


def quiet(cmd, check=False):
    return run(cmd, check=check)


# --------------------------------------------------------------- cfg 读取与校验
def load_cfgs(d):
    nodes = {}
    for fn in sorted(os.listdir(d)):
        path = os.path.join(d, fn)
        if not os.path.isfile(path):
            continue
        txt = open(path, encoding="utf-8").read().strip()
        if not txt.startswith("{"):
            continue
        cfg = json.loads(txt)
        me = cfg.get("name", "")
        if not NODE_RE.match(me):
            sys.exit("cfg %s 的 name=%r 不是 L 四位数字, 脚本无法认身份" % (fn, me))
        if me in nodes:
            sys.exit("两份 cfg 都声明自己是 %s: %s" % (me, fn))
        nodes[me] = {"file": path, "cfg": cfg}
    if not nodes:
        sys.exit("%s 里没有可解析的 cfg" % d)
    return nodes


def parse_ports(nodes):
    """返回 links{linkid:[(node, eth, mac, veth)]}, spares[], mgmt[], 以及错误列表"""
    links, spares, mgmt, err = {}, [], [], []
    for me, n in nodes.items():
        cfg = n["cfg"]
        seen_eth = set()
        for mac, p in cfg.get("ports", {}).items():
            eth = p.get("eth", "")
            if eth in seen_eth:
                err.append("%s 接口名重复: %s" % (me, eth))
            seen_eth.add(eth)
            lid = p.get("linkid") or []
            if not lid:
                spares.append((me, eth, mac, p.get("veth", "")))
                continue
            if len(lid) > 1:
                err.append("%s %s 有多个 linkid: %r (脚本只支持一口一链路)" % (me, eth, lid))
                continue
            key = lid[0]
            if me not in key:
                err.append("%s %s 的 linkid=%s 里不含本节点名" % (me, eth, key))
            links.setdefault(key, []).append((me, eth, mac, p.get("veth", "")))
        m = cfg.get("man", {})
        if m.get("eth"):
            mgmt.append((me, m["eth"], m.get("mac", ""), m.get("veth", ""), m.get("ip", "")))
    for key, eps in sorted(links.items()):
        if len(eps) == 1:
            err.append("链路 %s 只有一端(%s %s), 对端 cfg 不在目录里" % (key, eps[0][0], eps[0][1]))
        elif len(eps) > 2:
            err.append("链路 %s 有 %d 端, 接线表不自洽" % (key, len(eps)))
    return links, spares, mgmt, err


def check_names(nodes, links, spares, mgmt, applying=False):
    """接口名/MAC/宿主名冲突先查出来, 别建到一半失败留下半成品"""
    err = []
    for key, eps in links.items():
        (a, pa, ma, va), (b, pb, mb, vb) = eps
        if a == b:
            err.append("链路 %s 两端同节点 %s(自环不支持)" % (key, a))
    host_names = [v for _, _, _, v in spares] + [v for _, _, _, v, _ in mgmt]
    dup = {x for x in host_names if x and host_names.count(x) > 1}
    if dup:
        err.append("宿主机侧接口名重复: %s" % " ".join(sorted(dup)))
    exist = set(quiet("ip -br link | awk '{print $1}'").split())
    clash = sorted({x for x in host_names if x and x in exist})
    if clash and applying:
        err.append("宿主上已存在同名接口, 换 cfg 或先回收: %s" % " ".join(clash[:8]))
    live = set(quiet("%s docker ps -a --format '{{.Names}}'" % SUDO).split())
    if applying:
        busy = sorted(n for n in nodes if n in live)
        if busy:
            err.append("容器名已被占用(脚本默认不覆盖): %s" % " ".join(busy[:8]))
    elif clash:
        print("提示: 宿主上已有 %d 个同名接口(仅 --apply 才拦): %s"
              % (len(clash), " ".join(clash[:5])))
    return err


# --------------------------------------------------------------- 建网
def docker_run(me, image):
    return run("%s docker run -d --name %s --privileged --cap-add NET_ADMIN --network none "
               "--sysctl net.ipv6.conf.all.disable_ipv6=0 "
               "--sysctl net.ipv6.conf.all.forwarding=1 %s" % (SUDO, me, image))


_PID = {}


def pid_of(me):
    if me not in _PID:
        _PID[me] = run("%s docker inspect -f '{{.State.Pid}}' %s" % (SUDO, me))
    return _PID[me]


def iface_in(me, dev, newname, mac, up):
    if mac:
        # MAC 只能在宿主侧、设备改名前进 netns 之前设(移动后旧名就没了)
        run("%s ip link set %s address %s" % (SUDO, dev, mac))
    run("%s ip link set %s netns %s" % (SUDO, dev, pid_of(me)))
    run("%s nsenter -t %s -n ip link set %s name %s" % (SUDO, pid_of(me), dev, newname))
    if up:
        run("%s nsenter -t %s -n ip link set %s up" % (SUDO, pid_of(me), newname))


def apply(nodes, links, spares, mgmt, image, lite, probe):
    state = {"containers": [], "host_ends": [], "links": [], "spares": [], "mgmt": []}
    try:
        for me in sorted(nodes):
            docker_run(me, image)
            state["containers"].append(me)
        save(state)
        print("容器就绪 %d/%d" % (len(state["containers"]), len(nodes)))

        for key, eps in sorted(links.items()):
            (a, pa, ma, va), (b, pb, mb, vb) = eps
            na = va or ("vz_%s_a" % key[-8:])   # cfg 里那端记的是对面的名字, 直接拿来用
            nb = vb or ("vz_%s_b" % key[-8:])
            run("%s ip link add %s address %s type veth peer name %s address %s"
                % (SUDO, na, ma, nb, mb))
            state["host_ends"] += [na, nb]
            iface_in(a, na, pa, None, False)
            iface_in(b, nb, pb, None, False)
            state["links"].append([a, pa, b, pb, key])
            save(state)

        for me, eth, mac, hv in spares:
            host = hv or ("vz_%s_%s" % (me, eth))
            tmp = "vz_t_%s_%s" % (me, eth)
            run("%s ip link add %s type veth peer name %s" % (SUDO, host, tmp))
            state["host_ends"].append(host)
            iface_in(me, tmp, eth, mac, False)
            run("%s ip link set %s down" % (SUDO, host), check=False)
            state["spares"].append([me, eth, host])
            save(state)

        for me, eth, mac, hv, ip in mgmt:
            host = hv or ("vz_%s_m" % me)
            tmp = "vz_%s_mt" % me
            run("%s ip link add %s type veth peer name %s" % (SUDO, host, tmp))
            state["host_ends"].append(host)
            iface_in(me, tmp, eth, mac, True)
            if ip:
                run("%s nsenter -t %s -n ip addr add %s/16 dev %s" % (SUDO, pid_of(me), ip, eth),
                    check=False)
            run("%s ip link set %s up" % (SUDO, host), check=False)
            state["mgmt"].append([me, eth, host, ip])
            save(state)
        print("接线完成: 链路 %d 条, 备用口 %d, 管理口 %d"
              % (len(state["links"]), len(state["spares"]), len(state["mgmt"])))

        for me, n in sorted(nodes.items()):
            run("%s docker cp %s %s:/cfg.sh" % (SUDO, n["file"], me))
            if lite:
                run("%s docker cp %s %s:/auto66-lite.sh" % (SUDO, lite, me))
            if probe:
                run("%s docker cp %s %s:/queue-probe.sh" % (SUDO, probe, me))
        print("cfg.sh 原样写入 %d 份(md5 与你给的原件一致)" % len(nodes))

        for me, _ in sorted(nodes.items()):
            run("%s docker exec %s sh -c 'for d in $(ls /sys/class/net | grep ^eth); do "
                "ip link set $d up; done'" % (SUDO, me), check=False)
        print("业务口已置 up(两端都 up 才有载波)")
    except SystemExit as e:
        print("!! 中途失败, 已建对象记在 %s, 回滚执行: python3 %s --undo" % (STATE, sys.argv[0]))
        raise


def save(state):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def load_state():
    if not os.path.exists(STATE):
        sys.exit("没有 %s, 本脚本不知道该回收哪些对象(绝不做全量清理)" % STATE)
    return json.load(open(STATE, encoding="utf-8"))


def undo():
    st = load_state()
    for c in st.get("containers", []):
        quiet("%s docker rm -f %s" % (SUDO, c))
    for h in st.get("host_ends", []):
        quiet("%s ip link del %s" % (SUDO, h))
    print("回收容器 %d 个、宿主侧接口 %d 个(只删状态文件里记的)"
          % (len(st.get("containers", [])), len(st.get("host_ends", []))))
    os.remove(STATE)


def start_lite(nodes):
    for me in sorted(nodes):
        quiet("%s docker exec -d %s bash /auto66-lite.sh" % (SUDO, me))
    print("已在 %d 个容器内拉起 lite(约 60~180 秒收敛)" % len(nodes))


# --------------------------------------------------------------- 体检
def lo_of(name):
    x, y = int(name[1:3]), int(name[3:5])   # 十进制读行列, 十六进制进地址(L0611 -> fc00:0:6::b)
    return "fc00:0:%x::%x" % (x, y)


def verify(nodes, links):
    want, bifs = {}, {}
    for eps in links.values():
        for me, eth, _, _ in eps:
            want[me] = want.get(me, 0) + 1
            bifs.setdefault(me, set()).add(eth)
    tot_up = tot_want = 0
    bad = []
    for me in sorted(nodes):
        txt = quiet("%s docker exec %s vtysh -c 'show isis neighbor' 2>/dev/null" % (SUDO, me))
        up = len(re.findall(r"\bUp\b", txt))
        tot_up += up
        tot_want += want.get(me, 0)
        if up != want.get(me, 0):
            bad.append("%s 邻居 %d/%d" % (me, up, want.get(me, 0)))
        rows = quiet("%s docker exec %s ip -br link" % (SUDO, me)).splitlines()
        for r in rows:
            f = r.split()
            if not f:
                continue
            d = f[0].split("@")[0]
            if d in bifs.get(me, set()) and "LOWER_UP" not in r:
                bad.append("%s %s 无载波(对端没 up 或没接上)" % (me, d))
    print("邻居 Up 合计 %d, 期望 %d; 异常 %d 项" % (tot_up, tot_want, len(bad)))
    for b in bad[:12]:
        print("  " + b)
    ns = sorted(nodes)
    if len(ns) >= 2:
        a, z = ns[0], ns[-1]
        print("端到端 %s -> %s(%s): %s" % (a, z, lo_of(z),
              quiet("%s docker exec %s ping6 -c 3 -W 2 %s 2>&1 | tail -1"
                    % (SUDO, a, lo_of(z)))))
    tail = quiet("%s docker exec %s tail -c 400 /lsdb.json" % (SUDO, ns[0]), check=False)
    print("队列尾部(%s): %s" % (ns[0], tail[-260:] if tail else "还没生成"))


# --------------------------------------------------------------- 生成一套 cfg(现场无 cfg 时)
def gen_cfg(d, rings, cols, spares_n, seed):
    rng = random.Random(seed)
    os.makedirs(d, exist_ok=True)

    def nm(x, y):
        return "L%02d%02d" % (x, y)

    alln = [nm(x, y) for x in range(1, rings + 1) for y in range(1, cols + 1)]
    links = []
    for x in range(1, rings + 1):
        for y in range(1, cols + 1):
            me = nm(x, y)
            links.append((me, "eth1", nm(x, y % cols + 1), "eth2"))
            links.append((me, "eth3", nm((x - 2) % rings + 1, y), "eth4"))
    idx = {}
    for a, pa, b, pb in links:
        idx[(a, pa)] = (b, pb)
        idx[(b, pb)] = (a, pa)
    ports = ["eth1", "eth2", "eth3", "eth4"] + ["eth%d" % (5 + i) for i in range(spares_n)]
    rid = rng.randint(10000, 99999)
    macs = set()

    def mac():
        while True:
            # 首字节强制"单播 + 本地管理"(02-3e 类), 否则内核拒绝把它设成接口地址
            b0 = (rng.randint(0, 255) & 0xfe) | 0x02
            m = "%02x:" % b0 + ":".join("%02x" % rng.randint(0, 255) for _ in range(5))
            if m not in macs:
                macs.add(m)
                return m
    seq = 0
    for i, me in enumerate(alln, 1):
        body = {"man": {"type": "veth", "mac": mac(), "eth": "eth0",
                        "veth": "ve_%d_m%d_i" % (rid, i), "ip": "14.84.4.%d" % i},
                "name": me, "ports": {}}
        for p in ports:
            seq += 1
            peer = idx.get((me, p), (None, None))
            if peer[0]:
                lid = ["".join(sorted([me, peer[0]]))]
                hv = "ve_%d_%d_o" % (rid, seq)
            else:
                lid = []
                hv = "ve_%d_%d_i" % (rid, seq)
            body["ports"][mac()] = {"type": "veth", "eth": p, "linkid": lid, "veth": hv}
        open(os.path.join(d, "%s.cfg.sh" % me), "w", encoding="utf-8").write(
            json.dumps(body, ensure_ascii=False))
    print("已按 %dx%d 环网生成 %d 份 cfg 到 %s (runid=%d, 业务链路 %d 条)"
          % (rings, cols, len(alln), d, rid, len(links)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cfg-dir", default="cfg")
    ap.add_argument("--image", default="frr-srv6-qw:latest")
    ap.add_argument("--lite", default="auto66-lite.sh")
    ap.add_argument("--probe", default="queue-probe.sh")
    ap.add_argument("--plan", action="store_true", help="只出计划(默认)")
    ap.add_argument("--apply", action="store_true", help="真建: 容器+veth+写 cfg")
    ap.add_argument("--with-lite", action="store_true", help="建完顺手拉起 lite")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--undo", action="store_true", help="只回收状态文件里记的对象")
    ap.add_argument("--gen-cfg", metavar="DIR", help="按环网生成一套 cfg 后退出")
    ap.add_argument("--rings", type=int, default=6)
    ap.add_argument("--cols", type=int, default=11)
    ap.add_argument("--spares", type=int, default=3)
    ap.add_argument("--seed", type=int, default=20261027)
    a = ap.parse_args()

    if a.gen_cfg:
        gen_cfg(a.gen_cfg, a.rings, a.cols, a.spares, a.seed)
        return
    if a.undo:
        undo()
        return

    nodes = load_cfgs(a.cfg_dir)
    links, spares, mgmt, err = parse_ports(nodes)
    err += check_names(nodes, links, spares, mgmt, applying=a.apply)
    if err:
        print("接线表不自洽, 先修 cfg 再建:")
        for e in err[:20]:
            print("  - " + e)
        sys.exit(1)

    print("节点 %d 个, 业务链路 %d 条, 备用口 %d, 管理口 %d"
          % (len(nodes), len(links), len(spares), len(mgmt)))
    for key in sorted(links)[:3]:
        (a1, p1, _, _), (b1, q1, _, _) = links[key]
        print("  %s: %s:%s <-> %s:%s" % (key, a1, p1, b1, q1))
    print("  ...")

    if a.apply:
        lite = a.lite if os.path.exists(a.lite) else None
        probe = a.probe if os.path.exists(a.probe) else None
        apply(nodes, links, spares, mgmt, a.image, lite, probe)
        if a.with_lite:
            start_lite(nodes)
    elif a.verify:
        verify(nodes, links)
    else:
        print("(干跑, 什么都没建。要真建加 --apply, 回滚用 --undo)")


if __name__ == "__main__":
    main()

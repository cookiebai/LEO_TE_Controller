"""Inside-container acceptance of an existing two/three-node subgraph."""
import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid

import networkx as nx
import yaml

from integration.controller import atomic_json, make_native_policy
from integration.lsdb import attach_metrics, coordinates
from integration.native_queues import decode_tail, validate_native_queues
from python_te_solver.lyapunov_solver import LyapunovSolver
from mini_acceptance import probe


ROOT = Path(os.environ.get('MINI_DATA_DIR', '/data'))


def load(path, default=None):
    p=Path(path)
    return json.loads(p.read_text()) if p.exists() else default


def state():
    ROOT.mkdir(parents=True,exist_ok=True)
    return load(ROOT/'state.json', {'token':uuid.uuid4().hex,'nodes':{},'order':[],
                                  'repairs':{},'stage':'not_selected'})


def save(data):
    atomic_json(ROOT/'state.json',data)


def store(name,data):
    atomic_json(ROOT/name,data)


@contextmanager
def lock():
    ROOT.mkdir(parents=True,exist_ok=True)
    with (ROOT/'.operation.lock').open('w') as f:
        try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('另一个修改操作正在进行，请等其结束')
        yield


def settings():
    return load(ROOT/'settings.json',{'lsdb_path':'/lsdb.json','prefix':None})


def discovery(prefix=None,names=None):
    config=settings()
    if prefix is not None:
        config['prefix']=prefix or None;store('settings.json',config)
    nodes,graph,report=probe.scan(config.get('prefix') if names is None else None,names,config['lsdb_path'])
    store('discovery.json',report)
    for node,info in sorted(nodes.items()):
        print(f'{node:6s} {info["container"]:30s} peers={",".join(info["peers"])} '
              f'End={bool(info["end_route"])} full-LSDB={info["full_lsdb_parsed"]}',flush=True)
    for node,names in report['ambiguous'].items():
        print('AMBIGUOUS:',node,names,'请用pick --nodes指定实际容器名或scan --prefix限制一套网络',flush=True)
    print(f'SCAN: {len(nodes)}可用身份，{graph.number_of_edges()}条双方声明且接口在位的有向邻接；'
          f'{sum(bool(r.get("excluded")) for r in report["candidates"])}个容器已排除。',flush=True)
    return nodes,graph


def selected(refresh=True):
    s=state()
    if len(s['order'])<2:raise ValueError('先执行 mini pick --count 3（有问题会回退现有两节点）')
    if refresh:
        old=s['nodes'];names=[old[n]['container'] for n in s['order']]
        nodes,graph,_=probe.scan(names=names,lsdb_path=settings()['lsdb_path'])
        if set(nodes)!=set(old):raise ValueError('所选节点身份/接口/End能力变化，请mini doctor检查后重新pick')
        for node in old:
            if nodes[node]['container_id']!=old[node]['container_id']:
                raise ValueError(f'{node}容器被重建，拒绝沿用旧测试状态')
        if not nx.is_strongly_connected(graph):raise ValueError('所选子网失去双方邻接，不能用Ping虚构一条直连边')
        s['nodes']=nodes
    else:graph=probe.observed_graph(s['nodes'])
    return s,graph


def show_topology(s,graph):
    for alias,node in zip('ABC',s['order']):
        info=s['nodes'][node]
        print(f'{alias} = {node} / {info["container"]} / loopback={info["loopback"]} / SID={info["sid"]}',flush=True)
        if not info['full_lsdb_parsed']:
            print('  全网LSP解析不可用，使用原LSDB中的真实本地邻接字段：',info['full_lsdb_error'],flush=True)
    for src,dst,data in graph.edges(data=True):
        print(f'  {src} -> {dst}  {data["interface"]}  source={data["topology_source"]}',flush=True)
    store('topology.json',{'nodes':dict(graph.nodes(data=True)),
                           'links':[dict(src=u,dst=v,**a) for u,v,a in graph.edges(data=True)]})
    print('TOPOLOGY PASS: 已读取原LSDB局部拓扑，未推算或生成网格边',flush=True)


def pick(count=3,explicit=None,prefix=None):
    s=state()
    if s.get('policy') or s['repairs']:raise ValueError('先 mini undo --restore-queues，再重新选节点')
    all_names=probe.call(['docker','ps','--format','{{.Names}}']).splitlines()
    only_names=explicit if explicit and all(n in all_names for n in explicit) else None
    nodes,graph=discovery(prefix,only_names)
    ids=None
    if explicit:
        ids=[]
        for value in explicit:
            hits=[node for node,info in nodes.items() if value in (node,info['container'])]
            if len(hits)!=1:raise ValueError(f'无法唯一识别{value}；重名时请填实际容器名')
            ids.append(hits[0])
    groups=probe.select_groups(graph,count,ids)
    # Prefer groups whose existing native queues are already complete/fresh.
    def quality(group):
        good=0;valid=set()
        for node in group:
            try:
                rows=validate_native_queues(nodes[node]['queue_document'],node)
                for q in rows:
                    if q['dst'] in group and all(q.get(k) is not None for k in ('queue_bytes','capacity_mbps','delay_ms')):
                        good+=1;valid.add((q['src'],q['dst']))
            except Exception:pass
        complete=set(graph.subgraph(group).edges)<=valid
        return not complete,-len(group),-good,tuple(group)
    groups=sorted(groups,key=quality)
    if count==2:groups=[g for g in groups if len(g)==2]
    attempts=[]
    for group in groups[:12]:
        sub={node:nodes[node] for node in group}
        evidence=probe.ping_rows(sub,count=1)
        attempts.append({'nodes':group,'ping':evidence})
        if all(row['returncode']==0 for row in evidence):
            s.update(nodes=sub,order=group,stage='selected',last_error=None,algorithm_ok=False,
                     plan_id=None,last_verified_at=None,queue_source=None)
            (ROOT/'plan.json').unlink(missing_ok=True)
            save(s);store('pick-attempts.json',attempts)
            show_topology(s,graph.subgraph(group))
            print(f'PICK PASS: 使用{len(group)}个已有节点，已完成相互Ping',flush=True)
            return s
    store('pick-attempts.json',attempts)
    raise ValueError('未找到可相互Ping的现有2/3节点子网；mini doctor保留证据，检查原auto/节点身份/内核End能力')


def plan(direct=False,rate_kbps=1000):
    s,graph=selected()
    if s.get('policy'):raise ValueError('已有本测试下发记录，先mini undo；不要用新方案覆盖旧回收凭证')
    rows=queue_rows(s,graph,direct)
    attach_metrics(graph,rows)
    config=(Path(__file__).resolve().parents[1] / 'config/controller.yaml')
    # solve_lyapunov_mcf is the project's real numerical solver. Redis client
    # construction is lazy; this calculation does not use an external service.
    solver=LyapunovSolver(config)
    source,dest=s['order'][0],s['order'][-1]
    identifier='mini_'+s['token'][:12]+'_'+uuid.uuid4().hex[:8]
    demand={'id':identifier,'src':source,'dst':dest,'class':'class_2_embb','demand':rate_kbps}
    answer=solver.solve_lyapunov_mcf(graph,[demand])
    if not answer or identifier not in answer:raise ValueError('项目求解器没有返回可安装的路径')
    path=answer[identifier][0]['path']
    policy=make_native_policy(identifier,path,10)
    data={'generated_at':time.time(),'algorithm':'project LyapunovSolver.solve_lyapunov_mcf',
          'demand':demand,'path':path,'policy':policy,'queue_mode':'direct-tc' if direct else 'native-lsdb',
          'nodes':s['nodes'],'link_metrics':[dict(src=u,dst=v,**a) for u,v,a in graph.edges(data=True)]}
    store('plan.json',data);store('plans/'+identifier+'.json',data)
    s.update(stage='planned',algorithm_ok=True,plan_id=identifier,queue_source=data['queue_mode']);save(s)
    print('ALGORITHM PASS:', ' -> '.join(path),flush=True)
    print('计算所得via/SID节点：',policy['via'],'；演示需求：',rate_kbps,'kbps',flush=True)
    print('尚未下发；下一步 mini apply。实际下发前会重读数据并重算。',flush=True)
    return data


def route_segments(text):
    match=re.search(r'encap seg6 mode encap segs\s+\d+\s*\[([^]]+)\]',text)
    return [str(ipaddress.IPv6Address(s)) for s in match.group(1).split()] if match else []


def expected_segments(data):
    return [str(ipaddress.IPv6Address(probe.address(n,True))) for n in data['policy']['via']]


def command_path(s):return '/path.cmd/leo-mini-'+s['token']+'.json'


def conflicts(s,data):
    source=data['policy']['src'];dst=data['policy']['dst'];container=s['nodes'][source]['container']
    files=probe.inside(container,['sh','-c','ls -1 /path.cmd/*.json 2>/dev/null || true']).splitlines()
    for file in files[:256]:
        try:
            value=json.loads(probe.inside(container,['cat','--',file]))
        except (RuntimeError,ValueError):continue
        if value.get('src')==source and value.get('dst')==dst:
            raise ValueError(f'{container}:{file}已有该源目的的策略；请先确认并停止原控制程序，不会清空或覆盖其他策略')
    current=probe.inside(container,['ip','-6','route','get',s['nodes'][dst]['loopback']])
    if 'encap seg6' in current:raise ValueError('目标已走其他SRv6路由，拒绝覆盖；先确认归属和回收')
    return current


def native_wait(s,timeout):
    owned=s['policy'];source=owned['src'];container=s['nodes'][source]['container']
    deadline=time.time()+timeout;last={}
    while True:
        try:
            statuses=json.loads(probe.inside(container,['cat','/path-tuner.status']))
            last=statuses.get(owned['biz_id'],{})
            stamp=last.get('installed_at',0)
            when=float(stamp) if isinstance(stamp,(float,int)) else datetime.fromisoformat(stamp.replace('Z','+00:00')).timestamp()
            if (last.get('status'),last.get('route'),last.get('ping'))==('ok','seg6','pass') and when>=int(owned['submitted_at']):
                store('native-ack.json',last)
                return last
        except (RuntimeError,ValueError,TypeError):pass
        if time.time()>=deadline:
            store('native-ack-last.json',last)
            raise ValueError(f'原执行器未在{timeout}s内确认ok/seg6/pass，最后状态={last}。'
                             'mini doctor查看原进程/日志；不要把写文件当执行成功。可先undo后明确使用apply --kernel排查')
        time.sleep(1)


def apply(kernel=False,direct=False,timeout=60,rate_kbps=1000):
    s,_=selected()
    if s.get('policy'):raise ValueError('已有待确认/已下发策略；请mini verify或mini undo，避免重复写入')
    ping(count=1)
    data=plan(direct,rate_kbps);s=state()
    before=conflicts(s,data)
    policy=data['policy'];source=policy['src'];dest=policy['dst'];container=s['nodes'][source]['container']
    owned={'src':source,'dst':dest,'biz_id':policy['biz_id'],'via':policy['via'],'mode':'kernel' if kernel else 'native',
           'submitted_at':time.time(),'queue_mode':data['queue_mode'],'path':command_path(s),
           'before_route':before,'plan_id':s['plan_id'],'installed':False}
    if kernel:
        initial=json.loads(probe.inside(container,['ip','-j','-6','route','get',s['nodes'][dest]['loopback']]))
        table=initial[0].get('table',254);base_metric=int(initial[0].get('metric',0))
        if base_metric<=1:raise ValueError('原路由metric≤1，无法安全插入更优先的诊断路由；不替换原路由')
        existing=json.loads(probe.inside(container,['ip','-j','-6','route','show','table',str(table),'exact',s['nodes'][dest]['loopback']+'/128']))
        used={int(row.get('metric',0)) for row in existing}
        available=[m for m in range(1,min(base_metric,10)) if m not in used]
        if not available:raise ValueError('没有安全可用的低metric，拒绝覆盖原路由')
        first=policy['via'][0];dev=s['nodes'][source]['peers'][first]['interface']
        owned.update(table=table,metric=available[0],dev=dev)
    s.update(policy=owned,stage='pending');save(s)
    if kernel:
        sids=','.join(expected_segments(data))
        # add (not replace) fails safely if another process races this key.
        probe.inside(container,['ip','-6','route','add','table',str(owned['table']),s['nodes'][dest]['loopback']+'/128',
                                'encap','seg6','mode','encap','segs',sids,'dev',owned['dev'],
                                'proto','196','metric',str(owned['metric'])])
        owned['installed']=True;save(s)
        print('KERNEL INSTALL: 实际添加专属proto196路由；该方式未取得原执行器回执',flush=True)
    else:
        text=json.dumps(policy)+'\n'
        probe.inside(container,['sh','-c','set -eu; mkdir -p /path.cmd; cat > "$1.tmp"; mv "$1.tmp" "$1"',
                                'mini-policy',owned['path']],text=text)
        owned['installed']=True;save(s)
        native_wait(s,timeout)
    s['stage']='applied';save(s)
    return verify(timeout=5)


def verify(timeout=5):
    s,graph=selected()
    owned=s.get('policy')
    if not owned:raise ValueError('没有本测试的下发记录；先mini apply')
    data=load(ROOT/'plans'/(owned['plan_id']+'.json'))
    queue_rows(s,graph,direct=owned['queue_mode']=='direct-tc')
    source=owned['src'];dest=owned['dst'];container=s['nodes'][source]['container']
    route=probe.inside(container,['ip','-6','route','get',s['nodes'][dest]['loopback']])
    expected=expected_segments(data)
    if route_segments(route)!=expected:
        store('route-mismatch.json',{'expected':expected,'actual_route':route})
        raise ValueError('内核SID顺序与算法计划不一致，拒绝通过；见mini doctor')
    ack=native_wait(s,timeout) if owned['mode']=='native' else None
    evidence=probe.ping_rows({n:s['nodes'][n] for n in s['order']},count=1)
    if any(row['returncode'] for row in evidence):
        store('ping-after-apply.json',evidence);raise ValueError('SID已安装但实际Ping不通，不能验收成功')
    full=owned['mode']=='native' and owned['queue_mode']=='native-lsdb'
    proof={'status':'MINI PASS' if full else 'MINI EXPLICIT FALLBACK PASS',
           'plan_id':owned['plan_id'],'policy_active':True,
           'verified_at':time.time(),'topology_from_existing_lsdb':True,
           'lsdb_queues':owned['queue_mode']=='native-lsdb','queue_source':owned['queue_mode'],
           'algorithm_ok':True,'apply_mode':owned['mode'],'native_ack':ack,
           'kernel_sid_matches':True,'expected_sids':expected,'kernel_route':route,
           'mutual_ping':evidence,'nodes':{n:s['nodes'][n]['container'] for n in s['order']}}
    store('acceptance.json',proof);store('acceptance/'+owned['plan_id']+'.json',proof)
    s.update(stage='verified',last_verified_at=proof['verified_at']);save(s)
    print(proof['status']+': 原拓扑 -> 实测队列 -> 项目算法 -> 实际内核SID -> 相互Ping',flush=True)
    print('下发方式：',owned['mode'],'；队列来源：',owned['queue_mode'],'；原生回执：',ack,flush=True)
    if not full:print('降级项已明确记录；直接tc不等于LSDB队列已写回，kernel模式不等于原执行器已确认',flush=True)
    print(route.strip(),flush=True)
    return proof


def check_container(info):
    actual=json.loads(probe.call(['docker','inspect',info['container']]))[0]['Id']
    if actual!=info['container_id']:raise ValueError(f'{info["container"]}已被重建，拒绝修改替换后的容器')


def undo(restore_queues=False):
    s=state();owned=s.get('policy')
    if owned:
        info=s['nodes'][owned['src']];check_container(info)
        container=info['container'];dst=s['nodes'][owned['dst']]['loopback']
        data=load(ROOT/'plans'/(owned['plan_id']+'.json'))
        if owned['mode']=='native':
            raw=probe.call(['docker','exec',container,'cat',owned['path']],check=False)
            if raw.returncode==0:
                actual=json.loads(raw.stdout)
                if actual!=data['policy']:raise ValueError('策略文件已被其他操作修改，拒绝删除')
                probe.inside(container,['rm','-f','--',owned['path']])
            deadline=time.time()+45
            while True:
                route=probe.inside(container,['ip','-6','route','get',dst])
                if 'encap seg6' not in route:break
                if time.time()>=deadline:raise ValueError('自有文件已撤销，但仍有seg6路由；原auto/其他策略需排查，不会泛删目的路由')
                time.sleep(1)
        else:
            text=probe.inside(container,['ip','-6','route','show','table',str(owned['table']),
                                         'proto','196','exact',dst+'/128'])
            lines=[row for row in text.splitlines() if re.search(r'\bmetric\s+'+str(owned['metric'])+r'\b',row)]
            if lines:
                if len(lines)!=1 or route_segments(lines[0])!=expected_segments(data) or f'dev {owned["dev"]}' not in lines[0]:
                    raise ValueError('诊断路由被外部修改，拒绝删除不再匹配的路由')
                probe.inside(container,['ip','-6','route','del','table',str(owned['table']),dst+'/128',
                                        'proto','196','metric',str(owned['metric']),'dev',owned['dev']])
        s.pop('policy',None);s.update(stage='undone');save(s)
        proof=load(ROOT/'acceptance.json')
        if proof and proof.get('plan_id')==owned['plan_id']:
            proof.update(policy_active=False,current_policy_state='undone');store('acceptance.json',proof)
        print('POLICY UNDONE: 仅撤销本次自有策略，原网络保留',flush=True)
    if restore_queues:
        for node,repair in list(s['repairs'].items()):
            check_container(repair);container=repair['container'];remote=tool_dir(s)
            actual=probe.inside(container,['cat','/queue-probe.sh'])
            if actual!=wrapper(remote):raise ValueError(f'{node}采集入口被外部改变，拒绝覆盖')
            if repair['had_original']:
                original=probe.inside(container,['cat',repair['backup']])
                if hashlib.sha256(original.encode()).hexdigest()!=repair['sha256']:
                    raise ValueError('原采集器备份SHA256不匹配，拒绝恢复')
                probe.inside(container,['sh','-c','cp -p "$1" /queue-probe.sh.mini-tmp; mv /queue-probe.sh.mini-tmp /queue-probe.sh',
                                        'mini-restore',repair['backup']])
            else:probe.inside(container,['rm','-f','/queue-probe.sh'])
            del s['repairs'][node];save(s)
            print('COLLECTOR RESTORED:',node,flush=True)
        print('队列根配置保留；不自动删除实验限速/时延，也不修改原auto',flush=True)


def doctor():
    s=state();out=ROOT/'doctor'/str(int(time.time()));out.mkdir(parents=True)
    result=probe.call(['docker','version'],check=False)
    (out/'docker-version.txt').write_text(result.stdout+result.stderr)
    for node,info in s['nodes'].items():
        dest=out/node;dest.mkdir()
        for label,cmd in [('lsdb',['cat',info['lsdb_path']]),('cfg',['cat','/cfg.sh']),
                          ('addresses',['ip','-j','-6','addr','show']),
                          ('routes',['ip','-6','route','show','table','all']),
                          ('isis',['vtysh','-c','show isis database detail json']),
                          ('qdisc',['tc','-j','-s','qdisc','show']),
                          ('auto-processes',['ps','-ef']),('auto-log',['tail','-n','100','/auto-debug.log']),
                          ('ack',['cat','/path-tuner.status'])]:
            run=probe.call(['docker','exec',info['container']]+cmd,check=False)
            (dest/(label+'.txt')).write_text(run.stdout+run.stderr)
    print('DOCTOR:',out,'；完整状态/方案/数据在/data，可从宿主docker cp导出',flush=True)


def status():
    s=state();proof=load(ROOT/'acceptance.json');error=load(ROOT/'last-error.json')
    print(json.dumps({'stage':s['stage'],'selected_nodes':s['order'],'queue_source':s.get('queue_source'),
          'algorithm_ok':s.get('algorithm_ok',False),'policy_mode':s.get('policy',{}).get('mode'),
          'acceptance_is_historical':not(s.get('policy') and proof and proof.get('plan_id')==s.get('plan_id') and s['stage']=='verified'),
          'last_verified_at':s.get('last_verified_at'),'latest_acceptance':proof,
          'last_error':error,
          'last_error_is_historical':bool(error and float(s.get('last_verified_at') or 0)>float(error.get('time') or 0))},ensure_ascii=False,indent=2))


def resolve(s,value):
    aliases=dict(zip('ABC',s['order']))
    hits=[n for n,i in s['nodes'].items() if value in (n,i['container'])]
    node=aliases.get(value) or (hits[0] if len(hits)==1 else None)
    if not node:raise ValueError('可用A/B/C、逻辑ID或实际容器名；先mini topo查看')
    return node


def main():
    parser=argparse.ArgumentParser(description='独立2/3节点最小验收。所有mini命令在测试容器内部执行。')
    sub=parser.add_subparsers(dest='action')
    for action in ('help','serve','topo','status','verify','doctor'):sub.add_parser(action)
    p=sub.add_parser('scan');p.add_argument('--prefix');p.add_argument('--lsdb-path')
    p=sub.add_parser('pick');p.add_argument('--count',type=int,choices=[2,3],default=3);p.add_argument('--nodes',nargs='+');p.add_argument('--prefix')
    p=sub.add_parser('ping');p.add_argument('--count',type=int,default=2);p.add_argument('--size',type=int,default=56)
    p=sub.add_parser('queues');p.add_argument('--direct',action='store_true')
    p=sub.add_parser('fix-queues');p.add_argument('--rate',default='16mbit');p.add_argument('--delay',default='10ms')
    for action in ('plan','apply','run'):
        p=sub.add_parser(action);p.add_argument('--direct-queues',action='store_true');p.add_argument('--rate-kbps',type=float,default=1000)
        if action in ('apply','run'):p.add_argument('--kernel',action='store_true');p.add_argument('--ack-timeout',type=int,default=60)
        if action=='run':p.add_argument('--count',type=int,choices=[2,3]);p.add_argument('--repair',action='store_true')
    p=sub.add_parser('undo');p.add_argument('--restore-queues',action='store_true')
    p=sub.add_parser('ip');p.add_argument('node')
    p=sub.add_parser('node');p.add_argument('node');p.add_argument('command',nargs=argparse.REMAINDER)
    args=parser.parse_args();ROOT.mkdir(parents=True,exist_ok=True)
    if args.action in (None,'help'):parser.print_help();return
    if args.action=='serve':
        print('MINI controller ready. docker exec -it leo-te-mini bash，然后mini help。不会创建业务拓扑。',flush=True)
        signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
        while True:time.sleep(30)
    if args.action=='node':
        s,_=selected(False);node=resolve(s,args.node)
        cmd=args.command[1:] if args.command[:1]==['--'] else args.command
        if not cmd:raise ValueError('示例：mini node A -- ip -6 route show')
        tty=sys.stdin.isatty() and cmd[0] in ('sh','bash')
        raise SystemExit(subprocess.call(['docker','exec','-it' if tty else '-i',s['nodes'][node]['container']]+cmd))
    if args.action=='ip':
        s,_=selected(False);print(s['nodes'][resolve(s,args.node)]['loopback']);return
    mutating=args.action in ('pick','fix-queues','plan','apply','verify','run','undo') or (
        args.action=='scan' and (args.prefix is not None or args.lsdb_path is not None))
    @contextmanager
    def no_lock():yield
    with (lock() if mutating else no_lock()):
        if hasattr(args,'rate_kbps') and not (0<args.rate_kbps<=1e8):raise ValueError('rate-kbps须为正数（单位kbps）')
        if hasattr(args,'ack_timeout') and not 5<=args.ack_timeout<=180:raise ValueError('ack-timeout=5..180秒')
        if args.action=='scan':
            if args.lsdb_path:
                if not args.lsdb_path.startswith('/'):raise ValueError('lsdb-path须为容器内绝对路径')
                config=settings();config['lsdb_path']=args.lsdb_path;store('settings.json',config)
            discovery(args.prefix);return
        if args.action=='pick':pick(args.count,args.nodes,args.prefix);return
        if args.action=='topo':s,g=selected();show_topology(s,g);return
        if args.action=='ping':
            if not 1<=args.count<=100 or not 0<=args.size<=1200:raise ValueError('count=1..100, size=0..1200')
            ping(args.count,args.size);return
        if args.action=='queues':s,g=selected();queue_rows(s,g,args.direct);return
        if args.action=='fix-queues':fix_queues(args.rate,args.delay);return
        if args.action=='plan':plan(args.direct_queues,args.rate_kbps);return
        if args.action=='apply':apply(args.kernel,args.direct_queues,args.ack_timeout,args.rate_kbps);return
        if args.action=='verify':verify();return
        if args.action=='undo':undo(args.restore_queues);return
        if args.action=='doctor':doctor();return
        if args.action=='status':status();return
        if args.action=='run':
            s=state()
            if s.get('policy'):raise ValueError('上次测试已有下发记录，先mini undo后再run')
            if not s['order'] or (args.count is not None and len(s['order'])!=args.count):pick(args.count or 3)
            s,g=selected()
            try:queue_rows(s,g,args.direct_queues)
            except ValueError:
                if not args.repair:raise
                fix_queues()
            apply(args.kernel,args.direct_queues,args.ack_timeout,args.rate_kbps);return
        raise ValueError('未处理命令，拒绝静默成功')


def ping(count=2,size=56):
    s,_=selected()
    results=probe.ping_rows({n:s['nodes'][n] for n in s['order']},count,size)
    store('ping.json',results)
    for row in results:
        print(f'[{row["src"]} -> {row["dst"]}]',row['output'],flush=True)
    if any(row['returncode'] for row in results):raise ValueError('部分Ping失败，原始输出见/data/ping.json')
    print('PING PASS: 所选节点实际相互可达',flush=True)


def tool_dir(s):return '/tmp/leo-mini-tools-'+s['token']


def install_tool(s,node):
    info=s['nodes'][node];container=info['container'];remote=tool_dir(s)
    probe.inside(container,['mkdir','-p',remote])
    probe.call(['docker','cp','/app/bin/leo-te-queue-probe',container+':'+remote+'/probe'])
    probe.inside(container,['sh','-c','cat > "$1"; chmod 755 "$2"','mini-config',remote+'/cfg.json',remote+'/probe'],
                 text=json.dumps(info['config']))
    return remote


def queue_rows(s,graph,direct=False):
    output=[];errors={};details={}
    for node in s['order']:
        info=s['nodes'][node];container=info['container']
        try:
            if direct:
                remote=install_tool(s,node)
                text=probe.inside(container,['env','QPROBE_CFG='+remote+'/cfg.json',remote+'/probe','--once'])
                document={'queue_poll_s':7,'queues':json.loads(text)}
            else:
                text=probe.inside(container,['tail','-c','16384',info['lsdb_path']])
                document=decode_tail(text)
            native=document['queues']
            if native.get('node')!=node:raise ValueError(f'队列自称{native.get("node")}，实际身份为{node}')
            links=native.get('links',[])
            if native.get('links_total')!=len(links):raise ValueError('原队列links_total与实际条目数不一致')
            scoped=[q for q in links if q.get('src')==node and graph.has_edge(node,q.get('dst'))]
            view=dict(document,queues=dict(native,links=scoped,links_total=len(scoped)))
            rows=validate_native_queues(view,node)
            for q in rows:
                if q['interface']!=graph[node][q['dst']]['interface']:
                    raise ValueError(f'{node}->{q["dst"]}的队列接口与真实拓扑接口不一致')
                if any(q.get(k) is None for k in ('queue_bytes','capacity_mbps','delay_ms')):
                    raise ValueError(f'{node}->{q["dst"]}缺测：{q}')
                q['input_mode']='direct-tc' if direct else 'native-lsdb'
                if direct:q['source']=f'direct-tc:{container}:{q["interface"]}'
            expected={(node,d) for d in graph.successors(node)}
            if {(q['src'],q['dst']) for q in rows}!=expected:raise ValueError('所选出方向队列不齐')
            details[node]={'native_total':len(links),'used_rows':rows,'raw_queue_document':document}
            output+=rows
        except Exception as exc:errors[node]=str(exc)
    store('queues.json',{'source':'direct-tc' if direct else 'native-lsdb','links':output,'nodes':details,'errors':errors})
    if errors:
        for node,error in errors.items():print('QUEUE NOT READY:',node,error,flush=True)
        raise ValueError('所选链路队列未通过；先mini fix-queues，或明确使用--direct排查。缺失不会填0')
    for q in output:
        print(f'{q["src"]}->{q["dst"]} {q["interface"]} queue={q["queue_bytes"]:g}B '
              f'capacity={q["capacity_mbps"]:g}Mbps delay={q["delay_ms"]:g}ms age={q["age_seconds"]:.2f}s',flush=True)
    print('DIRECT QUEUE PASS (未验LSDB队列写回)' if direct else 'LSDB QUEUE PASS: 真实新鲜采样，0为合法实测空队列',flush=True)
    return output


def wrapper(remote):return f'#!/bin/bash\nexport QPROBE_CFG={remote}/cfg.json\nexec {remote}/probe "$@"\n'


def fix_queues(rate='16mbit',delay='10ms'):
    if not re.fullmatch(r'[0-9.]+[kKmMgG]?bit',rate) or not re.fullmatch(r'[0-9.]+(ms|us|s)',delay):
        raise ValueError('显式参数示例：--rate16mbit --delay10ms（参数和值之间需空格）')
    s,graph=selected()
    if s.get('policy'):raise ValueError('先mini undo，再改测试队列配置')
    actions=[]
    for node in s['order']:
        info=s['nodes'][node];container=info['container']
        for _,dst,edge in graph.out_edges(node,data=True):
            iface=edge['interface']
            q=json.loads(probe.inside(container,['tc','-j','qdisc','show','dev',iface]))
            roots=[r for r in q if r.get('root') and r['kind']!='noqueue']
            if roots:
                actions.append({'node':node,'interface':iface,'action':'preserved-existing-root'})
            else:
                probe.inside(container,['tc','qdisc','add','dev',iface,'root','handle','1:',
                                        'tbf','rate',rate,'burst','32kb','latency','100ms'])
                actions.append({'node':node,'interface':iface,'action':'created-root','rate':rate,'delay':delay})
                store('qdisc-actions.json',actions)
                probe.inside(container,['tc','qdisc','add','dev',iface,'parent','1:1','handle','10:',
                                        'netem','delay',delay,'limit','1000'])
        remote=install_tool(s,node)
        backup='/queue-probe.sh.before-leo-mini-'+s['token']
        if node not in s['repairs']:
            before=probe.call(['docker','exec',container,'cat','/queue-probe.sh'],check=False)
            s['repairs'][node]={'container':container,'container_id':info['container_id'],
                               'backup':backup,'had_original':before.returncode==0,
                               'sha256':hashlib.sha256(before.stdout.encode()).hexdigest() if before.returncode==0 else None,
                               'installed':False}
            save(s)
        repair=s['repairs'][node]
        probe.inside(container,['sh','-c',
            'set -eu; if [ -f /queue-probe.sh ] && [ ! -e "$1" ]; then cp -p /queue-probe.sh "$1"; fi; '
            'cat > /queue-probe.sh.mini-tmp; chmod 755 /queue-probe.sh.mini-tmp; mv /queue-probe.sh.mini-tmp /queue-probe.sh',
            'mini-install',backup],text=wrapper(remote))
        repair['installed']=True;repair['installed_at']=time.time();save(s)
        print('PREPARED:',node,container,'原auto继续作为唯一LSDB写入者',flush=True)
    store('qdisc-actions.json',actions)
    deadline=time.time()+45
    while True:
        try:
            rows=queue_rows(s,graph)
            if any('leo-te-backup-tc-json' not in row['source'] or
                   row['timestamp'] < s['repairs'][row['src']]['installed_at']+1 for row in rows):
                raise ValueError('等待原auto发布本次安装后的备用采样')
            print('REPAIR PASS: 原auto已把备用采集器的新鲜样本写回LSDB',flush=True)
            return
        except ValueError:
            if time.time()>=deadline:raise ValueError('原auto尚未维护有效队列；mini doctor检查，或mini queues --direct明确读内核')
            time.sleep(3)


if __name__=='__main__':
    try:main()
    except (Exception,KeyboardInterrupt) as exc:
        try:store('last-error.json',{'time':time.time(),'error':str(exc)})
        except Exception:pass
        print('ERROR:',exc,'\n下一步 mini doctor；不要把降级方式当作原接口完整通过。',file=sys.stderr,flush=True)
        raise SystemExit(1)

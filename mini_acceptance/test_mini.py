import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import networkx as nx

from mini_acceptance import cli, probe


def fixture():
    nodes={}
    for col in (1,2,3):
        node=f'L01{col:02d}'
        nodes[node]={'node':node,'container':'site-'+node,'container_id':'id-'+node,
                     'loopback':f'fc00:0:1::{col}','sid':f'fc00:1:{col}::',
                     'end_route':{'encap':'seg6local','action':'End'},'peers':{},
                     'config':{'name':node,'ports':{}},'lsdb_path':'/lsdb.json','full_lsdb_parsed':True}
    for src,dst,iface in [('L0101','L0102','eth7'),('L0102','L0101','eth8'),
                          ('L0102','L0103','eth9'),('L0103','L0102','eth6')]:
        nodes[src]['peers'][dst]={'interface':iface,'source':'lsdb.link_addr_pair'}
    return nodes


class MiniTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.p=patch.object(cli,'ROOT',self.root);self.p.start()
        self.nodes=fixture();self.graph=probe.observed_graph(self.nodes)
        self.s={'token':'a'*32,'stage':'selected','order':list(self.nodes),'nodes':self.nodes,'repairs':{}}
        cli.save(self.s)

    def tearDown(self):self.p.stop();self.tmp.cleanup()

    def test_graph_requires_two_explicit_directions(self):
        nodes=copy.deepcopy(self.nodes);nodes['L0102']['peers'].pop('L0101')
        graph=probe.observed_graph(nodes)
        self.assertFalse(graph.has_edge('L0101','L0102'))
        self.assertFalse(graph.has_edge('L0102','L0101'))

    def test_small_real_pair_does_not_need_66_nodes(self):
        graph=self.graph.subgraph(['L0101','L0102'])
        self.assertEqual(probe.select_groups(graph,3),[['L0101','L0102']])

    def test_self_loop_cannot_count_as_a_third_node(self):
        graph=self.graph.copy();graph.add_edge('L0102','L0102')
        self.assertTrue(all(len(set(group))==len(group) for group in probe.select_groups(graph,3)))

    def test_manual_nonadjacent_endpoints_are_not_a_fake_edge(self):
        with self.assertRaisesRegex(ValueError,'reciprocal'):
            probe.select_groups(self.graph,2,['L0101','L0103'])

    def test_native_local_pairs_work_without_areas_parser(self):
        interfaces=[{'ifname':'lo','addr_info':[{'local':'fc00:0:1::1','prefixlen':128}]},
                    {'ifname':'port7','address':'aa:bb:cc:00:00:01','flags':['LOWER_UP']}]
        document={'self':'L0101','link_addr_pair':{'L0101->L0102':{'iface':'port7'}},'lsdb':{'error':'unknown shape'}}
        routes=[{'dst':'fc00:1:1::/48','encap':'seg6local','action':'End'}]
        for raw in [{'error':'unknown shape'}, {'areas':[{'levels':[{'id':2,'lsps':[{'srv6Locator':None}]}]}]}]:
            document['lsdb']=raw
            info=probe.normalize({},document,interfaces,routes)
            self.assertFalse(info['full_lsdb_parsed'])
            self.assertEqual(info['peers']['L0102']['source'],'lsdb.link_addr_pair')
            self.assertTrue(info['end_route'])

    def test_cfg_alone_does_not_claim_lsdb_topology_read(self):
        interfaces=[{'ifname':'lo','addr_info':[{'local':'fc00:0:1::1','prefixlen':128}]},
                    {'ifname':'eth7','address':'aa','flags':['LOWER_UP']}]
        cfg={'name':'L0101','ports':{'aa':{'eth':'eth7','linkid':['L0101L0102']}}}
        info=probe.normalize(cfg,{},interfaces,[{'dst':'fc00:1:1::/48','encap':'seg6local','action':'End'}])
        self.assertEqual(info['peers'],{})

    def test_wrong_kernel_end_prefix_is_not_ready(self):
        self.assertIsNone(probe.end_route([{'dst':'fc00:1:2::/48','encap':'seg6local','action':'End'}],'fc00:1:1::'))

    def document(self,node,stale=False,extra_bad=False):
        links=[{'src':node,'dst':peer,'interface':p['interface'],'queue_bytes':0,'measured':True,
                'capacity_mbps':16,'delay_ms':10,'carrier':'up','source':'real-native'} for peer,p in self.nodes[node]['peers'].items()]
        if extra_bad:links.append({'src':node,'dst':'L0611','interface':'unused','measured':True,'queue_bytes':None})
        return {'queue_poll_s':7,'queues':{'node':node,'timestamp':time.time()-(100 if stale else 0),'links':links,'links_total':len(links)}}

    def queue_input(self,args,*unused,**kwargs):
        container=args[0] if isinstance(args,str) else ''
        return container

    def test_queue_scope_ignores_other_63_nodes_and_does_not_overwrite_lease(self):
        docs={n:self.document(n,extra_bad=True) for n in self.nodes}
        def inside(container,args,**kwargs):return json.dumps(docs[container.removeprefix('site-')])
        with patch.object(probe,'inside',side_effect=inside),patch.object(cli,'save',side_effect=AssertionError('read-only must not save lease')):
            rows=cli.queue_rows(self.s,self.graph)
        self.assertEqual(len(rows),4)
        self.assertTrue(all(r['queue_bytes']==0 for r in rows))

    def test_stale_queue_not_converted_to_zero(self):
        docs={n:self.document(n,stale=True) for n in self.nodes}
        with patch.object(probe,'inside',side_effect=lambda c,a,**kw:json.dumps(docs[c.removeprefix('site-')])):
            with self.assertRaisesRegex(ValueError,'队列未通过'):
                cli.queue_rows(self.s,self.graph)

    def test_direct_queue_records_do_not_claim_lsdb_origin(self):
        docs={n:self.document(n)['queues'] for n in self.nodes}
        with patch.object(cli,'install_tool',return_value='/tmp/tools'),patch.object(probe,'inside',side_effect=lambda c,a,**kw:json.dumps(docs[c.removeprefix('site-')])):
            rows=cli.queue_rows(self.s,self.graph,direct=True)
        self.assertTrue(all(row['source'].startswith('direct-tc:') for row in rows))

    def test_solver_runs_real_project_function_without_redis_daemon(self):
        rows=[]
        for u,v,a in self.graph.edges(data=True):
            rows.append({'src':u,'dst':v,'interface':a['interface'],'queue_bytes':0,'capacity_mbps':16,'delay_ms':10})
        with patch.object(cli,'selected',return_value=(self.s,self.graph)),patch.object(cli,'queue_rows',return_value=rows):
            plan=cli.plan(rate_kbps=1000)
        self.assertEqual(plan['path'],['L0101','L0102','L0103'])
        self.assertEqual(plan['algorithm'],'project LyapunovSolver.solve_lyapunov_mcf')

    def test_installer_stdin_reaches_docker_exec(self):
        with patch.object(probe,'call',return_value='') as call:
            probe.inside('actual-name',['sh','-c','cat > /tmp/test'],text='contents')
        self.assertEqual(call.call_args.args[0][:4],['docker','exec','-i','actual-name'])
        self.assertEqual(call.call_args.kwargs['text'],'contents')

    def plan_file(self):
        data={'policy':{'src':'L0101','dst':'L0103','via':['L0102','L0103'],'biz_id':'mine'}}
        cli.store('plans/test.json',data)
        return data

    def test_direct_fallback_has_no_native_ack_or_lsdb_pass(self):
        self.s['policy']={'src':'L0101','dst':'L0103','mode':'kernel','queue_mode':'direct-tc','plan_id':'test'}
        self.plan_file()
        route='fc00:0:1::3 encap seg6 mode encap segs 2 [ fc00:1:2:: fc00:1:3:: ] dev eth7'
        with patch.object(cli,'selected',return_value=(self.s,self.graph)),patch.object(cli,'queue_rows'),patch.object(probe,'inside',return_value=route),patch.object(probe,'ping_rows',return_value=[{'returncode':0}]):
            proof=cli.verify()
        self.assertEqual(proof['status'],'MINI EXPLICIT FALLBACK PASS')
        self.assertIsNone(proof['native_ack'])
        self.assertFalse(proof['lsdb_queues'])

    def test_existing_seg6_with_wrong_sids_is_not_pass(self):
        self.s['policy']={'src':'L0101','dst':'L0103','mode':'kernel','queue_mode':'native-lsdb','plan_id':'test'}
        self.plan_file()
        with patch.object(cli,'selected',return_value=(self.s,self.graph)),patch.object(cli,'queue_rows'),patch.object(probe,'inside',return_value='encap seg6 mode encap segs 1 [ fc00:1:6:: ] dev eth7'):
            with self.assertRaisesRegex(ValueError,'SID顺序'):
                cli.verify()

    def test_foreign_policy_is_not_overwritten(self):
        data={'policy':{'src':'L0101','dst':'L0103'}}
        def inside(container,args,**kwargs):
            return '/path.cmd/foreign.json' if args[0]=='sh' else json.dumps({'src':'L0101','dst':'L0103','biz_id':'other-project'})
        with patch.object(probe,'inside',side_effect=inside):
            with self.assertRaisesRegex(ValueError,'不.*覆盖其他策略'):
                cli.conflicts(self.s,data)

    def test_stale_native_ack_is_rejected(self):
        self.s['policy']={'src':'L0101','biz_id':'mine','submitted_at':1000}
        ack={'mine':{'status':'ok','route':'seg6','ping':'pass','installed_at':999}}
        with patch.object(probe,'inside',return_value=json.dumps(ack)),patch.object(cli.time,'time',side_effect=[1000,1001]):
            with self.assertRaisesRegex(ValueError,'原执行器未'):
                cli.native_wait(self.s,.01)

    def test_changed_direct_route_is_not_deleted(self):
        self.s['policy']={'src':'L0101','dst':'L0103','mode':'kernel','plan_id':'test','table':254,'metric':1,'dev':'eth7'}
        cli.save(self.s);self.plan_file()
        route='fc00:0:1::3 encap seg6 mode encap segs 1 [ fc00:1:6:: ] dev eth7 proto 196 metric 1'
        with patch.object(cli,'check_container'),patch.object(probe,'inside',return_value=route) as inside:
            with self.assertRaisesRegex(ValueError,'外部修改'):
                cli.undo()
        self.assertFalse(any('del' in c.args[1] for c in inside.call_args_list))

    def test_all_public_dispatches_reach_handlers(self):
        handlers={'scan':'discovery','pick':'pick','ping':'ping','fix-queues':'fix_queues',
                  'plan':'plan','apply':'apply','verify':'verify','undo':'undo','doctor':'doctor','status':'status'}
        for action,name in handlers.items():
            with self.subTest(action=action),patch('sys.argv',['mini',action]),patch.object(cli,name) as handler:
                cli.main();handler.assert_called_once()

    def test_run_explicit_count_is_not_ignored(self):
        with patch('sys.argv',['mini','run','--count','2']),patch.object(cli,'pick') as pick,patch.object(cli,'selected',return_value=(self.s,self.graph)),patch.object(cli,'queue_rows'),patch.object(cli,'apply'):
            cli.main()
        pick.assert_called_once_with(2)

    def test_run_without_count_keeps_manual_selection(self):
        self.s['order']=['L0101','L0102'];self.s['nodes']={n:self.nodes[n] for n in self.s['order']};cli.save(self.s)
        with patch('sys.argv',['mini','run']),patch.object(cli,'pick') as pick,patch.object(cli,'selected',return_value=(self.s,self.graph)),patch.object(cli,'queue_rows'),patch.object(cli,'apply'):
            cli.main()
        pick.assert_not_called()

    def test_status_reports_error_before_first_verification(self):
        self.s['last_verified_at']=None;cli.save(self.s)
        cli.store('last-error.json',{'time':time.time(),'error':'missing queues'})
        output=io.StringIO()
        with redirect_stdout(output):cli.status()
        self.assertFalse(json.loads(output.getvalue())['last_error_is_historical'])


if __name__=='__main__':unittest.main()

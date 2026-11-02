import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import networkx as nx

from integration import field
from integration.controller import live_document
from integration.lsdb import parse_lsdb


FIXTURE=Path(__file__).resolve().parents[1]/'integration/fixtures/weixing1020-lsdb.json'


class FieldTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.root_patch=patch.object(field,'ROOT',self.root);self.root_patch.start()
        self.document=json.loads(FIXTURE.read_text())

    def tearDown(self):
        self.root_patch.stop();self.temp.cleanup()

    def test_singular_area_and_default_wrapper_preserve_exact_advertisements(self):
        expected,_=parse_lsdb(self.document)
        wrapped=copy.deepcopy(self.document)
        wrapped['lsdb']={'default':{'area':wrapped['lsdb']['areas']}}
        actual,_=parse_lsdb(wrapped)
        self.assertEqual(set(expected.edges),set(actual.edges))
        self.assertEqual(dict(expected.nodes(data=True)),dict(actual.nodes(data=True)))

    def test_unsupported_area_shape_explains_actual_keys(self):
        with self.assertRaisesRegex(ValueError,'actual keys=.*error'):
            parse_lsdb({'lsdb':{'error':'ISIS not running'}})

    def test_multiple_vrfs_are_not_merged(self):
        areas=self.document['lsdb']['areas']
        with self.assertRaisesRegex(ValueError,'Multiple FRR'):
            parse_lsdb({'lsdb':{'red':{'areas':areas},'blue':{'areas':areas}}})

    def test_fresh_native_export_can_replace_unsupported_vty_readback(self):
        self.document.update(queue_poll_s=7,queues={'timestamp':time.time()})
        with patch('integration.controller.docker_read',return_value=self.document),patch('integration.controller.command',return_value='{}'):
            result=live_document('arbitrary-container')
        self.assertEqual(result['topology_readback'],'fresh-native-lsdb')

    def test_stale_native_file_cannot_hide_live_frr_failure(self):
        self.document.update(queue_poll_s=7,queues={'timestamp':time.time()-100})
        with patch('integration.controller.docker_read',return_value=self.document),patch('integration.controller.command',return_value='{}'):
            with self.assertRaisesRegex(ValueError,'not a fresh'):
                live_document('arbitrary-container')

    def discovery(self,clones=False,healthy_clone=False):
        names=['site-satellite-one','site-satellite-two']+(['old-snapshot'] if clones else [])
        configs={name:{'name':'L0101' if name!='site-satellite-two' else 'L0102',
                       'ports':{'aa:00:00:00:00:01':{'eth':'eth9','linkid':['L0101L0102']}}} for name in names}
        def command(args):
            if args[:2]==['docker','ps']:return '\n'.join(names)
            if args[-1]=='/run/leo-te-role':raise RuntimeError('no marker')
            if args[-3:]==['-6','addr','show']:
                active=args[2]!='old-snapshot' or healthy_clone
                return json.dumps([{'ifname':'eth9','address':'aa:00:00:00:00:01','flags':['LOWER_UP'] if active else []}])
            raise AssertionError(args)
        def read(container,path):return configs[container]
        return patch.object(field,'command',side_effect=command),patch.object(field,'docker_read',side_effect=read)

    def test_two_arbitrarily_named_nodes_are_enough(self):
        a,b=self.discovery()
        with a,b:result=field.candidates()
        self.assertEqual(set(result),{'L0101','L0102'})
        self.assertEqual(result['L0101']['container'],'site-satellite-one')

    def test_inactive_snapshot_does_not_duplicate_claim(self):
        a,b=self.discovery(clones=True)
        with a,b:result=field.candidates()
        self.assertEqual(len(result),2)
        self.assertIn('old-snapshot',json.loads((self.root/'discovery.json').read_text())['ignored'])

    def test_live_identity_and_explicit_pairs_override_inherited_cfg(self):
        cfg={'name':'L0101','ports':{'stale':{'eth':'eth1','linkid':['L0101L0102']}}}
        document={'self':'L0305','link_addr_pair':{'L0305->L0306':{'iface':'port7'}}}
        links=[{'ifname':'lo','addr_info':[{'local':'fc00:0:3::5','prefixlen':128}]},
               {'ifname':'port7','address':'aa:bb:cc:dd:ee:ff','flags':['LOWER_UP']}]
        actual,source=field.canonical_config(cfg,document,links)
        self.assertEqual(actual['name'],'L0305')
        self.assertEqual(actual['ports']['aa:bb:cc:dd:ee:ff']['linkid'],['L0305L0306'])
        self.assertEqual(cfg['name'],'L0101')
        self.assertIn('native-lsdb',source)

    def test_two_real_networks_require_explicit_selection(self):
        a,b=self.discovery(clones=True,healthy_clone=True)
        with a,b:
            with self.assertRaisesRegex(ValueError,'site-satellite-one.*old-snapshot'):
                field.candidates()

    def test_partial_network_uses_only_live_advertised_selected_edges(self):
        graph=nx.DiGraph()
        for node in ['L0101','L0102','L0103']:
            graph.add_node(node,srv6_ready=True,end_sid='fc00:1:1::',loopback='fc00:0:1::1')
        graph.add_edges_from([('L0101','L0102'),('L0102','L0101'),('L0102','L0103')])
        nodes={n:{'container':'site-'+n} for n in ['L0101','L0102']}
        with patch.object(field,'live_document',return_value={}),patch.object(field,'parse_lsdb',return_value=(graph,[])):
            usable,actual,_=field.state(nodes,metrics=False)
        self.assertEqual(set(actual.edges),{('L0101','L0102'),('L0102','L0101')})
        self.assertEqual(len(usable),2)

    def test_kernel_encap_with_wrong_sid_is_not_accepted(self):
        output=self.root/'plan';output.mkdir()
        (output/'plan.json').write_text(json.dumps({'policies':[{'biz_id':'leo_te_demo','src':'L0101','dst':'L0102','via':['L0102']}]}))
        nodes={'L0101':{'container':'site-source'}}
        with patch.object(field,'command',return_value='encap seg6 mode encap segs 1 [ fc00:1:3:: ] dev eth9'):
            with self.assertRaisesRegex(ValueError,'differs'):
                field.verify_plan_routes(nodes,output)
        self.assertTrue((output/'route-mismatch.json').is_file())

    def test_kernel_sid_normalization_accepts_same_address(self):
        output=self.root/'plan';output.mkdir()
        (output/'plan.json').write_text(json.dumps({'policies':[{'biz_id':'leo_te_demo','src':'L0101','dst':'L0102','via':['L0102']}]}))
        nodes={'L0101':{'container':'site-source'}}
        with patch.object(field,'command',return_value='encap seg6 mode encap segs 1 [ fc00:0001:0002:: ] dev eth9'):
            self.assertTrue(field.verify_plan_routes(nodes,output)['leo_te_demo']['matched'])

    def test_stale_probe_cannot_claim_continuous_traffic(self):
        stale={'finished':False,'sampled_at':time.time()-30}
        with patch.object(field,'probe_records',return_value=({'a':stale},{'a':stale})):
            with self.assertRaisesRegex(ValueError,'stopped updating'):
                field.assert_traffic({})

    def test_cleanup_rejects_unowned_path(self):
        with patch.object(field.subprocess,'run') as run:
            with self.assertRaisesRegex(ValueError,'invalid session path'):
                field.stop_probes({'remote':'/tmp','records':[]})
            run.assert_not_called()

    def test_demo_on_small_network_only_uses_available_endpoints(self):
        graph=nx.DiGraph([('L0101','L0102'),('L0102','L0101')])
        flows=field.demands_for(graph)
        self.assertEqual(len(flows),1)
        self.assertTrue(all(f['src'] in graph and f['dst'] in graph for f in flows))

    def test_backup_installer_forwards_stdin_and_preserves_existing_root(self):
        nodes={n:{'container':'site-'+n,'config':{'name':n,'ports':{'aa':{'eth':'eth7','linkid':['L0101L0102']}}}} for n in ['L0101','L0102']}
        calls=[];scripts=[]
        def command(args,stdin=None):
            calls.append(args)
            if stdin is not None:
                self.assertIn('-i',args,'docker exec must forward script/config stdin')
                scripts.append(stdin)
            if 'qdisc' in args:return '[{"root":true,"kind":"tbf"}]'
            return ''
        records=[{'source':'lsdb.queues:leo-te-backup-tc-json'}]
        with patch.object(field,'candidates',return_value=nodes),patch.object(field,'command',side_effect=command),patch.object(field,'state',return_value=(nodes,nx.DiGraph(),records)):
            field.queue_prepare(SimpleNamespace(rate='16mbit',delay='10ms'))
        self.assertEqual(sum(s.startswith('#!/bin/bash') for s in scripts),2)
        self.assertFalse(any('qdisc' in args and 'add' in args for args in calls))
        self.assertTrue(all('auto66-lite.sh' not in ' '.join(args) for args in calls))

    def test_compare_cli_dispatches_and_does_not_silently_succeed(self):
        with patch('sys.argv',['leo-te','compare']),patch.object(field,'compare') as handler:
            field.main()
        handler.assert_called_once_with()


if __name__=='__main__':unittest.main()

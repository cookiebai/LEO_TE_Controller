import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from integration.lsdb import attach_metrics, parse_lsdb, port_to, qdisc_metrics
from integration.controller import make_native_policy, validate_demands, install_commands
from integration.controller import collect_metrics
from python_te_solver.lyapunov_solver import LyapunovSolver
import networkx as nx


FIXTURE = Path(__file__).resolve().parents[1] / 'integration/fixtures/weixing1020-lsdb.json'


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.document = json.loads(FIXTURE.read_text())
        self.graph, self.warnings = parse_lsdb(self.document)

    def test_supplied_image_snapshot(self):
        self.assertEqual(len(self.graph), 49)
        self.assertEqual(self.graph.number_of_edges(), 129)
        self.assertEqual(sum(d['srv6_ready'] for _, d in self.graph.nodes(data=True)), 46)
        self.assertEqual(self.graph.nodes['L0111']['end_sid'], 'fc00:1:b::')
        self.assertFalse(self.graph.nodes['L0305']['srv6_ready'])
        self.assertEqual(self.warnings, [])

    def test_metric_is_not_mistaken_for_delay_capacity_or_queue(self):
        link = self.graph['L0101']['L0102']
        self.assertEqual(link['igp_metric'], 10)
        self.assertFalse({'delay', 'capacity', 'queue'} & link.keys())

    def test_does_not_fabricate_reverse_edges(self):
        lsps = self.document['lsdb']['areas'][0]['levels'][0]['lsps']
        first, second = copy.deepcopy(lsps[:2])
        first['extReach'] = first['extReach'][:1]
        second['extReach'] = []
        self.document['lsdb']['areas'][0]['levels'][0]['lsps'] = [first, second]
        graph, _ = parse_lsdb(self.document)
        self.assertEqual(list(graph.edges), [('L0101', 'L0102')])

    def test_purged_lsp_not_usable(self):
        for lsp in self.document['lsdb']['areas'][0]['levels'][0]['lsps']:
            lsp['holdtime'] = 0
        with self.assertRaisesRegex(ValueError, 'no identifiable'):
            parse_lsdb(self.document)

    def test_changed_sid_rejected(self):
        first = self.document['lsdb']['areas'][0]['levels'][0]['lsps'][0]
        first['srv6Locator']['subtlvs']['srv6EndSid']['sidValue'] = 'fd00::1'
        with self.assertRaisesRegex(ValueError, 'differs'):
            parse_lsdb(self.document)

    def test_qdisc_root_and_child_are_not_double_counted(self):
        metrics = qdisc_metrics([
            {'kind': 'tbf', 'handle': '1:', 'root': True, 'backlog': 9000, 'drops': 2},
            {'kind': 'netem', 'handle': '10:', 'parent': '1:1', 'backlog': 9000}],
            'qdisc tbf 1: root rate 16Mbit\nqdisc netem 10: parent 1:1 delay 10ms')
        self.assertEqual(metrics['queue_bytes'], 9000)
        self.assertEqual(metrics['capacity_mbps'], 16)
        self.assertEqual(metrics['delay_ms'], 10)

    def test_noqueue_is_missing_not_zero(self):
        with self.assertRaisesRegex(ValueError, 'No measurable'):
            qdisc_metrics([{'kind': 'noqueue', 'root': True, 'backlog': 0}], '')

    def test_metrics_missing_fail_closed(self):
        with self.assertRaisesRegex(ValueError, 'Missing link metrics'):
            attach_metrics(self.graph, [], default_capacity=16, default_delay=10)

    def test_demo_metrics_explicit_and_negative_metrics_rejected(self):
        missing = attach_metrics(self.graph, [], allow_missing=True)
        self.assertEqual(len(missing), 129)
        with self.assertRaisesRegex(ValueError, 'Invalid queue'):
            attach_metrics(self.graph, [{'src': 'L0101', 'dst': 'L0102', 'queue_bytes': -1}],
                           default_capacity=16, default_delay=10, allow_missing=True)

    def test_cfg_port_matches_link_endpoints(self):
        cfg = {'name': 'L0101', 'ports': {'a': {'eth': 'eth1', 'veth': 'v-dst',
                                                     'linkid': ['L0101L0102']}}}
        self.assertEqual(port_to(cfg, 'L0102')['eth'], 'eth1')
        with self.assertRaises(ValueError):
            port_to(cfg, 'L0103')

    def test_native_command_exact_format_and_order(self):
        self.assertEqual(make_native_policy('f1', ['L0101', 'L0102', 'L0103'], 10),
                         {'biz_id': 'leo_te_f1', 'src': 'L0101', 'dst': 'L0103',
                          'mode': 'end', 'priority': 10, 'via': ['L0102', 'L0103']})

    def test_host_queue_fallback_uses_destination_veth(self):
        graph = nx.DiGraph([('L0101', 'L0102')])
        nodes = {
            'L0101': {'container': 'source', 'config': {'name': 'L0101', 'ports': {
                'p': {'eth': 'eth4', 'veth': 'host-toward-source', 'linkid': ['L0101L0102']}}}},
            'L0102': {'container': 'destination', 'config': {'name': 'L0102', 'ports': {
                'p': {'eth': 'eth3', 'veth': 'host-toward-destination', 'linkid': ['L0101L0102']}}}},
        }
        with patch('integration.controller.read_qdisc', side_effect=[
            ValueError('noqueue'), {'queue_bytes': 100}]) as read:
            result = collect_metrics(graph, nodes)
        self.assertEqual(read.call_args_list[0].args, ('source', 'eth4'))
        self.assertEqual(read.call_args_list[1].args, (None, 'host-toward-destination'))
        self.assertEqual(result[0]['queue_bytes'], 100)

    def test_real_solver_avoids_large_measured_queue(self):
        graph = nx.DiGraph()
        for src, dst, queue in [('L0101', 'L0102', 1000000),
                                ('L0102', 'L0104', 0),
                                ('L0101', 'L0103', 0),
                                ('L0103', 'L0104', 0)]:
            graph.add_edge(src, dst, delay=1, capacity=16, queue=queue)
        solver = LyapunovSolver(FIXTURE.parents[2] / 'config/controller.yaml')
        result = solver.solve_lyapunov_mcf(graph, [
            {'id': 'f1', 'src': 'L0101', 'dst': 'L0104',
             'class': 'class_3_background', 'demand': 1000}])
        self.assertEqual(result['f1'][0]['path'], ['L0101', 'L0103', 'L0104'])

    def test_native_tuner_grid_limit_enforced(self):
        with self.assertRaisesRegex(ValueError, '6x11'):
            make_native_policy('f1', ['L0101', 'L2520'], 10)

    def test_duplicate_destination_route_refused(self):
        flow = {'id': 'a', 'src': 'L0101', 'dst': 'L0103', 'class': 'c', 'demand': 1000}
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            validate_demands([flow, dict(flow, id='b')], self.graph, {'c': {}})

    def test_route_installed_but_ping_failed_is_not_accepted(self):
        policy = make_native_policy('f1', ['L0101', 'L0102'], 10)
        status = {'leo_te_f1': {'status': 'ok', 'route': 'seg6', 'ping': 'fail',
                              'installed_at': '2026-10-21T00:00:00+00:00'}}
        with patch('integration.controller.command', return_value=''), \
             patch('integration.controller.docker_read', return_value=status), \
             patch('integration.controller.time.time', return_value=1792540800), \
             patch('integration.controller.time.sleep'):
            with self.assertRaisesRegex(RuntimeError, 'acceptance timed out'):
                install_commands([policy], {'L0101': {'container': 'test'}}, .001)


if __name__ == '__main__':
    unittest.main()

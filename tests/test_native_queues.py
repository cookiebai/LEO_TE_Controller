import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from integration.lsdb import parse_lsdb, attach_metrics
from integration.native_queues import decode_tail, validate_native_queues
from integration.controller import discover_nodes
from integration.benchmark import comparison, summarize_phase
from integration.queue_probe import qdisc_values


VENDOR = Path(__file__).resolve().parents[1] / 'integration/vendor/queue-20261027'


class NativeQueueTests(unittest.TestCase):
    def setUp(self):
        self.doc = json.loads((VENDOR / 'lsdb_L0305.json').read_text())
        self.timestamp = self.doc['queues']['timestamp']

    def test_delivered_topology_and_queue_are_parsed_without_creating_edges(self):
        graph, warnings = parse_lsdb(self.doc)
        self.assertEqual((len(graph), graph.number_of_edges()), (66, 264))
        self.assertTrue(all(data['srv6_ready'] for _, data in graph.nodes(data=True)))
        self.assertEqual(warnings, [])
        records = validate_native_queues(self.doc, 'L0305', now=self.timestamp)
        self.assertEqual(len(records), 4)
        congested = next(r for r in records if r['dst'] == 'L0306')
        self.assertEqual(congested['queue_bytes'], 292400)
        self.assertEqual(congested['capacity_mbps'], 16)
        self.assertEqual(congested['delay_ms'], 10)

    def test_actual_16k_tail_decodes(self):
        raw = (VENDOR / 'lsdb_L0305.json').read_bytes()[-16384:].decode('utf-8', errors='replace')
        tail = decode_tail(raw)
        self.assertEqual(tail['queues'], self.doc['queues'])
        self.assertEqual(tail['queue_poll_s'], 7)

    def test_default_expiry_is_two_declared_periods(self):
        validate_native_queues(self.doc, 'L0305', now=self.timestamp + 13.9)
        with self.assertRaisesRegex(ValueError, 'stale/future'):
            validate_native_queues(self.doc, 'L0305', now=self.timestamp + 14.1)

    def test_future_data_rejected(self):
        with self.assertRaisesRegex(ValueError, 'stale/future'):
            validate_native_queues(self.doc, 'L0305', now=self.timestamp - 6)

    def test_unmeasured_is_missing_even_if_a_zero_was_reported(self):
        self.doc['queues']['links'][0].update(measured=False, queue_bytes=0)
        records = validate_native_queues(self.doc, 'L0305', now=self.timestamp)
        self.assertIsNone(records[0]['queue_bytes'])
        graph, _ = parse_lsdb(self.doc)
        with self.assertRaisesRegex(ValueError, 'Missing link metrics'):
            attach_metrics(graph, records)

    def test_measured_null_is_rejected(self):
        self.doc['queues']['links'][0].update(measured=True, queue_bytes=None)
        with self.assertRaisesRegex(ValueError, 'queue_bytes is null'):
            validate_native_queues(self.doc, 'L0305', now=self.timestamp)

    def test_another_nodes_sample_cannot_be_reused(self):
        with self.assertRaisesRegex(ValueError, 'queues belong to'):
            validate_native_queues(self.doc, 'L0306', now=self.timestamp)

    def test_only_own_four_queues_are_available_in_one_full_lsdb(self):
        graph, _ = parse_lsdb(self.doc)
        records = validate_native_queues(self.doc, 'L0305', now=self.timestamp)
        self.assertEqual({r['src'] for r in records}, {'L0305'})
        with self.assertRaisesRegex(ValueError, 'Missing link metrics'):
            attach_metrics(graph, records)

    def test_source_interface_must_match_cfg(self):
        records = self.doc['queues']['links']
        cfg = {'name': 'L0305', 'ports': {
            str(i): {'eth': r['interface'], 'linkid': [r['src'] + r['dst']]}
            for i, r in enumerate(records)}}
        validate_native_queues(self.doc, 'L0305', cfg, now=self.timestamp)
        wrong = copy.deepcopy(cfg)
        wrong['ports']['0']['eth'] = 'eth99'
        with self.assertRaisesRegex(ValueError, 'interface differs'):
            validate_native_queues(self.doc, 'L0305', wrong, now=self.timestamp)

    def test_missing_tail_fails_instead_of_using_host_veth(self):
        with self.assertRaisesRegex(ValueError, 'No native queues tail'):
            decode_tail('{"self":"L0305","lsdb":{}}')

    def test_preserved_cfg_in_controller_image_is_not_discovered_as_a_node(self):
        def run(args, **kwargs):
            if args[1] == 'ps':
                return 'controller-sidecar\n'
            return 'control\n'
        with patch('integration.controller.command', side_effect=run), \
             patch('integration.controller.docker_read') as read:
            with self.assertRaisesRegex(ValueError, 'No running task-1 nodes'):
                discover_nodes()
            read.assert_not_called()

    def test_throughput_is_measured_receiver_payload_and_keeps_negative_change(self):
        flows = [{'id': 'f', 'demand': 1000}]
        client = {'bytes_per_second': [999999, 125000, 125000, 125000], 'send_errors': 0}
        baseline = summarize_phase({'f': client}, {'f': {'bytes_per_second': [0, 100000, 100000, 100000]}},
                                   1, 3, flows)
        te = summarize_phase({'f': client}, {'f': {'bytes_per_second': [0, 90000, 90000, 90000]}},
                             1, 3, flows)
        report = comparison(baseline, te)
        self.assertTrue(report['valid'])
        self.assertAlmostEqual(baseline['received_mbps'], .8)
        self.assertAlmostEqual(report['improvement_percent'], -10)

    def test_unmatched_offered_load_invalidates_comparison(self):
        baseline = {'actual_sent_mbps': 10, 'received_mbps': 8, 'cv_percent': 1, 'offered_error_percent': 0}
        te = dict(baseline, actual_sent_mbps=8, received_mbps=9)
        self.assertFalse(comparison(baseline, te)['valid'])

    def test_probe_send_errors_are_not_hidden(self):
        client = {'bytes_per_second': [125000] * 4, 'send_errors': 1}
        with self.assertRaisesRegex(ValueError, 'send errors'):
            summarize_phase({'f': client}, {'f': client}, 1, 3, [{'id': 'f', 'demand': 1000}])

    def test_zero_baseline_is_reported_invalid_without_inventing_improvement(self):
        flow = [{'id': 'f', 'demand': 1000}]
        client = {'bytes_per_second': [125000] * 4, 'send_errors': 0}
        server = {'bytes_per_second': [0] * 4}
        baseline = summarize_phase({'f': client}, {'f': server}, 1, 3, flow)
        result = comparison(baseline, baseline)
        self.assertFalse(result['valid'])
        self.assertIsNone(result['improvement_percent'])
        self.assertIsNone(result['baseline']['cv_percent'])

    def test_tc_json_preserves_large_backlog_and_does_not_double_count_child(self):
        root = {'kind': 'tbf', 'root': True, 'handle': '1:', 'backlog': 1061390,
                'qlen': 999, 'drops': 19410, 'overlimits': 270983,
                'options': {'rate': 2000000}, 'bytes': 89310024, 'packets': 84197}
        child = {'kind': 'netem', 'parent': '1:1', 'backlog': 1061390,
                 'options': {'delay': {'delay': .01}}}
        result = qdisc_values([root, child])
        self.assertEqual(result['queue_bytes'], 1061390)
        self.assertEqual(result['qlen_packets'], 999)
        self.assertEqual(result['capacity_mbps'], 16)
        self.assertEqual(result['delay_ms'], 10)
        self.assertEqual(result['drops_total'], 19410)

    def test_tc_json_missing_qdisc_remains_unmeasured(self):
        self.assertFalse(qdisc_values([])['measured'])
        result = qdisc_values([{'kind': 'noqueue', 'root': True, 'backlog': 0}])
        self.assertFalse(result['measured'])
        self.assertIsNone(result['queue_bytes'])


if __name__ == '__main__':
    unittest.main()

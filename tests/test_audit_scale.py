import json
from pathlib import Path
import tempfile
import unittest

from scripts.audit_scale_results import audit


class ScaleAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.write('provenance.json', {'parameters': {'TE_SCALE': '25x20', 'TE_DEMAND_TOTAL_KBPS': '80000'}})
        self.write('exit_status.json', {'exit_code': 0})
        self.write('frontend_health.jsonl', {'ok': True})
        phase = {'total_cv': .01, 'actual_sent_mbps': 80, 'sent_load_ratio': 1, 'min_flow_sent_ratio': 1}
        self.data = {'status': 'completed', 'target_met': True,
                     'experiment': {'topology': {'nodes': 502, 'down_links': 0}},
                     'cycles': [{'cycle': 1,
                                 'baseline': dict(phase, total_mean_mbps=60),
                                 'te': dict(phase, total_mean_mbps=69),
                                 'policy': {'desired': 24, 'applied': 24, 'queued': 0},
                                 'ping': {'srv6_ipv4_ping_ok': 24, 'srv6_ipv6_ping_ok': 24}}]}

    def write(self, name, value):
        (self.path / name).write_text(json.dumps(value) + '\n')

    def check(self):
        self.write('result.json', self.data)
        return audit(self.path, '25x20')

    def test_recomputes_instead_of_trusting_target_flag(self):
        self.data['cycles'][0]['te']['total_mean_mbps'] = 75
        self.assertFalse(self.check()['independent_pass'])

    def test_single_pass_is_not_three_cycle_pass(self):
        row = self.check()
        self.assertTrue(row['independent_pass'])
        self.assertFalse(row['three_cycle_pass'])

    def test_missing_exit_marker_is_not_accepted(self):
        (self.path / 'exit_status.json').unlink()
        self.assertFalse(self.check()['independent_pass'])

    def test_web_failure_rejects_good_throughput(self):
        self.write('frontend_health.jsonl', {'ok': False})
        self.assertFalse(self.check()['independent_pass'])

    def test_other_scale_is_ignored(self):
        self.assertIsNone(audit(self.path, '20x25'))

    def test_fixed_neighbor_mode_requires_restoration_evidence(self):
        self.write('provenance.json', {'parameters': {'TE_SCALE': '25x20', 'TE_FIXED_NEIGHBORS': '1'}})
        self.assertFalse(self.check()['independent_pass'])
        self.write('fixed_neighbors.json', {'status': 'restored', 'pinned_unix': 123,
                                          'nodes': {str(i): [] for i in range(502)}})
        self.assertTrue(self.check()['independent_pass'])


if __name__ == '__main__':
    unittest.main()

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from integration import deploy
from integration.lsdb import parse_lsdb


class Task1DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.nodes = deploy.original.load_cfgs(str(deploy.VENDOR / 'cfg'))
        self.links, self.spares, self.mgmt, errors = deploy.original.parse_ports(self.nodes)
        self.assertEqual(errors, [])

    def test_supplied_cfgs_match_delivered_live_topology_snapshot(self):
        deploy.validate_configs(self.nodes, self.links, self.spares, self.mgmt)
        self.assertEqual((len(self.nodes), len(self.links), len(self.spares), len(self.mgmt)),
                         (66, 132, 198, 66))
        graph, _ = parse_lsdb(json.loads((deploy.VENDOR.parent / 'queue-20261027/lsdb_L0305.json').read_text()))
        supplied = set()
        for endpoints in self.links.values():
            a, b = endpoints[0][0], endpoints[1][0]
            supplied.update([(a, b), (b, a)])
        self.assertEqual(supplied, set(graph.edges))

    def test_vendor_deployer_changes_only_incremental_state_persistence(self):
        raw = (deploy.VENDOR / 'deploy_from_cfg.py').read_text()
        patched = Path(deploy.original.__file__).read_text()
        expected = raw.replace('            state["containers"].append(me)\n        save(state)\n',
                               '            state["containers"].append(me)\n'
                               '            save(state)  # Persist each successful node creation before the next one.\n'
                               '        save(state)\n')
        self.assertEqual(patched, expected)

    def test_shell_metacharacters_in_interface_rejected(self):
        nodes = copy.deepcopy(self.nodes)
        first = next(iter(nodes['L0101']['cfg']['ports'].values()))
        first['eth'] = 'eth1;id'
        with self.assertRaisesRegex(ValueError, 'Invalid interface'):
            deploy.validate_configs(nodes, self.links, self.spares, self.mgmt)

    def test_undo_refuses_replaced_container_before_any_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = {'deployment_id': 'owner', 'containers': ['L0101'],
                     'container_ids': {'L0101': 'original-id'}, 'host_ends': []}
            Path(tmp, 'deploy_state.json').write_text(json.dumps(state))
            inspected = [{'Id': 'replacement-id', 'Config': {'Labels': {deploy.LABEL: 'owner'}}}]
            with patch('sys.argv', ['deploy', '--undo', '--output', tmp]), \
                 patch.object(deploy, 'call', return_value=json.dumps(inspected)), \
                 patch.object(deploy.original, 'undo') as undo:
                with self.assertRaisesRegex(ValueError, 'unowned container'):
                    deploy.main()
                undo.assert_not_called()

    def test_cfg_generation_option_not_exposed(self):
        with patch('sys.argv', ['deploy', '--gen-cfg', '/tmp/not-allowed']):
            with self.assertRaises(SystemExit) as result:
                deploy.main()
            self.assertEqual(result.exception.code, 2)


if __name__ == '__main__':
    unittest.main()

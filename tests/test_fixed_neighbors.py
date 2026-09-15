import unittest

from scripts.fixed_neighbors import peer_entries, restore_commands


class FixedNeighborsTests(unittest.TestCase):
    def setUp(self):
        self.links = [{'src': 'a', 'dst': 'b', 'local_iface': '1234abcd', 'status': 'UP'},
                      {'src': 'b', 'dst': 'a', 'local_iface': '1234abcd', 'status': 'UP'}]
        self.inventory = {}
        for name, address, mac in [('a', '10.0.0.1', '02:00:00:00:00:01'),
                                   ('b', '10.0.0.2', '02:00:00:00:00:02')]:
            self.inventory[name] = {'neighbors': [], 'interfaces': {
                '1234abcd': {'address': mac, 'addr_info': [
                    {'family': 'inet', 'local': address, 'prefixlen': 30, 'scope': 'global'},
                    {'family': 'inet6', 'local': 'fe80::' + ('1' if name == 'a' else '2'),
                     'prefixlen': 64, 'scope': 'link'}]},
                'lo': {'addr_info': [{'family': 'inet6', 'local': 'fd01::' + ('1' if name == 'a' else '2'),
                                     'prefixlen': 128, 'scope': 'global'}]}}}

    def test_only_verified_direct_peers_are_pinned(self):
        plan = peer_entries(self.links, self.inventory)
        self.assertEqual({entry['dst'] for entry in plan['a']}, {'10.0.0.2', 'fe80::2', 'fd01::2'})
        self.assertTrue(all(entry['lladdr'] == '02:00:00:00:00:02' for entry in plan['a']))

    def test_wrong_subnet_is_rejected(self):
        self.inventory['b']['interfaces']['1234abcd']['addr_info'][0]['local'] = '10.1.0.2'
        with self.assertRaisesRegex(RuntimeError, 'subnet'):
            peer_entries(self.links, self.inventory)

    def test_missing_reverse_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'reverse'):
            peer_entries(self.links[:1], self.inventory)

    def test_down_link_is_rejected(self):
        self.links[0]['status'] = 'DOWN'
        with self.assertRaisesRegex(RuntimeError, 'non-UP'):
            peer_entries(self.links, self.inventory)

    def test_restore_deletes_only_new_owned_entries(self):
        entries = peer_entries(self.links, self.inventory)['a']
        current = [dict(entry, state=['PERMANENT']) for entry in entries]
        commands = restore_commands(entries, current)
        self.assertEqual(len(commands), 3)
        self.assertTrue(all(command.startswith('neigh del ') for command in commands))
        current[0]['lladdr'] = '02:00:00:00:00:ff'
        self.assertEqual(len(restore_commands(entries, current)), 2)

    def test_previous_dynamic_and_static_are_restored(self):
        entries = peer_entries(self.links, self.inventory)['a']
        current = [dict(entry, state=['PERMANENT']) for entry in entries]
        for index, state in enumerate(['REACHABLE', 'PERMANENT']):
            entries[index]['previous'] = dict(entries[index], state=[state])
        commands = restore_commands(entries, current)
        self.assertIn('nud stale', commands[0])
        self.assertIn('nud permanent', commands[1])
        self.assertEqual(restore_commands(entries, []), [])


if __name__ == '__main__':
    unittest.main()

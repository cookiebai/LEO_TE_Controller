import unittest

from scripts.collect_srv6_evidence import (
    choose_policy,
    identity_sid,
    path_markdown,
    redact_account_paths,
    sanitize,
    transit_sid,
)


class Srv6EvidenceTests(unittest.TestCase):
    def test_identity_and_transit_sids(self):
        node = "Satellite_0123abcd"
        self.assertEqual(identity_sid(node), "fd00:123:abcd::1")
        self.assertEqual(transit_sid(node), "fd01:123:abcd::1")

    def test_choose_policy_skips_short_paths_and_honors_requested_flow(self):
        policies = [
            {"flow_id": "flow-z", "path": ["source", "middle", "destination"]},
            {"flow_id": "flow-a", "path": ["source", "destination"]},
            {"flow_id": "flow-b", "path": ["source", "middle", "destination"]},
        ]

        self.assertEqual(choose_policy(policies, None)["flow_id"], "flow-b")
        self.assertEqual(choose_policy(policies, "flow-z")["flow_id"], "flow-z")
        with self.assertRaisesRegex(RuntimeError, "missing-flow"):
            choose_policy(policies, "missing-flow")

    def test_sanitize_redacts_credentials_but_preserves_network_fields(self):
        original = "\n".join(
            [
                "password=hunter2",
                'token: "token-value"',
                "accessToken=access-token-value",
                "Authorization: Bearer bearer-value",
                "jwt=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signatureabcdefghijk",
                "redis_url=redis://uri-user:uri-password@10.0.0.10:6379/0",
                "local_iface=eth0",
                "source_ipv6=fd00:123:abcd::1",
                "next_hop=fe80::1",
                "destination_ipv4=10.0.0.2",
            ]
        )

        cleaned, findings = sanitize(original)

        self.assertNotIn("hunter2", cleaned)
        self.assertNotIn("token-value", cleaned)
        self.assertNotIn("access-token-value", cleaned)
        self.assertNotIn("bearer-value", cleaned)
        self.assertNotIn("eyJhbGciOiJIUzI1NiJ9", cleaned)
        self.assertNotIn("uri-user", cleaned)
        self.assertNotIn("uri-password", cleaned)
        self.assertIn("accessToken=[REDACTED]", cleaned)
        self.assertIn("jwt=[REDACTED JWT]", cleaned)
        self.assertIn("redis_url=redis://[REDACTED]@10.0.0.10:6379/0", cleaned)
        self.assertGreaterEqual(cleaned.count("[REDACTED]"), 5)
        self.assertEqual(
            set(findings),
            {"凭据形态赋值", "Bearer 凭据", "JWT", "URI 内嵌凭据"},
        )
        for network_field in (
            "local_iface=eth0",
            "source_ipv6=fd00:123:abcd::1",
            "next_hop=fe80::1",
            "destination_ipv4=10.0.0.2",
        ):
            self.assertIn(network_field, cleaned)

    def test_path_markdown_uses_reverse_edge_for_receive_interfaces(self):
        source = "Satellite_00000001"
        middle = "Satellite_00000002"
        destination = "GroundStation_00000003"
        policy = {"path": [source, middle, destination]}
        links = {
            (source, middle): {
                "local_iface": "source-out",
                "host_iface": "source-out-veth",
            },
            (middle, source): {
                "local_iface": "middle-from-source",
                "host_iface": "middle-from-source-veth",
            },
            (middle, destination): {
                "local_iface": "middle-out",
                "host_iface": "middle-out-veth",
            },
            (destination, middle): {
                "local_iface": "destination-from-middle",
                "host_iface": "destination-from-middle-veth",
            },
        }

        diagram, table = path_markdown(policy, links)

        self.assertIn(f"**{source}**（源）", diagram)
        self.assertIn(f"**{middle}**（采集中间节点）", diagram)
        self.assertIn(f"**{destination}**（目的）", diagram)
        self.assertIn(
            f"| 1 | `{source}` | `{middle}` | `source-out` | `source-out-veth` | "
            f"`middle-from-source` | `middle-from-source-veth` | `{transit_sid(middle)}` |",
            table,
        )
        self.assertIn(
            f"| 2 | `{middle}` | `{destination}` | `middle-out` | `middle-out-veth` | "
            f"`destination-from-middle` | `destination-from-middle-veth` | "
            f"`{transit_sid(destination)}` |",
            table,
        )

    def test_account_paths_are_redacted_even_when_docker_truncates_them(self):
        cleaned = redact_account_paths(
            "/home/lenovo/bxr_lab/file\n/home/lenovo/bxr_la…\n/home/bai/legacy"
        )
        self.assertIn("<PROJECT_DIR>/file", cleaned)
        self.assertIn("<HOME>/bxr_la…", cleaned)
        self.assertIn("<HOME>/legacy", cleaned)
        self.assertNotIn("/home/lenovo", cleaned)
        self.assertNotIn("/home/bai", cleaned)


if __name__ == "__main__":
    unittest.main()

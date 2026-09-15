import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run_te_experiment as experiment
import udp_flow_probe as probe


class PacingTests(unittest.TestCase):
    def test_deschedule_does_not_reduce_offered_load(self):
        class Clock:
            now = 0.0

            def time(self):
                return self.now

            def sleep(self, seconds):
                self.now += seconds

        clock = Clock()

        class Socket:
            sent = 0

            def setsockopt(self, *args):
                pass

            def bind(self, *args):
                pass

            def close(self):
                pass

            def sendto(self, *args):
                self.sent += 1
                # Reproduce the >50-ms deschedules that formerly reset pacing.
                if self.sent % 100 == 0:
                    clock.now += 0.075

        sock = Socket()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "client.json"
            args = argparse.Namespace(
                start_file=None, start_at=0.0, duration=5,
                rate_kbps=4400, payload_bytes=1000,
                source_port=30001, destination="127.0.0.1",
                port=30002, output=str(output),
            )
            with patch.object(probe.socket, "socket", return_value=sock), \
                    patch.object(probe.time, "time", clock.time), \
                    patch.object(probe.time, "sleep", clock.sleep):
                probe.run_client(args)
            result = json.loads(output.read_text())
            self.assertEqual(result["target_packets"], 2750)
            self.assertEqual(result["sent_packets"], 2750)
            self.assertEqual(result["sent_ratio"], 1.0)


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.args = argparse.Namespace(
            protocol="udp-native", cycles=2,
            target_low_percent=17, target_high_percent=23,
            max_improvement_stddev_percent=3.0,
            max_phase_cv_percent=5.0,
            max_paired_sent_difference_percent=1.0,
            result=Path(self.directory.name) / "result.json",
        )

    def cycle(self, improvement=20, baseline_cv=0.01, te_sent=76.4):
        return {
            "improvement_percent": improvement,
            "baseline": {"total_cv": baseline_cv, "actual_sent_mbps": 76.4},
            "te": {"total_cv": 0.01, "actual_sent_mbps": te_sent},
        }

    def checkpoint(self, cycles, complete=True):
        return experiment.write_checkpoint(self.args, {}, cycles, complete)

    def test_valid_repeated_measurement(self):
        result = self.checkpoint([self.cycle(19), self.cycle(21)])
        self.assertTrue(result["target_met"])

    def test_mean_in_range_does_not_hide_outlier_cycle(self):
        result = self.checkpoint([self.cycle(16), self.cycle(24)])
        self.assertEqual(result["mean_improvement_percent"], 20)
        self.assertFalse(result["all_cycles_in_target"])
        self.assertFalse(result["target_met"])

    def test_high_phase_cv_is_not_accepted(self):
        result = self.checkpoint([self.cycle(baseline_cv=0.06), self.cycle()])
        self.assertFalse(result["phase_stable"])
        self.assertFalse(result["target_met"])

    def test_mismatched_offered_load_is_not_accepted(self):
        result = self.checkpoint([self.cycle(te_sent=74.8), self.cycle()])
        self.assertFalse(result["paired_load_valid"])
        self.assertFalse(result["target_met"])

    def test_interruption_preserves_completed_cycle_without_claiming_success(self):
        result = self.checkpoint([self.cycle()], complete=False)
        saved = json.loads(self.args.result.read_text())
        self.assertEqual(saved["completed_cycles"], 1)
        self.assertEqual(saved["status"], "in_progress")
        self.assertFalse(result["target_met"])


class ResourceTests(unittest.TestCase):
    def test_memory_pressure_refuses_measurement(self):
        args = argparse.Namespace(
            min_host_available_mib=512, max_host_memory_stall_percent=5,
        )

        def read_text(path, *args, **kwargs):
            if str(path) == "/proc/meminfo":
                return "MemAvailable: 65536 kB\nSwapFree: 0 kB\n"
            return "some avg10=40 avg60=30 avg300=20 total=1\nfull avg10=19 avg60=20 avg300=10 total=1\n"

        with patch.object(Path, "read_text", read_text), \
                patch.object(Path, "exists", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "Free memory"):
                experiment.validate_host_resources(args)

    def test_transient_memory_pressure_is_retried(self):
        args = argparse.Namespace(resource_wait_seconds=10)
        recovered = {
            "available_mib": 600,
            "memory_full_stall_avg10_percent": 0.5,
        }
        with patch.object(
            experiment,
            "validate_host_resources",
            side_effect=[RuntimeError("temporarily busy"), recovered],
        ) as validate, patch.object(experiment.time, "sleep") as sleep:
            result = experiment.wait_for_host_resources(args)
        self.assertEqual(result, recovered)
        self.assertEqual(validate.call_count, 2)
        sleep.assert_called_once()


class RuntimeRecoveryTests(unittest.TestCase):
    def test_ospf_health_repairs_only_dead_node(self):
        health = [
            {"container": "sat-a", "live": 0, "routes": 0},
            {"container": "sat-b", "live": 1, "routes": 200},
            {"container": "sat-a", "live": 1, "routes": 119},
            {"container": "sat-b", "live": 1, "routes": 200},
        ]
        with patch.object(experiment, "satellite_containers",
                          return_value=["sat-a", "sat-b"]), \
                patch.object(experiment, "container_ospf_health",
                             side_effect=health), \
                patch.object(experiment, "repair_container_ospf") as repair:
            result = experiment.ensure_ospf_health(timeout_seconds=1)
        repair.assert_called_once_with("sat-a")
        self.assertEqual(len(result), 2)

    def test_ping_validation_retries_only_failed_policy(self):
        redis_client = unittest.mock.Mock()
        redis_client.hvals.return_value = [
            json.dumps({"flow_id": "flow-1"}),
        ]
        with patch.object(
            experiment,
            "ping_policy",
            side_effect=[
                ("flow-1", False, True),
                ("flow-1", True, True),
            ],
        ), patch.object(experiment.time, "sleep"):
            result = experiment.validate_policy_pings(redis_client)
        self.assertEqual(result["validation_rounds"], 2)
        self.assertEqual(result["srv6_ipv6_ping_ok"], 1)
        self.assertEqual(result["srv6_ipv4_ping_ok"], 1)


if __name__ == "__main__":
    unittest.main()

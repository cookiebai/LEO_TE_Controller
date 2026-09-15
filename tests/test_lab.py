import json
import fcntl
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import calibrate, lab


class CalibrationRecordsTests(unittest.TestCase):
    def test_concurrent_calibration_is_rejected_before_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'controller'
            runtime = Path(directory) / 'runtime'
            runtime.mkdir()
            with (runtime / 'calibration.lock').open('a') as lock, \
                    patch.object(calibrate, 'ROOT', root), \
                    patch.object(calibrate, 'calibrate') as run:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(RuntimeError, '已有校准'):
                    calibrate.main()
                run.assert_not_called()

    def test_directory_is_taken_from_this_trial_log(self):
        path = calibrate.ROOT / 'results/runs/example'
        self.assertEqual(calibrate.run_directory(f'[time] 本次运行目录：{path}\n'), path)

    def test_failed_launch_does_not_reuse_latest(self):
        with self.assertRaisesRegex(RuntimeError, 'did not start'):
            calibrate.run_directory('错误：已经有实验或部署正在运行\n')

    def test_directory_outside_results_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'Unexpected'):
            calibrate.run_directory('本次运行目录：/tmp/unrelated\n')


class OwnershipTests(unittest.TestCase):
    def test_same_count_transposed_scale_is_rejected(self):
        data = [{'type': 'Satellite', 'extra': {'OrbitIndex': str(i), 'SatelliteIndex': str(j)}}
                for i in range(20) for j in range(25)]
        with patch.object(lab, 'api', return_value=data):
            lab.verify_scale('20x25')
            with self.assertRaisesRegex(RuntimeError, '不能混用'):
                lab.verify_scale('25x20')

    def test_missing_config_does_not_clean_deployment(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(lab, 'OPEN', Path(directory)), \
                patch.object(lab, 'rebuild_cleanup') as cleanup:
            with self.assertRaisesRegex(RuntimeError, '缺少'):
                lab.prepare('25x20', 500, rebuild=True)
            cleanup.assert_not_called()

    def test_other_workspace_nodes_are_rejected(self):
        info = [{'Name': 'Satellite_deadbeef', 'Mounts': [
            {'Destination': '/share', 'Source': '/home/lenovo/another-lab/share'}]}]
        with patch.object(lab, 'nodes', return_value=['Satellite_deadbeef']), \
                patch.object(lab, 'command', return_value=json.dumps(info)):
            with self.assertRaisesRegex(RuntimeError, '其他目录'):
                lab.verify_ownership()

    def test_unlabelled_service_is_rejected(self):
        with patch.object(lab, 'nodes', return_value=[]), \
                patch.object(lab, 'has_container', return_value=True), \
                patch.object(lab, 'command', return_value='[{"Config":{"Labels":null}}]'):
            with self.assertRaisesRegex(RuntimeError, '归属标签'):
                lab.verify_ownership()


if __name__ == '__main__':
    unittest.main()

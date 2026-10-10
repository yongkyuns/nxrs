"""Safety and command routing tests without touching a serial device."""
import importlib.util
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('comparison_matrix', Path(__file__).with_name('run_matrix.py'))
matrix = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(matrix)


class MatrixTests(unittest.TestCase):
    def test_rotation_keeps_each_case_once(self):
        cases = ['c-packet-2', 'rust-packet-2', 'zephyr-packet-2']
        self.assertEqual(matrix.rotated_cases(cases, 1), cases[1:] + cases[:1])
        self.assertEqual(matrix.rotated_cases(cases, 3), cases)

    def test_command_and_image_match_each_platform(self):
        for case, (os_name, _, language, mode) in matrix.CASES.items():
            command = matrix.command(case, Path('/frozen'), Path('/fresh'), 'PORT', 'FLASHER', 3)
            image = command[command.index('--image') + 1]
            self.assertEqual(image, '/frozen/zephyr.bin' if os_name == 'zephyr' else f'/frozen/{language}.merged.bin')
            self.assertEqual(command[command.index('--runs') + 1], '3')
            if mode == 'baseline':
                self.assertIn('baseline', command[command.index('--command') + 1])

    def test_restore_preserves_header_then_verifies(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            with patch.object(matrix.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'ok', '')) as run:
                matrix.restore('FLASHER', 'PORT', Path('/private/backup.bin'), out)
            commands = [call.args[0] for call in run.call_args_list]
            self.assertIn('write_flash', commands[0])
            for flag in ('--flash_mode', '--flash_freq', '--flash_size'):
                self.assertEqual(commands[0][commands[0].index(flag) + 1], 'keep')
            self.assertIn('verify_flash', commands[1])
            self.assertTrue((out / 'restore-verify.log').exists())

    def test_restore_failure_is_not_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(matrix.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'failed')):
                with self.assertRaises(subprocess.CalledProcessError):
                    matrix.restore('FLASHER', 'PORT', Path('/backup.bin'), Path(temporary))

    def test_measurement_failure_still_restores_and_records_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backup, output = root / 'backup.bin', root / 'output'
            with backup.open('wb') as handle:
                handle.truncate(16777216)
            backup.chmod(0o600)
            argv = ['run_matrix.py', '--artifacts', str(root), '--out', str(output),
                    '--backup', str(backup), '--port', 'PORT', '--flasher', 'FLASHER',
                    '--case', 'c-packet-2', '--runs', '1']
            with patch.object(matrix.sys, 'argv', argv), \
                    patch.object(matrix, 'frozen_image'), \
                    patch.object(matrix.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'measurement')), \
                    patch.object(matrix, 'restore') as restore:
                with self.assertRaises(subprocess.CalledProcessError):
                    matrix.main()
            restore.assert_called_once_with('FLASHER', 'PORT', backup, output)
            record = json.loads((output / 'matrix.json').read_text())
            self.assertTrue(record['restore_verified'])
            self.assertIn('CalledProcessError', record['failure'])
            self.assertEqual(record['order'], [])
            self.assertIsNone(record['restore_error'])
            self.assertEqual((output / 'matrix.json').stat().st_mode & 0o777, 0o600)

    def test_restore_failure_and_changed_backup_are_recorded_without_masking_run_failure(self):
        for scenario in ('restore', 'backup', 'both'):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                backup, output = root / 'backup.bin', root / 'output'
                with backup.open('wb') as handle:
                    handle.truncate(16_777_216)
                backup.chmod(0o600)
                argv = ['--artifacts', str(root), '--out', str(output),
                        '--backup', str(backup), '--port', 'PORT', '--flasher', 'FLASHER',
                        '--case', 'c-packet-2', '--runs', '1']

                def restore_backup(*args):
                    if scenario == 'backup':
                        with backup.open('r+b') as handle:
                            handle.write(b'changed')
                    else:
                        raise OSError('restore failed')

                measurement = (subprocess.CalledProcessError(1, 'measurement')
                               if scenario == 'both' else None)
                expected_error = subprocess.CalledProcessError if measurement else RuntimeError
                with patch.object(matrix, 'frozen_image'), \
                        patch.object(matrix.subprocess, 'run', side_effect=measurement), \
                        patch.object(matrix, 'restore', side_effect=restore_backup) as restore:
                    with self.assertRaises(expected_error):
                        matrix.main(argv)
                restore.assert_called_once_with('FLASHER', 'PORT', backup, output)
                record = json.loads((output / 'matrix.json').read_text())
                self.assertFalse(record['restore_verified'])
                detail = 'backup changed' if scenario == 'backup' else 'restore failed'
                self.assertIn(detail, record['restore_error'])
                if measurement:
                    self.assertIn('CalledProcessError', record['failure'])
                else:
                    self.assertEqual(record['failure'], record['restore_error'])
                self.assertEqual((output / 'matrix.json').stat().st_mode & 0o777, 0o600)

    def test_frozen_artifact_corruption_or_partial_build_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = ('zephyr.bin', 'zephyr/zephyr.elf', 'resolved.config')
            for name in files:
                path = root / name
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(name.encode())
            digest = lambda name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            provenance = dict(status='success', failure=None, zephyr_bin_sha256=digest(files[0]),
                              elf_sha256=digest(files[1]), resolved_config_sha256=digest(files[2]))
            path = root / 'build-provenance.json'
            path.write_text(json.dumps(provenance))
            self.assertEqual(matrix.frozen_image(root, 'zephyr-packet-2'), root / 'zephyr.bin')
            (root / 'zephyr.bin').write_bytes(b'corrupt')
            with self.assertRaisesRegex(ValueError, 'changed frozen'):
                matrix.frozen_image(root, 'zephyr-packet-2')
            provenance['status'] = 'failed'
            path.write_text(json.dumps(provenance))
            with self.assertRaisesRegex(ValueError, 'incomplete'):
                matrix.frozen_image(root, 'zephyr-packet-2')


if __name__ == '__main__':
    unittest.main()

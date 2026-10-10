"""Embassy matrix safety and routing tests; never flash hardware."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
SPEC = importlib.util.spec_from_file_location('embassy_matrix_tests_module', HERE / 'run_matrix.py')
matrix = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(matrix)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_provenance(directory, *, status='success', failure=None, artifacts=None):
    files = {'embassy.elf': b'elf', 'embassy.bin': b'image'}
    for name, content in files.items():
        (directory / name).write_bytes(content)
    hashes = {name: sha256(directory / name) for name in files}
    record = {'status': status, 'failure': failure, 'artifacts': hashes}
    if artifacts is not None:
        record['artifacts'] = artifacts
    (directory / 'build-provenance.json').write_text(json.dumps(record))
    return record


class EmbassyMatrixTests(unittest.TestCase):
    def test_embassy_adapter_uses_verified_private_restore_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            case = 'embassy-wire-2'
            directory = matrix.case_directory(root, case)
            directory.mkdir()
            write_provenance(directory)
            backup, output = root / 'backup.bin', root / 'output'
            with backup.open('wb') as handle:
                handle.truncate(16_777_216)
            backup.chmod(0o600)
            argv = ['--artifacts', str(root), '--out', str(output),
                    '--backup', str(backup), '--port', 'PORT', '--flasher', 'FLASHER',
                    '--case', case, '--runs', '1']
            with patch.object(matrix.shared.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run, \
                    patch.object(matrix.shared, 'restore') as restore:
                matrix.shared.main(argv, cases=matrix.CASES,
                                   image_validator=matrix.frozen_image, command_builder=matrix.command)
            self.assertEqual(run.call_args.args[0][1], str(HERE / 'measure.py'))
            restore.assert_called_once_with('FLASHER', 'PORT', backup, output)
            report = output / 'matrix.json'
            record = json.loads(report.read_text())
            self.assertTrue(record['restore_verified'])
            self.assertIsNone(record['restore_error'])
            self.assertIsNone(record['failure'])
            self.assertEqual(record['order'], [{'case': case, 'block': 0}])
            self.assertEqual(report.stat().st_mode & 0o777, 0o600)

    def test_embassy_extension_does_not_mutate_shared_runner(self):
        self.assertTrue(matrix.EMBASSY.keys().isdisjoint(matrix.shared.CASES))
        self.assertIsNot(matrix.frozen_image, matrix.shared.frozen_image)
        self.assertIsNot(matrix.command, matrix.shared.command)
        self.assertEqual(matrix.CASES, {**matrix.shared.CASES, **matrix.EMBASSY})

    def test_embassy_cases_are_registered_with_expected_modes(self):
        self.assertEqual(matrix.EMBASSY, {
            'embassy-wire-2': ('embassy', 'embassy-wire-2-v2', 'embassy', 'wire'),
            'embassy-packet-2': ('embassy', 'embassy-packet-2-v2', 'embassy', 'packet'),
            'embassy-packet-os': ('embassy', 'embassy-packet-os-v2', 'embassy', 'packet'),
            'embassy-packet-yield-2': ('embassy', 'embassy-packet-yield-2-v2', 'embassy', 'packet'),
            'embassy-baseline': ('embassy', 'embassy-baseline-2-v3', 'embassy', 'baseline'),
            'embassy-baseline-os': ('embassy', 'embassy-baseline-v2', 'embassy', 'baseline'),
        })
        for case, definition in matrix.EMBASSY.items():
            with self.subTest(case=case):
                self.assertEqual(matrix.CASES[case], definition)

    def test_embassy_commands_use_frozen_image_and_case_mode(self):
        for case, (_, _, _, mode) in matrix.EMBASSY.items():
            with self.subTest(case=case):
                command = matrix.command(case, Path('/frozen'), Path('/out'), 'PORT', 'FLASHER', 4)
                self.assertEqual(command[command.index('--image') + 1], '/frozen/embassy.bin')
                self.assertEqual(command[command.index('--mode') + 1], mode)
                self.assertEqual(command[command.index('--runs') + 1], '4')
                self.assertEqual(command[command.index('--port') + 1], 'PORT')
                self.assertEqual(command[command.index('--flasher') + 1], 'FLASHER')
                self.assertEqual(command[0], sys.executable)

    def test_frozen_image_accepts_complete_matching_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_provenance(directory)
            self.assertEqual(matrix.frozen_image(directory, 'embassy-packet-2'),
                             directory / 'embassy.bin')

    def test_frozen_image_rejects_incomplete_or_failed_builds(self):
        cases = (
            ({'status': 'building', 'failure': None}, 'incomplete Embassy build'),
            ({'status': 'success', 'failure': 'link failed'}, 'incomplete Embassy build'),
            ({'status': 'success', 'failure': None,
              'artifacts': {'embassy.bin': '0' * 64}}, 'missing Embassy artifacts'),
        )
        for record, error in cases:
            with self.subTest(record=record), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                write_provenance(directory, status=record['status'], failure=record['failure'],
                                 artifacts=record.get('artifacts'))
                with self.assertRaisesRegex(ValueError, error):
                    matrix.frozen_image(directory, 'embassy-packet-2')

    def test_frozen_image_rejects_changed_hash_and_unsafe_artifact_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            record = write_provenance(directory)
            (directory / 'embassy.bin').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'changed Embassy artifact: embassy.bin'):
                matrix.frozen_image(directory, 'embassy-packet-2')

            (directory / 'embassy.bin').write_bytes(b'image')
            record['artifacts']['../outside.bin'] = '0' * 64
            (directory / 'build-provenance.json').write_text(json.dumps(record))
            with self.assertRaisesRegex(ValueError, 'unsafe artifact path'):
                matrix.frozen_image(directory, 'embassy-packet-2')

            del record['artifacts']['../outside.bin']
            record['artifacts']['/outside.bin'] = '0' * 64
            (directory / 'build-provenance.json').write_text(json.dumps(record))
            with self.assertRaisesRegex(ValueError, 'unsafe artifact path'):
                matrix.frozen_image(directory, 'embassy-packet-2')

    def test_non_embassy_cases_forward_to_cached_original_helpers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nuttx = root / 'nuttx'
            nuttx.mkdir()
            for name, content in (('c.merged.bin', b'nuttx image'), ('c.elf', b'nuttx elf'),
                                  ('resolved.config', b'config')):
                (nuttx / name).write_bytes(content)
            (nuttx / 'c-build-provenance.json').write_text(json.dumps({
                'artifacts': {name: sha256(nuttx / name) for name in
                              ('c.merged.bin', 'c.elf', 'resolved.config')}}))

            zephyr = root / 'zephyr'
            (zephyr / 'zephyr').mkdir(parents=True)
            for name, content in (('zephyr.bin', b'zephyr image'),
                                  ('zephyr/zephyr.elf', b'zephyr elf'),
                                  ('resolved.config', b'config')):
                (zephyr / name).write_bytes(content)
            (zephyr / 'build-provenance.json').write_text(json.dumps({
                'status': 'success', 'failure': None,
                'zephyr_bin_sha256': sha256(zephyr / 'zephyr.bin'),
                'elf_sha256': sha256(zephyr / 'zephyr/zephyr.elf'),
                'resolved_config_sha256': sha256(zephyr / 'resolved.config')}))

            self.assertEqual(matrix.frozen_image(nuttx, 'c-packet-2'), nuttx / 'c.merged.bin')
            self.assertEqual(matrix.frozen_image(zephyr, 'zephyr-packet-2'), zephyr / 'zephyr.bin')

            n_command = matrix.command('c-packet-2', Path('/frozen'), Path('/out'),
                                       'PORT', 'FLASHER', 3)
            z_command = matrix.command('zephyr-packet-2', Path('/frozen'), Path('/out'),
                                       'PORT', 'FLASHER', 3)
            self.assertIn('measure_native.py', n_command[1])
            self.assertEqual(n_command[n_command.index('--image') + 1], '/frozen/c.merged.bin')
            self.assertIn('measure.py', z_command[1])
            self.assertEqual(z_command[z_command.index('--image') + 1], '/frozen/zephyr.bin')


if __name__ == '__main__':
    unittest.main()

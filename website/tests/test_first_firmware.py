"""Source-workflow and console rejection tests; no firmware or emulator required."""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from check_examples import ROOT, block, command, create_project, phases

# The target console runner uses Unix PTYs; source checks remain portable.
if os.name == 'posix':
    from first_firmware import Console, validate_response


class SourceWorkflowTests(unittest.TestCase):
    def test_create_command_is_cargo_new_not_binary_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve() / 'my-firmware'
            args = command('first-application.md', 'create', project)
            self.assertEqual(args[:2], ['cargo', 'new'])
            self.assertIn('--bin', args)
            self.assertEqual(Path(args[-1]), project)
            with patch('check_examples.run') as invoke:
                project.mkdir()
                with self.assertRaises(FileExistsError):
                    create_project(project, {})
                invoke.assert_not_called()

    def test_manifest_and_hello_are_the_published_source(self):
        manifest = tomllib.loads(block('first-application.md', 'manifest', 'toml'))
        reference = ROOT / 'website/examples/basic-firmware'
        self.assertEqual(manifest, tomllib.loads((reference / 'Cargo.toml').read_text()))
        self.assertEqual(block('first-application.md', 'source', 'rust').strip(),
                         (reference / 'src/main.rs').read_text().strip())
        self.assertFalse(manifest.get('dependencies'))
        self.assertEqual(manifest['package']['metadata']['nxrs']['firmware']['command'], 'hello')

    def test_full_worker_matches_tested_source(self):
        reference = (ROOT / 'website/examples/basic-firmware/examples/worker.rs').read_text()
        self.assertEqual(block('services.md', 'worker-source', 'rust').strip(),
                         reference.split('#[cfg(test)]')[0].strip())
        cases = phases()
        self.assertEqual([name for name, _, _ in cases], ['hello', 'edited', 'worker'])
        self.assertEqual(len({source for _, source, _ in cases}), 3)
        self.assertEqual(len({expected for _, _, expected in cases}), 3)

    def test_target_command_builds_external_source_without_dry_run(self):
        project = Path(tempfile.gettempdir()).resolve() / 'my-firmware'
        args = command('firmware.md', 'build', project)
        self.assertEqual(args, ['cargo', 'nxrs', 'build', '--manifest-path',
                               str(project / 'Cargo.toml'), '--profile', 'esp32s3-qemu-mock'])
        self.assertNotIn('--dry-run', args)
        self.assertIn('cargo generate-lockfile', block('first-application.md', 'lock', 'sh'))

    def test_setup_and_launch_are_explicit(self):
        packages = block('firmware.md', 'packages', 'sh').splitlines()
        self.assertEqual(shlex.split(packages[1])[:4], ['sudo', 'apt-get', 'install', '-y'])
        launch = block('firmware.md', 'qemu', 'sh')
        self.assertTrue(launch.startswith('(\n'))
        for item in ('-machine esp32s3', '-nic none', '-snapshot', 'target/build/hello/', 'if=mtd,format=raw'):
            self.assertIn(item, launch)
        subprocess.run(['bash', '-n'], input=launch, text=True, check=True)


@unittest.skipUnless(os.name == 'posix', 'target console requires Unix PTYs')
class ConsoleTests(unittest.TestCase):
    def test_exact_output_and_shell_status(self):
        validate_response(' hello\r\nHello from NXRS\r\nnsh>', 'hello', 'Hello from NXRS')
        validate_response(' echo $?\r\n0\r\nnsh>', 'echo $?', '0')
        validate_response('Hello from NXRS\nnsh>', 'hello', 'Hello from NXRS')

    def test_echo_partial_duplicate_wrong_revision_and_nonzero_fail(self):
        for text in ('hello\nnsh>', 'hello\nHello from NXRS',
                     'hello\nHello from NXRS\nHello from NXRS\nnsh>',
                     'hello\nHello again from NXRS\nnsh>',
                     'hello\nHello from NXRS\npanic\nnsh>'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                validate_response(text, 'hello', 'Hello from NXRS')
        for text in ('echo $?\nnsh>', 'echo $?\n1\nnsh>', 'echo $?\n0\n0\nnsh>'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                validate_response(text, 'echo $?', '0')

    def test_readline_clear_to_eol_prefix_is_command_echo(self):
        # NSH's exact response shape from tutorial run 42939498260.
        for expected in ('Hello from NXRS', 'Hello again from NXRS', 'Processed total: 6'):
            with self.subTest(expected=expected):
                validate_response(f' \x1b[Khello\r\r\n{expected}\r\r\nnsh>', 'hello', expected)
        validate_response(' \x1b[Kecho $?\r\r\n0\r\r\nnsh>', 'echo $?', '0')

    def test_readline_echo_does_not_relax_output_checks(self):
        for text in ('\x1b[Khello\nnsh>',
                     '\x1b[Khello\nHello from NXRS',
                     '\x1b[Khello\nHello from NXRS\nHello from NXRS\nnsh>',
                     '\x1b[Khello\nHello again from NXRS\nnsh>',
                     '\x1b[Khello\nHello from NXRS\npanic\nnsh>',
                     '\x1b[Khello\n\x1b[KHello from NXRS\nnsh>',
                     '\x1b[Khello\nHello\x1b[K from NXRS\nnsh>',
                     '\x1b[Khello\n\x1b[K\nHello from NXRS\nnsh>',
                     '\x1b[2Khello\nHello from NXRS\nnsh>',
                     '\x1b[K\x1b[Khello\nHello from NXRS\nnsh>',
                     '\x1b[Khello\nhello\nHello from NXRS\nnsh>'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                validate_response(text, 'hello', 'Hello from NXRS')
        for text in ('\x1b[Kecho $?\nnsh>', '\x1b[Kecho $?\n1\nnsh>',
                     '\x1b[Kecho $?\n0\n0\nnsh>', '\x1b[Kecho $?\n\x1b[K0\nnsh>'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                validate_response(text, 'echo $?', '0')

    def test_fragmented_readline_echo_keeps_raw_transcript(self):
        chunks = [b' ', b'\x1b', b'[', b'Khello', b'\r\r\n',
                  b'Hello from NXRS', b'\r\r\n', b'ns', b'h>']
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / 'readline.log'
            console = Console([sys.executable, '-u', '-c',
                               'import time; time.sleep(60)'], log)
            try:
                with patch('first_firmware.select.select', return_value=([console.fd], [], [])), \
                     patch('first_firmware.os.read', side_effect=chunks) as read:
                    response = console.until('nsh>', 5)
                    self.assertEqual(read.call_count, len(chunks))
                validate_response(response, 'hello', 'Hello from NXRS')
            finally:
                console.close()
            self.assertEqual(log.read_bytes(), b''.join(chunks))
            self.assertIsNotNone(console.process.poll())

    def test_timeout_closes_owned_child_and_keeps_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / 'timeout.log'
            console = Console([sys.executable, '-u', '-c',
                               'import time; print("started", flush=True); time.sleep(60)'], log)
            try:
                console.until('started', 5)
                with self.assertRaises(TimeoutError):
                    console.until('nsh>', 0.05)
            finally:
                console.close()
            self.assertIsNotNone(console.process.poll())
            self.assertIn(b'started', log.read_bytes())
            console.close()  # Idempotent cleanup.

    def test_output_limit_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / 'overflow.log'
            console = Console([sys.executable, '-u', '-c', 'print("x" * 4096)'], log, limit=256)
            try:
                with self.assertRaisesRegex(RuntimeError, 'transcript limit'):
                    console.until('nsh>', 5)
            finally:
                console.close()
            self.assertLessEqual(log.stat().st_size, 256)
            self.assertIsNotNone(console.process.poll())


if __name__ == '__main__':
    unittest.main()

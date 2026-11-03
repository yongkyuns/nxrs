"""Subprocess console-protocol tests; synthetic records are never measurement evidence."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest

import analyze
from test_analyze import fixture, transcript


HERE = Path(__file__).resolve().parent
FAKE_QEMU = r'''
import json
import os
from pathlib import Path
import signal
import sys

config = json.loads(Path(__file__).with_suffix('.json').read_text())
state_path = Path(config['state_path'])
state = {'pid': os.getpid(), 'commands': [], 'terminated_by': None}

def save():
    state_path.write_text(json.dumps(state))

def terminate(signum, frame):
    state['terminated_by'] = signum
    save()
    raise SystemExit(0)

signal.signal(signal.SIGTERM, terminate)
save()
if config['behavior'] == 'missing-banner':
    print('FAKE boot without a required banner\nnsh> ', end='', flush=True)
elif config['behavior'] == 'missing-prompt':
    print('*** Booting NuttX *** (FAKE subprocess, no firmware)', flush=True)
else:
    banner = '*** Booting NuttX ***' if config['behavior'] == 'nuttx-banner' else 'NuttShell'
    if config['behavior'] == 'rom-warning':
        print('SHA-256 comparison failed:\\nAttempting to boot anyway...', flush=True)
    print(banner + ' (FAKE subprocess, no firmware)\nnsh> ', end='', flush=True)
    state['commands'].append(sys.stdin.readline().rstrip('\r\n'))
    save()
    if config['behavior'] == 'timeout':
        print('\nFAKE fixture remains active', flush=True)
    elif config['behavior'] == 'no-result':
        print('\nFAKE fixture returned without records\nnsh> ', end='', flush=True)
    else:
        print('\n' + config['records'] + 'nsh> ', end='', flush=True)
while True:
    signal.pause()
'''


class CaptureProtocolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='std-channel-fake-console-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.build = self.root / 'build'
        self.build.mkdir()
        (self.build / 'image').write_bytes(b'FAKE image for protocol tests; not firmware')
        (self.build / 'config').write_text('CONFIG_SMP=n\n')
        image_hash = analyze.sha256(self.build / 'image')
        receipt = dict(
            schema='std-channel-build-v1', status='complete', mode='latency', hook_stage=0,
            platform='mps2-an521-mock', image_file='image', image_sha256=image_hash,
            elf_file='image', elf_sha256=image_hash, config_file='config',
            config_sha256=analyze.sha256(self.build / 'config'),
            std_array_sha256='a' * 64, std_array_original_sha256='a' * 64,
            compiler_version='FAKE console test', kernel_revision='FAKE console test',
            build_id='b' * 64, clock_request='baseline',
        )
        (self.build / 'build.json').write_text(json.dumps(receipt))
        self.child_states = []
        self.addCleanup(self.cleanup_children)

    def cleanup_children(self):
        # Protect the test runner if capture itself fails before cleaning up.
        for path in self.child_states:
            if path.exists():
                state = json.loads(path.read_text())
                if state['terminated_by'] is None:
                    try:
                        os.kill(state['pid'], signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def run_capture(self, name, behavior='records', records=None):
        directory = self.root / name
        directory.mkdir()
        fake = directory / 'fake-qemu'
        fake.write_text(f'#!{sys.executable}\n' + FAKE_QEMU)
        fake.chmod(0o755)
        state_path = directory / 'child-state.json'
        self.child_states.append(state_path)
        fake.with_suffix('.json').write_text(json.dumps(dict(
            state_path=str(state_path), behavior=behavior,
            records=transcript(*fixture()) if records is None else records,
        )))
        output = directory / 'capture'
        deadline = '1' if behavior in {'missing-banner', 'missing-prompt', 'timeout'} else '3'
        completed = subprocess.run([
            sys.executable, str(HERE / 'capture.py'), 'qemu',
            '--build-directory', str(self.build), '--mode', 'latency',
            '--output', str(output), '--samples', '2', '--warmup', '1',
            '--timeout', deadline, '--qemu', str(fake),
        ], capture_output=True, text=True, timeout=10)
        self.assertTrue((output / 'capture.json').is_file(), completed.stderr)
        report = json.loads((output / 'capture.json').read_text())
        self.assertEqual(json.loads(completed.stdout), report)
        self.assertTrue((output / 'console.log').is_file())
        self.assertTrue((output / 'build.json').is_file())
        self.assertFalse(report['physical_timing_qualified'])
        state = json.loads(state_path.read_text())
        self.assertEqual(state['terminated_by'], signal.SIGTERM)
        # The owned emulator must be reaped before the capture subprocess exits.
        with self.assertRaises(ProcessLookupError):
            os.kill(state['pid'], 0)
        return completed, report, output, state

    def test_one_fresh_boot_one_command_and_two_samples(self):
        completed, report, output, state = self.run_capture('success')
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(report['capture_complete'])
        self.assertTrue(report['fresh_boot_observed'])
        self.assertTrue(report['one_command_per_boot'])
        self.assertEqual(state['commands'], ['std_channel_perf latency 2 1'])
        summary = json.loads((output / 'analysis.json').read_text())
        self.assertEqual(summary['runtime_kind'], 'qemu')
        self.assertEqual(summary['send_to_receive_return']['count'], 2)
        self.assertFalse(summary['physical_timing_qualified'])
        self.assertFalse(summary['priority_progress_qualified'])

    def test_returning_without_a_result_preserves_failure_evidence(self):
        completed, report, output, state = self.run_capture('no-result', 'no-result')
        self.assertEqual(completed.returncode, 1)
        self.assertFalse(report['capture_complete'])
        self.assertEqual(report['failure_stage'], 'analysis')
        self.assertIn('incomplete transcript', report['error'])
        self.assertIn('FAKE fixture returned', (output / 'console.log').read_text())
        self.assertEqual(state['commands'], ['std_channel_perf latency 2 1'])
        self.assertFalse((output / 'analysis.json').exists())

    def test_nuttx_boot_banner_without_optional_shell_greeting(self):
        completed, report, _, state = self.run_capture('nuttx-banner', 'nuttx-banner')
        self.assertEqual(completed.returncode, 0)
        self.assertTrue(report['fresh_boot_observed'])
        self.assertEqual(state['commands'], ['std_channel_perf latency 2 1'])

    def test_rom_boot_warning_remains_visible_and_disqualifies_timing(self):
        completed, report, output, _ = self.run_capture('rom-warning', 'rom-warning')
        self.assertEqual(completed.returncode, 0)
        self.assertTrue(report['boot_rom_digest_warning_observed'])
        self.assertFalse(report['physical_timing_qualified'])
        self.assertTrue(json.loads((output / 'analysis.json').read_text())
                        ['boot_rom_digest_warning_observed'])

    def test_boot_banner_without_prompt_never_sends_a_command(self):
        completed, report, _, state = self.run_capture('missing-prompt', 'missing-prompt')
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(report['failure_stage'], 'fresh-boot')
        self.assertNotIn('fresh_boot_observed', report)
        self.assertEqual(state['commands'], [])

    def test_missing_banner_never_sends_a_command(self):
        completed, report, output, state = self.run_capture('missing-banner', 'missing-banner')
        self.assertEqual(completed.returncode, 1)
        self.assertFalse(report['capture_complete'])
        self.assertEqual(report['failure_stage'], 'fresh-boot')
        self.assertIn('external console deadline', report['error'])
        self.assertNotIn('fresh_boot_observed', report)
        self.assertEqual(state['commands'], [])
        self.assertIn('FAKE boot', (output / 'console.log').read_text())
        self.assertFalse((output / 'analysis.json').exists())

    def test_external_timeout_terminates_owned_child_and_keeps_partial_console(self):
        completed, report, output, state = self.run_capture('timeout', 'timeout')
        self.assertEqual(completed.returncode, 1)
        self.assertFalse(report['capture_complete'])
        self.assertEqual(report['failure_stage'], 'command')
        self.assertIn('external console deadline', report['error'])
        self.assertIsNone(report['emulator_returncode_before_cleanup'])
        self.assertEqual(state['commands'], ['std_channel_perf latency 2 1'])
        self.assertIn('FAKE fixture remains active', (output / 'console.log').read_text())
        self.assertFalse((output / 'analysis.json').exists())

    def test_stale_image_identity_and_wrong_runtime_mode_are_rejected(self):
        stale = fixture()
        stale[0]['build_id'] = 'c' * 64
        cases = [
            ('stale-image', stale, 'running firmware build identity mismatch'),
            ('wrong-mode', fixture('witness-published'), 'unexpected command mode'),
        ]
        for name, rows, error in cases:
            with self.subTest(case=name):
                completed, report, output, state = self.run_capture(name, records=transcript(*rows))
                self.assertEqual(completed.returncode, 1)
                self.assertFalse(report['capture_complete'])
                self.assertEqual(report['failure_stage'], 'analysis')
                self.assertIn(error, report['error'])
                self.assertEqual(state['commands'], ['std_channel_perf latency 2 1'])
                self.assertIn('STD_CHANNEL_PERF ', (output / 'console.log').read_text())
                self.assertFalse((output / 'analysis.json').exists())


if __name__ == '__main__':
    unittest.main()

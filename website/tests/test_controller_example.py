"""Reject drifted source and false controller completion claims without firmware."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import controller_example as controller


class PublishedSourceTests(unittest.TestCase):
    def test_published_source_and_commands_match_the_executed_package(self):
        receipts = controller.check_sources()
        for path in ('website/examples/controller/Cargo.toml',
                     'website/examples/controller/Cargo.lock',
                     'website/examples/controller/src/lib.rs',
                     'website/examples/controller/src/main.rs',
                     'tests/profiles/esp32s3-qemu-led-mock.toml'):
            self.assertIn(path, receipts)
            self.assertEqual(len(receipts[path]['sha256']), 64)
            self.assertGreater(receipts[path]['bytes'], 0)

    def test_drifted_full_source_and_library_excerpt_are_rejected(self):
        original = controller.block
        for changed in ('controller-main', 'controller-admit'):
            def altered(page, tag, language):
                value = original(page, tag, language)
                return value + '// edited independently of the executable\n' if tag == changed else value
            with self.subTest(tag=changed), patch('controller_example.block', side_effect=altered):
                with self.assertRaisesRegex(ValueError, 'published source differs'):
                    controller.check_sources()

    def test_host_requires_explicit_mock_and_target_requires_led_profile(self):
        original = controller.block
        for changed, old, new in (
            ('controller-host', 'NXRS_PLATFORM=mock ', ''),
            ('controller-build', 'esp32s3-qemu-led-mock', 'esp32s3-qemu-mock'),
        ):
            def altered(page, tag, language):
                value = original(page, tag, language)
                return value.replace(old, new) if tag == changed else value
            with self.subTest(tag=changed), patch('controller_example.block', side_effect=altered):
                with self.assertRaisesRegex(ValueError, 'qualified command'):
                    controller.check_sources()

    def test_changed_shutdown_output_is_rejected(self):
        original = controller.block
        def altered(page, tag, language):
            value = original(page, tag, language)
            return value.replace('stopped: off', 'stopped: on') if tag == 'controller-output' else value
        with patch('controller_example.block', side_effect=altered):
            with self.assertRaisesRegex(ValueError, 'exercised behavior'):
                controller.check_sources()


class SourceRegionTests(unittest.TestCase):
    def test_region_dedents_methods_and_excludes_markers(self):
        source = ('impl Demo {\n'
                  '    // tutorial:example:start\n'
                  '    fn run() {\n'
                  '        work();\n'
                  '    }\n'
                  '    // tutorial:example:end\n'
                  '}\n')
        self.assertEqual(controller.source_region(source, 'example'),
                         'fn run() {\n    work();\n}')

    def test_missing_duplicate_and_reversed_markers_fail(self):
        region = '// tutorial:example:start\nwork();\n// tutorial:example:end\n'
        for source in (region.replace('// tutorial:example:end', ''),
                       region + region,
                       '// tutorial:example:end\nwork();\n// tutorial:example:start\n'):
            with self.subTest(source=source), self.assertRaises(ValueError):
                controller.source_region(source, 'example')


class HostResultTests(unittest.TestCase):
    def result(self, stdout, *, status=0, stderr=''):
        return subprocess.CompletedProcess(['controller'], status, stdout, stderr)

    def test_error_requires_cleanup_and_failure_exit(self):
        good = self.result('\n'.join(controller.INVALID_OUTPUT) + '\n', status=1,
                           stderr=controller.INVALID_ERROR + '\n')
        controller.check_host_response(good, controller.INVALID_OUTPUT, 1, controller.INVALID_ERROR)
        cases = [
            self.result(good.stdout, status=0, stderr=good.stderr),
            self.result(good.stdout.replace('stopped: off\n', ''), status=1, stderr=good.stderr),
            self.result(good.stdout, status=1),
            self.result(good.stdout, status=1, stderr=good.stderr * 2),
        ]
        for result in cases:
            with self.subTest(result=result), self.assertRaises(ValueError):
                controller.check_host_response(result, controller.INVALID_OUTPUT, 1, controller.INVALID_ERROR)

    def test_status_is_not_an_acknowledgement_of_another_command(self):
        wrong = '\n'.join(controller.DEFAULT_OUTPUT).replace('status: on', 'status: off') + '\n'
        with self.assertRaises(ValueError):
            controller.check_host_response(self.result(wrong), controller.DEFAULT_OUTPUT)


class ConsoleResponseTests(unittest.TestCase):
    def test_exact_multiline_output_and_only_exact_echo_are_accepted(self):
        output = '\r\r\n'.join(controller.DEFAULT_OUTPUT)
        for prefix in ('controller', ' \x1b[Kcontroller', ''):
            with self.subTest(prefix=prefix):
                controller.validate_console_response(prefix + '\r\r\n' + output + '\r\r\nnsh>',
                                                     'controller', controller.DEFAULT_OUTPUT)
        controller.validate_console_response('echo $?\r\n1\r\nnsh>', 'echo $?', ['1'])

    def test_incomplete_wrong_repeated_or_escaped_output_fails(self):
        valid = 'controller\n' + '\n'.join(controller.DEFAULT_OUTPUT) + '\nnsh>'
        for text in (
            'controller\nnsh>',
            valid.removesuffix('nsh>'),
            valid.replace('status: on', 'status: off'),
            valid.replace('off: off\n', ''),
            valid.replace('stopped: off', 'stopped: off\nstopped: off'),
            valid.replace('stopped: off', '\x1b[Kstopped: off'),
            valid.replace('controller\n', '\x1b[2Kcontroller\n'),
            valid.replace('nsh>', 'panic\nnsh>'),
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                controller.validate_console_response(text, 'controller', controller.DEFAULT_OUTPUT)

    def test_missing_or_wrong_exit_status_fails(self):
        for text in ('echo $?\nnsh>', 'echo $?\n0\nnsh>', 'echo $?\n1\n1\nnsh>'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                controller.validate_console_response(text, 'echo $?', ['1'])


@unittest.skipUnless(os.name == 'posix', 'target console uses Unix PTYs')
class BootRejectionTests(unittest.TestCase):
    def boot_with_console(self, *, bad_status=False, missing_status=False,
                          fatal=False, changed_image=False, exited=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / 'firmware.bin'
            image.write_bytes(b'qualified image')
            closed = []
            commands = []

            class Process:
                def poll(self):
                    return 1 if exited else None

            class Console:
                raw = bytearray(b'assertion failed' if fatal else b'clean transcript')
                process = Process()

                def __init__(self, args, log):
                    self.last = ''

                def until(self, marker, seconds):
                    return 'nsh>'

                def shell(self, command):
                    commands.append(command)
                    if command == 'echo $?':
                        expected = '1' if self.last == 'controller on invalid' else '0'
                        if bad_status:
                            expected = '0'
                        return command + '\n' + ('' if missing_status else expected + '\n') + 'nsh>'
                    self.last = command
                    expected = {
                        'controller': controller.DEFAULT_OUTPUT,
                        'controller on status stop': controller.STOP_OUTPUT,
                        'controller on invalid': controller.INVALID_OUTPUT + [controller.INVALID_ERROR],
                    }[command]
                    return command + '\n' + '\n'.join(expected) + '\nnsh>'

                def close(self):
                    closed.append(True)
                    if changed_image:
                        image.write_bytes(b'mutated image')

            with patch('controller_example.ROOT', root), \
                 patch('first_firmware.Console', Console), \
                 patch('first_firmware.verify_identity'):
                try:
                    result = controller.boot(root / 'qemu', image, root / 'console.log')
                finally:
                    self.assertEqual(closed, [True], 'all target exit paths must release the emulator')
            return result, commands

    def test_success_requires_repeated_execution_and_explicit_error_status(self):
        result, commands = self.boot_with_console()
        self.assertEqual(result['result'], 'passed')
        self.assertEqual(commands.count('controller'), 2)
        self.assertEqual(commands.count('echo $?'), 4)
        self.assertEqual([case['exit_status'] for case in result['invocations']], [0, 0, 1, 0])

    def test_false_success_console_and_image_controls_fail_and_cleanup(self):
        for options in ({'bad_status': True}, {'missing_status': True}, {'fatal': True},
                        {'changed_image': True}, {'exited': True}):
            with self.subTest(options=options), self.assertRaises((ValueError, RuntimeError)):
                self.boot_with_console(**options)


if __name__ == '__main__':
    unittest.main()

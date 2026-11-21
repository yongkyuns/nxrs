"""Reject drifted source and false controller completion claims without firmware."""
from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import Mock, patch

import controller_example as controller

STATS_LINE = ('stats: observed=8 rejected=0 processed=7 gaps=0 max_dispatch_us=42 '
              'last_interval_us=40000 fault=none watchdog_fault=none')


def observed_output():
    return [STATS_LINE if line == controller.STATS_MARKER else line
            for line in controller.OBSERVATION_OUTPUT]


class PublishedSourceTests(unittest.TestCase):
    def test_published_source_and_commands_match_the_executed_package(self):
        receipts = controller.check_sources()
        for path in ('website/examples/controller/Cargo.toml',
                     'website/examples/controller/Cargo.lock',
                     'website/examples/controller/src/lib.rs',
                     'website/examples/controller/src/main.rs',
                     'tests/profiles/esp32s3-qemu-pulse-mock.toml'):
            self.assertIn(path, receipts)
            self.assertEqual(len(receipts[path]['sha256']), 64)
            self.assertGreater(receipts[path]['bytes'], 0)

    def test_every_published_library_excerpt_rejects_source_drift(self):
        original = controller.block
        for changed in ('controller-commands', 'controller-start',
                        'controller-admit', 'controller-stop'):
            def altered(page, tag, language):
                value = original(page, tag, language)
                return value + '// edited independently of the executable\n' if tag == changed else value
            with self.subTest(tag=changed), patch('controller_example.block', side_effect=altered):
                with self.assertRaisesRegex(ValueError, 'published source differs'):
                    controller.check_sources()

    def test_host_requires_explicit_mock_and_target_requires_pulse_profile(self):
        original = controller.block
        for changed, old, new in (
            ('controller-host', 'NXRS_PLATFORM=mock ', ''),
            ('controller-build', 'esp32s3-qemu-pulse-mock', 'esp32s3-qemu-mock'),
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


class ObservationTests(unittest.TestCase):
    def test_statistics_allow_in_flight_receipts_without_an_exact_rate_claim(self):
        stats = controller.parse_stats(STATS_LINE)
        self.assertEqual(stats['processed'], 7)
        self.assertEqual(stats['observed'], 8)
        self.assertEqual(stats['last_interval_us'], 40000)
        single = STATS_LINE.replace('processed=7', 'processed=1').replace('40000', 'none')
        self.assertIsNone(controller.parse_stats(single)['last_interval_us'])
        lossy = STATS_LINE.replace('rejected=0 processed=7 gaps=0',
                                  'rejected=3 processed=4 gaps=2')
        self.assertEqual(controller.parse_stats(lossy)['gaps'], 2)

    def test_absent_malformed_faulted_or_inconsistent_statistics_fail(self):
        bad = [
            STATS_LINE.replace('processed=7', 'processed=0'),
            STATS_LINE.replace('processed=7', 'processed=9'),
            STATS_LINE.replace('rejected=0', 'rejected=2'),
            STATS_LINE.replace('gaps=0', 'gaps=1'),
            STATS_LINE.replace('observed=8', f'observed={1 << 64}'),
            STATS_LINE.replace('40000', 'none'),
            STATS_LINE.replace('processed=7', 'processed=1'),
            STATS_LINE.replace('fault=none', 'fault=Io', 1),
            STATS_LINE.replace('watchdog_fault=none', 'watchdog_fault=Io'),
            STATS_LINE.replace('max_dispatch_us=42', 'max_dispatch_us=-1'),
            STATS_LINE + ' extra=1',
            STATS_LINE.replace(' rejected=0', ''),
        ]
        for line in bad:
            with self.subTest(line=line), self.assertRaises(ValueError):
                controller.parse_stats(line)

    def test_statistics_must_occupy_exactly_the_expected_output_line(self):
        lines = observed_output()
        self.assertEqual(controller.check_output(lines, controller.OBSERVATION_OUTPUT)['processed'], 7)
        for wrong in (lines[:-1], lines + [STATS_LINE],
                      lines[:2] + ['no observations'] + lines[3:]):
            with self.subTest(lines=wrong), self.assertRaises(ValueError):
                controller.check_output(wrong, controller.OBSERVATION_OUTPUT)


class InteractiveResultTests(unittest.TestCase):
    def transcript(self):
        return ('ready: off\ncontroller> set 25 50: on (25 Hz, 50%)\n'
                'controller> controller> ' + STATS_LINE + '\n'
                'controller> off: off\ncontroller> stopped: off\n')

    def test_interactive_reconfiguration_observation_and_stop(self):
        result = subprocess.CompletedProcess(['controller', '--interactive'], 0, self.transcript(), '')
        self.assertEqual(controller.check_interactive_response(result)['processed'], 7)

    def test_missing_prompt_state_observation_or_cleanup_fails(self):
        text = self.transcript()
        for invalid in (text.replace('controller> controller> ', 'controller> '),
                        text.replace('50%)', '25%)'),
                        text.replace(STATS_LINE, 'stats: no data'),
                        text.replace('controller> stopped: off\n', '')):
            result = subprocess.CompletedProcess(['controller', '--interactive'], 0, invalid, '')
            with self.subTest(output=invalid), self.assertRaises(ValueError):
                controller.check_interactive_response(result)
        with self.assertRaises(ValueError):
            controller.check_interactive_response(subprocess.CompletedProcess(['controller'], 1, text, ''))


class DetachedProjectTests(unittest.TestCase):
    def test_relocation_preserves_source_and_lock_and_only_rewrites_sdk_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'controller'
            hashes = controller.copy_detached_project(destination)
            self.assertEqual(hashes, controller.project_hashes(controller.PROJECT))
            self.assertEqual(hashes, controller.project_hashes(destination))
            original = tomllib.loads((controller.PROJECT / 'Cargo.toml').read_text())
            copied = tomllib.loads((destination / 'Cargo.toml').read_text())
            for dependency in original['dependencies'].values():
                if isinstance(dependency, dict) and 'path' in dependency:
                    dependency['path'] = str((controller.PROJECT / dependency['path']).resolve())
            self.assertEqual(copied, original)
            with self.assertRaises(FileExistsError):
                controller.copy_detached_project(destination)

    def test_detached_execution_pins_sdk_and_rejects_source_mutation(self):
        for mutate in (False, True):
            def invoke(args, env, *, cwd):
                self.assertEqual(env['NXRS_SDK_ROOT'], str(controller.ROOT.resolve()))
                self.assertEqual(env['NXRS_PLATFORM'], 'mock')
                self.assertEqual(Path(args[args.index('--manifest-path') + 1]), cwd / 'Cargo.toml')
                self.assertFalse(cwd.is_relative_to(controller.ROOT))
                if mutate:
                    with (cwd / 'src/main.rs').open('a') as source:
                        source.write('// unintended detached edit\n')
                return subprocess.CompletedProcess(args, 0, '\n'.join(observed_output()) + '\n', '')
            with self.subTest(mutate=mutate), patch('controller_example.invoke', side_effect=invoke):
                if mutate:
                    with self.assertRaisesRegex(ValueError, 'changed during detached execution'):
                        controller.detached_check({})
                else:
                    result = controller.detached_check({})
                    self.assertEqual(result['result'], 'passed')
                    self.assertEqual(result['statistics']['processed'], 7)


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
                        'controller on wait 300 stats off': observed_output(),
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
        self.assertEqual(commands.count('echo $?'), 5)
        self.assertEqual([case['exit_status'] for case in result['invocations']], [0, 0, 0, 1, 0])
        self.assertEqual(result['invocations'][2]['statistics']['processed'], 7)

    def test_false_success_console_and_image_controls_fail_and_cleanup(self):
        for options in ({'bad_status': True}, {'missing_status': True}, {'fatal': True},
                        {'changed_image': True}, {'exited': True}):
            with self.subTest(options=options), self.assertRaises((ValueError, RuntimeError)):
                self.boot_with_console(**options)


def write_json(path, value):
    path.write_text(json.dumps(value) + '\n')


def candidate_ledger(directory):
    """Model the build receipt's old Linux paths, including legal duplicates."""
    original = '/old/ci/nxrs/target/controller'
    lines = [hashlib.sha256((directory / name).read_bytes()).hexdigest()
             + '  ' + original + '/' + name for name in controller.CANDIDATE_FILES]
    manifest = (hashlib.sha256((controller.PROJECT / 'Cargo.toml').read_bytes()).hexdigest()
                + '  /old/ci/nxrs/' + controller.MANIFEST)
    lines += [manifest, manifest, 'a' * 64 + '  ' + original + '/nuttx/include/nuttx/config.h',
              'b' * 64 + '  /not/available/cargo/release/controller']
    (directory / 'binaries.sha256').write_text('\n'.join(lines) + '\n')


def candidate_fixture(directory, profile='wroom-pulse'):
    """A retained CI layout: no NuttX source tree, compiler or copied application."""
    (directory / 'nuttx').mkdir(parents=True)
    (directory / 'nuttx/nuttx').write_bytes(b'candidate ELF')
    (directory / 'nuttx/nuttx.bin').write_bytes(b'candidate binary')
    config = b'CONFIG_DEV_GPIO=y\nCONFIG_NSH_MAXARGUMENTS=7\n'
    (directory / 'resolved.config').write_bytes(config)
    config_hash = hashlib.sha256(config).hexdigest()
    target, board = controller.HARDWARE_PROFILES[profile]
    manifest = '/old/ci/nxrs/' + controller.MANIFEST
    resolved = {
        'version': 1, 'workspace': '/old/ci/nxrs', 'product_manifest': manifest,
        'profile': profile, 'os': 'nuttx', 'target': target,
        'semantic_digest': 'c' * 64, 'raw_fingerprint': 'd' * 64,
        'data': {'name': profile, 'target': target, 'board': board, 'image': 'nuttx.bin',
                 'requires-qemu': False, 'composition': {'mode': 'hardware', 'version': 1},
                 'hal': {name: 'nuttx' for name in ('pulse', 'boot', 'watchdog')},
                 'kconfig': {'require': ['CONFIG_DEV_GPIO=y'],
                             'forbid-regex': '^CONFIG_SMP=y$'}},
    }
    provider = {
        'app_manifest': manifest, 'app_package': 'nxrs-controller-demo', 'platform': profile,
        'target_os': 'nuttx', 'nuttx-build-mode': 'flat',
        'dependency_resolution': {'status': 'resolved'},
        'providers': [{'capability': name, 'implementation': 'nuttx', 'package': f'nxrs-{name}'}
                      for name in ('pulse', 'boot', 'watchdog')],
        'resolved-composition': {'graph': {'mode': 'hardware', 'version': 1},
                                 'semantic-digest': 'c' * 64, 'input-fingerprint': 'd' * 64},
    }
    abi = {'errors': [], 'probe_abi_compatible': True, 'poll_abi_compatible': True,
           'general_signal_api_qualified': False,
           'checks': [{'name': 'int.size', 'status': 'compatible'},
                      {'name': 'SIG_IGN', 'status': 'known-unsupported-SIG_IGN'}],
           'target': target, 'config_sha256': config_hash, 'config_header_sha256': 'a' * 64,
           'headers_sha256': '9' * 64}
    native = {'schema': 1, 'target': target, 'effective_config_sha256': config_hash,
              'config_sha256': 'a' * 64, 'headers_sha256': '9' * 64}
    for name, value in (('resolved-profile.json', resolved), ('provider-selection.json', provider),
                        ('abi-report.json', abi), ('native-context.json', native),
                        ('kconfig-requests.json', {'effective_config_sha256': config_hash})):
        write_json(directory / name, value)
    (directory / 'provenance.txt').write_text(
        f'app_package=nxrs-controller-demo\napp_bin=controller\napp_command=controller\n'
        f'platform={profile}\ntarget={target}\nboard={board}\nabi_profile=active\n'
        'nuttx_build_mode=flat\nnxrs=' + 'e' * 40 + '\nnxrs_dirty=yes\n')
    candidate_ledger(directory)
    return directory


class SerialBuildTests(unittest.TestCase):
    def test_both_native_profiles_and_relocated_ci_layout_pass_without_original_paths(self):
        for profile in controller.HARDWARE_PROFILES:
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                original = candidate_fixture(root / 'original', profile)
                relocated = root / 'download'
                shutil.copytree(original, relocated)
                shutil.rmtree(original)
                result = controller.validate_device_build(relocated)
                self.assertEqual(result['profile'], profile)
                self.assertFalse(result['installed_image_verified'])
                self.assertFalse(result['compiled_source_identity_verified'])
                self.assertEqual(result['build_provenance']['nxrs_dirty'], 'yes')
                self.assertIn('src/main.rs', result['current_checkout_sources'])
                self.assertFalse((relocated / 'nuttx/include/nuttx/config.h').exists())

    def test_declared_elf_or_binary_hash_matches_only_candidate_and_remains_unverified(self):
        with tempfile.TemporaryDirectory() as directory:
            build = candidate_fixture(Path(directory) / 'build')
            for name in ('nuttx/nuttx', 'nuttx/nuttx.bin'):
                digest = hashlib.sha256((build / name).read_bytes()).hexdigest()
                result = controller.validate_device_build(build, digest.upper())
                self.assertEqual(result['declared_installed_image_sha256'], digest)
                self.assertFalse(result['installed_image_verified'])
            for wrong in ('f' * 64, 'invalid'):
                with self.assertRaisesRegex(ValueError, 'declared installed-image hash'):
                    controller.validate_device_build(build, wrong)

    def test_checkout_crlf_conversion_is_allowed_without_normalizing_candidate_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build = candidate_fixture(root / 'build')
            checkout = root / 'checkout'
            checkout.mkdir()
            original = (controller.PROJECT / 'Cargo.toml').read_bytes()
            (checkout / 'Cargo.toml').write_bytes(original.replace(b'\n', b'\r\n'))
            with patch('controller_example.PROJECT', checkout):
                result = controller.validate_device_build(build)
                self.assertNotEqual(result['manifest']['sha256'], result['build_manifest_sha256'])
                (build / 'resolved.config').write_bytes(
                    (build / 'resolved.config').read_bytes().replace(b'\n', b'\r\n'))
                with self.assertRaisesRegex(ValueError, 'candidate hash mismatch'):
                    controller.validate_device_build(build)

    def test_invalid_provenance_is_rejected_before_open_with_failure_report(self):
        mutations = [
            ('resolved-profile.json', lambda x: x.update(profile='esp32s3-qemu-pulse-mock')),
            ('resolved-profile.json', lambda x: x.update(product_manifest='/other/Cargo.toml')),
            ('resolved-profile.json', lambda x: x['data']['composition'].update(mode='qualification')),
            ('resolved-profile.json', lambda x: x['data'].update(target='other-target')),
            ('resolved-profile.json', lambda x: x.update(semantic_digest='f' * 64)),
            ('resolved-profile.json', lambda x: x['data']['kconfig'].update(require=['CONFIG_ABSENT=y'])),
            ('provider-selection.json', lambda x: x.update(app_package='nxrs-another-app')),
            ('provider-selection.json', lambda x: x['providers'][0].update(implementation='mock')),
            ('provider-selection.json', lambda x: x['resolved-composition']['graph'].update(mode='mock')),
            ('provider-selection.json', lambda x: x['dependency_resolution'].update(status='failed')),
            ('native-context.json', lambda x: x.update(effective_config_sha256='f' * 64)),
            ('native-context.json', lambda x: x.update(config_sha256='f' * 64)),
            ('native-context.json', lambda x: x.update(headers_sha256='f' * 64)),
            ('abi-report.json', lambda x: x.update(probe_abi_compatible=False)),
            ('abi-report.json', lambda x: x.update(poll_abi_compatible='true')),
            ('abi-report.json', lambda x: x.update(errors=['mismatch'])),
            ('abi-report.json', lambda x: x.update(config_header_sha256='f' * 64)),
            ('abi-report.json', lambda x: x['checks'][0].update(status='incompatible')),
            ('abi-report.json', lambda x: x['checks'][0].pop('status')),
            ('abi-report.json', lambda x: x['checks'][0].update(status='known-unsupported-SIG_IGN')),
            ('abi-report.json', lambda x: x['checks'][1].update(name='some-other-symbol')),
            ('abi-report.json', lambda x: x.update(headers_sha256='f' * 64)),
            ('kconfig-requests.json', lambda x: x.update(effective_config_sha256='f' * 64)),
        ]
        for name, mutate in mutations:
            with self.subTest(name=name, mutate=mutate), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                build = candidate_fixture(root / 'build')
                value = json.loads((build / name).read_text())
                mutate(value)
                write_json(build / name, value)
                candidate_ledger(build)  # Test semantic checks independently of hash mismatches.
                factory = Mock(side_effect=AssertionError('invalid candidate must not open serial'))
                with redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
                    controller.serial_checks('COM5', build, out=root / 'report', console_factory=factory)
                factory.assert_not_called()
                report = json.loads((root / 'report/report.json').read_text())
                self.assertEqual(report['result'], 'failed')
                self.assertEqual(report['invocations'], [])
                self.assertEqual((root / 'report/console.log').read_bytes(), b'')

    def test_corrupt_missing_ambiguous_or_mixed_root_artifacts_fail(self):
        def corrupt(build):
            (build / 'nuttx/nuttx.bin').write_bytes(b'changed image')
        def missing(build):
            (build / 'native-context.json').unlink()
        def conflict(build):
            with (build / 'binaries.sha256').open('a') as ledger:
                ledger.write('f' * 64 + '  /old/ci/nxrs/' + controller.MANIFEST + '\n')
        def ambiguous(build):
            with (build / 'binaries.sha256').open('a') as ledger:
                ledger.write('f' * 64 + '  /other/build/nuttx/nuttx.bin\n')
        def mixed(build):
            path = build / 'binaries.sha256'
            path.write_text(path.read_text().replace('/old/ci/nxrs/target/controller/nuttx/nuttx.bin',
                                                    '/other/build/nuttx/nuttx.bin'))
        def provenance(build):
            with (build / 'provenance.txt').open('a') as value:
                value.write('app_command=other\n')
        def missing_revision(build):
            path = build / 'provenance.txt'
            path.write_text(path.read_text().replace('nxrs=' + 'e' * 40, 'nxrs='))
        def missing_dirty(build):
            path = build / 'provenance.txt'
            path.write_text(path.read_text().replace('nxrs_dirty=yes', 'nxrs_dirty=unknown'))
        for mutate in (corrupt, missing, conflict, ambiguous, mixed, provenance, missing_revision, missing_dirty):
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as directory:
                build = candidate_fixture(Path(directory) / 'build')
                mutate(build)
                with self.assertRaises((ValueError, FileNotFoundError)):
                    controller.validate_device_build(build)

    @unittest.skipUnless(os.name == 'posix', 'symlink fixture requires Unix')
    def test_candidate_symlink_cannot_escape_the_allowed_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build = candidate_fixture(root / 'build')
            image = build / 'nuttx/nuttx.bin'
            outside = root / 'image.bin'
            image.rename(outside)
            image.symlink_to(outside)
            with self.assertRaisesRegex(ValueError, 'escapes the build directory'):
                controller.validate_device_build(build)


class ControlledSerial:
    """Protocol fixture; the separate shared-helper tests cover actual byte I/O."""
    def __init__(self, port, log, *, timeout, transform=None, close_action=None):
        self.log, self.timeout = log, timeout
        self.transform, self.close_action = transform, close_action
        self.raw = bytearray()
        self.commands = []
        self.closed = 0
        self.last_status = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed += 1
        self.log.write_bytes(self.raw)
        if self.close_action:
            self.close_action()

    def synchronize(self):
        self.raw.extend(b'nsh>')

    def shell(self, command):
        self.commands.append(command)
        self.raw.extend((command + '\r\n').encode())
        if command == 'echo $?':
            lines = [str(self.last_status)]
        elif len(shlex.split(command)) > 7:
            self.last_status = 1
            lines = ['nsh: too many arguments']
        else:
            self.last_status = 1 if command == 'controller on invalid' else 0
            lines = {
                'uname': ['NuttX'],
                'controller': controller.DEFAULT_OUTPUT,
                'controller on status stop': controller.STOP_OUTPUT,
                'controller on wait 300 stats off': observed_output(),
                'controller on invalid': controller.INVALID_OUTPUT + [controller.INVALID_ERROR],
                'controller set 25 50 wait 300 stats': [
                    'ready: off', 'set 25 50: on (25 Hz, 50%)', STATS_LINE, 'stopped: off'],
                'controller --watchdog 3000 on wait 1000 stats': [
                    'ready: off', 'on: on (25 Hz, 25%)', STATS_LINE, 'stopped: off'],
                'controller --reset-cause': ['reset cause: Unknown'],
                'free': ['total used free largest', '4096 1024 3072 2048'],
                'ps': ['PID PRI POLICY TYPE SIZE STKUSED COMMAND', '0 0 FIFO Kthread 1024 400 idle'],
            }[command]
        response = command + '\r\n' + '\r\n'.join(lines) + '\r\nnsh>'
        if self.transform:
            response = self.transform(command, response)
        self.raw.extend(response.encode())
        return response


class SerialSessionTests(unittest.TestCase):
    def run_serial(self, root, *, repeat=2, watchdog=False, transform=None, close_action=None, out=None):
        build = candidate_fixture(root / 'build')
        consoles = []
        def factory(*args, **kwargs):
            console = ControlledSerial(*args, **kwargs, transform=transform, close_action=close_action)
            consoles.append(console)
            return console
        try:
            with redirect_stdout(io.StringIO()):
                report = controller.serial_checks('COM5', build, repeat=repeat, watchdog=watchdog,
                                                   out=out or root / 'report', console_factory=factory)
        finally:
            self.assertEqual(len(consoles), 1)
            self.assertEqual(consoles[0].closed, 1, 'every session exit must close the console')
            self.assertEqual((root / 'report/console.log').read_bytes(), consoles[0].raw)
        return report, consoles[0]

    def test_success_records_candidate_observations_repetition_and_resource_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report, console = self.run_serial(root, repeat=3, watchdog=True)
            self.assertEqual(report['result'], 'passed')
            self.assertEqual(report['scope'], 'serial-console acceptance')
            self.assertFalse(report['installed_image_verified'])
            self.assertEqual(len(report['invocations']), 10)
            self.assertEqual(console.commands.count('controller on wait 300 stats off'), 4)
            self.assertEqual(console.commands.count('controller'), 2)
            self.assertEqual(console.commands.count('free'), 2)
            self.assertEqual(console.commands.count('ps'), 2)
            self.assertEqual(console.commands[0], 'uname')
            self.assertEqual(report['invocations'][3]['exit_status'], 1)
            self.assertEqual(report['invocations'][5]['statistics']['processed'], 7)
            self.assertIn('no heap or stack threshold', report['resources']['assessment'])
            self.assertEqual(json.loads((root / 'report/report.json').read_text()), report)

    def test_optional_watchdog_and_all_cases_fit_the_real_nsh_argument_limit(self):
        for command, _, _ in controller.CONSOLE_CASES + [controller.RECONFIGURE_CASE,
                                                        controller.WATCHDOG_CASE]:
            self.assertLessEqual(len(shlex.split(command)), 7, command)
        with tempfile.TemporaryDirectory() as directory:
            _, console = self.run_serial(Path(directory))
            self.assertFalse(any('--watchdog' in command for command in console.commands))

    def test_bad_output_status_statistics_fatal_reset_or_timeout_preserves_failure_and_closes(self):
        def replace(command, old, new):
            return lambda sent, response: response.replace(old, new) if sent == command else response
        def timeout(sent, response):
            if sent == 'controller on wait 300 stats off':
                raise TimeoutError('injected serial timeout')
            return response
        transforms = [
            replace('uname', 'NuttX', 'echo NuttX'),
            replace('controller', 'status: on', 'status: off'),
            replace('controller on invalid', 'stopped: off\r\n', ''),
            replace('echo $?', '\r\n1\r\n', '\r\n0\r\n'),
            replace('controller on wait 300 stats off', 'processed=7', 'processed=0'),
            replace('controller on wait 300 stats off', 'nsh>', ''),
            replace('free', 'total used free largest', 'panic'),
            replace('controller --reset-cause', 'Unknown', 'InventedCause'),
            timeout,
        ]
        for transform in transforms:
            with self.subTest(transform=transform), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with self.assertRaises((ValueError, RuntimeError, TimeoutError)):
                    self.run_serial(root, transform=transform)
                report = json.loads((root / 'report/report.json').read_text())
                self.assertEqual(report['result'], 'failed')
                self.assertIn('error', report)
                self.assertGreater((root / 'report/console.log').stat().st_size, 0)

    def test_close_failure_and_candidate_mutation_cannot_report_success(self):
        for change in ('close-error', 'image'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                def close_action():
                    if change == 'close-error':
                        raise RuntimeError('injected close failure')
                    (root / 'build/nuttx/nuttx.bin').write_bytes(b'changed after execution')
                with self.assertRaises((ValueError, RuntimeError)):
                    self.run_serial(root, close_action=close_action)
                self.assertEqual(json.loads((root / 'report/report.json').read_text())['result'], 'failed')

    def test_bad_options_existing_output_and_build_output_collision_fail_before_open(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build = candidate_fixture(root / 'build')
            existing = root / 'existing'
            existing.mkdir()
            marker = existing / 'keep'
            marker.write_text('retained')
            factory = Mock(side_effect=AssertionError('input validation must precede opening'))
            cases = [{'repeat': 0}, {'repeat': 101}, {'repeat': True}, {'port': ''},
                     {'out': existing}, {'out': build / 'report'}]
            for options in cases:
                kwargs = {'port': 'COM5', 'build_dir': build, 'out': root / 'report',
                          'console_factory': factory, **options}
                with self.subTest(options=options), self.assertRaises((ValueError, FileExistsError)):
                    controller.serial_checks(**kwargs)
            factory.assert_not_called()
            self.assertEqual(marker.read_text(), 'retained')
            self.assertFalse((build / 'report').exists())

    def test_default_output_directory_is_unique_and_no_toolchain_is_used(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build = candidate_fixture(root / 'build')
            with patch('controller_example.REPORT', root / 'reports'), \
                 patch('controller_example.environment', side_effect=AssertionError('no toolchain')), \
                 patch('controller_example.invoke', side_effect=AssertionError('no Cargo')), \
                 redirect_stdout(io.StringIO()):
                for _ in range(2):
                    controller.serial_checks('COM5', build, repeat=1, console_factory=ControlledSerial)
            reports = list((root / 'reports').glob('serial-*/report.json'))
            self.assertEqual(len(reports), 2)
            self.assertTrue(all(json.loads(path.read_text())['result'] == 'passed' for path in reports))


class SerialCliTests(unittest.TestCase):
    def test_serial_dispatch_skips_host_source_and_cargo_workflows(self):
        with patch('controller_example.serial_checks') as serial, \
             patch('controller_example.environment', side_effect=AssertionError('no toolchain')), \
             patch('controller_example.check_sources', side_effect=AssertionError('no host checks')), \
             redirect_stdout(io.StringIO()):
            controller.main(['--serial-port', 'COM5', '--build-dir', 'download', '--watchdog'])
        serial.assert_called_once_with('COM5', Path('download'), repeat=10, watchdog=True,
                                       out=None, installed_image_sha256=None)

    def test_serial_only_options_and_conflicting_modes_are_rejected(self):
        for args in (['--serial-port', 'COM5'], ['--build-dir', 'download'], ['--watchdog'],
                     ['--repeat', '10'], ['--out', 'report'],
                     ['--target', '--serial-port', 'COM5']):
            with self.subTest(args=args), patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit):
                controller.main(args)


if __name__ == '__main__':
    unittest.main()

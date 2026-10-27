from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from run_transport_matrix import build_command
from run_transport_paired import build_command as paired_build_command
import run_transport_matrix
import run_transport_paired
from transport_pipeline_report import CASES, validate_policy

class MatrixCommandTests(unittest.TestCase):
    def test_every_named_case_preserves_the_recorded_measurement_policy(self):
        for case, (_, language, ack_bytes) in CASES.items():
            with self.subTest(case=case):
                command = build_command(case, Path('/tree'), Path('/std'), Path('/cargo'),
                                        Path('/out'), Path('/baseline.config'))
                flags = lambda option: [command[i + 1] for i, value in enumerate(command) if value == option]
                # mq-backend is enabled transitively by the queue controls.
                build = {'features': [], 'experiment_case': case,
                         'c_defines': flags('--c-define'),
                         'app_opt_level': '2' if case.endswith('-2') else 'z',
                         'std_source': {'selected_source_matches': True}}
                validate_policy(build, case, language, ack_bytes)

    def test_thread_baseline_has_its_own_fresh_boot(self):
        with tempfile.TemporaryDirectory() as directory:
            argv = [run_transport_matrix.__file__, '--case', 'rust-owned-z',
                    '--measure', '--port', '/dev/test', '--tree', directory,
                    '--sysroot', directory, '--out-root', directory]
            with patch.object(sys, 'argv', argv), \
                    patch('run_transport_matrix.subprocess.run') as run:
                run_transport_matrix.main()
            self.assertEqual(run.call_count, 2)
            baseline = run.call_args_list[1].args[0]
            self.assertIn('cq_scale threads', baseline)
            self.assertNotIn('--skip-flash', baseline)

    def test_paired_controls_use_same_image_and_explicit_provider_policies(self):
        for kind in ('wire', 'packet', 'packet-inplace'):
            command = paired_build_command(kind, Path('/tree'), Path('/std'), Path('/cargo'), Path('/out'))
            self.assertEqual(command[command.index('--case')+1], 'paired-'+kind)
            self.assertNotIn('--feature', command)
            self.assertIn('NXRS_CQ_EMBED_CONTROL', command)
            self.assertIn('NXRS_CQ_SPEED', command)
            self.assertEqual('NXRS_CQ_WIRE_ONLY' in command, kind == 'wire')
            self.assertEqual('NXRS_CQ_PACKET_SERVICE' in command, kind != 'wire')
            self.assertEqual('NXRS_PAYLOAD_C_SPEED' in command, kind != 'wire')
            self.assertEqual(command[command.index('--app-opt-level') + 1], '2')

    def test_cpp_and_rust_wire_controls_have_matched_layout_and_kernel_scope(self):
        c = build_command('c-packet-wire-2', Path('/tree'), Path('/std'), Path('/cargo'),
                          Path('/out/c'), Path('/baseline/resolved.config'))
        rust = build_command('rust-packet-wire-2', Path('/tree'), Path('/std'), Path('/cargo'),
                             Path('/out/rust'), Path('/baseline/resolved.config'))
        self.assertIn('NXRS_CQ_PACKET_SERVICE', c)
        self.assertIn('NXRS_CQ_WIRE_ONLY', c)
        self.assertEqual(rust[rust.index('--app-opt-level') + 1], '2')
        self.assertIn('NXRS_CQ_SPEED', c)
        self.assertIn('--reuse-kernel', c)
        self.assertNotIn('NXRS_PAYLOAD_C_SPEED', c)

    def test_real_packet_work_reuses_existing_kernels_and_keeps_borrowed_io(self):
        c = build_command('c-packet-2', Path('/tree'), Path('/std'), Path('/cargo'),
                          Path('/out/c'), Path('/baseline/resolved.config'))
        rust = build_command('rust-packet-2', Path('/tree'), Path('/std'), Path('/cargo'),
                             Path('/out/rust'), Path('/baseline/resolved.config'))
        self.assertEqual(rust[rust.index('--case')+1], 'rust-packet-2')
        self.assertNotIn('NXRS_CQ_WIRE_ONLY', c)
        self.assertIn('NXRS_PAYLOAD_C_SPEED', c)
        self.assertTrue(any(path.endswith('/payload_processing.c') for path in c))
        self.assertTrue(any(path.endswith('/pipeline_processing.h') for path in c))

    def test_c_build_forwards_matched_source_profile_command_and_baseline(self):
        command = build_command('c-wire-2', Path('/tree'), Path('/std'), Path('/cargo'),
                                Path('/fresh/case'), Path('/explicit/baseline.config'))
        self.assertIn(str(Path(__file__).parent / 'channel_scale_mq.c'), command)
        self.assertIn('esp32s3-service-footprint', command)
        self.assertIn('cq_c_scale', command)
        self.assertEqual(command[command.index('--stack-size') + 1], '8192')
        self.assertEqual(command[command.index('--baseline-config') + 1],
                         '/explicit/baseline.config')
        self.assertEqual(command[command.index('--out') + 1], '/fresh/case')

    def test_rust_build_uses_demo_builder_and_explicit_tree_inputs(self):
        command = build_command('rust-packet-2', Path('/tree'), Path('/std'), Path('/cargo'),
                                Path('/fresh/rust'), None)
        self.assertTrue(command[1].endswith('/relink_rust.py'))
        self.assertEqual(command[command.index('--tree') + 1], '/tree')
        self.assertEqual(command[command.index('--sysroot') + 1], '/std')
        self.assertEqual(command[command.index('--cargo-target') + 1], '/cargo')
        self.assertEqual(command[command.index('--out') + 1], '/fresh/rust')
        self.assertIn('--target-c-source', command)
        self.assertTrue(any(path.endswith('/transport_gate.c') for path in command))

    def test_measurement_requires_port_in_both_drivers(self):
        for module, args in (
                (run_transport_matrix, ['--case', 'rust-owned-z', '--build', '--measure']),
                (run_transport_paired, ['--case', 'wire', '--measure'])):
            with tempfile.TemporaryDirectory() as directory:
                argv = [module.__file__, *args, '--tree', directory, '--sysroot', directory,
                        '--out-root', str(Path(directory) / 'output')]
                with patch.object(sys, 'argv', argv), self.assertRaises(SystemExit) as error:
                    module.main()
                self.assertEqual(error.exception.code, 2)

    def test_matrix_requires_explicit_baseline_config_for_c_build(self):
        with tempfile.TemporaryDirectory() as directory:
            argv = [run_transport_matrix.__file__, '--case', 'c-wire-2', '--build',
                    '--tree', directory, '--sysroot', directory,
                    '--out-root', str(Path(directory) / 'output')]
            with patch.object(sys, 'argv', argv), self.assertRaises(SystemExit) as error:
                run_transport_matrix.main()
            self.assertEqual(error.exception.code, 2)

if __name__ == '__main__': unittest.main()

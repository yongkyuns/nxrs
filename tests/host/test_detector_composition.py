"""Exercise actual product/build-script wiring without native SDKs or devices."""
from pathlib import Path
import os
import json
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class DetectorCompositionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.temporary.name)
        cls.executables = {}
        result = subprocess.run(['cargo', 'build', '--locked', '-p', 'nxrs-build-support', '-p', 'nxrs-detector-build-support', '--lib',
                                 '--message-format=json', '--target-dir', str(cls.directory / 'cargo')], cwd=ROOT, check=True,
                                capture_output=True, text=True, timeout=120)
        libraries = {record['target']['name']: Path(path) for line in result.stdout.splitlines()
                     if (record := json.loads(line)).get('reason') == 'compiler-artifact'
                     and record['target']['name'] in ('nxrs_build_support', 'nxrs_detector_build_support')
                     for path in record['filenames'] if path.endswith('.rlib')}
        library = libraries['nxrs_build_support']
        externs = [argument for name, path in libraries.items()
                   for argument in ('--extern', name + '=' + str(path))]
        for name, path in [('hal', 'tools/detector-build-support/cargo.rs'),
                           ('stream', 'tests/nuttx-std/stream/build.rs')]:
            executable = cls.directory / (name + ('.exe' if os.name == 'nt' else ''))
            subprocess.run(['rustc', '--edition=2024', str(ROOT / path), '-o', str(executable),
                            *externs, '-L', 'dependency=' + str(library.parent / 'deps'), '-Cpanic=abort'],
                           cwd=ROOT, check=True, capture_output=True, text=True, timeout=60)
            cls.executables[name] = executable

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def script(self, name, platform, features=()):
        manifest = ROOT / ('hal/detector' if name == 'hal' else 'tests/nuttx-std/stream')
        out = self.directory / 'out'
        out.mkdir(exist_ok=True)
        env = {**os.environ, 'NXRS_PLATFORM': platform, 'CARGO_MANIFEST_DIR': str(manifest),
               'CARGO_PKG_NAME': 'nxrs-detector', 'CARGO_CFG_TARGET_OS': 'nuttx',
               'TARGET': 'xtensa-esp32s3-nuttx', 'OUT_DIR': str(out)}
        for feature in ('PICO_MODEL', 'PERSON_MODEL', 'PROBE'):
            env.pop('CARGO_FEATURE_' + feature, None)
        env.update({'CARGO_FEATURE_' + feature: '1' for feature in features})
        result = subprocess.run([str(self.executables[name])], env=env, cwd=ROOT,
                                capture_output=True, text=True, timeout=15)
        return result, out

    def test_hal_rejects_missing_or_conflicting_product_model_before_sdk_build(self):
        for platform in ('esp32s3-camera-stream', 'esp32s3-camera-stream-vww'):
            wrong = ('PERSON_MODEL',) if platform.endswith('stream') else ('PICO_MODEL',)
            for features in ((), wrong, ('PICO_MODEL', 'PERSON_MODEL')):
                with self.subTest(platform=platform, features=features):
                    result, _ = self.script('hal', platform, features)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('dependencies do not match the product profile', result.stderr)

    def test_vww_product_build_generates_network_without_sdk(self):
        result, out = self.script('hal', 'esp32s3-camera-stream-vww', ('PERSON_MODEL',))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('person_detect.tflite', (out / 'network.rs').read_text())

    def test_stream_factory_and_budget_follow_profile_with_independent_probes(self):
        for suffix, enabled, stack, interval in [('', True, 131072, 750),
                                                 ('-vww', True, 524288, 2000),
                                                 ('-video', False, 131072, 750)]:
            for features in ((), ('PROBE',)):
                with self.subTest(suffix=suffix, features=features):
                    result, out = self.script('stream', 'esp32s3-camera-stream' + suffix, features)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    text = (out / 'stream_detector.rs').read_text()
                    self.assertIn(f'ENABLED:bool = {str(enabled).lower()}', text)
                    self.assertIn(f'STACK_BYTES:usize = {stack}', text)
                    self.assertIn(f'SAMPLE_MS:u64 = {interval}', text)
                    self.assertEqual('mod batch;' in text, suffix == '' and features == ('PROBE',))
                    self.assertIn('esp32s3-camera-stream.toml', result.stdout)

    def test_dry_run_leaves_model_dependency_resolution_pending(self):
        for suffix, expected in [('', ['nxrs-detector/pico-model']),
                                 ('-vww', ['nxrs-detector/person-model']), ('-video', [])]:
            for manifest, dependencies in [('tests/nuttx-std/stream/Cargo.toml', expected),
                                           ('app/std-demo/Cargo.toml', [])]:
                with self.subTest(suffix=suffix, manifest=manifest):
                    args = ['cargo', 'nxrs', 'build', '--dry-run', '--manifest-path', manifest,
                            '--profile', 'esp32s3-camera-stream' + suffix,
                            '--out', str(self.directory / 'firmware'), '--feature', 'probe']
                    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=120)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    deployment = json.loads(result.stdout)['deployment']
                    self.assertIsNone(deployment['model_features'])
                    self.assertEqual(deployment['dependency_resolution']['status'], 'pending')
                    self.assertFalse((self.directory / 'firmware').exists())
                    rejected = subprocess.run([*args, '--feature', 'nxrs-detector/pico-model'],
                                              cwd=ROOT, capture_output=True, text=True, timeout=120)
                    self.assertNotEqual(rejected.returncode, 0)
                    self.assertIn('remove manual model features', rejected.stderr)


if __name__ == '__main__':
    unittest.main()

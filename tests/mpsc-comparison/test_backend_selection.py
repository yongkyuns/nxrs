"""Check that firmware entry selection rejects unavailable transport claims."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent


class BackendSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='mpsc-selection-')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.script = cls.root / 'build-script'
        subprocess.run(['rustc', '--edition=2024', str(HERE / 'rust/build.rs'),
                        '-o', str(cls.script)], check=True, timeout=60)

    def select(self, target, backend, observer=None, hz=240000000):
        out = self.root / f'{target}-{backend}-{observer}-{hz}'
        out.mkdir(exist_ok=True)
        env = {**os.environ, 'OUT_DIR': str(out), 'CARGO_CFG_TARGET_OS': target,
               'MPSC_CLOCK_HZ': str(hz), 'MPSC_BUILD_ID': 'host'}
        env.pop('MPSC_BACKEND', None)
        env.pop('MPSC_OBSERVER', None)
        if backend is not None:
            env['MPSC_BACKEND'] = backend
        if observer is not None:
            env['MPSC_OBSERVER'] = observer
        result = subprocess.run([str(self.script)], env=env, capture_output=True,
                                text=True, timeout=10)
        return result, out

    def test_nuttx_entries_reach_only_the_selected_transport(self):
        for backend in ('std', 'native'):
            with self.subTest(backend=backend):
                result, out = self.select('nuttx', backend)
                self.assertEqual(result.returncode, 0, result.stderr)
                entry = (out / 'backend.rs').read_text()
                self.assertIn(f'crate::execute_{backend}(words)', entry)
                other = 'native' if backend == 'std' else 'std'
                self.assertNotIn(f'execute_{other}', entry)
                self.assertIn('rerun-if-env-changed=MPSC_BACKEND', result.stdout)

    def test_existing_defaults_remain_explicit(self):
        for target, expected in (('nuttx', 'std'), ('none', 'native'), ('linux', 'std')):
            with self.subTest(target=target):
                result, out = self.select(target, None)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f'crate::execute_{expected}(words)', (out / 'backend.rs').read_text())

    def test_unavailable_or_invalid_backends_are_rejected(self):
        for target, backend in (('none', 'std'), ('linux', 'native'),
                                ('nuttx', 'both'), ('nuttx', '')):
            with self.subTest(target=target, backend=backend):
                result, out = self.select(target, backend)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('unsupported MPSC backend for target', result.stderr)
                self.assertFalse((out / 'backend.rs').exists())

    def test_cpu_observer_is_bound_to_the_native_s3_target_scope(self):
        for target, scope in [('nuttx', 'nuttx-thread-runtime-includes-attributed-irqs'),
                              ('none', 'zephyr-thread-runtime-excludes-isr-dispatch')]:
            result, _ = self.select(target, None, 'cpu')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('cargo:rustc-env=MPSC_OBSERVER=cpu', result.stdout)
            self.assertIn('cargo:rustc-env=MPSC_CPU_SCOPE=' + scope, result.stdout)
        for target, observer, hz in [('linux', 'cpu', 240000000), ('nuttx', 'cpu', 1000000),
                                     ('none', 'cpu', 1000000), ('nuttx', '', 240000000)]:
            result, _ = self.select(target, None, observer, hz)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('unsupported MPSC observer', result.stderr)


if __name__ == '__main__':
    unittest.main()

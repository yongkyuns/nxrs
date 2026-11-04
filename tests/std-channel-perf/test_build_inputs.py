"""Offline integration guards; none of these execute firmware or claim timing."""
import importlib.util
import json
from pathlib import Path
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    'integration_build_inputs', ROOT / 'tests/std-channel-perf/build_inputs.py')
inputs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inputs)


class BenchmarkIntegrationTests(unittest.TestCase):
    def test_main_frontend_and_all_three_fixtures_are_workspace_members(self):
        manifest = tomllib.loads((ROOT / 'Cargo.toml').read_text())
        members = manifest['workspace']['members']
        for path in ('platform/build', 'tests/std-channel-perf/rust',
                     'tests/channel-progress/rust', 'tests/event-path-perf/rust'):
            self.assertEqual(members.count(path), 1)
        self.assertNotIn('platform/firmware', members)
        packages = tomllib.loads((ROOT / 'Cargo.lock').read_text())['package']
        for name in ('nxrs-build', 'nxrs-std-channel-perf',
                     'nxrs-channel-progress', 'nxrs-event-path-perf'):
            self.assertEqual(sum(p['name'] == name for p in packages), 1)

    def test_kernel_repairs_registered_once_in_current_manifest(self):
        manifest = json.loads((ROOT / 'upstream/build-patches.json').read_text())
        patches = manifest['nuttx']['patches']
        self.assertEqual(len(patches), len(set(patches)))
        for name in ('0106-armv8m-systick-coherent-snapshot.patch',
                     '0107-xtensa-simpleboot-hash-offset.patch'):
            self.assertEqual(patches.count(name), 1)
            self.assertTrue((ROOT / 'upstream/nuttx/patches' / name).is_file())
        self.assertIn('exec cargo nxrs nuttx-build',
                      (ROOT / 'tools/build-nuttx-std-app.sh').read_text())

    def test_source_closure_includes_actual_rust_frontend_and_patch_manifest(self):
        paths = {p.relative_to(ROOT).as_posix() for p in inputs.files(ROOT)}
        for path in ('platform/build/src/nuttx.rs', 'platform/build/src/sdk.rs',
                     'platform/build/src/source.rs', 'upstream/build-patches.json',
                     'hal/detector/build_esp_dl.rs', '.cargo/config.toml'):
            self.assertIn(path, paths)
        self.assertEqual(paths, set(inputs.select(paths)))
        self.assertEqual(paths, set(inputs.select(paths | {
            'platform/build/target/generated.rs', 'docs/unrelated.rs'})))
        for name in inputs.FIXED:
            with self.subTest(name=name), self.assertRaises(ValueError):
                inputs.select(paths - {name})

    def test_frontend_source_closure_refuses_missing_and_linked_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                inputs.files(root)
            for name in (*inputs.FIXED, 'platform/build/src/main.rs', 'platform/build/src/lib.rs'):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('synthetic source')
            self.assertTrue(inputs.files(root))
            target = root / 'platform/build/src/main.rs'
            target.unlink()
            target.symlink_to(root / 'platform/build/src/lib.rs')
            with self.assertRaises(ValueError):
                inputs.files(root)

    def test_p2_withdrawn_raw_evidence_is_not_reintroduced(self):
        directory = ROOT / 'tests/event-path-perf/results/2026-11-04-esp32s3'
        paths = {p.relative_to(directory).as_posix() for p in directory.rglob('*') if p.is_file()}
        self.assertEqual(paths, {'README.md', 'summary.json', 'provenance.json'})


if __name__ == '__main__':
    unittest.main()

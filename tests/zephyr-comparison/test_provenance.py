"""Focused tests for firmware source scoping and legacy manifest verification."""
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('zephyr_provenance_test', HERE / 'provenance.py')
provenance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(provenance)


class FirmwareProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'comparison'
        self.service = Path(self.temp.name) / 'service-footprint'
        self.root.mkdir()
        self.service.mkdir()
        self.out = Path(self.temp.name) / 'frozen'
        self.out.mkdir()
        self._copy_inputs('packet', {'native_defaults': True, 'lean': True})
        shutil.copy2(HERE / 'generate_control.py', self.root / 'generate_control.py')
        shutil.copy2(HERE / 'build.py', self.root / 'build.py')
        shutil.copy2(HERE / 'provenance.py', self.root / 'provenance.py')
        for tool_name in ('measure.py', 'report.py', 'run_matrix.py'):
            shutil.copy2(HERE / tool_name, self.root / tool_name)

    def tearDown(self):
        self.temp.cleanup()

    def _copy_inputs(self, mode, variant=None):
        for name in provenance.firmware_input_names(mode, variant):
            source = (HERE / name).resolve()
            target_root = self.service if name.startswith('../service-footprint/') else self.root
            shutil.copy2(source, target_root / Path(name).name)

    def _legacy_manifest(self, mode='packet', variant=None):
        if variant is None:
            variant = {'native_defaults': True, 'lean': True}
        manifest = {}
        for name in provenance.firmware_input_names(mode, variant):
            root = self.service if name.startswith('../service-footprint/') else self.root
            manifest[name] = provenance._digest(root / Path(name).name)
        manifest.update({'build.py': 'old-harness-digest',
                         'test_build.py': 'old-test-digest',
                         'results/frozen.json': 'old-results-digest',
                         'docs/notes.md': 'old-doc-digest',
                         'speed.conf': 'retired-config-digest'})
        (self.out / 'source-hashes.json').write_text(json.dumps(manifest))
        return manifest

    def test_explicit_mode_and_variant_inputs(self):
        wire = provenance.firmware_input_names('wire')
        packet = provenance.firmware_input_names('packet')
        baseline = provenance.firmware_input_names('baseline')
        sensitivity = provenance.firmware_input_names(
            'wire', {'native_defaults': True, 'lean': True})
        self.assertNotIn('../service-footprint/payload_processing.c', wire)
        self.assertIn('../service-footprint/payload_processing.c', packet)
        self.assertNotIn('native_adapter.c', baseline)
        self.assertNotIn('../service-footprint/channel_scale_mq.c', baseline)
        self.assertIn('native-defaults.conf', sensitivity)
        self.assertIn('lean.conf', sensitivity)

    def test_legacy_tool_and_result_differences_are_historical_only(self):
        legacy = self._legacy_manifest()
        # Compare against the actual current adaptation, as frozen target data does.
        spec = importlib.util.spec_from_file_location('test_generator', self.root / 'generate_control.py')
        generator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(generator)
        source = (self.service / 'channel_scale_mq.c').read_text()
        adapted = generator.adapt(source).encode()
        (self.out / 'matched_control.c').write_bytes(adapted)
        result = provenance.verify_firmware_sources(
            self.out, 'packet', {'native_defaults': True, 'lean': True}, self.root)
        self.assertEqual(result['firmware_sources'],
                         {name: legacy[name] for name in provenance.firmware_input_names(
                             'packet', {'native_defaults': True, 'lean': True})})
        self.assertTrue(result['generated_control']['matches_frozen'])
        diff_by_name = {item['name']: item for item in result['differences']}
        self.assertEqual(diff_by_name['speed.conf']['classification'], 'retired_non_input')
        self.assertEqual(diff_by_name['build.py']['classification'], 'tool')
        self.assertEqual(diff_by_name['build.py']['change'], 'changed')
        self.assertEqual(result['current_tools']['legacy_source_sha256']['build.py'],
                         'old-harness-digest')
        self.assertIn('test_build.py', result['historical_non_firmware']['source_sha256'])
        self.assertIn('results/frozen.json', result['historical_non_firmware']['source_sha256'])
        self.assertIn('build.py', result['current_tools']['source_sha256'])
        self.assertIn('report.py', result['current_tools']['source_sha256'])
        self.assertIn('measure.py', result['current_tools']['source_sha256'])
        self.assertIn('run_matrix.py', result['current_tools']['source_sha256'])
        self.assertNotIn('test_build.py', result['firmware_sources'])

    def test_frozen_directory_requires_manifest_and_control_evidence(self):
        with self.assertRaisesRegex(ValueError, 'missing frozen source hash manifest'):
            provenance.verify_firmware_sources(self.out, 'baseline', root=self.root)
        (self.out / 'source-hashes.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'empty frozen source hash manifest'):
            provenance.verify_firmware_sources(self.out, 'baseline', root=self.root)
        self._legacy_manifest()
        with self.assertRaisesRegex(ValueError, 'missing frozen matched_control.c'):
            provenance.verify_firmware_sources(
                self.out, 'packet', {'native_defaults': True, 'lean': True}, self.root)

    def test_modified_or_missing_firmware_inputs_fail(self):
        self._legacy_manifest()
        target = self.root / 'main.c'
        target.write_bytes(target.read_bytes() + b'\n/* changed */\n')
        with self.assertRaisesRegex(ValueError, 'modified firmware inputs: main.c'):
            provenance.verify_firmware_sources(self.out, 'packet',
                                               {'native_defaults': True, 'lean': True}, self.root)
        target.unlink()
        with self.assertRaisesRegex(ValueError, 'missing firmware input: main.c'):
            provenance.verify_firmware_sources(self.out, 'packet',
                                               {'native_defaults': True, 'lean': True}, self.root)

    def test_generated_control_mismatch_fails(self):
        self._legacy_manifest()
        (self.out / 'matched_control.c').write_text('not the adapted control')
        with self.assertRaisesRegex(ValueError, 'generated matched_control.c differs'):
            provenance.verify_firmware_sources(self.out, 'packet',
                                               {'native_defaults': True, 'lean': True}, self.root)


if __name__ == '__main__':
    unittest.main()

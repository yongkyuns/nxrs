"""Negative controls for native model provenance, lifetime planning and evidence."""
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


REPORT = load('pico_report', 'tests/detection/report.py')
GRAPH = load('pico_graph', 'upstream/esp-dl/graph.py')
PREPARE = load('pico_prepare', 'upstream/esp-dl/prepare.py')
BUILD = load('pico_build', 'hal/detector/src/compute/esp_dl/ffi/build.py')
BOXES = load('pico_boxes', 'tests/detection/report_boxes.py')
QUALIFY = load('pico_entries', 'upstream/esp-dl/qualify.py')
DEVICE = load('pico_device', 'tests/detection/run_device.py')
PROFILE = load('pico_profile', 'tests/detection/report_profile.py')


class PicoQualification(unittest.TestCase):
    def test_fixed_graph_rejects_non_s8_operators(self):
        names = ['input', 'score0', 'score1', 'score2', 'bbox0', 'bbox1', 'bbox2']
        variables = {name: {'shape': [1, 1, 1, 1], 'exp': 0} for name in names}
        nodes = [dict(op='Concat', ins=['input'], out=name,
                      attrs={'axis': 3, 'quant_type': 'S8'}) for name in names[1:]]
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(GRAPH, 'read', return_value=({}, variables, nodes)):
            output = Path(directory) / 'engine.cpp'
            GRAPH.generate(b'', output, False)
            for precision in ('S16', 'F32', None):
                nodes[-1]['attrs']['quant_type'] = precision
                with self.assertRaisesRegex(ValueError, 'requires S8'):
                    GRAPH.generate(b'', output, False)

    def test_optimized_native_corpus_preserves_scores_and_stage_summaries(self):
        evidence = ROOT / 'tests/detection/results/2026-10-22-pico'
        previous = ROOT / 'tests/detection/results/2026-10-21-pico'
        for tag, count in (('vww', 800), ('oi', 200)):
            scores = [list(map(int, row.split())) for row in (evidence / f'native-{tag}-scores.tsv').read_text().splitlines()]
            old = [list(map(int, row.split())) for row in (previous / f'native-{tag}-scores.tsv').read_text().splitlines()]
            stages = [list(map(int, row.split())) for row in (evidence / f'native-{tag}-stages.tsv').read_text().splitlines()]
            old_device = json.loads((previous / f'native-{tag}-device.json').read_text())
            self.assertEqual((len(scores), len(stages)), (count, count))
            self.assertEqual([row[:3] for row in scores], [row[:3] for row in old])
            rows = []
            for i, (score, stage, original) in enumerate(zip(scores, stages, old_device['rows'])):
                self.assertEqual((score[0], stage[0]), (i, i))
                self.assertEqual(stage[6:], [original['boxes'], int(original['truncated'])])
                rows.append(dict(index=i, score=score[2], inference_us=score[3],
                                 stages_us=dict(zip(PROFILE.STAGES, stage[1:6]))))
            report = dict(passed=True, rows=rows, dataset_sha256=old_device['dataset_sha256'])
            actual = PROFILE.summarize(report, (previous / f'native-{tag}-scores.tsv').read_text())
            recorded = json.loads((evidence / f'native-{tag}-stages.json').read_text())
            for key, value in actual.items():
                self.assertEqual(recorded[key], value)

    def test_recorded_scores_match_frozen_labels_native_results_and_metrics(self):
        evidence = ROOT / 'tests/detection/results/2026-10-21-pico'
        original = ROOT / 'tests/detection/results/2026-10-21'
        for tag, manifest, expected in [('vww', 'sample-manifest.json', (359, 329, 71, 41)),
                                        ('oi', 'open-images/sample-manifest.json', (67, 86, 14, 33))]:
            images = json.loads((original / manifest).read_text())['images']
            host = [list(map(int, line.split())) for line in (evidence / f'host-{tag}-scores.tsv').read_text().splitlines()]
            native = [list(map(int, line.split())) for line in (evidence / f'native-{tag}-scores.tsv').read_text().splitlines()]
            device = json.loads((evidence / f'native-{tag}-device.json').read_text())
            self.assertTrue(device['passed'])
            self.assertEqual(len(host), len(images))
            self.assertEqual(len(native), len(host))
            self.assertEqual(len(device['rows']), len(host))
            for index, (a, b, image, recorded) in enumerate(zip(host, native, images, device['rows'])):
                self.assertEqual(a[:3], b[:3])
                self.assertEqual(a[:2], [index, image['label']])
                self.assertTrue(0 <= a[2] <= 1000 and a[3] > 0 and b[3] > 0)
                self.assertEqual(recorded['jpeg_sha256'], image['jpeg_sha256'])
                self.assertEqual(recorded['score'], a[2])
            metrics = REPORT.metrics([row[1] for row in host], [row[2] for row in host], 500)
            self.assertEqual(tuple(metrics[k] for k in ('tp', 'tn', 'fp', 'fn')), expected)
            reported = json.loads((evidence / f'host-{tag}-accuracy.json').read_text())
            self.assertEqual(reported['thresholds'][0], metrics)

    def test_compute_lease_rejects_unqualified_cpu_or_execution_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'nuttx').mkdir()
            config = root / 'nuttx/config.h'
            qualified = '#define CONFIG_ARCH_CHIP_ESP32S3 1\n#define CONFIG_BUILD_FLAT 1\n'
            config.write_text(qualified)
            BUILD.qualify_compute_context(root)
            for content in (qualified + '#define CONFIG_SMP 1\n',
                            qualified.replace('CONFIG_ARCH_CHIP_ESP32S3', 'CONFIG_OTHER_CPU'),
                            qualified.replace('CONFIG_BUILD_FLAT', 'CONFIG_BUILD_PROTECTED')):
                config.write_text(content)
                with self.assertRaisesRegex(ValueError, 'single-core flat'):
                    BUILD.qualify_compute_context(root)

    def test_localization_matching_is_one_to_one_and_crowd_images_are_excluded(self):
        box = (0, 0, 1000, 1000)
        self.assertEqual(BOXES.match([(900, box), (800, box)], [box]), (1, 1, 0))
        self.assertEqual(BOXES.match([(900, (0, 0, 100, 100))], [box]), (0, 1, 1))
        dataset = {'images': [{'id': i, 'width': 100, 'height': 100} for i in (1, 2)]}
        annotations = {'annotations': [{'image_id': i, 'category_id': 1, 'bbox': [0, 0, 100, 100],
                                        'iscrowd': i == 2} for i in (1, 2)]}
        rows = ['0\t0\t900,0,0,1000,1000', '1\t0']
        result = BOXES.evaluate(dataset, annotations, rows)
        self.assertEqual((result['tp'], result['fp'], result['fn']), (1, 0, 0))
        self.assertEqual(result['excluded_person_crowd_images'], 1)
        with self.assertRaises(ValueError):
            BOXES.evaluate(dataset, annotations, rows[:-1])
        with self.assertRaises(ValueError):
            BOXES.evaluate(dataset, annotations, ['0\t0\t900,1000,0,0,1000', '1\t0'])

    def test_literal_pool_at_function_address_is_rejected(self):
        # Minimal ELF object: null/text/strings/symbols sections; function at 0.
        data = bytearray(256)
        data[:6] = b'\x7fELF\x01\x01'
        struct.pack_into('<H', data, 18, 94)
        struct.pack_into('<I', data, 32, 64)
        struct.pack_into('<HH', data, 46, 40, 4)
        struct.pack_into('<IIIIIIIIII', data, 104, 0, 1, 0, 0, 224, 3, 0, 0, 1, 0)
        struct.pack_into('<IIIIIIIIII', data, 144, 0, 3, 0, 0, 227, 7, 0, 0, 1, 0)
        struct.pack_into('<IIIIIIIIII', data, 184, 0, 2, 0, 0, 240, 16, 2, 0, 4, 16)
        data[224:227] = b'\x36\x01\x01'  # ENTRY a1, 128
        data[227:234] = b'kernel\0'
        struct.pack_into('<IIIBBH', data, 240, 0, 0, 0, 0x12, 0, 1)
        self.assertEqual(QUALIFY.check_entries(data), 1)
        data[224:227] = b'\x05\x00\x00'  # literal at the call target
        with self.assertRaisesRegex(ValueError, 'does not begin with ENTRY'):
            QUALIFY.check_entries(data)

    def test_outputs_do_not_alias_inputs_or_retained_heads(self):
        # A branched graph keeps its input until both consumers finish and keeps
        # all six heads alive until the final output copy.
        names = ['input', 'branch'] + ['score0', 'score1', 'score2', 'bbox0', 'bbox1', 'bbox2']
        variables = {name: {'shape': [1, 1, 1, 17]} for name in names}
        nodes = [{'ins': ['input'], 'out': 'branch'}]
        nodes += [{'ins': ['branch', 'input'], 'out': name} for name in names[2:]]
        offsets, high = GRAPH.plan(variables, nodes)
        self.assertNotEqual(offsets['input'], offsets['branch'])
        heads = [offsets[name] for name in names[2:]]
        self.assertEqual(len(set(heads)), 6)
        self.assertNotIn(offsets['input'], heads)
        self.assertNotIn(offsets['branch'], heads)
        self.assertEqual(high, 8 * 32)

    def test_rejects_use_before_definition(self):
        variables = {name: {'shape': [1, 1, 1, 1]} for name in ('input', 'score0', 'later')}
        with self.assertRaisesRegex(ValueError, 'input lifetime lost'):
            GRAPH.plan(variables, [{'ins': ['later'], 'out': 'score0'}])

    def test_largest_first_plan_reuses_dead_input_without_fragmenting_arena(self):
        sizes = {'input': 48, 'a': 64, 'b': 64, 'score0': 16}
        variables = {name: {'shape': [1, 1, 1, size]} for name, size in sizes.items()}
        nodes = [dict(ins=[a], out=b) for a, b in
                 [('input', 'a'), ('a', 'b'), ('b', 'score0')]]
        offsets, high = GRAPH.plan(variables, nodes)
        self.assertEqual(high, 128)  # The two 64-byte tensors must coexist.
        for name, offset in offsets.items():
            self.assertEqual(offset % 16, 0)
            self.assertLessEqual(offset + sizes[name], high)
        for node in nodes:
            a, b = node['ins'][0], node['out']
            self.assertTrue(offsets[a] + sizes[a] <= offsets[b] or
                            offsets[b] + sizes[b] <= offsets[a])

    def test_edited_sdk_cannot_self_certify_with_a_forged_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'tensor.hpp'
            source.write_bytes(b'reviewed')
            reviewed = hashlib.sha256(source.read_bytes()).hexdigest()
            ledger = root / 'ledger.json'
            ledger.write_text(json.dumps({'prepared_files': {'tensor.hpp': reviewed}}))
            identity = {'ledger_sha256': hashlib.sha256(ledger.read_bytes()).hexdigest(),
                        'files': {'tensor.hpp': reviewed}}
            (root / 'prepared.json').write_text(json.dumps(identity))
            original = PREPARE.LEDGER
            PREPARE.LEDGER = ledger
            try:
                PREPARE.verify(root)
                source.write_bytes(b'changed')
                with self.assertRaisesRegex(ValueError, 'SDK changed'):
                    PREPARE.verify(root)
                identity['files']['tensor.hpp'] = hashlib.sha256(source.read_bytes()).hexdigest()
                (root / 'prepared.json').write_text(json.dumps(identity))
                with self.assertRaisesRegex(ValueError, 'inventory differs'):
                    PREPARE.verify(root)
            finally:
                PREPARE.LEDGER = original

    def test_dataset_must_match_frozen_bytes_and_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jpeg = root / 'image.jpg'
            jpeg.write_bytes(b'fixed image')
            (root / 'manifest.tsv').write_text(f'1\t320\t240\t{jpeg}\n')
            (root / 'dataset.json').write_text(json.dumps({'images': [
                {'label': 1, 'jpeg_sha256': hashlib.sha256(jpeg.read_bytes()).hexdigest()}]}))
            self.assertEqual(len(DEVICE.load_dataset(root)[0]), 1)
            jpeg.write_bytes(b'other image')
            with self.assertRaisesRegex(ValueError, 'identity/label'):
                DEVICE.load_dataset(root)

    def test_native_results_reject_stale_sequence_and_impossible_values(self):
        self.assertEqual(DEVICE.parse_result(
            b'NXRS_DETECT_RESULT index=1 score=940 inference_us=20000 boxes=2 truncated=1', 1),
            (940, 20000, 2, True))
        for row in (b'index=0 score=940 inference_us=20000 boxes=2 truncated=0',
                    b'index=1 score=1001 inference_us=20000 boxes=2 truncated=0',
                    b'index=1 score=940 inference_us=0 boxes=2 truncated=0',
                    b'index=1 score=940 inference_us=20000 boxes=11 truncated=0',
                    b'index=1 score=940 inference_us=20000 boxes=2 truncated=2'):
            with self.assertRaises(ValueError):
                DEVICE.parse_result(b'NXRS_DETECT_RESULT ' + row, 1)

    def test_stage_profiles_reject_wrong_sequence_and_inconsistent_clock_totals(self):
        row = b'NXRS_DETECT_PROFILE index=4 decode_us=1000 resize_us=2000 kernel_us=3000 postprocess_us=0 total_us=6000'
        self.assertEqual(DEVICE.parse_profile(row, 4, 7000)['kernel_us'], 3000)
        for bad, index, elapsed in ((row, 3, 7000), (row, 4, 5000),
                                    (row.replace(b'total_us=6000', b'total_us=5999'), 4, 7000),
                                    (row.replace(b'kernel_us=3000', b'kernel_us=0'), 4, 7000)):
            with self.assertRaises(ValueError):
                DEVICE.parse_profile(bad, index, elapsed)

    def test_performance_summary_requires_passed_complete_consistent_measurements(self):
        timing = dict(decode_us=1000, resize_us=2000, kernel_us=3000, postprocess_us=0, total_us=6000)
        row = dict(index=0, score=900, inference_us=6000, stages_us=timing)
        report = dict(passed=True, dataset_sha256='frozen', rows=[row])
        self.assertEqual(PROFILE.summarize(report, '0\t1\t900\t6000\t0')['reference_score_differences'], 0)
        for bad in (dict(report, passed=False), dict(report, rows=[]),
                    dict(report, rows=[dict(row, index=1)]),
                    dict(report, rows=[dict(row, inference_us=5999)]),
                    dict(report, rows=[dict(row, stages_us=dict(timing, kernel_us=True))])):
            with self.assertRaises(ValueError):
                PROFILE.summarize(bad)
        with self.assertRaisesRegex(ValueError, 'changed native scores'):
            PROFILE.summarize(report, '0\t1\t899\t6000\t0')


if __name__ == '__main__':
    unittest.main()

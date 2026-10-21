import importlib.util
import hashlib
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
RUN = load('detection_stream', 'tests/nuttx-std/stream/run.py')
DATA = load('detection_prepare', 'tests/detection/prepare.py')
OPEN_IMAGES = load('detection_prepare_open_images', 'tests/detection/prepare_open_images.py')
REPORT = load('detection_metrics', 'tests/detection/report.py')


class DetectionQualificationTests(unittest.TestCase):
    def test_accepts_only_consistent_bounded_detection_record_schema(self):
        row = b'NXRS_DETECTION_REPORT ms=10000 state=Clear submitted=8 completed=7 failures=0 inference_us=7000000 max_inference_us=1100000 events=0 sequence=100 age_ms=1200 score=200'
        parsed = RUN.detection_report(row)
        self.assertEqual(parsed['state'], 'Clear')
        self.assertEqual(parsed['completed'], 7)
        for bad in (row+b' extra', row.replace(b'Clear', b'Unknown'), row.replace(b'completed=7', b'completed=9'),
                    row.replace(b'max_inference_us=1100000', b'max_inference_us=8000000'),
                    row.replace(b'sequence=100', b'sequence=0'), row.replace(b'score=200', b'score=1001'),
                    row.replace(b'age_ms=1200', b'age_ms=10001')):
            with self.subTest(line=bad), self.assertRaises(ValueError):
                RUN.detection_report(bad)

    def test_holdout_selection_is_disjoint_and_vww_area_rule_is_applied(self):
        data = {'images': [{'id': i, 'width': 100, 'height': 100} for i in range(40)],
                'annotations': [{'image_id': i, 'category_id': 1, 'bbox': [0, 0, 10, 10], 'area': 100} for i in range(20)]}
        # A tiny person does not satisfy the VWW foreground-size requirement.
        data['annotations'].append({'image_id': 39, 'category_id': 1, 'bbox': [0, 0, 80, 80], 'area': 1})
        first = DATA.select(data, 20)
        held = DATA.select(data, 20, 10)
        self.assertEqual(sum(v['label'] for v in first), 10)
        self.assertEqual(sum(v['label'] for v in held), 10)
        self.assertFalse({v['id'] for v in first} & {v['id'] for v in held})
        small = next(v for v in first+held if v['id'] == 39)
        self.assertEqual((small['label'], small['has_small_person']), (0, True))
        self.assertNotIn(first[0]['id'], {v['id'] for v in DATA.select(data, 20, excluded={first[0]['id']})})

    def test_accuracy_is_not_confused_with_precision_or_recall(self):
        m = REPORT.metrics([1, 1, 0, 0], [900, 100, 800, 200], 500)
        self.assertEqual([m[k] for k in ('tp', 'tn', 'fp', 'fn')], [1, 1, 1, 1])
        self.assertEqual(m['accuracy'], .5)
        self.assertEqual(m['precision'], .5)
        self.assertEqual(m['recall'], .5)
        lo, hi = m['accuracy_wilson_95']
        self.assertLess(lo, .5); self.assertGreater(hi, .5)

    def test_open_images_negatives_require_explicit_nonconflicting_human_labels(self):
        images = [{'ImageID': f'{i:016x}', 'Rotation': '0.0', 'License': OPEN_IMAGES.LICENSE}
                  for i in range(40)]
        labels = [{'ImageID': f'{i:016x}', 'Source': 'verification',
                   'LabelName': OPEN_IMAGES.PERSON, 'Confidence': str(i % 2)} for i in range(40)]
        labels.append(dict(labels[0], Confidence='1'))  # Conflicting verification.
        labels[1]['Source'] = 'machine'
        labels[2]['LabelName'] = '/m/different'
        images[3]['Rotation'] = '90.0'
        images[4]['License'] = 'unverified'
        selected = OPEN_IMAGES.select(images, labels, 20)
        self.assertEqual(sum(row['label'] for row in selected), 10)
        self.assertFalse({f'{i:016x}' for i in range(5)} & {row['ImageID'] for row in selected})
        self.assertEqual(selected, OPEN_IMAGES.select(list(reversed(images)), list(reversed(labels)), 20))

    def test_reference_gate_rejects_score_drift_and_threshold_disagreements(self):
        self.assertTrue(REPORT.reference_agreement([900, 100], [904, 96])['passed'])
        self.assertFalse(REPORT.reference_agreement([900, 100], [950, 100])['passed'])
        self.assertFalse(REPORT.reference_agreement([499, 100], [500, 100])['passed'])
        with self.assertRaises(ValueError):
            REPORT.reference_agreement([], [])

    def test_recorded_sample_evidence_is_complete_and_recomputable(self):
        root = ROOT / 'tests/detection/results/2026-10-21'
        for suffix, count in [('', 800), ('open-images', 200)]:
            with self.subTest(dataset=suffix):
                directory = root / suffix
                data = json.loads((directory / 'sample-manifest.json').read_text())
                report = json.loads((directory / 'accuracy.json').read_text())
                rows = [[int(v) for v in line.split()] for line in (directory / 'scores.tsv').read_text().splitlines()]
                self.assertEqual(len(rows), count)
                self.assertEqual([r[0] for r in rows], list(range(count)))
                self.assertEqual([r[1] for r in rows], [r['label'] for r in data['images']])
                self.assertEqual(sum(r[1] for r in rows), count // 2)
                ids = {r['id'] for r in data['images']}
                self.assertEqual(len(ids), count)
                self.assertFalse(ids & set(data.get('excluded_image_ids', [])))
                self.assertEqual(REPORT.metrics([r[1] for r in rows], [r[2] for r in rows], 500), report['thresholds'][0])
                agreement = REPORT.reference_agreement([r[2] for r in rows], report['reference']['scores_permille'])
                self.assertTrue(agreement['passed'])
                for key, value in agreement.items():
                    self.assertEqual(value, report['reference'][key])
                for filename, key in [('scores.tsv', 'scores_sha256'), ('sample-manifest.json', 'sample_manifest_sha256')]:
                    self.assertEqual(hashlib.sha256((directory / filename).read_bytes()).hexdigest(), report[key])
                self.assertEqual(hashlib.sha256((ROOT / 'service/detection/models/person_detect.tflite').read_bytes()).hexdigest(),
                                 report['reference']['model_sha256'])

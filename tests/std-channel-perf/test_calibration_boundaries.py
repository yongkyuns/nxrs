"""Counter/reference boundary tests using synthetic, not physical, evidence."""
import hashlib
from pathlib import Path
import tempfile
import unittest

import analyze
from test_analyze import fixture, transcript


class CalibrationBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        evidence = self.root / 'reference.txt'
        evidence.write_bytes(b'synthetic boundary-control reference')
        self.summary = analyze.analyze(transcript(*fixture()))
        self.receipt = {'build_id': 'b' * 64, 'image_sha256': 'c' * 64}
        self.record = dict(
            schema='std-channel-clock-calibration-v1', method='paired-independent-timebase',
            reference_type='external-instrument', reference_identity='synthetic-test-reference',
            measurement_session='test', build_id='b' * 64, image_sha256='c' * 64,
            reference_hz=1_000_000, reference_accuracy_ppm=100,
            reference_start_ticks=1000, reference_end_ticks=1_001_000,
            cpu_counter_bits=32, cpu_start_ticks=1000, cpu_end_ticks=100_001_000,
            evidence_file='reference.txt', evidence_sha256=hashlib.sha256(evidence.read_bytes()).hexdigest())
        self.session = {'measurement_session': 'test', 'clock_calibration': self.record}

    def result(self):
        return analyze.independent_clock_calibration(
            self.summary, self.receipt, self.session, self.root)

    def test_exact_frequency_limit_is_inclusive_without_flooring_errors(self):
        self.record['cpu_end_ticks'] = 100_101_000
        self.assertTrue(self.result()['qualified'])
        # 1000.01 ppm cannot be floored into a 1000 ppm acceptance limit.
        self.record['cpu_end_ticks'] += 1
        rejected = self.result()
        self.assertFalse(rejected['qualified'])
        self.assertIn('disagrees', rejected['reason'])

    def test_reference_window_cannot_hide_complete_cpu_counter_wrap(self):
        self.summary['metadata']['clock_hz'] = 1_000_000_000
        self.record.update(reference_end_ticks=5_001_000, cpu_end_ticks=705_033_704)
        rejected = self.result()
        self.assertFalse(rejected['qualified'])
        self.assertIn('ambiguous', rejected['reason'])

    def test_placeholder_reference_is_not_calibration(self):
        for placeholder in ['REPLACE-with-reference', '<instrument>', 'unmeasured']:
            self.record['reference_identity'] = placeholder
            with self.subTest(identity=placeholder):
                self.assertFalse(self.result()['qualified'])


if __name__ == '__main__':
    unittest.main()

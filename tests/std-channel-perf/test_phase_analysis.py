"""Versioned schedule, duration and failure controls. All observations synthetic."""
import copy
import unittest
from phase_analysis import analyze_phase, next_delay
from test_analyze import fixture, transcript
import analyze


def paced_fixture(seed=1):
    metadata, samples, result = fixture()
    result.update(release_profile='setup-counter-delay-v1', release_seed=seed,
                  retained_window_ns=10_000_000, counter_read_probe_count=64,
                  counter_read_min_ticks=2, counter_read_max_ticks=5)
    state = seed
    for _ in range(metadata['warmup']):
        state, _ = next_delay(state)
    for i, sample in enumerate(samples):
        state, us = next_delay(state)
        ticks = (metadata['clock_hz'] * us + 999_999) // 1_000_000
        start = (0xfffffff0 + i * 1_000_000) % (1 << 32)
        sample.update(pacing_requested_ticks=ticks, pacing_start_ticks=start,
                      pacing_return_ticks=(start + ticks + 2) % (1 << 32),
                      pacing_elapsed_upper_ns=10_000_000)
    return metadata, samples, result


class PhaseAnalysisTests(unittest.TestCase):
    def test_replay_preserves_warmup_wrap_and_sample_order(self):
        self.assertEqual(next_delay(1)[1], 370)
        rows = paced_fixture()
        report = analyze_phase(*rows)
        self.assertEqual(report['profile'], 'setup-counter-delay-v1')
        self.assertGreater(report['requested_pacing_ticks'], 0)
        self.assertFalse(report['phase_coverage_qualified'])
        self.assertFalse(report['counter_read_probe']['subtracted_from_samples'])
        self.assertEqual(report['max_send_sample_indices'], [0, 1])
        integrated = analyze.analyze(transcript(*rows))
        self.assertEqual(integrated['sampling'], report)

    def test_old_captures_stay_historical(self):
        report = analyze_phase(*fixture())
        self.assertEqual(report['profile'], 'historical-unrecorded')
        self.assertFalse(report['phase_coverage_qualified'])

    def test_tight_profile_requires_zero_pacing(self):
        metadata, samples, result = paced_fixture()
        result.update(release_profile='tight-v1', release_seed=0)
        with self.assertRaises(ValueError):
            analyze_phase(metadata, samples, result)
        for sample in samples:
            sample.update(pacing_requested_ticks=0, pacing_start_ticks=0,
                          pacing_return_ticks=0, pacing_elapsed_upper_ns=0)
        self.assertEqual(analyze_phase(metadata, samples, result)['requested_pacing_ticks'], 0)

    def test_seed_sequence_timestamp_window_and_probe_corruption_fail(self):
        rows = paced_fixture()
        for target, field, value in [
            ('result', 'release_seed', 2), ('result', 'release_seed', True),
            ('result', 'release_profile', 'unknown'), ('result', 'retained_window_ns', 0),
            ('result', 'counter_read_probe_count', 0), ('result', 'counter_read_min_ticks', 6),
            ('metadata', 'counter_validated', False), ('metadata', 'warmup', 129),
            ('sample', 'pacing_requested_ticks', 2), ('sample', 'pacing_return_ticks', 0xfffffff1),
            ('sample', 'pacing_elapsed_upper_ns', 1),
            ('sample', 'pacing_elapsed_upper_ns', 1_000_000_000_000),
        ]:
            metadata, samples, result = copy.deepcopy(rows)
            {'metadata': metadata, 'sample': samples[0], 'result': result}[target][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                analyze_phase(metadata, samples, result)

    def test_pacing_cannot_be_relabelled_as_historical(self):
        metadata, samples, result = paced_fixture()
        del result['release_profile']
        with self.assertRaisesRegex(ValueError, 'without release profile'):
            analyze_phase(metadata, samples, result)


if __name__ == '__main__':
    unittest.main()

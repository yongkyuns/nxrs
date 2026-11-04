"""Synthetic controls; retained physical evidence is read-only and never relabelled."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import analyze
import analyze_alternation
from capture import command_for, validate_sampling_request
from phase_analysis import analyze_phase
from test_analyze import fixture, transcript
from test_phase_analysis import paced_fixture

HERE = Path(__file__).resolve().parent


def fixed_fixture(delay=1000):
    metadata, samples, result = paced_fixture()
    result.update(release_profile='setup-fixed-counter-delay-v1', release_seed=0,
                  release_fixed_delay_us=delay)
    ticks = (metadata['clock_hz'] * delay + 999_999) // 1_000_000
    for sample in samples:
        sample.update(pacing_requested_ticks=ticks,
                      pacing_return_ticks=(sample['pacing_start_ticks'] + ticks + 2) % (1 << 32))
    return metadata, samples, result


class FixedControlTests(unittest.TestCase):
    def test_fixed_schedule_replays_with_wrap_and_odd_warmup(self):
        rows = fixed_fixture()
        actual = analyze.analyze(transcript(*rows))
        self.assertEqual(actual['sampling']['fixed_delay_us'], 1000)
        self.assertEqual(actual['sampling']['requested_pacing_ticks'], 200_000)
        self.assertFalse(actual['physical_timing_qualified'])
        self.assertFalse(actual['sampling']['phase_coverage_qualified'])

    def test_fixed_fields_cannot_disguise_seeded_or_historical_pacing(self):
        for field, value in [('release_fixed_delay_us', 0), ('release_fixed_delay_us', 2001),
                             ('release_fixed_delay_us', True), ('release_fixed_delay_us', 999),
                             ('release_seed', 1), ('release_profile', 'tight-v1'),
                             ('release_profile', 'setup-counter-delay-v1')]:
            rows = copy.deepcopy(fixed_fixture())
            rows[2][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                analyze_phase(*rows)
        rows = fixed_fixture()
        del rows[2]['release_profile']
        with self.assertRaises(ValueError):
            analyze_phase(*rows)
        rows = fixed_fixture()
        del rows[2]['release_fixed_delay_us']
        with self.assertRaises(ValueError):
            analyze_phase(*rows)

    def test_fixed_command_and_watchdog_budgets(self):
        self.assertEqual(command_for('latency', 1000, 101, fixed_delay_us=1000),
                         'std_channel_perf latency 1000 101 fixed:1000')
        for kwargs in [dict(fixed_delay_us=value) for value in [0, -1, 2001, True]]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                command_for('latency', 2, 1, **kwargs)
        for mode, samples, warmup, seed in [('latency', 1025, 1, None),
                                           ('latency', 2, 129, None),
                                           ('latency', 2, 1, 1),
                                           ('witness-reserved', 2, 1, None)]:
            with self.assertRaises(ValueError):
                command_for(mode, samples, warmup, seed, fixed_delay_us=1000)

    def test_stale_firmware_cannot_satisfy_fixed_request(self):
        sampling = analyze_phase(*fixed_fixture())
        validate_sampling_request(sampling, fixed_delay_us=1000)
        for request in [dict(fixed_delay_us=999), dict(phase_seed=1), {},
                        dict(fixed_delay_us=1000, phase_seed=1)]:
            with self.subTest(request=request), self.assertRaises(ValueError):
                validate_sampling_request(sampling, **request)
        for profile in ['tight-v1', 'historical-unrecorded', 'setup-counter-delay-v1']:
            with self.assertRaises(ValueError):
                validate_sampling_request({'profile': profile}, fixed_delay_us=1000)


class AlternationTests(unittest.TestCase):
    def test_odd_warmup_changes_transfer_parity_not_sample_identity(self):
        metadata, samples, result = fixture()
        samples[0]['receive_return_ticks'] = samples[0]['send_start_ticks'] + 35
        report = analyze_alternation.report(transcript(metadata, samples, result))
        retained = report['retained_index_mod_2']
        transfer = report['transfer_index_mod_2']
        self.assertEqual(retained[0]['send_to_receive_return']['p50_ticks'], 35)
        self.assertEqual(transfer[1]['send_to_receive_return']['p50_ticks'], 35)
        self.assertEqual(report['last_sample']['index'], 1)
        self.assertEqual(report['samples_excluded'], 0)
        self.assertFalse(report['internal_channel_slot_observed'])
        self.assertFalse(report['root_cause_established'])
        self.assertFalse(report['physical_timing_qualified'])

    def test_empty_remainder_and_noninterpolated_percentiles(self):
        report = analyze_alternation.report(transcript(*fixture()))
        self.assertEqual(report['transfer_index_mod_capacity'][7]['send_call'], {'count': 0})
        stats = analyze_alternation.ticks_summary([10, 20, 30, 40])
        self.assertEqual((stats['p50_ticks'], stats['p99_ticks']), (20, 40))

    def test_invalid_samples_and_witness_cannot_get_descriptive_pass(self):
        rows = fixture()
        rows[1][1]['index'] = 0
        with self.assertRaises(ValueError):
            analyze_alternation.report(transcript(*rows))
        with self.assertRaises(ValueError):
            analyze_alternation.report(transcript(*fixture('witness-published')))

    def test_cli_preserves_existing_files_and_hashes_raw_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, out = root / 'console.log', root / 'report.json'
            raw = transcript(*fixture()).replace('\n', '\r\n').encode()
            source.write_bytes(raw)
            argv = [sys.executable, str(HERE / 'analyze_alternation.py'), str(source),
                    '--output', str(out)]
            subprocess.run(argv, check=True, capture_output=True)
            original_report = out.read_bytes()
            self.assertEqual(json.loads(original_report)['transcript_sha256'], analyze.sha256(source))
            self.assertNotEqual(subprocess.run(argv, capture_output=True).returncode, 0)
            self.assertEqual(source.read_bytes(), raw)
            self.assertEqual(out.read_bytes(), original_report)

    def test_retained_physical_seed_run_is_not_rewritten(self):
        path = HERE / 'results/2026-11-03-esp32s3-simpleboot/runs/latency-rom-digest-seed-1-1/console.log'
        before = path.read_bytes()
        report = analyze_alternation.report(before.decode())
        even, odd = report['retained_index_mod_2']
        self.assertEqual(even['send_to_receive_return']['most_common'][0],
                         {'ticks': 3673, 'count': 498})
        self.assertEqual(odd['send_to_receive_return']['most_common'][0],
                         {'ticks': 2990, 'count': 375})
        self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()

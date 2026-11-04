"""Negative controls for the three-class owner's useful-work accounting."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location('owner_campaign_controls', Path(__file__).with_name('owner_campaign.py'))
owner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(owner)


def fixture(case='owner-select-burst-1'):
    backlog = case.endswith('backlog')
    samples = 1 if backlog else 16
    meta = dict(schema='owner-path-v1', record='experiment', case=case, build_id='unrecorded-host',
                runtime='host-functional-only', samples=samples, clock_hz=1_000_000_000,
                counter_bits=32, capacity=8, stop_capacity=1, important_burst=8,
                payload_bytes=16, worker_stack=16384, physical_timing_qualified=False,
                priority_progress_qualified=False)
    provider = dict(schema='owner-path-v1', record='provider', provider=0, tid=1, policy=0, priority=0)
    attempts = [dict(schema='owner-path-v1', record='attempt', provider=0, sequence=i,
                     checksum=owner.p2.payload_checksum('bytes16', i, 0), start_ticks=10,
                     admission_return_ticks=20, accepted=True, discarded=False,
                     handler_start_ticks=40, handler_end_ticks=50, elapsed_upper_ns=100)
                for i in range(1, 17)]
    ready = '-ready-' in case
    result = dict(schema='owner-path-v1', record='result', success=True, attempted=16,
                  admitted=16, rejected=0, handled=16, discarded=0, select_calls=0 if ready else 16,
                  ready_hits=16 if ready else 0, deadline_turns=0, stop_request_ticks=100,
                  stop_observed_ticks=120, owner_tid=1 if backlog else 2, owner_policy=0,
                  owner_priority=0, elapsed_ns=1000, threads_joined=True, actual_switch_count=None,
                  cpu_time_ns=None, drain_ticks=700 if backlog else None,
                  physical_timing_qualified=False, priority_progress_qualified=False)
    return [meta, provider, *attempts, result]


def transcript(rows):
    return '\n'.join(owner.PREFIX + json.dumps(row) for row in rows) + '\n'


class OwnerEvidenceTests(unittest.TestCase):
    def test_backlog_reports_both_strategies_without_promoting_host_results(self):
        for strategy in ('select', 'ready'):
            result = owner.analyze(transcript(fixture(f'owner-{strategy}-backlog')))
            self.assertTrue(result['structural_valid'])
            self.assertFalse(result['physical_timing_qualified'])
            self.assertEqual(result['raw_ticks']['admission_to_handler_start']['p50_ticks'], 30)

    def test_rejection_and_stop_discard_remain_distinct(self):
        rows = fixture()
        rows[2].update(accepted=False, handler_start_ticks=None, handler_end_ticks=None)
        rows[3].update(discarded=True, handler_start_ticks=None, handler_end_ticks=None)
        rows[-1].update(admitted=15, rejected=1, handled=14, discarded=1)
        result = owner.analyze(transcript(rows))
        self.assertEqual(result['result']['admitted'], 15)
        self.assertEqual(result['raw_ticks']['admission_to_handler_start']['count'], 14)

    def test_counter_wrap_is_retained_but_reversal_refuses(self):
        rows = fixture()
        rows[2].update(start_ticks=(1 << 32) - 10, admission_return_ticks=0,
                       handler_start_ticks=20, handler_end_ticks=30)
        owner.analyze(transcript(rows))
        rows[2]['handler_end_ticks'] = 10
        with self.assertRaises(ValueError):
            owner.analyze(transcript(rows))

    def test_corrupt_identity_checksum_and_work_counts_refuse(self):
        for index, key, value in [(2, 'checksum', 0), (2, 'sequence', 2), (2, 'accepted', 1),
                                  (-1, 'handled', 15), (-1, 'discarded', 1),
                                  (-1, 'threads_joined', False), (0, 'capacity', 16),
                                  (0, 'clock_hz', 0)]:
            rows = fixture()
            rows[index][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                owner.analyze(transcript(rows))

    def test_unhandled_values_cannot_claim_latency(self):
        rows = fixture()
        rows[2].update(accepted=False)
        rows[-1].update(admitted=15, handled=15, rejected=1)
        with self.assertRaises(ValueError):
            owner.analyze(transcript(rows))

    def test_missing_extra_or_mixed_records_refuse(self):
        rows = fixture()
        for broken in (rows[:-1], rows + rows, rows[:2] + rows[3:], rows + [rows[2]]):
            with self.assertRaises(ValueError):
                owner.analyze(transcript(broken))

    def test_unsupported_cpu_switch_and_qualification_claims_refuse(self):
        for key, value in [('cpu_time_ns', 10), ('actual_switch_count', 1),
                           ('physical_timing_qualified', True), ('priority_progress_qualified', 1)]:
            rows = fixture()
            rows[-1][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                owner.analyze(transcript(rows))

    def test_backlog_must_keep_work_and_selector_counts_comparable(self):
        for key, value in [('select_calls', 1), ('ready_hits', 15), ('drain_ticks', 0)]:
            rows = fixture('owner-ready-backlog')
            rows[-1][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                owner.analyze(transcript(rows))

    def test_duplicate_json_fields_and_native_faults_refuse(self):
        raw = transcript(fixture())
        with self.assertRaises(ValueError):
            owner.analyze(raw.replace('"samples": 16', '"samples": 16, "samples": 16'))
        with self.assertRaises(ValueError):
            owner.analyze('PANIC\n' + raw)

    def test_native_schedule_requires_equal_fifo_actors_but_never_qualifies_progress(self):
        rows = fixture()
        rows[0]['runtime'] = 'nuttx-unqualified'
        rows[1].update(policy=1, priority=180)
        rows[-1].update(owner_policy=1, owner_priority=180)
        result = owner.analyze(transcript(rows))
        self.assertFalse(result['priority_progress_qualified'])
        rows[1]['priority'] = 120
        with self.assertRaises(ValueError):
            owner.analyze(transcript(rows))


if __name__ == '__main__':
    unittest.main()

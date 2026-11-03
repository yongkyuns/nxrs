"""Negative controls for evidence that could otherwise produce false performance claims."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

import analyze


def transcript(metadata, samples, result):
    return '\n'.join(analyze.PREFIX + json.dumps(row) for row in [metadata, *samples, result]) + '\n'


def fixture(mode='latency', runtime='nuttx'):
    common = dict(schema=analyze.SCHEMA, run_id='test-run')
    metadata = dict(common, record='metadata', mode=mode, runtime=runtime,
                    hook_stage=analyze.MODES[mode], capacity=8, payload_bytes=16,
                    samples=2, warmup=1, clock_hz=100_000_000, counter_bits=32,
                    clock_source='nuttx-arch-perf', build_id='b' * 64,
                    clock_source_request='baseline',
                    witness_api='recv' if mode == 'latency' else 'try_recv',
                    blocked_observation_scope='WAIT_SEM-inside-recv',
                    physical_measurement=False, single_core_tcb_witness=runtime == 'nuttx',
                    sender_policy=1, sender_priority=120, receiver_policy=1, receiver_priority=180,
                    observer_policy=1, observer_priority=120 if mode == 'latency' else 220,
                    watchdog_policy=1, watchdog_priority=240, native_counter_bits=32,
                    monotonic_resolution_ns=1000, counter_probe_ns=20_000_000,
                    counter_probe_ticks=2_000_000, counter_validated=True)
    metadata['counter_probe_kind'] = 'active-monotonic-plausibility'
    if mode == 'latency':
        metadata['terminal_quiescence_protocol'] = 'final-gate-native-wait-v1'
    samples = [dict(common, record='sample', index=i, send_start_ticks=1000 + i * 100,
                    send_return_ticks=1040 + i * 100, receive_return_ticks=1030 + i * 100,
                    sequence_valid=True, elapsed_upper_ns=10000,
                    blocked_verified=runtime == 'nuttx') for i in range(2)]
    result = dict(common, record='result', success=True, threads_joined=True, deadline_hit=False,
                  count=2, blocked_verified_count=2 if runtime == 'nuttx' else 0,
                  counter_resolved=True)
    if mode == 'latency':
        result['terminal_quiescence_verified'] = runtime == 'nuttx'
    if mode != 'latency':
        samples = []
        metadata.update(samples=0, warmup=0)
        done = mode == 'witness-published'
        result.update(hook_hit_count=1, consumer_entered=True, delivered=True,
                      consumer_done_before_recovery=done, producer_done_before_recovery=done,
                      recovery_applied=not done, observation_ns=100_000_000,
                      consumer_state_at_deadline='ready', producer_state_at_deadline='ready',
                      initial_gate_waits_verified=True, consumer_woken_from_hook=True,
                      observation_deadline_reached=True, hook_returned_before_recovery=done,
                      hook_timestamp_ns=1000, consumer_enter_timestamp_ns=2000,
                      observer_timestamp_ns=100_002_000, consumer_priority_at_deadline=180,
                      producer_priority_at_deadline=120, reservation_stall_observed=not done)
    return metadata, samples, result


class TranscriptTests(unittest.TestCase):
    def test_receiver_may_return_before_sender(self):
        result = analyze.analyze(transcript(*fixture()))
        self.assertEqual(result['send_to_receive_return']['p99_ticks'], 30)
        self.assertEqual(result['send_call']['p99_ticks'], 40)
        self.assertFalse(result['physical_timing_qualified'])
        self.assertFalse(result['priority_progress_qualified'])

    def test_real_counter_wrap_is_preserved(self):
        metadata, samples, result = fixture()
        samples[0].update(send_start_ticks=(1 << 32) - 20, receive_return_ticks=10, send_return_ticks=20)
        actual = analyze.analyze(transcript(metadata, samples, result))
        self.assertEqual(actual['send_to_receive_return']['max_ticks'], 30)

    def test_nearest_rank_does_not_invent_interpolated_observations(self):
        stats = analyze.distribution([10, 20, 30, 40], 1000)
        self.assertEqual((stats['p50_ticks'], stats['p99_ticks']), (20, 40))

    def test_host_functional_is_not_blocked_or_physical_qualification(self):
        result = analyze.analyze(transcript(*fixture(runtime='linux-functional')))
        self.assertEqual(result['outcome'], 'host-functional-only')
        self.assertFalse(result['receiver_blocked_verified'])
        self.assertFalse(result['physical_timing_qualified'])

    def test_observed_reservation_stall_never_qualifies_channel_progress(self):
        result = analyze.analyze(transcript(*fixture('witness-reserved')))
        self.assertEqual(result['outcome'], 'reservation-stall-observed')
        self.assertFalse(result['progress_contract_satisfied'])
        self.assertFalse(result['priority_progress_qualified'])

    def test_published_control_proves_only_its_control_interleaving(self):
        result = analyze.analyze(transcript(*fixture('witness-published')))
        self.assertTrue(result['progress_contract_satisfied'])
        self.assertFalse(result['priority_progress_qualified'])

    def test_rejects_incomplete_or_mixed_run(self):
        text = transcript(*fixture())
        for broken in [text.rsplit('\n', 2)[0] + '\n', text + text, text.replace('test-run', 'other', 1),
                       text + 'panicked at fixture.rs:12\n', text.replace(analyze.PREFIX, 'partial ' + analyze.PREFIX, 1)]:
            with self.subTest(broken=broken[-120:]), self.assertRaises(ValueError):
                analyze.analyze(broken)

    def test_rejects_duplicate_json_keys(self):
        text = transcript(*fixture()).replace('"schema":', '"schema":"wrong", "schema":', 1)
        with self.assertRaisesRegex(ValueError, 'duplicate JSON'):
            analyze.analyze(text)

    def test_rejects_false_block_claim_and_instrumented_latency(self):
        for target, field, value in [
            ('sample', 'blocked_verified', False), ('sample', 'sequence_valid', False),
            ('sample', 'index', 4), ('sample', 'send_return_ticks', 999),
            ('sample', 'send_return_ticks', True), ('metadata', 'hook_stage', 1),
            ('result', 'threads_joined', False), ('result', 'deadline_hit', True),
            ('result', 'blocked_verified_count', 0), ('result', 'terminal_quiescence_verified', False),
            ('metadata', 'samples', 3),
            ('sample', 'elapsed_upper_ns', 100_000_000_000),
            ('sample', 'elapsed_upper_ns', 1), ('metadata', 'counter_validated', False),
            ('metadata', 'receiver_policy', 2), ('metadata', 'single_core_tcb_witness', False),
        ]:
            metadata, samples, result = fixture()
            row = {'sample': samples[0], 'metadata': metadata, 'result': result}[target]
            row[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                analyze.analyze(transcript(metadata, samples, result))

    def test_rejects_a_published_control_that_needed_rescue(self):
        metadata, samples, result = fixture('witness-published')
        result['recovery_applied'] = True
        with self.assertRaisesRegex(ValueError, 'control failed'):
            analyze.analyze(transcript(metadata, samples, result))

    def test_tick_clock_and_underresolved_samples_are_retained_without_timing_qualification(self):
        metadata, samples, result = fixture()
        metadata['clock_source'] = 'nuttx-system-ticks'
        metadata['counter_validated'] = result['counter_resolved'] = False
        measured = analyze.analyze(transcript(metadata, samples, result))
        self.assertFalse(measured['timing_resolution_sufficient'])
        metadata['clock_source'] = 'nuttx-arch-perf'
        metadata['counter_validated'] = True
        samples[0]['receive_return_ticks'] = samples[0]['send_start_ticks']
        result['counter_resolved'] = False
        measured = analyze.analyze(transcript(metadata, samples, result))
        self.assertFalse(measured['timing_resolution_sufficient'])

    def test_historical_no_final_gate_retains_samples_without_timing_qualification(self):
        metadata, samples, result = fixture()
        metadata.pop('terminal_quiescence_protocol')
        result.pop('terminal_quiescence_verified')
        summary = analyze.analyze(transcript(metadata, samples, result))
        self.assertTrue(summary['receiver_blocked_verified'])
        self.assertFalse(summary['terminal_quiescence_verified'])
        self.assertEqual(summary['terminal_gate_protocol'], 'historical-no-final-gate')
        # An old log remains readable but cannot become physically timing-qualified.
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            image = base / 'image'
            config = base / 'config'
            image.write_bytes(b'firmware')
            config.write_bytes(b'config')
            receipt = dict(mode='latency', clock_request='baseline', build_id='b' * 64,
                           image_sha256=analyze.sha256(image))
            session = dict(schema='std-channel-session-v1', runtime_kind='physical',
                           image_sha256=receipt['image_sha256'], active_cores=1, cpu_clock_hz=100_000_000,
                           measurement_session='historical', board='test', board_serial='test',
                           clock_verification='historical', cache_state='on', memory_placement='test',
                           power_management='off', external_load='none', flash_verification='test')
            qualified = analyze.qualify(summary, receipt, session, fresh_boot_observed=True)
            self.assertFalse(qualified['physical_timing_qualified'])

    def test_counter_probe_boolean_cannot_hide_wrong_frequency(self):
        metadata, samples, result = fixture()
        metadata['counter_probe_ticks'] = 20_000
        with self.assertRaisesRegex(ValueError, 'raw probe'):
            analyze.analyze(transcript(metadata, samples, result))

    def test_monotonic_fallback_is_functional_without_timing_qualification(self):
        metadata, samples, result = fixture()
        metadata.update(clock_source='nuttx-monotonic', counter_validated=False)
        result['counter_resolved'] = False
        measured = analyze.analyze(transcript(metadata, samples, result))
        self.assertTrue(measured['receiver_blocked_verified'])
        self.assertFalse(measured['timing_resolution_sufficient'])

    def test_wrong_witness_schedule_state_or_wake_cannot_pass(self):
        for field, value in [('consumer_state_at_deadline', 'wait_sem'),
                             ('hook_returned_before_recovery', True),
                             ('initial_gate_waits_verified', False),
                             ('consumer_woken_from_hook', False),
                             ('consumer_priority_at_deadline', 100),
                             ('observation_ns', 1), ('consumer_enter_timestamp_ns', 0)]:
            metadata, samples, result = fixture('witness-reserved')
            result[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                analyze.analyze(transcript(metadata, samples, result))


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'image').write_bytes(b'test image')
        (self.root / 'config').write_text('CONFIG_SMP=n\n')
        self.receipt = dict(schema='std-channel-build-v1', status='complete', mode='latency', hook_stage=0,
                            image_file='image', image_sha256=analyze.sha256(self.root / 'image'),
                            elf_file='image', elf_sha256=analyze.sha256(self.root / 'image'),
                            config_file='config', config_sha256=analyze.sha256(self.root / 'config'),
                            std_array_sha256='a' * 64, std_array_original_sha256='a' * 64,
                            compiler_version='test compiler', kernel_revision='test kernel', build_id='b' * 64,
                            clock_request='baseline')
        self.session = dict(schema='std-channel-session-v1', runtime_kind='physical',
                            image_sha256=self.receipt['image_sha256'], measurement_session='test',
                            board='test fixture', board_serial='test-only', clock_verification='test calibration',
                            cache_state='off', memory_placement='internal SRAM', power_management='disabled',
                            external_load='none', flash_verification='test readback', active_cores=1,
                            cpu_clock_hz=100_000_000)

    def write_receipt(self):
        (self.root / 'build.json').write_text(json.dumps(self.receipt))

    def test_artifact_digest_is_rechecked(self):
        self.write_receipt()
        analyze.build_receipt(self.root, 'latency')
        (self.root / 'image').write_bytes(b'different image')
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            analyze.build_receipt(self.root, 'latency')

    def test_artifact_path_cannot_escape_build(self):
        self.receipt['image_file'] = '../outside'
        self.write_receipt()
        with self.assertRaisesRegex(ValueError, 'escapes|missing'):
            analyze.build_receipt(self.root, 'latency')

    def test_hidden_source_instrumentation_rejected(self):
        self.receipt['std_array_sha256'] = 'b' * 64
        self.write_receipt()
        with self.assertRaisesRegex(ValueError, 'instrumentation'):
            analyze.build_receipt(self.root, 'latency')

    def test_physical_needs_complete_session_and_fresh_boot(self):
        summary = analyze.analyze(transcript(*fixture()))
        with self.assertRaisesRegex(ValueError, 'fresh boot'):
            analyze.qualify(summary, self.receipt, self.session)
        for field in ['clock_verification', 'board_serial', 'flash_verification']:
            session = copy.deepcopy(self.session)
            del session[field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                analyze.qualify(summary, self.receipt, session, fresh_boot_observed=True)
        result = analyze.qualify(summary, self.receipt, self.session, fresh_boot_observed=True)
        self.assertTrue(result['physical_timing_qualified'])
        self.assertFalse(result['hard_realtime_bound'])
        self.assertIn('operator-supplied', result['hardware_identity_origin'])

    def test_qemu_can_never_become_physical_timing(self):
        summary = analyze.analyze(transcript(*fixture()))
        self.session['runtime_kind'] = 'qemu'
        result = analyze.qualify(summary, self.receipt, self.session, fresh_boot_observed=True)
        self.assertFalse(result['physical_timing_qualified'])

    def test_cold_run_retained_without_steady_state_timing_qualification(self):
        metadata, samples, result = fixture()
        metadata['warmup'] = 0
        summary = analyze.analyze(transcript(metadata, samples, result))
        qualified = analyze.qualify(summary, self.receipt, self.session, fresh_boot_observed=True)
        self.assertFalse(qualified['physical_timing_qualified'])

    def test_stale_firmware_build_identity_rejected(self):
        metadata, samples, result = fixture()
        metadata['build_id'] = 'c' * 64
        summary = analyze.analyze(transcript(metadata, samples, result))
        with self.assertRaisesRegex(ValueError, 'build identity mismatch'):
            analyze.qualify(summary, self.receipt, self.session, fresh_boot_observed=True)

    def test_template_values_and_wrong_physical_clock_rejected(self):
        metadata, samples, result = fixture()
        metadata.update(clock_source_request='native-counter', clock_source='xtensa-ccount')
        summary = analyze.analyze(transcript(metadata, samples, result))
        receipt = dict(self.receipt, clock_request='native-counter')
        for field, value in [('clock_verification', 'REPLACE-with-calibration'), ('cpu_clock_hz', 80_000_000)]:
            session = dict(self.session, **{field: value})
            with self.subTest(field=field), self.assertRaises(ValueError):
                analyze.qualify(summary, receipt, session, fresh_boot_observed=True)

    def test_wrong_flashing_or_core_count_rejected(self):
        summary = analyze.analyze(transcript(*fixture()))
        for field, value in [('image_sha256', 'bad'), ('active_cores', 2)]:
            session = dict(self.session, **{field: value})
            with self.subTest(field=field), self.assertRaises(ValueError):
                analyze.qualify(summary, self.receipt, session, fresh_boot_observed=True)


if __name__ == '__main__':
    unittest.main()

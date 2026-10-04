import unittest
import hashlib
from pathlib import Path
import tempfile
from transport_pipeline_report import heap_accounting, validate_artifacts, validate_policy, validate_threads, validate_transport

def transcript(language='rust', ack=16, cycles=96000000):
    prefix = 'CQ_C_' if language == 'c' else 'CQ_'
    fields = ' '.join(f'{key}=100' for key in (
        'elapsed_us', 'rx_p50_us', 'rx_p99_us', 'rx_max_us', 'ack_p99_us',
        'heap_before', 'heap_queues', 'heap_workers', 'heap_producers', 'heap_after_join',
        'stack_main', 'stack_producer_max', 'stack_worker_max', 'stack_collector'))
    return (f'CQ_TRANSPORT_PASS language={language} mode=large cycles={cycles} event_bytes=248 ack_bytes={ack}\n'
            f'{prefix}SCALE_PASS mode=large queues=60 logical_streams=60 threads=20 messages=5760 digest=441445568 {fields}\n'
            f'{prefix}WAKE_PASS trials=64 queues=3 threads=2 sleep_clock_us=100 rx_p50_us=100 rx_p99_us=100 rx_max_us=100 ack_p99_us=100\n')

class TransportReportTests(unittest.TestCase):
    def test_peak_accounting_excludes_command_stack_as_well_as_spawned_threads(self):
        workload = {key: {'median': value} for key, value in
                    [('heap_before', 15284), ('heap_queues', 36100), ('heap_producers', 124708)]}
        result = heap_accounting(workload, 88608, 103892, 140112)
        self.assertEqual(result['above_idle_thread_heap'], 20816)
        self.assertEqual(result['peak_above_idle_thread_heap'], 36220)
        self.assertNotEqual(result['peak_above_idle_thread_heap'], 140112 - 7164 - 88608)
        with self.assertRaises(ValueError): heap_accounting(workload, 88608, 103892, 100000)

    def test_changed_frozen_artifact_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'image').write_bytes(b'original')
            build = dict(artifacts={'image': hashlib.sha256(b'original').hexdigest()})
            validate_artifacts(root, build)
            (root / 'image').write_bytes(b'changed')
            with self.assertRaises(ValueError): validate_artifacts(root, build)

    def test_prevents_wire_image_from_being_called_packet_processing(self):
        c = dict(c_defines=['NXRS_CQ_DEVICE','NXRS_CQ_SYNCHRONIZED','NXRS_CQ_SPEED',
                            'NXRS_CQ_PACKET_SERVICE','NXRS_PAYLOAD_C_SPEED'])
        validate_policy(c, 'c-packet-2', 'c', 28)
        c['c_defines'].append('NXRS_CQ_WIRE_ONLY')
        with self.assertRaises(ValueError): validate_policy(c, 'c-packet-2', 'c', 28)
        rust = dict(features=['mq-backend','native-scale-worker','ffi-scale-entry','shared-mq-code',
                             'synchronized-scale','packet-service','borrowed-mq-io'], app_opt_level='2',
                    std_source=dict(selected_source_matches=True))
        validate_policy(rust, 'rust-packet-2', 'rust', 28)
        rust['app_opt_level'] = 'z'
        with self.assertRaises(ValueError): validate_policy(rust, 'rust-packet-2', 'rust', 28)
    def test_cycle_clock_is_converted_without_coarse_elapsed(self):
        for language in ('c', 'rust'):
            result = validate_transport(transcript(language), language, 1, 16)
            self.assertEqual(result['traffic_us']['median'], 400000)
            self.assertEqual(result['workload']['elapsed_us']['median'], 100)

    def test_rejects_layout_clock_digest_topology_and_failures(self):
        valid = transcript()
        bad = [valid.replace('ack_bytes=16', 'ack_bytes=28'),
               valid.replace('language=rust', 'language=c'),
               valid.replace('queues=60', 'queues=59'),
               valid.replace('digest=441445568', 'digest=0'),
               valid.replace('cycles=96000000', 'cycles=0'),
               valid.replace('cycles=96000000', 'cycles=3840000000'),
               valid + 'CQ_SCALE_FAIL error\n', valid + valid]
        for text in bad:
            with self.subTest(text=text), self.assertRaises(ValueError):
                validate_transport(text, 'rust', 1, 16)

    def test_thread_baseline_must_match_and_clean_up(self):
        valid = 'CQ_THREADS_PASS threads=20 heap_before=100 heap_live=85108 heap_after=100\n'
        self.assertEqual(validate_threads(valid, 1)['median'], 85008)
        for text in (valid.replace('threads=20', 'threads=19'),
                     valid.replace('heap_live=85108', 'heap_live=100')):
            with self.assertRaises(ValueError): validate_threads(text, 1)
        self.assertEqual(validate_threads(valid.replace('heap_after=100', 'heap_after=4200'), 1)['median'], 85008)

if __name__ == '__main__': unittest.main()

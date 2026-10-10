import unittest
from unittest.mock import patch
from measure import validate, read_command


def output(ack=28):
    return (f'CQ_TRANSPORT_PASS language=embassy mode=large cycles=2400000 event_bytes=248 ack_bytes={ack}\n'
            'CQ_EMBASSY_SCALE_PASS mode=large queues=60 logical_streams=60 tasks=20 messages=5760 '
            'digest=441445568 rx_p50_us=10 rx_p99_us=20 rx_max_us=30 ack_p50_us=10 '
            'ack_p99_us=20 ack_max_us=30 samples=720\n'
            f'EMBASSY_RESOURCES heap_allocated=0 queue_buffers={45*248+60*ack} '
            'channel_storage=14400 shared_stack=8192 tasks=20 queues=60\n'
            'EMBASSY_COMMAND_EXIT status=0\nembassy> ').encode()


class MeasurementTests(unittest.TestCase):
    def test_late_blank_prompt_is_not_command_completion(self):
        with patch('measure.shared.read_prompt', side_effect=[b'embassy> ', output()]):
            self.assertEqual(validate(read_command(123), 'packet')['scale']['messages'], 5760)

    def test_wire_packet_and_baseline(self):
        self.assertEqual(validate(output(), 'packet')['scale']['messages'], 5760)
        self.assertEqual(validate(output(16), 'wire')['transport']['ack_bytes'], 16)
        validate(b'EMBASSY_BASELINE_PASS heap_allocated=0 shared_stack=8192\nEMBASSY_COMMAND_EXIT status=0\n', 'baseline')

    def test_contract_failures(self):
        for old, new in [(b'tasks=20', b'tasks=19'), (b'queues=60', b'queues=59'),
                         (b'heap_allocated=0', b'heap_allocated=1'), (b'status=0', b'status=1'),
                         (b'cycles=2400000', b'cycles=0'), (b'samples=720', b'samples=719'),
                         (b'rx_p99_us=20', b'rx_p99_us=31'), (b'digest=441445568', b'digest=0')]:
            with self.assertRaises(ValueError):
                validate(output().replace(old, new), 'packet')
        with self.assertRaises(ValueError):
            validate(output() + output(), 'packet')
        with self.assertRaises(ValueError):
            validate(output(), 'wire')


if __name__ == '__main__':
    unittest.main()

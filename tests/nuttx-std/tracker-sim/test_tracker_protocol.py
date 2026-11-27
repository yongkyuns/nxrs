"""Protocol witnesses independent of firmware, Bluetooth and Apple SDKs."""
import ast
import math
from pathlib import Path
import struct
from types import SimpleNamespace
import unittest

from tracker_protocol import (
    CaptureFault, CaptureOverflow, Measurement, NoFix,
    decode_batch, decode_frame, preferred_version,
)


def frame(payload, version=4):
    return struct.pack('<BBH', 0xa5, version, len(payload)) + payload


def imu(tag=1, sequence=9, timestamp=1000, *, version=4, values=(1.25, -.5, 9.75, 0, 1, -2)):
    return (bytes([tag]) + struct.pack('<IQ' if version >= 3 else '<II', sequence, timestamp)
            + struct.pack('<6f', *values))


def no_fix(generation=7, received=1000, epoch=500, tag=4):
    return struct.pack('<BIQQ', tag, generation, received, epoch)


class TrackerProtocolTests(unittest.TestCase):
    def test_rust_no_fix_and_terminal_goldens(self):
        # These exact bytes are also asserted by the Rust export encoder tests.
        golden = bytes.fromhex('a5041d0000000000000000800407000000e803000000000000f401000000000000')
        self.assertEqual(golden, frame(struct.pack('<q', -(1 << 63)) + no_fix()))
        self.assertEqual(decode_frame(golden), [NoFix(7, 1000, 500)])
        for encoded, source, cause in [('a50402000103', 1, 3), ('a50402000307', 3, 7)]:
            with self.assertRaises(CaptureFault) as caught:
                decode_frame(bytes.fromhex(encoded))
            self.assertEqual((caught.exception.source, caught.exception.cause), (source, cause))

    def test_rust_receipt_sample_preserves_unknown_utc(self):
        encoded = frame(struct.pack('<q', -(1 << 63)) + imu(tag=0x31))
        self.assertEqual(encoded[:5], bytes.fromhex('a5042d0000'))
        records = decode_frame(encoded)
        self.assertEqual(records, [Measurement(1, 9, 1000, None, (1.25, -.5, 9.75, 0, 1, -2), 'received')])

    def test_v4_counts_deltas_no_fix_and_per_record_provenance(self):
        counts = struct.pack('<6h', 8, -8, 0, 4, -4, 0)
        first = bytes([0x71]) + struct.pack('<IQ', 7, 100) + counts
        second = bytes([0xc1, 10]) + counts
        payload = struct.pack('<q', 1000) + first + no_fix(received=105, epoch=(1 << 64) - 1) + second
        records = decode_frame(frame(payload), ((.125, .25), None))
        self.assertEqual(records, [
            Measurement(1, 7, 100, None, (1, -1, 0, 1, -1, 0), 'received'),
            NoFix(7, 105, None),
            Measurement(1, 8, 110, 1110, (1, -1, 0, 1, -1, 0), 'capture'),
        ])
        self.assertEqual(tuple(records[0]), (1, 7, 100, None, (1, -1, 0, 1, -1, 0)))

    def test_legacy_versions_preserve_unknown_provenance(self):
        for version in (1, 2, 3):
            payload = (struct.pack('<q', 1000) if version == 3 else b'') + imu(version=version)
            sample, = decode_frame(frame(payload, version))
            self.assertEqual(sample.provenance, 'unknown')
            self.assertEqual(sample.utc_ms, 2000 if version == 3 else None)
        fixed = imu(sequence=0xffffffff, timestamp=0xfffffffa, version=2)
        delta = bytes([0x81, 11]) + struct.pack('<6f', *([0] * 6))
        records = decode_frame(frame(fixed + delta, 2))
        self.assertEqual((records[-1].sequence, records[-1].timestamp_ms), (0, 5))

    def test_typed_fault_range_and_legacy_overflow(self):
        for source in (1, 2, 3):
            for cause in range(1, 8):
                with self.subTest(source=source, cause=cause), self.assertRaises(CaptureFault):
                    decode_frame(frame(bytes([source, cause])))
        for source, cause in [(0, 1), (4, 1), (1, 0), (1, 8), (255, 255)]:
            with self.subTest(source=source, cause=cause), self.assertRaises(ValueError) as caught:
                decode_frame(frame(bytes([source, cause])))
            self.assertNotIsInstance(caught.exception, CaptureFault)
        for version in (2, 3, 4):
            with self.assertRaises(CaptureOverflow) as caught:
                decode_frame(frame(struct.pack('<BI', 2, 800), version))
            self.assertEqual((caught.exception.source, caught.exception.sequence), (2, 800))
        with self.assertRaises(CaptureOverflow):
            decode_frame(frame(b'', 1))

    def test_reserved_flags_truncation_limits_and_unknown_version(self):
        unknown = struct.pack('<q', -(1 << 63))
        invalid = [frame(unknown + no_fix(tag=flag | 4)) for flag in (0x10, 0x20, 0x40, 0x80)]
        invalid += [
            frame(unknown + no_fix()[:-1]), frame(unknown + no_fix() + b'\0'),
            frame(unknown + no_fix() * 17), frame(unknown + imu(tag=0)),
            frame(unknown + imu(tag=5)), frame(unknown + imu(tag=0x43)),
            frame(unknown + imu(), 5), frame(unknown + imu())[:-1],
            frame(unknown + imu()) + b'\0', bytes.fromhex('a504ffff'),
        ]
        for encoded in invalid:
            with self.subTest(encoded=encoded.hex()), self.assertRaises(ValueError):
                decode_frame(encoded)

    def test_bad_delta_clock_ranges_scales_and_nonfinite_values(self):
        unknown = struct.pack('<q', -(1 << 63))
        axes = struct.pack('<6f', *([0] * 6))
        delta = bytes([0x81, 1]) + axes
        invalid = [
            frame(unknown + delta + delta),
            frame(unknown + imu(timestamp=(1 << 64) - 1) + delta),
            frame(struct.pack('<q', (1 << 63) - 1) + imu(timestamp=1)),
            frame(struct.pack('<q', 0) + imu(timestamp=1 << 63)),
            frame(unknown + imu(values=(math.nan, 0, 0, 0, 0, 0))),
        ]
        for encoded in invalid:
            with self.subTest(encoded=encoded.hex()), self.assertRaises(ValueError):
                decode_frame(encoded)
        counts = frame(unknown + bytes([0x41]) + struct.pack('<IQ6h', 1, 1, *([32767] * 6)))
        for scale in (None, (0, 1), (math.inf, 1), (3e38, 3e38)):
            with self.subTest(scale=scale), self.assertRaises(ValueError):
                decode_frame(counts, (scale, None))

    def test_v4_utc_requires_capture_qualification_and_a_mapping(self):
        known = struct.pack('<q', 500)
        samples = decode_batch(known + imu() + imu(tag=0x31, sequence=10) + imu(tag=0x21, sequence=11), 4, (None, None))
        self.assertEqual((samples[0].utc_ms, samples[0].provenance), (1500, 'capture'))
        self.assertEqual((samples[1].utc_ms, samples[1].provenance), (None, 'received'))
        self.assertEqual((samples[2].utc_ms, samples[2].provenance), (None, 'capture'))
        for offset in (500, -(1 << 63)):
            with self.assertRaisesRegex(ValueError, 'without NO_UTC'):
                decode_frame(frame(struct.pack('<q', offset) + imu(tag=0x11)))

    def test_request_negotiation_keeps_older_peers(self):
        for capabilities, version in [({}, 1), ({'export_version': 2}, 2),
                                      ({'export_version': 2, 'imu_scale_bits': [None, None]}, 3),
                                      ({'export_version': 3}, 3), ({'export_version': 4}, 4),
                                      ({'export_version': 5}, 4)]:
            self.assertEqual(preferred_version(capabilities), version)

    def test_actual_peer_counters_keep_no_fix_separate_from_measurements(self):
        # Load the actual task-context counter methods without importing the
        # optional Bluetooth/QR SDKs. No counter behavior is reimplemented here.
        path = Path(__file__).with_name('dashboard.py')
        tree = ast.parse(path.read_text())
        peer = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'SecureStream')
        methods = [node for node in peer.body if isinstance(node, ast.FunctionDef)
                   and node.name in ('count_export', 'observe_no_fix')]
        peer.name = 'Counter'
        peer.body = methods
        module = ast.fix_missing_locations(ast.Module(body=[peer], type_ignores=[]))
        context = {'NoFix': NoFix, 'time': SimpleNamespace(monotonic=lambda: 1.0)}
        exec(compile(module, str(path), 'exec'), context)
        counter = context['Counter']()
        counter.last_batch_received = None
        counter.max_batch_gap = 0
        counter.sample_sequences = [None] * 3
        counter.sample_counts = [0] * 3
        counter.sample_ticks = [(0, 0)] * 3
        counter.no_fix_count = 0
        counter.last_no_fix = None
        prefix = struct.pack('<q', -(1 << 63)) + imu(tag=0x31)
        prefix += no_fix() + struct.pack('<BIQddf', 0x33, 1, 1010, 45, -76, 2)
        counter.count_export((decode_frame(frame(prefix)), len(prefix)))
        followup = struct.pack('<q', -(1 << 63)) + no_fix(received=1020)
        followup += struct.pack('<BIQddf', 0x33, 2, 1030, 45, -76, 2)
        counter.count_export((decode_frame(frame(followup)), len(followup)))
        self.assertEqual(counter.sample_counts, [1, 0, 2])
        self.assertEqual(counter.sample_sequences, [9, None, 2])
        self.assertEqual(counter.no_fix_count, 2)
        self.assertEqual(counter.last_no_fix, NoFix(7, 1020, 500))


if __name__ == '__main__':
    unittest.main()

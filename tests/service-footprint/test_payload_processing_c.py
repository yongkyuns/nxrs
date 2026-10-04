"""Native C qualification tests for the standalone payload kernels."""
import ctypes
import random
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path


SAMPLES = 96
PACKET_BYTES = 200
INT16_MIN = -32768
INT16_MAX = 32767
HERE = Path(__file__).resolve().parent


def sample_pointer(values):
    return (ctypes.c_int16 * SAMPLES)(*values)


class PayloadProcessingCTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which('cc')
        if compiler is None:
            raise unittest.SkipTest('host C compiler unavailable')

        cls._build_dir = tempfile.TemporaryDirectory(prefix='nxrs-payload-c-')
        library_path = Path(cls._build_dir.name) / 'libpayload_processing.so'
        subprocess.run(
            [compiler, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
             '-shared', str(HERE / 'payload_processing.c'), '-o', str(library_path)],
            check=True, capture_output=True, text=True)
        cls.lib = ctypes.CDLL(str(library_path))
        byte_pointer = ctypes.POINTER(ctypes.c_uint8)
        sample_pointer_type = ctypes.POINTER(ctypes.c_int16)
        state_pointer = ctypes.POINTER(ctypes.c_int32)

        cls.lib.nxrs_c_encode.argtypes = [byte_pointer, ctypes.c_size_t,
                                          ctypes.c_uint32, sample_pointer_type]
        cls.lib.nxrs_c_encode.restype = ctypes.c_int
        cls.lib.nxrs_c_parse.argtypes = [byte_pointer, ctypes.c_size_t,
                                         sample_pointer_type,
                                         ctypes.POINTER(ctypes.c_uint32)]
        cls.lib.nxrs_c_parse.restype = ctypes.c_int
        cls.lib.nxrs_c_filter.argtypes = [sample_pointer_type, state_pointer,
                                          state_pointer]
        cls.lib.nxrs_c_filter.restype = None
        cls.lib.nxrs_c_filter_xyz.argtypes = [sample_pointer_type, state_pointer, state_pointer]
        cls.lib.nxrs_c_filter_xyz.restype = None

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, '_build_dir'):
            cls._build_dir.cleanup()

    @staticmethod
    def varied_samples(seed):
        rng = random.Random(seed)
        boundary = [INT16_MIN, INT16_MAX, -1, 0, 1, -256, 256, -12345, 23456]
        return [boundary[i % len(boundary)] if i % 3 else rng.randint(INT16_MIN, INT16_MAX)
                for i in range(SAMPLES)]

    def test_encode_matches_exact_little_endian_golden_packets(self):
        samples = [
            INT16_MIN, INT16_MAX, -1, 0, 1, 0x1234, -0x1234, -256,
            256,
        ]
        samples += self.varied_samples(71)[len(samples):]
        for sequence in (0, 1, 0x89ABCDEF, 0xFFFFFFFF):
            with self.subTest(sequence=sequence):
                packet = (ctypes.c_uint8 * PACKET_BYTES)(*[0xA5] * PACKET_BYTES)
                source = sample_pointer(samples)
                self.assertEqual(self.lib.nxrs_c_encode(
                    packet, PACKET_BYTES, sequence, source), 0)
                expected = (struct.pack('<I H B B', sequence, 32, 1, 0) +
                            struct.pack('<96h', *samples))
                self.assertEqual(bytes(packet), expected)

    def test_parse_decodes_golden_bytes_and_signed_boundaries(self):
        samples = [INT16_MIN, INT16_MAX, -1, 0, 1, -2, 2]
        samples += self.varied_samples(99)[len(samples):]
        sequence = 0xFEDCBA98
        wire = (struct.pack('<I H B B', sequence, 32, 1, 0) +
                struct.pack('<96h', *samples))
        packet = (ctypes.c_uint8 * PACKET_BYTES).from_buffer_copy(wire)
        decoded = (ctypes.c_int16 * SAMPLES)(*[123] * SAMPLES)
        decoded_sequence = ctypes.c_uint32(0x12345678)

        self.assertEqual(self.lib.nxrs_c_parse(
            packet, PACKET_BYTES, decoded, ctypes.byref(decoded_sequence)), 0)
        self.assertEqual(list(decoded), samples)
        self.assertEqual(decoded_sequence.value, sequence)

    def test_encode_rejects_bad_arguments_without_writing(self):
        samples = sample_pointer([0] * SAMPLES)
        for length in (PACKET_BYTES - 1, PACKET_BYTES + 1):
            with self.subTest(length=length):
                packet = (ctypes.c_uint8 * PACKET_BYTES)(*[0xA5] * PACKET_BYTES)
                self.assertEqual(self.lib.nxrs_c_encode(
                    packet, length, 7, samples), -1)
                self.assertEqual(bytes(packet), bytes([0xA5] * PACKET_BYTES))

        packet = (ctypes.c_uint8 * PACKET_BYTES)(*[0x5A] * PACKET_BYTES)
        original = bytes(packet)
        self.assertEqual(self.lib.nxrs_c_encode(
            None, PACKET_BYTES, 7, samples), -1)
        self.assertEqual(self.lib.nxrs_c_encode(
            packet, PACKET_BYTES, 7, None), -1)
        self.assertEqual(bytes(packet), original)

    def test_parse_rejects_malformed_input_without_writing_outputs(self):
        valid_wire = bytearray(struct.pack('<I H B B', 0x12345678, 32, 1, 0) +
                               struct.pack('<96h', *([0] * SAMPLES)))
        invalid_packets = []
        for offset, value in ((4, 31), (5, 1), (6, 2), (7, 1)):
            malformed = valid_wire.copy()
            malformed[offset] = value
            invalid_packets.append((f'header[{offset}]', malformed, PACKET_BYTES))
        invalid_packets.extend((
            ('short', valid_wire, PACKET_BYTES - 1),
            ('long', valid_wire, PACKET_BYTES + 1),
        ))

        for label, wire, length in invalid_packets:
            with self.subTest(case=label):
                packet = (ctypes.c_uint8 * PACKET_BYTES).from_buffer_copy(wire)
                decoded = (ctypes.c_int16 * SAMPLES)(*[0x3456] * SAMPLES)
                decoded_sequence = ctypes.c_uint32(0xCAFEBABE)
                self.assertEqual(self.lib.nxrs_c_parse(
                    packet, length, decoded, ctypes.byref(decoded_sequence)), -1)
                self.assertEqual(list(decoded), [0x3456] * SAMPLES)
                self.assertEqual(decoded_sequence.value, 0xCAFEBABE)

        decoded = (ctypes.c_int16 * SAMPLES)(*[0x2345] * SAMPLES)
        decoded_sequence = ctypes.c_uint32(0xDEADBEEF)
        valid_packet = (ctypes.c_uint8 * PACKET_BYTES).from_buffer_copy(valid_wire)
        self.assertEqual(self.lib.nxrs_c_parse(
            None, PACKET_BYTES, decoded, ctypes.byref(decoded_sequence)), -1)
        self.assertEqual(self.lib.nxrs_c_parse(
            valid_packet, PACKET_BYTES, None, ctypes.byref(decoded_sequence)), -1)
        self.assertEqual(self.lib.nxrs_c_parse(
            valid_packet, PACKET_BYTES, decoded, None), -1)
        self.assertEqual(list(decoded), [0x2345] * SAMPLES)
        self.assertEqual(decoded_sequence.value, 0xDEADBEEF)

    @staticmethod
    def trunc_divide_by_eight(value):
        return value // 8 if value >= 0 else -((-value) // 8)

    def test_filter_matches_independent_truncation_toward_zero_reference(self):
        rng = random.Random(20261003)
        cases = [
            ([INT16_MIN, 0, INT16_MAX], [INT16_MAX, INT16_MIN, -1] * 32),
            ([0, 0, 0], [INT16_MIN, INT16_MAX, -1, 1] * 24),
            ([-32767, 123, 32766],
             [rng.randint(INT16_MIN, INT16_MAX) for _ in range(SAMPLES)]),
        ]
        for initial_state, samples in cases:
            with self.subTest(initial_state=initial_state):
                expected_state = list(initial_state)
                expected_output = []
                for i, sample in enumerate(samples):
                    axis = i % 3
                    delta = self.trunc_divide_by_eight(sample - expected_state[axis])
                    expected_state[axis] += delta
                    expected_output.append(expected_state[axis])

                source = sample_pointer(samples)
                state = (ctypes.c_int32 * 3)(*initial_state)
                output = (ctypes.c_int32 * SAMPLES)()
                self.lib.nxrs_c_filter(source, state, output)
                self.assertEqual(list(output), expected_output)
                self.assertEqual(list(state), expected_state)
                grouped_state = (ctypes.c_int32 * 3)(*initial_state)
                grouped_output = (ctypes.c_int32 * SAMPLES)()
                self.lib.nxrs_c_filter_xyz(source, grouped_state, grouped_output)
                self.assertEqual(list(grouped_output), expected_output)
                self.assertEqual(list(grouped_state), expected_state)


if __name__ == '__main__':
    unittest.main()

"""Independent golden and failure-atomic tests for the C packet pipeline."""
import ctypes
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
SAMPLES = 96
PACKET_BYTES = 200
PAYLOAD_BYTES = 236


def token_for(lane, producer, sequence):
    return (lane << 24) | (producer << 16) | sequence


def golden_samples(token):
    return [(((token + i * 7919) & 0xFFFFFFFF) & 0xFFFF) - 32768
            for i in range(SAMPLES)]


def trunc_divide_by_eight(value):
    return value // 8 if value >= 0 else -((-value) // 8)


class PipelineProcessingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which('cc')
        if compiler is None:
            raise unittest.SkipTest('host C compiler unavailable')

        cls._build_dir = tempfile.TemporaryDirectory(prefix='nxrs-pipeline-c-')
        library = Path(cls._build_dir.name) / 'libpipeline_processing.so'
        subprocess.run(
            [compiler, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
             '-shared', str(HERE / 'pipeline_processing.c'),
             str(HERE / 'payload_processing.c'), '-o', str(library)],
            check=True, capture_output=True, text=True)
        cls.lib = ctypes.CDLL(str(library))
        byte_pointer = ctypes.POINTER(ctypes.c_uint8)
        state_pointer = ctypes.POINTER(ctypes.c_int32)
        cls.lib.nxrs_c_pipeline_build.argtypes = [
            byte_pointer, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
        cls.lib.nxrs_c_pipeline_build.restype = ctypes.c_int
        cls.lib.nxrs_c_pipeline_process.argtypes = [
            byte_pointer, ctypes.c_uint32, state_pointer, state_pointer]
        cls.lib.nxrs_c_pipeline_process.restype = ctypes.c_int

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, '_build_dir'):
            cls._build_dir.cleanup()

    @staticmethod
    def golden_packet(token):
        return (struct.pack('<I H B B', token, 32, 1, 0) +
                struct.pack('<96h', *golden_samples(token)))

    def test_build_matches_independent_packet_and_zero_tail(self):
        lane, producer, sequence = 0xFF, 0xFF, 0xFFFF
        token = token_for(lane, producer, sequence)
        payload = (ctypes.c_uint8 * PAYLOAD_BYTES)(*[0xA5] * PAYLOAD_BYTES)
        self.assertEqual(self.lib.nxrs_c_pipeline_build(
            payload, lane, producer, sequence), 0)
        self.assertEqual(bytes(payload[:PACKET_BYTES]), self.golden_packet(token))
        self.assertEqual(bytes(payload[PACKET_BYTES:]), bytes(36))

    def test_process_matches_independent_filter_and_commits_final_state(self):
        state_values = [1234, -2345, 32760]
        state = (ctypes.c_int32 * 3)(*state_values)
        result = (ctypes.c_int32 * 3)(-1, -1, -1)
        expected_state = state_values[:]

        for token in (token_for(1, 0, 0), token_for(12, 3, 8)):
            packet = self.golden_packet(token)
            payload = (ctypes.c_uint8 * PAYLOAD_BYTES)(
                *(packet + bytes(36)))
            expected_output = [0, 0, 0]
            for i, sample in enumerate(golden_samples(token)):
                axis = i % 3
                expected_state[axis] += trunc_divide_by_eight(
                    sample - expected_state[axis])
                expected_output[axis] = expected_state[axis]

            self.assertEqual(self.lib.nxrs_c_pipeline_process(
                payload, token, state, result), 0)
            self.assertEqual(list(state), expected_state)
            self.assertEqual(list(result), expected_output)

    def test_invalid_packet_or_token_preserves_state_and_result(self):
        token = token_for(4, 1, 9)
        packet = bytearray(self.golden_packet(token))
        cases = []
        bad_header = packet.copy()
        bad_header[4] = 31
        cases.append(('header', bad_header, token))
        cases.append(('token', packet, token ^ 1))

        for label, wire, supplied_token in cases:
            with self.subTest(case=label):
                payload = (ctypes.c_uint8 * PAYLOAD_BYTES)(
                    *(wire + bytes(36)))
                state = (ctypes.c_int32 * 3)(111, -222, 333)
                result = (ctypes.c_int32 * 3)(444, -555, 666)
                self.assertEqual(self.lib.nxrs_c_pipeline_process(
                    payload, supplied_token, state, result), -1)
                self.assertEqual(list(state), [111, -222, 333])
                self.assertEqual(list(result), [444, -555, 666])


if __name__ == '__main__':
    unittest.main()

"""The first-boot tool sends only bounded chunks after matching acknowledgements."""
import io
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'tests/nuttx-std/stream'))
from commission import provision


class Console(io.BytesIO):
    def __init__(self, input):
        super().__init__(input)
        self.output = bytearray()

    def write(self, data):
        self.output.extend(data)
        return len(data)


class CommissionTests(unittest.TestCase):
    def test_exact_acknowledgements_gate_chunks_and_final_completion(self):
        packet = b'NXC1'+bytes(96)
        lines = b'boot\nNXRS_BOOTSTRAP_PROVISION_READY max=269\n'+b''.join(
            f'NXRS_BOOTSTRAP_INPUT_ACK offset={n}\n'.encode() for n in [0, 32, 64, 96, 100])
        console = Console(lines)
        provision(console, packet)
        self.assertEqual(console.output, len(packet).to_bytes(2, 'big')+packet)
        console = Console(lines.replace(b'offset=32', b'offset=31'))
        with self.assertRaises(RuntimeError):
            provision(console, packet)
        self.assertEqual(len(console.output), 2+32)
        console = Console(b'')
        with self.assertRaises(ValueError):
            provision(console, b'wrong')
        self.assertFalse(console.output)

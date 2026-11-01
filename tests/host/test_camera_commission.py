"""The first-boot tool sends only bounded chunks after matching acknowledgements."""
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'tests/nuttx-std/stream'))
from commission import provision
import commission


class Console(io.BytesIO):
    def __init__(self, input):
        super().__init__(input)
        self.output = bytearray()

    def write(self, data):
        self.output.extend(data)
        return len(data)


class CommissionTests(unittest.TestCase):
    def test_serial_is_exclusive_before_open_and_released_after_commissioning(self):
        console = Console(b'')
        def opened():
            self.assertTrue(console.exclusive)
            self.assertFalse(console.dtr or console.rts)
        console.open = opened
        with tempfile.TemporaryDirectory() as directory:
            packet = Path(directory)/'commission.bin'
            packet.write_bytes(b'NXC1'+bytes(96));packet.chmod(0o600)
            with patch.dict(sys.modules, serial=SimpleNamespace(Serial=lambda: console)), \
                 patch.object(sys, 'argv', ['commission', '--port', 'fixture', '--commission', str(packet), '--wifi-file', 'fixture']), \
                 patch.object(commission.RUN, 'read_wifi_file', return_value=(b's', b'password')), \
                 patch.object(commission, 'provision') as provisioned, patch.object(commission, 'Session'):
                commission.main()
                provisioned.assert_called_once_with(console, packet.read_bytes())
        self.assertTrue(console.closed)

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

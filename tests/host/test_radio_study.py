"""Radio study runner contracts, using mocked serial and BLE devices."""
import asyncio
import importlib.util
from pathlib import Path
import socket
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

PATH = Path(__file__).resolve().parents[1] / 'nuttx-std/radio/run.py'
SPEC = importlib.util.spec_from_file_location('radio_study_runner', PATH)
RADIO = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RADIO)


class FakeClient:
    def __init__(self, device, services):
        self.is_connected = False
        self.reads = 0
        self.writes = []
        self.disconnect_reads = None
        self.corrupt_at = None
        self.services = types.SimpleNamespace(
            get_characteristic=lambda value: types.SimpleNamespace(properties={'read', 'write'}))

    async def __aenter__(self):
        self.is_connected = True
        return self

    async def __aexit__(self, *args):
        self.disconnect_reads = self.reads
        self.is_connected = False

    async def write_gatt_char(self, characteristic, value, response):
        self.writes.append(value)

    async def read_gatt_char(self, characteristic, use_cached):
        self.reads += 1
        if self.corrupt_at == self.reads:
            return b'corrupt'
        return self.writes[-1]


def bleak_modules(client):
    class Scanner:
        def __init__(self, detection_callback):
            self.detected = detection_callback

        async def __aenter__(self):
            self.detected(types.SimpleNamespace(address='fixture', name=None),
                          types.SimpleNamespace(service_uuids=[RADIO.SERVICE], local_name=None))
            return self

        async def __aexit__(self, *args):
            pass

    module = types.ModuleType('bleak')
    module.BleakScanner = Scanner
    module.BleakClient = lambda device, services: client
    return {'bleak': module}


class RadioStudyTests(unittest.TestCase):
    def setUp(self):
        # The scan budget is asserted by discovery tests, not spent on a fake radio.
        scan_sleep = patch.object(RADIO.asyncio, 'sleep', AsyncMock())
        scan_sleep.start()
        self.addCleanup(scan_sleep.stop)

    def test_gatt_failure_records_operation_without_backend_details(self):
        for stage, method in (('connect-discover', '__aenter__'),
                              ('write', 'write_gatt_char'),
                              ('read', 'read_gatt_char')):
            with self.subTest(stage=stage):
                client = FakeClient('fixture', [RADIO.SERVICE])
                with patch.dict(sys.modules, bleak_modules(client)), \
                     patch.object(FakeClient, method, AsyncMock(
                         side_effect=RuntimeError('private-backend-details'))), \
                     patch('builtins.print') as output:
                    with self.assertRaises(RuntimeError):
                        asyncio.run(RADIO.gatt())
                output.assert_called_once_with(
                    f'NXRS_HOST_GATT_OPERATION stage={stage} sequence=0', flush=True)

    def test_gatt_cancellation_preserves_the_operation_and_cancellation(self):
        client = FakeClient('fixture', [RADIO.SERVICE])
        with patch.dict(sys.modules, bleak_modules(client)), \
             patch.object(FakeClient, 'read_gatt_char', AsyncMock(
                 side_effect=asyncio.CancelledError())), patch('builtins.print') as output:
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(RADIO.gatt())
        output.assert_called_once_with(
            'NXRS_HOST_GATT_OPERATION stage=read sequence=0', flush=True)

    def test_default_gatt_stays_at_100_write_readback_pairs(self):
        client = FakeClient('fixture', [RADIO.SERVICE])
        with patch.dict(sys.modules, bleak_modules(client)):
            asyncio.run(RADIO.gatt())
        self.assertEqual(len(client.writes), 100)
        self.assertEqual(client.reads, 100)
        self.assertEqual(client.disconnect_reads, 100)

    def test_coexist_reads_final_value_until_control_done_then_disconnects(self):
        client = FakeClient('fixture', [RADIO.SERVICE])
        done_checks = []

        def control_done():
            done_checks.append(True)
            return len(done_checks) == 3

        with patch.dict(sys.modules, bleak_modules(client)):
            asyncio.run(RADIO.gatt(control_done))
        self.assertEqual(len(client.writes), 100)
        self.assertEqual(client.reads, 103)
        self.assertEqual(client.disconnect_reads, 103)

    def test_corrupted_extra_read_fails_before_disconnect_or_success(self):
        client = FakeClient('fixture', [RADIO.SERVICE])
        client.corrupt_at = 101
        with patch.dict(sys.modules, bleak_modules(client)):
            with self.assertRaisesRegex(RuntimeError, 'study readback mismatch'):
                asyncio.run(RADIO.gatt(lambda: False))
        self.assertEqual(client.reads, 101)
        self.assertEqual(client.disconnect_reads, 101)

    def test_failure_and_credential_echo_take_priority_over_done(self):
        for output, message in (
                (b'NXRS_RADIO_FAIL error=Timeout\nNXRS_RADIO_CONTROL_TRAFFIC_DONE', 'device failure'),
                (b'my-secret NXRS_RADIO_CONTROL_TRAFFIC_DONE', 'credential echo')):
            with self.subTest(output=output):
                with self.assertRaisesRegex(RuntimeError, message):
                    RADIO.take_marker(bytearray(output), b'NXRS_RADIO_CONTROL_TRAFFIC_DONE', b'my-secret')

    def test_fixed_studies_follow_the_device_protocol(self):
        class Console:
            def __init__(self, study):
                self.study = study
                self.timeout = None
                self.output = bytearray()
                self.commands = []
                self.closed = False

            def open(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.closed = True

            def write(self, data):
                self.commands.append(data)
                if data == b'\n\n\n':
                    self.output.extend(b'nsh> ')
                elif data.startswith(b'radio_test '):
                    self.output.extend(b'NXRS_WIFI_CREDENTIALS_READY')
                elif data[:1] and data[0] == 3:
                    target = self.commands[-2].split()[1].decode()
                    address, port = target.rsplit(':', 1)
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                        sender.settimeout(1)
                        for sequence in range(RADIO.CONTROL_PACKETS):
                            packet = RADIO.WIFI.expected_packet(sequence)
                            sender.sendto(packet, (address, int(port)))
                            self.assert_echo(sender, packet)
                    self.output.extend(b'NXRS_WIFI_ASSOCIATED_DHCP')
                    if self.study == 'wifi-only':
                        self.output.extend(b'NXRS_RADIO_CONTROL_READY'
                                           b'NXRS_RADIO_CONTROL_TRAFFIC_DONE'
                                           b'NXRS_RADIO_PASSnsh> ')
                    else:
                        self.output.extend(b'NXRS_RADIO_GATT_READY'
                                           b'NXRS_RADIO_CONTROL_TRAFFIC_DONE'
                                           b'NXRS_RADIO_GATT_DONE')
                elif data == b'done\n':
                    self.output.extend(b'NXRS_RADIO_PASSnsh> ')
                elif data == b'echo $?\n':
                    self.output.extend(b'0\nnsh> ')

            def read(self, length):
                chunk = bytes(self.output[:length])
                del self.output[:length]
                return chunk

            @staticmethod
            def assert_echo(sender, expected):
                actual, _ = sender.recvfrom(513)
                if actual != expected:
                    raise AssertionError('mock device received an incorrect echo')

        for study in ('wifi-only', 'coexist'):
            with self.subTest(study=study):
                console = Console(study)
                serial = types.SimpleNamespace(Serial=lambda: console)
                client = FakeClient('fixture', [RADIO.SERVICE])
                modules = {'serial': serial, **bleak_modules(client)}
                with patch.dict(sys.modules, modules), patch.object(
                        RADIO, 'gatt', wraps=RADIO.gatt) as gatt, patch('builtins.print') as output:
                    RADIO.qualify('mock-port', '127.0.0.1', b'lab', b'pw', study=study)
                self.assertTrue(console.closed)
                command = next(item for item in console.commands if item.startswith(b'radio_test '))
                self.assertEqual(command.split()[-1], RADIO.study_transport(study).encode())
                report = ' '.join(str(call.args[0]) for call in output.call_args_list)
                if study == 'wifi-only':
                    gatt.assert_not_called()
                    self.assertNotIn(b'done\n', console.commands)
                    self.assertIn('GATT not started', report)
                    self.assertNotIn('exact GATT', report)
                else:
                    gatt.assert_called_once()
                    self.assertEqual(client.disconnect_reads, 101)
                    self.assertEqual(console.commands.count(b'done\n'), 1)
                    self.assertEqual(console.timeout, 0.1)

    def test_study_command_mapping_and_validation(self):
        self.assertIsNone(RADIO.study_transport(None))
        self.assertEqual(RADIO.study_transport('wifi-only'), 'wifi-only')
        self.assertEqual(RADIO.study_transport('coexist'), 'coexist-control')
        with self.assertRaises(ValueError):
            RADIO.study_transport('unexpected')
        self.assertEqual(RADIO.CONTROL_PACKETS, 1000)


if __name__ == '__main__':
    unittest.main()

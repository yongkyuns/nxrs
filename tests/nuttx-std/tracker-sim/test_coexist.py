import asyncio
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import tomllib
import unittest
from unittest.mock import AsyncMock, call, patch

from bumble.hci import HCI_Reset_Command
from coexist import STACK_TASKS, check_stack_headroom
from dashboard import Guest, GuestFaultError, H4Controller, Qmp, resume_guest, retain_failure


def snapshot(used, tracker_size=1000):
    names = [name for name, count in STACK_TASKS.items() for _ in range(count)]
    rows = [f'{pid} {tracker_size if name == "motion_tracker" else 1000} '
            f'{used if name == "motion_tracker" else 700} {name}'
            for pid, name in enumerate(names, 1)]
    return ('\n'.join(['PID   SIZE   USED   THREAD NAME', *rows]) + '\n').encode()


class StackHeadroomTests(unittest.TestCase):
    def test_entry_reservation_covers_retained_peak_and_native_overhead(self):
        # Run 45913615465 at 66c57797: 46 samples retained this peak in
        # coexist.log. The native task report exposed 520 fewer usable bytes
        # than the requested 14,848 B reservation.
        observed_request, observed_size, observed_used = 14848, 14328, 12352
        with self.assertRaisesRegex(AssertionError, '1976/14328 B free'):
            check_stack_headroom(snapshot(observed_used, observed_size))

        manifest = Path(__file__).resolve().parents[3] / 'app/motion-tracker/Cargo.toml'
        request = tomllib.loads(manifest.read_text())['package']['metadata']['nxrs']['firmware']['stack-size']
        projected_size = request - (observed_request - observed_size)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            check_stack_headroom(snapshot(observed_used, projected_size))
        # Cover the existing 20% target, not only the 15% rejection floor.
        # This is a budget regression; rerunning export and maps on the target
        # must still measure their actual usable capacities and touched peaks.
        self.assertIn('PASS: motion_tracker', output.getvalue())

    def test_headroom_boundaries_use_the_highest_recorded_usage(self):
        for used, status in [(800, 'PASS'), (801, 'WARN'), (850, 'WARN')]:
            with self.subTest(used=used), contextlib.redirect_stdout(io.StringIO()) as output:
                check_stack_headroom(snapshot(used) + snapshot(700))
                self.assertIn(f'{status}: motion_tracker', output.getvalue())

        with self.assertRaisesRegex(AssertionError, 'below the 15% stack headroom floor'):
            check_stack_headroom(snapshot(851) + snapshot(700))

    def test_missing_imu_cannot_pass(self):
        incomplete = snapshot(800).replace(b'5 1000 700 imu-acquire\n', b'')
        with self.assertRaisesRegex(AssertionError, 'never observed all tracker tasks'):
            check_stack_headroom(incomplete)


class StartupDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_fatal_exception_stops_without_waiting_for_eof(self):
        stdout = asyncio.StreamReader()
        log = io.BytesIO()
        guest = Guest(SimpleNamespace(stdout=stdout), log)
        reader = asyncio.create_task(guest.read_console())
        prefix = b'xtensa_debug_handler: Unhandled debug cause '
        stdout.feed_data(b'normal startup\r\n' + prefix + b'0x')
        await asyncio.sleep(0)
        self.assertFalse(reader.done())
        stdout.feed_data(b'8\r\n' + prefix + b'4\r\n')
        with self.assertRaisesRegex(GuestFaultError, 'Unhandled debug cause 0x8$'):
            await asyncio.wait_for(reader, 1)
        self.assertEqual(log.getvalue(), b'normal startup\r\n' + prefix + b'0x8\r\n' + prefix + b'4\r\n')
        self.assertIn(prefix + b'0x8\n', guest.buffer)

    async def test_normal_console_and_expected_guard_diagnostics_continue(self):
        stdout = asyncio.StreamReader()
        guest = Guest(SimpleNamespace(stdout=stdout), io.BytesIO())
        stdout.feed_data(
            b'nsh> echo stack-guard-control\r\n'
            b'dump_assert_info: Assertion failed stack margin overwritten: task: tracker_sim_init\r\n'
            b'example: xtensa_debug_handler: Unhandled debug cause 0x8\r\n'
            b'{"v":1,"sample":42}\r\n')
        stdout.feed_eof()
        await guest.read_console()
        self.assertEqual(guest.samples, [{'v': 1, 'sample': 42}])

    async def test_split_failure_line_preserves_stage_and_hci_progress(self):
        stdout = asyncio.StreamReader()
        guest = Guest(SimpleNamespace(stdout=stdout), io.BytesIO())
        guest.controller = H4Controller('test')
        guest.controller.on_packet(bytes(HCI_Reset_Command()))
        reader = asyncio.create_task(guest.read_console())
        stdout.feed_data(b'tracker simulator initialization failed: -5')
        await asyncio.sleep(0)
        self.assertFalse(reader.done())
        stdout.feed_data(b' (register H4 controller)\r\n')
        stdout.feed_eof()
        with self.assertRaisesRegex(
            RuntimeError, r'-5 \(register H4 controller\)\nH4 startup: command 0x0c03, complete 0x0c03'
        ):
            await reader


class QmpWriter:
    def __init__(self):
        self.requests = []
        self.closed = False

    def write(self, value):
        self.requests.append(json.loads(value))

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


class QmpDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_retains_the_session_and_matches_response_ids(self):
        reader, writer = asyncio.StreamReader(), QmpWriter()
        for reply in [
            {'QMP': {'version': {'qemu': {'major': 9, 'minor': 0, 'micro': 0}}}},
            {'return': {}, 'id': 1},
            {'event': 'RESUME'},
            {'return': 'stale reply', 'id': 1},
            {'return': {}, 'id': 2},
        ]:
            reader.feed_data(json.dumps(reply).encode() + b'\n')
        with patch('dashboard.asyncio.open_unix_connection', AsyncMock(return_value=(reader, writer))):
            monitor = await resume_guest(Path('unused'))
        self.assertFalse(writer.closed)
        self.assertEqual(writer.requests, [
            {'execute': 'qmp_capabilities', 'id': 1},
            {'execute': 'cont', 'id': 2},
        ])
        await monitor.close()
        self.assertTrue(writer.closed)

    async def test_command_rejects_errors_eof_and_an_unresponsive_monitor(self):
        reader = asyncio.StreamReader()
        monitor = Qmp(reader, QmpWriter(), {})
        reader.feed_data(b'{"error":{"desc":"unsupported"},"id":1}\n')
        with self.assertRaisesRegex(RuntimeError, 'unsupported'):
            await monitor.command('stop')
        reader.feed_eof()
        with self.assertRaisesRegex(EOFError, 'QMP closed'):
            await monitor.command('stop')
        monitor = Qmp(asyncio.StreamReader(), QmpWriter(), {})
        with self.assertRaises(TimeoutError):
            await monitor.command('stop', timeout=0.01)

    async def test_failure_stops_guest_and_retains_both_cores(self):
        monitor = SimpleNamespace(
            greeting={'version': {'qemu': {'major': 9}}},
            command=AsyncMock(side_effect=[{}, 'EPC6=42001234 DEBUGCAUSE=00000008', 'PC=40000400']),
        )
        error = TimeoutError('complete panel frame')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'tiles.log'
            await retain_failure(monitor, error, path, Path('firmware/nuttx.merged.bin'))
            report = json.loads(path.with_suffix('.failure.json').read_text())
        self.assertEqual(report['failure'], {'type': 'TimeoutError', 'message': 'complete panel frame'})
        self.assertTrue(report['stopped'])
        self.assertEqual(report['elf'], 'firmware/nuttx')
        self.assertEqual(report['registers'], {'0': 'EPC6=42001234 DEBUGCAUSE=00000008', '1': 'PC=40000400'})
        self.assertEqual(report['diagnostic_errors'], [])
        self.assertEqual(monitor.command.call_args_list, [
            call('stop'),
            call('human-monitor-command', {'command-line': 'info registers', 'cpu-index': 0}),
            call('human-monitor-command', {'command-line': 'info registers', 'cpu-index': 1}),
        ])
        self.assertEqual(error.args, ('complete panel frame',))

    async def test_partial_diagnostics_do_not_hide_the_original_failure(self):
        monitor = SimpleNamespace(greeting={}, command=AsyncMock(side_effect=[
            TimeoutError('stop response'), RuntimeError('core unavailable'), 'EPC6=42005678',
        ]))
        error = AssertionError('map sample missing')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'tiles.log'
            await retain_failure(monitor, error, path, Path('firmware/nuttx.merged.bin'))
            report = json.loads(path.with_suffix('.failure.json').read_text())
        self.assertFalse(report['stopped'])
        self.assertEqual(report['registers'], {'1': 'EPC6=42005678'})
        self.assertEqual(len(report['diagnostic_errors']), 2)
        self.assertEqual(error.args, ('map sample missing',))

    async def test_fatal_console_error_survives_failed_snapshot(self):
        monitor = SimpleNamespace(greeting={}, command=AsyncMock(side_effect=TimeoutError('monitor')))
        error = GuestFaultError('xtensa_debug_handler: Unhandled debug cause 0x8')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'tiles.log'
            await retain_failure(monitor, error, path, Path('firmware/nuttx.merged.bin'))
            report = json.loads(path.with_suffix('.failure.json').read_text())
        self.assertEqual(report['failure'], {'type': 'GuestFaultError', 'message': str(error)})
        self.assertFalse(report['stopped'])
        self.assertEqual(len(report['diagnostic_errors']), 3)

    async def test_unavailable_session_and_artifact_write_keep_original_error(self):
        error = TimeoutError('original startup failure')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'tiles.log'
            await retain_failure(None, error, path, Path('firmware/nuttx.merged.bin'))
            report = json.loads(path.with_suffix('.failure.json').read_text())
        self.assertIsNone(report['qmp'])
        self.assertEqual(report['registers'], {})
        self.assertTrue(report['diagnostic_errors'])
        with patch.object(Path, 'write_text', side_effect=OSError('storage unavailable')):
            await retain_failure(None, error, Path('tiles.log'), Path('firmware/nuttx.merged.bin'))
        self.assertIn('storage unavailable', error.__notes__[-1])
        self.assertEqual(error.args, ('original startup failure',))

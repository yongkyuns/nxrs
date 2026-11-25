import asyncio
import contextlib
import io
from types import SimpleNamespace
import unittest

from bumble.hci import HCI_Reset_Command
from coexist import STACK_TASKS, check_stack_headroom
from dashboard import Guest, H4Controller


def snapshot(used):
    names = [name for name, count in STACK_TASKS.items() for _ in range(count)]
    rows = [f'{pid} 1000 {used if name == "motion_tracker" else 700} {name}'
            for pid, name in enumerate(names, 1)]
    return ('\n'.join(['PID   SIZE   USED   THREAD NAME', *rows]) + '\n').encode()


class StackHeadroomTests(unittest.TestCase):
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

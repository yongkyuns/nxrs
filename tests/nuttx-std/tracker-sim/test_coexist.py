import asyncio
import contextlib
import io
from pathlib import Path
from types import SimpleNamespace
import tomllib
import unittest

from bumble.hci import HCI_Reset_Command
from coexist import STACK_TASKS, check_stack_headroom
from dashboard import Guest, H4Controller


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

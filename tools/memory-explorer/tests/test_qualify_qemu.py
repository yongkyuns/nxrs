"""Negative witnesses for the QEMU qualification report's lifecycle checks.

These host tests are not emulator evidence; qualify_qemu.py performs that run.
"""

from copy import deepcopy
from io import BytesIO
import unittest
from unittest.mock import patch

from qualify_qemu import drain_console, validate_lifecycle


def observations():
    # Literal small witnesses, independent of both exporter and debugger code.
    points = []
    for tasks, size in [([(0x100, 0)], 0x40),
                        ([(0x100, 0), (0x180, 4)], 0x60),
                        ([(0x100, 0)], 0x40)]:
        points.append({"tasks": [{"tcb": tcb, "pid": pid} for tcb, pid in tasks],
                       "heaps": [{"address": 0x800, "regions": [
                           {"start": 0x1000, "size": 0x100, "chunks": [
                               {"start": 0x1000, "size": size, "kind": "allocation"},
                               {"start": 0x1000 + size, "size": 0x100 - size, "kind": "free"}]}]}]})
    return points


class LifecycleChecks(unittest.TestCase):
    def test_accepts_observed_task_creation_and_retirement(self):
        result = validate_lifecycle(observations())
        self.assertEqual(result["new_entry_tasks"], [{"tcb": 0x180, "pid": 4}])
        self.assertTrue(result["entry_tasks_retired"])

    def test_rejects_missing_observation(self):
        with self.assertRaisesRegex(RuntimeError, "requires ready"):
            validate_lifecycle(observations()[:2])

    def test_rejects_repeated_ready_snapshot(self):
        point = observations()[0]
        with self.assertRaisesRegex(RuntimeError, "No new task"):
            validate_lifecycle([point, point, point])

    def test_rejects_command_that_did_not_retire(self):
        points = observations()
        points[2]["tasks"] = deepcopy(points[1]["tasks"])
        with self.assertRaisesRegex(RuntimeError, "remains registered"):
            validate_lifecycle(points)

    def test_rejects_unchanged_heap_at_entry(self):
        points = observations()
        points[1]["heaps"] = deepcopy(points[0]["heaps"])
        with self.assertRaisesRegex(RuntimeError, "Heap topology"):
            validate_lifecycle(points)

    def test_rejects_unchanged_heap_after_command(self):
        points = observations()
        points[2]["heaps"] = deepcopy(points[1]["heaps"])
        with self.assertRaisesRegex(RuntimeError, "Heap topology"):
            validate_lifecycle(points)


class ConsoleDrainChecks(unittest.TestCase):
    def test_continuously_readable_uart_stops_at_output_cap(self):
        with patch("qualify_qemu.select.select", return_value=([17], [], [])), \
                patch("qualify_qemu.os.read", return_value=b"x" * 65536) as read, \
                patch("qualify_qemu.time.monotonic", return_value=0):
            with self.assertRaisesRegex(RuntimeError, "transcript exceeded"):
                drain_console(17, bytearray(), BytesIO(), deadline=1)
            self.assertLessEqual(read.call_count, 65)

    def test_continuously_readable_uart_keeps_original_deadline(self):
        with patch("qualify_qemu.select.select", return_value=([17], [], [])), \
                patch("qualify_qemu.os.read", return_value=b"x") as read, \
                patch("qualify_qemu.time.monotonic", side_effect=[0, 1]):
            with self.assertRaisesRegex(TimeoutError, "deadline exceeded"):
                drain_console(17, bytearray(), BytesIO(), deadline=1)
            self.assertEqual(read.call_count, 1)


if __name__ == "__main__":
    unittest.main()

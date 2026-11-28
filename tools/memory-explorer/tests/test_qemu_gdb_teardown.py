"""Host controls for voluntary teardown after native observations complete."""

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock


class RemoteError(Exception):
    pass


class QemuTeardownTests(unittest.TestCase):
    def collector(self, response=None):
        remote = SimpleNamespace(error=RemoteError, execute=mock.Mock(side_effect=response))
        spec = importlib.util.spec_from_file_location(
            "qemu_capture_gdb", Path(__file__).with_name("qemu_capture_gdb.py"))
        collector = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {"gdb": remote}):
            spec.loader.exec_module(collector)
        return collector, remote

    def test_acknowledged_kill_is_reported_without_claiming_process_exit(self):
        collector, remote = self.collector()
        self.assertEqual(collector.terminate_remote(), {"kill_packet": "acknowledged"})
        remote.execute.assert_called_once_with("kill")

    def test_gdb_shutdown_response_is_retained_verbatim(self):
        response = "Remote communication error. Target disconnected: error while writing: Broken pipe."
        collector, remote = self.collector(RemoteError(response))
        self.assertEqual(collector.terminate_remote(),
                         {"kill_packet": "unacknowledged", "kill_response": response})
        remote.execute.assert_called_once_with("kill")

    def test_unexpected_exception_is_not_a_remote_cleanup_response(self):
        collector, remote = self.collector(RuntimeError("unexpected harness defect"))
        with self.assertRaisesRegex(RuntimeError, "unexpected harness defect"):
            collector.terminate_remote()
        remote.execute.assert_called_once_with("kill")


if __name__ == "__main__":
    unittest.main()

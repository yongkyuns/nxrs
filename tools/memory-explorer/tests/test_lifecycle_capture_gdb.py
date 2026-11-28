"""Host regressions for stopped checkpoint progress, without GDB or QEMU."""

from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ENTRY_PC = 0x1003326A


class RemoteError(Exception):
    pass


class Breakpoint:
    def __init__(self, spec, type, temporary=False):
        self.spec, self.type, self.temporary = spec, type, temporary
        self.enabled, self.valid, self.hit_count = True, True, 0

    def is_valid(self):
        return self.valid

    def delete(self):
        self.valid = False


class StoppedTarget:
    """An entry stop executes no instruction until a disabled-breakpoint step.

    Rearming a breakpoint at the same PC reproduces the duplicate entry stop.
    This is a scripted remote-stop witness, not an emulated NuttX target.
    """

    BP_HARDWARE_BREAKPOINT = 1
    error = RemoteError

    def __init__(self, stalled_step=False, step_error=False, bad_stop=None, kill_error=None):
        self.stalled_step, self.step_error, self.bad_stop = stalled_step, step_error, bad_stop
        self.kill_error = kill_error
        self.pc, self.native_id, self.continues = 0, 0, 0
        self.breakpoints, self.steps, self.commands = [], [], []
        self.locking = None

    def Breakpoint(self, *args, **kwargs):
        breakpoint = Breakpoint(*args, **kwargs)
        self.breakpoints.append(breakpoint)
        return breakpoint

    def newest_frame(self):
        return SimpleNamespace(read_register=lambda name: self.pc if name == "pc" else None)

    def inferiors(self):
        return [SimpleNamespace(threads=lambda: [SimpleNamespace(is_stopped=lambda: True)])]

    def parse_and_eval(self, expression):
        if expression == "g_memory_checkpoint_id":
            return self.native_id
        raise AssertionError(f"Unexpected target read: {expression}")

    def execute(self, command):
        self.commands.append(command)
        if command == "set scheduler-locking step":
            self.locking = "step"
        elif command == "continue":
            self.continues += 1
            breakpoint = self.breakpoints[-1]
            if not breakpoint.is_valid() or not breakpoint.enabled:
                self.pc = 0x10001000
                return
            if self.bad_stop == "other" and self.continues == 2:
                self.pc = 0x10001000
                return
            if self.pc != ENTRY_PC:
                self.native_id += 1
            self.pc = ENTRY_PC
            breakpoint.hit_count += 2 if self.bad_stop == "extra-hit" else 1
            if breakpoint.temporary or self.bad_stop == "invalid":
                breakpoint.delete()
        elif command == "stepi":
            breakpoint = self.breakpoints[-1]
            self.steps.append((self.native_id, breakpoint.enabled, self.locking))
            if self.step_error:
                if isinstance(self.step_error, Exception):
                    raise self.step_error
                raise RuntimeError("Remote step failed")
            if not breakpoint.enabled and not self.stalled_step:
                self.pc += 2
        elif command == "kill" and self.kill_error is not None:
            raise self.kill_error
        elif not command.startswith(("set ", "source ", "target remote ", "quit ")) and command != "kill":
            raise AssertionError(f"Unexpected GDB command: {command}")


def load_collector(target, name="lifecycle_capture_gdb"):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).with_name(name + ".py"))
    collector = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {"gdb": target}):
        spec.loader.exec_module(collector)
    return collector


class CheckpointProgressTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name)
        self.image = self.output / "fixture.elf"
        self.image.write_bytes(b"Host fixture identity; not a native ELF")
        self.settings = {"output": str(self.output), "image": str(self.image),
                         "test_tools": str(self.output / "tools"),
                         "pynuttx": str(self.output / "pynuttx"),
                         "exporter": str(self.output / "export.py"),
                         "qemu_command": ["unused-host-fixture"], "recording_id": "1" * 32}
        self.settings_path = self.output / "run.json"
        self.settings_path.write_text(json.dumps(self.settings))
        self.captured, self.prompts = [], []

    def collect(self, target):
        collector = load_collector(target)

        def capture(settings, checkpoint_id, raw_snapshot, compare_capture):
            # A capture must still be at the entry, before its first instruction,
            # and must describe the next native event, not another stop at it.
            self.assertEqual(target.pc, ENTRY_PC)
            self.assertEqual(target.native_id, checkpoint_id)
            self.assertEqual(len(target.steps), checkpoint_id - 1)
            self.captured.append(checkpoint_id)
            return {"checkpoint_id": checkpoint_id, "recording_id": settings["recording_id"]}

        def stop_at(symbol):
            self.prompts.append(symbol)
            if len(self.prompts) == 2:
                self.assertFalse(any(bp.is_valid() for bp in target.breakpoints))

        def terminate_remote():
            self.assertFalse(any(bp.is_valid() for bp in target.breakpoints))
            self.assertEqual(self.captured, [1, 2, 3, 4])
            self.assertTrue((self.output / "gdb-report.json").is_file())
            return load_collector(target, "qemu_capture_gdb").terminate_remote()

        self.terminate_remote = mock.Mock(side_effect=terminate_remote)
        modules = {
            "qemu_capture_gdb": SimpleNamespace(raw_snapshot=mock.Mock(), compare_capture=mock.Mock(),
                                                stop_at=stop_at, terminate_remote=self.terminate_remote),
            "nxgdb": SimpleNamespace(
                mm=SimpleNamespace(__file__=str(self.output / "pynuttx" / "nxgdb" / "mm.py")),
                utils=SimpleNamespace(get_tcbs=lambda: [])),
        }
        with mock.patch.dict(sys.modules, modules), \
                mock.patch.dict(os.environ, {"NXRS_MEMORY_LIFECYCLE_RUN": str(self.settings_path)}), \
                mock.patch.object(sys, "path", list(sys.path)), \
                mock.patch.object(collector, "checkpoint", side_effect=capture), \
                redirect_stdout(io.StringIO()):
            collector.run()

    def assert_failure(self, target, message, captured):
        self.collect(target)
        self.assertEqual(self.captured, captured)
        self.assertIn(message, json.loads((self.output / "failure.json").read_text())["error"])
        self.assertFalse((self.output / "gdb-report.json").exists())
        self.assertEqual(target.commands[-1], "quit 1")
        self.terminate_remote.assert_not_called()

    def test_capture_precedes_instruction_progress_and_keeps_source_identity(self):
        target = StoppedTarget()
        self.collect(target)
        self.assertFalse((self.output / "failure.json").exists())
        self.assertEqual(self.captured, [1, 2, 3, 4])
        self.assertEqual(target.steps, [(1, False, "step"), (2, False, "step"), (3, False, "step")])
        self.assertEqual(len(target.breakpoints), 1)
        breakpoint = target.breakpoints[0]
        self.assertEqual(breakpoint.spec, "*timing_checkpoint_stop")
        self.assertEqual(breakpoint.type, target.BP_HARDWARE_BREAKPOINT)
        self.assertFalse(breakpoint.temporary)
        self.assertFalse(breakpoint.is_valid())
        self.assertEqual(self.prompts, ["nsh_prompt", "nsh_prompt"])
        report = json.loads((self.output / "gdb-report.json").read_text())
        self.assertEqual(report["elf_sha256"], hashlib.sha256(self.image.read_bytes()).hexdigest())
        self.assertEqual(report["recording_id"], self.settings["recording_id"])
        self.assertEqual([point["checkpoint_id"] for point in report["snapshots"]], [1, 2, 3, 4])
        self.terminate_remote.assert_called_once_with()
        self.assertEqual(json.loads((self.output / "teardown.json").read_text()),
                         {"kill_packet": "acknowledged"})
        self.assertEqual(target.commands[-1], "quit 0")

    def test_completed_observations_survive_the_recorded_kill_pipe_race(self):
        response = "Remote communication error. Target disconnected: error while writing: Broken pipe."
        target = StoppedTarget(kill_error=RemoteError(response))
        self.collect(target)
        self.assertEqual(self.captured, [1, 2, 3, 4])
        self.assertFalse((self.output / "failure.json").exists())
        self.assertEqual(json.loads((self.output / "teardown.json").read_text()),
                         {"kill_packet": "unacknowledged", "kill_response": response})
        self.terminate_remote.assert_called_once_with()
        self.assertEqual(target.commands[-2:], ["kill", "quit 0"])

    def test_remote_error_during_observation_still_fails_before_teardown(self):
        target = StoppedTarget(step_error=RemoteError("Remote checkpoint read failed"))
        self.assert_failure(target, "Remote checkpoint read failed", [1])
        self.assertFalse((self.output / "teardown.json").exists())

    def test_stalled_step_does_not_rearm_or_claim_a_second_capture(self):
        target = StoppedTarget(stalled_step=True)
        self.assert_failure(target, "Single-step did not advance", [1])
        self.assertEqual(target.continues, 1)
        self.assertFalse(target.breakpoints[0].enabled)

    def test_failed_step_does_not_rearm_or_report_success(self):
        target = StoppedTarget(step_error=True)
        self.assert_failure(target, "Remote step failed", [1])
        self.assertEqual(target.continues, 1)
        self.assertFalse(target.breakpoints[0].enabled)

    def test_unrelated_stop_cannot_be_counted_as_the_next_checkpoint(self):
        self.assert_failure(StoppedTarget(bad_stop="other"), "next checkpoint safe point", [1])

    def test_invalid_or_extra_breakpoint_hits_cannot_be_captured(self):
        for bad_stop in ("invalid", "extra-hit"):
            with self.subTest(bad_stop=bad_stop):
                self.assert_failure(StoppedTarget(bad_stop=bad_stop), "next checkpoint safe point", [])

    def test_duplicate_native_checkpoint_reports_expected_and_observed_before_export(self):
        target = StoppedTarget()
        target.native_id = 1
        collector = load_collector(target)
        raw_snapshot, compare_capture = mock.Mock(), mock.Mock()
        with self.assertRaisesRegex(RuntimeError, "expected 2, observed 1"):
            collector.checkpoint(self.settings, 2, raw_snapshot, compare_capture)
        raw_snapshot.assert_not_called()
        compare_capture.assert_not_called()
        self.assertEqual(target.commands, [])


if __name__ == "__main__":
    unittest.main()

"""Fault and framing controls; all generated records/processes are synthetic."""
import errno
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch


HERE = Path(__file__).resolve().parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fixtures = load("event_path_console_capture_fixtures", "test_capture.py")
capture = fixtures.capture
analysis = capture.analysis
replay = load("event_path_console_replay", "replay.py")
COMMAND = "event_path_perf ready-std-try 2 1 bytes16"
FAULT_LINES = (
    "EVENT_PATH_PERF FAIL synthetic failure",
    "PANIC at task startup", "ASSERTION failed", "Assertion failed panic",
    "arm_hardfault.c:123", "UsageFault at pc", "BusFault at pc",
    "qemu: fatal: guest failure", "Lockup: task stopped", "panicked at fixture.rs:12",
    'EVENT_PATH_PER {"record":"fatal","reason":"partial record prefix"}',
    '{"record": "failure", "reason": "lost record prefix"}',
)


def frame(erase=True):
    rows = fixtures.transcript(fixtures.fixture(native=True)).replace("\n", "\r\r\n").encode()
    return (b"qemu-system-arm: warning: nic lan9118.0 has no peer\r\n"
            b"NuttShell (NSH) SYNTHETIC TEST ONLY\r\r\nnsh> "
            + (b"\x1b[K" if erase else b"") + COMMAND.encode() + b"\r\r\n" + rows + b"nsh> ")


class ConsoleContractControls(unittest.TestCase):
    def test_known_nsh_prompt_decorations_are_preserved_at_exact_raw_eof(self):
        for erase in (False, True):
            for decoration in (b"", b"\x1b[K", b"\x1b[", b"\x1b"):
                raw = frame(erase) + decoration
                with self.subTest(erase=erase, decoration=decoration):
                    self.assertEqual(analysis.firmware_frame(raw, COMMAND), {
                        "fresh_boot_observed": True, "boot_rom_digest_warning_observed": False,
                    })
                    self.assertTrue(raw.endswith(b"nsh> " + decoration))
        self.assertTrue(analysis.firmware_frame(
            b"*** Booting NuttX ***\r\n" + frame(), COMMAND)["fresh_boot_observed"])
        self.assertTrue(analysis.firmware_frame(
            b"SHA-256 comparison failed: synthetic warning\n" + frame(),
            COMMAND)["boot_rom_digest_warning_observed"])

    def test_boot_and_exact_sole_echo_must_precede_records(self):
        raw = frame()
        variants = (
            raw.replace(COMMAND.encode(), b"event_path_perf ready-messaging-try 2 1 bytes16", 1),
            raw.replace(COMMAND.encode(), b"", 1),
            raw.replace(b"nsh> ", b"nsh> extra_command\r\nnsh> ", 1),
            raw.replace(analysis.PREFIX.encode(), b"nsh> " + COMMAND.encode() + b"\r\n"
                        + analysis.PREFIX.encode(), 1),
            raw.replace(b"NuttShell", b"NuttShell\r\nNuttShell", 1),
            raw.replace(b"NuttShell", b"old unrelated output", 1),
            raw + b"\nNuttShell\nnsh> ",
        )
        for broken in variants:
            with self.subTest(raw=broken[:160]), self.assertRaises(ValueError):
                analysis.firmware_frame(broken, COMMAND)

    def test_partial_records_controls_and_extra_terminal_content_are_rejected(self):
        raw = frame()
        variants = [raw + suffix for suffix in (
            b"EVENT_PATH_PER", b"unexpected text", b"\x1b[0", b"\x1b]", b"\x1b[\n",
            b"\x1b\n", b"\x1b[\r", b"\x1b\r[", b"\x1b\r", b"\x1b\x1b[",
            b"another_command\r\nnsh> \x1b[", b"\r\n", b" ",
        )]
        variants += [
            raw[:-len(b"nsh> ")], raw[:-1], raw[:-1] + b"\x1b[", raw[:-1] + b"\r \x1b[",
            b"\x1b[\n" + raw,
            raw.replace(COMMAND.encode(), b"\x1b[\n" + COMMAND.encode(), 1),
            raw.replace(b'"record": "sample"', b'"record": \x1b[\n"sample"', 1),
            raw.replace(b"\r\r\nEVENT_PATH_PERF", b"\r\r\npartial record\nEVENT_PATH_PERF", 2),
        ]
        for broken in variants:
            with self.subTest(suffix=broken[-100:]), self.assertRaises(ValueError):
                analysis.firmware_frame(broken, COMMAND)

    def test_faults_are_rejected_by_analysis_and_framing_in_every_capitalization(self):
        for fault in FAULT_LINES:
            for variant in (fault, fault.lower(), fault.upper()):
                raw = frame() + variant.encode() + b"\n"
                with self.subTest(fault=variant):
                    with self.assertRaisesRegex(ValueError, "kernel fault"):
                        analysis.analyze(raw.decode())
                    with self.assertRaisesRegex(ValueError, "kernel fault"):
                        analysis.firmware_frame(raw, COMMAND)

    def test_live_fault_refuses_boot_prompt_while_only_diagnostics_continue(self):
        for fault in FAULT_LINES:
            for variant in (fault, fault.lower(), fault.upper()):
                for suffix in (b"", b"\nNuttShell\r\nnsh> "):
                    payload = variant.encode() + suffix
                    transcript = bytearray()
                    with self.subTest(fault=variant, prompt=bool(suffix)), \
                            patch.object(capture.console.select, "select", return_value=([7], [], [])) as ready, \
                            patch.object(capture.console.os, "read", side_effect=[payload, b""]) as read, \
                            patch.object(capture.console.time, "monotonic", return_value=0):
                        with self.assertRaisesRegex(RuntimeError, "kernel fault"):
                            capture.read_until(7, transcript, capture.console.fresh_boot, 120)
                        self.assertEqual(ready.call_count, 2)
                        self.assertEqual(read.call_count, 2)
                        self.assertEqual(read.call_args_list[0].args, (7, 65536))
                        self.assertEqual(transcript, payload)

    def test_split_fault_signature_and_fault_after_result_never_satisfy_the_prompt(self):
        for chunks in ([b"ASSER", b"TION failed\nNuttShell\nnsh> "],
                       [frame() + b"PANIC after result\nnsh> "]):
            transcript = bytearray()
            with self.subTest(chunks=len(chunks)), \
                    patch.object(capture.console.select, "select", return_value=([7], [], [])), \
                    patch.object(capture.console.os, "read", side_effect=[*chunks, b""]) as read, \
                    patch.object(capture.console.time, "monotonic", return_value=0):
                with self.assertRaisesRegex(RuntimeError, "kernel fault"):
                    capture.read_until(7, transcript, lambda data: capture.console.PROMPT in data, 120)
                self.assertEqual(read.call_count, len(chunks) + 1)
                self.assertEqual(transcript, b"".join(chunks))


class FaultDiagnosticControls(unittest.TestCase):
    def test_diagnostic_bytes_are_bounded_by_tail_and_total_console_limits(self):
        for spare in (capture.FAULT_TAIL_BYTES + 1, 17, 0):
            transcript = bytearray(b"x" * (capture.console.MAX_LOG_BYTES - spare))
            initial = len(transcript)
            with self.subTest(spare=spare), \
                    patch.object(capture.select, "select", return_value=([7], [], [])) as ready, \
                    patch.object(capture.os, "read", side_effect=lambda _fd, count: b"z" * count) as read, \
                    patch.object(capture.time, "monotonic", return_value=10):
                capture.drain_fault_diagnostics(7, transcript, 30)
                self.assertEqual(len(transcript) - initial, min(capture.FAULT_TAIL_BYTES, spare))
                self.assertLessEqual(len(transcript), capture.console.MAX_LOG_BYTES)
                if spare:
                    read.assert_called_once_with(7, min(capture.FAULT_TAIL_BYTES, spare))
                else:
                    ready.assert_not_called()
                    read.assert_not_called()

    def test_diagnostic_wait_never_exceeds_one_second_or_the_existing_deadline(self):
        for deadline, allowance in ((30, 1.0), (10.25, 0.25), (10, 0.0), (9, 0.0)):
            for readable_at_deadline in (False, True):
                clock = [10.0]
                waits = []

                def wait(fds, _write, _error, timeout):
                    waits.append(timeout)
                    clock[0] += timeout
                    return (fds if readable_at_deadline else [], [], [])

                transcript = bytearray(b"PANIC")
                with self.subTest(deadline=deadline, readable=readable_at_deadline), \
                        patch.object(capture.select, "select", side_effect=wait), \
                        patch.object(capture.os, "read") as read, \
                        patch.object(capture.time, "monotonic", side_effect=lambda: clock[0]):
                    capture.drain_fault_diagnostics(7, transcript, deadline)
                    self.assertEqual(sum(waits), allowance)
                    self.assertEqual(clock[0], 10 + allowance)
                    read.assert_not_called()
                    self.assertEqual(transcript, b"PANIC")

    def test_tail_errors_never_replace_the_original_fault_and_nonfault_errors_do_not_drain(self):
        for error in (OSError(errno.EIO, "PTY closed"), OSError(errno.EBADF, "closed descriptor"),
                      ValueError("invalid descriptor"), RuntimeError("secondary diagnostic failure")):
            original = RuntimeError("guest failure or kernel fault")
            transcript = bytearray(b"Assertion failed")
            with self.subTest(error=type(error).__name__), \
                    patch.object(capture.console, "read_until", side_effect=original), \
                    patch.object(capture.select, "select", return_value=([7], [], [])), \
                    patch.object(capture.os, "read", side_effect=error), \
                    patch.object(capture.time, "monotonic", return_value=10):
                with self.assertRaises(RuntimeError) as caught:
                    capture.read_until(7, transcript, lambda _data: True, 30)
                self.assertIs(caught.exception, original)
                self.assertEqual(transcript, b"Assertion failed")
        original = RuntimeError("console EOF before complete result and prompt")
        with patch.object(capture.console, "read_until", side_effect=original), \
                patch.object(capture, "drain_fault_diagnostics") as drained:
            with self.assertRaises(RuntimeError) as caught:
                capture.read_until(7, bytearray(), lambda _data: False, 30)
            self.assertIs(caught.exception, original)
            drained.assert_not_called()


class CaptureAndReplayControls(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CaptureControls(
            "test_successful_host_capture_is_functional_only_and_output_is_never_reused")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_split_delayed_fault_diagnostics_never_send_command_and_preserve_failed_replay(self):
        executable, _ = self.fixture.executable("faulted-qemu", runtime="qemu")
        for index, (first, second, error) in enumerate((
            (b"ASSER", b"TION", "guest fixture failure or kernel fault"),
            (b"Assert", b"ion failed", "guest failure or kernel fault"),
        )):
            args = self.fixture.arguments(f"faulted-boot-{index}", runtime="qemu", qemu=str(executable))
            suffix = b" panic: at file: synthetic/arm_hardfault.c:7 task: SyntheticIdle pc: 0x1234\r\n"
            chunks = [b"dump_assert_info: " + first, second, suffix, b"NuttShell\nnsh> ", b""]
            clock = [10.0]
            process = Mock()
            process.poll.return_value = 0

            def delayed(fds, _write, _error, _timeout):
                clock[0] += 0.02  # Model delayed diagnostic chunks without a real sleep.
                return fds, [], []

            with self.subTest(detector=index), \
                    patch.object(capture.subprocess, "Popen", return_value=process), \
                    patch.object(capture.select, "select", side_effect=delayed), \
                    patch.object(capture.os, "read", side_effect=chunks), \
                    patch.object(capture.time, "monotonic", side_effect=lambda: clock[0]), \
                    patch.object(capture.console, "fresh_boot", wraps=capture.console.fresh_boot) as boot, \
                    patch.object(capture.console, "write_command") as written:
                report = capture.capture(args)
            written.assert_not_called()
            self.assertFalse(any(b"NuttShell" in call.args[0] for call in boot.call_args_list))
            self.assertFalse(report["capture_complete"])
            self.assertEqual(report["failure_stage"], "fresh-boot")
            self.assertEqual(report["error"], "RuntimeError: " + error)
            self.assertEqual((args.output / "console.log").read_bytes(), b"".join(chunks))
            self.assertFalse((args.output / "analysis.json").exists())
            checked = replay.replay(args.output)
            self.assertTrue(checked["evidence_valid"])
            self.assertFalse(checked["capture_complete"])

    def test_zero_host_exit_with_fault_keeps_both_streams_and_failed_status(self):
        executable, _ = self.fixture.executable("faulted-host", stderr="PANIC after valid records", exit_code=0)
        args = self.fixture.arguments("faulted-host-capture", binary=executable)
        report = capture.capture(args)
        self.assertEqual(report["returncode"], 0)
        self.assertFalse(report["capture_complete"])
        self.assertIn("kernel fault", report["error"])
        raw = (args.output / "console.log").read_bytes()
        self.assertIn(analysis.PREFIX.encode(), raw)
        self.assertTrue(raw.endswith(b"PANIC after valid records\n"))
        checked = replay.replay(args.output)
        self.assertTrue(checked["evidence_valid"])
        self.assertFalse(checked["capture_complete"])

    def test_replay_refuses_framing_mutations_even_after_rehashing_capture_files(self):
        executable, _ = self.fixture.executable("framing-qemu", runtime="qemu")
        args = self.fixture.arguments("framing-capture", runtime="qemu", qemu=str(executable))
        self.assertTrue(capture.capture(args)["capture_complete"])
        report_path, manifest_path = args.output / "capture.json", args.output / "evidence.json"
        raw = (args.output / "console.log").read_bytes()
        report_bytes, manifest_bytes = report_path.read_bytes(), manifest_path.read_bytes()
        variants = (
            raw[:raw.rfind(b"nsh> ")],
            raw.replace(COMMAND.encode(), b"event_path_perf ready-crossbeam-send 2 1 bytes16", 1),
            raw + b"EVENT_PATH_PER", raw + b"\x1b[0", raw + b"\nNuttShell\nnsh> ",
            raw + b"UsageFault at pc\n",
        )
        for broken in variants:
            with self.subTest(suffix=broken[-100:]):
                (args.output / "console.log").write_bytes(broken)
                report = json.loads(report_bytes)
                report["console_sha256"] = capture.digest(args.output / "console.log")
                report_path.write_text(json.dumps(report))
                manifest = json.loads(manifest_bytes)
                manifest["files_sha256"]["console.log"] = report["console_sha256"]
                manifest["files_sha256"]["capture.json"] = capture.digest(report_path)
                manifest_path.write_text(json.dumps(manifest))
                with self.assertRaises(ValueError):
                    replay.replay(args.output)
        (args.output / "console.log").write_bytes(raw)
        report_path.write_bytes(report_bytes)
        manifest_path.write_bytes(manifest_bytes)
        self.assertTrue(replay.replay(args.output)["capture_complete"])


if __name__ == "__main__":
    unittest.main()

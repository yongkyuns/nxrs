"""Fault and framing controls; all generated records/processes are synthetic."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch


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

    def test_live_fault_refuses_the_first_read_even_with_a_ready_boot_prompt(self):
        for fault in FAULT_LINES:
            for variant in (fault, fault.lower(), fault.upper()):
                for suffix in (b"", b"\nNuttShell\r\nnsh> "):
                    payload = variant.encode() + suffix
                    transcript = bytearray()
                    with self.subTest(fault=variant, prompt=bool(suffix)), \
                            patch.object(capture.console.select, "select", return_value=([7], [], [])) as ready, \
                            patch.object(capture.console.os, "read", side_effect=[payload]) as read, \
                            patch.object(capture.console.time, "monotonic", return_value=0):
                        with self.assertRaisesRegex(RuntimeError, "kernel fault"):
                            capture.read_until(7, transcript, capture.console.fresh_boot, 120)
                        ready.assert_called_once()
                        read.assert_called_once_with(7, 65536)
                        self.assertEqual(transcript, payload)

    def test_split_fault_signature_and_fault_after_result_never_satisfy_the_prompt(self):
        for chunks in ([b"ASSER", b"TION failed\nNuttShell\nnsh> "],
                       [frame() + b"PANIC after result\nnsh> "]):
            transcript = bytearray()
            with self.subTest(chunks=len(chunks)), \
                    patch.object(capture.console.select, "select", return_value=([7], [], [])), \
                    patch.object(capture.console.os, "read", side_effect=chunks) as read, \
                    patch.object(capture.console.time, "monotonic", return_value=0):
                with self.assertRaisesRegex(RuntimeError, "kernel fault"):
                    capture.read_until(7, transcript, lambda data: capture.console.PROMPT in data, 120)
                self.assertEqual(read.call_count, len(chunks))
                self.assertEqual(transcript, b"".join(chunks))


class CaptureAndReplayControls(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CaptureControls(
            "test_successful_host_capture_is_functional_only_and_output_is_never_reused")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_faulted_boot_never_sends_command_and_preserves_replayable_failure(self):
        executable, _ = self.fixture.executable("faulted-qemu", runtime="qemu")
        args = self.fixture.arguments("faulted-boot", runtime="qemu", qemu=str(executable))
        raw = b"arm_hardfault.c:123\nNuttShell\nnsh> "

        def read_fault(_fd, transcript, *_args):
            transcript.extend(raw)
            return raw

        with patch.object(capture.console, "read_until", side_effect=read_fault), \
                patch.object(capture.console, "write_command") as written:
            report = capture.capture(args)
        written.assert_not_called()
        self.assertFalse(report["capture_complete"])
        self.assertEqual(report["failure_stage"], "fresh-boot")
        self.assertIn("kernel fault", report["error"])
        self.assertEqual((args.output / "console.log").read_bytes(), raw)
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

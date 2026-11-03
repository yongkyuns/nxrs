"""Capture lifecycle controls using synthetic streams, never physical evidence."""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


evidence = module("progress_capture_evidence_fixtures", HERE / "test_analyze.py")
with mock.patch.dict(sys.modules, {"analyze": evidence.analyze}):
    capture = module("channel_progress_capture_tests", HERE / "capture.py")


class StreamTests(unittest.TestCase):
    def test_fresh_boot_requires_banner_and_prompt(self):
        for banner in capture.BOOT_BANNERS:
            self.assertTrue(capture.fresh_boot(banner + b"\r\nnsh> "))
            self.assertFalse(capture.fresh_boot(banner))
        self.assertFalse(capture.fresh_boot(b"nsh> "))
        self.assertFalse(capture.fresh_boot(b"old shell output\n"))

    def test_closed_faulted_and_expired_streams_preserve_raw_bytes(self):
        for payload, expected in ((b"partial boot\n", "EOF"),
                                  (b"HardFault at pc\n", "kernel fault")):
            read_fd, write_fd = os.pipe()
            try:
                os.write(write_fd, payload)
                os.close(write_fd)
                write_fd = None
                transcript = bytearray()
                with self.assertRaisesRegex(RuntimeError, expected):
                    capture.read_until(read_fd, transcript, lambda _: False, time.monotonic() + 1)
                self.assertEqual(transcript, payload)
                with self.assertRaisesRegex(TimeoutError, "external console deadline"):
                    capture.read_until(read_fd, transcript, lambda _: False, time.monotonic() - 1)
                self.assertEqual(transcript, payload)
            finally:
                os.close(read_fd)
                if write_fd is not None:
                    os.close(write_fd)

    def test_native_faults_fail_on_the_first_read_even_with_a_boot_prompt(self):
        for fault in evidence.FAULT_LINES:
            for variant in (fault, fault.lower(), fault.upper()):
                for suffix in (b"", b"NuttShell\r\nnsh> "):
                    payload = variant.encode() + suffix
                    transcript = bytearray()
                    with self.subTest(fault=variant, prompt=bool(suffix)), \
                            mock.patch.object(capture.select, "select", return_value=([7], [], [])) as ready, \
                            mock.patch.object(capture.os, "read", side_effect=[payload]) as read, \
                            mock.patch.object(capture.time, "monotonic", return_value=0):
                        with self.assertRaisesRegex(RuntimeError, "kernel fault"):
                            capture.read_until(7, transcript, capture.fresh_boot, 120)
                        ready.assert_called_once()
                        read.assert_called_once_with(7, 65536)
                        self.assertEqual(transcript, payload)

    def test_native_fault_signature_split_across_reads_is_retained_and_refused(self):
        payload = evidence.NUTTX_BOOT_FAULT.encode()
        boundary = payload.index(b"Assertion") + len(b"Assert")
        transcript = bytearray()
        with mock.patch.object(capture.select, "select", return_value=([7], [], [])), \
                mock.patch.object(capture.os, "read", side_effect=[payload[:boundary], payload[boundary:]]) as read, \
                mock.patch.object(capture.time, "monotonic", return_value=0):
            with self.assertRaisesRegex(RuntimeError, "kernel fault"):
                capture.read_until(7, transcript, capture.fresh_boot, 120)
        self.assertEqual(read.call_count, 2)
        self.assertEqual(transcript, payload)

    def test_only_named_progress_commands_are_allowed(self):
        for mode in capture.MODES:
            self.assertEqual(capture.command_for(mode), "channel_progress " + mode)
        for mode in ("latency", "select-reserved; reboot", "", "host-functional"):
            with self.assertRaises(ValueError):
                capture.command_for(mode)


class CaptureLifecycleTests(unittest.TestCase):
    def setUp(self):
        # BuildEvidenceTests supplies authenticated synthetic archives with
        # test-scoped pins. It does not replace the source/receipt validators.
        self.fixture = evidence.BuildEvidenceTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.session = dict(
            schema="channel-progress-session-v1", runtime_kind="physical",
            build_id=self.fixture.receipt["build_id"], image_sha256=self.fixture.receipt["image_sha256"],
            board="synthetic-board", measurement_session="synthetic-session", board_serial="unit-test",
            flash_verification="synthetic-only", active_cores=1,
        )
        self.session_path = self.root / "session-input.json"
        self.session_bytes = (json.dumps(self.session, indent=2) + "\n").encode()
        self.session_path.write_bytes(self.session_bytes)
        self.args = argparse.Namespace(
            build_directory=self.root, mode=self.fixture.mode, output=self.root / "capture",
            runtime="physical", session=self.session_path, port="unused-synthetic-port",
            timeout=1, qemu="unused-synthetic-qemu",
        )

    def result_bytes(self):
        metadata, result = evidence.fixture(self.fixture.mode)
        metadata["build_id"] = self.fixture.receipt["build_id"]
        return evidence.transcript(metadata, result).encode() + b"nsh> "

    def test_physical_session_is_frozen_before_capture_and_replayable(self):
        read_fd, write_fd = os.pipe()
        os.close(write_fd)
        saved = object()
        responses = 0

        def read_until(fd, transcript, predicate, deadline, process):
            nonlocal responses
            self.assertEqual(fd, read_fd)
            responses += 1
            if responses == 1:
                data = b"*** Booting NuttX ***\r\nnsh> "
            else:
                # A concurrent session edit must not change the retained
                # evidence of the bytes actually admitted before serial open.
                self.session_path.write_text('{"concurrently":"replaced"}\n')
                data = self.result_bytes()
            self.assertTrue(predicate(data))
            transcript.extend(data)
            return data

        with mock.patch.object(capture, "serial_open", return_value=(read_fd, saved)), \
                mock.patch.object(capture, "read_until", side_effect=read_until), \
                mock.patch.object(capture, "write_command"), \
                mock.patch.object(capture.termios, "tcsetattr"), \
                contextlib.redirect_stdout(io.StringIO()):
            result = capture.capture(self.args)
        self.assertTrue(result["capture_complete"], result)
        self.assertFalse(result["progress_contract_satisfied"])
        self.assertEqual((self.args.output / "session.json").read_bytes(), self.session_bytes)
        analysis = evidence.analyze.read_json(self.args.output / "analysis.json")
        self.assertEqual(analysis["session"], self.session)
        self.assertTrue(analysis["fixture_valid"])
        self.assertEqual(analysis["outcome"], "stall-observed")
        retained = evidence.analyze.build_receipt(self.args.output / "build.json", self.fixture.mode)
        self.assertEqual(retained["build_id"], self.fixture.receipt["build_id"])

    def test_duplicate_session_fields_refused_before_port_is_opened(self):
        self.session_path.write_bytes(self.session_bytes.replace(
            b'"active_cores": 1', b'"active_cores": 2, "active_cores": 1'))
        with mock.patch.object(capture, "serial_open") as serial:
            result = capture.capture(self.args)
        self.assertFalse(result["capture_complete"])
        serial.assert_not_called()
        self.assertIn("duplicate JSON", result["error"])
        self.assertTrue((self.args.output / "console.log").is_file())
        self.assertTrue((self.args.output / "capture.json").is_file())

    def test_external_timeout_retains_failed_log_and_terminates_emulator(self):
        self.args.runtime = "qemu"
        self.args.session = None
        self.args.port = None
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.return_value = 0

        def timeout(fd, transcript, predicate, deadline, child):
            transcript.extend(b"partial rejected boot\r\n")
            raise TimeoutError("synthetic external console deadline")

        with mock.patch.object(capture.subprocess, "Popen", return_value=process), \
                mock.patch.object(capture, "read_until", side_effect=timeout):
            result = capture.capture(self.args)
        self.assertFalse(result["capture_complete"])
        self.assertEqual(result["failure_stage"], "fresh-boot")
        self.assertIn("external console deadline", result["error"])
        self.assertEqual((self.args.output / "console.log").read_bytes(), b"partial rejected boot\r\n")
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=3)
        self.assertTrue((self.args.output / "session.json").is_file())
        self.assertFalse((self.args.output / "analysis.json").exists())

    def test_native_boot_fault_is_retained_without_sending_a_command_or_retrying(self):
        self.args.runtime = "qemu"
        self.args.session = self.args.port = None
        self.args.timeout = 120
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        payload = evidence.NUTTX_BOOT_FAULT.encode()
        read_fd, write_fd = os.pipe()

        def start(*args, **kwargs):
            os.write(kwargs["stdout"], payload)
            return process

        with mock.patch.object(capture.pty, "openpty", return_value=(read_fd, write_fd)), \
                mock.patch.object(capture.subprocess, "Popen", side_effect=start) as launch, \
                mock.patch.object(capture, "write_command") as command:
            result = capture.capture(self.args)
        self.assertFalse(result["capture_complete"])
        self.assertEqual(result["failure_stage"], "fresh-boot")
        self.assertEqual(result["error"], "RuntimeError: guest fixture failure or kernel fault")
        self.assertNotIn("fresh_boot_observed", result)
        command.assert_not_called()
        launch.assert_called_once()
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=3)
        self.assertEqual((self.args.output / "console.log").read_bytes(), payload)
        self.assertEqual(evidence.analyze.read_json(self.args.output / "capture.json"), result)
        self.assertTrue((self.args.output / "session.json").is_file())
        self.assertFalse((self.args.output / "analysis.json").exists())

    def test_existing_capture_cannot_be_overwritten(self):
        self.args.output.mkdir()
        marker = self.args.output / "console.log"
        marker.write_text("prior rejected evidence")
        with self.assertRaisesRegex(ValueError, "preserve previous evidence"):
            capture.capture(self.args)
        self.assertEqual(marker.read_text(), "prior rejected evidence")


if __name__ == "__main__":
    unittest.main()

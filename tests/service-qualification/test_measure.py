import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_publish import report as footprint_fixture

# Keep legacy serial-helper path setup local to this import.
with patch.object(sys, "path", sys.path.copy()):
    import measure as harness


def paired_files(root):
    records = footprint_fixture()["builds"]
    source = "tests/service-qualification/runtime.c"
    for language, record in records.items():
        folder = root / language
        folder.mkdir()
        for name in ("app.elf", "image.bin", "resolved.config"):
            (folder / name).write_bytes(name.encode())
        record.update(language=language, command="sq_" + language,
                      artifacts={name: harness.digest(folder / name)
                                 for name in ("app.elf", "image.bin", "resolved.config")},
                      source_sha256={source: harness.digest(harness.ROOT / source)},
                      kernel_header_sha256="a" * 64)
        (folder / "build-provenance.json").write_text(json.dumps(record))
    return records


class PairIdentityTests(unittest.TestCase):
    def test_matching_compiled_headers_are_required_with_frozen_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = paired_files(root)
            self.assertEqual(harness.validate_pair(root / "c", root / "rust"), records)
            for value in (None, "b" * 64, "invalid"):
                changed = copy.deepcopy(records["rust"])
                changed["kernel_header_sha256"] = value
                (root / "rust/build-provenance.json").write_text(json.dumps(changed))
                with self.subTest(header=value), self.assertRaisesRegex(ValueError, "kernel header"):
                    harness.validate_pair(root / "c", root / "rust")

    def test_missing_or_mismatched_headers_reject_before_board_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = paired_files(root)
            for missing in (True, False):
                changed = copy.deepcopy(records["rust"])
                if missing:
                    del changed["kernel_header_sha256"]
                else:
                    changed["kernel_header_sha256"] = "b" * 64
                (root / "rust/build-provenance.json").write_text(json.dumps(changed))
                args = SimpleNamespace(c=root / "c", rust=root / "rust")
                with self.subTest(missing=missing), patch.object(harness.subprocess, "run") as run, \
                        patch.object(harness, "open_serial") as serial, \
                        patch.object(harness, "load") as load, self.assertRaises(ValueError):
                    harness.measure(args)
                run.assert_not_called()
                serial.assert_not_called()
                load.assert_not_called()


class MeasurementRecoveryTests(unittest.TestCase):
    def check_capture(self, stage=None, *, restore_fails=False, provenance_changes=False,
                      harness_changes=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup = root / "backup.bin"
            backup.write_bytes(bytes(16777216))
            backup.chmod(0o600)
            args = SimpleNamespace(c=root / "c", rust=root / "rust", backup=backup,
                                   backup_sha256=harness.digest(backup), out=root / "capture",
                                   flasher="fixture", port="fixture", source="messages", period_us=2000,
                                   events=10, blocks=1, services=[3], language="c",
                                   rust_worker=None, rust_entry=None, pm_mode=None)
            fixture = footprint_fixture()
            fixture["runs"][0]["mean_us"] = 1.0
            check, flash = Mock(stdout="", stderr=""), Mock(stdout="", stderr="")
            capture_error = RuntimeError((stage or "capture") + " failed")
            if stage == "preflight":
                check.check_returncode.side_effect = capture_error
            if stage == "flash":
                flash.check_returncode.side_effect = capture_error
            restore_error = ValueError("restoration failed")
            restore = Mock(side_effect=restore_error if restore_fails else None)
            validations = None
            if provenance_changes:
                validations = [fixture["builds"]]
                if provenance_changes == "after_capture":
                    validations.append(fixture["builds"])
                validations.append(ValueError("frozen metadata changed"))
            captured = False
            digest = harness.digest

            def file_digest(path):
                if harness_changes and captured and path == harness.HERE / "evidence.py":
                    return "0" * 64
                return digest(path)

            def parse_output(*unused_args, **unused_kwargs):
                nonlocal captured
                captured = True
                return copy.deepcopy(fixture["runs"][0])

            with patch.object(harness, "validate_pair", return_value=fixture["builds"],
                              side_effect=validations), \
                    patch.object(harness, "digest", side_effect=file_digest), \
                    patch.object(harness, "load", return_value=SimpleNamespace(restore=restore)), \
                    patch.object(harness.subprocess, "run", side_effect=[check, flash]) as run, \
                    patch.object(harness, "open_serial", return_value=123) as serial, \
                    patch.object(harness, "read_prompt", return_value=b"boot", \
                                 side_effect=capture_error if stage == "capture" else None), \
                    patch.object(harness, "command", return_value=b"fixture"), \
                    patch.object(harness, "parse", side_effect=parse_output), \
                    patch.object(harness, "parse_free", return_value={"used": 100, "maxused": 1200}), \
                    patch.object(harness.os, "close") as close, \
                    contextlib.redirect_stdout(io.StringIO()) as stdout:
                raised = None
                try:
                    harness.measure(args)
                except (RuntimeError, ValueError) as error:
                    raised = error
            report = json.loads((args.out / "report.json").read_text())
            touched = stage != "preflight" and provenance_changes is not True
            self.assertEqual(report["restoration_verified"], touched and not restore_fails)
            self.assertEqual(report["restoration_error"], "ValueError: restoration failed" if restore_fails else None)
            if stage is not None:
                self.assertIs(raised, restore_error if restore_fails else capture_error)
                self.assertEqual(report["failure"], "RuntimeError: " + stage + " failed")
                if restore_fails:
                    self.assertIs(raised.__cause__, capture_error)
            elif provenance_changes:
                self.assertIsInstance(raised, ValueError)
                self.assertIn("frozen metadata changed", report["failure"])
            elif harness_changes:
                self.assertIsInstance(raised, ValueError)
                self.assertIn("measurement harness changed", report["failure"])
            elif restore_fails:
                self.assertIs(raised, restore_error)
                self.assertIsNone(report["failure"])
            else:
                self.assertIsNone(raised)
                self.assertIsNone(report["failure"])
                self.assertIn("SERVICE_MATRIX_PASS firmware_restored=true", stdout.getvalue())
                self.assertEqual(len(report["runs"]), 1)
                self.assertTrue(report["paired_kernel_headers_verified"])
                self.assertIn("tests/service-qualification/evidence.py", report["harness_sha256"])
            if raised is not None:
                self.assertNotIn("SERVICE_MATRIX_PASS", stdout.getvalue())
            if touched:
                restore.assert_called_once()
            else:
                restore.assert_not_called()
            if stage in ("preflight", "flash") or provenance_changes is True:
                serial.assert_not_called()
                close.assert_not_called()
            else:
                serial.assert_called_once()
                close.assert_called_once_with(123)
            self.assertEqual(run.call_count, 1 if not touched else 2)

    def test_preflight_failure_leaves_board_untouched(self):
        self.check_capture("preflight")

    def test_attempted_flash_failure_still_restores(self):
        self.check_capture("flash")

    def test_capture_failure_is_preserved_after_successful_restoration(self):
        self.check_capture("capture")

    def test_capture_and_restoration_failures_are_preserved_and_chained(self):
        self.check_capture("capture", restore_fails=True)

    def test_successful_capture_with_failed_restoration_is_not_success(self):
        self.check_capture(restore_fails=True)

    def test_successful_capture_and_verified_restoration_are_recorded(self):
        self.check_capture()

    def test_changed_provenance_is_rejected_before_first_flash(self):
        self.check_capture(provenance_changes=True)

    def test_changed_provenance_after_capture_still_restores(self):
        self.check_capture(provenance_changes="after_capture")

    def test_changed_harness_after_capture_still_restores(self):
        self.check_capture(harness_changes=True)


if __name__ == "__main__":
    unittest.main()

"""Offline checks for the common board-restoration policy."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtos_harness.matrix import backup_identity, restored_session, rotated_cases


def envelope():
    return {"backup_sha256": "frozen", "order": [], "failure": None,
            "restore_verified": False, "restore_error": None}


class SessionTests(unittest.TestCase):
    def test_success_restores_once_and_keeps_report_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "matrix.json"
            record, restore = envelope(), Mock()
            with restored_session(record, report, Path("backup"), restore=restore,
                                  identity=lambda path: "frozen"):
                record["order"].append({"case": "c", "block": 0})
            restore.assert_called_once_with()
            self.assertEqual(json.loads(report.read_text()), record)
            self.assertEqual(report.stat().st_mode & 0o777, 0o600)
            self.assertTrue(record["restore_verified"])

    def test_measurement_error_is_preserved_when_restore_also_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "matrix.json"
            record = envelope()
            restore = Mock(side_effect=OSError("restore failed"))
            with self.assertRaisesRegex(ValueError, "measurement failed"):
                with restored_session(record, report, Path("backup"), restore=restore):
                    raise ValueError("measurement failed")
            restore.assert_called_once_with()
            self.assertEqual(record["failure"], "ValueError: measurement failed")
            self.assertEqual(record["restore_error"], "OSError: restore failed")
            self.assertFalse(json.loads(report.read_text())["restore_verified"])

    def test_failed_restore_or_changed_backup_cannot_publish_success(self):
        for reason in ("restore", "backup"):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as temporary:
                report = Path(temporary) / "matrix.json"
                record = envelope()
                restore = Mock(side_effect=OSError("failed") if reason == "restore" else None)
                with self.assertRaisesRegex(RuntimeError, "firmware restoration failed"):
                    with restored_session(record, report, Path("backup"), restore=restore,
                                          identity=lambda path: "changed"):
                        pass
                self.assertFalse(record["restore_verified"])
                self.assertEqual(record["failure"], record["restore_error"])
                self.assertTrue(report.is_file())

    def test_interruption_still_restores_and_records_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            record, restore = envelope(), Mock()
            with self.assertRaises(KeyboardInterrupt):
                with restored_session(record, Path(temporary) / "matrix.json", Path("backup"),
                                      restore=restore, identity=lambda path: "frozen"):
                    raise KeyboardInterrupt()
            restore.assert_called_once_with()
            self.assertTrue(record["restore_verified"])
            self.assertTrue(record["failure"].startswith("KeyboardInterrupt:"))


class PreflightTests(unittest.TestCase):
    def test_backup_requires_full_private_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            backup = Path(temporary) / "firmware.bin"
            backup.write_bytes(b"incomplete")
            with self.assertRaisesRegex(ValueError, "complete 16 MiB"):
                backup_identity(backup)
            with backup.open("wb") as stream:
                stream.truncate(16_777_216)
            backup.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "private"):
                backup_identity(backup)
            backup.chmod(0o600)
            self.assertRegex(backup_identity(backup), r"^[0-9a-f]{64}$")

    def test_order_rotation_supports_tuples_without_mutating_input(self):
        cases = ("c", "rust", "zephyr")
        self.assertEqual(rotated_cases(cases, 4), ["rust", "zephyr", "c"])
        self.assertEqual(cases, ("c", "rust", "zephyr"))

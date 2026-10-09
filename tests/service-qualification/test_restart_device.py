import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

# The standalone serial CLI imports legacy helpers that extend sys.path.
# Isolate that setup so discovery cannot pick a neighboring test_results.py.
with patch.object(sys, "path", sys.path.copy()):
    from restart_device import digest, measure, public_report, summaries
from test_publish import report as footprint_fixture


def fixture():
    evidence = footprint_fixture()
    prototype = evidence["runs"][0]
    evidence.update(rounds=4, blocks=2, warmup_rounds=1, events=10,
                    restoration_error=None, harness_sha256={"tests/service-qualification/restart_device.py": "hash"})
    evidence["runs"] = []
    for block in range(2):
        for language in (("c", "rust") if block == 0 else ("rust", "c")):
            for round_ in range(4):
                for services in (3, 20):
                    row = copy.deepcopy(prototype)
                    row.update(block=block, language=language, round=round_)
                    row["result"].update(services=services, queues=services * 3)
                    row["nsh_after"].update(used=128 if round_ == 0 else 144, nused=10)
                    evidence["runs"].append(row)
    return evidence


class DeviceRestartTests(unittest.TestCase):
    def test_published_device_matrix_is_complete_private_and_bounded(self):
        self.check_device_matrix("esp32s3-message-restart-2026-10-09.json")

    def test_published_current_device_matrix_matches_frozen_runtime(self):
        evidence = self.check_device_matrix("esp32s3-message-restart-2026-10-09.json")
        runtime = "tests/service-qualification/runtime.c"
        expected = hashlib.sha256((Path(__file__).parent / "runtime.c").read_bytes()).hexdigest()
        for language, binary, code, resident in (("c", 213380, 174536, 66360),
                                                ("rust", 214040, 184770, 66504)):
            row = evidence["builds"][language]
            self.assertEqual(row["source_sha256"][runtime], expected)
            self.assertEqual((row["binary_bytes"], row["code_initialized_data_bytes"],
                              row["resident_ram_bytes"]), (binary, code, resident))

    def check_device_matrix(self, filename):
        path = Path(__file__).parent / "results" / filename
        evidence = json.loads(path.read_text())
        self.assertEqual((evidence["calls"], evidence["delivered_events"]), (80, 8000))
        self.assertEqual((evidence["rounds"], evidence["blocks"], evidence["warmup_rounds"]), (10, 2, 2))
        self.assertTrue(evidence["same_boot_per_language_block"])
        self.assertFalse(evidence["same_process"])
        self.assertFalse(evidence["fault_injection"])
        self.assertFalse(evidence["interrupt_qualified"])
        self.assertTrue(evidence["restoration_verified"])
        self.assertEqual(len(evidence["summaries"]), 4)
        self.assertEqual({(row["block"], row["language"]) for row in evidence["summaries"]},
                         {(block, language) for block in (0, 1) for language in ("c", "rust")})
        for row in evidence["summaries"]:
            self.assertEqual((row["calls"], row["delivered_events"], row["steady_samples"]), (20, 2000, 16))
            self.assertEqual(row["steady_heap_net_change_bytes"], 0)
            self.assertTrue(row["observed_steady_heap_flat"])
            self.assertEqual(row["steady_heap_min_bytes"], 7332 if row["language"] == "c" else 7372)
        for private in ("/Users/", "/home/", "/private/", "transcript", "backup_sha256"):
            self.assertNotIn(private, path.read_text())
        return evidence

    def test_complete_matrix_keeps_warmup_separate(self):
        result = summaries(fixture())
        self.assertEqual(len(result), 4)
        for row in result:
            self.assertEqual((row["calls"], row["delivered_events"], row["steady_samples"]), (8, 80, 6))
            self.assertEqual((row["first_post_heap_bytes"], row["steady_heap_min_bytes"]), (128, 144))
            self.assertTrue(row["observed_steady_heap_flat"])

    def test_incomplete_duplicate_reordered_or_failed_rows_are_rejected(self):
        for mutation in (lambda rows: rows.pop(), lambda rows: rows.append(copy.deepcopy(rows[0])),
                         lambda rows: rows.reverse(), lambda rows: rows[0]["result"].update(received=9),
                         lambda rows: rows[0]["done"].update(status=1)):
            item = fixture()
            mutation(item["runs"])
            with self.assertRaises(ValueError):
                summaries(item)

    def test_bounded_growth_is_reported_not_hidden_as_flat(self):
        item = fixture()
        item["runs"][7]["nsh_after"].update(used=160, nused=11)
        row = summaries(item)[0]
        self.assertEqual(row["steady_heap_max_bytes"], 160)
        self.assertEqual(row["steady_heap_net_change_bytes"], 16)
        self.assertFalse(row["observed_steady_heap_flat"])

    def test_public_summary_excludes_private_transcripts_and_requires_restoration(self):
        result = public_report(fixture())
        self.assertEqual((result["calls"], result["delivered_events"]), (32, 320))
        self.assertTrue(result["same_boot_per_language_block"])
        self.assertFalse(result["same_process"])
        self.assertFalse(result["fault_injection"])
        self.assertNotIn("runs", result)
        for private in ("/private", "transcript", "backup_sha256", "rustflags"):
            self.assertNotIn(private, json.dumps(result))
        for field, value in (("restoration_error", "verification failed"), ("failure", "capture failed"),
                             ("restoration_verified", False)):
            item = fixture()
            item[field] = value
            with self.assertRaises(ValueError):
                public_report(item)

    def test_preflight_failure_does_not_flash_or_restore(self):
        self.check_flash_failure(preflight=True)

    def test_attempted_flash_failure_still_restores(self):
        self.check_flash_failure()

    def test_restoration_failure_is_recorded_separately(self):
        self.check_flash_failure(restore_fails=True)

    def test_diagnostic_capture_failure_still_restores_and_keeps_both_errors(self):
        for diagnostic in ("faults", "pressure"):
            for restore_fails in (False, True):
                self.check_diagnostic_failure(diagnostic, restore_fails)

    def check_diagnostic_failure(self, diagnostic, restore_fails):
        with self.subTest(diagnostic=diagnostic, restore_fails=restore_fails), tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup = root / "backup.bin"
            backup.write_bytes(bytes(16777216))
            backup.chmod(0o600)
            args = SimpleNamespace(c=root / "c", rust=root / "rust", backup=backup, out=root / "capture",
                                   backup_sha256=digest(backup), flasher="fixture", port="fixture-port",
                                   blocks=1, rounds=1, warmup_rounds=0, events=100)
            records = fixture()["builds"]
            for row in records.values():
                row["diagnostic_" + diagnostic] = True
            restore = Mock(side_effect=RuntimeError("restoration failed") if restore_fails else None)
            capture = Mock(side_effect=ValueError("diagnostic capture failed"))
            check = Mock(stdout="", stderr="")
            with patch("restart_device.validate_pair", return_value=records), \
                    patch("restart_device.load", return_value=SimpleNamespace(restore=restore)), \
                    patch("restart_device.subprocess.run", return_value=check), \
                    patch("restart_device.open_serial", return_value=123), \
                    patch("restart_device.read_prompt", return_value=b"nsh> "), \
                    patch("restart_device.os.close") as close:
                with self.assertRaisesRegex(RuntimeError if restore_fails else ValueError,
                                            "restoration failed" if restore_fails else "diagnostic capture failed"):
                    measure(args, capture=capture, diagnostic=diagnostic)
            capture.assert_called_once()
            close.assert_called_once_with(123)
            restore.assert_called_once()
            evidence = json.loads((args.out / "report.json").read_text())
            self.assertEqual(evidence["failure"], "ValueError: diagnostic capture failed")
            self.assertEqual(evidence["diagnostic_capture"], diagnostic)
            self.assertEqual(evidence["restoration_verified"], not restore_fails)
            self.assertEqual(evidence["restoration_error"], "RuntimeError: restoration failed" if restore_fails else None)

    def test_diagnostic_callback_rejects_normal_images_before_board_access(self):
        for diagnostic in ("faults", "pressure"):
            with self.subTest(diagnostic=diagnostic), \
                    patch("restart_device.validate_pair", return_value=fixture()["builds"]), \
                    patch("restart_device.subprocess.run") as run:
                with self.assertRaisesRegex(ValueError, "capture mode"):
                    measure(SimpleNamespace(c=Path("c"), rust=Path("rust")), capture=Mock(), diagnostic=diagnostic)
            run.assert_not_called()

    def check_flash_failure(self, preflight=False, restore_fails=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup = root / "backup.bin"
            backup.write_bytes(bytes(16777216))
            backup.chmod(0o600)
            args = SimpleNamespace(c=root / "c", rust=root / "rust", backup=backup, out=root / "capture",
                                   backup_sha256=digest(backup), flasher="fixture-esptool", port="fixture-port",
                                   blocks=1, rounds=2, warmup_rounds=1, events=10)
            restore = Mock(side_effect=RuntimeError("restore verification failed") if restore_fails else None)
            check = Mock(stdout="", stderr="")
            flash_error = subprocess.CalledProcessError(1, "fixture-esptool")
            if preflight:
                check.check_returncode.side_effect = flash_error
            effects = [check] if preflight else [check, flash_error]
            with patch("restart_device.validate_pair", return_value=fixture()["builds"]), \
                    patch("restart_device.load", return_value=SimpleNamespace(restore=restore)), \
                    patch("restart_device.subprocess.run", side_effect=effects) as run:
                with self.assertRaises(RuntimeError if restore_fails else subprocess.CalledProcessError):
                    measure(args)
            self.assertEqual(run.call_count, 1 if preflight else 2)
            self.assertEqual(restore.call_count, 0 if preflight else 1)
            evidence = json.loads((args.out / "report.json").read_text())
            self.assertIsNotNone(evidence["failure"])
            self.assertEqual(evidence["restoration_verified"], not preflight and not restore_fails)
            self.assertEqual(evidence["restoration_error"] is not None, restore_fails)


if __name__ == "__main__":
    unittest.main()

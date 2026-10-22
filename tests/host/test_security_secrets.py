"""Scanner failures and findings must never disclose matching source or secrets."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("secret_check", ROOT / "tools/scan-secrets.py")
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


class SecretScanTests(unittest.TestCase):
    def test_findings_print_locations_without_sensitive_fields(self):
        finding = {"RuleID": "test", "File": "source.rs", "StartLine": 2, "Commit": "abc",
                   "Secret": "private-value", "Match": "private-value", "Line": "private-value"}
        output = io.StringIO()
        with patch.object(CHECK, "execute", return_value=([finding], "private-value")), contextlib.redirect_stdout(output):
            with self.assertRaises(ValueError):
                CHECK.scan("scanner")
        self.assertNotIn("private-value", output.getvalue())
        self.assertEqual(json.loads(output.getvalue()), {k: finding[k] for k in ("RuleID", "File", "StartLine", "Commit")})

    def test_scanner_error_or_missing_report_cannot_pass(self):
        for code in [0, 1, 2, 101]:
            with self.subTest(code=code), patch.object(CHECK.subprocess, "run", side_effect=[
                subprocess.CompletedProcess([], 0, stdout="8.30.1\n"),
                subprocess.CompletedProcess([], code, stdout="private-output", stderr="private-error")]):
                with self.assertRaisesRegex(ValueError, "raw output withheld"):
                    CHECK.execute("scanner", "git", ROOT, ROOT / "security/gitleaks.toml")

    def test_cli_errors_withhold_exception_text(self):
        output = io.StringIO()
        with patch.object(CHECK.sys, "argv", ["scan-secrets", "scan", "--scanner", "scanner"]), \
                patch.object(CHECK, "scan", side_effect=ValueError("private-input")), \
                contextlib.redirect_stderr(output):
            self.assertEqual(CHECK.main(), 1)
        self.assertNotIn("private-input", output.getvalue())

    def test_archive_checksum_failure_never_installs_a_binary(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(CHECK.platform, "system", return_value="Linux"), \
                patch.object(CHECK.platform, "machine", return_value="x86_64"), \
                patch.object(CHECK, "urlopen", return_value=io.BytesIO(b"untrusted binary")):
            output = Path(directory) / "scanner"
            with self.assertRaisesRegex(ValueError, "checksum"):
                CHECK.install(output)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()

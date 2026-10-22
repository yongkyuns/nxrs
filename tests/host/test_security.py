"""Security-gate negative controls; no network or real credentials."""
import datetime as dt
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("security_check", ROOT / "tools/check-security.py")
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "security").mkdir()
        (self.root / "gateway").mkdir()
        self.policy = self.root / "security/advisory-exceptions.toml"
        self.policy.write_text("exception = []\n")
        for lock in CHECK.WORKSPACES.values():
            (self.root / lock).write_text("version = 4\n")
        self.today = dt.date(2026, 10, 22)

    def exception(self, **changes):
        item = dict(id="RUSTSEC-2025-0024", workspace="root", owner="maintainer",
                    reason="Reviewed affected API is unreachable; upgrade tracked",
                    issue="https://github.com/yongkyuns/nxrs/issues/38", expires="2026-11-01")
        item.update(changes)
        return "[[exception]]\n" + "\n".join(f'{k} = "{v}"' for k, v in item.items()) + "\n"

    def test_exceptions_are_scoped_and_require_future_expiry(self):
        self.policy.write_text(self.exception())
        self.assertEqual(CHECK.exceptions(self.policy, self.today),
                         {"root": ["RUSTSEC-2025-0024"], "gateway": []})
        for expires in ["2026-10-21", "2026-10-22", "2027-02-01", "invalid"]:
            with self.subTest(expires=expires):
                self.policy.write_text(self.exception(expires=expires))
                with self.assertRaises(ValueError):
                    CHECK.exceptions(self.policy, self.today)

    def test_malformed_unaccountable_and_duplicate_exceptions_fail(self):
        cases = [self.exception(owner=""), self.exception(reason=""),
                 self.exception(issue="https://example.com/38"),
                 self.exception(workspace="all"), self.exception(id="anything"),
                 self.exception() + self.exception(), "ignore = []\n",
                 self.exception().replace('owner = "maintainer"\n', "")]
        for value in cases:
            with self.subTest(value=value):
                self.policy.write_text(value)
                with self.assertRaises(ValueError):
                    CHECK.exceptions(self.policy, self.today)

    def test_full_pins_and_checkout_credential_setting(self):
        sha = "a" * 40
        workflow = f"steps:\n  - uses: 'actions/checkout@{sha}' # reviewed\n    with:\n      persist-credentials: false\n  - uses: owner/action@{sha}\n"
        self.assertEqual(CHECK.action_errors(workflow, "test.yml"), [])
        for bad in [workflow.replace(sha, "v4"), workflow.replace(sha, "abc123"),
                    workflow.replace("persist-credentials: false", "persist-credentials: true"),
                    workflow.replace("persist-credentials: false", "# persist-credentials: false"),
                    "steps:\n  - uses: |\n      actions/checkout@" + sha]:
            with self.subTest(value=bad):
                self.assertTrue(CHECK.action_errors(bad, "test.yml"))

    def test_credential_setting_in_later_step_cannot_satisfy_checkout(self):
        workflow = f"  - uses: actions/checkout@{'b' * 40}\n  - name: Other step\n    with:\n      persist-credentials: false\n"
        self.assertTrue(CHECK.action_errors(workflow, "test.yml"))

    def test_auditor_failure_is_not_a_clean_scan_and_config_is_isolated(self):
        for returncode in [1, 2, 101]:
            calls = []
            def run(command, **kwargs):
                calls.append(command)
                if "--version" in command:
                    return subprocess.CompletedProcess(command, 0, stdout="cargo-audit 0.22.2\n")
                config = Path(kwargs["cwd"]) / ".cargo/audit.toml"
                self.assertEqual(config.read_text(), "[advisories]\nignore = []\n")
                return subprocess.CompletedProcess(command, returncode)
            with self.subTest(code=returncode), patch.object(CHECK.subprocess, "run", side_effect=run):
                with self.assertRaisesRegex(ValueError, "advisory audit failed"):
                    CHECK.audit(self.root, "gateway", "auditor")
            self.assertIn(str(self.root / "gateway/Cargo.lock"), calls[-1])
            self.assertIn("unsound", calls[-1])
            self.assertNotIn("--no-fetch", calls[-1])
            self.assertNotIn("--stale", calls[-1])

    def test_missing_or_symlinked_lock_is_not_generated_or_followed(self):
        lock = self.root / "Cargo.lock"
        lock.unlink()
        with patch.object(CHECK.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                CHECK.audit(self.root, "root", "auditor")
            lock.symlink_to(self.root / "gateway/Cargo.lock")
            with self.assertRaises(ValueError):
                CHECK.audit(self.root, "root", "auditor")
            run.assert_not_called()

    def test_wrong_auditor_version_is_rejected(self):
        with patch.object(CHECK.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="cargo-audit 0.21.0")):
            with self.assertRaisesRegex(ValueError, "0.22.2 required"):
                CHECK.audit(self.root, "root", "auditor")

    def test_checked_in_policy(self):
        CHECK.policy(ROOT)


if __name__ == "__main__":
    unittest.main()

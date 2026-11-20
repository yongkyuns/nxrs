"""Process controls for the qualification runner, never target-build evidence."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("concurrent_runner", ROOT / "tools/qualify-concurrent-builds.py")
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)

# This child only marks phases and waits. It never produces a valid build report
# or any of the actual checker-required target artifacts.
CHILD = r"""
import json, pathlib, signal, subprocess, sys, time
out=pathlib.Path(sys.argv[1]); settings=json.loads(sys.argv[2])
out.mkdir()
(out/'pid').write_text(str(__import__('os').getpid()))
time.sleep(settings.get('ready_after',0))
(out/'prepared.fixture').write_text(settings.get('label','prepared'))
if settings.get('descendant'):
    code="import os,pathlib,signal,sys,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(30)"
    child=subprocess.Popen([sys.executable,'-c',code,str(out/'descendant.pid')])
    deadline=time.monotonic()+2
    while not (out/'descendant.pid').exists():
        assert time.monotonic()<deadline
        time.sleep(.005)
print('fixture child reached prepared phase',flush=True)
time.sleep(settings.get('hold',.35))
raise SystemExit(settings.get('status',0))
"""


@unittest.skipUnless(sys.platform == "linux", "runner process groups are Linux qualification")
class ConcurrentRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="nxrs-concurrent-runner-")
        self.root = Path(self.temporary.name)
        self.outputs = {case: self.root / case for case in RUNNER.CASES}
        self.report = {"cases": {}, "overlap": None}
        self.phases = []

    def tearDown(self):
        self.temporary.cleanup()

    def commands(self, first=None, second=None):
        return {
            case: [sys.executable, "-u", "-c", CHILD, str(self.outputs[case]), json.dumps(settings or {})]
            for case, settings in zip(RUNNER.CASES, (first, second))
        }

    @staticmethod
    def capture(_case, out, _root):
        path = out / "prepared.fixture"
        if not path.exists():
            return None
        return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()}

    def run_pair(self, commands=None, capture=None, timeout=3, cancelled=None):
        RUNNER.run_pair(commands or self.commands(), self.outputs, self.root, self.root,
                        dict(os.environ), time.monotonic() + timeout,
                        capture or self.capture, self.report, self.phases.append,
                        poll_seconds=.01, cancelled=cancelled)

    @staticmethod
    def running(pid):
        # A killed orphan can briefly remain a zombie until init reaps it. It
        # must not remain executing after process-group cleanup.
        status = Path(f"/proc/{pid}/stat")
        try:
            return status.read_text().rsplit(")", 1)[1].split()[0] != "Z"
        except FileNotFoundError:
            return False

    def assert_retired(self):
        for record in self.report["cases"].values():
            self.assertIsNotNone(record["returncode"])
            self.assertIsNotNone(record["exited_ns"])
            self.assertFalse(self.running(record["pid"]))

    def test_both_prepared_children_overlap_before_successful_exits(self):
        self.run_pair(self.commands({"ready_after": .03}, {"ready_after": .08}))
        overlap = self.report["overlap"]
        self.assertEqual(overlap["alive_before"], list(RUNNER.CASES))
        self.assertEqual(overlap["alive_after"], list(RUNNER.CASES))
        self.assertLessEqual(overlap["start_ns"], overlap["end_ns"])
        self.assertIn("prepared-input-overlap", self.phases)
        for case, record in self.report["cases"].items():
            self.assertLess(record["started_ns"], overlap["start_ns"])
            self.assertLess(overlap["end_ns"], record["exited_ns"])
            self.assertEqual(record["returncode"], 0)
            self.assertEqual(record["prepared_files"], self.capture(case, self.outputs[case], self.root))
            self.assertIn("prepared phase", (self.root / f"{case}.log").read_text())
        self.assert_retired()

    def test_two_started_pids_do_not_prove_prepared_overlap(self):
        with self.assertRaisesRegex(RUNNER.QualificationError, "before prepared-input overlap"):
            self.run_pair(self.commands({"hold": .02}, {"ready_after": .3, "hold": 30}))
        self.assertIsNone(self.report["overlap"])
        self.assert_retired()

    def test_exit_during_snapshot_is_not_overlap(self):
        def slow_capture(case, out, root):
            value = self.capture(case, out, root)
            if value is not None and case == "lsm6dso":
                time.sleep(.2)
            return value
        with self.assertRaisesRegex(RUNNER.QualificationError, "before prepared-input overlap"):
            self.run_pair(self.commands({"hold": .08}, {"hold": 30}), capture=slow_capture)
        self.assertIsNone(self.report["overlap"])
        self.assert_retired()

    def test_failed_child_stops_the_other_process_group(self):
        with self.assertRaisesRegex(RUNNER.QualificationError, "bmi270 build failed with exit 7"):
            self.run_pair(self.commands({"hold": .15, "status": 7}, {"hold": 30}))
        self.assert_retired()

    def test_failed_leader_does_not_leave_a_term_ignoring_descendant(self):
        with self.assertRaisesRegex(RUNNER.QualificationError, "bmi270 build failed"):
            self.run_pair(self.commands({"hold": .15, "status": 7, "descendant": True}, {"hold": 30}))
        descendant = int((self.outputs["bmi270"] / "descendant.pid").read_text())
        deadline = time.monotonic() + 1
        while self.running(descendant) and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.running(descendant), "orphaned build descendant survived cleanup")
        self.assert_retired()

    def test_timeout_retires_both_children(self):
        with self.assertRaisesRegex(RUNNER.QualificationError, "deadline expired"):
            self.run_pair(self.commands({"ready_after": 30}, {"ready_after": 30}), timeout=.15)
        self.assert_retired()

    def test_malformed_prepared_evidence_aborts_both_children(self):
        def malformed(case, out, root):
            if self.capture(case, out, root) is not None:
                raise ValueError("substituted prepared handoff")
            return None
        with self.assertRaisesRegex(ValueError, "substituted prepared handoff"):
            self.run_pair(self.commands({"hold": 30}, {"hold": 30}), capture=malformed)
        self.assert_retired()

    def test_interrupt_retires_both_children(self):
        def interrupted(case, out, root):
            if self.capture(case, out, root) is not None:
                raise KeyboardInterrupt()
            return None
        with self.assertRaises(KeyboardInterrupt):
            self.run_pair(self.commands({"hold": 30}, {"hold": 30}), capture=interrupted)
        self.assert_retired()

    def test_pending_interrupt_does_not_start_children(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_pair(cancelled=lambda: True)
        self.assertEqual(self.report["cases"], {})
        self.assertTrue(all(not path.exists() for path in self.outputs.values()))

    def test_requested_interrupt_after_launch_retires_children(self):
        def cancelled():
            return all((out / "prepared.fixture").exists() for out in self.outputs.values())
        with self.assertRaises(KeyboardInterrupt):
            self.run_pair(self.commands({"hold": 30}, {"hold": 30}), cancelled=cancelled)
        self.assert_retired()

    def test_second_spawn_failure_retires_already_started_child(self):
        commands = self.commands({"hold": 30})
        commands["lsm6dso"] = [str(self.root / "missing-frontend")]
        with self.assertRaises(FileNotFoundError):
            self.run_pair(commands)
        self.assertEqual(set(self.report["cases"]), {"bmi270"})
        self.assert_retired()

    def test_shared_mutable_build_overrides_are_rejected(self):
        for name in RUNNER.FORBIDDEN_ENVIRONMENT:
            with self.subTest(name=name), mock.patch.dict(os.environ, {name: "/tmp/shared"}):
                with self.assertRaisesRegex(RUNNER.QualificationError, name):
                    RUNNER.environment()


class SourceAndOutputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="nxrs-concurrent-source-")
        self.root = Path(self.temporary.name).resolve()
        self.git("init", "-q")
        self.git("config", "user.name", "Qualification Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.root / ".gitignore").write_text("/target/\n")
        (self.root / "input.txt").write_text("source\n")
        self.git("add", ".gitignore", "input.txt")
        self.git("commit", "-qm", "fixture source")
        self.revision = self.git("rev-parse", "HEAD").strip()

    def tearDown(self):
        self.temporary.cleanup()

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, text=True,
                                       stderr=subprocess.STDOUT)

    def test_source_requires_exact_revision_and_clean_inputs(self):
        source = RUNNER.checked_source(self.root, self.revision, ("input.txt",))
        self.assertEqual(source["tree"], self.git("rev-parse", "HEAD^{tree}").strip())
        with self.assertRaisesRegex(RUNNER.QualificationError, "differs"):
            RUNNER.checked_source(self.root, "0" * 40, ("input.txt",))
        (self.root / "input.txt").write_text("changed\n")
        with self.assertRaisesRegex(RUNNER.QualificationError, "clean"):
            RUNNER.checked_source(self.root, self.revision, ("input.txt",))

    def test_untracked_source_is_not_silently_admitted(self):
        (self.root / "untracked.rs").write_text("// unexpected input\n")
        with self.assertRaisesRegex(RUNNER.QualificationError, "clean"):
            RUNNER.checked_source(self.root, self.revision, ("input.txt",))

    @unittest.skipUnless(sys.platform == "linux", "Linux qualification runner")
    def test_late_cancellation_removes_success_receipts(self):
        # Build/checker stubs only exercise publication lifecycle; these files
        # are deliberately not valid target artifacts or qualification reports.
        original_atomic_json = RUNNER.atomic_json
        for phase in ("audit", "export", "publication"):
            with self.subTest(phase=phase):
                requested = False
                parent = self.root / "target" / phase
                checker = mock.Mock(SOURCE_INPUTS=("input.txt",))
                checker.finish_case.return_value = {}

                def frontend(_root, output, _env, _deadline, _cancelled):
                    binary = output / "frontend.fixture"
                    binary.write_text("not an executable\n")
                    return {"path": str(binary), "sha256": RUNNER.sha256(binary)}

                def pair(_commands, outputs, _root, output, _env, _deadline,
                         _capture, report, _checkpoint, **_kwargs):
                    for case in RUNNER.CASES:
                        report["cases"][case] = {"output": str(outputs[case])}
                        (output / f"{case}.log").write_text("fixture log\n")

                def audit(*_args):
                    nonlocal requested
                    if phase == "audit":
                        requested = True

                def export(_report, _root, destination):
                    nonlocal requested
                    destination.mkdir()
                    (destination / "concurrent-builds.json").write_text("fixture receipt\n")
                    (destination / "retained.fixture").write_text("diagnostics\n")
                    if phase == "export":
                        requested = True

                def publish(path, value):
                    nonlocal requested
                    original_atomic_json(path, value)
                    if phase == "publication" and path == parent / "concurrent-builds.json":
                        requested = True

                checker.check_report.side_effect = audit
                checker.export_bundle.side_effect = export
                with mock.patch.object(RUNNER, "load_checker", return_value=checker), \
                        mock.patch.object(RUNNER, "build_frontend", side_effect=frontend), \
                        mock.patch.object(RUNNER, "run_pair", side_effect=pair), \
                        mock.patch.object(RUNNER, "atomic_json", side_effect=publish), \
                        mock.patch("builtins.print") as printed:
                    with self.assertRaises(KeyboardInterrupt):
                        RUNNER.run(self.root, parent, self.revision, 5,
                                   cancelled=lambda: requested)
                self.assertFalse((parent / "concurrent-builds.json").exists())
                self.assertFalse((parent / "evidence/concurrent-builds.json").exists())
                diagnostic = json.loads((parent / "concurrent-builds.incomplete.json").read_text())
                self.assertEqual(diagnostic["status"], "failed")
                self.assertIn("interrupted", diagnostic["error"])
                self.assertFalse(any("PASS" in str(call) for call in printed.call_args_list))
                if phase != "audit":
                    self.assertEqual((parent / "evidence/retained.fixture").read_text(), "diagnostics\n")

    def test_output_must_be_fresh_ignored_and_outside_tracked_source(self):
        out = RUNNER.fresh_parent(self.root, Path("target/qualification"))
        (out / "keep.txt").write_text("retained\n")
        for path in (out, self.root / "source-output", self.root / "target/../source-output"):
            with self.subTest(path=path), self.assertRaises(RUNNER.QualificationError):
                RUNNER.fresh_parent(self.root, path)
        self.assertEqual((out / "keep.txt").read_text(), "retained\n")
        self.assertFalse((self.root / "source-output").exists())

    @unittest.skipUnless(os.name == "posix", "symlink test")
    def test_output_cannot_follow_a_symlink_to_another_build(self):
        (self.root / "target").mkdir()
        (self.root / "target/real").mkdir()
        (self.root / "target/link").symlink_to("real", target_is_directory=True)
        with self.assertRaisesRegex(RUNNER.QualificationError, "symlinks"):
            RUNNER.fresh_parent(self.root, Path("target/link/new"))
        self.assertFalse((self.root / "target/real/new").exists())


if __name__ == "__main__":
    unittest.main()

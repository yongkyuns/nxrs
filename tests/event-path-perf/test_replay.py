"""Replay actual capture outputs after relocation/pruning; records are synthetic."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


replay_module = load("event_path_replay_tests", "replay.py")
fixtures = load("event_path_replay_capture_fixtures", "test_capture.py")
capture = fixtures.capture


class ReplayControls(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CaptureControls("test_successful_host_capture_is_functional_only_and_output_is_never_reused")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        relocated = tempfile.TemporaryDirectory(prefix="event-path-relocated-")
        self.addCleanup(relocated.cleanup)
        self.relocated = Path(relocated.name)

    def qemu_capture(self, *, shared=False):
        rows = fixtures.fixture(native=True)
        rows[0]["build_id"] = self.fixture.receipt["build_id"]
        binary, _ = self.fixture.executable("qemu", runtime="qemu", records=fixtures.transcript(rows))
        args = self.fixture.arguments("runs/ready" if shared else "ready", runtime="qemu", qemu=str(binary),
                                      artifact_root=self.fixture.root if shared else None)
        report = capture.capture(args)
        self.assertTrue(report["capture_complete"], report["error"])
        return args.output, binary

    def physical_capture(self, calibration_kind="valid", *, cleanup_failure=False):
        _, receipt, session = self.fixture.physical()
        receipt["build_id"] = capture.builder.build_identity(receipt)
        self.fixture.receipt = receipt
        (self.fixture.build / "build.json").write_text(json.dumps(receipt, indent=2) + "\n")
        session["clock_calibration"]["build_id"] = receipt["build_id"]
        original_trace = self.fixture.root / session["clock_calibration"]["evidence_file"]
        if calibration_kind == "absolute":
            session["clock_calibration"]["evidence_file"] = str(original_trace)
        elif calibration_kind == "mismatch":
            session["clock_calibration"]["evidence_sha256"] = "0" * 64
        elif calibration_kind == "missing":
            del session["clock_calibration"]
        elif calibration_kind == "unavailable":
            original_trace.unlink()
        source = self.fixture.root / "operator-session.json"
        session_bytes = (json.dumps(session, indent=3) + "\n\n").encode()
        source.write_bytes(session_bytes)
        rows = fixtures.fixture(native=True, hardware_counter=True)
        rows[0]["build_id"] = receipt["build_id"]
        args = self.fixture.arguments("physical", runtime="serial", session=source,
                                      port="synthetic", timeout=90)
        descriptor = os.open(os.devnull, os.O_RDWR)
        replies = iter((b"*** Booting NuttX *** synthetic\nnsh> ",
                        b"\n" + fixtures.transcript(rows).encode() + b"nsh> "))

        def read_until(_fd, buffer, *_args):
            data = next(replies)
            buffer.extend(data)
            return data

        with patch.object(capture.console, "serial_open", return_value=(descriptor, ["synthetic"])), \
                patch.object(capture.console, "read_until", side_effect=read_until), \
                patch.object(capture.console, "write_command"), \
                patch.object(capture.termios, "tcsetattr",
                             side_effect=OSError("synthetic cleanup failure") if cleanup_failure else None):
            report = capture.capture(args)
        self.assertEqual(report["capture_complete"], not cleanup_failure, report["error"])
        self.assertEqual((args.output / "session.json").read_bytes(), session_bytes)
        return args.output, source, original_trace

    def prune_and_relocate(self, output, *originals):
        shutil.rmtree(self.fixture.build)
        for path in originals:
            if path.exists():
                path.unlink()
        destination = self.relocated / "capture"
        shutil.move(str(output), destination)
        return destination

    def rewrite_manifest_hash(self, directory, filename):
        manifest = json.loads((directory / "evidence.json").read_text())
        manifest["files_sha256"][filename] = capture.digest(directory / filename)
        (directory / "evidence.json").write_text(json.dumps(manifest))

    def test_standalone_qemu_replays_after_original_build_and_executable_are_removed(self):
        output, executable = self.qemu_capture()
        original_receipt = (self.fixture.build / "build.json").read_bytes()
        relocated = self.prune_and_relocate(output, executable, executable.with_suffix(".json"))
        self.assertEqual((relocated / "build/build.json").read_bytes(), original_receipt)
        with patch.object(replay_module.capture.subprocess, "run", side_effect=AssertionError("replay executed a process")), \
                patch.object(replay_module.capture.subprocess, "Popen", side_effect=AssertionError("replay started an emulator")):
            result = replay_module.replay(relocated)
        self.assertTrue(result["evidence_valid"])
        self.assertTrue(result["capture_complete"])
        self.assertTrue(result["stored_analysis_verified"])
        self.assertTrue(result["build_verified"])
        self.assertFalse(result["physical_timing_qualified"])
        self.assertFalse(result["priority_progress_qualified"])

    def test_host_binary_and_generic_failed_capture_replay_without_execution(self):
        for failed in (False, True):
            with self.subTest(failed=failed):
                name = "host-failure" if failed else "host-success"
                options = ({"records": "EVENT_PATH_PERF FAIL synthetic-refusal\n", "exit_code": 1}
                           if failed else {})
                binary, _ = self.fixture.executable(name, **options)
                args = self.fixture.arguments(name + "-capture", binary=binary)
                report = capture.capture(args)
                self.assertEqual(report["capture_complete"], not failed)
                binary.unlink()
                binary.with_suffix(".json").unlink()
                moved = self.relocated / name
                shutil.move(str(args.output), moved)
                result = replay_module.replay(moved)
                self.assertTrue(result["evidence_valid"])
                self.assertTrue(result["binary_verified"])
                self.assertEqual(result["capture_complete"], not failed)
                self.assertEqual(result["stored_analysis_verified"], not failed)
                self.assertEqual(result["outcome"], "retained-capture-failure" if failed else "complete-capture")
                completed = subprocess.run([sys.executable, str(HERE / "replay.py"), str(moved)],
                                           capture_output=True, text=True)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(json.loads(completed.stdout)["capture_complete"], not failed)

    def test_shared_root_moves_once_and_requires_an_explicit_replay_root(self):
        output, _ = self.qemu_capture(shared=True)
        self.assertFalse((output / "build").exists())
        archive = self.relocated / "archive"
        shutil.move(str(self.fixture.root), archive)
        run = archive / "runs/ready"
        with self.assertRaisesRegex(ValueError, "explicit artifact root"):
            replay_module.replay(run)
        result = replay_module.replay(run, archive)
        self.assertTrue(result["capture_complete"])
        with self.assertRaises((OSError, ValueError)):
            replay_module.replay(run, run)
        manifest_path = run / "evidence.json"
        original = manifest_path.read_text()
        for key, value in (("directory", "../outside"), ("directory", str(archive / "build")),
                           ("capture_directory", "build")):
            with self.subTest(key=key, value=value):
                changed = json.loads(original)
                changed["build"][key] = value
                manifest_path.write_text(json.dumps(changed))
                with self.assertRaises(ValueError):
                    replay_module.replay(run, archive)
        manifest_path.write_text(original)

    def test_shared_host_binary_is_verified_after_archive_relocation(self):
        binary, _ = self.fixture.executable("shared-host")
        args = self.fixture.arguments("runs/host", binary=binary, artifact_root=self.fixture.root)
        self.assertTrue(capture.capture(args)["capture_complete"])
        self.assertFalse((args.output / "binary").exists())
        archive = self.relocated / "host-archive"
        shutil.move(str(self.fixture.root), archive)
        run = archive / "runs/host"
        self.assertTrue(replay_module.replay(run, archive)["binary_verified"])
        with self.assertRaisesRegex(ValueError, "explicit artifact root"):
            replay_module.replay(run)
        (archive / "shared-host").write_bytes(b"mutated archived executable")
        with self.assertRaisesRegex(ValueError, "executable identity"):
            replay_module.replay(run, archive)

    def test_absolute_calibration_trace_survives_session_build_and_trace_pruning(self):
        output, source, trace = self.physical_capture("absolute")
        trace.write_bytes(b"changed original trace after capture")
        relocated = self.prune_and_relocate(output, source, trace)
        result = replay_module.replay(relocated)
        self.assertTrue(result["capture_complete"])
        self.assertTrue(result["clock_calibration_qualified"])
        self.assertFalse(result["physical_timing_qualified"])
        manifest = json.loads((relocated / "evidence.json").read_text())
        manifest["calibration"] = {"status": "not-applicable", "original_file": None,
                                   "retained_file": None, "actual_sha256": None,
                                   "claimed_sha256": None, "error": None}
        (relocated / "evidence.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "physical capture skipped"):
            replay_module.replay(relocated)

    def test_hash_mismatch_calibration_replays_the_original_rejection_after_pruning(self):
        output, source, trace = self.physical_capture("mismatch")
        relocated = self.prune_and_relocate(output, source, trace)
        result = replay_module.replay(relocated)
        self.assertTrue(result["evidence_valid"])
        self.assertTrue(result["capture_complete"])
        self.assertFalse(result["clock_calibration_qualified"])
        manifest = json.loads((relocated / "evidence.json").read_text())
        self.assertEqual(manifest["calibration"]["status"], "retained")
        trace = relocated / manifest["calibration"]["retained_file"]
        trace.write_bytes(b"changed retained calibration")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            replay_module.replay(relocated)

    def test_missing_calibration_remains_valid_behavior_without_qualification(self):
        output, source, trace = self.physical_capture("missing")
        result = replay_module.replay(self.prune_and_relocate(output, source, trace))
        self.assertTrue(result["capture_complete"])
        self.assertTrue(result["stored_analysis_verified"])
        self.assertFalse(result["clock_calibration_qualified"])

    def test_operator_session_and_calibration_cannot_certify_physical_execution(self):
        output, _, _ = self.physical_capture()
        result = replay_module.replay(output)
        self.assertTrue(result["clock_calibration_qualified"])
        report_path = output / "capture.json"
        analysis_path = output / "analysis.json"
        report = json.loads(report_path.read_text())
        analysis = json.loads(analysis_path.read_text())
        self.assertIs(report["physical_execution_verified"], False)
        self.assertIs(analysis["capture_qualification"]["physical_execution_verified"], False)
        report["physical_execution_verified"] = True
        analysis["capture_qualification"]["physical_execution_verified"] = True
        report_path.write_text(json.dumps(report))
        analysis_path.write_text(json.dumps(analysis))
        self.rewrite_manifest_hash(output, "capture.json")
        self.rewrite_manifest_hash(output, "analysis.json")
        original_claims = (report_path.read_bytes(), analysis_path.read_bytes())
        with self.assertRaisesRegex(ValueError, "physical-execution promotion"):
            replay_module.replay(output)
        self.assertEqual((report_path.read_bytes(), analysis_path.read_bytes()), original_claims)

        report["physical_execution_verified"] = False
        report_path.write_text(json.dumps(report))
        self.rewrite_manifest_hash(output, "capture.json")
        with self.assertRaisesRegex(ValueError, "stored analysis differs"):
            replay_module.replay(output)

    def test_incomplete_capture_allows_only_absent_or_false_physical_execution_claim(self):
        binary, _ = self.fixture.executable("refusal", records="EVENT_PATH_PERF FAIL synthetic-refusal\n",
                                            exit_code=1)
        args = self.fixture.arguments("refused-capture", binary=binary)
        report = capture.capture(args)
        self.assertFalse(report["capture_complete"])
        self.assertFalse((args.output / "analysis.json").exists())
        self.assertNotIn("physical_execution_verified", report)
        self.assertTrue(replay_module.replay(args.output)["evidence_valid"])
        report_path = args.output / "capture.json"
        for value in (False, True, 0, 1, None, "false"):
            with self.subTest(value=value):
                report["physical_execution_verified"] = value
                report_path.write_text(json.dumps(report))
                self.rewrite_manifest_hash(args.output, "capture.json")
                if value is False:
                    result = replay_module.replay(args.output)
                    self.assertTrue(result["evidence_valid"])
                    self.assertFalse(result["capture_complete"])
                else:
                    with self.assertRaisesRegex(ValueError, "physical-execution promotion"):
                        replay_module.replay(args.output)

    def test_unavailable_calibration_does_not_follow_the_original_path_during_replay(self):
        output, source, trace = self.physical_capture("unavailable")
        trace.write_text("a new original file must never repair old evidence")
        result = replay_module.replay(self.prune_and_relocate(output, source))
        self.assertTrue(result["capture_complete"])
        self.assertFalse(result["clock_calibration_qualified"])

    def test_cleanup_failure_keeps_valid_analysis_and_failed_capture_status(self):
        output, source, trace = self.physical_capture(cleanup_failure=True)
        result = replay_module.replay(self.prune_and_relocate(output, source, trace))
        self.assertTrue(result["evidence_valid"])
        self.assertTrue(result["stored_analysis_verified"])
        self.assertFalse(result["capture_complete"])
        self.assertEqual(result["failure_stage"], "cleanup")

    def test_retained_files_build_provenance_and_stored_analysis_mutations_are_refused(self):
        output, _ = self.qemu_capture()
        for name in ("console.log", "capture.json", "analysis.json", "session.json", "build.json", "build/image"):
            with self.subTest(name=name):
                path = output / name
                original = path.read_bytes()
                path.write_bytes(original + b"changed")
                with self.assertRaises(ValueError):
                    replay_module.replay(output)
                path.write_bytes(original)
        stored = output / "analysis.json"
        changed = json.loads(stored.read_text())
        changed["raw_ticks"]["admission_call"]["max_ticks"] += 1
        stored.write_text(json.dumps(changed))
        self.rewrite_manifest_hash(output, "analysis.json")
        with self.assertRaisesRegex(ValueError, "stored analysis differs"):
            replay_module.replay(output)

    def test_manifest_cannot_promote_qualification_or_relabel_the_capture_command(self):
        output, _ = self.qemu_capture()
        report_path = output / "capture.json"
        original = report_path.read_text()
        for key, value in (("physical_timing_qualified", True),
                           ("priority_progress_qualified", True),
                           ("command", "event_path_perf ready-crossbeam-try 2 1 bytes16")):
            with self.subTest(key=key):
                changed = json.loads(original)
                changed[key] = value
                report_path.write_text(json.dumps(changed))
                self.rewrite_manifest_hash(output, "capture.json")
                with self.assertRaises(ValueError):
                    replay_module.replay(output)

    def test_recomputed_outer_hashes_do_not_hide_changed_frozen_build_provenance(self):
        output, _ = self.qemu_capture()
        path = output / "build/build.json"
        record = json.loads(path.read_text())
        source = next(iter(record["source_sha256"]))
        record["source_sha256"][source] = "f" * 64
        changed = json.dumps(record).encode()
        path.write_bytes(changed)
        (output / "build.json").write_bytes(changed)
        new_hash = capture.digest(path)
        report = json.loads((output / "capture.json").read_text())
        report["build_receipt_sha256"] = new_hash
        (output / "capture.json").write_text(json.dumps(report))
        manifest = json.loads((output / "evidence.json").read_text())
        manifest["build"]["receipt_sha256"] = new_hash
        manifest["files_sha256"]["build.json"] = new_hash
        manifest["files_sha256"]["capture.json"] = capture.digest(output / "capture.json")
        (output / "evidence.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "frozen provenance"):
            replay_module.replay(output)

    def test_manifest_and_nested_build_receipt_cannot_link_outside_the_archive(self):
        output, _ = self.qemu_capture()
        for name in ("evidence.json", "build/build.json"):
            with self.subTest(name=name):
                path = output / name
                original = path.read_bytes()
                external = self.fixture.root / (name.replace("/", "-") + "-external")
                external.write_bytes(original)
                path.unlink()
                path.symlink_to(external)
                with self.assertRaisesRegex(ValueError, "escapes"):
                    replay_module.replay(output)
                path.unlink()
                path.write_bytes(original)


if __name__ == "__main__":
    unittest.main()

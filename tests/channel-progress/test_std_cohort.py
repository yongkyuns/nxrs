"""Refuse incomplete, substituted and mixed clean-image std primitive cohorts."""
from __future__ import annotations

import contextlib
import copy
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import test_analyze as channel_fixtures
import test_mutex_analyze as wire
import test_suite_binding as binding_fixtures

evidence = binding_fixtures.analyzer
with mock.patch.dict(sys.modules, {"analyze": evidence}):
    common = channel_fixtures.module("std_common_capture_tests", wire.HERE / "cohort.py")
with mock.patch.dict(sys.modules, {"analyze": evidence, "cohort": common, "mutex_analyze": wire.analyzer}):
    cohort = channel_fixtures.module("std_cohort_tests", wire.HERE / "std_cohort.py")


class StdCohortTests(unittest.TestCase):
    def setUp(self):
        self.binding = binding_fixtures.SuiteBindingTests()
        self.binding.setUp()
        self.addCleanup(self.binding.doCleanups)
        self.binding.prepare_clean_suite()
        self.build = self.binding.build
        self.build.receipt["source_dirty"] = False
        self.build.refresh()
        self.root = self.build.root / "cases"
        self.root.mkdir()
        self.physical = set()
        for mode in wire.CASES:
            run = self.run_directory(mode)
            run.mkdir(parents=True)
            for relative in self.build.receipt["artifacts_sha256"]:
                path = run / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(self.build.root / relative, path)
            self.write(mode, "build.json", self.build.receipt)
            self.refresh(mode)

    def run_directory(self, mode):
        return self.root / mode / "run"

    def read(self, mode, name):
        return evidence.read_json(self.run_directory(mode) / name)

    def write(self, mode, name, value):
        (self.run_directory(mode) / name).write_text(json.dumps(value, indent=2) + "\n")

    def refresh(self, mode):
        """Regenerate a standard case, analysis and acceptance for its receipt."""
        receipt = self.read(mode, "build.json")
        metadata, result = wire.fixture(mode)
        metadata["build_id"] = receipt["build_id"]
        console = "NuttShell (NSH) NuttX-13.1.0\r\n"
        console += f"nsh> \x1b[Kchannel_progress {mode}\r\n"
        console += "\r\n".join(evidence.PREFIX + json.dumps(row) for row in (metadata, result))
        console += "\r\nnsh> "
        (self.run_directory(mode) / "console.log").write_bytes(console.encode())
        physical = mode in self.physical
        session = dict(schema="channel-progress-session-v1", runtime_kind="physical" if physical else "qemu",
                       board="mps2-an521", build_id=receipt["build_id"], image_sha256=receipt["image_sha256"])
        if physical:
            session.update(measurement_session="operator-entry-" + mode, board_serial="operator-serial",
                           flash_verification="operator-entry", active_cores=1)
        self.write(mode, "session.json", session)
        bound = evidence.bind_build(evidence.analyze(console), receipt)
        self.write(mode, "acceptance.json", bound)
        summary = evidence.attach_execution(bound, session, fresh_boot_observed=True)
        self.write(mode, "analysis.json", summary)
        capture = dict(schema="channel-progress-capture-v1", capture_complete=True,
                       mode=mode, command="channel_progress " + mode, runtime_kind=session["runtime_kind"],
                       build_id=receipt["build_id"], image_sha256=receipt["image_sha256"],
                       one_command_per_boot=True, fresh_boot_observed=True,
                       boot_rom_digest_warning_observed=False, failure_stage=None, error=None,
                       outcome=summary["outcome"], progress_contract_satisfied=summary["progress_contract_satisfied"],
                       physical_timing_qualified=False, priority_progress_qualified=False)
        if not physical:
            capture.update(emulator_command=["qemu-system-arm", "-machine", "mps2-an521", "-kernel",
                                             str(self.build.root / receipt["elf_file"]), "-display", "none",
                                             "-serial", "stdio", "-monitor", "none", "-nic", "none", "-no-reboot"],
                           emulator_returncode_before_cleanup=None)
        self.write(mode, "capture.json", capture)

    def rebind(self, mode, **changes):
        """Reseal retained files, then regenerate the standard case and reports."""
        run = self.run_directory(mode)
        receipt = self.read(mode, "build.json")
        receipt.update(changes)
        for key in ("image", "elf", "config"):
            receipt[key + "_sha256"] = evidence.sha256(run / receipt[key + "_file"])
        receipt["artifacts_sha256"] = {
            relative: evidence.sha256(run / relative) for relative in receipt["artifacts_sha256"]
        }
        receipt["build_id"] = evidence.canonical({key: receipt[key]
                                                  for key in evidence.identity_fields(receipt["mode"])})
        self.write(mode, "build.json", receipt)
        self.refresh(mode)
        return receipt

    def test_all_seven_distinct_cases_share_one_clean_image_and_keep_busy_meaning(self):
        report = cohort.analyze_cohort(self.root)
        cohort.require_no_repair(report)
        self.assertEqual(report["schema"], "std-transfer-cohort-v1")
        self.assertEqual(report["scope"], "std-transfer-primitives")
        self.assertEqual(report["contract"], "std-prewarmed-single-item-v1")
        for flag in ("cohort_complete", "diagnostic_valid", "primitive_contract_satisfied",
                     "all_cases_completed_without_repair", "progress_contract_satisfied"):
            self.assertTrue(report[flag])
        self.assertEqual(report["required_modes"], list(wire.CASES))
        self.assertEqual(report["blocking_cases"], [])
        self.assertEqual({row["mode"] for row in report["cases"]}, set(wire.CASES))
        self.assertEqual(len({row["build_id"] for row in report["cases"]}), 1)
        self.assertEqual(len({row["image_sha256"] for row in report["cases"]}), 1)
        # A monotonic run identifier can legitimately repeat across fresh boots.
        self.assertEqual(len({row["run_id"] for row in report["cases"]}), 1)
        self.assertEqual(report["execution_evidence"], "virtual")
        for key in common.IDENTITY_FIELDS:
            self.assertEqual(report["matched_identity"][key], self.build.receipt[key])
        self.assertEqual(report["matched_identity"]["hook_stage"], 0)
        for field in cohort.QUALIFICATION_FLAGS:
            self.assertFalse(report[field])
        for case in report["cases"]:
            self.assertIn("acceptance.json", case["evidence_sha256"])
            if case["mode"].endswith("-held"):
                self.assertEqual(case["outcome"], "busy")
                self.assertEqual(case["observation"]["accepted"], 0)
                self.assertEqual(case["observation"]["transferred"], 0)

    def test_missing_and_extra_case_directories_are_refused_without_invalidating_individual_cases(self):
        foreign = self.root / "fifo-direct-published"
        foreign.mkdir()
        with self.assertRaisesRegex(ValueError, "exactly seven"):
            cohort.analyze_cohort(self.root)
        foreign.rmdir()
        shutil.rmtree(self.root / "condvar-close-wait")
        cohort.replay_case(self.run_directory("mutex-admit-held"), "mutex-admit-held")
        with self.assertRaisesRegex(ValueError, "exactly seven"):
            cohort.analyze_cohort(self.root)

    def test_valid_same_image_case_cannot_substitute_for_another_mode(self):
        requested, substituted = tuple(wire.CASES)[:2]
        shutil.rmtree(self.run_directory(requested))
        shutil.copytree(self.run_directory(substituted), self.run_directory(requested))
        with self.assertRaisesRegex(ValueError, "raw command differs"):
            cohort.analyze_cohort(self.root)
        path = self.run_directory(requested) / "console.log"
        path.write_bytes(path.read_bytes().replace(
            ("channel_progress " + substituted).encode(), ("channel_progress " + requested).encode(), 1))
        with self.assertRaisesRegex(ValueError, "named experiment family"):
            cohort.analyze_cohort(self.root)

    def test_linked_run_directory_is_refused(self):
        mode = "mutex-admit-held"
        original = self.run_directory(mode)
        parked = self.build.root / "parked-run"
        original.rename(parked)
        original.symlink_to(parked, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "linked case directory"):
            cohort.analyze_cohort(self.root)

    def test_individually_valid_different_build_and_full_receipt_are_refused(self):
        mode = "condvar-close-wait"
        original = self.read(mode, "build.json")
        for changes in ({"build_nonce": "a" * 32}, {"operator_note": "a different receipt"}):
            self.write(mode, "build.json", original)
            self.rebind(mode, **changes)
            cohort.replay_case(self.run_directory(mode), mode)
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "mixed std suite build"):
                cohort.analyze_cohort(self.root)

    def test_same_build_id_with_resealed_different_image_is_refused(self):
        mode = "condvar-close-wait"
        original = self.read(mode, "build.json")
        (self.run_directory(mode) / original["image_file"]).write_bytes(b"different synthetic linked image")
        changed = self.rebind(mode)
        self.assertEqual(original["build_id"], changed["build_id"])
        self.assertNotEqual(original["image_sha256"], changed["image_sha256"])
        cohort.replay_case(self.run_directory(mode), mode)
        with self.assertRaisesRegex(ValueError, "mixed std suite build/image"):
            cohort.analyze_cohort(self.root)

    def test_configuration_and_clean_source_must_match(self):
        mode = "condvar-close-wait"
        receipt = self.read(mode, "build.json")
        path = self.run_directory(mode) / receipt["config_file"]
        path.write_text(path.read_text() + "CONFIG_ANOTHER_DEBUG_SETTING=y\n")
        self.rebind(mode)
        cohort.replay_case(self.run_directory(mode), mode)
        with self.assertRaisesRegex(ValueError, "mixed std suite build"):
            cohort.analyze_cohort(self.root)
        self.rebind(mode, source_dirty=True)
        with self.assertRaisesRegex(ValueError, "clean frozen source"):
            cohort.analyze_cohort(self.root)

    def test_raw_boot_capture_and_primitive_observations_precede_stored_success(self):
        mode = "mutex-admit-held"
        path = self.run_directory(mode) / "console.log"
        original = path.read_bytes()
        for raw in (
            original.replace(b"NuttShell", b"stale transcript"),
            original.replace(("channel_progress " + mode).encode(), b"unrelated_command"),
            original.replace(b'"guard_held_at_return": true', b'"guard_held_at_return": false'),
            original.replace(b'"original_payload_retained": true', b'"original_payload_retained": false'),
            original.replace(b'"recovery_applied": false', b'"recovery_applied": true'),
            original[:-5], original + original, original + b"HardFault\n",
        ):
            path.write_bytes(raw)
            with self.subTest(raw=raw[-60:]), self.assertRaises(ValueError):
                cohort.analyze_cohort(self.root)
        path.write_bytes(original)

    def test_acceptance_is_required_and_compared_to_bound_unattached_replay(self):
        mode = "mutex-admit-held"
        path = self.run_directory(mode) / "acceptance.json"
        original = path.read_bytes()
        path.unlink()
        with self.assertRaises((ValueError, OSError)):
            cohort.analyze_cohort(self.root)
        path.write_bytes(original)
        self.assertEqual(self.read(mode, "acceptance.json")["runtime_kind"], "unbound")
        self.assertEqual(self.read(mode, "analysis.json")["runtime_kind"], "qemu")
        for forged in (self.read(mode, "analysis.json"),
                       dict(self.read(mode, "acceptance.json"), primitive_contract_satisfied=False),
                       dict(self.read(mode, "acceptance.json"), production_transport_qualified=True)):
            self.write(mode, "acceptance.json", forged)
            with self.assertRaisesRegex(ValueError, "stored std acceptance"):
                cohort.analyze_cohort(self.root)
        path.write_bytes(original)

    def test_stored_analysis_cannot_invent_scope_success_or_physical_qualification(self):
        mode = "mutex-admit-held"
        original = self.read(mode, "analysis.json")
        for field, value in (("scope", "production-progress"), ("physical_execution_verified", True),
                             ("production_transport_qualified", True), ("timing_qualified", True),
                             ("fixture_valid", 1), ("build_bound", False)):
            self.write(mode, "analysis.json", dict(original, **{field: value}))
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "stored std analysis"):
                cohort.analyze_cohort(self.root)
        self.write(mode, "analysis.json", original)

    def test_consistent_session_or_capture_claims_cannot_verify_physical_execution(self):
        mode = "mutex-admit-held"
        for name in ("session.json", "capture.json"):
            original = self.read(mode, name)
            self.write(mode, name, dict(original, physical_execution_verified=True))
            if name == "session.json":
                analysis = self.read(mode, "analysis.json")
                analysis["session"] = self.read(mode, name)
                self.write(mode, "analysis.json", analysis)
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "unsupported .*qualification"):
                cohort.analyze_cohort(self.root)
            self.refresh(mode)

    def test_capture_and_session_cannot_override_raw_case_or_image(self):
        mode = "mutex-admit-held"
        for name, changes in (
            ("capture.json", (("capture_complete", False), ("fresh_boot_observed", False),
                              ("mode", "mutex-admit-released"), ("image_sha256", "0" * 64),
                              ("error", "deadline"), ("serial_restore_error", "failed"),
                              ("emulator_returncode_before_cleanup", 1))),
            ("session.json", (("build_id", "0" * 64), ("image_sha256", "0" * 64), ("board", "other-board"))),
        ):
            original = self.read(mode, name)
            for field, value in changes:
                self.write(mode, name, dict(original, **{field: value}))
                with self.subTest(name=name, field=field), self.assertRaises(ValueError):
                    cohort.analyze_cohort(self.root)
            self.write(mode, name, original)

    def test_operator_sessions_remain_unverified_and_do_not_pass_physical_gate(self):
        self.physical.update(wire.CASES)
        for mode in wire.CASES:
            self.refresh(mode)
        report = cohort.analyze_cohort(self.root)
        cohort.require_no_repair(report)
        self.assertEqual(report["execution_evidence"], "operator-asserted")
        self.assertFalse(report["physical_execution_verified"])
        self.assertIn("not independently verified", report["physical_evidence_scope"])
        with self.assertRaisesRegex(ValueError, "physical gate refused"):
            cohort.require_physical(report)

    def test_mixed_execution_environments_are_refused(self):
        self.physical.add("condvar-close-wait")
        self.refresh("condvar-close-wait")
        with self.assertRaisesRegex(ValueError, "mixed std execution environment"):
            cohort.analyze_cohort(self.root)

    def test_different_operator_device_labels_cannot_share_one_environment(self):
        self.physical.update(wire.CASES)
        for mode in wire.CASES:
            self.refresh(mode)
        mode = "condvar-close-wait"
        session = self.read(mode, "session.json")
        session["board_serial"] = "other-operator-serial"
        self.write(mode, "session.json", session)
        summary = self.read(mode, "analysis.json")
        summary["session"] = session
        self.write(mode, "analysis.json", summary)
        cohort.replay_case(self.run_directory(mode), mode)
        with self.assertRaisesRegex(ValueError, "mixed std execution environment"):
            cohort.analyze_cohort(self.root)

    def test_retained_cases_replay_without_original_build_tree(self):
        original = cohort.analyze_cohort(self.root)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        relocated = Path(temporary.name) / "archive"
        shutil.copytree(self.root, relocated)
        for name in ("crossbeam", "prepared", "firmware"):
            shutil.rmtree(self.build.root / name)
        self.assertEqual(cohort.analyze_cohort(relocated), original)

    def test_cli_saves_valid_diagnosis_before_gate_refusal_but_not_invalid_evidence(self):
        output = self.root / "cohort.json"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cohort.main([str(self.root), "--output", str(output)]), 0)
            self.assertEqual(cohort.main([str(self.root), "--output", str(output), "--require-no-repair"]), 0)
            self.assertEqual(cohort.main([str(self.root), "--output", str(output), "--require-physical"]), 2)
        report = evidence.read_json(output)
        self.assertTrue(report["primitive_contract_satisfied"])
        self.assertFalse(report["physical_execution_verified"])
        shutil.rmtree(self.root / "condvar-close-wait")
        missing = self.root / "invalid.json"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cohort.main([str(self.root), "--output", str(missing), "--require-no-repair"]), 1)
        self.assertFalse(missing.exists())

    def test_no_repair_gate_requires_the_complete_named_primitive_scope(self):
        original = cohort.analyze_cohort(self.root)
        for field, value in (("schema", "channel-progress-cohort-v1"), ("scope", "production-progress"),
                             ("contract", "arbitrary-method-progress"), ("cohort_complete", False),
                             ("diagnostic_valid", False), ("primitive_contract_satisfied", False),
                             ("primitive_contract_satisfied", 1), ("progress_contract_satisfied", False),
                             ("all_cases_completed_without_repair", False),
                             ("required_modes", list(wire.CASES)[:3])):
            summary = copy.deepcopy(original)
            summary[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "std no-repair gate refused"):
                cohort.require_no_repair(summary)


if __name__ == "__main__":
    unittest.main()

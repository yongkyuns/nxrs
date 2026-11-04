"""Refuse incomplete, relabelled and mixed one-image FIFO evidence cohorts."""
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
import test_fifo_analyze as wire
import test_fifo_binding as binding_fixtures

evidence = binding_fixtures.analyzer
with mock.patch.dict(sys.modules, {"analyze": evidence}):
    common = channel_fixtures.module("fifo_common_capture_tests", wire.HERE / "cohort.py")
with mock.patch.dict(sys.modules, {"analyze": evidence, "cohort": common, "fifo_analyze": wire.analyzer}):
    cohort = channel_fixtures.module("fifo_cohort_tests", wire.HERE / "fifo_cohort.py")


class FifoCohortTests(unittest.TestCase):
    def setUp(self):
        self.binding = binding_fixtures.FifoBindingTests()
        self.binding.setUp()
        self.addCleanup(self.binding.doCleanups)
        self.binding.prepare_fifo_suite()
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
            # All six captures really retain the same frozen suite receipt.
            self.write(mode, "build.json", self.build.receipt)
            self.write_run(mode)

    def run_directory(self, mode):
        return self.root / mode / "run"

    def read(self, mode, name):
        return evidence.read_json(self.run_directory(mode) / name)

    def write(self, mode, name, value):
        (self.run_directory(mode) / name).write_text(json.dumps(value, indent=2) + "\n")

    def write_run(self, mode):
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
        summary = evidence.bind_build(evidence.analyze(console), receipt)
        summary = evidence.attach_execution(summary, session, fresh_boot_observed=True)
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

    def reseal_case(self, mode, **changes):
        run = self.run_directory(mode)
        receipt = self.read(mode, "build.json")
        receipt.update(changes)
        for key in ("image", "elf", "config"):
            receipt[key + "_sha256"] = evidence.sha256(run / receipt[key + "_file"])
        receipt["artifacts_sha256"] = {
            relative: evidence.sha256(run / relative) for relative in receipt["artifacts_sha256"]
        }
        receipt["build_id"] = evidence.canonical({key: receipt[key]
                                                  for key in evidence.identity_fields(receipt["mode"], receipt)})
        self.write(mode, "build.json", receipt)
        self.write_run(mode)
        return receipt

    def test_exact_six_distinct_modes_share_one_image_and_admit_only_the_candidate_scope(self):
        report = cohort.analyze_cohort(self.root)
        cohort.require_no_repair(report)
        self.assertEqual(report["scope"], "equal-fifo-reservation-progress")
        self.assertEqual(report["contract"], "equal-fifo-run-to-block-v1")
        self.assertTrue(report["candidate_contract_satisfied"])
        self.assertTrue(report["cohort_complete"])
        self.assertTrue(report["diagnostic_valid"])
        self.assertEqual(report["required_modes"], list(wire.CASES))
        self.assertEqual(report["blocking_cases"], [])
        self.assertEqual({row["mode"] for row in report["cases"]}, set(wire.CASES))
        self.assertEqual(len({row["build_id"] for row in report["cases"]}), 1)
        self.assertEqual(len({row["image_sha256"] for row in report["cases"]}), 1)
        # A monotonic run_id can repeat after a fresh boot of the same image.
        self.assertEqual(len({row["run_id"] for row in report["cases"]}), 1)
        self.assertEqual(report["execution_evidence"], "virtual")
        self.assertEqual(report["matched_identity"]["fifo_kernel_source_sha256"], self.binding.kernel_pins)
        self.assertNotIn("boot_image_profile", report["matched_identity"])
        for field in ("physical_execution_verified", "physical_progress_qualified", "priority_progress_qualified",
                      "production_transport_qualified", "physical_timing_qualified", "timing_qualified",
                      "hard_realtime_bound"):
            self.assertFalse(report[field])

    def test_explicit_profile_survives_fifo_aggregation_without_changing_legacy_shape(self):
        original = cohort.analyze_cohort(self.root)
        for mode in wire.CASES:
            self.reseal_case(mode, boot_image_profile="platform-default")
        report = cohort.analyze_cohort(self.root)
        cohort.require_no_repair(report)
        self.assertNotIn("boot_image_profile", original["matched_identity"])
        self.assertEqual(report["matched_identity"]["boot_image_profile"], "platform-default")
        self.assertEqual(report["matched_identity"]["fifo_kernel_source_sha256"], self.binding.kernel_pins)
        self.assertEqual(report["matched_identity_sha256"], evidence.canonical(report["matched_identity"]))
        self.assertEqual(report["required_modes"], original["required_modes"])

    def test_published_controls_alone_and_foreign_case_directories_are_refused(self):
        foreign = self.root / "std-mutex-suite"
        foreign.mkdir()
        with self.assertRaisesRegex(ValueError, "exactly six"):
            cohort.analyze_cohort(self.root)
        foreign.rmdir()
        for mode in wire.CASES:
            if mode.endswith("reserved"):
                shutil.rmtree(self.root / mode)
        with self.assertRaisesRegex(ValueError, "exactly six"):
            cohort.analyze_cohort(self.root)

    def test_valid_same_image_case_cannot_be_copied_into_another_mode(self):
        requested, substituted = wire.CASES[:2]
        shutil.rmtree(self.run_directory(requested))
        shutil.copytree(self.run_directory(substituted), self.run_directory(requested))
        with self.assertRaisesRegex(ValueError, "raw command differs"):
            cohort.analyze_cohort(self.root)
        path = self.run_directory(requested) / "console.log"
        path.write_bytes(path.read_bytes().replace(
            ("channel_progress " + substituted).encode(), ("channel_progress " + requested).encode(), 1))
        with self.assertRaisesRegex(ValueError, "named experiment family"):
            cohort.analyze_cohort(self.root)

    def test_linked_run_directory_is_refused_even_when_its_contents_would_validate(self):
        mode = wire.CASES[0]
        original = self.run_directory(mode)
        parked = self.build.root / "parked-run"
        original.rename(parked)
        original.symlink_to(parked, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "linked case directory"):
            cohort.analyze_cohort(self.root)

    def test_individually_valid_but_different_build_identity_is_refused(self):
        mode = wire.CASES[-1]
        self.reseal_case(mode, build_nonce="a" * 32)
        # This is a valid separate build; it cannot silently join the one-image suite.
        cohort.replay_case(self.run_directory(mode), mode)
        with self.assertRaisesRegex(ValueError, "mixed FIFO suite build"):
            cohort.analyze_cohort(self.root)

    def test_same_build_id_with_resealed_different_image_is_refused(self):
        mode = wire.CASES[-1]
        old = self.read(mode, "build.json")
        (self.run_directory(mode) / old["image_file"]).write_bytes(b"different synthetic linked image")
        new = self.reseal_case(mode)
        self.assertEqual(old["build_id"], new["build_id"])
        self.assertNotEqual(old["image_sha256"], new["image_sha256"])
        cohort.replay_case(self.run_directory(mode), mode)
        with self.assertRaisesRegex(ValueError, "mixed FIFO suite build/image"):
            cohort.analyze_cohort(self.root)

    def test_actual_configuration_and_complete_receipt_must_match(self):
        mode = wire.CASES[-1]
        receipt = self.read(mode, "build.json")
        path = self.run_directory(mode) / receipt["config_file"]
        path.write_text(path.read_text() + "CONFIG_ANOTHER_DEBUG_SETTING=y\n")
        self.reseal_case(mode)
        cohort.replay_case(self.run_directory(mode), mode)
        with self.assertRaisesRegex(ValueError, "mixed FIFO suite build"):
            cohort.analyze_cohort(self.root)

    def test_actual_kernel_archive_image_and_std_evidence_remain_required(self):
        mode = wire.CASES[0]
        paths = (
            "prepared/fifo-kernel/" + next(iter(self.binding.kernel_pins)),
            "crossbeam/original/crossbeam-channel-0.5.17.crate",
            "firmware/image", "prepared/std-library-sources.json",
        )
        for name in paths:
            path = self.run_directory(mode) / name
            original = path.read_bytes()
            path.write_bytes(original + b"mutation")
            with self.subTest(name=name), self.assertRaises(ValueError):
                cohort.analyze_cohort(self.root)
            path.write_bytes(original)

    def test_dirty_inputs_cannot_claim_a_frozen_cohort(self):
        mode = wire.CASES[0]
        self.reseal_case(mode, source_dirty=True)
        with self.assertRaisesRegex(ValueError, "clean frozen source"):
            cohort.analyze_cohort(self.root)

    def test_raw_console_and_native_proof_are_replayed_before_stored_success(self):
        mode = wire.CASES[0]
        path = self.run_directory(mode) / "console.log"
        original = path.read_bytes()
        for raw in (
            original.replace(b"NuttShell", b"stale transcript"),
            original.replace(("channel_progress " + mode).encode(), b"unrelated_command"),
            original.replace(b'"holder_commit_count_at_ready": 0', b'"holder_commit_count_at_ready": 1'),
            original.replace(b'"peer_ready_state": "ready"', b'"peer_ready_state": "wait_sem"'),
            original.replace(b'"recovery_applied": false', b'"recovery_applied": true'),
            original.replace(b'"schema": "fifo-channel-progress-v1"', b'"schema": "channel-progress-v1"'),
            original[:-5], original + original, original + b"HardFault\n",
        ):
            path.write_bytes(raw)
            with self.subTest(raw=raw[-60:]), self.assertRaises(ValueError):
                cohort.analyze_cohort(self.root)
        path.write_bytes(original)

    def test_capture_and_session_records_cannot_override_raw_case_or_image(self):
        mode = wire.CASES[0]
        for name, changes in (
            ("capture.json", (("capture_complete", False), ("fresh_boot_observed", False),
                              ("one_command_per_boot", False), ("mode", wire.CASES[1]),
                              ("command", "channel_progress " + wire.CASES[1]),
                              ("build_id", "0" * 64), ("image_sha256", "0" * 64),
                              ("runtime_kind", "physical"), ("error", "deadline"),
                              ("progress_contract_satisfied", False), ("serial_restore_error", "failed"),
                              ("emulator_returncode_before_cleanup", 1))),
            ("session.json", (("build_id", "0" * 64), ("image_sha256", "0" * 64), ("board", "other-board"))),
        ):
            original = self.read(mode, name)
            for field, value in changes:
                self.write(mode, name, dict(original, **{field: value}))
                with self.subTest(name=name, field=field), self.assertRaises(ValueError):
                    cohort.analyze_cohort(self.root)
            self.write(mode, name, original)

    def test_stored_analysis_cannot_add_cohort_production_or_physical_claims(self):
        mode = wire.CASES[0]
        original = self.read(mode, "analysis.json")
        for field, value in (("scope", "production-progress"), ("candidate_contract_satisfied", True),
                             ("physical_execution_verified", True), ("production_transport_qualified", True),
                             ("timing_qualified", True), ("fixture_valid", 1), ("build_bound", False)):
            self.write(mode, "analysis.json", dict(original, **{field: value}))
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "stored FIFO analysis"):
                cohort.analyze_cohort(self.root)
        self.write(mode, "analysis.json", original)

    def test_operator_assertions_can_describe_execution_but_never_verify_physical_hardware(self):
        self.physical.update(wire.CASES)
        for mode in wire.CASES:
            self.write_run(mode)
        report = cohort.analyze_cohort(self.root)
        cohort.require_no_repair(report)
        self.assertTrue(report["candidate_contract_satisfied"])
        self.assertEqual(report["execution_evidence"], "operator-asserted")
        self.assertFalse(report["physical_execution_verified"])
        self.assertIn("not independently verified", report["physical_evidence_scope"])
        with self.assertRaisesRegex(ValueError, "physical gate refused"):
            cohort.require_physical(report)
        stored = self.read(wire.CASES[0], "analysis.json")
        stored["physical_execution_verified"] = True
        self.write(wire.CASES[0], "analysis.json", stored)
        with self.assertRaisesRegex(ValueError, "stored FIFO analysis"):
            cohort.analyze_cohort(self.root)

    def test_mixed_execution_environments_cannot_be_aggregated(self):
        self.physical.add(wire.CASES[-1])
        self.write_run(wire.CASES[-1])
        with self.assertRaisesRegex(ValueError, "mixed FIFO execution environment"):
            cohort.analyze_cohort(self.root)

    def test_retained_cohort_replays_after_original_build_and_sources_are_pruned(self):
        original = cohort.analyze_cohort(self.root)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        relocated = Path(temporary.name) / "archive"
        shutil.copytree(self.root, relocated)
        for name in ("crossbeam", "prepared", "firmware", "synthetic-kernel"):
            shutil.rmtree(self.build.root / name)
        self.assertEqual(cohort.analyze_cohort(relocated), original)

    def test_cli_saves_candidate_before_physical_refusal_and_refuses_incomplete_cohort(self):
        output = self.root / "cohort.json"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cohort.main([str(self.root), "--output", str(output), "--require-no-repair"]), 0)
            self.assertEqual(cohort.main([str(self.root), "--output", str(output), "--require-physical"]), 2)
        report = evidence.read_json(output)
        self.assertTrue(report["candidate_contract_satisfied"])
        self.assertFalse(report["physical_execution_verified"])
        shutil.rmtree(self.root / wire.CASES[0])
        missing = self.root / "invalid.json"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cohort.main([str(self.root), "--output", str(missing), "--require-no-repair"]), 1)
        self.assertFalse(missing.exists())

    def test_no_repair_gate_cannot_admit_an_incomplete_or_different_scope_summary(self):
        original = cohort.analyze_cohort(self.root)
        for field, value in (("schema", "channel-progress-cohort-v1"), ("scope", "production-progress"),
                             ("contract", "arbitrary-fifo"), ("cohort_complete", False),
                             ("diagnostic_valid", False), ("candidate_contract_satisfied", False),
                             ("candidate_contract_satisfied", 1), ("all_cases_completed_without_repair", False),
                             ("required_modes", list(wire.CASES[:3]))):
            summary = copy.deepcopy(original)
            summary[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "FIFO no-repair gate refused"):
                cohort.require_no_repair(summary)


if __name__ == "__main__":
    unittest.main()

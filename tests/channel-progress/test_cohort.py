"""Adversarial controls for whole-cohort admission and hardware-label claims."""
from __future__ import annotations

import contextlib
import copy
import io
import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest import mock

import test_analyze as fixtures

with mock.patch.dict(sys.modules, {"analyze": fixtures.analyze}):
    cohort = fixtures.module("channel_progress_cohort_tests", fixtures.HERE / "cohort.py")

evidence = fixtures.analyze


class CohortTests(unittest.TestCase):
    def setUp(self):
        # Reuse the source validator's synthetic, test-scoped archive pins. All
        # six builds still undergo actual archive reconstruction and hash checks.
        self.build = fixtures.BuildEvidenceTests()
        self.build.setUp()
        self.addCleanup(self.build.doCleanups)
        self.root = self.build.root / "cohort"
        self.root.mkdir()
        self.receipts = {}
        self.stalled = {}
        self.physical = set()
        for mode in cohort.MODES:
            run = self.run_directory(mode)
            run.mkdir(parents=True)
            for name in ("firmware", "prepared"):
                shutil.copytree(self.build.root / name, run / name)
            source = self.build.prep.prepare(
                self.build.root / "crossbeam-channel.crate",
                self.build.root / "crossbeam-utils.crate", run / "crossbeam",
                evidence.SOURCE_MODES[evidence.MODES[mode]])
            cargo = self.read(mode, "firmware/crossbeam-source.json")
            for name, item in cargo["sources"].items():
                src = run / "crossbeam/sources" / f"{name}-{evidence.PINS[name][0]}/src/lib.rs"
                item.update(src_path=str(src), expected_src_path=str(src),
                            package_id=f"path+{src.parent}#{name}@{evidence.PINS[name][0]}")
            self.write(mode, "firmware/crossbeam-source.json", cargo)
            std_root = run / "prepared/toolchain"
            std_source = str(std_root / "lib/rustlib/src/rust/library/std/src/lib.rs")
            self.write(mode, "firmware/std-source.json", dict(
                std_source=std_source, expected_source=std_source,
                selected_source_matches=True, rustc=str(std_root / "bin/rustc")))
            receipt = copy.deepcopy(self.build.receipt)
            receipt.update(mode=mode, hook_stage=evidence.MODES[mode], source_dirty=False,
                           build_nonce=evidence.canonical(mode)[:32], crossbeam_receipt=source)
            self.receipts[mode] = receipt
            self.stalled[mode] = mode.endswith("reserved")
            (run / receipt["image_file"]).write_bytes(b"synthetic firmware " + mode.encode())
            self.write_run(mode)

    def run_directory(self, mode):
        return self.root / mode / "run"

    def read(self, mode, name):
        return evidence.read_json(self.run_directory(mode) / name)

    def write(self, mode, name, value):
        (self.run_directory(mode) / name).write_text(json.dumps(value, indent=2) + "\n")

    def write_run(self, mode):
        run, receipt = self.run_directory(mode), self.receipts[mode]
        for key in ("image", "elf", "config"):
            receipt[key + "_sha256"] = evidence.sha256(run / receipt[key + "_file"])
        receipt["artifacts_sha256"] = {
            path.relative_to(run).as_posix(): evidence.sha256(path)
            for parent in ("crossbeam", "firmware", "prepared")
            for path in (run / parent).rglob("*")
            if path.is_file() and "/sources/" not in str(path)
        }
        receipt["build_id"] = evidence.canonical({
            key: receipt[key] for key in evidence.identity_fields(receipt["mode"], receipt)})
        self.write(mode, "build.json", receipt)
        metadata, result = fixtures.fixture(mode, stalled=self.stalled[mode])
        metadata["build_id"] = receipt["build_id"]
        # Real NSH echoes the command, with ANSI clear-to-end-of-line. Fresh
        # emulated boots can legitimately reuse the same monotonic run_id.
        console = "NuttShell (NSH) NuttX-12.10.0\r\n"
        console += f"nsh> \x1b[Kchannel_progress {mode}\r\n"
        console += "\r\n".join(evidence.PREFIX + json.dumps(row) for row in (metadata, result))
        console += "\r\nnsh> "
        (run / "console.log").write_bytes(console.encode())
        physical = mode in self.physical
        session = dict(schema="channel-progress-session-v1", runtime_kind="physical" if physical else "qemu",
                       build_id=receipt["build_id"], image_sha256=receipt["image_sha256"], board="mps2-an521")
        if physical:
            session.update(measurement_session="operator-entry", board_serial="operator-serial",
                           flash_verification="operator-entry", active_cores=1)
        self.write(mode, "session.json", session)
        analysis = evidence.bind_build(evidence.analyze(console), receipt)
        analysis = evidence.attach_execution(analysis, session, fresh_boot_observed=True)
        self.write(mode, "analysis.json", analysis)
        capture = dict(schema="channel-progress-capture-v1", capture_complete=True,
                       runtime_kind=session["runtime_kind"], mode=mode, command=f"channel_progress {mode}",
                       build_id=receipt["build_id"], image_sha256=receipt["image_sha256"],
                       one_command_per_boot=True, physical_timing_qualified=False,
                       priority_progress_qualified=False, failure_stage=None, error=None,
                       fresh_boot_observed=True, boot_rom_digest_warning_observed=False,
                       outcome=analysis["outcome"], progress_contract_satisfied=analysis["progress_contract_satisfied"])
        if not physical:
            capture.update(emulator_command=["qemu-system-arm", "-machine", "mps2-an521", "-kernel",
                                             str(run / receipt["elf_file"]), "-display", "none", "-serial",
                                             "stdio", "-monitor", "none", "-nic", "none", "-no-reboot"],
                           emulator_returncode_before_cleanup=None)
        self.write(mode, "capture.json", capture)

    def test_complete_negative_diagnosis_is_distinct_from_no_repair_acceptance(self):
        report = cohort.analyze_cohort(self.root)
        self.assertTrue(report["cohort_complete"])
        self.assertTrue(report["diagnostic_valid"])
        self.assertEqual(report["blocking_cases"], ["direct-reserved", "select-reserved", "send-reserved"])
        self.assertTrue(report["controls_completed_without_repair"])
        self.assertFalse(report["progress_contract_satisfied"])
        self.assertEqual(report["execution_evidence"], "virtual")
        self.assertEqual(len(report["cases"]), 6)
        self.assertNotIn("boot_image_profile", report["matched_identity"])
        self.assertEqual(len({row["run_id"] for row in report["cases"]}), 1)
        with self.assertRaisesRegex(ValueError, "no-repair gate refused"):
            cohort.require_no_repair(report)

    def test_native_faults_in_otherwise_complete_captures_are_refused_in_any_case(self):
        mode = "direct-published"
        raw = (self.run_directory(mode) / "console.log").read_bytes()
        for fault in fixtures.FAULT_LINES:
            for variant in (fault, fault.lower(), fault.upper()):
                for broken in (variant.encode() + raw, raw + variant.encode()):
                    with self.subTest(fault=variant), self.assertRaisesRegex(ValueError, "fault in raw capture"):
                        cohort.capture_structure(broken, mode)

    def test_complete_final_prompt_allows_only_its_known_eof_display_prefixes(self):
        for mode in ("direct-reserved", "select-published"):
            path = self.run_directory(mode) / "console.log"
            original = path.read_bytes()
            self.assertTrue(original.endswith(b"nsh> "))
            for decoration in (b"", b"\x1b", b"\x1b[", b"\x1b[K"):
                raw = original + decoration
                path.write_bytes(raw)
                with self.subTest(mode=mode, decoration=decoration):
                    case, _ = cohort.replay_case(path.parent, mode)
                    self.assertEqual(case["mode"], mode)
                    self.assertEqual(path.read_bytes(), raw)
                    self.assertEqual(case["evidence_sha256"]["console.log"], evidence.sha256(path))
            path.write_bytes(original)

    def test_partial_controls_unknown_suffixes_and_additional_commands_remain_invalid(self):
        mode = "direct-published"
        raw = (self.run_directory(mode) / "console.log").read_bytes()
        broken = [raw + tail for tail in (
            b"\x1b[0", b"\x1b]", b"\x1b[\n", b"\x1b[ ", b"\x1b\n", b"\x1b\x1b[",
            b"\x1b[\r", b"\x1b\r[", b"\x1b\r",
            b"another_command\r\nnsh> \x1b[", b"\r\nNuttShell\r\nnsh> \x1b[",
            b"\x1b[\nAssertion failed panic\n", b"unexpected text",
        )]
        broken += [
            raw[:-1] + b"\x1b[",  # Missing the completed literal prompt's space.
            raw[:-1] + b"\r \x1b[",  # CR normalization cannot fabricate that prompt.
            b"\x1b[\n" + raw,
            raw.replace(b"channel_progress", b"\x1b[\nchannel_progress", 1),
            raw.replace(b"CHANNEL_PROGRESS", b"\x1b\nCHANNEL_PROGRESS", 1),
            raw.replace(b'"record": "result"', b'"record": \x1b[\n"result"', 1),
            raw.replace(b'"record": "result"', b'"record": "result"\x1b\n', 1),
            raw.replace(b"CHANNEL_PROGRESS", b"nsh> second_command\r\nCHANNEL_PROGRESS", 1) + b"\x1b[",
        ]
        for payload in broken:
            with self.subTest(payload=payload[-100:]), self.assertRaises(ValueError):
                cohort.capture_structure(payload, mode)

    def test_six_no_repair_cases_pass_only_the_scoped_experiment_gate(self):
        for mode in cohort.MODES:
            self.stalled[mode] = False
            self.write_run(mode)
        report = cohort.analyze_cohort(self.root)
        cohort.require_no_repair(report)
        self.assertEqual(report["blocking_cases"], [])
        for field in ("physical_execution_verified", "physical_progress_qualified", "priority_progress_qualified",
                      "physical_timing_qualified", "timing_qualified", "hard_realtime_bound"):
            self.assertFalse(report[field])
        with self.assertRaisesRegex(ValueError, "physical gate refused"):
            cohort.require_physical(report)

    def test_controls_alone_extra_family_and_linked_captures_are_refused(self):
        extra = self.root / "std-mutex-suite"
        extra.mkdir()
        with self.assertRaisesRegex(ValueError, "exactly six"):
            cohort.analyze_cohort(self.root)
        extra.rmdir()
        original = self.root / "direct-reserved"
        parked = self.build.root / "parked"
        original.rename(parked)
        with self.assertRaisesRegex(ValueError, "missing=.*direct-reserved"):
            cohort.analyze_cohort(self.root)
        original.symlink_to(self.root / "direct-published", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "linked case"):
            cohort.analyze_cohort(self.root)
        original.unlink()
        parked.rename(original)
        for mode in cohort.MODES:
            if mode not in cohort.CONTROLS:
                shutil.rmtree(self.root / mode)
        with self.assertRaisesRegex(ValueError, "exactly six"):
            cohort.analyze_cohort(self.root)

    def test_a_valid_case_cannot_be_substituted_for_another_required_case(self):
        shutil.rmtree(self.run_directory("direct-reserved"))
        shutil.copytree(self.run_directory("direct-published"), self.run_directory("direct-reserved"))
        with self.assertRaisesRegex(ValueError, "raw command differs"):
            cohort.analyze_cohort(self.root)

    def test_different_primitive_schema_cannot_enter_the_channel_cohort(self):
        path = self.run_directory("direct-reserved") / "console.log"
        path.write_bytes(path.read_bytes().replace(b'"schema": "channel-progress-v1"',
                                                  b'"schema": "std-transfer-progress-v1"'))
        with self.assertRaises(ValueError):
            cohort.analyze_cohort(self.root)

    def test_otherwise_valid_builds_must_share_frozen_inputs(self):
        mode = "direct-published"
        original = copy.deepcopy(self.receipts[mode])
        changes = (("source_revision", "9" * 40), ("source_sha256", {"different.rs": "9" * 64}),
                   ("staged_inputs_sha256", {"different.rs": "9" * 64}),
                   ("kernel_revision", "9" * 40), ("apps_revision", "9" * 40),
                   ("sdk", "another-compiler-bundle"),
                   ("resolved_configuration", dict(original["resolved_configuration"], CONFIG_DEBUG_FEATURES="y")))
        for field, value in changes:
            with self.subTest(field=field):
                self.receipts[mode] = copy.deepcopy(original)
                self.receipts[mode][field] = value
                self.write_run(mode)
                evidence.build_receipt(self.run_directory(mode) / "build.json", mode)
                with self.assertRaisesRegex(ValueError, "mixed cohort.*" + field):
                    cohort.analyze_cohort(self.root)
        self.receipts[mode] = original
        self.write_run(mode)

    def test_explicit_boot_profile_is_retained_and_cannot_mix_with_legacy_identity(self):
        original = cohort.analyze_cohort(self.root)
        self.assertNotIn("boot_image_profile", original["matched_identity"])
        mode = "direct-published"
        self.receipts[mode]["boot_image_profile"] = "platform-default"
        self.write_run(mode)
        evidence.build_receipt(self.run_directory(mode) / "build.json", mode)
        with self.assertRaisesRegex(ValueError, "mixed cohort.*boot_image_profile"):
            cohort.analyze_cohort(self.root)
        for mode in cohort.MODES:
            self.receipts[mode]["boot_image_profile"] = "platform-default"
            self.write_run(mode)
        report = cohort.analyze_cohort(self.root)
        self.assertEqual(report["matched_identity"], {
            **original["matched_identity"], "boot_image_profile": "platform-default"})
        self.assertEqual(report["matched_identity_sha256"], evidence.canonical(report["matched_identity"]))
        self.assertEqual(report["blocking_cases"], original["blocking_cases"])

    def test_actual_configuration_bytes_must_match_even_outside_selected_config_fields(self):
        mode = "direct-published"
        path = self.run_directory(mode) / self.receipts[mode]["config_file"]
        path.write_text(path.read_text() + "CONFIG_DEBUG_EXTRA=y\n")
        self.write_run(mode)
        evidence.build_receipt(self.run_directory(mode) / "build.json", mode)
        with self.assertRaisesRegex(ValueError, "mixed cohort.*config_sha256"):
            cohort.analyze_cohort(self.root)

    def test_compiler_and_std_identity_are_compared_across_valid_builds(self):
        mode = "direct-published"
        receipt = self.receipts[mode]
        hook = self.read(mode, "prepared/std-channel-hook.json")
        library = self.read(mode, "prepared/std-library-sources.json")
        library["std/src/lib.rs"] = "7" * 64
        hook["library_tree_sha256"] = receipt["std_library_tree_sha256"] = evidence.canonical(library)
        hook["compiler"]["version"] = "different synthetic compiler"
        receipt["compiler_identity"] = copy.deepcopy(hook["compiler"])
        self.write(mode, "prepared/std-channel-hook.json", hook)
        self.write(mode, "prepared/std-library-sources.json", library)
        self.write(mode, "firmware/std-patch.json", dict(channel_qualification=hook))
        self.write_run(mode)
        evidence.build_receipt(self.run_directory(mode) / "build.json", mode)
        with self.assertRaisesRegex(ValueError, "mixed cohort.*compiler_identity.*std_library_tree_sha256"):
            cohort.analyze_cohort(self.root)

    def test_dirty_sources_mutated_images_and_mutated_source_archives_are_refused(self):
        mode = "direct-reserved"
        self.receipts[mode]["source_dirty"] = True
        self.write_run(mode)
        with self.assertRaisesRegex(ValueError, "clean frozen source"):
            cohort.analyze_cohort(self.root)
        self.receipts[mode]["source_dirty"] = False
        self.write_run(mode)
        for name in ("firmware/image", "crossbeam/original/crossbeam-channel-0.5.17.crate"):
            path = self.run_directory(mode) / name
            original = path.read_bytes()
            path.write_bytes(original + b"unrecorded mutation")
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "digest mismatch"):
                cohort.analyze_cohort(self.root)
            path.write_bytes(original)

    def test_capture_completion_identity_and_qualification_fields_are_checked(self):
        mode = "direct-reserved"
        original = self.read(mode, "capture.json")
        changes = (("capture_complete", False), ("one_command_per_boot", False),
                   ("fresh_boot_observed", False), ("error", "console timeout"),
                   ("failure_stage", "analysis"), ("serial_restore_error", "restore failed"),
                   ("build_id", "0" * 64), ("image_sha256", "0" * 64),
                   ("command", "channel_progress direct-published"), ("mode", "direct-published"),
                   ("runtime_kind", "physical"), ("outcome", "completed-without-repair"),
                   ("progress_contract_satisfied", True), ("boot_rom_digest_warning_observed", True),
                   ("priority_progress_qualified", True), ("physical_execution_verified", True),
                   ("timing_qualified", True), ("emulator_returncode_before_cleanup", 1),
                   ("emulator_command", ["qemu-system-arm", "-machine", "wrong", "-kernel", "wrong"]))
        for field, value in changes:
            row = dict(original, **{field: value})
            self.write(mode, "capture.json", row)
            with self.subTest(field=field), self.assertRaises(ValueError):
                cohort.analyze_cohort(self.root)
        self.write(mode, "capture.json", original)

    def test_raw_boot_command_record_and_returned_prompt_cannot_be_replaced_by_labels(self):
        path = self.run_directory("direct-reserved") / "console.log"
        original = path.read_bytes()
        changes = (
            original.replace(b"NuttShell", b"old-output"),
            original.replace(b"channel_progress direct-reserved", b"channel_progress direct-published"),
            original.replace(b"nsh> \x1b[Kchannel_progress direct-reserved\r\n", b""),
            original.replace(b"CHANNEL_PROGRESS", b"nsh> extra-command\nCHANNEL_PROGRESS", 1),
            original.replace(b'"record": "result"', b'"record": "metadata"'),
            original[:-5], original + b"HardFault\n", original + original,
            original.replace(b"NuttShell", b"NuttShell\nNuttShell"),
        )
        for raw in changes:
            path.write_bytes(raw)
            with self.subTest(raw=raw[-60:]), self.assertRaises(ValueError):
                cohort.analyze_cohort(self.root)
        path.write_bytes(original)

    def test_session_and_stored_analysis_are_replayed_instead_of_trusted(self):
        mode = "direct-reserved"
        for name, changes in (
            ("session.json", (("build_id", "0" * 64), ("image_sha256", "0" * 64), ("board", "different-board"))),
            ("analysis.json", (("progress_contract_satisfied", True), ("fixture_valid", 1),
                               ("completed_without_repair", True), ("physical_execution_verified", True),
                               ("priority_progress_qualified", True), ("build_bound", False))),
        ):
            original = self.read(mode, name)
            for field, value in changes:
                self.write(mode, name, dict(original, **{field: value}))
                with self.subTest(name=name, field=field), self.assertRaises(ValueError):
                    cohort.analyze_cohort(self.root)
            self.write(mode, name, original)

    def test_historical_physical_claims_remain_readable_but_cannot_qualify_hardware(self):
        self.physical.update(cohort.MODES)
        for mode in cohort.MODES:
            self.write_run(mode)
            stored = self.read(mode, "analysis.json")
            stored["physical_execution_verified"] = True
            del stored["execution_evidence"]
            del stored["physical_evidence_scope"]
            self.write(mode, "analysis.json", stored)
        report = cohort.analyze_cohort(self.root)
        self.assertEqual(report["execution_evidence"], "operator-asserted")
        self.assertFalse(report["physical_execution_verified"])
        self.assertTrue(all(case["legacy_operator_physical_claim_ignored"] for case in report["cases"]))
        self.assertIn("not independently verified", report["physical_evidence_scope"])
        with self.assertRaisesRegex(ValueError, "physical gate refused"):
            cohort.require_physical(report)

    def test_virtual_and_operator_labelled_sessions_cannot_form_one_cohort(self):
        self.physical.add("direct-published")
        self.write_run("direct-published")
        with self.assertRaisesRegex(ValueError, "mixed cohort.*runtime_kind"):
            cohort.analyze_cohort(self.root)

    def test_cli_retains_valid_negative_report_before_enforcing_acceptance(self):
        output = self.root / "cohort.json"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cohort.main([str(self.root), "--output", str(output)]), 0)
            self.assertEqual(cohort.main([str(self.root), "--output", str(output), "--require-no-repair"]), 2)
            self.assertEqual(cohort.main([str(self.root), "--output", str(output), "--require-physical"]), 2)
        report = evidence.read_json(output)
        self.assertTrue(report["diagnostic_valid"])
        self.assertFalse(report["progress_contract_satisfied"])
        shutil.rmtree(self.root / "direct-reserved")
        missing = self.root / "invalid-cohort.json"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cohort.main([str(self.root), "--output", str(missing)]), 1)
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()

"""Synthetic trust-boundary controls; these transcripts never ran on hardware."""
from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import campaign

HERE = Path(__file__).resolve().parent
FIXTURE = HERE.parent / "channel-progress"


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


# Import by filename: both independent host test directories have test_replay.py.
replay = module("physical_campaign_replay_tests", HERE / "replay.py")
arm = module("physical_campaign_existing_replay_fixtures", FIXTURE / "test_replay.py")
import test_boot_image as candidate_fixtures

evidence = arm.evidence


class PhysicalReplayTests(unittest.TestCase):
    def setUp(self):
        self.fixture = arm.ReplayTests()
        self.addCleanup(self.fixture.doCleanups)
        builder = candidate_fixtures.build
        self.fixture.source_revision = "f" * 40
        self.fixture.source_inventory = builder.inventory(
            builder.baseline.resolve_platform(campaign.PLATFORM, builder.ROOT))
        self.fixture.setUp()
        self.root = self.fixture.root
        family_modules = {
            "crossbeam": arm.crossbeam_fixtures.cohort,
            "std": arm.std_fixtures.cohort,
            "fifo": arm.fifo_fixtures.cohort,
        }
        for family, fixture in self.fixture.families.items():
            destination = self.root / "runs" / family
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(fixture.root, destination)
            fixture.root = destination
        for patch in (
            mock.patch.object(replay, "evidence", evidence),
            mock.patch.object(replay, "cohort", family_modules["crossbeam"]),
            mock.patch.object(replay, "FAMILIES", {
                name: ("runs/" + name, selected) for name, selected in family_modules.items()}),
            mock.patch.object(campaign, "evidence", evidence),
            mock.patch.object(campaign, "PINNED_REVISION", self.fixture.source_revision),
        ):
            patch.start()
            self.addCleanup(patch.stop)

        # All three families retain one common std ledger, with the genuine
        # ESP-specific primitive and parker pins checked by the real validators.
        first_fifo = self.run_directory("fifo-try-recv-reserved")
        library = evidence.read_json(first_fifo / "prepared/std-library-sources.json")
        library.update(evidence.std_transfer_validator().ESP_STD_SOURCE_SHA256)
        library.update(self.fixture.families["fifo"].binding.kernel.ESP_STD_SOURCE_SHA256)
        built = {}
        for build_mode in campaign.BUILDS:
            row = next(row for row in campaign.CASES if row["build_mode"] == build_mode)
            run = self.run_directory(row["mode"])
            build = arm.channel_fixtures.BuildEvidenceTests()
            build.root, build.receipt = run, evidence.read_json(run / "build.json")
            candidate = candidate_fixtures.CandidateEvidence(self, build)
            build.receipt["std_library_tree_sha256"] = evidence.canonical(library)
            hook = evidence.read_json(run / "prepared/std-channel-hook.json")
            hook["library_tree_sha256"] = build.receipt["std_library_tree_sha256"]
            self.write(run / "prepared/std-library-sources.json", library)
            self.write(run / "prepared/std-channel-hook.json", hook)
            self.write(run / "firmware/std-patch.json", {"channel_qualification": hook})
            build.refresh()
            built[build_mode] = (run, copy.deepcopy(build.receipt))
        image_bytes = candidate.candidate.stat().st_size
        image_size = mock.patch.object(campaign, "FLASH_BYTES", image_bytes)
        image_size.start()
        self.addCleanup(image_size.stop)

        for row in campaign.CASES:
            run = self.run_directory(row["mode"])
            original, receipt = built[row["build_mode"]]
            if run != original:
                for relative in receipt["artifacts_sha256"]:
                    destination = run / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(original / relative, destination)
            self.fixture.reseal(row["family"], row["mode"], receipt=copy.deepcopy(receipt), physical=True)
            raw = (run / "console.log").read_bytes()
            (run / "console.log").write_bytes((replay.ROM_BANNER + "\r\n").encode() + raw)
            session = evidence.read_json(run / "session.json")
            session.update(board="synthetic ESP32-S3 fixture; never executed", board_serial="synthetic-only",
                           campaign_id="synthetic-campaign", flash_verification="synthetic operator assertion")
            self.write(run / "session.json", session)
            self.refresh(row["mode"])

        self.manifest = {
            "schema": "synthetic-physical-campaign-test-only",
            "builds": [self.manifest_build(mode, built[mode][0]) for mode in campaign.BUILDS],
            "cases": list(campaign.CASES),
        }
        self.manifest_path = self.root / "synthetic-manifest.json"
        self.write(self.manifest_path, self.manifest)
        # Fixed production-manifest and independent Git inventory checks have
        # dedicated controls in test_campaign.py. Only their fixed identities
        # are replaced here; source receipts, raw parsers, packing, loader proof,
        # build binding and all three cohort implementations remain real.
        for patch in (
            mock.patch.object(campaign, "load_manifest", return_value=self.manifest),
            mock.patch.object(campaign, "verify_sources", return_value={
                "test_fixture_only": True, "source_revision": self.fixture.source_revision,
                "inventory_sha256": evidence.canonical(self.fixture.source_inventory)}),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.write_reports()

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value, indent=2) + "\n")

    def run_directory(self, mode):
        row = next(row for row in campaign.CASES if row["mode"] == mode)
        return self.root / row["run_path"]

    def manifest_build(self, mode, run):
        receipt = evidence.read_json(run / "build.json")
        return {"mode": mode, "build_receipt_sha256": evidence.sha256(run / "build.json"),
                **{key: receipt[key] for key in ("build_id", "image_file", "image_sha256")},
                "original_image_sha256": evidence.sha256(run / receipt["unmodified_source_image"])}

    def refresh(self, mode):
        """Reseal summaries from edited raw/session inputs, never repair the input."""
        run = self.run_directory(mode)
        receipt = evidence.read_json(run / "build.json")
        raw = (run / "console.log").read_bytes()
        bound = evidence.bind_build(evidence.analyze(raw.decode()), receipt)
        session = evidence.read_json(run / "session.json")
        attached = evidence.attach_execution(bound, session, fresh_boot_observed=True)
        self.write(run / "analysis.json", attached)
        self.write(run / "acceptance.json", bound)
        capture = evidence.read_json(run / "capture.json")
        capture.update(build_id=receipt["build_id"], image_sha256=receipt["image_sha256"],
                       boot_rom_digest_warning_observed=b"SHA-256 comparison failed:" in raw,
                       outcome=attached["outcome"],
                       progress_contract_satisfied=attached["progress_contract_satisfied"])
        self.write(run / "capture.json", capture)

    def write_reports(self):
        for family, (relative, selected) in replay.FAMILIES.items():
            self.write(self.root / (family + "-cohort.json"), selected.analyze_cohort(self.root / relative))

    def reseal_receipt(self, mode, receipt):
        row = next(row for row in campaign.CASES if row["mode"] == mode)
        run = self.run_directory(mode)
        session = evidence.read_json(run / "session.json")
        receipt = self.fixture.reseal(row["family"], mode, receipt=receipt, physical=True)
        session.update(build_id=receipt["build_id"], image_sha256=receipt["image_sha256"])
        self.write(run / "session.json", session)
        self.refresh(mode)

    def report(self):
        return replay.analyze_replay(self.root, campaign.ROOT, self.manifest_path)

    def cli(self, *arguments):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return replay.main([str(self.root), "--manifest", str(self.manifest_path), *arguments])

    def test_complete_diagnostic_preserves_three_original_failures_and_narrow_gates(self):
        report = self.report()
        self.assertEqual((report["case_count"], report["build_count"]), (19, 8))
        self.assertEqual(report["families"]["crossbeam"]["report"]["blocking_cases"],
                         ["direct-reserved", "select-reserved", "send-reserved"])
        replay.require_no_repair(report, ["std", "fifo"])
        replay.require_clean_boot(report)
        self.assertEqual(report["operator_campaign_id"], "synthetic-campaign")
        self.assertFalse(report["capture_order_verified"])
        for field in replay.QUALIFICATION_FLAGS:
            self.assertIs(report[field], False)
        with self.assertRaisesRegex(ValueError, "no-repair gate refused"):
            replay.require_no_repair(report, ["crossbeam"])
        with self.assertRaisesRegex(ValueError, "physical gate refused"):
            replay.require_physical(report)
        report["physical_execution_verified"] = report["physical_progress_qualified"] = True
        with self.assertRaisesRegex(ValueError, "physical gate refused"):
            replay.require_physical(report)

    def test_cli_preserves_report_before_scoped_refusal_and_never_overwrites(self):
        for arguments, expected in (((), 0), (("--require-no-repair", "std", "fifo", "--require-clean-boot"), 0),
                                    (("--require-no-repair", "crossbeam"), 2), (("--require-physical",), 2)):
            with self.subTest(arguments=arguments):
                output = self.root / ("result-" + str(len(list(self.root.glob("result-*")))) + ".json")
                self.assertEqual(self.cli("--output", str(output), *arguments), expected)
                self.assertIs(evidence.read_json(output)["physical_execution_verified"], False)
        retained = self.root / "std-cohort.json"
        before = retained.read_bytes()
        self.assertEqual(self.cli("--output", str(retained)), 1)
        self.assertEqual(retained.read_bytes(), before)

    def test_missing_four_cases_is_a_15_case_diagnostic_not_a_complete_campaign(self):
        for mode in ("direct-reserved", "direct-published", "send-reserved", "send-reclaimed"):
            shutil.rmtree(self.run_directory(mode).parent)
        with self.assertRaisesRegex(ValueError, "exactly 19 named captures"):
            self.report()
        self.assertEqual(self.cli(), 1)
        # The existing std/FIFO gates remain usable independently.
        for family in ("std", "fifo"):
            relative, selected = replay.FAMILIES[family]
            selected.require_no_repair(selected.analyze_cohort(self.root / relative))

    def test_extra_capture_and_missing_saved_family_report_refuse(self):
        extra = self.root / "extra"
        extra.mkdir()
        shutil.copyfile(self.run_directory("direct-reserved") / "capture.json", extra / "capture.json")
        with self.assertRaisesRegex(ValueError, "exactly 19 named captures"):
            self.report()
        shutil.rmtree(extra)
        (self.root / "fifo-cohort.json").unlink()
        with self.assertRaises(ValueError):
            self.report()

    def test_duplicate_measurement_session_refuses_even_when_stored_analysis_agrees(self):
        first = evidence.read_json(self.run_directory("direct-reserved") / "session.json")
        run = self.run_directory("direct-published")
        session = evidence.read_json(run / "session.json")
        session["measurement_session"] = first["measurement_session"]
        self.write(run / "session.json", session)
        self.refresh("direct-published")
        self.write_reports()
        with self.assertRaisesRegex(ValueError, "duplicate campaign measurement session"):
            self.report()

    def test_different_campaign_id_is_not_inferred_from_distinct_measurement_labels(self):
        run = self.run_directory("direct-published")
        session = evidence.read_json(run / "session.json")
        for value in ("another-synthetic-campaign", None, "", 0):
            with self.subTest(value=value):
                if value is None:
                    session.pop("campaign_id", None)
                else:
                    session["campaign_id"] = value
                self.write(run / "session.json", session)
                self.refresh("direct-published")
                self.write_reports()
                with self.assertRaisesRegex(ValueError, "campaign ID"):
                    self.report()

    def test_individually_valid_family_cannot_change_board_identity(self):
        for row in campaign.CASES:
            if row["family"] == "std":
                run = self.run_directory(row["mode"])
                session = evidence.read_json(run / "session.json")
                session["board_serial"] = "different-synthetic-board"
                self.write(run / "session.json", session)
                self.refresh(row["mode"])
        self.write_reports()
        with self.assertRaisesRegex(ValueError, "mixed campaign board/session environment"):
            self.report()

    def test_frozen_receipt_rejects_valid_rebuilt_identity(self):
        mode = "direct-published"
        run = self.run_directory(mode)
        receipt = evidence.read_json(run / "build.json")
        receipt["build_nonce"] = "9" * 32
        self.reseal_receipt(mode, receipt)
        # Resealing preserves all semantic build proofs but does not change the
        # independent frozen manifest's exact receipt and runtime-build identity.
        with self.assertRaisesRegex(ValueError, "frozen manifest"):
            self.report()

    def test_mixed_source_inventory_cannot_hide_behind_rehashed_valid_capture(self):
        mode = "direct-published"
        run = self.run_directory(mode)
        receipt = evidence.read_json(run / "build.json")
        receipt["source_sha256"]["tests/channel-progress/rust/src/main.rs"] = "0" * 64
        self.reseal_receipt(mode, receipt)
        with self.assertRaisesRegex(ValueError, "source_sha256"):
            self.report()

    def test_profile_downgrade_and_image_mutation_refuse(self):
        run = self.run_directory("direct-published")
        original = (run / "build.json").read_bytes()
        receipt = evidence.read_json(run / "build.json")
        receipt["boot_image_profile"] = "platform-default"
        self.write(run / "build.json", receipt)
        with self.assertRaises(ValueError):
            self.report()
        (run / "build.json").write_bytes(original)
        image = run / receipt["image_file"]
        data = bytearray(image.read_bytes())
        data[-1] ^= 1
        image.write_bytes(data)
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            self.report()

    def test_stored_crossbeam_analysis_extra_claim_is_not_legacy_compatible(self):
        run = self.run_directory("direct-published")
        stored = evidence.read_json(run / "analysis.json")
        stored["unreplayed_result_claim"] = "synthetic forgery"
        self.write(run / "analysis.json", stored)
        with self.assertRaisesRegex(ValueError, "stored campaign analysis differs"):
            self.report()

    def test_acceptance_and_saved_family_reports_require_exact_boolean_types(self):
        for mode in ("direct-published", "fifo-try-recv-published"):
            with self.subTest(mode=mode):
                path = self.run_directory(mode) / "acceptance.json"
                before = path.read_bytes()
                record = evidence.read_json(path)
                record["physical_execution_verified"] = 0
                self.write(path, record)
                with self.assertRaisesRegex(ValueError, "stored campaign acceptance differs"):
                    self.report()
                path.write_bytes(before)
        path = self.root / "std-cohort.json"
        report = evidence.read_json(path)
        report["physical_execution_verified"] = 0
        self.write(path, report)
        with self.assertRaisesRegex(ValueError, "stored campaign report differs"):
            self.report()

    def test_consistent_operator_claims_do_not_authenticate_execution(self):
        mode = "direct-published"
        run = self.run_directory(mode)
        session = evidence.read_json(run / "session.json")
        session["physical_execution_verified"] = True
        self.write(run / "session.json", session)
        self.refresh(mode)
        self.write_reports()
        with self.assertRaisesRegex(ValueError, "unsupported campaign qualification"):
            self.report()

    def test_warning_and_missing_or_misplaced_rom_prefix_only_refuse_clean_boot_gate(self):
        mode = "direct-published"
        run = self.run_directory(mode)
        original = (run / "console.log").read_bytes()
        variants = {
            "warning": original.replace((replay.ROM_BANNER + "\r\n").encode(),
                                        (replay.ROM_BANNER + "\r\nSHA-256 comparison failed:\r\n").encode()),
            "missing": original.replace((replay.ROM_BANNER + "\r\n").encode(), b""),
            "misplaced": original.replace((replay.ROM_BANNER + "\r\n").encode(), b"").replace(
                b"nsh> ", (replay.ROM_BANNER + "\r\nnsh> ").encode(), 1),
        }
        for name, raw in variants.items():
            with self.subTest(name=name):
                (run / "console.log").write_bytes(raw)
                self.refresh(mode)
                self.write_reports()
                report = self.report()
                self.assertIs(report["diagnostic_valid"], True)
                self.assertIs(report["clean_boot_observed"], False)
                self.assertEqual(report["clean_boot_blocking_cases"], [mode])
                self.assertEqual(self.cli(), 0)
                self.assertEqual(self.cli("--require-clean-boot"), 2)

    def test_raw_fault_and_duplicate_rom_boots_cannot_use_saved_success(self):
        run = self.run_directory("direct-published")
        original = (run / "console.log").read_bytes()
        for prefix in (b"PANIC synthetic fault\r\n", (replay.ROM_BANNER + "\r\n").encode()):
            with self.subTest(prefix=prefix):
                (run / "console.log").write_bytes(prefix + original)
                with self.assertRaisesRegex(ValueError, "fault|multiple ESP ROM boots"):
                    self.report()
        (run / "console.log").write_bytes(original)

    def test_symlink_and_special_file_refuse_before_evidence_read(self):
        path = self.root / "alias"
        path.symlink_to(self.run_directory("direct-published") / "console.log")
        with self.assertRaisesRegex(ValueError, "linked campaign evidence"):
            self.report()
        path.unlink()
        os.mkfifo(path)
        with self.assertRaisesRegex(ValueError, "non-regular campaign evidence"):
            self.report()

    def test_clean_boot_gate_rechecks_cases_and_strict_boolean_values(self):
        report = self.report()
        report["boot_observations"][0]["rom_boot_prefix_observed"] = 1
        with self.assertRaisesRegex(ValueError, "clean-boot gate refused"):
            replay.require_clean_boot(report)


class CliFailureTests(unittest.TestCase):
    def test_git_timeout_is_invalid_evidence_without_a_partial_report(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output.json"
            with mock.patch.object(replay, "analyze_replay", side_effect=subprocess.TimeoutExpired("git", 30)), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(replay.main([directory, "--output", str(output)]), 1)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()

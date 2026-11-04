"""Adversarial controls for the complete source-bound ARM/QEMU evidence set."""
from __future__ import annotations

import contextlib
import copy
import gzip
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import test_analyze as channel_fixtures
import test_cohort as crossbeam_fixtures
import test_fifo_cohort as fifo_fixtures
import test_std_cohort as std_fixtures

evidence = channel_fixtures.analyze
with mock.patch.dict(sys.modules, {
    "analyze": evidence,
    "cohort": crossbeam_fixtures.cohort,
    "fifo_cohort": fifo_fixtures.cohort,
    "std_cohort": std_fixtures.cohort,
}):
    replay = channel_fixtures.module("channel_progress_replay_tests", channel_fixtures.HERE / "replay.py")

BROAD_CLAIMS = (
    "physical_execution_verified", "physical_progress_qualified", "priority_progress_qualified",
    "production_transport_qualified", "physical_timing_qualified", "timing_qualified", "hard_realtime_bound",
)


def git(repository, *arguments):
    return subprocess.run(["git", "-C", str(repository), *arguments], check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout


def source_repository(directory):
    """Commit the actual complete ARM input set, including the validators.

    This is a local synthetic source commit, not a target build. Keeping source
    verification real makes omitted inventory entries and stale revisions
    distinguishable from ordinary per-capture validation failures.
    """
    builder = channel_fixtures.module("progress_replay_build_inputs", channel_fixtures.HERE / "build.py")
    profile = builder.baseline.resolve_platform("mps2-an521-mock", builder.ROOT)
    inventory = builder.inventory(profile)
    directory.mkdir()
    for relative in inventory:
        destination = directory / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(builder.ROOT / relative, destination)
    git(directory, "init", "--quiet")
    git(directory, "add", ".")
    git(directory, "-c", "user.name=Progress evidence tests", "-c", "user.email=progress-tests@example.invalid",
        "-c", "commit.gpgSign=false", "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m",
        "Freeze synthetic replay source inputs")
    return git(directory, "rev-parse", "HEAD").decode().strip(), inventory


class ReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source_tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.source_tmp.cleanup)
        cls.repository = Path(cls.source_tmp.name) / "source"
        cls.source_revision, cls.source_inventory = source_repository(cls.repository)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "artifact"
        self.root.mkdir()
        self.families = {}
        archive = channel_fixtures.source_tests.archive

        def frozen_archive(*args, **kwargs):
            # Separate synthetic build factories must use identical release
            # archive bytes even when setup crosses a wall-clock second.
            return gzip.compress(gzip.decompress(archive(*args, **kwargs)), mtime=0)

        with mock.patch.object(channel_fixtures.source_tests, "archive", side_effect=frozen_archive):
            for name, factory, relative in (
                ("crossbeam", crossbeam_fixtures.CohortTests, "mps2"),
                ("std", std_fixtures.StdCohortTests, "std-transfer/mps2/cases"),
                ("fifo", fifo_fixtures.FifoCohortTests, "fifo/mps2/cases"),
            ):
                fixture = factory()
                self.addCleanup(fixture.doCleanups)
                fixture.setUp()
                destination = self.root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(fixture.root, destination)
                fixture.root = destination
                self.families[name] = fixture

        # The real builds share one prepared std tree and one source/config
        # cohort. Give all synthetic builds the same complete std ledger;
        # FIFO's additional pins remain genuinely checked by its validator.
        first_fifo = next(iter(fifo_fixtures.wire.CASES))
        fifo_run = self.families["fifo"].run_directory(first_fifo)
        common_library = evidence.read_json(fifo_run / "prepared/std-library-sources.json")
        fifo_receipt = evidence.read_json(fifo_run / "build.json")
        self.sources = copy.deepcopy(self.source_inventory)
        for family, mode, run in self.runs():
            receipt = evidence.read_json(run / "build.json")
            receipt.update(source_sha256=copy.deepcopy(self.sources),
                           source_revision=self.source_revision,
                           kernel_revision=fifo_receipt["kernel_revision"],
                           std_library_tree_sha256=evidence.canonical(common_library))
            hook = evidence.read_json(run / "prepared/std-channel-hook.json")
            hook["library_tree_sha256"] = receipt["std_library_tree_sha256"]
            self.write(run / "prepared/std-library-sources.json", common_library)
            self.write(run / "prepared/std-channel-hook.json", hook)
            self.write(run / "firmware/std-patch.json", {"channel_qualification": hook})
            self.reseal(family, mode, receipt=receipt)
        self.write_reports()

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value, indent=2) + "\n")

    def runs(self, selected=None):
        for family, fixture in self.families.items():
            if selected is None or family == selected:
                for mode in sorted(path.name for path in fixture.root.iterdir() if path.is_dir()):
                    yield family, mode, fixture.run_directory(mode)

    def run_directory(self, family, mode=None):
        return next(run for _, current, run in self.runs(family) if mode is None or current == mode)

    def reseal(self, family, mode, *, receipt=None, physical=False):
        run = self.families[family].run_directory(mode)
        if receipt is None:
            receipt = evidence.read_json(run / "build.json")
        for key in ("image", "elf", "config"):
            receipt[key + "_sha256"] = evidence.sha256(run / receipt[key + "_file"])
        receipt["artifacts_sha256"] = {
            name: evidence.sha256(run / name) for name in receipt["artifacts_sha256"]
        }
        receipt["build_id"] = evidence.canonical({
            key: receipt[key] for key in evidence.identity_fields(receipt["mode"])
        })
        self.write(run / "build.json", receipt)
        original = (run / "console.log").read_text()
        records = []
        for line in original.splitlines():
            if line.startswith(evidence.PREFIX):
                row = json.loads(line[len(evidence.PREFIX):])
                if row["record"] == "metadata":
                    row["build_id"] = receipt["build_id"]
                line = evidence.PREFIX + json.dumps(row)
            records.append(line)
        raw = "\r\n".join(records)
        (run / "console.log").write_bytes(raw.encode())
        session = evidence.read_json(run / "session.json")
        session.update(build_id=receipt["build_id"], image_sha256=receipt["image_sha256"])
        if physical:
            session.update(runtime_kind="physical", measurement_session="operator-entry-" + mode,
                           board_serial="operator-serial", flash_verification="operator-entry", active_cores=1)
        self.write(run / "session.json", session)
        bound = evidence.bind_build(evidence.analyze(raw), receipt)
        self.write(run / "analysis.json", evidence.attach_execution(bound, session, fresh_boot_observed=True))
        if family != "crossbeam":
            # CI's no-repair acceptance invocation binds firmware but does not
            # attach a capture session. Preserve that distinct stored contract.
            self.write(run / "acceptance.json", bound)
        capture = evidence.read_json(run / "capture.json")
        capture.update(build_id=receipt["build_id"], image_sha256=receipt["image_sha256"],
                       runtime_kind=session["runtime_kind"])
        self.write(run / "capture.json", capture)
        return receipt

    def write_reports(self):
        reports = {name: getattr(module, "analyze_cohort")(self.families[name].root)
                   for name, module in (("crossbeam", crossbeam_fixtures.cohort),
                                        ("std", std_fixtures.cohort), ("fifo", fifo_fixtures.cohort))}
        for relative, family in (("mps2/cohort.json", "crossbeam"), ("mps2/acceptance.json", "crossbeam"),
                                 ("std-transfer/mps2/cohort.json", "std"), ("fifo/mps2/cohort.json", "fifo")):
            self.write(self.root / relative, reports[family])
        return reports

    def analyze(self):
        return replay.analyze_replay(self.root, self.repository)

    def test_complete_nineteen_preserves_each_familys_scope_and_original_blockers(self):
        report = self.analyze()
        self.assertEqual(report["schema"], "channel-progress-replay-v1")
        self.assertTrue(report["diagnostic_valid"])
        self.assertTrue(report["cohort_complete"])
        self.assertEqual(report["case_count"], 19)
        self.assertEqual(report["build_count"], 8)
        self.assertEqual(set(report["families"]), {"crossbeam", "std", "fifo"})
        self.assertEqual({name: value["no_repair_satisfied"] for name, value in report["families"].items()},
                         {"crossbeam": False, "std": True, "fifo": True})
        self.assertEqual({name: value["case_count"] for name, value in report["families"].items()},
                         {"crossbeam": 6, "std": 7, "fifo": 6})
        self.assertEqual(report["families"]["crossbeam"]["report"]["blocking_cases"],
                         ["direct-reserved", "select-reserved", "send-reserved"])
        self.assertEqual(report["families"]["std"]["report"]["scope"], "std-transfer-primitives")
        self.assertEqual(report["families"]["fifo"]["report"]["scope"], "equal-fifo-reservation-progress")
        self.assertEqual(report["runtime_kind"], "qemu")
        self.assertEqual(report["execution_evidence"], "virtual")
        self.assertIsNot(report.get("progress_contract_satisfied"), True)
        for field in BROAD_CLAIMS:
            self.assertIs(report[field], False)
            for family in report["families"].values():
                # The retained legacy Crossbeam schema predates a separate
                # production flag; any claim it does contain must stay false.
                self.assertIs(family["report"].get(field, False), False)
        replay.require_no_repair(report, ["std"])
        replay.require_no_repair(report, ["fifo"])
        replay.require_no_repair(report, ["std", "fifo"])
        with self.assertRaisesRegex(ValueError, "no-repair"):
            replay.require_no_repair(report, ["crossbeam"])
        with self.assertRaisesRegex(ValueError, "no-repair"):
            replay.require_no_repair(report, ["std", "crossbeam", "fifo"])
        with self.assertRaisesRegex(ValueError, "physical"):
            replay.require_physical(report)

    def test_any_missing_case_and_an_extra_capture_are_refused(self):
        for family in self.families:
            path = self.run_directory(family) / "capture.json"
            raw = path.read_bytes()
            path.unlink()
            with self.subTest(family=family), self.assertRaises(ValueError):
                self.analyze()
            path.write_bytes(raw)
        unexpected = self.root / "extra/run"
        unexpected.mkdir(parents=True)
        shutil.copyfile(self.run_directory("std") / "capture.json", unexpected / "capture.json")
        with self.assertRaises(ValueError):
            self.analyze()

    def test_every_case_requires_raw_capture_session_analysis_and_suite_acceptance(self):
        for family in self.families:
            names = ("console.log", "build.json", "session.json", "analysis.json")
            if family != "crossbeam":
                names += ("acceptance.json",)
            for name in names:
                path = self.run_directory(family) / name
                original = path.read_bytes()
                path.unlink()
                with self.subTest(family=family, name=name), self.assertRaises((ValueError, OSError)):
                    self.analyze()
                path.write_bytes(original)

    def test_valid_same_suite_case_cannot_substitute_for_another_mode(self):
        source = self.run_directory("std", "mutex-admit-released")
        target = self.run_directory("std", "mutex-admit-held")
        shutil.rmtree(target)
        shutil.copytree(source, target)
        with self.assertRaises(ValueError):
            self.analyze()
        raw = target / "console.log"
        raw.write_bytes(raw.read_bytes().replace(b"channel_progress mutex-admit-released",
                                                b"channel_progress mutex-admit-held", 1))
        with self.assertRaises(ValueError):
            self.analyze()

    def test_individually_valid_shared_std_build_cannot_use_a_different_configuration(self):
        for family, mode, run in self.runs("std"):
            receipt = evidence.read_json(run / "build.json")
            config = run / receipt["config_file"]
            config.write_text(config.read_text() + "CONFIG_ANOTHER_DEBUG_SETTING=y\n")
            receipt["resolved_configuration"]["CONFIG_ANOTHER_DEBUG_SETTING"] = "y"
            self.reseal(family, mode, receipt=receipt)
        # This entire family remains independently coherent and accepted.
        std_fixtures.cohort.require_no_repair(std_fixtures.cohort.analyze_cohort(self.families["std"].root))
        self.write_reports()
        with self.assertRaisesRegex(ValueError, "mixed|configuration|identity"):
            self.analyze()

    def test_individually_valid_std_family_cannot_change_common_source_or_toolchain_identity(self):
        changes = (
            ("source_revision", "9" * 40),
            ("source_sha256", {**self.sources, "invented.rs": "8" * 64}),
            ("kernel_revision", "7" * 40),
            ("apps_revision", "6" * 40),
            ("staged_inputs_sha256", {"workspace/alternate.rs": "5" * 64}),
        )
        original = {mode: evidence.read_json(run / "build.json") for _, mode, run in self.runs("std")}
        for key, value in changes:
            for family, mode, _ in self.runs("std"):
                receipt = copy.deepcopy(original[mode])
                receipt[key] = value
                self.reseal(family, mode, receipt=receipt)
            std_fixtures.cohort.require_no_repair(std_fixtures.cohort.analyze_cohort(self.families["std"].root))
            self.write_reports()
            with self.subTest(field=key), self.assertRaisesRegex(ValueError, "mixed|identity"):
                self.analyze()

    def test_individually_valid_std_library_and_compiler_cannot_differ_across_families(self):
        for family, mode, run in self.runs("std"):
            receipt = evidence.read_json(run / "build.json")
            library = evidence.read_json(run / "prepared/std-library-sources.json")
            library["std/src/synthetic-additional.rs"] = "4" * 64
            receipt["std_library_tree_sha256"] = evidence.canonical(library)
            receipt["compiler_identity"]["version"] = "a different synthetic compiler"
            hook = evidence.read_json(run / "prepared/std-channel-hook.json")
            hook.update(library_tree_sha256=receipt["std_library_tree_sha256"],
                        compiler=receipt["compiler_identity"])
            self.write(run / "prepared/std-library-sources.json", library)
            self.write(run / "prepared/std-channel-hook.json", hook)
            self.write(run / "firmware/std-patch.json", {"channel_qualification": hook})
            self.reseal(family, mode, receipt=receipt)
        std_fixtures.cohort.require_no_repair(std_fixtures.cohort.analyze_cohort(self.families["std"].root))
        self.write_reports()
        with self.assertRaisesRegex(ValueError, "mixed|identity"):
            self.analyze()

    def test_a_common_resealed_false_source_hash_is_still_rejected_against_git(self):
        altered = {**self.sources, "tests/channel-progress/rust/src/main.rs": "0" * 64}
        for family, mode, run in self.runs():
            receipt = evidence.read_json(run / "build.json")
            receipt["source_sha256"] = altered
            self.reseal(family, mode, receipt=receipt)
        # All eight builds and all three families agree on the false claim;
        # independent Git bytes, rather than agreement alone, must reject it.
        self.write_reports()
        with self.assertRaisesRegex(ValueError, "source bytes differ"):
            self.analyze()

    def test_common_identity_comparison_preserves_json_boolean_types(self):
        for family, mode, run in self.runs():
            receipt = evidence.read_json(run / "build.json")
            receipt["compiler_identity"]["synthetic_identity_flag"] = 1 if family == "std" else True
            hook = evidence.read_json(run / "prepared/std-channel-hook.json")
            hook["compiler"] = receipt["compiler_identity"]
            self.write(run / "prepared/std-channel-hook.json", hook)
            self.write(run / "firmware/std-patch.json", {"channel_qualification": hook})
            self.reseal(family, mode, receipt=receipt)
        # Each complete family independently validates; Python dict equality
        # would incorrectly collapse their distinct frozen compiler identities.
        self.write_reports()
        with self.assertRaisesRegex(ValueError, "mixed|identity"):
            self.analyze()

    def test_stored_analysis_acceptance_and_cohort_claims_are_replayed_not_trusted(self):
        cases = (
            (self.run_directory("crossbeam") / "analysis.json", "physical_execution_verified"),
            (self.run_directory("std") / "acceptance.json", "primitive_contract_satisfied"),
            (self.run_directory("fifo") / "acceptance.json", "production_transport_qualified"),
            (self.root / "mps2/cohort.json", "diagnostic_valid"),
            (self.root / "mps2/acceptance.json", "progress_contract_satisfied"),
            (self.root / "std-transfer/mps2/cohort.json", "primitive_contract_satisfied"),
            (self.root / "fifo/mps2/cohort.json", "candidate_contract_satisfied"),
        )
        for path, field in cases:
            original = path.read_bytes()
            value = evidence.read_json(path)
            value[field] = not value[field]
            self.write(path, value)
            with self.subTest(path=path.relative_to(self.root), field=field), self.assertRaises(ValueError):
                self.analyze()
            path.write_bytes(original)

    def test_stored_json_booleans_cannot_be_replaced_by_equal_python_integers(self):
        for path, field, integer in (
            (self.root / "mps2/cohort.json", "diagnostic_valid", 1),
            (self.root / "fifo/mps2/cohort.json", "physical_execution_verified", 0),
            (self.run_directory("fifo") / "acceptance.json", "physical_execution_verified", 0),
        ):
            original = path.read_bytes()
            value = evidence.read_json(path)
            self.assertIs(type(value[field]), bool)
            value[field] = integer
            self.write(path, value)
            with self.subTest(path=path.relative_to(self.root)), self.assertRaises(ValueError):
                self.analyze()
            path.write_bytes(original)

    def test_linked_raw_evidence_is_refused_even_when_the_bytes_are_unchanged(self):
        run = self.run_directory("std")
        original = run / "console.log"
        retained = run / "same-console.log"
        original.rename(retained)
        original.symlink_to(retained.name)
        with self.assertRaisesRegex(ValueError, "linked"):
            self.analyze()

    def test_new_source_requires_retained_std_cohort_report(self):
        (self.root / "std-transfer/mps2/cohort.json").unlink()
        with self.assertRaises((ValueError, OSError)):
            self.analyze()

    def test_historical_source_without_std_cohort_can_replay_without_its_report(self):
        historical = Path(self.tmp.name) / "historical-source"
        shutil.copytree(self.repository, historical)
        relative = "tests/channel-progress/std_cohort.py"
        git(historical, "rm", "--quiet", relative)
        git(historical, "-c", "user.name=Progress evidence tests", "-c", "user.email=progress-tests@example.invalid",
            "-c", "commit.gpgSign=false", "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m",
            "Synthetic historical source without std cohort gate")
        revision = git(historical, "rev-parse", "HEAD").decode().strip()
        historical_inventory = {key: value for key, value in self.sources.items() if key != relative}
        for family, mode, run in self.runs():
            receipt = evidence.read_json(run / "build.json")
            receipt.update(source_revision=revision, source_sha256=historical_inventory)
            self.reseal(family, mode, receipt=receipt)
        self.write_reports()
        (self.root / "std-transfer/mps2/cohort.json").unlink()
        report = replay.analyze_replay(self.root, historical)
        self.assertTrue(report["diagnostic_valid"])
        self.assertTrue(report["families"]["std"]["no_repair_satisfied"])
        replay.require_no_repair(report, ["std", "fifo"])

    def test_raw_fault_and_capture_identity_corruption_cannot_hide_behind_stored_success(self):
        run = self.run_directory("fifo")
        path = run / "console.log"
        original = path.read_bytes()
        path.write_bytes(original + channel_fixtures.NUTTX_BOOT_FAULT.encode())
        with self.assertRaisesRegex(ValueError, "fault"):
            self.analyze()
        path.write_bytes(original)
        capture = evidence.read_json(run / "capture.json")
        capture["image_sha256"] = "0" * 64
        self.write(run / "capture.json", capture)
        with self.assertRaisesRegex(ValueError, "identity"):
            self.analyze()

    def test_operator_physical_labels_cannot_enter_the_arm_virtual_replay_gate(self):
        for family, mode, _ in self.runs():
            self.reseal(family, mode, physical=True)
        reports = self.write_reports()
        for report in reports.values():
            self.assertEqual(report["execution_evidence"], "operator-asserted")
            self.assertFalse(report["physical_execution_verified"])
        with self.assertRaisesRegex(ValueError, "QEMU|qemu|virtual"):
            self.analyze()

    def test_broader_claim_cannot_be_smuggled_through_consistent_session_or_capture(self):
        # The raw observation may be coherent, but an extra unsupported scope
        # assertion in an otherwise matching JSON record must still be refused.
        run = self.run_directory("fifo")
        for name in ("capture.json", "session.json"):
            path = run / name
            original = path.read_bytes()
            for field in BROAD_CLAIMS:
                record = json.loads(original)
                record[field] = True
                self.write(path, record)
                analysis_path = run / "analysis.json"
                analysis_original = analysis_path.read_bytes()
                if name == "session.json":
                    analysis = evidence.read_json(analysis_path)
                    analysis["session"] = record
                    self.write(analysis_path, analysis)
                try:
                    self.write_reports()
                except ValueError:
                    # Some claims are already refused by a family validator.
                    # Others must survive that narrower check and be rejected
                    # by the complete replay, even with fresh stored reports.
                    pass
                with self.subTest(name=name, field=field), self.assertRaises(ValueError):
                    self.analyze()
                analysis_path.write_bytes(analysis_original)
            path.write_bytes(original)
            self.write_reports()

    def test_cli_keeps_diagnostic_report_when_only_requested_acceptance_refuses(self):
        for flags, expected in (((), 0), (("--require-no-repair", "std", "fifo"), 0),
                                (("--require-no-repair", "crossbeam"), 2), (("--require-physical",), 2)):
            output = self.root / "replay.json"
            output.unlink(missing_ok=True)
            with self.subTest(flags=flags), contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                status = replay.main([str(self.root), "--repository", str(self.repository),
                                      "--output", str(output), *flags])
            self.assertEqual(status, expected)
            self.assertEqual(evidence.read_json(output)["schema"], replay.SCHEMA)
            self.assertTrue(evidence.read_json(output)["diagnostic_valid"])
        (self.run_directory("std") / "capture.json").unlink()
        output.unlink()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            status = replay.main([str(self.root), "--repository", str(self.repository), "--output", str(output)])
        self.assertEqual(status, 1)
        self.assertFalse(output.exists())

    def test_family_gate_rechecks_its_named_scope_and_underlying_result(self):
        report = self.analyze()
        forged = copy.deepcopy(report)
        forged["families"]["crossbeam"]["no_repair_satisfied"] = True
        with self.assertRaisesRegex(ValueError, "no-repair"):
            replay.require_no_repair(forged, ["crossbeam"])
        for family, scope in (("std", "production-transport"), ("fifo", "std-transfer-primitives")):
            forged = copy.deepcopy(report)
            forged["families"][family]["report"]["scope"] = scope
            with self.subTest(family=family), self.assertRaisesRegex(ValueError, "no-repair"):
                replay.require_no_repair(forged, [family])
        for families in ([], ["unknown"], ["std", "unknown"]):
            with self.subTest(families=families), self.assertRaises(ValueError):
                replay.require_no_repair(report, families)


class SourceVerificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source_tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.source_tmp.cleanup)
        cls.repository = Path(cls.source_tmp.name) / "source"
        cls.revision, cls.inventory = source_repository(cls.repository)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run = Path(self.tmp.name)
        self.receipt = {
            "schema": "channel-progress-build-v1", "status": "complete",
            "platform": "mps2-an521-mock", "target": "thumbv8m.main-nuttx-eabi",
            "source_revision": self.revision, "source_sha256": copy.deepcopy(self.inventory),
            "source_dirty": False,
        }
        ReplayTests.write(self.run / "build.json", self.receipt)

    def verify(self, receipt=None):
        if receipt is not None:
            self.receipt = receipt
            ReplayTests.write(self.run / "build.json", self.receipt)
        return replay.verify_sources(self.repository, self.receipt, self.run)

    def test_full_inventory_is_hashed_against_the_retained_git_commit(self):
        report = self.verify()
        self.assertEqual(report["source_revision"], self.revision)
        self.assertEqual(report["source_tree"], git(self.repository, "rev-parse", self.revision + "^{tree}").decode().strip())
        self.assertEqual(report["source_sha256"], self.inventory)
        self.assertEqual(report["source_input_count"], len(self.inventory))
        self.assertEqual(report["inventory_sha256"], evidence.canonical(self.inventory))
        self.assertEqual(report["inventory_policy"], "mps2-channel-progress-inputs-v1")
        self.assertFalse(report["historical_std_report_optional"])

    def test_inventory_policy_matches_the_live_build_recipe_including_new_modules(self):
        builder = channel_fixtures.module("progress_replay_inventory_parity", channel_fixtures.HERE / "build.py")
        profile = builder.baseline.resolve_platform("mps2-an521-mock", builder.ROOT)
        inventory = builder.inventory(profile)
        available = {name.decode() for name in git(builder.ROOT, "ls-files", "--cached", "--others",
                                                   "--exclude-standard", "-z").split(b"\0") if name}
        self.assertEqual(set(replay.expected_source_paths(available)), set(inventory))
        for name in ("std_cohort.py", "test_std_cohort.py", "replay.py", "test_replay.py"):
            self.assertIn("tests/channel-progress/" + name, inventory)
        unrelated = {"docs/unrelated.py", "tests/channel-progress/README.md",
                     "tests/channel-progress/__pycache__/ignored.py"}
        self.assertEqual(set(replay.expected_source_paths(available | unrelated)), set(inventory))

    def test_omitted_extra_and_rehashed_wrong_source_entries_are_refused(self):
        name = "tests/channel-progress/rust/src/main.rs"
        omitted = {key: value for key, value in self.inventory.items() if key != name}
        extra = {**self.inventory, "invented.rs": "1" * 64}
        altered = {**self.inventory, name: "2" * 64}
        for label, inventory in (("omitted", omitted), ("extra", extra), ("wrong hash", altered)):
            receipt = {**self.receipt, "source_sha256": inventory}
            with self.subTest(change=label), self.assertRaises(ValueError):
                self.verify(receipt)

    def test_nonexistent_revision_and_real_revision_with_changed_bytes_are_refused(self):
        with self.assertRaises((ValueError, OSError)):
            self.verify({**self.receipt, "source_revision": "0" * 40})
        changed = self.run / "changed-repository"
        shutil.copytree(self.repository, changed)
        source = changed / "tests/channel-progress/rust/src/main.rs"
        source.write_bytes(source.read_bytes() + b"\n// synthetic source mutation\n")
        git(changed, "add", "tests/channel-progress/rust/src/main.rs")
        git(changed, "-c", "user.name=Progress evidence tests", "-c", "user.email=progress-tests@example.invalid",
            "-c", "commit.gpgSign=false", "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m",
            "Mutate one frozen input")
        revision = git(changed, "rev-parse", "HEAD").decode().strip()
        receipt = {**self.receipt, "source_revision": revision}
        ReplayTests.write(self.run / "build.json", receipt)
        with self.assertRaises(ValueError):
            replay.verify_sources(changed, receipt, self.run)

    def test_working_tree_bytes_cannot_replace_the_recorded_git_revision(self):
        source = self.repository / "tests/channel-progress/rust/src/main.rs"
        original = source.read_bytes()
        try:
            source.write_bytes(original + b"\n// uncommitted bytes are not the recorded source\n")
            report = self.verify()
            self.assertEqual(report["source_revision"], self.revision)
            self.assertEqual(report["source_sha256"], self.inventory)
        finally:
            source.write_bytes(original)

    def test_a_git_symlink_cannot_replace_a_frozen_regular_source(self):
        linked = self.run / "linked-repository"
        shutil.copytree(self.repository, linked)
        name = "tests/channel-progress/rust/src/main.rs"
        path = linked / name
        path.unlink()
        path.symlink_to("../../analyze.py")
        git(linked, "add", name)
        git(linked, "-c", "user.name=Progress evidence tests", "-c", "user.email=progress-tests@example.invalid",
            "-c", "commit.gpgSign=false", "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m",
            "Substitute a source symlink")
        revision = git(linked, "rev-parse", "HEAD").decode().strip()
        receipt = {**self.receipt, "source_revision": revision}
        ReplayTests.write(self.run / "build.json", receipt)
        with self.assertRaisesRegex(ValueError, "regular Git blobs"):
            replay.verify_sources(linked, receipt, self.run)

    def test_escape_absolute_and_newline_source_paths_are_refused(self):
        for name in ("../outside.rs", "/absolute.rs", "tests/channel-progress/line\nbreak.py"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.verify({**self.receipt, "source_sha256": {**self.inventory, name: "3" * 64}})

    def test_receipt_must_match_its_regular_retained_file(self):
        path = self.run / "build.json"
        ReplayTests.write(path, {**self.receipt, "source_revision": "4" * 40})
        with self.assertRaises(ValueError):
            self.verify()
        outside = self.run / "linked-build.json"
        ReplayTests.write(outside, self.receipt)
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaises(ValueError):
            self.verify()

    def test_retained_receipt_match_preserves_json_boolean_types(self):
        # Python mapping equality would equate False with 0. The helper must
        # identify the exact stored JSON receipt that was already validated.
        ReplayTests.write(self.run / "build.json", {**self.receipt, "source_dirty": 0})
        with self.assertRaises(ValueError):
            self.verify()


if __name__ == "__main__":
    unittest.main()

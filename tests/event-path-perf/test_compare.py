"""Comparison gates over independently replayable, explicitly synthetic captures."""
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
from contextlib import redirect_stdout
from unittest.mock import patch


HERE = Path(__file__).resolve().parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


comparison = load("event_path_comparison_tests", "compare.py")
replay_module = load("event_path_comparison_replay", "replay.py")
fixtures = load("event_path_comparison_capture_fixtures", "test_capture.py")
capture = fixtures.capture
specimens = fixtures.fixtures

GROUPS = {
    "ready-try": ("ready-std-try", "ready-messaging-try", "ready-crossbeam-try"),
    "ready-send": ("ready-std-send", "ready-messaging-send", "ready-crossbeam-send"),
    "blocked-try": ("blocked-std-try", "blocked-messaging-try", "blocked-crossbeam-try"),
    "blocked-send": ("blocked-std-send", "blocked-messaging-send", "blocked-crossbeam-send"),
    "full-rejection": ("full-std", "full-messaging", "full-crossbeam"),
    "ready-selection": ("ready-crossbeam-try", "ready-select-one", "ready-select-three"),
    "blocked-selection": ("blocked-crossbeam-try", "blocked-select-one", "blocked-select-three"),
}


class ComparisonControls(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CaptureControls(
            "test_successful_host_capture_is_functional_only_and_output_is_never_reused")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.artifacts = {name: (self.fixture.build / name).read_bytes()
                          for name in self.fixture.receipt["artifacts_sha256"]}
        _, self.physical_receipt, self.physical_session = self.fixture.physical()
        self.serial = 0

    def captured(self, case, *, runtime="host", profile="bytes16", samples=2, warmup=1,
                 metadata=None, scheduling=None, receipt=None, artifact_changes=None,
                 context=None, calibration="valid", binary_suffix="", qemu_name="qemu",
                 shared=False, failed=False, legacy=False):
        """Run capture/replay unchanged; only the synthetic producer or serial I/O is substituted."""
        self.serial += 1
        name = f"capture-{self.serial}"
        rows = specimens.specimen(case, native=runtime != "host", count=samples, warmup=warmup,
                                  payload="imu-sample" if profile == "imu16" else "bytes16")
        for row in rows:
            row["run_id"] = f"synthetic-independent-run-{self.serial}"
            if row["record"] == "sample":
                row["admission_return_ticks"] = row["start_ticks"] + 80 + self.serial % 20
        schedule = specimens.first(rows, "scheduling")
        same_thread = schedule["sender_tid"] == schedule["receiver_tid"]
        schedule["sender_tid"] = 1000 + 2 * self.serial
        schedule["receiver_tid"] = schedule["sender_tid"] + (not same_thread)
        if scheduling:
            schedule.update(scheduling)
        session = None
        if runtime != "host":
            original = self.physical_receipt if runtime == "physical" else self.fixture.receipt
            record = copy.deepcopy(receipt or original)
            for filename, data in self.artifacts.items():
                (self.fixture.build / filename).write_bytes(data)
            for filename, data in (artifact_changes or {}).items():
                (self.fixture.build / filename).write_bytes(data)
                record["artifacts_sha256"][filename] = capture.digest(self.fixture.build / filename)
                if filename in {"image", "elf", "config"}:
                    record[filename + "_sha256"] = record["artifacts_sha256"][filename]
            record["build_id"] = capture.builder.build_identity(record)
            (self.fixture.build / "build.json").write_text(json.dumps(record))
            rows[0]["build_id"] = record["build_id"]
            if runtime == "qemu":
                rows[0].update(clock_source=1, clock_hz=100_000_000,
                               clock_source_request="baseline")
            else:
                session = copy.deepcopy(self.physical_session)
                session["image_sha256"] = record["image_sha256"]
                session["measurement_session"] = f"synthetic-boot-{self.serial}"
                session.update(context or {})
                reference = session["clock_calibration"]
                reference.update(measurement_session=session["measurement_session"],
                                 reference_identity=f"synthetic reference {self.serial}",
                                 build_id=record["build_id"], image_sha256=record["image_sha256"])
                if calibration == "missing":
                    del session["clock_calibration"]
                elif calibration == "rejected":
                    reference["evidence_sha256"] = "0" * 64
        if metadata:
            rows[0].update(metadata)
        if legacy:
            for key in ("release_period_ns", "warmup_scope", "timer_delay_ns"):
                del rows[0][key]
        arguments = dict(case=case, samples=samples, warmup=warmup, payload=profile,
                         artifact_root=self.fixture.root if shared else None)
        if runtime == "physical":
            source = self.fixture.root / f"{name}-session.json"
            source.write_text(json.dumps(session) + "\n")
            args = self.fixture.arguments(name, runtime="serial", session=source,
                                          port="synthetic", **arguments)
            fd = os.open(os.devnull, os.O_RDWR)
            echo = ("event_path_perf " + " ".join(capture.command_for(
                args.case, args.samples, args.warmup, args.payload)) + "\n").encode()
            replies = iter((b"*** Booting NuttX *** SYNTHETIC TEST ONLY\nnsh> ",
                            echo + fixtures.transcript(rows).encode() + b"nsh> "))

            def read_until(_fd, buffer, *_args):
                data = next(replies)
                buffer.extend(data)
                return data

            with patch.object(capture.console, "serial_open", return_value=(fd, ["synthetic"])), \
                    patch.object(capture.console, "read_until", side_effect=read_until), \
                    patch.object(capture.console, "write_command"), \
                    patch.object(capture.termios, "tcsetattr"), redirect_stdout(io.StringIO()):
                report = capture.capture(args)
        else:
            executable, _ = self.fixture.executable(
                qemu_name if runtime == "qemu" else f"producer-{self.serial}",
                runtime=runtime, records=fixtures.transcript(rows), exit_code=17 if failed else 0)
            if binary_suffix:
                with executable.open("a") as output:
                    output.write(binary_suffix)
            extra = {"qemu": str(executable)} if runtime == "qemu" else {"binary": executable}
            args = self.fixture.arguments(name, runtime=runtime, **extra, **arguments)
            report = capture.capture(args)
        self.assertEqual(report["capture_complete"], not failed, report["error"])
        return args.output

    def cohort(self, group="ready-try", **options):
        return [self.captured(case, **options) for case in GROUPS[group]]

    def replay_complete(self, directories, artifact_root=None):
        for directory in directories:
            result = replay_module.replay(directory, artifact_root)
            self.assertTrue(result["evidence_valid"])
            self.assertTrue(result["capture_complete"])
            self.assertTrue(result["stored_analysis_verified"])

    def assert_unmatched(self, group, directories, artifact_root=None):
        # These failures must belong to cohort matching, not corrupt individual evidence.
        self.replay_complete(directories, artifact_root)
        with self.assertRaises(ValueError):
            comparison.compare(group, directories, artifact_root)

    def assert_unqualified(self, result):
        self.assertTrue(result["matched"])
        for key in ("timing_qualified", "physical_execution_verified", "priority_progress_qualified",
                    "execution_context_verified"):
            self.assertIs(result[key], False)

    def test_all_seven_groups_accept_independent_runs_and_order_members_canonically(self):
        self.assertEqual(comparison.GROUPS, GROUPS)
        for group, cases in GROUPS.items():
            with self.subTest(group=group):
                directories = self.cohort(group)
                self.replay_complete(directories)
                result = comparison.compare(group, list(reversed(directories)))
                self.assertEqual(result["schema"], "event-path-comparison-v1")
                self.assertEqual(result["group"], group)
                self.assertEqual([member["case"] for member in result["members"]], list(cases))
                self.assertEqual(len({member["run_id"] for member in result["members"]}), 3)
                for member, directory in zip(result["members"], directories):
                    retained = json.loads((directory / "analysis.json").read_text())
                    self.assertEqual(member["raw_ticks"], retained["raw_ticks"])
                    self.assertEqual(member["console_sha256"], capture.digest(directory / "console.log"))
                self.assert_unqualified(result)

    def test_same_imu_profile_compares_and_preserves_selection_topology(self):
        directories = self.cohort("ready-selection", profile="imu16")
        self.replay_complete(directories)
        result = comparison.compare("ready-selection", directories)
        self.assertEqual(result["conditions"]["payload"], "imu-sample")
        self.assertEqual(result["conditions"]["capacity"], 16)
        self.assertEqual(len({member["receive_api"] for member in result["members"]}), 3)
        self.assertEqual([member["topology"]["selector_arms"] for member in result["members"]], [0, 1, 3])
        self.assert_unqualified(result)

    def test_group_requires_exactly_one_of_each_named_case(self):
        directories = self.cohort()
        for selected in (directories[:2], [*directories, directories[0]],
                         [directories[0], directories[0], directories[2]]):
            with self.subTest(selected=[path.name for path in selected]):
                self.assert_unmatched("ready-try", selected)
        wrong = self.captured("ready-crossbeam-send")
        self.assert_unmatched("ready-try", [*directories[:2], wrong])
        duplicate_case = self.captured("ready-std-try")
        self.assert_unmatched("ready-try", [directories[0], duplicate_case, directories[2]])
        self.assert_unmatched("ready", directories)

    def test_retained_execution_failure_cannot_join_a_complete_cohort(self):
        directories = self.cohort()
        failed = self.captured("ready-crossbeam-try", failed=True)
        result = replay_module.replay(failed)
        self.assertTrue(result["evidence_valid"])
        self.assertTrue(result["analysis_replayed"])
        self.assertFalse(result["capture_complete"])
        with self.assertRaises(ValueError):
            comparison.compare("ready-try", [*directories[:2], failed])

    def test_comparison_replays_raw_evidence_before_using_stored_summaries(self):
        directories = self.cohort()
        with (directories[2] / "console.log").open("a") as output:
            output.write("changed after capture\n")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            comparison.compare("ready-try", directories)

    def test_different_host_executable_bytes_refuse_even_with_identical_records(self):
        directories = self.cohort()
        different = self.captured("ready-crossbeam-try", binary_suffix="\n# another executable build\n")
        self.assertNotEqual(capture.digest(directories[0] / "binary/event-path-perf"),
                            capture.digest(different / "binary/event-path-perf"))
        self.assert_unmatched("ready-try", [*directories[:2], different])

    def test_profile_samples_and_warmup_must_match(self):
        directories = self.cohort()
        for options in ({"profile": "imu16"}, {"samples": 3}, {"warmup": 2}):
            with self.subTest(options=options):
                different = self.captured("ready-crossbeam-try", **options)
                self.assert_unmatched("ready-try", [*directories[:2], different])

    def test_same_named_payload_with_different_valid_abi_layout_refuses(self):
        directories = self.cohort(profile="imu16")
        different = self.captured("ready-crossbeam-try", profile="imu16",
                                  metadata={"payload_bytes": 44, "payload_alignment": 4})
        self.assert_unmatched("ready-try", [*directories[:2], different])

    def test_valid_host_scheduler_changes_refuse_but_task_ids_do_not(self):
        directories = self.cohort("blocked-try")
        different = self.captured("blocked-crossbeam-try", scheduling={
            "sender_policy": 1, "sender_priority": 20,
            "receiver_policy": 1, "receiver_priority": 30,
        })
        self.assert_unmatched("blocked-try", [*directories[:2], different])
        self.assert_unqualified(comparison.compare("blocked-try", directories))

    def test_legacy_optional_schedule_fields_use_existing_sequential_semantics(self):
        directories = self.cohort()
        legacy = self.captured("ready-crossbeam-try", legacy=True)
        selected = [*directories[:2], legacy]
        self.replay_complete(selected)
        result = comparison.compare("ready-try", selected)
        self.assertEqual(result["conditions"]["warmup_scope"], "sequential-prefix")
        self.assertEqual(result["conditions"]["release_period_ns"], 0)
        self.assertEqual(result["conditions"]["timer_delay_ns"], 0)
        self.assert_unqualified(result)

    def test_target_build_identity_and_independently_hashed_artifacts_must_match(self):
        directories = self.cohort(runtime="qemu")
        record = copy.deepcopy(self.fixture.receipt)
        record["build_nonce"] = "c" * 32
        changed_build = self.captured("ready-crossbeam-try", runtime="qemu", receipt=record)
        self.assert_unmatched("ready-try", [*directories[:2], changed_build])
        for name in ("config", "image", "elf"):
            with self.subTest(artifact=name):
                changed = self.captured("ready-crossbeam-try", runtime="qemu",
                                        artifact_changes={name: b"another valid synthetic artifact\n"})
                # Artifact bytes may change without changing the pre-build identity.
                actual = json.loads((changed / "build.json").read_text())
                self.assertEqual(actual["build_id"], self.fixture.receipt["build_id"])
                self.assert_unmatched("ready-try", [*directories[:2], changed])

    def test_clock_source_frequency_and_resolution_must_match(self):
        directories = self.cohort(runtime="qemu")
        for metadata in ({"clock_source": 2}, {"clock_hz": 120_000_000},
                         {"monotonic_resolution_ns": 1000}, {"counter_native_bits": 64}):
            with self.subTest(metadata=metadata):
                changed = self.captured("ready-crossbeam-try", runtime="qemu", metadata=metadata)
                self.assert_unmatched("ready-try", [*directories[:2], changed])

    def test_host_target_and_emulator_declarations_cannot_be_mixed(self):
        host = self.cohort()
        target = self.cohort(runtime="qemu")
        self.assert_unmatched("ready-try", [*host[:2], target[2]])
        other_emulator = self.captured("ready-crossbeam-try", runtime="qemu", qemu_name="other-qemu")
        self.assert_unmatched("ready-try", [*target[:2], other_emulator])
        self.assert_unqualified(comparison.compare("ready-try", target))

    def test_independent_boots_and_calibration_statuses_remain_unqualified(self):
        directories = [self.captured(case, runtime="physical", calibration=status)
                       for case, status in zip(GROUPS["ready-try"], ("valid", "missing", "rejected"))]
        self.replay_complete(directories)
        sessions = [json.loads((path / "session.json").read_text()) for path in directories]
        self.assertEqual(len({session["measurement_session"] for session in sessions}), 3)
        qualified = [replay_module.replay(path)["clock_calibration_qualified"] for path in directories]
        self.assertEqual(qualified, [True, False, False])
        result = comparison.compare("ready-try", directories)
        self.assertEqual(result["runtime_kind"], "physical")
        self.assertEqual([member["measurement_session"] for member in result["members"]],
                         [session["measurement_session"] for session in sessions])
        self.assertEqual([member["qualification"]["clock_calibration"]["qualified"]
                          for member in result["members"]], [True, False, False])
        self.assertEqual([member["qualification"]["calibration_retention_status"]
                          for member in result["members"]], ["retained", "not-supplied", "retained"])
        for member, directory in zip(result["members"], directories):
            self.assertEqual(member["session_sha256"], capture.digest(directory / "session.json"))
        self.assert_unqualified(result)

    def test_physical_operator_context_must_match_without_certifying_it(self):
        directories = self.cohort(runtime="physical")
        for field, value in (("board", "another synthetic board"), ("board_serial", "other serial"),
                             ("cache_state", "disabled"), ("memory_placement", "declared external RAM"),
                             ("power_management", "enabled"), ("external_load", "synthetic IRQ load"),
                             ("flash_verification", "different operator byte check")):
            with self.subTest(field=field):
                changed = self.captured("ready-crossbeam-try", runtime="physical", context={field: value})
                self.assert_unmatched("ready-try", [*directories[:2], changed])

    def test_shared_archives_compare_after_relocation_only_with_explicit_root(self):
        directories = self.cohort("blocked-selection", runtime="qemu", shared=True)
        temporary = tempfile.TemporaryDirectory(prefix="event-path-comparison-relocated-")
        self.addCleanup(temporary.cleanup)
        destination = Path(temporary.name) / "archive"
        shutil.move(str(self.fixture.root), destination)
        directories = [destination / directory.name for directory in directories]
        self.replay_complete(directories, destination)
        with self.assertRaisesRegex(ValueError, "explicit artifact root"):
            comparison.compare("blocked-selection", directories)
        result = comparison.compare("blocked-selection", directories, destination)
        self.assert_unqualified(result)
        completed = subprocess.run([sys.executable, str(HERE / "compare.py"), "blocked-selection",
                                    *map(str, directories), "--artifact-root", str(destination)],
                                   capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), result)
        refused = subprocess.run([sys.executable, str(HERE / "compare.py"), "blocked-selection",
                                  str(directories[0]), str(directories[0]), str(directories[2]),
                                  "--artifact-root", str(destination)], capture_output=True, text=True)
        self.assertEqual(refused.returncode, 1)
        self.assertNotIn('"matched": true', refused.stdout)


if __name__ == "__main__":
    unittest.main()

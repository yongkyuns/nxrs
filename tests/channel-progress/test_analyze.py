"""Negative controls against false no-repair and source/firmware claims."""
from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


analyze = module("channel_progress_analyze_tests", HERE / "analyze.py")
source_tests = module("channel_progress_source_fixtures", HERE / "test_prepare_crossbeam.py")


def fixture(mode="direct-reserved", *, stalled=True):
    host, selected, reverse = mode == "host-functional", mode.startswith("select-"), mode.startswith("send-")
    common = dict(schema=analyze.SCHEMA, run_id="synthetic-test-run")
    metadata = dict(common, record="metadata", mode=mode, build_id="b" * 64,
                    runtime="linux-functional" if host else "nuttx",
                    transport="crossbeam-channel", transport_version="0.5.17",
                    hook_stage=analyze.MODES[mode], capacity=8, payload_bytes=16,
                    provider_count=1, arm_count=3 if host or selected else 1,
                    selection_bias="stop-important-ordinary" if host or selected else "none",
                    witness_api="functional-smoke" if host else "select_biased" if selected else "send" if reverse else "recv",
                    single_core_tcb_witness=not host)
    priorities = dict(sender=180 if reverse else 120, receiver=120 if reverse else 180,
                      observer=220, watchdog=240)
    for role, priority in priorities.items():
        metadata.update({role + "_policy": 0 if host else 1,
                         role + "_priority": 0 if host else priority})
    metadata.update(waker_policy=1 if selected else -1,
                    waker_priority=160 if selected else -1)
    completed = not host and not stalled
    result = dict(common, record="result", success=True,
                  outcome="host-functional-only" if host else "stall-observed" if stalled else "completed-without-repair",
                  threads_joined=True, watchdog_joined=True, deadline_hit=False,
                  hook_hit_count=0 if host else 1,
                  hook_returned_before_recovery=completed, hook_error=False,
                  consumer_entered=True, producer_entered=True,
                  consumer_done_before_recovery=host or completed,
                  producer_done_before_recovery=host or completed,
                  waker_done_before_recovery=not selected or completed,
                  hook_timestamp_ns=0 if host else 1000,
                  operation_enter_timestamp_ns=0 if host else 2000,
                  observer_timestamp_ns=0 if host else 100_002_000,
                  observation_ns=0 if host else 100_000_000,
                  initial_gate_waits_verified=not host,
                  initial_select_wait_verified=selected,
                  selection_registered_arms=3 if selected else 0,
                  independent_wake_consumed=selected,
                  selected_target_arm="important" if selected else "none",
                  recovery_applied=not host and stalled,
                  delivered=True, messages_sent=10 if host else 9 if reverse else 1,
                  messages_received=10 if host else 9 if reverse else 1,
                  reservation_stall_observed=not host and stalled,
                  completed_without_repair=completed, progress_contract_satisfied=completed,
                  priority_progress_qualified=False, timing_qualified=False)
    for role, priority in (("consumer", priorities["receiver"]), ("producer", priorities["sender"]),
                           ("waker", 160)):
        unused = host or (role == "waker" and not selected)
        result.update({role + "_state_at_deadline": "unavailable" if unused else "ready" if stalled else "absent",
                       role + "_priority_at_deadline": -1 if unused or not stalled else priority})
    return metadata, result


def transcript(metadata, result):
    return "boot banner\n" + "\n".join(analyze.PREFIX + json.dumps(row) for row in (metadata, result)) + "\n"


class TranscriptTests(unittest.TestCase):
    def test_all_six_native_cases_accept_observed_stall_and_completion_separately(self):
        for mode in analyze.CHANNEL_MODES - {"host-functional"}:
            for stalled in (True, False):
                with self.subTest(mode=mode, stalled=stalled):
                    result = analyze.analyze(transcript(*fixture(mode, stalled=stalled)))
                    self.assertTrue(result["fixture_valid"])
                    self.assertEqual(result["progress_contract_satisfied"], not stalled)
                    self.assertEqual(result["completed_without_repair"], not stalled)
                    for field in ("priority_progress_qualified", "physical_timing_qualified",
                                  "physical_execution_verified", "timing_qualified", "hard_realtime_bound"):
                        self.assertFalse(result[field])

    def test_host_functional_never_qualifies_native_contract(self):
        result = analyze.analyze(transcript(*fixture("host-functional")))
        self.assertEqual(result["outcome"], "host-functional-only")
        self.assertFalse(result["progress_contract_satisfied"])
        with self.assertRaisesRegex(ValueError, "no-repair gate"):
            analyze.require_no_repair(result)

    def test_no_repair_requires_bound_target_build(self):
        result = analyze.analyze(transcript(*fixture("direct-published", stalled=False)))
        with self.assertRaisesRegex(ValueError, "no-repair gate"):
            analyze.require_no_repair(result)
        result["build_bound"] = True
        analyze.require_no_repair(result)

    def test_incomplete_duplicated_mixed_and_faulted_transcripts_refused(self):
        text = transcript(*fixture())
        for broken in (text.splitlines()[1], text + text,
                       text.replace("synthetic-test-run", "other", 1),
                       text.replace(analyze.PREFIX, "nsh> " + analyze.PREFIX, 1),
                       text + "panicked at witness.rs:10\n", text + "HardFault\n",
                       text + "CHANNEL_PROGRESS {\"schema\":\"channel-progress-v1\",\"record\":\"fatal\"}\n",
                       text.replace('"record": "metadata"', '"record": "result"', 1)):
            with self.subTest(broken=broken[-100:]), self.assertRaises(ValueError):
                analyze.analyze(broken)

    def test_duplicate_keys_nonfinite_json_and_unknown_schema_refused(self):
        text = transcript(*fixture())
        for broken in (text.replace('"schema":', '"schema":"fake", "schema":', 1),
                       text.replace('"capacity": 8', '"capacity": NaN', 1),
                       text.replace(analyze.SCHEMA, "channel-progress-v0", 1),
                       text.replace('"schema": "channel-progress-v1"', '"schema": null', 1)):
            with self.assertRaises(ValueError):
                analyze.analyze(broken)

    def test_metadata_must_match_exact_intended_experiment(self):
        for field, value in (
            ("mode", "witness-reserved"), ("hook_stage", 2), ("hook_stage", True),
            ("runtime", "linux-functional"), ("capacity", 1), ("payload_bytes", 4),
            ("provider_count", 4), ("arm_count", 3), ("selection_bias", "none-random"),
            ("witness_api", "try_recv"), ("transport_version", "0.5.16"),
            ("transport", "std"), ("single_core_tcb_witness", False),
            ("build_id", "unrecorded-host"), ("sender_policy", 2),
            ("sender_priority", 180), ("receiver_priority", 120),
            ("observer_priority", 100), ("watchdog_priority", 180),
            ("waker_policy", 1),
        ):
            metadata, result = fixture()
            metadata[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                analyze.analyze(transcript(metadata, result))

    def test_missing_runtime_evidence_cannot_be_inferred_from_success(self):
        _, exemplar = fixture("select-reserved")
        for field in exemplar:
            if field in {"schema", "record", "run_id"}:
                continue
            metadata, result = fixture("select-reserved")
            result.pop(field)
            with self.subTest(field=field), self.assertRaises(ValueError):
                analyze.analyze(transcript(metadata, result))

    def test_recovery_or_fabricated_completion_cannot_become_a_pass(self):
        for field, value in (
            ("success", False), ("threads_joined", False), ("watchdog_joined", False),
            ("deadline_hit", True), ("hook_error", True), ("hook_hit_count", 0),
            ("consumer_entered", False), ("producer_entered", False),
            ("consumer_done_before_recovery", True), ("producer_done_before_recovery", True),
            ("hook_returned_before_recovery", True), ("recovery_applied", False),
            ("initial_gate_waits_verified", False), ("delivered", False),
            ("consumer_state_at_deadline", "wait_sem"), ("producer_state_at_deadline", "absent"),
            ("consumer_priority_at_deadline", 100), ("producer_priority_at_deadline", 200),
            ("messages_received", 0), ("messages_sent", 2),
            ("reservation_stall_observed", False), ("completed_without_repair", True),
            ("progress_contract_satisfied", True), ("outcome", "completed-without-repair"),
        ):
            metadata, result = fixture()
            result[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                analyze.analyze(transcript(metadata, result))

    def test_selected_wait_requires_native_registration_and_other_arm_wake(self):
        for field, value in (
            ("initial_select_wait_verified", False), ("selection_registered_arms", 1),
            ("independent_wake_consumed", False), ("selected_target_arm", "ordinary"),
            ("waker_state_at_deadline", "unavailable"), ("waker_priority_at_deadline", 120),
        ):
            metadata, result = fixture("select-reserved")
            result[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                analyze.analyze(transcript(metadata, result))

    def test_completed_selected_case_requires_waker_completion_without_repair(self):
        for field, value in (("waker_done_before_recovery", False), ("recovery_applied", True),
                             ("hook_returned_before_recovery", False)):
            metadata, result = fixture("select-published", stalled=False)
            result[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                analyze.analyze(transcript(metadata, result))

    def test_observation_boundaries_must_follow_real_hook_and_operation_entry(self):
        for field, value in (("hook_timestamp_ns", 0), ("hook_timestamp_ns", 3000),
                             ("operation_enter_timestamp_ns", 100),
                             ("observer_timestamp_ns", 1000), ("observation_ns", 1),
                             ("observation_ns", 100_001_000)):
            metadata, result = fixture()
            result[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                analyze.analyze(transcript(metadata, result))

    def test_target_cannot_certify_physical_timing_or_production_progress(self):
        for role in ("metadata", "result"):
            for field in ("physical_measurement", "physical_timing_qualified",
                          "timing_qualified", "priority_progress_qualified"):
                metadata, result = fixture("direct-published", stalled=False)
                (metadata if role == "metadata" else result)[field] = True
                with self.subTest(role=role, field=field), self.assertRaises(ValueError):
                    analyze.analyze(transcript(metadata, result))

    def test_host_cannot_relabel_smoke_as_native_progress(self):
        for field, value in (("completed_without_repair", True), ("initial_gate_waits_verified", True),
                             ("hook_hit_count", 1), ("selection_registered_arms", 3),
                             ("messages_sent", 1), ("messages_received", 1),
                             ("consumer_state_at_deadline", "ready")):
            metadata, result = fixture("host-functional")
            result[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                analyze.analyze(transcript(metadata, result))


class BuildEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.mode = "select-reserved"
        self.prep = source_tests.prep
        pins = copy.deepcopy(self.prep.PINS)
        inputs = {}
        for name, pin in pins.items():
            files = {"Cargo.toml": f'[package]\nname="{name}"\nversion="{pin["version"]}"\n'.encode(),
                     "src/lib.rs": b"// synthetic unit fixture; no actual source claim\n"}
            if name == "crossbeam-channel":
                files.update({self.prep.ARRAY: source_tests.synthetic_array(),
                              self.prep.SELECT: source_tests.synthetic_select()})
            path = self.root / f"{name}.crate"
            path.write_bytes(source_tests.archive(name, files))
            pins[name]["sha256"] = analyze.sha256(path)
            inputs[name] = path
        for patch in (mock.patch.object(self.prep, "PINS", pins),
                      mock.patch.object(analyze, "PINS", {name: (pin["version"], pin["sha256"])
                                                          for name, pin in pins.items()}),
                      mock.patch.object(analyze, "source_preparer", return_value=self.prep)):
            patch.start()
            self.addCleanup(patch.stop)
        self.source = self.prep.prepare(inputs["crossbeam-channel"], inputs["crossbeam-utils"],
                                        self.root / "crossbeam", "send-reserved")
        for directory in ("firmware", "prepared"):
            (self.root / directory).mkdir()
        (self.root / "firmware/image").write_bytes(b"synthetic unit-test firmware")
        (self.root / "firmware/config").write_text("CONFIG_BUILD_FLAT=y\n# CONFIG_SMP is not set\n")
        cargo = {"schema": "channel-progress-cargo-sources-v1", "sources": {}}
        for name, pin in pins.items():
            src = self.root / "crossbeam/sources" / f"{name}-{pin['version']}/src/lib.rs"
            cargo["sources"][name] = dict(package_id=f"path+{src.parent}#{name}@{pin['version']}",
                                          src_path=str(src), expected_src_path=str(src), source_matches=True,
                                          artifact_sha256={f"firmware/cargo/lib{name}.rlib": "a" * 64})
        self.write("firmware/crossbeam-source.json", cargo)
        std_root = self.root / "prepared/toolchain"
        std_source = str(std_root / "lib/rustlib/src/rust/library/std/src/lib.rs")
        self.write("firmware/std-source.json", dict(std_source=std_source, expected_source=std_source,
                                                    selected_source_matches=True, rustc=str(std_root / "bin/rustc")))
        compiler = dict(version="synthetic compiler", rustc_sha256="c" * 64, driver_sha256={})
        library = {"std/src/sync/mpmc/array.rs": "d" * 64, "std/src/lib.rs": "e" * 64}
        hook = dict(schema="std-channel-hook-v1", mode="latency", hook_stage=0,
                    channel_instrumented=False, compiler=compiler,
                    library_tree_sha256=analyze.canonical(library),
                    original={"sha256": "d" * 64}, selected={"sha256": "d" * 64})
        self.write("prepared/std-channel-hook.json", hook)
        self.write("prepared/std-library-sources.json", library)
        self.write("firmware/std-patch.json", dict(channel_qualification=hook))
        self.receipt = dict(schema="channel-progress-build-v1", status="complete", runtime_kind="unmeasured",
                            mode=self.mode, hook_stage=1, source_revision="f" * 40,
                            platform="mps2-an521-mock", target="thumbv8m.main-nuttx-eabi", sdk="nightly-2025-09-15",
                            source_sha256={"tests/channel-progress/rust/src/main.rs": "a" * 64},
                            staged_inputs_sha256={"workspace/tests/channel-progress/rust/src/main.rs": "a" * 64},
                            kernel_revision="1" * 40, apps_revision="2" * 40, build_nonce="3" * 32,
                            compiler_identity=compiler, std_library_tree_sha256=hook["library_tree_sha256"],
                            std_array_original_sha256="d" * 64, std_array_sha256="d" * 64,
                            crossbeam_receipt=self.source, crossbeam_receipt_file="crossbeam/crossbeam-hook.json",
                            crossbeam_source_proof="firmware/crossbeam-source.json", std_source_proof="firmware/std-source.json",
                            resolved_configuration=dict(CONFIG_BUILD_FLAT="y", CONFIG_SMP="n"),
                            image_file="firmware/image", elf_file="firmware/image", config_file="firmware/config")
        self.refresh()

    def write(self, relative, value):
        (self.root / relative).write_text(json.dumps(value, indent=2) + "\n")

    def refresh(self):
        for key in ("image", "elf", "config"):
            self.receipt[key + "_sha256"] = analyze.sha256(self.root / self.receipt[key + "_file"])
        self.receipt["artifacts_sha256"] = {
            p.relative_to(self.root).as_posix(): analyze.sha256(p)
            for parent in ("crossbeam", "firmware", "prepared")
            for p in (self.root / parent).rglob("*") if p.is_file() and "/sources/" not in str(p)
        }
        self.receipt["build_id"] = analyze.canonical({
            key: self.receipt[key] for key in analyze.identity_fields(self.receipt["mode"])})
        self.write("build.json", self.receipt)

    def summary(self):
        metadata, result = fixture(self.mode)
        metadata["build_id"] = self.receipt["build_id"]
        return analyze.analyze(transcript(metadata, result))

    def test_complete_build_reconstructs_pinned_sources_and_binds_runtime(self):
        receipt = analyze.build_receipt(self.root / "build.json", self.mode)
        bound = analyze.bind_build(self.summary(), receipt)
        self.assertTrue(bound["build_bound"])
        self.assertFalse(bound["priority_progress_qualified"])
        with self.assertRaisesRegex(ValueError, "no-repair gate"):
            analyze.require_no_repair(bound)

    def test_source_validation_survives_relocated_evidence_without_source_trees(self):
        import shutil
        relocated = self.root / "relocated"
        shutil.copytree(self.root, relocated, ignore=shutil.ignore_patterns("relocated", "sources"))
        receipt = analyze.build_receipt(relocated / "build.json", self.mode)
        self.assertEqual(receipt["build_id"], self.receipt["build_id"])

    def test_runtime_build_mismatch_is_refused(self):
        summary = self.summary()
        summary["metadata"]["build_id"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "identities differ"):
            analyze.bind_build(summary, analyze.build_receipt(self.root, self.mode))

    def test_artifact_mutation_is_refused(self):
        (self.root / "firmware/image").write_bytes(b"different firmware")
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            analyze.build_receipt(self.root, self.mode)

    def test_changed_frozen_inputs_cannot_keep_old_build_id(self):
        self.receipt["source_sha256"]["tests/channel-progress/rust/src/main.rs"] = "7" * 64
        self.write("build.json", self.receipt)
        with self.assertRaisesRegex(ValueError, "frozen inputs"):
            analyze.build_receipt(self.root, self.mode)

    def test_self_consistent_source_ledger_forgery_fails_archive_reconstruction(self):
        self.source["crates"]["crossbeam-channel"]["selected_files"]["src/lib.rs"] = "0" * 64
        self.source["crates"]["crossbeam-channel"]["selected_tree_sha256"] = analyze.canonical(
            self.source["crates"]["crossbeam-channel"]["selected_files"])
        self.write("crossbeam/crossbeam-hook.json", self.source)
        self.refresh()
        with self.assertRaisesRegex(ValueError, "archive reconstruction"):
            analyze.build_receipt(self.root, self.mode)

    def test_archive_replacement_cannot_be_legitimized_by_outer_receipt(self):
        (self.root / "crossbeam/original/crossbeam-channel-0.5.17.crate").write_bytes(b"new archive")
        self.refresh()
        with self.assertRaisesRegex(ValueError, "original Crossbeam archive"):
            analyze.build_receipt(self.root, self.mode)

    def test_wrong_actual_cargo_source_refused(self):
        proof = analyze.read_json(self.root / "firmware/crossbeam-source.json")
        proof["sources"]["crossbeam-channel"]["src_path"] = "/shared/cargo/registry/src/lib.rs"
        self.write("firmware/crossbeam-source.json", proof)
        self.refresh()
        with self.assertRaisesRegex(ValueError, "different source path"):
            analyze.build_receipt(self.root, self.mode)

    def test_missing_crate_source_proof_refused(self):
        proof = analyze.read_json(self.root / "firmware/crossbeam-source.json")
        proof["sources"].pop("crossbeam-utils")
        self.write("firmware/crossbeam-source.json", proof)
        self.refresh()
        with self.assertRaisesRegex(ValueError, "package source proof"):
            analyze.build_receipt(self.root, self.mode)

    def test_std_hook_or_different_std_compiler_refused(self):
        proof = analyze.read_json(self.root / "firmware/std-source.json")
        proof["rustc"] = "/shared/rustc"
        self.write("firmware/std-source.json", proof)
        self.refresh()
        with self.assertRaisesRegex(ValueError, "different build cohort"):
            analyze.build_receipt(self.root, self.mode)

    def test_actual_multicore_config_refused_even_with_single_core_summary(self):
        (self.root / "firmware/config").write_text("CONFIG_BUILD_FLAT=y\nCONFIG_SMP=y\n")
        self.refresh()
        with self.assertRaisesRegex(ValueError, "actual firmware config"):
            analyze.build_receipt(self.root, self.mode)

    def test_artifact_path_escape_refused(self):
        self.receipt["artifacts_sha256"] = {"../outside": "0" * 64}
        self.write("build.json", self.receipt)
        with self.assertRaisesRegex(ValueError, "artifact inventory path"):
            analyze.build_receipt(self.root, self.mode)

    def test_fresh_execution_identity_required_without_clock_calibration(self):
        bound = analyze.bind_build(self.summary(), analyze.build_receipt(self.root, self.mode))
        session = dict(schema="channel-progress-session-v1", runtime_kind="qemu", board="mps2-an521",
                       build_id=self.receipt["build_id"], image_sha256=self.receipt["image_sha256"])
        with self.assertRaisesRegex(ValueError, "fresh boot"):
            analyze.attach_execution(bound, session)
        qemu = analyze.attach_execution(bound, session, True)
        self.assertEqual(qemu["runtime_kind"], "qemu")
        self.assertFalse(qemu["physical_execution_verified"])
        self.assertEqual(qemu["execution_evidence"], "virtual")
        session.update(runtime_kind="physical", measurement_session="synthetic-physical-session",
                       board_serial="unit-test", flash_verification="synthetic-readback", active_cores=1)
        physical = analyze.attach_execution(bound, session, True)
        # The same MPS2 emulator build/transcript cannot become hardware proof
        # merely because the caller supplies physical-looking session labels.
        self.assertEqual(bound["build"]["platform"], "mps2-an521-mock")
        self.assertEqual(physical["runtime_kind"], "physical")
        self.assertFalse(physical["physical_execution_verified"])
        self.assertEqual(physical["execution_evidence"], "operator-asserted")
        self.assertIn("not independently verified", physical["physical_evidence_scope"])
        self.assertFalse(physical["physical_timing_qualified"])
        self.assertFalse(physical["priority_progress_qualified"])
        session["image_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "image identity"):
            analyze.attach_execution(bound, session, True)

    def test_cli_writes_valid_negative_diagnosis_before_acceptance_refusal(self):
        metadata, result = fixture(self.mode)
        metadata["build_id"] = self.receipt["build_id"]
        console = self.root / "console.log"
        console.write_text(transcript(metadata, result))
        output = self.root / "analysis.json"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = analyze.main([str(console), "--build-receipt", str(self.root / "build.json"),
                                 "--output", str(output), "--require-no-repair"])
        self.assertEqual(code, 2)
        diagnosis = analyze.read_json(output)
        self.assertEqual(diagnosis["outcome"], "stall-observed")
        self.assertTrue(diagnosis["fixture_valid"])
        self.assertFalse(diagnosis["progress_contract_satisfied"])


if __name__ == "__main__":
    unittest.main()

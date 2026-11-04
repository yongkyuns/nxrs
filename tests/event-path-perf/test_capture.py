"""Capture refusal/retention controls; all executable records here are synthetic."""
import copy
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("event_path_capture_tests", HERE / "capture.py")
capture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capture)


FIXTURE_SPEC = importlib.util.spec_from_file_location(
    "event_path_capture_specimens", HERE / "test_analyze.py")
fixtures = importlib.util.module_from_spec(FIXTURE_SPEC)
FIXTURE_SPEC.loader.exec_module(fixtures)


def fixture(native=False, hardware_counter=False, build_id=None):
    """Reuse the analyzer's synthetic ready case while testing capture identity."""
    rows = fixtures.specimen(native=native, count=2, warmup=1)
    if native:
        rows[0]["build_id"] = build_id or "b" * 64
        if not hardware_counter:
            rows[0].update(clock_source=1, clock_hz=100_000_000,
                           clock_source_request="baseline")
    return rows

def transcript(rows):
    return "".join(capture.analysis.PREFIX + json.dumps(row) + "\n" for row in rows)


FAKE_EXECUTABLE = r'''
import json
import os
from pathlib import Path
import signal
import sys
import time

config = json.loads(Path(__file__).with_suffix('.json').read_text())
state_path = Path(config['state_path'])
state = {'pid': os.getpid(), 'commands': [], 'terminated_by': None}

def save():
    state_path.write_text(json.dumps(state))

def terminate(signum, frame):
    state['terminated_by'] = signum
    save()
    raise SystemExit(0)

signal.signal(signal.SIGTERM, terminate)
save()
if config['runtime'] == 'qemu':
    if config.get('rom_warning'):
        print('SHA-256 comparison failed: SYNTHETIC TEST ONLY', flush=True)
    print('*** Booting NuttX *** SYNTHETIC TEST ONLY\nnsh> ', end='', flush=True)
    state['commands'].append(sys.stdin.readline().rstrip('\r\n'))
    save()
    print('\n', end='', flush=True)
if config.get('mutate'):
    Path(config['mutate']).write_bytes(b'changed by synthetic executable')
print(config.get('records', 'SYNTHETIC partial stdout'), end='', flush=True)
if config.get('stderr'):
    print(config['stderr'], file=sys.stderr, flush=True)
if config.get('timeout'):
    time.sleep(60)
if config['runtime'] == 'qemu':
    print('nsh> ', end='', flush=True)
    while True:
        signal.pause()
raise SystemExit(config.get('exit_code', 0))
'''


class CaptureControls(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="event-path-capture-tests-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.build = self.root / "build"
        self.build.mkdir()
        self.states = []
        self.addCleanup(self.cleanup_children)
        self.receipt = self.write_build()

    def cleanup_children(self):
        for path in self.states:
            if path.exists():
                try:
                    os.kill(json.loads(path.read_text())["pid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def write_build(self):
        for name in ("image", "elf", "config"):
            (self.build / name).write_text(f"SYNTHETIC {name}, not firmware\n")
        compiler = dict(version="synthetic compiler", rustc_sha256="e" * 64,
                        driver_sha256={"synthetic-driver.so": "f" * 64})
        library = {"std/src/lib.rs": "c" * 64, "std/src/sync/mpmc/array.rs": "a" * 64}
        tree_hash = hashlib.sha256(json.dumps(library, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        (self.build / "std-library-sources.json").write_text(json.dumps(library))
        hook = dict(schema="std-channel-hook-v1", mode="latency", hook_stage=0,
                    channel_instrumented=False,
                    compiler=compiler, library_tree_sha256=tree_hash,
                    original={"sha256": "a" * 64}, selected={"sha256": "a" * 64})
        registry = dict(schema="event-path-registry-sources-v1", packages=[
            dict(name=name, version=version, registry_checksum=checksum,
                 uninstrumented_archive_match=True)
            for name, (version, checksum) in capture.builder.REGISTRY_PINS.items()
        ])
        (self.build / "hook.json").write_text(json.dumps(hook))
        (self.build / "registry.json").write_text(json.dumps(registry))
        record = dict(
            schema="event-path-build-v1", status="complete", mode="uninstrumented",
            hook_stage=0, command="event_path_perf", platform="mps2-an521-mock",
            package=capture.builder.PACKAGE, binary=capture.builder.BINARY,
            target="thumbv8m.main-nuttx-eabi", sdk="nightly-2025-09-15",
            source_sha256={"tests/event-path-perf/rust/src/main.rs": "d" * 64},
            build_nonce="b" * 32, clock_request="baseline",
            optimization={"rust_opt_level": "2", "kernel_request": "baseline"},
            build_environment={"CARGO_PROFILE_RELEASE_OPT_LEVEL": "2"},
            std_array_sha256="a" * 64, std_array_original_sha256="a" * 64,
            std_library_tree_sha256=tree_hash, compiler_identity=compiler,
            compiler_version=compiler["version"], kernel_revision="a" * 40, apps_revision="b" * 40,
            hook_receipt="hook.json", crossbeam_source_receipt="registry.json",
            crossbeam=registry,
        )
        for name in ("image", "elf", "config"):
            record[name + "_file"] = name
            record[name + "_sha256"] = capture.digest(self.build / name)
        record["artifacts_sha256"] = {
            name: capture.digest(self.build / name)
            for name in ("image", "elf", "config", "hook.json", "registry.json", "std-library-sources.json")
        }
        record["build_id"] = capture.builder.build_identity(record)
        (self.build / "build.json").write_text(json.dumps(record))
        return record

    def arguments(self, name, runtime="host", **overrides):
        values = dict(runtime=runtime, case="ready-std-try", samples=2, warmup=1,
                      payload="bytes16", output=self.root / name, timeout=None if runtime == "serial" else 3,
                      binary=None, build_directory=self.build, qemu=None,
                      session=None, port=None)
        values.update(overrides)
        return SimpleNamespace(**values)

    def executable(self, name, runtime="host", **configuration):
        path = self.root / name
        path.write_text(f"#!{sys.executable}\n" + FAKE_EXECUTABLE)
        path.chmod(0o755)
        state = self.root / (name + "-state.json")
        self.states.append(state)
        values = dict(runtime=runtime, state_path=str(state),
                      records=transcript(fixture(native=runtime == "qemu", build_id=self.receipt["build_id"])))
        values.update(configuration)
        path.with_suffix(".json").write_text(json.dumps(values))
        return path, state

    def saved(self, args, report):
        self.assertEqual(json.loads((args.output / "capture.json").read_text()), report)
        self.assertEqual(capture.digest(args.output / "console.log"), report["console_sha256"])
        self.assertFalse(report["physical_timing_qualified"])
        self.assertFalse(report["priority_progress_qualified"])
        manifest = json.loads((args.output / "evidence.json").read_text())
        for name, expected in manifest["files_sha256"].items():
            self.assertEqual(capture.digest(args.output / name), expected)

    def test_command_arguments_refuse_injection_and_storage_overflow(self):
        self.assertEqual(capture.command_for("blocked-select-three", 64, 8, "imu16"),
                         ["blocked-select-three", "64", "8", "imu16"])
        for name in ("ready;reboot", "ready\nreboot", "ready std", "$(reboot)", "A", "a" * 65):
            with self.subTest(name=name), self.assertRaises(ValueError):
                capture.command_for(name, 2, 1, "bytes16")
        for samples, warmup, payload in ((0, 0, "bytes16"), (4097, 0, "bytes16"),
                                         (True, 0, "bytes16"), (1, -1, "bytes16"),
                                         (1, 1025, "bytes16"), (1, 0, "imu16;reboot")):
            with self.subTest(values=(samples, warmup, payload)), self.assertRaises(ValueError):
                capture.command_for("ready-std-try", samples, warmup, payload)
        for samples, warmup in ((257, 0), (1, 65)):
            with self.assertRaisesRegex(ValueError, "storage bound"):
                capture.command_for("fanin-std-4", samples, warmup, "bytes16")
        for samples, warmup in ((129, 0), (1, 33)):
            with self.assertRaisesRegex(ValueError, "timer-provider storage"):
                capture.command_for("timer-provider-messaging", samples, warmup, "bytes16")
        for case, samples, payload in (("fanin-messaging-1-saturation", 8, "bytes16"),
                                       ("fanin-messaging-4-saturation", 4, "imu16")):
            with self.assertRaisesRegex(ValueError, "exceed capacity"):
                capture.command_for(case, samples, 0, payload)
        self.assertEqual(capture.command_for("fanin-messaging-1-saturation", 8, 1, "bytes16"),
                         ["fanin-messaging-1-saturation", "8", "1", "bytes16"])

    def test_serial_budget_handles_supported_rows_and_refuses_short_deadlines_before_open(self):
        args = self.arguments("budget", runtime="serial", samples=4096, warmup=0)
        timeout, budget = capture.choose_timeout(args)
        self.assertGreater(timeout, 170)  # Actual 4096-row output needs >170s at 115200 baud.
        self.assertLessEqual(timeout, 600)
        self.assertEqual(budget["retained_rows"], 4096)
        args.timeout = 120
        with patch.object(capture.console, "serial_open") as opened:
            with self.assertRaisesRegex(ValueError, "115200-baud"):
                capture.capture(args)
            opened.assert_not_called()
        self.assertFalse(args.output.exists())
        for runtime in ("host", "qemu"):
            args.runtime, args.timeout = runtime, None
            self.assertEqual(capture.choose_timeout(args), (30, None))

    def test_successful_host_capture_is_functional_only_and_output_is_never_reused(self):
        binary, _ = self.executable("host-pass")
        args = self.arguments("host-pass-capture", binary=binary)
        report = capture.capture(args)
        self.saved(args, report)
        self.assertTrue(report["capture_complete"])
        self.assertEqual(json.loads((args.output / "analysis.json").read_text())["outcome"],
                         "host-functional-only")
        retained = (args.output / "capture.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "output must be new"):
            capture.capture(args)
        self.assertEqual((args.output / "capture.json").read_bytes(), retained)

    def test_failed_host_preserves_both_output_streams_and_failed_receipt(self):
        binary, _ = self.executable("host-failed", records="partial stdout\n",
                                    stderr="distinct stderr", exit_code=17)
        args = self.arguments("host-failed-capture", binary=binary)
        report = capture.capture(args)
        self.saved(args, report)
        self.assertFalse(report["capture_complete"])
        self.assertEqual(report["returncode"], 17)
        self.assertEqual(report["failure_stage"], "execution")
        text = (args.output / "console.log").read_text()
        self.assertIn("partial stdout", text)
        self.assertIn("distinct stderr", text)
        self.assertFalse((args.output / "analysis.json").exists())

    def test_host_deadline_preserves_partial_output_and_reaps_the_process(self):
        binary, state = self.executable("host-timeout", records="partial before deadline\n", timeout=True)
        args = self.arguments("host-timeout-capture", binary=binary, timeout=1)
        report = capture.capture(args)
        self.saved(args, report)
        self.assertFalse(report["capture_complete"])
        self.assertIn("deadline", report["error"])
        self.assertIn("partial before deadline", (args.output / "console.log").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(json.loads(state.read_text())["pid"], 0)

    def test_host_runtime_identity_and_binary_drift_are_rejected(self):
        for name, options, expected in (
                ("wrong-runtime", {"records": transcript(fixture(native=True))}, "host runtime"),
                ("changed-binary", {}, "binary changed")):
            with self.subTest(name=name):
                if name == "changed-binary":
                    options["mutate"] = str(self.root / name)
                binary, _ = self.executable(name, **options)
                args = self.arguments(name + "-capture", binary=binary)
                report = capture.capture(args)
                self.saved(args, report)
                self.assertFalse(report["capture_complete"])
                self.assertIn(expected, report["error"])

    def test_fake_qemu_uses_one_command_and_cannot_promote_rom_warning(self):
        binary, state_path = self.executable("fake-qemu", runtime="qemu", rom_warning=True)
        args = self.arguments("qemu-capture", runtime="qemu", qemu=str(binary))
        report = capture.capture(args)
        self.saved(args, report)
        self.assertTrue(report["capture_complete"], report["error"])
        self.assertTrue(report["boot_rom_digest_warning_observed"])
        self.assertIn("rom-digest-warning", report["timing_qualification_blockers"])
        self.assertFalse(report["physical_execution_verified"])
        self.assertEqual(json.loads((args.output / "session.json").read_text())["runtime_kind"], "qemu")
        state = json.loads(state_path.read_text())
        self.assertEqual(state["commands"], ["event_path_perf ready-std-try 2 1 bytes16"])
        self.assertEqual(state["terminated_by"], signal.SIGTERM)
        with self.assertRaises(ProcessLookupError):
            os.kill(state["pid"], 0)

    def test_firmware_drift_during_execution_is_retained_as_failure(self):
        binary, _ = self.executable("drifting-qemu", runtime="qemu",
                                    mutate=str(self.build / "image"))
        args = self.arguments("drifting-capture", runtime="qemu", qemu=str(binary))
        report = capture.capture(args)
        self.saved(args, report)
        self.assertFalse(report["capture_complete"])
        self.assertIn("artifact", report["error"])
        self.assertTrue((args.output / "session.json").is_file())
        self.assertTrue((args.output / "build.json").is_file())
        self.assertIn(capture.analysis.PREFIX, (args.output / "console.log").read_text())

    def test_failed_serial_identity_preserves_the_supplied_session(self):
        session = dict(schema="event-path-session-v1", runtime_kind="physical",
                       image_sha256="0" * 64, board="synthetic identity refusal")
        source = self.root / "session.json"
        source.write_text(json.dumps(session))
        args = self.arguments("serial-refusal", runtime="serial", session=source,
                              port="/must-not-be-opened")
        report = capture.capture(args)
        self.saved(args, report)
        self.assertFalse(report["capture_complete"])
        self.assertIn("flashed image", report["error"])
        self.assertEqual(json.loads((args.output / "session.json").read_text()), session)

    def test_source_proof_and_artifact_substitution_are_refused(self):
        self.assertEqual(capture.build_receipt(self.build)["build_id"], self.receipt["build_id"])
        for mutation in ("artifact", "std", "hook", "registry", "missing-proof", "compiler-proof", "library"):
            with self.subTest(mutation=mutation):
                record = self.write_build()
                if mutation == "artifact":
                    (self.build / "elf").write_text("substituted image")
                elif mutation == "std":
                    record["std_array_sha256"] = "c" * 64
                elif mutation == "hook":
                    hook = json.loads((self.build / "hook.json").read_text())
                    hook["channel_instrumented"] = True
                    (self.build / "hook.json").write_text(json.dumps(hook))
                    record["artifacts_sha256"]["hook.json"] = capture.digest(self.build / "hook.json")
                elif mutation == "registry":
                    record["crossbeam"]["packages"][0]["uninstrumented_archive_match"] = False
                    (self.build / "registry.json").write_text(json.dumps(record["crossbeam"]))
                    record["artifacts_sha256"]["registry.json"] = capture.digest(self.build / "registry.json")
                elif mutation == "compiler-proof":
                    hook = json.loads((self.build / "hook.json").read_text())
                    hook["compiler"]["version"] = "different compiler proof"
                    (self.build / "hook.json").write_text(json.dumps(hook))
                    record["artifacts_sha256"]["hook.json"] = capture.digest(self.build / "hook.json")
                elif mutation == "library":
                    path = self.build / "std-library-sources.json"
                    library = json.loads(path.read_text())
                    library["std/src/lib.rs"] = "0" * 64
                    path.write_text(json.dumps(library))
                    record["artifacts_sha256"][path.name] = capture.digest(path)
                else:
                    del record["artifacts_sha256"]["hook.json"]
                (self.build / "build.json").write_text(json.dumps(record))
                with self.assertRaises(ValueError):
                    capture.build_receipt(self.build)

    def test_frozen_build_identity_refuses_provenance_relabeling(self):
        for mutation in ("source", "compiler", "optimization", "kernel", "environment", "compiler-alias"):
            with self.subTest(mutation=mutation):
                record = self.write_build()
                if mutation == "source":
                    record["source_sha256"]["tests/event-path-perf/rust/src/main.rs"] = "0" * 64
                elif mutation == "compiler":
                    record["compiler_identity"]["rustc_sha256"] = "0" * 64
                elif mutation == "optimization":
                    record["optimization"]["rust_opt_level"] = "3"
                elif mutation == "kernel":
                    record["kernel_revision"] = "0" * 40
                elif mutation == "environment":
                    record["build_environment"]["CARGO_PROFILE_RELEASE_OPT_LEVEL"] = "3"
                else:
                    record["compiler_version"] = "different displayed compiler"
                (self.build / "build.json").write_text(json.dumps(record))
                with self.assertRaisesRegex(ValueError, "identity"):
                    capture.build_receipt(self.build)

    def test_artifact_paths_are_portable_and_cannot_occupy_receipt_namespace(self):
        for name in ("./image", "../build/image", str(self.build / "image"), "build.json/child"):
            with self.subTest(name=name):
                record = self.write_build()
                record["artifacts_sha256"][name] = record["image_sha256"]
                (self.build / "build.json").write_text(json.dumps(record))
                with self.assertRaisesRegex(ValueError, "relative|namespace"):
                    capture.build_receipt(self.build)

    def test_shared_capture_cannot_follow_an_escaping_build_receipt_symlink(self):
        source = self.build / "build.json"
        outside = self.root / "outside-build.json"
        outside.write_bytes(source.read_bytes())
        source.unlink()
        source.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "escapes"):
            capture.build_receipt(self.build)
        args = self.arguments("symlink-refusal", runtime="qemu", qemu="must-not-execute",
                              artifact_root=self.build, output=self.build / "runs/symlink-refusal")
        with patch.object(capture.subprocess, "Popen") as spawned:
            report = capture.capture(args)
            spawned.assert_not_called()
        self.saved(args, report)
        self.assertFalse(report["capture_complete"])
        self.assertIn("escapes", report["error"])
        self.assertFalse((args.output / "build.json").exists())

    def serial_capture(self, name, receipt, session, after_snapshot=None):
        """Synthetic console only; retain the same real capture/evidence code paths."""
        (self.build / "build.json").write_text(json.dumps(receipt, indent=3) + "\n")
        source = self.root / (name + "-session.json")
        source.write_text(json.dumps(session, indent=3) + "\n\n")
        args = self.arguments(name, runtime="serial", session=source, port="synthetic")
        fd = os.open(os.devnull, os.O_RDWR)
        echo = ("event_path_perf " + " ".join(capture.command_for(
            args.case, args.samples, args.warmup, args.payload)) + "\n").encode()
        responses = iter((b"*** Booting NuttX *** synthetic\nnsh> ",
                          echo + transcript(fixture(native=True, hardware_counter=True,
                                                     build_id=receipt["build_id"])).encode() + b"nsh> "))

        def read_until(_fd, buffer, *_args):
            if after_snapshot is not None:
                after_snapshot()
            data = next(responses)
            buffer.extend(data)
            return data

        with patch.object(capture.console, "serial_open", return_value=(fd, ["synthetic attrs"])), \
                patch.object(capture.console, "read_until", side_effect=read_until), \
                patch.object(capture.console, "write_command"), \
                patch.object(capture.termios, "tcsetattr"):
            report = capture.capture(args)
        self.saved(args, report)
        return args, report

    def test_retained_physical_inputs_reassociate_after_original_build_session_trace_are_removed(self):
        _, receipt, session = self.physical()
        original_trace = self.root / session["clock_calibration"]["evidence_file"]
        original_bytes = original_trace.read_bytes()
        # P0 accepts absolute trace paths that remain inside the session root.
        # The retained bundle must rebase them without reopening the original.
        session["clock_calibration"]["evidence_file"] = str(original_trace)
        args, report = self.serial_capture(
            "retained-physical", receipt, session,
            after_snapshot=lambda: original_trace.write_bytes(b"changed after snapshot"))
        self.assertTrue(report["capture_complete"], report["error"])
        self.assertTrue(report["clock_calibration_qualified"])
        self.assertEqual((args.output / "session.json").read_bytes(), args.session.read_bytes())
        self.assertEqual((args.output / "build.json").read_bytes(), (self.build / "build.json").read_bytes())
        manifest = json.loads((args.output / "evidence.json").read_text())
        retained_trace = args.output / manifest["calibration"]["retained_file"]
        self.assertEqual(retained_trace.read_bytes(), original_bytes)
        shutil.rmtree(self.build)
        args.session.unlink()
        original_trace.unlink()
        retained_receipt = capture.build_receipt(args.output / manifest["build"]["directory"])
        retained_session = json.loads((args.output / "session.json").read_text())
        summary = capture.analysis.analyze((args.output / "console.log").read_text())
        facts = capture.associate(
            summary, retained_receipt, capture.retained_session(retained_session, manifest["calibration"]),
            args.output, True, False)
        self.assertEqual(facts, json.loads((args.output / "analysis.json").read_text())["capture_qualification"])
        self.assertFalse(facts["physical_timing_qualified"])
        (args.output / "build" / retained_receipt["elf_file"]).write_bytes(b"substituted retained ELF")
        with self.assertRaisesRegex(ValueError, "artifact"):
            capture.build_receipt(args.output / "build")

    def test_retention_preserves_wrong_hash_missing_and_malformed_calibration_as_unqualified(self):
        for kind in ("hash", "missing", "malformed", "oversized"):
            with self.subTest(kind=kind):
                _, receipt, session = self.physical()
                if kind == "hash":
                    session["clock_calibration"]["evidence_sha256"] = "0" * 64
                elif kind == "missing":
                    del session["clock_calibration"]
                elif kind == "malformed":
                    session["clock_calibration"] = ["not an object"]
                limit = 1 if kind == "oversized" else capture.MAX_CALIBRATION_BYTES
                with patch.object(capture, "MAX_CALIBRATION_BYTES", limit):
                    args, report = self.serial_capture("calibration-" + kind, receipt, session)
                self.assertTrue(report["capture_complete"], report["error"])
                self.assertFalse(report["clock_calibration_qualified"])
                manifest = json.loads((args.output / "evidence.json").read_text())
                expected = "retained" if kind == "hash" else "not-supplied" if kind == "missing" else "unavailable"
                self.assertEqual(manifest["calibration"]["status"], expected)
                if kind == "hash":
                    self.assertNotEqual(manifest["calibration"]["actual_sha256"], "0" * 64)
                    self.assertTrue((args.output / manifest["calibration"]["retained_file"]).is_file())

    def test_shared_root_avoids_duplicate_build_and_binary_and_refuses_external_inputs(self):
        binary, _ = self.executable("shared-host")
        args = self.arguments("shared-host-run", binary=binary, artifact_root=self.root)
        report = capture.capture(args)
        self.saved(args, report)
        self.assertTrue(report["capture_complete"], report["error"])
        manifest = json.loads((args.output / "evidence.json").read_text())
        self.assertEqual(manifest["host_binary"]["mode"], "shared")
        self.assertEqual(manifest["host_binary"]["file"], binary.name)
        self.assertFalse((args.output / "binary").exists())

        emulator, _ = self.executable("shared-qemu", runtime="qemu")
        args = self.arguments("ignored", runtime="qemu", qemu=str(emulator), artifact_root=self.build,
                              output=self.build / "runs/shared")
        report = capture.capture(args)
        self.saved(args, report)
        self.assertTrue(report["capture_complete"], report["error"])
        manifest = json.loads((args.output / "evidence.json").read_text())
        self.assertEqual(manifest["build"]["mode"], "shared")
        self.assertEqual(manifest["build"]["directory"], ".")
        self.assertEqual(manifest["build"]["capture_directory"], "runs/shared")
        self.assertFalse((args.output / "build").exists())

        args = self.arguments("outside-shared", binary=binary, artifact_root=self.build,
                              output=self.build / "runs/refused")
        report = capture.capture(args)
        self.saved(args, report)
        self.assertFalse(report["capture_complete"])
        self.assertIn("inside artifact root", report["error"])

    def physical(self):
        receipt = dict(self.receipt, clock_request="native-counter")
        receipt["build_id"] = capture.builder.build_identity(receipt)
        summary = capture.analysis.analyze(transcript(fixture(native=True, hardware_counter=True,
                                                            build_id=receipt["build_id"])))
        evidence = self.root / "independent-reference.json"
        evidence.write_text('{"synthetic_validator_control":true,"physical_measurement":false}\n')
        session = dict(
            schema="event-path-session-v1", runtime_kind="physical",
            image_sha256=receipt["image_sha256"], measurement_session="synthetic-session",
            board="synthetic board", board_serial="synthetic serial", cache_state="enabled",
            memory_placement="declared SRAM", power_management="disabled", external_load="none",
            flash_verification="synthetic byte equality", active_cores=1, cpu_clock_hz=240_000_000,
        )
        session["clock_calibration"] = dict(
            schema="std-channel-clock-calibration-v1", method="paired-independent-timebase",
            reference_type="external-instrument", reference_identity="synthetic reference",
            measurement_session=session["measurement_session"], build_id=receipt["build_id"],
            image_sha256=receipt["image_sha256"], reference_hz=1_000_000,
            reference_accuracy_ppm=100, reference_start_ticks=1000,
            reference_end_ticks=1_001_000, cpu_counter_bits=32,
            cpu_start_ticks=1000, cpu_end_ticks=240_001_000,
            evidence_file=evidence.name, evidence_sha256=capture.digest(evidence),
        )
        return summary, receipt, session

    def test_wrong_runtime_build_image_clock_and_boot_associations_fail(self):
        for kind in ("runtime", "build", "image", "clock", "boot", "cpu"):
            with self.subTest(kind=kind):
                summary, receipt, session = self.physical()
                if kind == "runtime":
                    session["runtime_kind"] = "host"
                elif kind == "build":
                    summary["metadata"]["build_id"] = "c" * 64
                elif kind == "image":
                    session["image_sha256"] = "c" * 64
                elif kind == "clock":
                    summary["metadata"]["clock_source_request"] = "baseline"
                elif kind == "cpu":
                    session["cpu_clock_hz"] = 80_000_000
                with self.assertRaises(ValueError):
                    capture.associate(summary, receipt, session, self.root, kind != "boot", False)

    def test_physical_metadata_and_core_placeholders_never_pass(self):
        for field, value in (("board", "unknown"), ("memory_placement", "<replace>"),
                             ("flash_verification", ""), ("active_cores", 2),
                             ("active_cores", True), ("cpu_clock_hz", 0)):
            with self.subTest(field=field, value=value):
                summary, receipt, session = self.physical()
                session[field] = value
                with self.assertRaises(ValueError):
                    capture.associate(summary, receipt, session, self.root, True, False)

    def test_calibration_rejection_preserves_valid_behavior_and_independent_status(self):
        for kind in ("missing", "stale", "hash", "same-clock"):
            with self.subTest(kind=kind):
                summary, receipt, session = self.physical()
                if kind == "missing":
                    del session["clock_calibration"]
                elif kind == "stale":
                    session["clock_calibration"]["measurement_session"] = "different-session"
                elif kind == "hash":
                    session["clock_calibration"]["evidence_sha256"] = "0" * 64
                else:
                    session["clock_calibration"]["reference_type"] = "cpu-derived-timer"
                before = copy.deepcopy(summary)
                facts = capture.associate(summary, receipt, session, self.root, True, False)
                self.assertEqual(summary, before)
                self.assertTrue(summary["structural_valid"])
                self.assertFalse(facts["physical_execution_verified"])
                self.assertFalse(facts["clock_calibration_qualified"])
                self.assertEqual(facts["clock_calibration"]["status"], "missing" if kind == "missing" else "rejected")
                self.assertFalse(facts["physical_timing_qualified"])

    def test_operator_session_and_valid_calibration_do_not_verify_physical_execution(self):
        summary, receipt, session = self.physical()
        original_summary, original_session = copy.deepcopy(summary), copy.deepcopy(session)
        facts = capture.associate(summary, receipt, session, self.root, True, False)
        self.assertEqual(summary, original_summary)
        self.assertEqual(session, original_session)
        self.assertTrue(summary["structural_valid"])
        self.assertEqual(facts["runtime_kind"], "physical")
        self.assertEqual(facts["hardware_identity_origin"], "operator-supplied-session-and-image-receipt")
        self.assertTrue(facts["fresh_boot_observed"])
        self.assertEqual(facts["clock_calibration"]["status"], "qualified")
        self.assertTrue(facts["clock_calibration_qualified"])
        self.assertFalse(facts["physical_execution_verified"])
        self.assertFalse(facts["physical_timing_qualified"])
        self.assertFalse(facts["priority_progress_qualified"])

        # A complete synthetic console can supply every accepted label and
        # calibration field. Persisting those facts must retain their provenance.
        args, report = self.serial_capture("operator-evidence-only", receipt, session)
        self.assertTrue(report["capture_complete"], report["error"])
        self.assertEqual(report["runtime_kind"], "physical")
        self.assertTrue(report["clock_calibration_qualified"])
        self.assertFalse(report["physical_execution_verified"])
        stored = json.loads((args.output / "analysis.json").read_text())
        self.assertTrue(stored["structural_valid"])
        self.assertEqual(stored["capture_qualification"], facts)

    def test_valid_clock_never_removes_p0_review_or_rom_warning_gate(self):
        summary, receipt, session = self.physical()
        facts = capture.associate(summary, receipt, session, self.root, True, True)
        self.assertTrue(facts["clock_calibration_qualified"])
        self.assertFalse(facts["physical_execution_verified"])
        self.assertFalse(facts["physical_timing_qualified"])
        self.assertIn("rom-digest-warning", facts["timing_qualification_blockers"])
        self.assertIn("P0-matched-physical-cohort-review-required", facts["timing_qualification_blockers"])

    def test_serial_restore_failure_still_saves_raw_session_and_failed_report(self):
        summary, receipt, session = self.physical()
        (self.build / "build.json").write_text(json.dumps(receipt))
        source = self.root / "physical-session.json"
        source.write_text(json.dumps(session))
        args = self.arguments("serial-restore", runtime="serial", session=source, port="synthetic")
        fd = os.open(os.devnull, os.O_RDWR)
        echo = ("event_path_perf " + " ".join(capture.command_for(
            args.case, args.samples, args.warmup, args.payload)) + "\n").encode()
        responses = iter((b"*** Booting NuttX *** synthetic\nnsh> ",
                          echo + transcript(fixture(native=True, hardware_counter=True,
                                                     build_id=receipt["build_id"])).encode() + b"nsh> "))

        def read_until(_fd, buffer, *_args):
            data = next(responses)
            buffer.extend(data)
            return data

        with patch.object(capture.console, "serial_open", return_value=(fd, ["synthetic attrs"])), \
                patch.object(capture.console, "read_until", side_effect=read_until), \
                patch.object(capture.console, "write_command"), \
                patch.object(capture.termios, "tcsetattr", side_effect=OSError(errno.EIO, "synthetic disconnect")):
            report = capture.capture(args)
        self.saved(args, report)
        self.assertFalse(report["capture_complete"])
        self.assertEqual(report["failure_stage"], "cleanup", report["error"])
        self.assertIn("serial restore", report["error"])
        self.assertEqual(json.loads((args.output / "session.json").read_text()), session)
        self.assertIn(capture.analysis.PREFIX, (args.output / "console.log").read_text())
        with self.assertRaises(OSError):
            os.fstat(fd)


if __name__ == "__main__":
    unittest.main()

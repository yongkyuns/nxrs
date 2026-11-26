"""Negative controls for evidence admission. Generated records are parser fixtures,
not target captures or measurements. Actual cases run through `workflow.py host`.
"""
import copy
from argparse import Namespace
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("owner_qualification_workflow", Path(__file__).with_name("workflow.py"))
W = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(W)
TOKEN = "parser-control-001"
BUILD_ID = "a" * 64


def suite_inputs(native, case="std-disconnect"):
    suite = (case.removeprefix("std-") if case in W.MUTEX_CASES else "owner") if native else "host-all"
    return {"kind": "nuttx-target" if native else "host-functional", "suite": suite,
            "cases": list(W.SUITES[suite]), "priority_inheritance": suite.startswith("mutex-") if native else None,
            "pthread_mutex_default_protocol": {"mutex-inherit": "inherit", "mutex-no-inherit": "none"}.get(suite)}


def report(case, native=False, build_id=BUILD_ID):
    participants = []
    for index, (role, priority) in enumerate(W.ROLES[case], 1):
        before = {"tid": index, "policy": 1, "priority": 100}
        start = {"tid": index, "policy": 1, "priority": priority}
        if not native:
            before = start = {"tid": None, "policy": None, "priority": None}
        participants.append({"role": role, "requested_priority": priority,
                             "before": before, "start": start,
                             "end": copy.deepcopy(start), "restored": copy.deepcopy(before)})
    observed = dict(zip(W.FIELDS, W.CASES[case]))
    observed.update(participants=participants, native_waits=W.CASES[case][-1] if native else 0,
                    important_observations=0, ordinary_observations=0, fairness_links_before_ordinary=0,
                    deadline_observations=0, failure_observations=0, stop_requests=0, endpoints_closed=True)
    if case == "owner-independent-inputs":
        observed.update(important_observations=1, ordinary_observations=2, stop_requests=1)
    if case == "owner-fairness-deadline":
        observed.update(fairness_links_before_ordinary=7, deadline_observations=1, stop_requests=2)
    if case == "owner-failure-recovery":
        observed.update(failure_observations=3, stop_requests=2)
    if case in W.MUTEX_CASES:
        inherit = case == "std-mutex-inherit"
        evidence = None
        if native:
            def state(role, effective=None, waiting=False):
                person = next(p for p in participants if p["role"] == role)
                return {"tid": person["start"]["tid"], "policy": 1,
                        "base_priority": person["requested_priority"],
                        "effective_priority": effective or person["requested_priority"],
                        "state": 6 if waiting else 3, "waiting_semaphore": waiting, "runnable": not waiting}
            evidence = {"configuration": {"priority_inheritance": True, "default_prio_inherit": inherit,
                        "default_protocol": 1 if inherit else 0, "protocol_none": 0, "protocol_inherit": 1},
                        "low_before_high": state("mutex-low", waiting=True),
                        "medium_before_high": state("mutex-medium", waiting=True),
                        "high_wait": state("mutex-high", waiting=True),
                        "low_while_high_waits": state("mutex-low", 180 if inherit else 120, waiting=True),
                        "low_ready": state("mutex-low", 180 if inherit else 120),
                        "medium_ready": state("mutex-medium"),
                        "low_before_unlock": state("mutex-low", 180 if inherit else 120),
                        "low_after_unlock": state("mutex-low")}
        observed["mutex"] = {"expected_inheritance": inherit, "native": evidence,
                             "order": dict(zip(("low_unlock", "high_acquire", "medium_start", "medium_finish"),
                                               (1, 2, 3, 4) if inherit else (3, 4, 1, 2))),
                             "high_wait_us": 100, "medium_window_us": 25000, "medium_elapsed_us": 25000,
                             "medium_iterations": 100, "high_completed_at_medium_start": inherit,
                             "high_completed_at_medium_finish": inherit, "final_value": 42}
    return {"schema": "owner-qualification-v1", "case": case, "token": TOKEN, "build_id": build_id,
            "domain": "nuttx-target" if native else "host-functional", "pass": True,
            "native_scheduling": native, "fifo_policy": 1 if native else None,
            "physical_execution_verified": False, "timing_qualified": False, "production_qualified": False,
            "quiescence_basis": "all-fixture-workers-joined", "observations": observed}


def transcript(value, physical=False):
    case, build_id, token = value["case"], value["build_id"], value["token"]
    text = (f"OWNER_QUALIFICATION_BEGIN {build_id} {case} {token}\n"
            + "OWNER_QUALIFICATION_RESULT " + json.dumps(value) + "\n"
            + f"OWNER_QUALIFICATION_END {build_id} {case} {token}\n")
    if physical:
        text = (f"NuttShell (NSH) NuttX-parser-fixture\nnsh> owner_qualification {case} {token}\n" + text
                + f"nsh> echo OQ_STATUS_{token}:$?\nOQ_STATUS_{token}:0\nnsh> ")
    return text


class ParserControls(unittest.TestCase):
    def parse(self, value, text=None, physical=None):
        native = value["domain"] == "nuttx-target"
        build = {"build_id": BUILD_ID, "inputs": suite_inputs(native, value["case"])}
        return W.parse(transcript(value, native) if text is None else text,
                       build, value["case"], TOKEN, native if physical is None else physical)

    def test_every_case_has_distinct_host_and_target_domains(self):
        for case in W.CASES:
            for native in (False, True):
                with self.subTest(case=case, native=native):
                    value = report(case, native)
                    self.assertEqual(self.parse(value), value)

    def test_rejects_missing_work_join_wait_or_endpoint_retirement(self):
        base = report("owner-independent-inputs", True)
        for key in ("attempted", "admitted", "handled", "joined", "required_waits", "native_waits", "ordinary_observations", "important_observations", "stop_requests"):
            with self.subTest(field=key):
                value = copy.deepcopy(base)
                value["observations"][key] -= 1
                with self.assertRaises(ValueError): self.parse(value)
        base["observations"]["endpoints_closed"] = False
        with self.assertRaises(ValueError): self.parse(base)

    def test_rejects_priority_drift_missing_restore_or_unaccounted_participant(self):
        base = report("owner-independent-inputs", True)
        for phase, field, invalid in (("end", "priority", 181), ("start", "policy", 2),
                                      ("restored", "priority", 180), ("end", "tid", 99)):
            with self.subTest(phase=phase, field=field):
                value = copy.deepcopy(base)
                value["observations"]["participants"][0][phase][field] = invalid
                with self.assertRaises(ValueError): self.parse(value)
        for remove in (True, False):
            value = copy.deepcopy(base)
            people = value["observations"]["participants"]
            if remove: people.pop()
            else: people.append(copy.deepcopy(people[0]))
            with self.assertRaises(ValueError): self.parse(value)

    def test_host_cannot_acquire_native_or_physical_evidence(self):
        base = report("std-disconnect")
        for edit in (lambda v: v.update(native_scheduling=True), lambda v: v.update(fifo_policy=1),
                     lambda v: v["observations"].update(native_waits=1),
                     lambda v: v["observations"]["participants"][0]["start"].update(priority=180)):
            value = copy.deepcopy(base)
            edit(value)
            with self.assertRaises(ValueError): self.parse(value)
        with self.assertRaises(ValueError): self.parse(base, transcript(base, True), physical=True)

    def test_claims_and_counts_have_strict_types(self):
        for field in ("physical_execution_verified", "production_qualified", "timing_qualified"):
            value = report("std-disconnect")
            value[field] = True
            with self.assertRaises(ValueError): self.parse(value)
        value = report("std-disconnect")
        value["observations"]["joined"] = True
        with self.assertRaises(ValueError): self.parse(value)
        value = report("owner-fairness-deadline")
        value["observations"]["fairness_links_before_ordinary"] = -1
        with self.assertRaises(ValueError): self.parse(value)

    def test_framing_nonce_duplicates_order_and_late_failures(self):
        value = report("std-disconnect", True)
        text = transcript(value, True)
        begin, end = [line for line in text.splitlines() if line.startswith("OWNER_QUALIFICATION_") and "RESULT" not in line]
        invalid = [text.replace(TOKEN, "unbound-token"), text + "\n" + begin,
                   text + "\nOWNER_QUALIFICATION_BEGIN foreign case wrong-token\n",
                   text.replace(begin, end, 1), text + "\nPANIC: late watchdog\n",
                   text + "\nassertion failed after result\n", text + "\x00",
                   text.replace('"pass": true', '"pass": true, "pass": true')]
        for data in invalid:
            with self.subTest(tail=data[-80:]), self.assertRaises(ValueError): self.parse(value, data)

    def test_fresh_boot_and_unambiguous_terminal_success_required(self):
        value = report("std-disconnect", True)
        text = transcript(value, True)
        for data in (text.replace("NuttShell (NSH)", "stale session"), text + "\nNuttShell (NSH) NuttX-reboot\n",
                     text.replace(f"OQ_STATUS_{TOKEN}:0", f"OQ_STATUS_{TOKEN}:1"),
                     text + f"\nOQ_STATUS_{TOKEN}:1\n", text.removesuffix("nsh> "),
                     text.replace("nsh> owner_qualification", "nsh> another_command")):
            with self.subTest(tail=data[-80:]), self.assertRaises(ValueError): self.parse(value, data)

    def test_esp_loader_banner_without_shell_greeting(self):
        value = report("std-disconnect", True)
        greeting, text = transcript(value, True).split("\n", 1)
        loader = "*** Booting NuttX ***\n"
        for boot in (loader, loader + greeting + "\n"):
            self.assertTrue(self.parse(value, boot + text)["pass"])
        for data in (loader * 2 + text, loader + text + "\n" + loader):
            with self.assertRaisesRegex(ValueError, "missing or repeated fresh shell boot"):
                self.parse(value, data)

    def test_mutex_requires_actual_donation_and_restoration(self):
        base = report("std-mutex-inherit", True)
        for phase, field, invalid in (("low_before_high", "effective_priority", 180),
                                     ("high_wait", "waiting_semaphore", False),
                                     ("high_wait", "tid", 99),
                                     ("low_while_high_waits", "effective_priority", 120),
                                     ("low_while_high_waits", "base_priority", 180),
                                     ("low_ready", "runnable", False),
                                     ("medium_ready", "runnable", False),
                                     ("low_before_unlock", "effective_priority", 120),
                                     ("low_after_unlock", "effective_priority", 180)):
            with self.subTest(phase=phase, field=field):
                value = copy.deepcopy(base)
                value["observations"]["mutex"]["native"][phase][field] = invalid
                with self.assertRaises(ValueError): self.parse(value)

    def test_mutex_protocol_and_interference_controls_are_required(self):
        for case in W.MUTEX_CASES:
            base = report(case, True)
            mutations = [lambda m: m["native"]["configuration"].update(priority_inheritance=False),
                         lambda m: m["native"]["configuration"].update(default_prio_inherit=not m["expected_inheritance"]),
                         lambda m: m["native"]["configuration"].update(default_protocol=99),
                         lambda m: m["order"].update(low_unlock=m["order"]["high_acquire"]),
                         lambda m: m.update(high_completed_at_medium_finish=not m["expected_inheritance"]),
                         lambda m: m.update(medium_elapsed_us=24999),
                         lambda m: m.update(medium_iterations=0), lambda m: m.update(final_value=0)]
            for index, mutate in enumerate(mutations):
                with self.subTest(case=case, mutation=index):
                    value = copy.deepcopy(base)
                    mutate(value["observations"]["mutex"])
                    with self.assertRaises(ValueError): self.parse(value)
        value = report("std-mutex-no-inherit", True)
        value["observations"]["mutex"]["native"]["low_while_high_waits"]["effective_priority"] = 180
        with self.assertRaises(ValueError): self.parse(value)

    def test_host_mutex_cannot_claim_native_priority_inheritance(self):
        value = report("std-mutex-inherit")
        value["observations"]["mutex"]["native"] = report("std-mutex-inherit", True)["observations"]["mutex"]["native"]
        with self.assertRaises(ValueError): self.parse(value)
        # Host scheduling may produce either ordering; no FIFO conclusion is admitted.
        value = report("std-mutex-no-inherit")
        value["observations"]["mutex"]["order"] = report("std-mutex-inherit")["observations"]["mutex"]["order"]
        self.assertEqual(self.parse(value), value)

    def test_mutex_cannot_run_against_other_suite_or_legacy_build(self):
        value = report("std-mutex-inherit", True)
        for specification in (suite_inputs(True), suite_inputs(True, "std-mutex-no-inherit"), {"kind": "nuttx-target"}):
            with self.subTest(specification=specification), self.assertRaises(ValueError):
                W.parse(transcript(value), {"build_id": BUILD_ID, "inputs": specification}, value["case"], TOKEN)


class BuildSuiteControls(unittest.TestCase):
    def test_config_requires_flat_single_core_and_both_protocol_settings(self):
        for case in ("std-disconnect", *W.MUTEX_CASES):
            specification = suite_inputs(True, case)
            enabled = ["BUILD_FLAT"]
            if specification["priority_inheritance"]: enabled.append("PRIORITY_INHERITANCE")
            if case == "std-mutex-inherit": enabled.append("PTHREAD_MUTEX_DEFAULT_PRIO_INHERIT")
            def config(symbols): return "".join(f"CONFIG_{symbol}=y\n" for symbol in symbols)
            W.validate_config(config(enabled), specification)
            for symbol in ("BUILD_FLAT", "SMP", "PRIORITY_INHERITANCE", "PTHREAD_MUTEX_DEFAULT_PRIO_INHERIT"):
                changed = set(enabled) ^ {symbol}
                with self.subTest(case=case, symbol=symbol), self.assertRaises(ValueError):
                    W.validate_config(config(changed), specification)

    def test_build_identity_cannot_relabel_suite_case_inventory_or_protocol(self):
        for field, value in (("suite", "owner"), ("suite", "host-all"), ("cases", list(W.CASES)),
                             ("priority_inheritance", False), ("pthread_mutex_default_protocol", "none")):
            specification = suite_inputs(True, "std-mutex-inherit")
            specification[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                W.selected_cases(specification)


class RetainedEvidenceControls(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def build(self, native=False, case="std-disconnect", profile=None):
        root = self.root / "input-build"
        root.mkdir()
        source = root / "retained/sources/tests/owner-qualification/src/main.rs"
        source.parent.mkdir(parents=True)
        source.write_text("// synthetic evidence-validator input, never executed\n")
        specification = {**suite_inputs(native, case),
                         "profile": (profile or "esp32s3-ram-footprint") if native else None,
                         "sources": {"tests/owner-qualification/src/main.rs": W.digest(source)}}
        build_id = W.identity(specification)
        artifacts = root / "retained/artifacts"
        artifacts.mkdir()
        (artifacts / "firmware.elf").write_bytes(b"\x7fELF parser input only " + build_id.encode())
        if native:
            (artifacts / "firmware.bin").write_bytes(b"parser input only " + build_id.encode())
            (artifacts / "frontend").mkdir()
            config = "CONFIG_BUILD_FLAT=y\n"
            if specification["priority_inheritance"]: config += "CONFIG_PRIORITY_INHERITANCE=y\n"
            if specification["pthread_mutex_default_protocol"] == "inherit": config += "CONFIG_PTHREAD_MUTEX_DEFAULT_PRIO_INHERIT=y\n"
            (artifacts / "frontend/resolved.config").write_text(config)
        records = {str(p.relative_to(root)): {"sha256": W.digest(p), "bytes": p.stat().st_size}
                   for p in artifacts.rglob("*") if p.is_file()}
        value = {"schema": "owner-qualification-build-v1", "build_id": build_id, "inputs": specification,
                 "artifacts": records, "executable": "retained/artifacts/firmware.elf",
                 "image": "retained/artifacts/firmware.bin" if native else None,
                 "physical_execution_verified": False}
        W.save(root / "build.json", value)
        return root, value

    def run_record(self, native=False, profile=None):
        build_root, build = self.build(native, profile=profile)
        root = self.root / "run"
        root.mkdir()
        shutil.copytree(build_root, root / "build")
        value = report("std-disconnect", native, build["build_id"])
        (root / "console.log").write_text(transcript(value, native))
        capture = {"schema": "owner-qualification-capture-v1", "case": value["case"], "token": TOKEN,
                   "build_directory": "build", "build_sha256": W.digest(root / "build/build.json"),
                   "console_sha256": W.digest(root / "console.log"),
                   "runtime": "physical-operator-asserted" if native else "host", "exit_code": 0,
                   "capture_complete": True, "physical_execution_verified": False}
        if native:
            W.save(root / "session.json", {"schema": "owner-qualification-session-v1", "case": value["case"],
                   "token": TOKEN, "build_id": build["build_id"],
                   "image_sha256": build["artifacts"][build["image"]]["sha256"],
                   "fresh_boot": True, "flash_verified": True, "measurement_session": "synthetic-parser-control"})
            capture["session_sha256"] = W.digest(root / "session.json")
        W.save(root / "capture.json", capture)
        W.save(root / "analysis.json", value)
        return root, capture

    def test_inspect_validates_selected_paths_and_cases(self):
        root, value = self.build(True)
        result = W.inspect(root)
        self.assertEqual(result["build_id"], value["build_id"])
        self.assertEqual(result["case_count"], 7)
        self.assertEqual(result["cases"], list(W.OWNER_CASES))
        self.assertEqual(result["image"]["sha256"], W.digest(Path(result["image"]["path"])))
        self.assertFalse(result["physical_execution_verified"])
        (root / value["image"]).write_bytes(b"changed image")
        with self.assertRaises(ValueError): W.inspect(root)

    def test_source_identity_and_symlink_containment(self):
        root, _ = self.build()
        source = root / "retained/sources/tests/owner-qualification/src/main.rs"
        original = source.read_bytes()
        source.write_bytes(b"different source")
        with self.assertRaises(ValueError): W.validate_build(root)
        source.unlink()
        outside = self.root / "outside.rs"
        outside.write_bytes(original)
        source.symlink_to(outside)
        with self.assertRaises(ValueError): W.validate_build(root)
        with self.assertRaises(ValueError): W.contained(root, "../outside.rs")

    def test_replay_rehashes_console_and_rejects_resealed_late_failure(self):
        root, capture = self.run_record()
        self.assertEqual(W.replay(root)["observations"]["joined"], 1)
        with (root / "console.log").open("a") as stream: stream.write("\nPANIC after success\n")
        with self.assertRaises(ValueError): W.replay(root)
        capture["console_sha256"] = W.digest(root / "console.log")
        W.save(root / "capture.json", capture)
        with self.assertRaises(ValueError): W.replay(root)

    def test_retained_failure_is_never_promoted(self):
        root, capture = self.run_record()
        capture["capture_complete"] = False
        W.save(root / "capture.json", capture)
        with self.assertRaises(ValueError): W.replay(root)

    def test_physical_session_must_bind_same_image_case_and_token(self):
        root, capture = self.run_record(True)
        self.assertEqual(W.replay(root)["native_scheduling"], True)
        session = json.loads((root / "session.json").read_text())
        session["image_sha256"] = "0" * 64
        W.save(root / "session.json", session)
        capture["session_sha256"] = W.digest(root / "session.json")
        W.save(root / "capture.json", capture)
        with self.assertRaises(ValueError): W.replay(root)

    def test_saved_analysis_cannot_replace_fresh_replay(self):
        root, _ = self.run_record()
        value = json.loads((root / "analysis.json").read_text())
        value["observations"]["joined"] = 999
        W.save(root / "analysis.json", value)
        with self.assertRaises(ValueError): W.replay(root)

    def test_mutex_build_rechecks_retained_resolved_protocol(self):
        root, value = self.build(True, "std-mutex-inherit")
        self.assertEqual(W.inspect(root)["cases"], ["std-mutex-inherit"])
        name = "retained/artifacts/frontend/resolved.config"
        config = root / name
        config.write_text("CONFIG_BUILD_FLAT=y\nCONFIG_PRIORITY_INHERITANCE=y\n")
        value["artifacts"][name] = {"sha256": W.digest(config), "bytes": config.stat().st_size}
        W.save(root / "build.json", value)
        with self.assertRaisesRegex(ValueError, "resolved pthread mutex default"):
            W.validate_build(root)

    def test_qemu_requires_fresh_boot_native_profile_and_distinct_receipt(self):
        root, capture = self.run_record(True, "mps2-an521-mock")
        capture.update(runtime="qemu", machine="mps2-an521", qemu_version="synthetic-parser-control",
                       qemu_sha256="b" * 64, exit_code=0)
        W.save(root / "capture.json", capture)
        self.assertFalse(W.replay(root)["physical_execution_verified"])
        for field, invalid in (("machine", "esp32s3"), ("exit_code", 1), ("exit_code", False),
                               ("qemu_version", ""), ("qemu_sha256", "missing"),
                               ("physical_execution_verified", True), ("runtime", "host")):
            changed = {**capture, field: invalid}
            W.save(root / "capture.json", changed)
            with self.subTest(field=field), self.assertRaises(ValueError): W.replay(root)
        W.save(root / "capture.json", capture)
        console = root / "console.log"
        console.write_text(console.read_text().replace("NuttShell (NSH)", "old console"))
        capture["console_sha256"] = W.digest(console)
        W.save(root / "capture.json", capture)
        with self.assertRaisesRegex(ValueError, "fresh shell boot"):
            W.replay(root)

    def test_uf2_identity_may_cross_payload_blocks(self):
        data = b"x" * 240 + BUILD_ID.encode() + b"z" * (512 - 304)
        image = bytearray()
        for index in range(2):
            block = bytearray(512)
            block[:8] = b"UF2\n\x57\x51\x5d\x9e"
            block[12:16] = (0x10000000 + index * 256).to_bytes(4, "little")
            block[16:20] = (256).to_bytes(4, "little")
            block[32:288] = data[index * 256:(index + 1) * 256]
            block[508:] = b"\x30\x6f\xb1\x0a"
            image.extend(block)
        path = self.root / "firmware.uf2"
        path.write_bytes(image)
        self.assertNotIn(BUILD_ID.encode(), image)
        self.assertTrue(W.contains_runtime_identity(path, BUILD_ID))
        path.write_bytes(image[:-1])
        with self.assertRaises(ValueError): W.contains_runtime_identity(path, BUILD_ID)

    def capture_control(self, failure=None):
        build_root, build = self.build(True)
        case = "std-disconnect"
        session = self.root / "session-input.json"
        W.save(session, {"schema": "owner-qualification-session-v1",
                        "measurement_session": "synthetic-serial-control-no-hardware",
                        "case": case, "token": TOKEN, "build_id": build["build_id"],
                        "image_sha256": build["artifacts"][build["image"]]["sha256"],
                        "fresh_boot": True, "flash_verified": True})
        value = report(case, True, build["build_id"])
        commands = []

        class Port:
            def __init__(self, pending): self.pending = pending
            def close(self): pass
            def read(self, _size):
                result = self.pending.pop(0) if self.pending else b""
                if isinstance(result, Exception): raise result
                return result
            def write(self, data):
                commands.append(data)
                if len(commands) == 1:
                    if failure == "disconnect":
                        self.pending.append(OSError("synthetic USB removal after submission"))
                    else:
                        self.pending.append(data + transcript(value).encode() + b"nsh> ")
                else:
                    self.pending.append(data + f"OQ_STATUS_{TOKEN}:0\r\nnsh> ".encode())
                    if failure == "late-panic":
                        self.pending.append(b"\r\nPANIC: synthetic late failure\r\n")

        # First open sees the USB device absent; the next connection disappears
        # before any command; the final connection delivers one fresh boot.
        connections = [OSError("synthetic device absent"), Port([OSError("synthetic reset")]),
                       Port([b"NuttShell (NSH) NuttX-parser-fixture\r\nnsh> "])]
        def connect(*_args, **_kwargs):
            connection = connections.pop(0)
            if isinstance(connection, Exception): raise connection
            return connection
        clock = [0.0]
        def now():
            clock[0] += 0.25
            return clock[0]
        args = Namespace(build=build_root, session=session, case=case, port="not-a-device",
                         out=self.root / "capture")
        with patch.dict(sys.modules, {"serial": types.SimpleNamespace(Serial=connect)}), \
             patch.object(W.time, "monotonic", side_effect=now), patch.object(W.time, "sleep"), \
             patch("builtins.print"):
            if failure:
                with self.assertRaises(ValueError): W.capture(args)
            else: W.capture(args)
        receipt = W.unique_json((args.out / "capture.json").read_text())
        self.assertEqual(receipt["pre_command_reconnects"], 2)
        self.assertIs(receipt["capture_complete"], failure is None)
        self.assertIs(receipt["physical_execution_verified"], False)
        self.assertEqual(commands[0], f"owner_qualification {case} {TOKEN}\n".encode())
        self.assertEqual(len(commands), 1 if failure == "disconnect" else 2)
        if failure:
            with self.assertRaises(ValueError): W.replay(args.out)
        else:
            self.assertEqual(commands[1], f"echo OQ_STATUS_{TOKEN}:$?\n".encode())
            self.assertEqual(W.replay(args.out)["observations"]["joined"], 1)

    def test_capture_reopens_native_usb_only_before_one_command(self):
        self.capture_control()

    def test_capture_never_retries_after_submission_disconnect(self):
        self.capture_control("disconnect")

    def test_capture_rejects_native_failure_after_terminal_status(self):
        self.capture_control("late-panic")


if __name__ == "__main__":
    unittest.main()

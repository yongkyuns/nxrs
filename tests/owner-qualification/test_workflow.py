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
QEMU_SPEC = importlib.util.spec_from_file_location("owner_qualification_qemu", Path(__file__).with_name("qemu.py"))
Q = importlib.util.module_from_spec(QEMU_SPEC)
with patch.dict(sys.modules, {"workflow": W}):
    QEMU_SPEC.loader.exec_module(Q)
TOKEN = "parser-control-001"
BUILD_ID = "a" * 64


def suite_inputs(native, case="std-disconnect"):
    suite = ("capture" if case == "capture-pool-progress" else
             "publication" if case == "publication-progress" else
             "mutex-inherit" if case == "std-mutex-nested" else
             case.removeprefix("std-") if case in W.MUTEX_CASES else "owner") if native else "host-all"
    return {"kind": "nuttx-target" if native else "host-functional", "suite": suite,
            "cases": list(W.SUITES[suite]), "priority_inheritance": suite in W.SUITE_PROTOCOLS if native else None,
            "sources": {path: "parser-control-source-marker" for path in (W.LATEST_SOURCE, W.NESTED_SOURCE, W.POLICY_SOURCE, W.PUBLICATION_SOURCE, W.CAPTURE_SOURCE, *W.CAPTURE_SUPPORT)},
            "mutex_policy": W.mutex_policy("nuttx-target" if native else "host-functional", suite),
            "pthread_mutex_default_protocol": W.SUITE_PROTOCOLS.get(suite) if native else None}


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
                        "default_protocol": 1 if inherit else 0, "protocol_none": 0, "protocol_inherit": 1,
                        "holder_capacity": 0},
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
    if case == "std-mutex-nested":
        def state(role, effective):
            if not native:
                return None
            person = next(p for p in participants if p["role"] == role)
            return {"tid": person["start"]["tid"], "policy": 1,
                    "base_priority": person["requested_priority"], "effective_priority": effective,
                    "state": 6, "waiting_semaphore": True, "runnable": False}
        def owner(effective, holders):
            return {"state": state("mutex-nested-owner", effective), "tracked_holders": holders} if native else None
        observed["mutex_nested"] = {
            "lock_count": 9, "handoffs": 18, "final_values": [22] * 9,
            "native_configuration": {"priority_inheritance": True, "default_prio_inherit": True,
                                     "default_protocol": 1, "protocol_none": 0, "protocol_inherit": 1,
                                     "holder_capacity": 0} if native else None,
            "rounds": [{"round": number, "initial": owner(120, 0),
                        "contended": [{"lock_index": index,
                                       "waiter": state(f"mutex-nested-waiter-{index}", 140 + 5 * index),
                                       "owner": owner(140 + 5 * index, index + 1)} for index in range(9)],
                        "released": [{"lock_index": index,
                                      "owner": owner(140 + 5 * (index - 1) if index else 120, index)}
                                     for index in reversed(range(9))]} for number in (1, 2)]}
    if case == "latest-value-progress":
        observed["latest"] = {"paused_writer_reads": 2, "paused_reader_publications": 32, "fanout_skew_checks": 1,
                              "rounds": 4096, "payloads": [{"record_bytes": 50,
                              "mutex_elapsed_ns": 150, "mailbox_elapsed_ns": 100}] * 3}
    if case == "publication-progress":
        def paused(role):
            if not native:
                return None
            person = next(p for p in participants if p["role"] == role)
            return {"tid": person["start"]["tid"], "policy": 1, "base_priority": 120,
                    "effective_priority": 120, "state": 6, "waiting_semaphore": True, "runnable": False}
        def cost(size, blocks):
            def window(before, after, count_before, count_after, alloc=0, dealloc=0):
                return {"alloc": alloc, "zeroed": 0, "realloc": 0, "dealloc": dealloc, "failed": 0,
                        "live_before": before, "live_after": after,
                        "blocks_before": count_before, "blocks_after": count_after}
            return {"elapsed_ns": 1000, "fully_reclaimed": True,
                    "setup": window(1000, 1000 + size, 10, 10 + blocks, alloc=blocks),
                    "first_use": window(1000 + size, 1000 + size, 10 + blocks, 10 + blocks),
                    "steady": window(1000 + size, 1000 + size, 10 + blocks, 10 + blocks),
                    "teardown": window(1000 + size, 1000, 10 + blocks, 10, dealloc=blocks)}
        observed["publication"] = {
            "imu_sources": 2, "readers": 2, "paused_writer_reads": 64, "paused_reader_publications": 64,
            "pause_scope": "between-production-api-calls", "paused_writer": paused("publication-writer-low"),
            "paused_reader": paused("publication-reader-low"), "timestamps_preserved": True,
            "freshness_preserved": True, "sticky_faults_preserved": True, "late_samples_suppressed": True,
            "independent_sources": True,
            "native_configuration": {"priority_inheritance": True, "default_prio_inherit": True,
                                     "default_protocol": 1, "protocol_none": 0, "protocol_inherit": 1,
                                     "holder_capacity": 0} if native else None,
            "comparison": {"rounds": 4096, "imu_sources": 2, "readers": 2, "gnss_enabled": False,
                           "scope": "same-image-instrumented-uncontended", "memory_scope": "requested-rust-global-allocator",
                           "mutex": cost(256, 1), "publication": cost(4096, 5)}}
    if case == "capture-pool-progress":
        def paused(role):
            if not native:
                return None
            person = next(p for p in participants if p["role"] == role)
            priority = person["requested_priority"]
            return {"tid": person["start"]["tid"], "policy": 1, "base_priority": priority,
                    "effective_priority": priority, "state": 6, "waiting_semaphore": True, "runnable": False}
        def checkpoint(occupied, busy, free):
            return {"occupied": occupied, "busy_sources": busy, "free_slots": free}
        pointer_bytes = 4 if native else 8
        observed["capture_pool"] = {
            "scope": "fixture-pooled-scalar-transport",
            "pause_stage": "after-slot-claim-before-payload",
            "capacity": 512, "sources": 3, "other_source_records": 511, "restart_records": 2,
            "rejected_full": 1, "rejected_stale": 1, "source_order": "per-source-fifo",
            "complete_payloads": True, "sticky_terminal": True, "clean_restart": True, "pause_hits": 1,
            "after_claim": checkpoint(1, 1, 511), "full": checkpoint(512, 1, 0),
            "pending": checkpoint(1, 1, 511), "retired": checkpoint(0, 0, 512),
            "restarted": checkpoint(0, 0, 512), "first_missing_source": 1, "first_missing_sequence": 511,
            "atomic_16": True, "atomic_32": True, "pointer_bytes": pointer_bytes,
            "storage": {"payload_heap_bytes": 18432, "index_heap_bytes": 3072, "bitmap_bytes": 64,
                        "cursor_bytes": 24, "storage_inline_bytes": 88 + 8 * pointer_bytes,
                        "raw_components_bytes": 21592,
                        "scope": "storage-components-excludes-gate-arc-thread-and-allocator-overhead"},
            "native_configuration": {"priority_inheritance": True, "default_prio_inherit": True,
                                     "default_protocol": 1, "protocol_none": 0, "protocol_inherit": 1,
                                     "holder_capacity": 0} if native else None,
            "low_after_claim": paused("capture-producer-low"),
            "low_after_other_progress": paused("capture-producer-low"),
            "owner_waiting": paused("capture-owner-high")}
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

    def test_latest_requires_both_paused_peer_controls(self):
        value = report("latest-value-progress", True)
        value["observations"]["latest"]["paused_writer_reads"] = 1
        with self.assertRaises(ValueError): self.parse(value)

    def test_publication_requires_paused_peers_lifecycle_and_matching_comparison(self):
        base = report("publication-progress", True)
        mutations = [lambda p: p.update(paused_writer_reads=63),
                     lambda p: p.update(paused_reader_publications=63),
                     lambda p: p.update(pause_scope="inside-atomic-operation"),
                     lambda p: p["paused_reader"].update(waiting_semaphore=False),
                     lambda p: p["paused_writer"].update(effective_priority=180),
                     lambda p: p["paused_writer"].update(tid=99),
                     lambda p: p["native_configuration"].update(default_prio_inherit=False),
                     lambda p: p["native_configuration"].update(holder_capacity=8),
                     lambda p: p["comparison"].update(gnss_enabled=True),
                     lambda p: p["comparison"].update(rounds=4095)]
        for field in ("timestamps_preserved", "freshness_preserved", "sticky_faults_preserved",
                      "late_samples_suppressed", "independent_sources"):
            mutations.append(lambda p, field=field: p.update({field: False}))
        for index, mutation in enumerate(mutations):
            with self.subTest(mutation=index):
                value = copy.deepcopy(base)
                mutation(value["observations"]["publication"])
                with self.assertRaises(ValueError): self.parse(value)

    def test_capture_pool_requires_exact_capacity_retirement_and_native_pauses(self):
        base = report("capture-pool-progress", True)
        mutations = [
            lambda p: p.update(capacity=511), lambda p: p.update(other_source_records=510),
            lambda p: p.update(pause_stage="between-calls"), lambda p: p.update(pause_hits=0),
            lambda p: p.update(source_order="global-reservation-fifo"),
            lambda p: p["pending"].update(busy_sources=0),
            lambda p: p["pending"].update(free_slots=512),
            lambda p: p["retired"].update(occupied=1), lambda p: p["full"].update(free_slots=1),
            lambda p: p.update(first_missing_sequence=512),
            lambda p: p["low_after_other_progress"].update(effective_priority=180),
            lambda p: p["owner_waiting"].update(waiting_semaphore=False),
            lambda p: p["owner_waiting"].update(tid=99),
            lambda p: p["native_configuration"].update(holder_capacity=8),
            lambda p: p["storage"].update(index_heap_bytes=1024),
            lambda p: p["storage"].update(scope="process-ram"),
            lambda p: p.update(atomic_16=False), lambda p: p.update(pointer_bytes=8),
        ]
        for field in ("complete_payloads", "sticky_terminal", "clean_restart"):
            mutations.append(lambda p, field=field: p.update({field: False}))
        for index, mutation in enumerate(mutations):
            with self.subTest(mutation=index):
                value = copy.deepcopy(base)
                mutation(value["observations"]["capture_pool"])
                with self.assertRaises(ValueError): self.parse(value)

    def test_capture_pool_cannot_fabricate_native_evidence_or_attach_to_other_case(self):
        for field in ("native_configuration", "low_after_claim", "low_after_other_progress", "owner_waiting"):
            value = report("capture-pool-progress")
            value["observations"]["capture_pool"][field] = report("capture-pool-progress", True)["observations"]["capture_pool"][field]
            with self.subTest(field=field), self.assertRaises(ValueError): self.parse(value)
        value = report("std-disconnect")
        value["observations"]["capture_pool"] = report("capture-pool-progress")["observations"]["capture_pool"]
        with self.assertRaises(ValueError): self.parse(value)

    def test_publication_requires_zero_call_windows_and_complete_reclamation(self):
        base = report("publication-progress", True)
        for variant, phase in (("mutex", "steady"), ("publication", "steady"), ("publication", "first_use")):
            value = copy.deepcopy(base)
            value["observations"]["publication"]["comparison"][variant][phase].update(alloc=1, dealloc=1)
            with self.subTest(variant=variant, phase=phase), self.assertRaises(ValueError): self.parse(value)
        for variant in ("mutex", "publication"):
            mutations = [lambda c: c.update(fully_reclaimed=False),
                         lambda c: c["teardown"].update(live_after=1001),
                         lambda c: c["setup"].update(failed=1),
                         lambda c: c["setup"].update(alloc=True),
                         lambda c: c["first_use"].update(blocks_before=99),
                         lambda c: c.update(elapsed_ns=0)]
            for index, mutation in enumerate(mutations):
                value = copy.deepcopy(base)
                mutation(value["observations"]["publication"]["comparison"][variant])
                with self.subTest(variant=variant, mutation=index), self.assertRaises(ValueError): self.parse(value)

    def test_publication_cannot_fabricate_native_evidence_or_attach_to_another_case(self):
        for field in ("native_configuration", "paused_writer", "paused_reader"):
            value = report("publication-progress")
            value["observations"]["publication"][field] = report("publication-progress", True)["observations"]["publication"][field]
            with self.subTest(field=field), self.assertRaises(ValueError): self.parse(value)
        value = report("std-disconnect")
        value["observations"]["publication"] = report("publication-progress")["observations"]["publication"]
        with self.assertRaises(ValueError): self.parse(value)

    def test_publication_comparison_retains_storage_until_teardown(self):
        value = report("publication-progress", True)
        cost = value["observations"]["publication"]["comparison"]["mutex"]
        cost["first_use"].update(dealloc=1, live_after=1000, blocks_after=10)
        for phase in ("steady", "teardown"):
            cost[phase].update(live_before=1000, live_after=1000, blocks_before=10, blocks_after=10, dealloc=0)
        with self.assertRaisesRegex(ValueError, "retired its storage"):
            self.parse(value)

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

    def test_nested_mutex_requires_all_holders_donation_and_reuse(self):
        base = report("std-mutex-nested", True)
        mutations = [lambda n: n["rounds"].pop(), lambda n: n["final_values"].__setitem__(8, 21),
                     lambda n: n["native_configuration"].update(holder_capacity=8),
                     lambda n: n["rounds"][0]["contended"][8]["owner"].update(tracked_holders=8),
                     lambda n: n["rounds"][0]["contended"][8]["owner"]["state"].update(effective_priority=175),
                     lambda n: n["rounds"][1]["initial"].update(tracked_holders=1),
                     lambda n: n["rounds"][1]["released"][8]["owner"]["state"].update(effective_priority=140)]
        for index, mutate in enumerate(mutations):
            with self.subTest(mutation=index):
                value = copy.deepcopy(base)
                mutate(value["observations"]["mutex_nested"])
                with self.assertRaises(ValueError): self.parse(value)

    def test_host_nested_mutex_cannot_claim_native_holder_evidence(self):
        value = report("std-mutex-nested")
        native = report("std-mutex-nested", True)["observations"]["mutex_nested"]
        value["observations"]["mutex_nested"]["rounds"][0]["initial"] = native["rounds"][0]["initial"]
        with self.assertRaises(ValueError): self.parse(value)


class BuildSuiteControls(unittest.TestCase):
    def test_config_requires_flat_single_core_and_both_protocol_settings(self):
        for case in ("std-disconnect", *W.MUTEX_CASES, "publication-progress", "capture-pool-progress"):
            specification = suite_inputs(True, case)
            values = {"BUILD_FLAT": "y", **specification["mutex_policy"]["kconfig"]}
            def config(symbols): return "".join(f"CONFIG_{symbol}={value}\n" for symbol, value in symbols.items())
            W.validate_config(config(values), specification)
            for symbol in ("BUILD_FLAT", "SMP", "PRIORITY_INHERITANCE", "PTHREAD_MUTEX_DEFAULT_PRIO_INHERIT", "PTHREAD_MUTEX_DEFAULT_PRIO_NONE"):
                changed = {**values, symbol: "n" if values.get(symbol) == "y" else "y"}
                with self.subTest(case=case, symbol=symbol), self.assertRaises(ValueError):
                    W.validate_config(config(changed), specification)
            if "SEM_PREALLOCHOLDERS" in values:
                for value in ("8", "n"):
                    with self.subTest(case=case, holder_capacity=value), self.assertRaises(ValueError):
                        W.validate_config(config({**values, "SEM_PREALLOCHOLDERS": value}), specification)

    def test_frontend_policy_receipt_cannot_relabel_an_experiment(self):
        specification = suite_inputs(True)
        W.validate_policy_receipt({"mutex_policy": specification["mutex_policy"]}, specification)
        for value in (None, W.mutex_policy("nuttx-target", "mutex-inherit"),
                      {**specification["mutex_policy"], "reason": "another experiment"}):
            with self.subTest(policy=value), self.assertRaises(ValueError):
                W.validate_policy_receipt({"mutex_policy": value}, specification)

    def test_current_source_receipt_cannot_downgrade_to_legacy(self):
        for source in (W.POLICY_SOURCE, W.NESTED_SOURCE, W.PUBLICATION_SOURCE, W.CAPTURE_SOURCE):
            for removed in ("suite", "mutex_policy"):
                specification = suite_inputs(True)
                specification["sources"] = {source: "parser-control-source-marker"}
                specification.pop(removed)
                with self.subTest(source=source, removed=removed), self.assertRaisesRegex(ValueError, "current retained sources"):
                    W.selected_cases(specification)

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

    def build(self, native=False, case="std-disconnect", profile=None, latest=True, nested=True, legacy=False, policy=True, publication=None, capture=None):
        if publication is None:
            publication = policy and not legacy
        if capture is None:
            capture = publication and policy and not legacy
        root = self.root / "input-build"
        root.mkdir()
        source = root / "retained/sources/tests/owner-qualification/src/main.rs"
        source.parent.mkdir(parents=True)
        source.write_text("// synthetic evidence-validator input, never executed\n")
        sources = {"tests/owner-qualification/src/main.rs": W.digest(source)}
        if latest:
            latest_source = root / "retained/sources" / W.LATEST_SOURCE
            latest_source.parent.mkdir(parents=True)
            latest_source.write_text("// synthetic latest-value source, never executed\n")
            sources[W.LATEST_SOURCE] = W.digest(latest_source)
        if nested:
            nested_source = root / "retained/sources" / W.NESTED_SOURCE
            nested_source.write_text("// synthetic nested-mutex source, never executed\n")
            sources[W.NESTED_SOURCE] = W.digest(nested_source)
        if policy and not legacy:
            policy_source = root / "retained/sources" / W.POLICY_SOURCE
            policy_source.parent.mkdir(parents=True)
            policy_source.write_text("// synthetic resource mutex policy source, never executed\n")
            sources[W.POLICY_SOURCE] = W.digest(policy_source)
        if publication:
            publication_source = root / "retained/sources" / W.PUBLICATION_SOURCE
            publication_source.parent.mkdir(parents=True, exist_ok=True)
            publication_source.write_text("// synthetic publication source, never executed\n")
            sources[W.PUBLICATION_SOURCE] = W.digest(publication_source)
        if capture:
            for path in (W.CAPTURE_SOURCE, *W.CAPTURE_SUPPORT):
                capture_source = root / "retained/sources" / path
                capture_source.parent.mkdir(parents=True, exist_ok=True)
                capture_source.write_text("// synthetic pooled capture source, never executed\n")
                sources[path] = W.digest(capture_source)
        specification = {**suite_inputs(native, case),
                         "profile": (profile or "esp32s3-ram-footprint") if native else None,
                         "sources": sources}
        omitted = ([] if latest else ["latest-value-progress"]) + ([] if nested else ["std-mutex-nested"]) + ([] if publication else ["publication-progress"]) + ([] if capture else ["capture-pool-progress"])
        specification["cases"] = [case for case in specification["cases"] if case not in omitted]
        if legacy:
            for key in ("suite", "cases", "pthread_mutex_default_protocol", "mutex_policy"):
                specification.pop(key)
        elif not policy:
            specification.pop("mutex_policy")
        build_id = W.identity(specification)
        artifacts = root / "retained/artifacts"
        artifacts.mkdir()
        (artifacts / "firmware.elf").write_bytes(b"\x7fELF parser input only " + build_id.encode())
        if native:
            (artifacts / "firmware.bin").write_bytes(b"parser input only " + build_id.encode())
            (artifacts / "frontend").mkdir()
            config = "CONFIG_BUILD_FLAT=y\n"
            if "mutex_policy" in specification:
                config += "".join(f"CONFIG_{name}={value}\n" for name, value in specification["mutex_policy"]["kconfig"].items())
                W.save(artifacts / "frontend/kconfig-requests.json", {"mutex_policy": specification["mutex_policy"]})
            else:
                if specification["priority_inheritance"]: config += "CONFIG_PRIORITY_INHERITANCE=y\n"
                if specification.get("pthread_mutex_default_protocol") == "inherit": config += "CONFIG_PTHREAD_MUTEX_DEFAULT_PRIO_INHERIT=y\n"
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
        self.assertEqual(result["case_count"], 8)
        self.assertEqual(result["cases"], list(W.OWNER_CASES))
        self.assertEqual(result["image"]["sha256"], W.digest(Path(result["image"]["path"])))
        self.assertFalse(result["physical_execution_verified"])
        (root / value["image"]).write_bytes(b"changed image")
        with self.assertRaises(ValueError): W.inspect(root)

    def test_legacy_owner_receipt_does_not_acquire_latest_case(self):
        root, build = self.build(True, latest=False, nested=False, legacy=True)
        self.assertEqual(W.inspect(root)["case_count"], 7)
        value = report("std-disconnect", True, build["build_id"])
        self.assertEqual(W.parse(transcript(value), build, value["case"], TOKEN), value)
        value = report("latest-value-progress", True, build["build_id"])
        with self.assertRaises(ValueError):
            W.parse(transcript(value), build, value["case"], TOKEN)

    def test_legacy_latest_receipt_requires_retained_source(self):
        root, build = self.build(True, nested=False, legacy=True)
        self.assertEqual(W.inspect(root)["case_count"], 8)
        value = report("latest-value-progress", True, build["build_id"])
        self.assertEqual(W.parse(transcript(value), build, value["case"], TOKEN), value)
        (root / "retained/sources" / W.LATEST_SOURCE).unlink()
        with self.assertRaises(ValueError): W.inspect(root)

    def test_explicit_prior_owner_inventory_remains_replayable(self):
        root, _ = self.build(True, latest=False, nested=False, policy=False)
        self.assertEqual(W.inspect(root)["case_count"], 7)

    def test_explicit_prior_host_inventory_remains_replayable(self):
        root, _ = self.build(latest=False, nested=False, policy=False)
        self.assertEqual(W.inspect(root)["case_count"], 9)

    def test_prior_inherit_suite_does_not_acquire_nested_case(self):
        root, build = self.build(True, case="std-mutex-inherit", nested=False, policy=False)
        self.assertEqual(W.inspect(root)["cases"], ["std-mutex-inherit"])
        value = report("std-mutex-inherit", True, build["build_id"])
        value["observations"]["mutex"]["native"]["configuration"].pop("holder_capacity")
        self.assertEqual(W.parse(transcript(value), build, value["case"], TOKEN), value)
        value = report("std-mutex-nested", True, build["build_id"])
        with self.assertRaises(ValueError):
            W.parse(transcript(value), build, value["case"], TOKEN)

    def test_prior_host_receipt_does_not_acquire_publication_case(self):
        root, build = self.build(publication=False)
        self.assertEqual(W.inspect(root)["case_count"], 11)
        value = report("publication-progress", False, build["build_id"])
        with self.assertRaises(ValueError):
            W.parse(transcript(value), build, value["case"], TOKEN)

    def test_publication_suite_requires_retained_source_and_inherit_control(self):
        root, build = self.build(True, case="publication-progress")
        self.assertEqual(W.inspect(root)["cases"], ["publication-progress", "std-mutex-inherit"])
        for case in W.SUITES["publication"]:
            value = report(case, True, build["build_id"])
            self.assertEqual(W.parse(transcript(value), build, case, TOKEN), value)
        specification = copy.deepcopy(build["inputs"])
        specification["sources"].pop(W.PUBLICATION_SOURCE)
        specification["cases"] = ["std-mutex-inherit"]
        with self.assertRaises(ValueError): W.selected_cases(specification)

    def test_prior_twelve_case_receipt_does_not_acquire_pooled_capture(self):
        root, build = self.build(capture=False)
        self.assertEqual(W.inspect(root)["case_count"], 12)
        value = report("capture-pool-progress", False, build["build_id"])
        with self.assertRaises(ValueError):
            W.parse(transcript(value), build, value["case"], TOKEN)

    def test_capture_suite_requires_scenario_shared_gate_and_pool_sources(self):
        root, build = self.build(True, case="capture-pool-progress")
        self.assertEqual(W.inspect(root)["cases"], ["capture-pool-progress", "std-mutex-inherit"])
        for case in W.SUITES["capture"]:
            value = report(case, True, build["build_id"])
            self.assertEqual(W.parse(transcript(value), build, case, TOKEN), value)
        for source in (W.CAPTURE_SOURCE, *W.CAPTURE_SUPPORT):
            specification = copy.deepcopy(build["inputs"])
            specification["sources"].pop(source)
            with self.subTest(source=source), self.assertRaises(ValueError):
                W.selected_cases(specification)

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
        self.assertEqual(W.inspect(root)["cases"], list(W.SUITES["mutex-inherit"]))
        name = "retained/artifacts/frontend/resolved.config"
        config = root / name
        config.write_text("CONFIG_BUILD_FLAT=y\nCONFIG_PRIORITY_INHERITANCE=y\n")
        value["artifacts"][name] = {"sha256": W.digest(config), "bytes": config.stat().st_size}
        W.save(root / "build.json", value)
        with self.assertRaisesRegex(ValueError, "resolved pthread mutex default"):
            W.validate_build(root)

    def test_current_build_requires_matching_retained_policy(self):
        root, build = self.build(True)
        name = "retained/artifacts/frontend/kconfig-requests.json"
        document = W.unique_json((root / name).read_text())
        document["mutex_policy"]["reason"] = "another experiment"
        W.save(root / name, document)
        build["artifacts"][name] = {"sha256": W.digest(root / name), "bytes": (root / name).stat().st_size}
        W.save(root / "build.json", build)
        with self.assertRaisesRegex(ValueError, "policy disagrees"):
            W.validate_build(root)
        (root / name).unlink()
        del build["artifacts"][name]
        W.save(root / "build.json", build)
        with self.assertRaisesRegex(ValueError, "policy evidence is not retained"):
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

    def test_qemu_uart_fix_release_boundaries(self):
        # The fix entered 9.1, then was backported separately to 8.2.7 and
        # 9.0.3. A flat >= 8.2.7 comparison would wrongly admit 9.0.0-9.0.2.
        for version in ("8.2.7", "8.2.8", "9.0.3", "9.1.0", "9.2.0", "10.0.13"):
            with self.subTest(version=version):
                Q.require_fixed_mps2_uart(f"QEMU emulator version {version} (distribution build)\nCopyright")
        for version in ("7.2.0", "8.2.2", "8.2.6", "8.3.0", "9.0.0", "9.0.2", "9.1.0-rc0"):
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, "UART RX/TX"):
                Q.require_fixed_mps2_uart(f"QEMU emulator version {version}")
        for version in ("", "QEMU emulator version unknown", "other emulator 10.0.0"):
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, "reported version"):
                Q.require_fixed_mps2_uart(version)

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

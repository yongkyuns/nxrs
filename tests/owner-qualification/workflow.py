#!/usr/bin/env python3
"""Build/capture/replay finite current-source owner witnesses; never flash a board."""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from rtos_harness.console import BOOT_MARKERS, fresh_shell_boot

CASES = {
    # attempted, admitted, rejected, handled, discarded, joined, native waits
    "std-blocked-delivery": (16, 16, 0, 16, 0, 2, 16),
    "std-full-progress": (3, 2, 1, 2, 0, 2, 0),
    "std-disconnect": (0, 0, 0, 0, 0, 1, 1),
    "owner-independent-inputs": (3, 3, 0, 3, 0, 3, 3),
    "owner-full-stop": (4, 2, 2, 0, 2, 1, 0),
    "owner-fairness-deadline": (23, 23, 0, 21, 2, 2, 0),
    "owner-failure-recovery": (2, 1, 1, 0, 1, 1, 0),
    "latest-value-progress": (0, 0, 0, 0, 0, 2, 0),
}
OWNER_CASES = tuple(CASES)
LATEST_SOURCE = "tests/owner-qualification/src/scenarios/latest.rs"
NESTED_SOURCE = "tests/owner-qualification/src/mutex_nested.rs"
PUBLICATION_SOURCE = "tests/owner-qualification/src/scenarios/publication.rs"
CAPTURE_SOURCE = "tests/owner-qualification/src/scenarios/capture_pool.rs"
CAPTURE_SUPPORT = (
    "tests/mpsc-comparison/rust/tests/support/capture_gate.rs",
    "tests/mpsc-comparison/rust/tests/support/capture_pool.rs",
)
POLICY_SOURCE = "tools/build/src/mutex_policy.rs"
MUTEX_CASES = ("std-mutex-inherit", "std-mutex-no-inherit")
CASES.update({case: (0, 0, 0, 0, 0, 3, 3) for case in MUTEX_CASES})
CASES["std-mutex-nested"] = (18, 18, 0, 18, 0, 10, 48)
CASES["publication-progress"] = (0, 0, 0, 0, 0, 4, 2)
CASES["capture-pool-progress"] = (516, 514, 2, 514, 0, 2, 2)
SUITES = {"owner": OWNER_CASES,
          "mutex-inherit": (MUTEX_CASES[0], "std-mutex-nested"),
          "mutex-no-inherit": (MUTEX_CASES[1],),
          "publication": ("publication-progress", MUTEX_CASES[0]),
          "capture": ("capture-pool-progress", MUTEX_CASES[0]),
          "host-all": tuple(CASES)}
SUITE_PROTOCOLS = {"mutex-inherit": "inherit", "mutex-no-inherit": "none",
                   "publication": "inherit", "capture": "inherit"}
MUTEX_CONTROLS = {
    "owner": ("kernel-pi-disabled", "Preserve the owner and channel progress baseline without priority inheritance."),
    "mutex-no-inherit": ("no-inherit", "Negative control for ordinary std::sync::Mutex priority inheritance."),
}
ROLES = {
    "std-blocked-delivery": [("std-producer", 120), ("std-receiver", 180), ("coordinator", 220)],
    "std-full-progress": [("std-producer", 180), ("std-receiver", 120), ("coordinator", 220)],
    "std-disconnect": [("std-receiver", 180), ("coordinator", 220)],
    "owner-independent-inputs": [("important-provider", 180), ("ordinary-provider", 180), ("selected-owner", 180), ("coordinator", 180)],
    "owner-full-stop": [("selected-owner", 180), ("coordinator", 180)],
    "owner-fairness-deadline": [("selected-owner", 180), ("selected-owner", 180), ("coordinator", 180)],
    "owner-failure-recovery": [("selected-owner", 180), ("coordinator", 180)],
    "latest-value-progress": [("paused-writer", 120), ("paused-reader", 120), ("coordinator", 220)],
}
ROLES.update({case: [("mutex-low", 120), ("mutex-medium", 150),
                    ("mutex-high", 180), ("coordinator", 220)] for case in MUTEX_CASES})
ROLES["std-mutex-nested"] = [("mutex-nested-owner", 120),
                            *[(f"mutex-nested-waiter-{index}", 140 + 5 * index) for index in range(9)],
                            ("coordinator", 220)]
ROLES["publication-progress"] = [("publication-writer-low", 120),
                                 ("publication-reader-high", 180),
                                 ("publication-reader-low", 120),
                                 ("publication-writer-high", 180), ("coordinator", 220)]
ROLES["capture-pool-progress"] = [("capture-producer-low", 120),
                                  ("capture-owner-high", 180), ("coordinator", 220)]
FIELDS = ("attempted", "admitted", "rejected", "handled", "discarded", "joined", "required_waits")
FAULT = re.compile(r"\b(panic|panicked|assertion|hardfault|dump_assert|watchdog expired|segmentation fault|aborted|fatal error)\b", re.I)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def unique_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON field {key}")
            result[key] = value
        return result
    return json.loads(text, object_pairs_hook=pairs)


def contained(root, relative):
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("unsafe evidence path")
    candidate = root / path
    if any((root / Path(*path.parts[:index])).is_symlink() for index in range(1, len(path.parts) + 1)):
        raise ValueError("symlink evidence is not supported")
    if not candidate.is_file():
        raise ValueError(f"missing evidence {relative}")
    return candidate


def inventory():
    # Includes actual owner/controller, transitive in-repo support, normal
    # frontend, ABI/SDK/kernel patches and every product profile. Captured files
    # are exact build inputs, not a claim that every one links into the image.
    paths = []
    for directory in (HERE, ROOT / "tests/nuttx-std", ROOT / "tests/allocation-probe",
                      ROOT / "hal", ROOT / "shared", ROOT / "service/navigation", ROOT / "app/motion-tracker",
                      ROOT / "driver", ROOT / "platform", ROOT / "upstream",
                      ROOT / "tools/build", ROOT / "tools/build-support", ROOT / "tools/detector-build-support"):
        paths.extend(p for p in directory.rglob("*") if p.is_file() and not p.is_symlink()
                     and not any(part in ("target", "__pycache__", "results") for part in p.relative_to(ROOT).parts)
                     and (p.suffix in (".rs", ".c", ".h", ".toml", ".lock", ".py", ".sh", ".json", ".ld", ".patch", ".defs", ".conf")
                          or p.name in ("Makefile", "Kconfig")))
    paths += [ROOT / name for name in ("Cargo.toml", "Cargo.lock", "rust-toolchain.toml", ".cargo/config.toml")]
    paths.append(ROOT / "tests/rtos_harness/console.py")
    paths.extend(ROOT / name for name in CAPTURE_SUPPORT)
    return {str(p.relative_to(ROOT)): digest(p) for p in sorted(set(paths))}


def selected_cases(specification):
    # Receipts without explicit suites never acquire mutex claims. The latest
    # case is admitted only when its source is part of the retained build;
    # earlier seven-owner/nine-host receipts keep their original inventories.
    sources = specification.get("sources", {})
    if any(source in sources for source in (POLICY_SOURCE, NESTED_SOURCE, PUBLICATION_SOURCE, CAPTURE_SOURCE)) and ("mutex_policy" not in specification or "suite" not in specification):
        raise ValueError("current retained sources require an explicit suite and resource mutex policy")
    suite = specification.get("suite", "owner")
    if suite not in SUITES or (specification["kind"] == "nuttx-target" and suite == "host-all"):
        raise ValueError("unsupported build suite")
    cases = SUITES[suite]
    if LATEST_SOURCE not in sources:
        cases = tuple(case for case in cases if case != "latest-value-progress")
    if NESTED_SOURCE not in sources:
        cases = tuple(case for case in cases if case != "std-mutex-nested")
    if PUBLICATION_SOURCE not in sources:
        if suite == "publication":
            raise ValueError("publication suite requires retained production publication scenario")
        cases = tuple(case for case in cases if case != "publication-progress")
    if CAPTURE_SOURCE not in sources:
        if suite == "capture":
            raise ValueError("capture suite requires retained pooled transport scenario")
        cases = tuple(case for case in cases if case != "capture-pool-progress")
    elif any(source not in sources for source in CAPTURE_SUPPORT):
        raise ValueError("pooled capture requires retained shared gate and transport sources")
    if "suite" in specification:
        if specification.get("cases") != list(cases):
            raise ValueError("case inventory disagrees with build suite")
        target = specification["kind"] == "nuttx-target"
        protocol = SUITE_PROTOCOLS.get(suite) if target else None
        inheritance = protocol is not None if target else None
        if specification.get("priority_inheritance") is not inheritance or specification.get("pthread_mutex_default_protocol") != protocol:
            raise ValueError("mutex protocol disagrees with build suite")
        if "mutex_policy" in specification and canonical(specification["mutex_policy"]) != canonical(mutex_policy(specification["kind"], suite)):
            raise ValueError("resource mutex policy disagrees with build suite")
    return cases


def mutex_policy(kind, suite):
    if kind != "nuttx-target":
        return None
    mode, reason = MUTEX_CONTROLS.get(suite, ("inherit", None))
    inheritance = mode != "kernel-pi-disabled"
    protocol = "inherit" if mode == "inherit" else "none"
    kconfig = {"PRIORITY_INHERITANCE": "y" if inheritance else "n",
               "PTHREAD_MUTEX_DEFAULT_PRIO_INHERIT": "y" if protocol == "inherit" else "n",
               "PTHREAD_MUTEX_DEFAULT_PRIO_NONE": "n" if protocol == "inherit" else "y"}
    if inheritance:
        kconfig["SEM_PREALLOCHOLDERS"] = "0"
    return {"version": 1, "mode": mode, "source": "default" if reason is None else "qualification-control",
            "reason": reason, "default_pthread_mutex_protocol": protocol, "priority_inheritance": inheritance,
            "holder_tracking": "embedded-single-holder" if inheritance else "disabled", "kconfig": kconfig}


def inputs(kind, profile=None, suite=None):
    suite = suite or ("host-all" if kind == "host-functional" else "owner")
    target = kind == "nuttx-target"
    protocol = SUITE_PROTOCOLS.get(suite) if target else None
    return {"kind": kind, "profile": profile, "suite": suite, "cases": list(SUITES[suite]),
            "build_nonce": secrets.token_hex(16),
            "sources": inventory(), "command": "owner_qualification", "main_stack": 16384,
            "worker_stack": 16384 if kind == "nuttx-target" else 131072,
            "priority_inheritance": protocol is not None if target else None,
            "pthread_mutex_default_protocol": protocol,
            "mutex_policy": mutex_policy(kind, suite),
            "gitlinks": subprocess.check_output(["git", "ls-files", "--stage", "external/nuttx", "external/nuttx-apps"], cwd=ROOT, text=True)}


def validate_config(config, specification):
    enabled = set(re.findall(r"^CONFIG_([A-Z0-9_]+)=y$", config, re.M))
    if "BUILD_FLAT" not in enabled or "SMP" in enabled:
        raise ValueError("unsupported resolved execution mode")
    if ("PRIORITY_INHERITANCE" in enabled) != specification["priority_inheritance"]:
        raise ValueError("resolved priority inheritance disagrees with selected suite")
    if ("PTHREAD_MUTEX_DEFAULT_PRIO_INHERIT" in enabled) != (specification["pthread_mutex_default_protocol"] == "inherit"):
        raise ValueError("resolved pthread mutex default disagrees with selected suite")
    if "mutex_policy" in specification:
        values = dict(re.findall(r"^CONFIG_([A-Z0-9_]+)=(.*)$", config, re.M))
        for name, expected in specification["mutex_policy"]["kconfig"].items():
            if values.get(name, "n") != expected:
                raise ValueError(f"resolved CONFIG_{name} disagrees with resource mutex policy")


def validate_policy_receipt(document, specification):
    if canonical(document.get("mutex_policy")) != canonical(specification["mutex_policy"]):
        raise ValueError("frontend resource mutex policy disagrees with qualification inputs")


def identity(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def contains_runtime_identity(path, build_id):
    data = path.read_bytes()
    if path.suffix == ".uf2":
        # UF2 inserts a header every 256 payload bytes. The marker may straddle
        # blocks, so inspect contiguous flash payload rather than container bytes.
        blocks = []
        if len(data) % 512:
            raise ValueError("truncated UF2 image")
        for offset in range(0, len(data), 512):
            block = data[offset:offset + 512]
            if block[:8] != b"UF2\n\x57\x51\x5d\x9e" or block[508:] != b"\x30\x6f\xb1\x0a":
                raise ValueError("invalid UF2 image")
            size = int.from_bytes(block[16:20], "little")
            if not 0 < size <= 476:
                raise ValueError("invalid UF2 payload length")
            blocks.append((int.from_bytes(block[12:16], "little"), block[32:32 + size]))
        spans = []
        last = None
        for address, payload in sorted(blocks):
            if last != address:
                spans.append(bytearray())
            spans[-1].extend(payload)
            last = address + len(payload)
        return any(build_id.encode() in span for span in spans)
    return build_id.encode() in data


def retain_build(out, specification, artifacts, executable=None, image=None):
    retained = out / "retained"
    retained.mkdir()
    for relative, expected in specification["sources"].items():
        source = ROOT / relative
        if digest(source) != expected:
            raise ValueError("source changed during build")
        destination = retained / "sources" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    records = {}
    for label, source in artifacts.items():
        destination = retained / "artifacts" / label
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        records[str(destination.relative_to(out))] = {"sha256": digest(destination), "bytes": destination.stat().st_size}
    report = {"schema": "owner-qualification-build-v1", "build_id": identity(specification),
              "inputs": specification, "artifacts": records,
              "executable": f"retained/artifacts/{executable}" if executable else None,
              "image": f"retained/artifacts/{image}" if image else None,
              "physical_execution_verified": False}
    save(out / "build.json", report)
    validate_build(out)
    return report


def validate_build(root):
    report = unique_json(contained(root, "build.json").read_text())
    if report.get("schema") != "owner-qualification-build-v1" or report.get("build_id") != identity(report["inputs"]):
        raise ValueError("invalid build identity")
    if report.get("physical_execution_verified") is not False:
        raise ValueError("build receipt cannot establish physical execution")
    if report["inputs"]["kind"] not in ("host-functional", "nuttx-target") or not report["inputs"]["sources"]:
        raise ValueError("unknown build domain or empty source identity")
    selected_cases(report["inputs"])
    if not report.get("executable") or (report["inputs"]["kind"] == "nuttx-target" and not report.get("image")):
        raise ValueError("selected executable/image missing")
    for relative, expected in report["inputs"]["sources"].items():
        if digest(contained(root, "retained/sources/" + relative)) != expected:
            raise ValueError("changed retained source")
    for relative, expected in report["artifacts"].items():
        path = contained(root, relative)
        if digest(path) != expected["sha256"] or path.stat().st_size != expected["bytes"]:
            raise ValueError("changed build artifact")
    for field in ("executable", "image"):
        if report.get(field):
            path = contained(root, report[field])
            if report[field] not in report["artifacts"] or not contains_runtime_identity(path, report["build_id"]):
                raise ValueError("runtime identity is not in the selected image/executable")
    if report["inputs"]["kind"] == "nuttx-target" and "suite" in report["inputs"]:
        config = "retained/artifacts/frontend/resolved.config"
        if config not in report["artifacts"]:
            raise ValueError("resolved configuration is not retained")
        validate_config(contained(root, config).read_text(), report["inputs"])
        if "mutex_policy" in report["inputs"]:
            policy = "retained/artifacts/frontend/kconfig-requests.json"
            if policy not in report["artifacts"]:
                raise ValueError("resource mutex policy evidence is not retained")
            validate_policy_receipt(unique_json(contained(root, policy).read_text()), report["inputs"])
    return report


def inspect(build_root):
    root = build_root.resolve()
    build = validate_build(root)
    def artifact(field):
        path = build[field]
        return {"path": str(contained(root, path)), "relative_path": path,
                **build["artifacts"][path]} if path else None
    return {"schema": "owner-qualification-inspect-v1", "build_id": build["build_id"],
            "build_sha256": digest(root / "build.json"), "domain": build["inputs"]["kind"],
            "profile": build["inputs"]["profile"], "source_count": len(build["inputs"]["sources"]),
            "source_manifest_sha256": identity(build["inputs"]["sources"]),
            "executable": artifact("executable"),
            "elf": artifact("executable") if build["inputs"]["kind"] == "nuttx-target" else None,
            "image": artifact("image"), "suite": build["inputs"].get("suite", "owner"),
            "cases": list(selected_cases(build["inputs"])), "case_count": len(selected_cases(build["inputs"])),
            "requires_fresh_boot": build["inputs"]["kind"] == "nuttx-target",
            "physical_execution_verified": False, "timing_qualified": False, "production_qualified": False}


def validate_mutex_configuration(config, inherit, embedded):
    if type(config) is not dict or config.get("priority_inheritance") is not True or config.get("default_prio_inherit") is not inherit:
        raise ValueError("kernel/default mutex protocol mismatch")
    for key in ("default_protocol", "protocol_none", "protocol_inherit"):
        if type(config.get(key)) is not int:
            raise ValueError("invalid native protocol readback")
    if config["protocol_none"] == config["protocol_inherit"] or config["default_protocol"] != config["protocol_inherit" if inherit else "protocol_none"]:
        raise ValueError("default pthread attribute did not select the expected protocol")
    if embedded and (type(config.get("holder_capacity")) is not int or config["holder_capacity"] != 0):
        raise ValueError("native mutex holder tracking is not embedded")


def validate_mutex(observed, report, specification):
    case = report["case"]
    if case not in MUTEX_CASES:
        if "mutex" in observed:
            raise ValueError("unrelated case cannot carry mutex evidence")
        return
    mutex = observed["mutex"]
    inherit = case == "std-mutex-inherit"
    if mutex["expected_inheritance"] is not inherit or mutex["final_value"] != 42 or type(mutex["final_value"]) is not int:
        raise ValueError("wrong mutex expectation or protected value")
    order = mutex["order"]
    keys = ("low_unlock", "high_acquire", "medium_start", "medium_finish")
    if set(order) != set(keys) or any(type(order[key]) is not int for key in keys) or sorted(order.values()) != [1, 2, 3, 4]:
        raise ValueError("incomplete mutex event order")
    if order["low_unlock"] >= order["high_acquire"] or order["medium_start"] >= order["medium_finish"]:
        raise ValueError("impossible mutex event order")
    for key in ("high_wait_us", "medium_window_us", "medium_elapsed_us", "medium_iterations"):
        if type(mutex[key]) is not int or mutex[key] < 0:
            raise ValueError("invalid mutex observation")
    if mutex["medium_window_us"] != 25_000 or mutex["medium_elapsed_us"] < mutex["medium_window_us"] or mutex["medium_iterations"] == 0:
        raise ValueError("finite medium interference was not exercised")
    for key in ("high_completed_at_medium_start", "high_completed_at_medium_finish"):
        if type(mutex[key]) is not bool:
            raise ValueError("invalid mutex completion observation")
    native = mutex["native"]
    if report["domain"] == "host-functional":
        if native is not None:
            raise ValueError("host run fabricated native mutex evidence")
        return
    allowed_suites = ("mutex-inherit", "publication", "capture") if inherit else ("mutex-no-inherit",)
    if specification.get("suite") not in allowed_suites:
        raise ValueError("mutex case does not match its configured firmware")
    validate_mutex_configuration(native["configuration"], inherit, "mutex_policy" in specification)
    people = {p["role"]: p for p in observed["participants"]}
    low_priority = 180 if inherit else 120
    snapshots = (("low_before_high", "mutex-low", 120, True, False),
                 ("medium_before_high", "mutex-medium", 150, True, False),
                 ("high_wait", "mutex-high", 180, True, False),
                 ("low_while_high_waits", "mutex-low", low_priority, True, False),
                 ("low_ready", "mutex-low", low_priority, False, True),
                 ("medium_ready", "mutex-medium", 150, False, True),
                 ("low_before_unlock", "mutex-low", low_priority, False, True),
                 ("low_after_unlock", "mutex-low", 120, False, True))
    for name, role, effective, waiting, runnable in snapshots:
        state = native[name]
        person = people[role]
        expected = {"tid": person["start"]["tid"], "policy": report["fifo_policy"],
                    "base_priority": person["requested_priority"], "effective_priority": effective,
                    "waiting_semaphore": waiting, "runnable": runnable}
        if any(type(state.get(key)) is not type(value) or state.get(key) != value for key, value in expected.items()):
            raise ValueError(f"mutex native phase/priority mismatch: {name}")
        if type(state["state"]) is not int or state["state"] < 0:
            raise ValueError("invalid native thread state")
    expected_order = (1, 2, 3, 4) if inherit else (3, 4, 1, 2)
    if tuple(order[key] for key in keys) != expected_order or any(mutex[key] is not inherit for key in ("high_completed_at_medium_start", "high_completed_at_medium_finish")):
        raise ValueError("mutex did not show the expected fixed-priority behavior")


def validate_nested_mutex(observed, report, specification):
    if report["case"] != "std-mutex-nested":
        if "mutex_nested" in observed:
            raise ValueError("unrelated case cannot carry nested mutex evidence")
        return
    nested = observed["mutex_nested"]
    if type(nested.get("lock_count")) is not int or nested["lock_count"] != 9 or type(nested.get("handoffs")) is not int or nested["handoffs"] != 18:
        raise ValueError("nested mutex workload is incomplete")
    values = nested.get("final_values")
    if type(values) is not list or len(values) != 9 or any(type(value) is not int or value != 22 for value in values):
        raise ValueError("nested mutex protected values are incomplete")
    rounds = nested.get("rounds")
    if type(rounds) is not list or len(rounds) != 2:
        raise ValueError("nested mutex holder reuse was not exercised")
    native = report["domain"] == "nuttx-target"
    if native:
        if specification.get("suite") != "mutex-inherit":
            raise ValueError("nested mutex case requires the inheritance suite")
        validate_mutex_configuration(nested.get("native_configuration"), True, True)
    elif nested.get("native_configuration") is not None:
        raise ValueError("host run fabricated nested mutex configuration")
    people = {person["role"]: person for person in observed["participants"]}

    def state(value, role, effective):
        if not native:
            if value is not None:
                raise ValueError("host run fabricated nested mutex thread state")
            return
        person = people[role]
        expected = {"tid": person["start"]["tid"], "policy": report["fifo_policy"],
                    "base_priority": person["requested_priority"], "effective_priority": effective,
                    "waiting_semaphore": True, "runnable": False}
        if type(value) is not dict or any(type(value.get(key)) is not type(expected_value) or value.get(key) != expected_value
                                         for key, expected_value in expected.items()):
            raise ValueError("nested mutex native phase/priority mismatch")
        if type(value.get("state")) is not int or value["state"] < 0:
            raise ValueError("invalid nested mutex thread state")

    def owner(value, effective, holders):
        if not native:
            if value is not None:
                raise ValueError("host run fabricated nested mutex holder state")
            return
        if type(value) is not dict or type(value.get("tracked_holders")) is not int or value["tracked_holders"] != holders:
            raise ValueError("nested mutex holder accounting is incomplete")
        state(value.get("state"), "mutex-nested-owner", effective)

    for number, round_value in enumerate(rounds, 1):
        if type(round_value) is not dict or type(round_value.get("round")) is not int or round_value["round"] != number:
            raise ValueError("nested mutex round order is invalid")
        owner(round_value.get("initial"), 120, 0)
        for field in ("contended", "released"):
            if type(round_value.get(field)) is not list or len(round_value[field]) != 9:
                raise ValueError("nested mutex stages are incomplete")
        for index, stage in enumerate(round_value["contended"]):
            if type(stage) is not dict or type(stage.get("lock_index")) is not int or stage["lock_index"] != index:
                raise ValueError("nested mutex contention order is invalid")
            state(stage.get("waiter"), f"mutex-nested-waiter-{index}", 140 + 5 * index)
            owner(stage.get("owner"), 140 + 5 * index, index + 1)
        for index, stage in zip(reversed(range(9)), round_value["released"]):
            if type(stage) is not dict or type(stage.get("lock_index")) is not int or stage["lock_index"] != index:
                raise ValueError("nested mutex release order is invalid")
            owner(stage.get("owner"), 140 + 5 * (index - 1) if index else 120, index)


def validate_publication(observed, report, specification):
    if report["case"] != "publication-progress":
        if "publication" in observed:
            raise ValueError("unrelated case cannot carry production publication evidence")
        return
    publication = observed.get("publication")
    expected = {"imu_sources": 2, "readers": 2, "paused_writer_reads": 64,
                "paused_reader_publications": 64, "pause_scope": "between-production-api-calls",
                "timestamps_preserved": True, "freshness_preserved": True,
                "sticky_faults_preserved": True, "late_samples_suppressed": True,
                "independent_sources": True}
    if type(publication) is not dict or any(type(publication.get(key)) is not type(value) or publication.get(key) != value
                                            for key, value in expected.items()):
        raise ValueError("production publication progress or lifecycle witnesses missing")
    native = report["domain"] == "nuttx-target"
    if native:
        if specification.get("suite") != "publication":
            raise ValueError("production publication requires its PI-enabled suite")
        validate_mutex_configuration(publication.get("native_configuration"), True, True)
    elif publication.get("native_configuration") is not None:
        raise ValueError("host publication fabricated native mutex configuration")
    people = {person["role"]: person for person in observed["participants"]}
    for field, role in (("paused_writer", "publication-writer-low"),
                        ("paused_reader", "publication-reader-low")):
        state = publication.get(field)
        if not native:
            if state is not None:
                raise ValueError("host publication fabricated native peer wait")
            continue
        expected_state = {"tid": people[role]["start"]["tid"], "policy": report["fifo_policy"],
                          "base_priority": 120, "effective_priority": 120,
                          "waiting_semaphore": True, "runnable": False}
        if type(state) is not dict or any(type(state.get(key)) is not type(value) or state.get(key) != value
                                         for key, value in expected_state.items()):
            raise ValueError("production publication peer was not paused at its declared priority")
        if type(state.get("state")) is not int or state["state"] < 0:
            raise ValueError("invalid production publication peer state")
    comparison = publication.get("comparison")
    expected_comparison = {"rounds": 4096, "imu_sources": 2, "readers": 2, "gnss_enabled": False,
                           "scope": "same-image-instrumented-uncontended",
                           "memory_scope": "requested-rust-global-allocator"}
    if type(comparison) is not dict or any(type(comparison.get(key)) is not type(value) or comparison.get(key) != value
                                         for key, value in expected_comparison.items()):
        raise ValueError("production publication comparison changed its workload or scope")
    calls = {"alloc", "zeroed", "realloc", "dealloc", "failed"}
    fields = calls | {"live_before", "live_after", "blocks_before", "blocks_after"}
    phases = ("setup", "first_use", "steady", "teardown")
    for variant in ("mutex", "publication"):
        cost = comparison.get(variant)
        if type(cost) is not dict or type(cost.get("elapsed_ns")) is not int or cost["elapsed_ns"] <= 0 or cost.get("fully_reclaimed") is not True:
            raise ValueError("invalid production publication cost or reclamation")
        previous = None
        for phase in phases:
            window = cost.get(phase)
            if type(window) is not dict or set(window) != fields or any(type(value) is not int or value < 0 for value in window.values()) or window["failed"] != 0:
                raise ValueError("invalid publication allocator window")
            if window["blocks_after"] - window["blocks_before"] != window["alloc"] + window["zeroed"] - window["dealloc"]:
                raise ValueError("publication allocation block accounting is inconsistent")
            if previous is not None and any(window[f"{field}_before"] != previous[f"{field}_after"] for field in ("live", "blocks")):
                raise ValueError("publication allocator windows are not contiguous")
            if phase == "steady" or (variant == "publication" and phase == "first_use"):
                if any(window[field] != 0 for field in calls) or window["live_before"] != window["live_after"]:
                    raise ValueError("allocation in a publication zero-call window")
            previous = window
        if any(cost["setup"][f"{field}_after"] <= cost["setup"][f"{field}_before"] for field in ("live", "blocks")):
            raise ValueError("publication setup did not retain the compared storage")
        if any(cost[phase][f"{field}_after"] < cost["setup"][f"{field}_after"]
               for phase in ("first_use", "steady") for field in ("live", "blocks")):
            raise ValueError("publication comparison retired its storage before teardown")
        if any(cost["teardown"][f"{field}_after"] != cost["setup"][f"{field}_before"] for field in ("live", "blocks")):
            raise ValueError("publication storage was not fully reclaimed")


def validate_capture_pool(observed, report, specification):
    if report["case"] != "capture-pool-progress":
        if "capture_pool" in observed:
            raise ValueError("unrelated case cannot carry pooled capture evidence")
        return
    capture = observed.get("capture_pool")
    expected = {
        "scope": "fixture-pooled-scalar-transport",
        "pause_stage": "after-slot-claim-before-payload",
        "capacity": 512, "sources": 3, "other_source_records": 511, "restart_records": 2,
        "rejected_full": 1, "rejected_stale": 1, "source_order": "per-source-fifo",
        "complete_payloads": True, "sticky_terminal": True, "clean_restart": True,
        "pause_hits": 1, "first_missing_source": 1, "first_missing_sequence": 511,
        "atomic_16": True, "atomic_32": True,
    }
    if type(capture) is not dict or any(type(capture.get(key)) is not type(value) or capture.get(key) != value
                                       for key, value in expected.items()):
        raise ValueError("pooled capture progress or lifecycle evidence missing")
    checkpoint_fields = ("occupied", "busy_sources", "free_slots")
    for field, values in (("after_claim", (1, 1, 511)), ("full", (512, 1, 0)),
                          ("pending", (1, 1, 511)), ("retired", (0, 0, 512)),
                          ("restarted", (0, 0, 512))):
        checkpoint = capture.get(field)
        if type(checkpoint) is not dict or set(checkpoint) != set(checkpoint_fields):
            raise ValueError("missing pooled capture checkpoint")
        if any(type(checkpoint[key]) is not int for key in checkpoint_fields) or tuple(checkpoint[key] for key in checkpoint_fields) != values:
            raise ValueError("pooled capture slot or obligation accounting changed")
    native = report["domain"] == "nuttx-target"
    pointer_bytes = capture.get("pointer_bytes")
    if type(pointer_bytes) is not int or pointer_bytes not in ((4,) if native else (4, 8)):
        raise ValueError("unsupported pooled capture storage layout")
    storage = capture.get("storage")
    expected_storage = {
        "payload_heap_bytes": 18432, "index_heap_bytes": 3072, "bitmap_bytes": 64,
        "cursor_bytes": 24, "storage_inline_bytes": 88 + 8 * pointer_bytes,
        "raw_components_bytes": 21592,
        "scope": "storage-components-excludes-gate-arc-thread-and-allocator-overhead",
    }
    if type(storage) is not dict or set(storage) != set(expected_storage) or any(
            type(storage[key]) is not type(value) or storage[key] != value
            for key, value in expected_storage.items()):
        raise ValueError("pooled capture storage accounting or scope changed")
    if native:
        if specification.get("suite") != "capture":
            raise ValueError("pooled capture requires its PI-enabled suite")
        validate_mutex_configuration(capture.get("native_configuration"), True, True)
    elif capture.get("native_configuration") is not None:
        raise ValueError("host pooled capture fabricated native mutex configuration")
    roles = {participant["role"]: participant for participant in observed["participants"]}
    for field, role in (("low_after_claim", "capture-producer-low"),
                        ("low_after_other_progress", "capture-producer-low"),
                        ("owner_waiting", "capture-owner-high")):
        state = capture.get(field)
        if not native:
            if state is not None:
                raise ValueError("host pooled capture fabricated native peer wait")
            continue
        person = roles[role]
        required = {"tid": person["start"]["tid"], "policy": report["fifo_policy"],
                    "base_priority": person["requested_priority"],
                    "effective_priority": person["requested_priority"],
                    "waiting_semaphore": True, "runnable": False}
        if type(state) is not dict or any(type(state.get(key)) is not type(value) or state.get(key) != value
                                          for key, value in required.items()):
            raise ValueError("pooled capture peer was not paused at its declared priority")
        if type(state.get("state")) is not int or state["state"] <= 0:
            raise ValueError("invalid pooled capture peer state")


def parse(text, build, case, token, physical=False):
    if case not in selected_cases(build["inputs"]) or not re.fullmatch(r"[A-Za-z0-9-]{8,64}", token):
        raise ValueError("unknown case or invalid token")
    if FAULT.search(text):
        raise ValueError("native/fixture failure in retained console")
    text = text.replace("\x1b[K", "").replace("\x1b[2K", "").replace("\r", "")
    if re.search(r"[\x00-\x08\x0b-\x1f\x7f]", text):
        raise ValueError("unexpected console control bytes")
    build_id = build["build_id"]
    begin = f"OWNER_QUALIFICATION_BEGIN {build_id} {case} {token}"
    end = f"OWNER_QUALIFICATION_END {build_id} {case} {token}"
    lines = text.splitlines()
    if lines.count(begin) != 1 or lines.count(end) != 1 or sum(line.startswith("OWNER_QUALIFICATION_BEGIN ") for line in lines) != 1 or sum(line.startswith("OWNER_QUALIFICATION_END ") for line in lines) != 1:
        raise ValueError("missing/duplicate/wrong invocation boundary")
    values = [line.removeprefix("OWNER_QUALIFICATION_RESULT ") for line in lines if line.startswith("OWNER_QUALIFICATION_RESULT ")]
    if len(values) != 1:
        raise ValueError("expected one result")
    report = unique_json(values[0])
    if lines.index(begin) >= lines.index("OWNER_QUALIFICATION_RESULT " + values[0]) or lines.index(end) <= lines.index("OWNER_QUALIFICATION_RESULT " + values[0]):
        raise ValueError("invalid result ordering")
    domain = build["inputs"]["kind"]
    if any(type(report.get(key)) is not type(value) or report.get(key) != value for key, value in {"schema": "owner-qualification-v1", "case": case, "token": token,
            "build_id": build_id, "domain": domain, "pass": True,
            "native_scheduling": domain == "nuttx-target", "physical_execution_verified": False,
            "timing_qualified": False, "production_qualified": False,
            "quiescence_basis": "all-fixture-workers-joined"}.items()):
        raise ValueError("incorrect domain, identity or unsupported qualification")
    observed = report["observations"]
    if any(type(value) is not int or value < 0 for key, value in observed.items()
           if key not in ("participants", "endpoints_closed", "mutex", "mutex_nested", "latest", "publication", "capture_pool")):
        raise ValueError("invalid observation count")
    if any(type(observed[field]) is not int for field in FIELDS) or tuple(observed[field] for field in FIELDS) != CASES[case]:
        raise ValueError("incomplete work, wait or joined cleanup")
    if type(observed["native_waits"]) is not int or observed["native_waits"] != (CASES[case][-1] if domain == "nuttx-target" else 0):
        raise ValueError("incorrect native wait evidence")
    if observed["endpoints_closed"] is not True:
        raise ValueError("incomplete endpoint retirement")
    participants = observed["participants"]
    if Counter((p["role"], p["requested_priority"]) for p in participants) != Counter(ROLES[case]):
        raise ValueError("missing or relabeled participant")
    for p in participants:
        if domain == "nuttx-target":
            if type(report["fifo_policy"]) is not int or any(p[phase]["policy"] != report["fifo_policy"] or p[phase]["priority"] != p["requested_priority"] or type(p[phase]["tid"]) is not int or p[phase]["tid"] <= 0 for phase in ("start", "end")):
                raise ValueError("participant did not retain its native schedule")
            if p["before"] != p["restored"] or p["start"]["tid"] != p["end"]["tid"]:
                raise ValueError("schedule was not restored")
        elif report["fifo_policy"] is not None or any(value is not None for phase in ("before", "start", "end", "restored") for value in p[phase].values()):
            raise ValueError("host run fabricated native scheduling")
    if case == "owner-independent-inputs" and (observed["important_observations"], observed["ordinary_observations"], observed["stop_requests"]) != (1, 2, 1):
        raise ValueError("independent inputs incomplete")
    if case == "owner-fairness-deadline" and (observed["fairness_links_before_ordinary"] > 7 or observed["deadline_observations"] != 1 or observed["stop_requests"] != 2):
        raise ValueError("fairness/deadline control failed")
    if case == "owner-failure-recovery" and (observed["failure_observations"], observed["stop_requests"]) != (3, 2):
        raise ValueError("failure/retry witnesses missing")
    validate_mutex(observed, report, build["inputs"])
    validate_nested_mutex(observed, report, build["inputs"])
    validate_publication(observed, report, build["inputs"])
    validate_capture_pool(observed, report, build["inputs"])
    if case == "latest-value-progress":
        latest = observed.get("latest")
        expected = {"paused_writer_reads": 2, "paused_reader_publications": 32, "fanout_skew_checks": 1, "rounds": 4096}
        if type(latest) is not dict or any(type(latest.get(key)) is not int or latest[key] != count
                                           for key, count in expected.items()):
            raise ValueError("latest-value progress witnesses missing")
        payloads = latest.get("payloads")
        fields = {"record_bytes", "mutex_elapsed_ns", "mailbox_elapsed_ns"}
        if type(payloads) is not list or len(payloads) != 3 or any(
                type(payload) is not dict or set(payload) != fields or
                any(type(value) is not int or value <= 0 for value in payload.values()) for payload in payloads):
            raise ValueError("invalid latest-value comparison")
    elif "latest" in observed:
        raise ValueError("latest-value results present for a different case")
    if physical:
        if domain != "nuttx-target":
            raise ValueError("host execution cannot become target evidence")
        command = f"owner_qualification {case} {token}"
        before = text[:text.index(begin)]
        after = text[text.index(end) + len(end):]
        if before.count(command) != 1 or f"OQ_STATUS_{token}:0" not in after:
            raise ValueError("fresh boot, exact command or clean exit missing")
        if re.findall(rf"(?m)^OQ_STATUS_{re.escape(token)}:([^\n]+)$", after) != ["0"] or "nsh>" not in after.split(f"OQ_STATUS_{token}:0", 1)[1]:
            raise ValueError("missing terminal status/prompt")
        boot, _, remainder = text.partition(command)
        if not fresh_shell_boot(boot) or BOOT_MARKERS.search(remainder):
            raise ValueError("missing or repeated fresh shell boot")
    return report


def build_target(args):
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    specification = inputs("nuttx-target", args.profile, args.suite)
    environment = os.environ.copy()
    environment["OWNER_QUALIFICATION_BUILD_ID"] = identity(specification)
    command = [args.cargo, "nxrs", "build", "--manifest-path", str(HERE / "Cargo.toml"),
               "--profile", args.profile, "--command", "owner_qualification", "--stack-size", "16384",
               "--target-c-source", str(HERE / "native.c"),
               "--out", str(out / "firmware")]
    if args.suite in MUTEX_CONTROLS:
        mode, reason = MUTEX_CONTROLS[args.suite]
        command += ["--qualification-mutex-policy", mode, "--qualification-mutex-reason", reason]
    with (out / "build.log").open("w") as log:
        subprocess.run(command, cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT, check=True)
    firmware = out / "firmware"
    config = (firmware / "resolved.config").read_text()
    validate_config(config, specification)
    validate_policy_receipt(unique_json((firmware / "kconfig-requests.json").read_text()), specification)
    selected = []
    for line in (firmware / "binaries.sha256").read_text().splitlines()[:2]:
        expected, path = line.split("  ", 1)
        path = Path(path).resolve()
        path.relative_to(firmware)
        if digest(path) != expected:
            raise ValueError("normal frontend final image digest disagrees")
        selected.append(path)
    if len(selected) != 2 or selected[0].read_bytes()[:4] != b"\x7fELF":
        raise ValueError("normal frontend image inventory missing")
    image_name = "firmware" + selected[1].suffix
    artifacts = {"firmware.elf": selected[0], image_name: selected[1]}
    for path in firmware.iterdir():
        if path.is_file() and path.suffix in (".json", ".txt", ".config", ".sha256"):
            artifacts["frontend/" + path.name] = path
    retain_build(out, specification, artifacts, executable="firmware.elf", image=image_name)
    print(out / "build.json")


def host(args):
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    specification = inputs("host-functional")
    environment = os.environ.copy()
    environment["OWNER_QUALIFICATION_BUILD_ID"] = identity(specification)
    command = [args.cargo, "build", "--locked", "--release", "--manifest-path", str(HERE / "Cargo.toml"), "--target-dir", str(out / "target")]
    with (out / "build.log").open("w") as log:
        subprocess.run(command, cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT, check=True)
    binary = out / "target/release/nxrs-owner-qualification"
    build = retain_build(out, specification, {"owner-qualification": binary}, executable="owner-qualification")
    for case in selected_cases(specification):
        run = out / "runs" / case
        run.mkdir(parents=True)
        token = secrets.token_hex(12)
        try:
            process = subprocess.run([str(binary), case, token], cwd=ROOT, capture_output=True, timeout=20)
            console, status = process.stdout + process.stderr, process.returncode
        except subprocess.TimeoutExpired as error:
            console, status = (error.stdout or b"") + (error.stderr or b""), None
        (run / "console.log").write_bytes(console)
        receipt = {"schema": "owner-qualification-capture-v1", "case": case, "token": token,
                   "build_directory": "../..", "build_sha256": digest(out / "build.json"),
                   "console_sha256": digest(run / "console.log"), "runtime": "host", "exit_code": status,
                   "capture_complete": False, "physical_execution_verified": False}
        save(run / "capture.json", receipt)
        if status != 0:
            raise ValueError(f"host case failed: {case}; retained {run}")
        try:
            analysis = parse((run / "console.log").read_text(), build, case, token)
        except (ValueError, KeyError, TypeError) as error:
            receipt["error"] = str(error)
            save(run / "capture.json", receipt)
            raise
        save(run / "analysis.json", analysis)
        receipt["capture_complete"] = True
        save(run / "capture.json", receipt)
        replay(run, out)
    print(f"Passed {len(selected_cases(specification))} fresh-process host cases; {out}")


def capture(args):
    import serial
    build_root = args.build.resolve()
    build = validate_build(build_root)
    if build["inputs"]["kind"] != "nuttx-target":
        raise ValueError("physical capture requires a NuttX build")
    if args.case not in selected_cases(build["inputs"]):
        raise ValueError("case is not supported by the selected firmware suite")
    raw_session = args.session.read_bytes()
    session = unique_json(raw_session.decode())
    expected = {"schema": "owner-qualification-session-v1", "case": args.case, "build_id": build["build_id"],
                "image_sha256": build["artifacts"][build["image"]]["sha256"], "fresh_boot": True, "flash_verified": True}
    if any(session.get(key) != value for key, value in expected.items()) or not session.get("measurement_session"):
        raise ValueError("session does not identify the selected fresh-boot image/case")
    token = session.get("token", "")
    if not re.fullmatch(r"[A-Za-z0-9-]{8,64}", token):
        raise ValueError("invalid run token")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    (out / "session.json").write_bytes(raw_session)
    # Retain exactly one immutable copy so standalone replay needs no checkout.
    (out / "build").mkdir()
    shutil.copyfile(build_root / "build.json", out / "build/build.json")
    shutil.copytree(build_root / "retained", out / "build/retained")
    receipt = {"schema": "owner-qualification-capture-v1", "case": args.case, "token": token,
               "build_directory": "build", "build_sha256": digest(out / "build/build.json"),
               "runtime": "physical-operator-asserted", "capture_complete": False,
               "physical_execution_verified": False, "session_sha256": digest(out / "session.json"),
               "pre_command_reconnects": 0}
    data = bytearray()
    try:
        print("Serial capture ready; reset into the verified image.", flush=True)
        deadline = time.monotonic() + 90
        sent = False
        status_sent = False
        finish_after = None
        port = None
        try:
            while time.monotonic() < deadline:
                if port is None:
                    try:
                        port = serial.Serial(args.port, 115200, timeout=0.1)
                    except OSError:
                        # Native USB may disappear and re-enumerate on reset.
                        # This retry is only allowed before any command write.
                        receipt["pre_command_reconnects"] += 1
                        time.sleep(0.1)
                        continue
                try:
                    chunk = port.read(4096)
                except OSError as error:
                    port.close()
                    port = None
                    if sent:
                        raise ValueError("serial disconnected after command submission") from error
                    receipt["pre_command_reconnects"] += 1
                    time.sleep(0.1)
                    continue
                data.extend(chunk)
                text = data.decode("utf-8", errors="strict")
                if FAULT.search(text):
                    raise ValueError("native/fixture failure; retained console")
                if len(data) > 1_048_576:
                    raise ValueError("console output limit")
                if not sent and "nsh>" in text and fresh_shell_boot(text):
                    # A partial write may already start the case. Never reopen
                    # or retry a command after reaching this point.
                    sent = True
                    port.write(f"owner_qualification {args.case} {token}\n".encode())
                marker = f"OWNER_QUALIFICATION_END {build['build_id']} {args.case} {token}"
                if sent and not status_sent and marker in text and "nsh>" in text.split(marker, 1)[1]:
                    port.write(f"echo OQ_STATUS_{token}:$?\n".encode())
                    status_sent = True
                statuses = re.findall(rf"(?m)^OQ_STATUS_{re.escape(token)}:([^\r\n]+)[\r\n]", text)
                if status_sent and statuses and statuses != ["0"]:
                    raise ValueError("nonzero or ambiguous target exit status")
                if status_sent and statuses == ["0"] and "nsh>" in text.split(f"OQ_STATUS_{token}:0", 1)[1]:
                    if finish_after is None:
                        finish_after = time.monotonic() + 1
                    if time.monotonic() >= finish_after:
                        break
            else:
                raise ValueError("fresh-boot capture deadline")
        finally:
            if port is not None:
                port.close()
        report = parse(data.decode(), build, args.case, token, physical=True)
        receipt["capture_complete"] = True
        save(out / "analysis.json", report)
    except Exception as error:
        receipt["error"] = str(error)
        raise
    finally:
        (out / "console.log").write_bytes(data)
        receipt["console_sha256"] = digest(out / "console.log")
        save(out / "capture.json", receipt)
    replay(out)
    print(out / "analysis.json")


def replay(run, artifact_root=None):
    run = run.resolve()
    capture_record = unique_json(contained(run, "capture.json").read_text())
    if capture_record.get("schema") != "owner-qualification-capture-v1" or capture_record.get("physical_execution_verified") is not False:
        raise ValueError("invalid capture identity or qualification")
    if capture_record.get("build_directory") == "../..":
        if artifact_root is None or run.parent.parent != artifact_root.resolve():
            raise ValueError("run corpus requires its explicit --artifact-root")
        build_root = artifact_root.resolve()
    elif capture_record.get("build_directory") == "build":
        build_root = run / "build"
    else:
        raise ValueError("unsupported build location")
    if digest(contained(build_root, "build.json")) != capture_record["build_sha256"]:
        raise ValueError("changed build receipt")
    build = validate_build(build_root)
    console = contained(run, "console.log")
    if digest(console) != capture_record["console_sha256"]:
        raise ValueError("changed console")
    if capture_record.get("capture_complete") is not True:
        raise ValueError("retained failed/incomplete capture")
    physical = capture_record["runtime"] == "physical-operator-asserted"
    if physical:
        if digest(contained(run, "session.json")) != capture_record["session_sha256"]:
            raise ValueError("changed physical session")
        session = unique_json((run / "session.json").read_text())
        expected = {"schema": "owner-qualification-session-v1", "case": capture_record["case"], "token": capture_record["token"],
                    "build_id": build["build_id"], "image_sha256": build["artifacts"][build["image"]]["sha256"],
                    "fresh_boot": True, "flash_verified": True}
        if any(session.get(key) != value for key, value in expected.items()) or not session.get("measurement_session"):
            raise ValueError("session/build/capture mismatch")
    elif capture_record["runtime"] == "qemu":
        if build["inputs"]["kind"] != "nuttx-target" or build["inputs"]["profile"] != "mps2-an521-mock" or capture_record.get("machine") != "mps2-an521":
            raise ValueError("QEMU capture requires its selected ARM NuttX image")
        if type(capture_record.get("exit_code")) is not int or capture_record["exit_code"] != 0 or not capture_record.get("qemu_version") or not re.fullmatch(r"[0-9a-f]{64}", capture_record.get("qemu_sha256", "")):
            raise ValueError("missing QEMU provenance or clean process exit")
    elif capture_record["runtime"] != "host" or type(capture_record.get("exit_code")) is not int or capture_record.get("exit_code") != 0 or build["inputs"]["kind"] != "host-functional":
        raise ValueError("invalid host process exit")
    result = parse(console.read_text(), build, capture_record["case"], capture_record["token"], physical or capture_record["runtime"] == "qemu")
    if result != unique_json(contained(run, "analysis.json").read_text()):
        raise ValueError("saved analysis differs from fresh replay")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    b = sub.add_parser("build")
    b.add_argument("--profile", required=True)
    b.add_argument("--out", type=Path, required=True)
    b.add_argument("--cargo", default="cargo")
    b.add_argument("--suite", choices=("owner", "mutex-inherit", "mutex-no-inherit", "publication", "capture"), default="owner",
                   help="select the native contract suite; owner preserves its PI-disabled baseline")
    h = sub.add_parser("host")
    h.add_argument("--out", type=Path, required=True)
    h.add_argument("--cargo", default="cargo")
    i = sub.add_parser("inspect", help="read-only validation of all retained build evidence")
    i.add_argument("--build", type=Path, required=True)
    c = sub.add_parser("capture")
    c.add_argument("--build", type=Path, required=True)
    c.add_argument("--case", choices=CASES, required=True)
    c.add_argument("--session", type=Path, required=True)
    c.add_argument("--port", required=True)
    c.add_argument("--out", type=Path, required=True)
    r = sub.add_parser("replay")
    r.add_argument("run", type=Path)
    r.add_argument("--artifact-root", type=Path)
    args = parser.parse_args()
    if args.action == "build": build_target(args)
    elif args.action == "host": host(args)
    elif args.action == "capture": capture(args)
    elif args.action == "inspect": print(json.dumps(inspect(args.build), indent=2))
    else:
        report = replay(args.run, args.artifact_root)
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"owner qualification refused: {error}", file=sys.stderr)
        raise SystemExit(1)

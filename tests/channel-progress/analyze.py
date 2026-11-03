#!/usr/bin/env python3
"""Validate exact-path progress evidence without promoting a recovered stall.

Progress observations do not require calibrated clocks. A successful diagnostic
can establish a stalled FIFO contract, while a no-repair result covers only the
named interleaving. Neither result admits a production transport by itself.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys

PREFIX = "CHANNEL_PROGRESS "
SCHEMA = "channel-progress-v1"
STD_TRANSFER_SCHEMA = "std-transfer-progress-v1"
STD_TRANSFER_BUILD_MODE = "std-mutex-suite"
FIFO_SCHEMA = "fifo-channel-progress-v1"
FIFO_BUILD_MODE = "fifo-channel-suite"
FIFO_MODES = (
    "fifo-try-recv-reserved", "fifo-try-recv-published",
    "fifo-select-reserved", "fifo-select-published",
    "fifo-try-send-reserved", "fifo-try-send-reclaimed",
)
STD_TRANSFER_MODES = (
    "mutex-admit-held", "mutex-admit-released",
    "mutex-take-held", "mutex-take-released",
    "condvar-wait-publish", "condvar-publish-before-wait", "condvar-close-wait",
)
MODES = {
    "host-functional": 0,
    "direct-reserved": 1,
    "direct-published": 2,
    "select-reserved": 1,
    "select-published": 2,
    "send-reserved": 3,
    "send-reclaimed": 4,
}
CHANNEL_MODES = frozenset(MODES)
MODES.update({mode: 0 for mode in STD_TRANSFER_MODES})
MODES.update({mode: 5 for mode in FIFO_MODES})
SOURCE_MODES = {
    0: "clean", 1: "send-reserved", 2: "send-published",
    3: "recv-reserved", 4: "recv-reclaimed", 5: "fifo-ordered",
}
PINS = {
    "crossbeam-channel": (
        "0.5.17", "98b0cc327b5bc766e7fda9c9260cc0fa81b43a8e240440422dff70788e3f9ef1"),
    "crossbeam-utils": (
        "0.8.23", "a31eee39dddec8330830986fcd7625edb5a24ec90ea038215273bbc3adb08ac6"),
}
FAILURE = re.compile(
    r'CHANNEL_PROGRESS FAIL|PANIC|ASSERTION|HardFault|UsageFault|BusFault|'
    r'qemu: fatal:|Lockup:|"record"\s*:\s*"(?:fatal|failure)"',
    re.IGNORECASE,
)
HEX256 = re.compile(r"[0-9a-f]{64}\Z")
HEX160 = re.compile(r"[0-9a-f]{40}\Z")
IDENTITY_FIELDS = (
    "mode", "hook_stage", "platform", "target", "sdk", "source_sha256",
    "kernel_revision", "apps_revision", "build_nonce", "crossbeam_receipt",
    "compiler_identity", "std_library_tree_sha256", "staged_inputs_sha256",
)


def identity_fields(mode):
    return IDENTITY_FIELDS + (("fifo_kernel_source_sha256",)
                              if expected_build_mode(mode) == FIFO_BUILD_MODE else ())


def canonical(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def integer(row, key, lower=0, upper=None):
    value = row.get(key)
    require(type(value) is int and value >= lower
            and (upper is None or value <= upper), f"invalid {key}")
    return value


def boolean(row, key):
    value = row.get(key)
    require(type(value) is bool, f"invalid {key}")
    return value


def string(row, key):
    value = row.get(key)
    require(isinstance(value, str) and bool(value.strip()), f"missing {key}")
    return value


def hash_string(value, label):
    require(isinstance(value, str) and HEX256.fullmatch(value), f"invalid {label}")
    return value


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"duplicate JSON field: {key}")
        result[key] = value
    return result


def no_constant(value):
    raise ValueError(f"non-finite JSON value: {value}")


def decode_json(text):
    return json.loads(text, object_pairs_hook=unique_keys, parse_constant=no_constant)


def read_json(path):
    return decode_json(Path(path).read_text())


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def records(text, schema=SCHEMA):
    require(not FAILURE.search(text), "failure or kernel fault in transcript")
    rows = []
    for line in text.replace("\r", "").splitlines():
        if line.startswith(PREFIX):
            row = decode_json(line[len(PREFIX):])
            require(isinstance(row, dict) and row.get("schema") == schema,
                    "unknown record schema")
            require(row.get("record") not in {"failure", "fatal"},
                    f"target failed: {row.get('reason', 'unspecified')}")
            rows.append(row)
        elif "CHANNEL_PROGRESS" in line:
            raise ValueError("truncated or interleaved result record")
    require(len(rows) == 2, "expected exactly one metadata record and one result")
    require(rows[0].get("record") == "metadata" and rows[1].get("record") == "result",
            "wrong record order or mixed runs")
    run_id = string(rows[0], "run_id")
    require(rows[1].get("run_id") == run_id, "mixed run identities")
    return rows


def _schedule(metadata, role, policy, priority):
    require(integer(metadata, role + "_policy", -1, 2) == policy
            and integer(metadata, role + "_priority", -1, 255) == priority,
            f"unverified scheduling contract for {role}")


def _state(result, role, expected_priority, *, host=False):
    state = result.get(role + "_state_at_deadline")
    priority = integer(result, role + "_priority_at_deadline", -1, 255)
    if host or expected_priority == -1:
        require(state == "unavailable" and priority == -1,
                f"unexpected native state claim for {role}")
    elif state == "absent":
        require(priority == -1, f"absent {role} has a priority")
    else:
        require(state in {"wait_sem", "ready", "running", "other"}
                and priority == expected_priority,
                f"unverified original priority/state for {role}")
    return state


def std_transfer_validator():
    spec = importlib.util.spec_from_file_location(
        "channel_progress_std_transfer_validation",
        Path(__file__).with_name("mutex_analyze.py"))
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    return validator


def fifo_validator():
    spec = importlib.util.spec_from_file_location(
        "channel_progress_fifo_validation", Path(__file__).with_name("fifo_analyze.py"))
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    return validator


def kernel_source_validator():
    spec = importlib.util.spec_from_file_location(
        "channel_progress_fifo_sources_validation", Path(__file__).with_name("fifo_sources.py"))
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    return validator


def analyze(text):
    # The primitive contract has its own schema and validator. Its one-attempt
    # contention result must never be confused with channel delivery or with
    # qualification of the three-arm Crossbeam owner.
    for line in text.replace("\r", "").splitlines():
        if line.startswith(PREFIX):
            first = decode_json(line[len(PREFIX):])
            if isinstance(first, dict) and first.get("schema") == STD_TRANSFER_SCHEMA:
                metadata, result = records(text, STD_TRANSFER_SCHEMA)
                return std_transfer_validator().analyze(metadata, result)
            if isinstance(first, dict) and first.get("schema") == FIFO_SCHEMA:
                metadata, result = records(text, FIFO_SCHEMA)
                return fifo_validator().analyze(metadata, result)
            break
    metadata, result = records(text)
    mode = metadata.get("mode")
    require(mode in CHANNEL_MODES, "unknown channel experiment mode")
    stage = integer(metadata, "hook_stage", 0, 4)
    require(stage == MODES[mode], "instrumentation/mode mismatch")
    host = mode == "host-functional"
    select = mode.startswith("select-")
    reverse = mode.startswith("send-")
    require(metadata.get("runtime") == ("linux-functional" if host else "nuttx"),
            "host and target experiments cannot exchange runtime identities")
    require(metadata.get("transport") == "crossbeam-channel"
            and metadata.get("transport_version") == PINS["crossbeam-channel"][0],
            "unexpected transport source")
    require(integer(metadata, "capacity", 1) == 8, "unexpected queue capacity")
    require(integer(metadata, "payload_bytes", 1) == 16, "unexpected payload size")
    require(integer(metadata, "provider_count", 1) == 1, "unexpected provider count")
    require(integer(metadata, "arm_count", 1) == (3 if host or select else 1),
            "wrong selection arm count")
    require(metadata.get("selection_bias") == (
        "stop-important-ordinary" if host or select else "none"),
        "wrong selected-owner priority order")
    api = "functional-smoke" if host else "select_biased" if select else "send" if reverse else "recv"
    require(metadata.get("witness_api") == api, "wrong API under test")
    require(boolean(metadata, "single_core_tcb_witness") == (not host),
            "native scheduling proof is missing or falsely claimed")
    build_id = string(metadata, "build_id")
    require(HEX256.fullmatch(build_id) or (host and build_id == "unrecorded-host"),
            "missing firmware build identity")
    for row in (metadata, result):
        for field in ("physical_measurement", "physical_timing_qualified",
                      "timing_qualified", "priority_progress_qualified"):
            if field in row:
                require(boolean(row, field) is False,
                        "runtime cannot self-certify physical, timing or production progress")
    require(boolean(result, "priority_progress_qualified") is False,
            "one experiment cannot qualify a production transport")
    require(boolean(result, "timing_qualified") is False,
            "progress fixture is not a timing benchmark")
    for field in ("success", "threads_joined", "watchdog_joined", "delivered",
                  "consumer_entered", "producer_entered"):
        require(boolean(result, field), f"incomplete experiment: {field}")
    for field in ("deadline_hit", "hook_error"):
        require(not boolean(result, field), f"failed experiment: {field}")

    sent = integer(result, "messages_sent", 1)
    received = integer(result, "messages_received", 1)
    require(sent == received == (10 if host else 9 if reverse else 1),
            "wrong payload delivery count")
    done_consumer = boolean(result, "consumer_done_before_recovery")
    done_producer = boolean(result, "producer_done_before_recovery")
    done_waker = boolean(result, "waker_done_before_recovery")
    returned = boolean(result, "hook_returned_before_recovery")
    recovery = boolean(result, "recovery_applied")
    stalled = boolean(result, "reservation_stall_observed")
    completed = boolean(result, "completed_without_repair")
    progress = boolean(result, "progress_contract_satisfied")
    hook_count = integer(result, "hook_hit_count", 0, 1)
    gate_waits = boolean(result, "initial_gate_waits_verified")
    selected_wait = boolean(result, "initial_select_wait_verified")
    registered = integer(result, "selection_registered_arms", 0, 3)
    wake_consumed = boolean(result, "independent_wake_consumed")
    hook = integer(result, "hook_timestamp_ns")
    entered = integer(result, "operation_enter_timestamp_ns")
    observed = integer(result, "observer_timestamp_ns")
    observation = integer(result, "observation_ns")

    if host:
        for role in ("sender", "receiver", "observer", "watchdog"):
            _schedule(metadata, role, 0, 0)
        _schedule(metadata, "waker", -1, -1)
        for role in ("consumer", "producer", "waker"):
            _state(result, role, -1, host=True)
        require(done_consumer and done_producer and done_waker
                and not returned and not recovery and not stalled
                and not completed and not progress,
                "host smoke cannot claim the native progress contract")
        require(hook_count == registered == hook == entered == observed == observation == 0
                and not gate_waits and not selected_wait and not wake_consumed
                and result.get("selected_target_arm") == "none",
                "host smoke cannot claim native interleaving observations")
        outcome = "host-functional-only"
    else:
        sender_priority, receiver_priority = (180, 120) if reverse else (120, 180)
        for role, priority in (("sender", sender_priority), ("receiver", receiver_priority),
                               ("observer", 220), ("watchdog", 240)):
            _schedule(metadata, role, 1, priority)
        _schedule(metadata, "waker", 1 if select else -1, 160 if select else -1)
        consumer_state = _state(result, "consumer", receiver_priority)
        producer_state = _state(result, "producer", sender_priority)
        waker_state = _state(result, "waker", 160 if select else -1)
        require(not select or done_waker or waker_state != "absent",
                "unfinished waker cannot already be absent")
        require(hook_count == 1 and gate_waits, "unverified native hook/gate interleaving")
        require(selected_wait == select and registered == (3 if select else 0)
                and wake_consumed == select
                and result.get("selected_target_arm") == ("important" if select else "none"),
                "selected wait needs three registered arms and an independent consumed wake")
        require(hook > 0 and hook <= entered <= observed and observed - entered == observation
                and observation >= 50_000_000,
                "inconsistent hook, operation-entry or observation boundary")
        no_repair = done_consumer and done_producer and returned and done_waker and not recovery
        observed_stall = (not done_consumer and not done_producer and not returned and recovery
                          and consumer_state == producer_state == "ready")
        require(no_repair or observed_stall,
                "neither complete no-repair execution nor an observed runnable priority stall")
        require(completed == progress == no_repair and stalled == observed_stall,
                "progress/stall labels disagree with independent observations")
        outcome = "completed-without-repair" if no_repair else "stall-observed"
    require(result.get("outcome") == outcome, "outcome disagrees with independent observations")
    return {
        "schema": "channel-progress-analysis-v1",
        "run_id": metadata["run_id"], "mode": mode, "fixture_valid": True,
        "outcome": outcome, "completed_without_repair": completed,
        "progress_contract_satisfied": progress,
        "build_bound": False, "runtime_kind": "linux-functional" if host else "unassigned",
        "physical_execution_verified": False, "priority_progress_qualified": False,
        "physical_timing_qualified": False, "timing_qualified": False,
        "hard_realtime_bound": False, "metadata": metadata, "result": result,
        "scope": ("host functional smoke only; no fixed-priority target qualification" if host else
                  "one forced interleaving of the stated FIFO contract; no general transport or timing proof"),
    }


def checked_file(directory, relative):
    require(isinstance(relative, str) and bool(relative), "missing artifact path")
    require(not Path(relative).is_absolute(), "artifact path must be relative")
    root = Path(directory).resolve()
    path = (root / relative).resolve()
    require(path.is_relative_to(root) and path.is_file(),
            "artifact escapes build directory or is missing")
    return path


def _hash_inventory(value, label):
    require(isinstance(value, dict) and bool(value), f"missing {label}")
    for name, digest in value.items():
        require(isinstance(name, str) and bool(name) and not Path(name).is_absolute()
                and ".." not in Path(name).parts, f"invalid {label} path")
        hash_string(digest, label + " digest")
    return value


def expected_build_mode(mode):
    require(mode in MODES or mode in {STD_TRANSFER_BUILD_MODE, FIFO_BUILD_MODE},
            "unknown experiment mode")
    if mode in STD_TRANSFER_MODES:
        return STD_TRANSFER_BUILD_MODE
    return FIFO_BUILD_MODE if mode in FIFO_MODES else mode


def hook_stage(mode):
    if mode == STD_TRANSFER_BUILD_MODE:
        return 0
    return 5 if mode == FIFO_BUILD_MODE else MODES[mode]


def build_receipt(path, mode):
    """Check retained compiler/source evidence and exact firmware/config bytes."""
    path = Path(path)
    if path.is_dir():
        path = path / "build.json"
    root = path.parent.resolve()
    receipt = read_json(path)
    require(isinstance(receipt, dict) and receipt.get("schema") == "channel-progress-build-v1",
            "unknown build receipt")
    require(receipt.get("status") == "complete", "incomplete build receipt")
    require(receipt.get("mode") == expected_build_mode(mode)
            and integer(receipt, "hook_stage", 0, 5) == hook_stage(mode), "wrong firmware experiment")
    require(receipt.get("runtime_kind") == "unmeasured", "build cannot certify execution")
    hash_string(receipt.get("build_id"), "build identity")
    require(isinstance(receipt.get("source_revision"), str)
            and HEX160.fullmatch(receipt["source_revision"]), "missing source revision")
    _hash_inventory(receipt.get("source_sha256"), "build source inventory")
    _hash_inventory(receipt.get("staged_inputs_sha256"), "staged fixture inventory")
    require(isinstance(receipt.get("compiler_identity"), dict)
            and bool(receipt["compiler_identity"]), "missing compiler identity")
    string(receipt["compiler_identity"], "version")
    hash_string(receipt["compiler_identity"].get("rustc_sha256"), "compiler executable")
    hash_string(receipt.get("std_library_tree_sha256"), "std library identity")
    fields = identity_fields(mode)
    require(all(key in receipt for key in fields), "incomplete build identity inputs")
    require(canonical({key: receipt[key] for key in fields}) == receipt["build_id"],
            "build identity does not match frozen inputs")
    artifacts = _hash_inventory(receipt.get("artifacts_sha256"), "retained artifact inventory")
    for relative, digest in artifacts.items():
        require(sha256(checked_file(root, relative)) == digest,
                f"retained artifact digest mismatch: {relative}")
    for file_key, hash_key in (("image_file", "image_sha256"), ("elf_file", "elf_sha256"),
                              ("config_file", "config_sha256")):
        relative = receipt.get(file_key)
        digest = hash_string(receipt.get(hash_key), hash_key)
        require(sha256(checked_file(root, relative)) == digest,
                f"{file_key} digest mismatch")
        require(artifacts.get(relative) == digest,
                f"{file_key} is absent from retained artifact inventory")
    configuration = receipt.get("resolved_configuration")
    require(isinstance(configuration, dict) and configuration.get("CONFIG_BUILD_FLAT") == "y"
            and configuration.get("CONFIG_SMP") in {"n", "absent"},
            "firmware must be flat and single-core")
    config_text = checked_file(root, receipt["config_file"]).read_text()
    config = {}
    for line in config_text.splitlines():
        if line.startswith("CONFIG_") and "=" in line:
            key, value = line.split("=", 1)
            config[key] = value
        elif line.startswith("# CONFIG_") and line.endswith(" is not set"):
            config[line[2:-11]] = "n"
    require(config.get("CONFIG_BUILD_FLAT") == "y" and config.get("CONFIG_SMP", "n") == "n",
            "actual firmware config disagrees with single-core flat receipt")
    _source_receipts(root, receipt, artifacts, mode)
    return receipt


def source_preparer():
    spec = importlib.util.spec_from_file_location(
        "channel_progress_source_validation", Path(__file__).with_name("prepare-crossbeam.py"))
    private = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(private)
    return private


def _source_receipts(root, receipt, artifacts, mode):
    # Filled against the private-source and actual Cargo-source receipt contract.
    # Source preparation validates the pinned archives and transformed bytes
    # before and after compilation; retained ledgers bind those observations.
    for key in ("crossbeam_receipt_file", "crossbeam_source_proof", "std_source_proof"):
        require(receipt.get(key) in artifacts, f"unbound {key}")
    source = read_json(checked_file(root, receipt["crossbeam_receipt_file"]))
    require(isinstance(source, dict), "invalid private source receipt")
    require(source == receipt.get("crossbeam_receipt"), "source receipt differs from build ledger")
    require(source.get("schema") == "crossbeam-progress-source-v1"
            and source.get("mode") == SOURCE_MODES[hook_stage(mode)]
            and integer(source, "hook_stage", 0, 5) == hook_stage(mode)
            and boolean(source, "instrumented") == (hook_stage(mode) != 0),
            "wrong private source instrumentation")
    cargo = read_json(checked_file(root, receipt["crossbeam_source_proof"]))
    require(isinstance(cargo, dict) and cargo.get("schema") == "channel-progress-cargo-sources-v1",
            "unknown Cargo source proof")
    sources = cargo.get("sources")
    require(isinstance(sources, dict) and set(sources) == set(PINS),
            "incomplete actual Cargo package source proof")
    original_prepared_root = None
    for package, (version, _) in PINS.items():
        record = sources[package]
        require(isinstance(record, dict) and record.get("source_matches") is True,
                "Cargo did not compile the selected private crate")
        actual = string(record, "src_path")
        expected = string(record, "expected_src_path")
        require(Path(actual) == Path(expected)
                and Path(actual).as_posix().endswith(
                    f"/crossbeam/sources/{package}-{version}/src/lib.rs"),
                "Cargo compiled a different source path")
        prepared_root = Path(expected).parents[3]
        if original_prepared_root is None:
            original_prepared_root = prepared_root
        require(original_prepared_root == prepared_root,
                "Cargo packages came from different private source cohorts")
        require(package in string(record, "package_id")
                and re.search(r"(?:#|@)" + re.escape(version) + r"\Z", record["package_id"]),
                "wrong actual Cargo package identity")
        _hash_inventory(record.get("artifact_sha256"), "compiled Cargo artifact inventory")
    private = source_preparer()
    archives = {}
    source_directory = Path(receipt["crossbeam_receipt_file"]).parent
    for package, (version, digest) in PINS.items():
        relative = (source_directory / "original" / f"{package}-{version}.crate").as_posix()
        require(artifacts.get(relative) == digest, "unbound original Crossbeam archive")
        archives[package] = checked_file(root, relative).read_bytes()
    reconstructed = private.expected_receipt(
        original_prepared_root, SOURCE_MODES[hook_stage(mode)], archives)
    require(source == reconstructed, "source ledger differs from pinned archive reconstruction")
    config_relative = (source_directory / "cargo-config.toml").as_posix()
    require(artifacts.get(config_relative) == source["patch_config_sha256"]
            and checked_file(root, config_relative).read_bytes()
            == private.config_bytes(original_prepared_root),
            "Cargo patch config differs from selected private sources")
    for name, digest in source["patches_sha256"].items():
        require(artifacts.get((source_directory / name).as_posix()) == digest,
                "unbound source witness patch")
    std = read_json(checked_file(root, receipt["std_source_proof"]))
    require(isinstance(std, dict) and std.get("selected_source_matches") is True
            and string(std, "std_source") == string(std, "expected_source"),
            "Cargo did not compile the selected std source")
    build_root = original_prepared_root.parent
    expected_std = build_root / "prepared/toolchain/lib/rustlib/src/rust/library/std/src/lib.rs"
    require(Path(std["std_source"]) == expected_std
            and Path(string(std, "rustc")) == build_root / "prepared/toolchain/bin/rustc",
            "std/compiler sources came from a different build cohort")
    for name in ("prepared/std-channel-hook.json", "prepared/std-library-sources.json",
                 "firmware/std-patch.json"):
        require(name in artifacts, "unbound common std source ledger")
    std_hook = read_json(checked_file(root, "prepared/std-channel-hook.json"))
    library = read_json(checked_file(root, "prepared/std-library-sources.json"))
    _hash_inventory(library, "std source inventory")
    require(isinstance(std_hook, dict) and std_hook.get("schema") == "std-channel-hook-v1"
            and std_hook.get("mode") == "latency" and std_hook.get("hook_stage") == 0
            and std_hook.get("channel_instrumented") is False,
            "progress image unexpectedly instruments Rust std")
    require(std_hook.get("library_tree_sha256") == receipt["std_library_tree_sha256"] == canonical(library)
            and std_hook.get("compiler") == receipt["compiler_identity"],
            "std/compiler receipt differs from build identity")
    original = std_hook.get("original", {})
    selected = std_hook.get("selected", {})
    require(isinstance(original, dict) and original == selected and original.get("sha256")
            == receipt.get("std_array_original_sha256") == receipt.get("std_array_sha256")
            == library.get("std/src/sync/mpmc/array.rs"),
            "Rust std array bytes changed in Crossbeam witness")
    ledger = read_json(checked_file(root, "firmware/std-patch.json"))
    require(isinstance(ledger, dict) and ledger.get("channel_qualification") == std_hook,
            "compiled std ledger differs from prepared source")
    if expected_build_mode(mode) in {STD_TRANSFER_BUILD_MODE, FIFO_BUILD_MODE}:
        std_transfer_validator().validate_std_sources(library, receipt["sdk"])
    if expected_build_mode(mode) == FIFO_BUILD_MODE:
        kernel = kernel_source_validator()
        kernel.validate_std_sources(library, receipt["sdk"])
        pinned = _hash_inventory(receipt.get("fifo_kernel_source_sha256"),
                                 "FIFO kernel source inventory")
        kernel.validate_retained_kernel(root / "prepared/fifo-kernel", pinned,
                                        receipt["kernel_revision"])
        for name, digest in pinned.items():
            require(artifacts.get("prepared/fifo-kernel/" + name) == digest,
                    "unbound retained FIFO kernel source")
        relative = receipt.get("fifo_kernel_source_proof")
        require(relative == "firmware/fifo-kernel-source.json" and relative in artifacts,
                "unbound compiled FIFO kernel source proof")
        proof = read_json(checked_file(root, relative))
        require(isinstance(proof, dict) and proof.get("schema") == "fifo-kernel-source-v1"
                and proof.get("kernel_revision") == kernel.KERNEL_REVISION
                and proof.get("source_sha256") == pinned
                and proof.get("selected_source_matches") is True
                and proof.get("source_root") == proof.get("expected_source_root")
                == str(build_root / "firmware/nuttx"),
                "compiled FIFO kernel differs from audited build sources")


def bind_build(summary, receipt):
    """Bind validated metadata to a receipt returned by :func:`build_receipt`."""
    metadata = summary["metadata"]
    require(receipt.get("schema") == "channel-progress-build-v1"
            and receipt.get("status") == "complete", "unvalidated build receipt")
    require(receipt.get("mode") == expected_build_mode(summary["mode"])
            and metadata.get("build_mode", summary["mode"]) == receipt.get("mode")
            and receipt.get("hook_stage") == metadata["hook_stage"]
            and receipt.get("build_id") == metadata["build_id"],
            "runtime and build identities differ")
    require(metadata["runtime"] == "nuttx", "host smoke cannot bind target firmware")
    result = dict(summary)
    result.update(build_bound=True, build=receipt)
    return result


def attach_execution(summary, session, fresh_boot_observed=False):
    """Associate execution records without authenticating operator hardware labels.

    A matching build ID and serial transcript bind the experiment's firmware.
    User-entered board/flash strings do not prove a physical device, readback or
    exclusive ownership. Keep historical physical sessions readable with that
    limitation explicit; no physical verification is granted by this API.
    """
    require(summary.get("build_bound") is True, "execution requires a bound firmware build")
    require(isinstance(session, dict) and session.get("schema") == "channel-progress-session-v1",
            "unknown execution session")
    kind = session.get("runtime_kind")
    require(kind in {"qemu", "physical"}, "unknown execution environment")
    receipt = summary["build"]
    require(session.get("build_id") == receipt["build_id"]
            and session.get("image_sha256") == receipt["image_sha256"],
            "executed image identity differs from bound build")
    string(session, "board")
    require(fresh_boot_observed is True, "fresh boot was not independently observed")
    if kind == "physical":
        for key in ("measurement_session", "board_serial", "flash_verification"):
            string(session, key)
        require(integer(session, "active_cores", 1) == 1,
                "physical execution must use the stated one-core contract")
    result = dict(summary)
    result.update(runtime_kind=kind, session=session,
                  physical_execution_verified=False,
                  execution_evidence="virtual" if kind == "qemu" else "operator-asserted")
    if kind == "physical":
        result["physical_evidence_scope"] = (
            "Operator-declared physical session; device identity, flash/readback and "
            "exclusive ownership are not independently verified.")
    return result


def require_no_repair(summary):
    require(summary.get("fixture_valid") is True and summary.get("build_bound") is True
            and summary.get("metadata", {}).get("runtime") == "nuttx"
            and summary.get("completed_without_repair") is True,
            "no-repair gate refused: requires an exact-build target experiment completed without priority repair")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("transcript", type=Path)
    parser.add_argument("--build-receipt", type=Path)
    parser.add_argument("--session", type=Path)
    parser.add_argument("--fresh-boot-observed", action="store_true")
    parser.add_argument("--require-no-repair", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        result = analyze(args.transcript.read_text())
        if args.build_receipt:
            result = bind_build(result, build_receipt(args.build_receipt, result["mode"]))
        if args.session:
            result = attach_execution(result, read_json(args.session), args.fresh_boot_observed)
    except (ValueError, OSError) as error:
        print(f"progress evidence rejected: {error}", file=sys.stderr)
        return 1
    # Preserve a valid negative result before enforcing the acceptance gate.
    encoded = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded)
    print(encoded, end="")
    if args.require_no_repair:
        try:
            require_no_repair(result)
        except ValueError as error:
            print(str(error), file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

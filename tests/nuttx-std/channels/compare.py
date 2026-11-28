#!/usr/bin/env python3
"""Finite, source-bound std comparisons through the ordinary NuttX frontend."""
import argparse
import copy
import difflib
import hashlib
import json
import os
from pathlib import Path
import pty
import re
import select
import shutil
import signal
import subprocess
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
LIBRARY = Path("lib/rustlib/src/rust/library")
MANIFEST = HERE / "fixture/Cargo.toml"
CANDIDATES = ("baseline", "upstream-queue", "pthread-parker-init", "no-outlining")
ARRAY = "std/src/sync/mpmc/array.rs"
PTHREAD = "std/src/sys/sync/thread_parking/pthread.rs"
UPSTREAM = {
    ARRAY: "3b7cce4fa348ff357658c44fa9def9bffe7ed6ff97a6ab4be74da3a0350e804c",
    PTHREAD: "0262635045e33a18a0f529739ae696c32266dc4d1d4dcfc64054ffbae69b5765",
    "std/src/sync/mpmc/utils.rs": "1d88085f0ed3fcbecff6625058d5aa4d32f45ee8015e09d05547a35874ab3802",
}
PATCHES = {
    "std-bounded-channel.patch": "rust-std/0007-nuttx-bounded-channel.patch",
    "std-parker.patch": "rust-std/0002-nuttx-parker.patch",
    "std-parker-selection.patch": "rust-std/0014-current-nuttx-parker-selection.patch",
    "std-unbounded-boundaries.patch": "rust-std/0010-unbounded-operation-boundaries.patch",
    "std-rendezvous-boundaries.patch": "rust-std/0011-rendezvous-operation-boundaries.patch",
    "std-main-thread-once.patch": "rust-std/0013-current-nuttx-main-thread-once.patch",
    "std-fd-sanitization.patch": "rust-std/0015-current-nuttx-fd-sanitization.patch",
    "std-sigign.patch": "rust-std/0016-current-nuttx-sigign.patch",
    "std-libc-override.patch": "rust-std/0017-current-nuttx-libc-override.patch",
    "std-pollfd-init.patch": "rust-std/0001-native-pollfd-initialization.patch",
    "libc-sockaddr-storage.patch": "libc/0003-nuttx-sockaddr-alignment.patch",
    "libc-nuttx-poll.patch": "libc/0005-current-nuttx-pollfd-abi.patch",
    "libc-nuttx-directory.patch": "libc/0006-current-nuttx-directory-inode-abi.patch",
    "libc-nuttx-wchar.patch": "libc/0007-nuttx-xtensa-wchar.patch",
}
REVERSE = {
    "baseline": (),
    "upstream-queue": ("std-bounded-channel.patch",),
    "pthread-parker-init": ("std-parker-selection.patch", "std-parker.patch"),
    "no-outlining": ("std-unbounded-boundaries.patch", "std-rendezvous-boundaries.patch"),
}
FAULT = re.compile(r"\b(panic|panicked|assertion|hardfault|dump_assert|aborted|fatal error)\b", re.I)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def identity(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def blob(path):
    data = path.read_bytes()
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def contained(root, name):
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError("unsafe source path")
    selected = root / relative
    if selected.exists() and not selected.resolve().is_relative_to(root.resolve()):
        raise ValueError("source escaped the private SDK")
    return selected


def replace_once(text, before, after):
    if text.count(before) != 1:
        raise ValueError("upstream experiment anchor drifted")
    return text.replace(before, after, 1)


def instrument_array(text):
    reserved = """                    Ok(_) => {
                        // Prepare the token for the follow-up call to `write`.
"""
    text = replace_once(text, reserved, """                    Ok(_) => {
                        #[cfg(target_os = "nuttx")]
                        unsafe {
                            unsafe extern "C" { fn __nxrs_std_compare_reserved(queue: usize); }
                            __nxrs_std_compare_reserved(self as *const Self as usize);
                        }
                        // Prepare the token for the follow-up call to `write`.
""")
    unpublished = """                backoff.spin_light();
                head = self.head.load(Ordering::Relaxed);
            } else {
"""
    return replace_once(text, unpublished, """                #[cfg(target_os = "nuttx")]
                unsafe {
                    unsafe extern "C" { fn __nxrs_std_compare_unpublished(queue: usize); }
                    __nxrs_std_compare_unpublished(self as *const Self as usize);
                }
                backoff.spin_light();
                head = self.head.load(Ordering::Relaxed);
            } else {
""")


def initialize_pthread_mutex(text):
    before = "        Pin::new_unchecked(&mut (*parker).cvar).init();\n"
    return replace_once(text, before, """        #[cfg(target_os = "nuttx")]
        Pin::new_unchecked(&mut (*parker).lock).init();
""" + before)


def verify_baseline(prepared):
    sdk = prepared / "toolchain"
    ledger = json.loads((prepared / "std-patch.json").read_text())
    if ledger.get("sdk") not in ("esp-1.100.0.0", "nightly-2026-11-26") or "comparison" in ledger:
        raise ValueError("use a current, unmodified production SDK receipt")
    if (sdk / "nxrs-std-comparison.json").exists() or ledger.get("installed_toolchain_unchanged") is not True:
        raise ValueError("invalid production baseline")
    expected = set(PATCHES) | {"std-libc-lock.patch"}
    if ledger["sdk"].startswith("nightly"):
        expected.remove("std-pollfd-init.patch")
    if set(ledger["patchset_sha256"]) != expected:
        raise ValueError("production patch set changed; review the comparison")
    for name, checksum in ledger["patchset_sha256"].items():
        if digest(prepared / name) != checksum:
            raise ValueError("baseline patch digest mismatch")
        if name in PATCHES:
            component, filename = PATCHES[name].split("/")
            if digest(ROOT / "upstream" / component / "patches" / filename) != checksum:
                raise ValueError("baseline differs from the reviewed repository patch")
    for entry in ledger["files"]:
        if blob(contained(sdk, entry["source_path"])) != entry["patched_blob"]:
            raise ValueError("baseline source differs from its production receipt")
    binding = contained(sdk, ledger["libc"]["path"] + "/src/unix/nuttx/mod.rs")
    if ledger["libc"]["libc_version"] != "0.2.191" or blob(binding) != ledger["libc"]["patched_blob"]:
        raise ValueError("baseline libc ABI source changed")
    for name, checksum in UPSTREAM.items():
        if digest(contained(sdk, str(LIBRARY / name))) != checksum:
            raise ValueError("pinned upstream algorithm source changed")
    return ledger


def prepare(prepared, out, candidate):
    if candidate not in CANDIDATES:
        raise ValueError("unknown comparison candidate")
    prepared = prepared.resolve(strict=True)
    baseline = verify_baseline(prepared)
    original = prepared / "toolchain"
    if out.exists() or out.is_symlink() or original.is_relative_to(out):
        raise ValueError("comparison output must be a new private directory")
    out.mkdir(parents=True)
    sdk = out / "toolchain"
    library = (original / LIBRARY).resolve(strict=True)
    if not library.is_relative_to(original.resolve()):
        raise ValueError("baseline Rust source is not private")

    def copy_file(source, destination):
        # Cargo may rewrite library/Cargo.lock. Every source byte is independent;
        # compiler binaries are only read and can share immutable disk storage.
        if Path(source).resolve().is_relative_to(library):
            return shutil.copy2(source, destination)
        try:
            return os.link(source, destination)
        except OSError:
            return shutil.copy2(source, destination)

    shutil.copytree(original, sdk, symlinks=True, copy_function=copy_file)
    for name in baseline["patchset_sha256"]:
        shutil.copy2(prepared / name, out / name)
    shutil.copy2(prepared / "std-patch.json", out / "baseline-std-patch.json")
    paths = {entry["source_path"] for entry in baseline["files"]}
    paths.update(str(LIBRARY / name) for name in UPSTREAM)
    paths.update(str(LIBRARY / name) for name in ("Cargo.toml", "Cargo.lock", "libc-nuttx/src/unix/nuttx/mod.rs"))
    before = {name: contained(original, name).read_bytes() for name in sorted(paths)}
    env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(out)}
    for name in REVERSE[candidate]:
        command = ["git", "apply", "--reverse", str(out / name)]
        subprocess.run(command[:3] + ["--check"] + command[3:], cwd=sdk, env=env, check=True, capture_output=True)
        subprocess.run(command, cwd=sdk, env=env, check=True, capture_output=True)
    if candidate == "upstream-queue":
        source = sdk / LIBRARY / ARRAY
        source.write_text(instrument_array(source.read_text()))
    elif candidate == "pthread-parker-init":
        source = sdk / LIBRARY / PTHREAD
        source.write_text(initialize_pthread_mutex(source.read_text()))
    records, differences = [], []
    for name, data in before.items():
        selected = contained(sdk, name)
        after = selected.read_bytes() if selected.exists() else None
        records.append({"path": name, "before_sha256": hashlib.sha256(data).hexdigest(),
                        "after_sha256": hashlib.sha256(after).hexdigest() if after is not None else None})
        if after != data:
            differences.extend(difflib.unified_diff(data.decode().splitlines(True),
                (after or b"").decode().splitlines(True), fromfile="a/" + name,
                tofile="b/" + name if after is not None else "/dev/null"))
        for stage, contents in (("before", data), ("after", after)):
            if contents is not None:
                retained = out / "sources" / stage / name
                retained.parent.mkdir(parents=True, exist_ok=True)
                retained.write_bytes(contents)
    patch = out / "std-comparison.patch"
    patch.write_text("".join(differences))
    comparison = {"schema": 1, "candidate": candidate, "production_qualified": False,
                  "fixture_manifest": str(MANIFEST), "fixture_manifest_sha256": digest(MANIFEST),
                  "baseline_receipt_sha256": digest(out / "baseline-std-patch.json"), "sources": records}
    comparison["id"] = identity(comparison)
    ledger = copy.deepcopy(baseline)
    # mode names the retained startup/libc ABI repairs. The separate mandatory
    # comparison marker scopes runtime changes without hiding those ABI fixes.
    ledger.update(comparison=comparison,
                  reversed_production_patches=list(REVERSE[candidate]))
    ledger["bounded_channel_backend"] = "upstream-array-with-reservation-probe" if candidate == "upstream-queue" else baseline["bounded_channel_backend"]
    ledger["parker_backend"] = "upstream-pthread-with-mutex-init" if candidate == "pthread-parker-init" else "nuttx-native"
    ledger["nuttx_operation_outlining"] = candidate != "no-outlining"
    # This is the real application sequence: production patches, then one
    # generated comparison diff (which explicitly reverses selected patches).
    ledger["patchset_sha256"][patch.name] = digest(patch)
    for entry in ledger["files"]:
        source = contained(sdk, entry["source_path"])
        final = blob(source) if source.exists() else None
        if final != entry["patched_blob"]:
            entry["patches"].append(patch.name)
        entry["patched_blob"] = final
    ledger["native_sources"] = [entry for entry in ledger["native_sources"] if contained(sdk, entry["path"]).exists()]
    save(sdk / "nxrs-std-comparison.json", comparison)
    save(out / "std-patch.json", ledger)
    verify_baseline(prepared)
    validate_sdk(out)
    return comparison


def validate_sdk(out):
    ledger = json.loads((out / "std-patch.json").read_text())
    comparison = ledger["comparison"]
    identity_fields = {k: v for k, v in comparison.items() if k != "id"}
    if comparison["candidate"] not in CANDIDATES or identity(identity_fields) != comparison["id"]:
        raise ValueError("comparison identity mismatch")
    if comparison != json.loads((out / "toolchain/nxrs-std-comparison.json").read_text()):
        raise ValueError("comparison marker mismatch")
    if comparison["fixture_manifest"] != str(MANIFEST) or digest(MANIFEST) != comparison["fixture_manifest_sha256"]:
        raise ValueError("comparison fixture manifest changed")
    if comparison["production_qualified"] is not False or digest(out / "baseline-std-patch.json") != comparison["baseline_receipt_sha256"]:
        raise ValueError("comparison baseline/qualification mismatch")
    for record in comparison["sources"]:
        path = contained(out / "toolchain", record["path"])
        if record["after_sha256"] is None:
            if path.exists() or path.is_symlink():
                raise ValueError("removed source reappeared")
        elif digest(path) != record["after_sha256"]:
            raise ValueError("comparison source changed")
        for stage in ("before", "after"):
            checksum = record[stage + "_sha256"]
            if checksum is not None and digest(contained(out / "sources" / stage, record["path"])) != checksum:
                raise ValueError("retained comparison source changed")
    for name, checksum in ledger["patchset_sha256"].items():
        if digest(contained(out, name)) != checksum:
            raise ValueError("comparison patch changed")
    return comparison


def fixture_inputs():
    files = [HERE / "compare.py", HERE / "src/controls.rs", MANIFEST,
             HERE / "fixture/Cargo.lock", HERE / "fixture/build.rs", HERE / "fixture/native.c", HERE / "fixture/src/main.rs"]
    return {str(path.relative_to(ROOT)): digest(path) for path in files}


def build(out, frontend=None):
    comparison = validate_sdk(out)
    firmware = out / "firmware"
    if firmware.exists():
        raise ValueError("build output must be fresh; retain one candidate at a time")
    inputs = {"comparison_id": comparison["id"], "fixture_sources": fixture_inputs(),
              "profile": "mps2-an521-mock", "priority_inheritance": False,
              "frontend_sha256": digest(frontend) if frontend else None,
              "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()}
    build_id = identity(inputs)
    launcher = [str(frontend), "--workspace-root", str(ROOT)] if frontend else ["cargo", "nxrs"]
    command = [*launcher, "build", "--manifest-path", str(MANIFEST),
               "--profile", inputs["profile"], "--out", str(firmware),
               "--target-c-source", str(HERE / "fixture/native.c"),
               "--kconfig-disable", "PRIORITY_INHERITANCE"]
    env = {**os.environ, "NXRS_SHARED_NUTTX_STD_SYSROOT": str(out / "toolchain"),
           "STD_COMPARISON_ID": build_id, "STD_COMPARISON_CANDIDATE": comparison["candidate"],
           "CARGO_INCREMENTAL": "0", "CARGO_BUILD_JOBS": "1", "CARGO_PROFILE_DEV_DEBUG": "0", "CARGO_PROFILE_TEST_DEBUG": "0"}
    save(out / "build-inputs.json", {"build_id": build_id, "inputs": inputs, "command": command})
    for name, checksum in inputs["fixture_sources"].items():
        source = ROOT / name
        retained = out / "fixture-sources" / name
        retained.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, retained)
        if digest(retained) != checksum:
            raise ValueError("fixture changed while retaining inputs")
    with (out / "build.log").open("w") as log:
        subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=1200)
    validate_sdk(out)
    if fixture_inputs() != inputs["fixture_sources"] or (frontend and digest(frontend) != inputs["frontend_sha256"]):
        raise ValueError("fixture changed during build")
    image = firmware / "nuttx/nuttx"
    if build_id.encode() not in image.read_bytes():
        raise ValueError("comparison build identity is not in the firmware")
    abi = json.loads((firmware / "abi-report.json").read_text())
    if not abi["probe_abi_compatible"] or abi["errors"]:
        raise ValueError("normal target ABI gate failed")
    artifacts = {}
    for path in sorted(firmware.glob("*.json")) + [firmware / "resolved.config", image]:
        artifacts[str(path.relative_to(out))] = {"sha256": digest(path), "bytes": path.stat().st_size}
    save(out / "build.json", {"schema": 1, "build_id": build_id, "candidate": comparison["candidate"],
         "inputs": inputs, "artifacts": artifacts, "image": str(image.relative_to(out)),
         "production_qualified": False, "physical_timing_qualified": False})


def validate_result(text, build_id, candidate, case):
    if FAULT.search(text) or "STD_COMPARISON FAIL" in text:
        raise ValueError("target reported a failure")
    rows = [json.loads(line.split("STD_COMPARISON ", 1)[1]) for line in text.splitlines()
            if line.startswith("STD_COMPARISON {")]
    if len(rows) != 1:
        raise ValueError("missing or duplicate target result")
    row = rows[0]
    expected = {"schema": 1, "candidate": candidate, "id": build_id, "case": case, "passed": True}
    if any(row.get(key) != value or type(row.get(key)) is not type(value) for key, value in expected.items()):
        raise ValueError("wrong target identity or result")
    if case == "functional":
        if row.get("controls") != 9 or len(re.findall(r"^PASS ", text, re.M)) != 9:
            raise ValueError("incomplete functional controls")
    else:
        expected = {"hazard_observed": True, "upstream_fixed_priority_safe": False, "joined": 2,
                    "producer_priority": 120, "receiver_priority": 180, "coordinator_priority": 220, "rescue_priority": 200}
        if candidate != "upstream-queue" or any(row.get(k) != v or type(row.get(k)) is not type(v) for k, v in expected.items()):
            raise ValueError("incomplete finite reservation witness")
        if not (isinstance(row.get("spins_before"), int) and isinstance(row.get("spins_after"), int)
                and 1 < row["spins_before"] < row["spins_after"]):
            raise ValueError("no repeated unpublished-slot observation")
    return row


def run_qemu(out, qemu):
    # A failed validation or rerun cannot leave an earlier success receipt.
    (out / "qualification.json").unlink(missing_ok=True)
    comparison = validate_sdk(out)
    record = json.loads((out / "build.json").read_text())
    if identity(record["inputs"]) != record["build_id"] or record["inputs"]["comparison_id"] != comparison["id"]:
        raise ValueError("firmware build identity mismatch")
    for name, checksum in record["inputs"]["fixture_sources"].items():
        if digest(contained(out / "fixture-sources", name)) != checksum:
            raise ValueError("retained fixture source changed")
    for name, expected in record["artifacts"].items():
        artifact = contained(out, name)
        if digest(artifact) != expected["sha256"] or artifact.stat().st_size != expected["bytes"]:
            raise ValueError("firmware evidence changed")
    cases = ["functional"] + (["reservation"] if comparison["candidate"] == "upstream-queue" else [])
    results = []
    for case in cases:
        master, slave = pty.openpty()
        transcript = ""
        process = subprocess.Popen([str(qemu), "-M", "mps2-an521", "-display", "none", "-serial", os.ttyname(slave),
            "-monitor", "none", "-nic", "none", "-no-reboot", "-qmp", "stdio", "-kernel", str(out / record["image"])],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=slave, bufsize=0, start_new_session=True)
        try:
            pending = ""
            def until_prompt():
                nonlocal transcript, pending
                deadline = time.monotonic() + 90
                while "nsh>" not in pending:
                    if time.monotonic() >= deadline or process.poll() is not None:
                        raise RuntimeError("bounded QEMU execution failed to return to NSH")
                    if select.select([master], [], [], 0.1)[0]:
                        data = os.read(master, 65536).decode(errors="replace").replace("\r", "")
                        transcript += data
                        pending += data
                        if len(transcript) > 2 * 1024 * 1024:
                            raise ValueError("QEMU transcript limit exceeded")
                end = pending.index("nsh>") + 4
                result, pending = pending[:end], pending[end:]
                return result

            def command(value):
                time.sleep(0.02)
                for byte in (value + "\n").encode():
                    os.write(master, bytes([byte]))
                    time.sleep(0.01)
                return until_prompt()

            until_prompt()
            result = validate_result(command("std_compare " + case), record["build_id"], comparison["candidate"], case)
            rejected = command("std_compare invalid")
            if "STD_COMPARISON FAIL unknown-case" not in rejected or "\"passed\":true" in rejected:
                raise ValueError("target negative control was not rejected")
            if FAULT.search(transcript):
                raise ValueError("fault outside result window")
            # Require acknowledged clean shutdown, not a killed process with
            # an apparently successful prefix in its transcript.
            def reply():
                if not select.select([process.stdout], [], [], 5)[0]:
                    raise RuntimeError("QMP reply timeout")
                return json.loads(process.stdout.readline(65536))
            if "QMP" not in reply():
                raise ValueError("missing QMP greeting")
            for number, action in enumerate(("qmp_capabilities", "quit")):
                process.stdin.write((json.dumps({"execute": action, "id": number}) + "\n").encode())
                process.stdin.flush()
                while True:
                    response = reply()
                    if response.get("id") == number:
                        if "return" not in response:
                            raise ValueError("QMP rejected clean shutdown")
                        break
            if process.wait(timeout=10) != 0:
                raise ValueError("QEMU exited unsuccessfully")
            results.append(result)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            os.close(master)
            os.close(slave)
            (out / (case + ".log")).write_text(transcript)
    save(out / "qualification.json", {"schema": 1, "candidate": comparison["candidate"], "build_id": record["build_id"],
        "build_sha256": digest(out / "build.json"), "qemu": subprocess.check_output([qemu, "--version"], text=True).splitlines()[0],
        "qemu_sha256": digest(qemu), "results": results,
        "transcripts": {case + ".log": digest(out / (case + ".log")) for case in cases},
        "fresh_boot_per_case": True, "clean_qemu_exit": True, "negative_control_rejected": True,
        "production_qualified": False, "physical_timing_qualified": False})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--prepared-sdk", type=Path, required=True)
    prepare_parser.add_argument("--candidate", choices=CANDIDATES, required=True)
    build_parser = commands.add_parser("build")
    build_parser.add_argument("--frontend", type=Path, help="stable private copy of the normal nxrs-build CLI")
    for child in (prepare_parser, build_parser, commands.add_parser("run")):
        child.add_argument("--out", type=Path, required=True)
        if child.prog.endswith(" run"):
            child.add_argument("--qemu", type=Path, required=True)
    args = parser.parse_args()
    out = args.out.resolve()
    if args.action == "prepare":
        print(json.dumps(prepare(args.prepared_sdk, out, args.candidate), indent=2))
    elif args.action == "build":
        build(out, args.frontend.resolve(strict=True) if args.frontend else None)
    else:
        run_qemu(out, args.qemu.resolve(strict=True))


if __name__ == "__main__":
    main()

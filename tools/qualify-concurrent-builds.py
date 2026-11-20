#!/usr/bin/env python3
"""Run the finite CoreS3 concurrent-profile qualification through nxrs-build.

The frontend is compiled once from the clean, identified checkout before the
measured pair. This runner has no production barrier or arbitrary command mode.
Its internal process helper is separately exercised with synthetic child
processes; those tests are not target-build qualification evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import sys
import tempfile
import time
from typing import Callable


CASES = ("bmi270", "lsm6dso")
JOBS = {"CARGO_BUILD_JOBS": "2", "NXRS_NUTTX_BUILD_JOBS": "2"}
FORBIDDEN_ENVIRONMENT = (
    "NXRS_SHARED_CARGO_TARGET_DIR",
    "NXRS_SHARED_NUTTX_STD_SYSROOT",
    "NXRS_CARGO_CONFIG",
)
CLEAR_ENVIRONMENT = (
    "RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "RUSTC_WRAPPER",
    "RUSTC_WORKSPACE_WRAPPER", "CARGO_BUILD_TARGET", "CARGO_BUILD_RUSTC",
    "CARGO_BUILD_RUSTC_WRAPPER", "CARGO_BUILD_RUSTC_WORKSPACE_WRAPPER",
)


class QualificationError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def output(argv: list[str], root: Path, env: dict[str, str] | None = None) -> str:
    return subprocess.check_output(argv, cwd=root, env=env, text=True,
                                   stderr=subprocess.STDOUT, timeout=30).strip()


def atomic_json(path: Path, value: dict) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def checked_source(root: Path, revision: str, paths: tuple[str, ...]) -> dict:
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise QualificationError("--revision must be a complete lowercase Git SHA")
    if output(["git", "rev-parse", "--show-toplevel"], root) != str(root):
        raise QualificationError("witness must use its own checkout root")
    if output(["git", "rev-parse", "HEAD"], root) != revision:
        raise QualificationError("checkout HEAD differs from --revision")
    if output(["git", "status", "--porcelain", "--untracked-files=all"], root):
        raise QualificationError("qualification requires a clean source checkout")
    return {
        "root": str(root), "revision": revision,
        "tree": output(["git", "rev-parse", "HEAD^{tree}"], root),
        "inputs": {name: sha256(root / name) for name in paths},
    }


def fresh_parent(root: Path, requested: Path) -> Path:
    candidate = requested if requested.is_absolute() else root / requested
    if ".." in candidate.parts:
        raise QualificationError("output parent must not contain parent traversal")
    resolved = candidate.resolve()
    target = root / "target"
    if resolved == target or not resolved.is_relative_to(target):
        raise QualificationError("choose a fresh generated output below this checkout's target/")
    if candidate.absolute() != resolved:
        raise QualificationError("output parent must not traverse symlinks")
    if resolved.exists() or resolved.is_symlink():
        raise QualificationError("output parent already exists; choose a fresh --out")
    ignored = subprocess.run(["git", "check-ignore", "--quiet", "--no-index", str(resolved)],
                             cwd=root, check=False)
    if ignored.returncode != 0:
        raise QualificationError("qualification output must be excluded from source control")
    resolved.mkdir(parents=True)
    return resolved


def environment() -> dict[str, str]:
    for name in FORBIDDEN_ENVIRONMENT:
        if name in os.environ:
            raise QualificationError(f"unset {name}: this witness requires private build outputs")
    result = dict(os.environ)
    for name in CLEAR_ENVIRONMENT:
        result.pop(name, None)
    result.update(JOBS)
    return result


def terminate_groups(processes: list[subprocess.Popen], grace: float = 3.0) -> None:
    """Retire descendants too, including when their process-group leader exited."""
    for child in processes:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + grace
    for child in processes:
        try:
            child.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
    # A descendant may have ignored TERM after its leader exited. Always address
    # the owned group; checking only Popen.poll() would leave that child behind.
    for child in processes:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    for child in processes:
        child.wait(timeout=5)


def check_interruption(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise KeyboardInterrupt("qualification interrupted")


def build_frontend(root: Path, parent: Path, env: dict[str, str], deadline: float,
                   cancelled: Callable[[], bool] | None = None) -> dict:
    native = dict(env)
    rustc = Path(output(["rustup", "which", "--toolchain", "1.90.0", "rustc"], root)).resolve()
    rustdoc = Path(output(["rustup", "which", "--toolchain", "1.90.0", "rustdoc"], root)).resolve()
    native.update(RUSTC=str(rustc), RUSTDOC=str(rustdoc), RUSTUP_TOOLCHAIN="1.90.0")
    compiler_version = output([str(rustc), "--version", "--verbose"], root, native)
    if not compiler_version.startswith("rustc 1.90.0 "):
        raise QualificationError("frontend compiler must be Rust 1.90.0")
    cargo_version = output(["cargo", "+1.90.0", "--version"], root, native)
    if not cargo_version.startswith("cargo 1.90.0 "):
        raise QualificationError("frontend Cargo must be 1.90.0")
    target = parent / "frontend"
    argv = ["cargo", "+1.90.0", "build", "--locked", "--manifest-path", str(root / "Cargo.toml"),
            "-p", "nxrs-build", "--bin", "nxrs-build", "--message-format=json-render-diagnostics",
            "--target-dir", str(target)]
    messages = parent / "frontend-messages.jsonl"
    log = parent / "frontend-build.log"
    with messages.open("wb") as stdout, log.open("wb") as stderr:
        check_interruption(cancelled)
        child = subprocess.Popen(argv, cwd=root, env=native, stdout=stdout, stderr=stderr,
                                 start_new_session=True)
        try:
            while child.poll() is None:
                check_interruption(cancelled)
                if time.monotonic() >= deadline:
                    raise QualificationError("frontend build deadline expired")
                time.sleep(.05)
            status = child.returncode
            if status != 0:
                raise QualificationError(f"frontend build failed with exit {status}; see {log}")
        except BaseException:
            terminate_groups([child])
            raise
    executable = target / "debug/nxrs-build"
    records = [json.loads(line) for line in messages.read_text().splitlines() if line.strip()]
    artifacts = [record for record in records if record.get("reason") == "compiler-artifact"
                 and record.get("target", {}).get("name") == "nxrs-build"
                 and record.get("target", {}).get("kind") == ["bin"]]
    if (len(artifacts) != 1
            or artifacts[0].get("target", {}).get("src_path") != str(root / "tools/build/src/main.rs")
            or artifacts[0].get("executable") != str(executable)
            or artifacts[0].get("fresh") is not False
            or not executable.is_file() or executable.is_symlink()
            or not any(record == {"reason": "build-finished", "success": True} for record in records)):
        raise QualificationError("frontend Cargo evidence does not identify the fresh workspace binary")
    return {
        "path": str(executable), "sha256": sha256(executable), "build_argv": argv,
        "compiler": {"path": str(rustc), "sha256": sha256(rustc), "version": compiler_version},
        "cargo_version": cargo_version,
        "build_log": {"path": str(log), "sha256": sha256(log)},
        "cargo_messages": {"path": str(messages), "sha256": sha256(messages)},
    }


def run_pair(commands: dict[str, list[str]], outputs: dict[str, Path], root: Path,
             parent: Path, env: dict[str, str], deadline: float,
             capture: Callable[[str, Path, Path], dict[str, str] | None],
             report: dict, checkpoint: Callable[[str], None],
             poll_seconds: float = 0.05,
             cancelled: Callable[[], bool] | None = None) -> None:
    """Process orchestration; injected capture is only used by internal unit tests."""
    children: dict[str, subprocess.Popen] = {}
    streams = []
    completed = False
    try:
        for case in CASES:
            check_interruption(cancelled)
            stream = (parent / f"{case}.log").open("wb")
            streams.append(stream)
            child = subprocess.Popen(commands[case], cwd=root, env=env, stdout=stream,
                                     stderr=subprocess.STDOUT, start_new_session=True)
            children[case] = child
            report["cases"][case] = {
                "output": str(outputs[case]), "pid": child.pid, "argv": commands[case],
                "started_ns": time.monotonic_ns(), "exited_ns": None, "returncode": None,
                "prepared_files": {}, "artifacts": {},
            }
        checkpoint("children-started")
        while True:
            check_interruption(cancelled)
            for case, child in children.items():
                status = child.poll()
                record = report["cases"][case]
                if status is not None and record["returncode"] is None:
                    record["returncode"] = status
                    record["exited_ns"] = time.monotonic_ns()
                    checkpoint(f"{case}-exited")
                if status not in (None, 0):
                    raise QualificationError(f"{case} build failed with exit {status}")
            if report["overlap"] is None:
                if any(child.poll() is not None for child in children.values()):
                    raise QualificationError("a build exited before prepared-input overlap was observed")
                started = time.monotonic_ns()
                snapshots = {case: capture(case, outputs[case], root) for case in CASES}
                ended = time.monotonic_ns()
                if (all(value is not None for value in snapshots.values())
                        and all(child.poll() is None for child in children.values())):
                    report["overlap"] = {
                        "start_ns": started, "end_ns": ended,
                        "alive_before": list(CASES), "alive_after": list(CASES),
                    }
                    for case in CASES:
                        report["cases"][case]["prepared_files"] = snapshots[case]
                    checkpoint("prepared-input-overlap")
            if all(record["returncode"] == 0 for record in report["cases"].values()):
                completed = True
                return
            if time.monotonic() >= deadline:
                raise QualificationError("concurrent build deadline expired")
            time.sleep(poll_seconds)
    finally:
        if not completed:
            terminate_groups(list(children.values()))
            for case, child in children.items():
                record = report["cases"][case]
                if record["returncode"] is None:
                    record["returncode"] = child.returncode
                    record["exited_ns"] = time.monotonic_ns()
        for stream in streams:
            stream.close()


def load_checker(root: Path):
    spec = importlib.util.spec_from_file_location("nxrs_concurrent_checker",
                                                root / "tools/check-concurrent-builds.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run(root: Path, requested: Path, revision: str, timeout: float,
        cancelled: Callable[[], bool] | None = None) -> Path:
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise QualificationError("this target qualification requires a Linux x86-64 host")
    if not 1 <= timeout <= 3600:
        raise QualificationError("timeout must be between 1 and 3600 seconds")
    root = root.resolve()
    checker = load_checker(root)
    env = environment()
    source = checked_source(root, revision, checker.SOURCE_INPUTS)
    check_interruption(cancelled)
    parent = fresh_parent(root, requested)
    report = {"schema": 1, "source": source, "frontend": None, "environment": JOBS,
              "clock": "monotonic_ns", "overlap": None, "cases": {}}
    diagnostics = parent / "concurrent-builds.incomplete.json"
    destination = parent / "concurrent-builds.json"
    evidence = parent / "evidence"

    def checkpoint(phase: str) -> None:
        atomic_json(diagnostics, {"status": "incomplete", "phase": phase, "observation": report})
        print(f"CONCURRENT_BUILD phase={phase}", flush=True)

    checkpoint("frontend-build")
    deadline = time.monotonic() + timeout
    try:
        report["frontend"] = build_frontend(root, parent, env, deadline, cancelled)
        # No preparation or selection inputs changed while compiling the controller.
        if checked_source(root, revision, checker.SOURCE_INPUTS) != source:
            raise QualificationError("source identity changed during frontend compilation")
        outputs = {case: parent / case for case in CASES}
        commands = {}
        for case in CASES:
            argv = [report["frontend"]["path"], "--workspace-root", str(root), "build",
                    "--app", "event-demo", "--platform", "cores3"]
            if case == "lsm6dso":
                argv += ["--overlay", "tests/host/fixtures/lsm6dso-overlay.toml"]
            commands[case] = [*argv, "--out", str(outputs[case])]
        run_pair(commands, outputs, root, parent, env, deadline, checker.snapshot_prepared,
                 report, checkpoint, cancelled=cancelled)
        for case in CASES:
            report["cases"][case]["artifacts"] = checker.finish_case(case, outputs[case], root, revision)
            log = parent / f"{case}.log"
            report["cases"][case]["build_log"] = {"path": str(log), "sha256": sha256(log)}
        if checked_source(root, revision, checker.SOURCE_INPUTS) != source:
            raise QualificationError("source identity changed during concurrent builds")
        if sha256(Path(report["frontend"]["path"])) != report["frontend"]["sha256"]:
            raise QualificationError("frontend executable changed during concurrent builds")
        check_interruption(cancelled)
        checker.check_report(report, root)
        check_interruption(cancelled)
        checker.export_bundle(report, root, evidence)
        check_interruption(cancelled)
        atomic_json(destination, report)
        check_interruption(cancelled)
        diagnostics.unlink()
        check_interruption(cancelled)
        print(f"CONCURRENT_BUILD PASS report={destination} evidence={evidence}", flush=True)
        return destination
    except BaseException as error:
        # A late interruption may arrive while exporting or publishing. Retain
        # the artifacts for diagnosis, but never leave either success receipt.
        destination.unlink(missing_ok=True)
        (evidence / "concurrent-builds.json").unlink(missing_ok=True)
        atomic_json(diagnostics, {"status": "failed", "error": str(error) or type(error).__name__,
                                  "observation": report})
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True,
                        help="fresh, ignored output parent below this checkout's target/")
    parser.add_argument("--revision", required=True, help="exact clean source HEAD")
    parser.add_argument("--timeout-seconds", type=float, default=1200)
    args = parser.parse_args()
    previous = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT)}
    cancellation_requested = False

    def interrupted(_signal, _frame):
        # Do not interrupt Popen between OS process creation and registration of
        # its owned process group. Check the request at the next safe boundary.
        nonlocal cancellation_requested
        cancellation_requested = True

    for number in previous:
        signal.signal(number, interrupted)
    try:
        run(Path(__file__).resolve().parents[1], args.out, args.revision, args.timeout_seconds,
            cancelled=lambda: cancellation_requested)
        return 0
    except KeyboardInterrupt:
        print("CONCURRENT_BUILD FAIL interrupted", file=sys.stderr)
        return 130
    except (OSError, ValueError, QualificationError, subprocess.SubprocessError) as error:
        print(f"CONCURRENT_BUILD FAIL {error}", file=sys.stderr)
        return 1
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


if __name__ == "__main__":
    raise SystemExit(main())

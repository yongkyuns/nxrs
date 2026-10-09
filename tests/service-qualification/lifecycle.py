#!/usr/bin/env python3
"""Build and exercise unchanged C/Rust workers repeatedly in Linux processes.

Host functional evidence only: no firmware timing, RAM or allocator-leak claim.
All build outputs and transcripts belong in a fresh private output directory.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from results import _parse_row, parse, validate_memory

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
STARTUP_SCENARIOS = ("normal", "fail-open", "fail-apply", "fail-queue", "fail-start")
SCENARIOS = (*STARTUP_SCENARIOS, "fail-stop", "fail-join", "fail-close")
SUMMARY_FIELDS = ("scenario", "repetitions", "calls", "failures", "recoveries",
                  "fd_delta", "thread_delta", "queues_left")
COMMON = (HERE / "runtime.c", HERE / "hal_host.c",
          HERE.parent / "service-footprint/native_thread.c", HERE / "lifecycle_host.c",
          HERE / "lifecycle_faults.c")


def expected_steps(scenario, repetitions, *, foreign_owner=True):
    if (scenario not in SCENARIOS or isinstance(repetitions, bool) or
            not isinstance(repetitions, int) or not 1 <= repetitions <= 100 or
            not isinstance(foreign_owner, bool)):
        raise ValueError("invalid lifecycle scenario or repetitions")
    if scenario == "normal":
        return [(0, services, 300) for _ in range(repetitions) for services in (3, 20)]
    if scenario == "fail-join" and foreign_owner:
        return [(status, 20, events) for _ in range(repetitions * 3)
                for status, events in ((1, 100), (1, 0), (1, 0), (0, 100))]
    if scenario in ("fail-stop", "fail-join"):
        return [(status, 20, events) for _ in range(repetitions * 3)
                for status, events in ((1, 100), (1, 0), (0, 100))]
    faults = 4 if scenario in ("fail-queue", "fail-start", "fail-close") else 1
    return [(status, 20, 100) for _ in range(repetitions * faults) for status in (1, 0)]


def validate(text, scenario, repetitions, *, foreign_owner=True):
    steps = expected_steps(scenario, repetitions, foreign_owner=foreign_owner)
    if not isinstance(text, str):
        raise ValueError("transcript must be text")
    segments, current, summary = [], [], None
    for line in text.splitlines():
        kind = line.partition(" ")[0]
        if kind == "SQ_LIFECYCLE":
            if summary is not None or current:
                raise ValueError("duplicate or misplaced lifecycle summary")
            parts = line.split()
            if len(parts) != len(SUMMARY_FIELDS) + 1 or parts[0] != "SQ_LIFECYCLE":
                raise ValueError("malformed lifecycle summary")
            summary = {}
            for field, part in zip(SUMMARY_FIELDS, parts[1:]):
                key, separator, value = part.partition("=")
                if key != field or not separator or not value:
                    raise ValueError("malformed lifecycle field")
                if field != "scenario":
                    if not value.isascii() or not value.isdecimal():
                        raise ValueError("lifecycle counts must be unsigned integers")
                    value = int(value)
                summary[field] = value
        elif kind in ("SQ_MEMORY", "SQ_RESULT", "SQ_DONE", "SQ_CLEANUP"):
            if summary is not None:
                raise ValueError("runtime output after lifecycle summary")
            current.append(line)
            if kind == "SQ_DONE":
                segments.append(current)
                current = []
        else:
            raise ValueError("unexpected lifecycle output")
    if summary is None or current or len(segments) != len(steps):
        raise ValueError("incomplete lifecycle evidence")
    failures = sum(status for status, _, _ in steps)
    recoveries = sum(status == 0 for status, _, _ in steps) if scenario != "normal" else 0
    expected = dict(scenario=scenario, repetitions=repetitions, calls=len(steps),
                    failures=failures, recoveries=recoveries,
                    fd_delta=0, thread_delta=0, queues_left=0)
    if summary != expected:
        raise ValueError("lifecycle summary does not match requested work or resources")
    for segment, (status, services, events) in zip(segments, steps):
        if _parse_row("SQ_DONE", segment[-1])["status"] != status:
            raise ValueError("unexpected lifecycle call status")
        kinds = [line.partition(" ")[0] for line in segment]
        if status == 0:
            if kinds != ["SQ_MEMORY", "SQ_RESULT", "SQ_DONE"]:
                raise ValueError("unexpected successful lifecycle records")
            parse("\n".join(segment), services=services, events=events, source="messages")
        elif scenario == "fail-apply":
            # LED apply fails on its first event. On a heavily loaded host the
            # producer may admit every event before observing that failure.
            if kinds not in (["SQ_MEMORY", "SQ_DONE"], ["SQ_MEMORY", "SQ_RESULT", "SQ_DONE"]):
                raise ValueError("unexpected LED-apply failure records")
            validate_memory(_parse_row("SQ_MEMORY", segment[0]), services)
            if len(segment) == 3:
                expected_result = dict(services=services, queues=services * 3, events=events,
                                       received=0, errors=1, mean_cycles=0, max_cycles=0,
                                       misses_1ms=0, source="messages")
                if _parse_row("SQ_RESULT", segment[1]) != expected_result:
                    raise ValueError("unexpected LED-apply failure result")
        elif scenario in ("fail-stop", "fail-join"):
            expected_kinds = ["SQ_MEMORY", "SQ_CLEANUP", "SQ_DONE"] if events else ["SQ_CLEANUP", "SQ_DONE"]
            if kinds != expected_kinds:
                raise ValueError("unexpected retained-shutdown records")
            if events:
                validate_memory(_parse_row("SQ_MEMORY", segment[0]), services)
            if segment[-2] != "SQ_CLEANUP pending_threads=1 retained_queues=60":
                raise ValueError("shutdown did not retain the expected ownership")
        elif scenario == "fail-close":
            if kinds != ["SQ_MEMORY", "SQ_RESULT", "SQ_DONE"]:
                raise ValueError("unexpected queue-close failure records")
            row = _parse_row("SQ_RESULT", segment[1])
            if row["errors"] != 1:
                raise ValueError("queue-close failure was not reported")
            # Business delivery must pass the same checks despite shutdown
            # failure. Normalize only the already-checked error and status.
            checked = [segment[0], segment[1].replace(" errors=1 ", " errors=0 "),
                       segment[2].replace("status=1 ", "status=0 ", 1)]
            parse("\n".join(checked), services=services, events=events, source="messages")
        elif kinds != ["SQ_DONE"]:
            raise ValueError("unexpected startup failure records")
    return dict(summary, delivered_events=sum(events for status, _, events in steps if status == 0))


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build(out, cc, rustc):
    library = out / "librust_worker.a"
    subprocess.run([rustc, "--crate-type=staticlib", "--crate-name=sq_lifecycle_worker",
                    "--edition=2021", "-Copt-level=2", "-Cpanic=abort", "-Adead_code",
                    str(HERE / "src/main.rs"), "-o", str(library)], check=True)
    flags = ["-std=c11", "-D_DEFAULT_SOURCE", "-O2", "-Wall", "-Wextra", "-Werror"]
    links = ["-Wl,--wrap=getpid", "-Wl,--wrap=mq_open", "-Wl,--wrap=mq_send", "-Wl,--wrap=mq_close",
             "-Wl,--wrap=nxrs_sq_record", "-Wl,--wrap=nxrs_cq_thread_start", "-Wl,--wrap=nxrs_cq_thread_join",
             "-pthread", "-lrt", "-ldl", "-lm"]
    for language in ("c", "rust"):
        # SQ_RUST only suppresses worker.c's ordinary CLI; the test owns main.
        worker = ["-DSQ_RUST", str(HERE / "worker.c")] if language == "c" else [str(library)]
        subprocess.run([cc, *flags, *(str(p) for p in COMMON), *worker, *links,
                        "-o", str(out / language)], check=True)


def run_case(program, scenario, repetitions, out):
    environment = {key: value for key, value in os.environ.items()
                   if key not in ("SQ_FAIL_LED_OPEN", "SQ_FAIL_LED_APPLY")}
    child = subprocess.Popen([str(program), scenario, str(repetitions)], env=environment,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    timed_out = False
    try:
        stdout, stderr = child.communicate(timeout=max(10, repetitions * 5))
    except subprocess.TimeoutExpired:
        timed_out = True
        child.kill()
        stdout, stderr = child.communicate()
    (out / f"{program.name}-{scenario}.txt").write_text(stdout + stderr)
    if timed_out or child.returncode != 0:
        # Named queues outlive processes and PIDs are reused. Never infer
        # ownership from a dead child's PID or unlink names after a failure.
        raise RuntimeError(f"{program.name}/{scenario}: " +
                           ("timed out" if timed_out else f"exit {child.returncode}: {stderr.strip()}"))
    if stderr:
        raise ValueError(f"{program.name}/{scenario}: unexpected stderr")
    return dict(language=program.name, **validate(stdout, scenario, repetitions))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--cc", default="cc")
    parser.add_argument("--rustc", default="rustc")
    args = parser.parse_args()
    if sys.platform != "linux" or not Path("/dev/mqueue").is_dir():
        parser.error("Linux with mounted /dev/mqueue is required")
    if not 1 <= args.repetitions <= 100:
        parser.error("repetitions must be between 1 and 100")
    # Preserve rustup/cc invocation names: resolving their symlinks can select
    # a different multicall-tool command instead of the requested compiler.
    tools = {key: Path(shutil.which(value) or value).absolute()
             for key, value in (("cc", args.cc), ("rustc", args.rustc))}
    for path in tools.values():
        if not path.is_file() or not os.access(path, os.X_OK):
            parser.error(f"compiler is not executable: {path}")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    sources = (*COMMON, HERE / "lifecycle_faults.h", HERE / "qualification.h", HERE / "pulse_snapshot.h",
               HERE / "worker.c", HERE / "src/main.rs",
               HERE / "results.py", Path(__file__))
    identities = {str(p.relative_to(ROOT)): sha256(p) for p in sources}
    report = dict(schema=1, host_only=True, firmware_claim=False, repetitions=args.repetitions,
                  source_sha256=identities, compiler_versions={}, runs=[], failure=None)
    try:
        report["compiler_versions"] = {key: subprocess.check_output([str(path), "--version"], text=True).splitlines()[0]
                                       for key, path in tools.items()}
        build(out, str(tools["cc"]), str(tools["rustc"]))
        report["binary_sha256"] = {language: sha256(out / language) for language in ("c", "rust")}
        for language in ("c", "rust"):
            for scenario in SCENARIOS:
                row = run_case(out / language, scenario, args.repetitions, out)
                report["runs"].append(row)
                print("SERVICE_LIFECYCLE_PASS", language, scenario, "calls=", row["calls"])
        if any(sha256(ROOT / path) != expected for path, expected in identities.items()):
            raise RuntimeError("source changed during lifecycle evaluation")
    except Exception as error:
        report["failure"] = str(error)
        raise
    finally:
        (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()

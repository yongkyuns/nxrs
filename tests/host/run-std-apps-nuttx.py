#!/usr/bin/env python3
"""Run one Cargo-selected std app on Cortex-M33 NuttX in QEMU.

A bounded PTY transcript and external deadline are deliberately independent of
in-app success markers and cooperative shutdown. Hardware timing is not inferred.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import sys
import time

from test_nuttx_memory import parse_meminfo, validate_heap_cycles, validate_stack_output
from test_std_apps import validate_std, validate_stress

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtos_harness.nuttx import verify_identity


def run(qemu: Path, image: Path, app: str, log: Path) -> None:
    master, slave = pty.openpty()
    process = None
    transcript: list[str] = []
    pending = ""
    total = 0
    try:
        process = subprocess.Popen(
            [str(qemu.resolve()), "-machine", "mps2-an521", "-kernel", str(image.resolve()),
             "-display", "none", "-serial", "stdio", "-monitor", "none", "-nic", "none", "-no-reboot"],
            stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
        )
        os.close(slave)
        slave = -1

        def until(marker: str, seconds: float = 90) -> str:
            nonlocal pending, total
            end_time = time.monotonic() + seconds
            while marker not in pending:
                if time.monotonic() >= end_time:
                    raise TimeoutError(f"missing {marker!r}, QEMU status={process.poll()}")
                if select.select([master], [], [], 0.1)[0]:
                    data = os.read(master, 65536)
                    if not data:
                        raise RuntimeError("console EOF")
                    total += len(data)
                    if total > 2 * 1024 * 1024:
                        raise RuntimeError("transcript exceeded 2 MiB")
                    text = data.decode("utf-8", errors="replace").replace("\r", "")
                    transcript.append(text)
                    pending += text
                    sys.stdout.write(text)
                    sys.stdout.flush()
                elif process.poll() is not None:
                    raise RuntimeError(f"QEMU exited {process.returncode}")
            end = pending.index(marker) + len(marker)
            answer, pending = pending[:end], pending[end:]
            return answer

        def command(text: str) -> str:
            # Same MPS2 UART pacing as the existing event-demo qualification;
            # this paces test input, never the app's measured event traffic.
            time.sleep(0.02)
            for value in (text + "\n").encode():
                os.write(master, bytes((value,)))
                time.sleep(0.01)
            return until("nsh>")

        until("nsh>")
        verify_identity(command)
        nsh_name = app.replace("-", "_")
        validator = validate_std if app == "std-demo" else validate_stress

        # Target-side lifecycle characterization is deliberately independent of
        # Rust GlobalAlloc instrumentation. /proc/meminfo causes NuttX to reclaim
        # delayed frees before it reports used/free/largest-free values.
        baseline_heap = parse_meminfo(command("cat /proc/meminfo"))
        memory_command = nsh_name
        if app == "ao-stress":
            memory_command += " --stack-report --shutdown-ms 5000"
        warm_output = command(memory_command)
        validator(warm_output)
        if app == "ao-stress":
            validate_stack_output(warm_output)
        warm_heap = parse_meminfo(command("cat /proc/meminfo"))

        checked_heap = []
        worst_stacks: dict[str, int] = {}
        for _ in range(5):
            output = command(memory_command)
            validator(output)
            if app == "ao-stress":
                for row in validate_stack_output(output):
                    worst_stacks[row["name"]] = max(
                        worst_stacks.get(row["name"], 0), row["stack_used"]
                    )
            checked_heap.append(parse_meminfo(command("cat /proc/meminfo")))

        try:
            heap_results = validate_heap_cycles(app, baseline_heap, warm_heap, checked_heap)
        except AssertionError:
            # MPS2 records owner PIDs and ARM EHABI allocation stacks. The
            # diagnostic Rust build emits unwind tables so these traces reach
            # Rust callers. NuttX's "leak" selector prints nodes whose owner
            # PID is no longer alive; keep it failure-only to avoid adding
            # console traffic to passing runs.
            command("echo leak > /proc/memdump")
            command("echo biggest > /proc/memdump")
            raise
        for row in heap_results:
            line = "NUTTX_HEAP_RESULT " + json.dumps(row, separators=(",", ":")) + "\n"
            transcript.append(line)
            sys.stdout.write(line)
        if worst_stacks:
            line = "NUTTX_STACK_WORST " + json.dumps(
                {"app": app, "checked_runs": 5, "worst_used_by_owner": worst_stacks},
                separators=(",", ":"),
                sort_keys=True,
            ) + "\n"
            transcript.append(line)
            sys.stdout.write(line)

        # Also invoke the uninstrumented/default CLI path twice in one boot so
        # the diagnostic barrier cannot hide an ordinary restart regression.
        for _ in range(2):
            output = command(nsh_name)
            validator(output)

        invalid = command(nsh_name + (" --case missing" if app == "std-demo" else " --capacity 0"))
        prefix = "STD_DEMO" if app == "std-demo" else "AO_STRESS"
        assert prefix + " FAIL" in invalid, invalid
        assert prefix + " PASS" not in invalid, invalid
        print(
            f"\nNUTTX_STD_APP PASS app={app} machine=mps2-an521 "
            "heap_checked_cycles=5 normal_restarts=2"
        )
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        if slave != -1:
            os.close(slave)
        os.close(master)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("".join(transcript), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qemu", type=Path, required=True)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--app", choices=("std-demo", "ao-stress"), required=True)
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()
    run(args.qemu, args.image, args.app, args.log)


if __name__ == "__main__":
    main()

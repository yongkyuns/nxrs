#!/usr/bin/env python3
"""Qualify real NuttX MPS2 captures through socket-free QEMU/GDB pipes.

Requires a final std-demo MPS2 ELF, its resolved config, matching NuttX pynuttx,
Python-enabled ARM GDB, host readelf, and a QEMU with the corrected MPS2 UART.
No firmware rebuild or target function invocation occurs in this harness.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import time


def version(executable):
    result = subprocess.run([str(executable), "--version"], capture_output=True, text=True, timeout=10, check=True)
    return result.stdout.splitlines()[0]


def validate_lifecycle(observations):
    """Reject repeated captures or a command that has not actually retired.

    The GDB side has already compared each capture with stopped target bytes.
    This checks the relationship between the three independent observations;
    console success markers alone are insufficient evidence of that lifecycle.
    """
    if len(observations) != 3:
        raise RuntimeError("The workload requires ready, entry, and returned observations")
    task_sets = [{(task["tcb"], task["pid"]) for task in point["tasks"]}
                 for point in observations]
    created = task_sets[1] - task_sets[0]
    if not created:
        raise RuntimeError("No new task was observed at std_demo entry")
    if created & task_sets[2]:
        raise RuntimeError("An entry task remains registered after std_demo returned")

    def topology(point):
        return tuple((heap["address"], region["start"], region["size"],
                      tuple((chunk["start"], chunk["size"], chunk["kind"])
                            for chunk in region["chunks"]))
                     for heap in point["heaps"] for region in heap["regions"])

    layouts = [topology(point) for point in observations]
    if layouts[0] == layouts[1] or layouts[1] == layouts[2]:
        raise RuntimeError("Heap topology did not change across both workload transitions")
    return {"new_entry_tasks": [{"tcb": tcb, "pid": pid} for tcb, pid in sorted(created)],
            "entry_tasks_retired": True, "heap_topology_changed_at_entry": True,
            "heap_topology_changed_after_workload": True}


def drain_console(out_fd, console, log, deadline):
    """Drain already available UART bytes without postponing owned-child cleanup."""
    while select.select([out_fd], [], [], 0)[0]:
        if time.monotonic() >= deadline:
            raise TimeoutError("QEMU/GDB qualification deadline exceeded while draining UART")
        chunk = os.read(out_fd, 65536)
        if not chunk:
            break
        console.extend(chunk)
        if len(console) > 4 * 1024 * 1024 or log.tell() > 4 * 1024 * 1024:
            raise RuntimeError("Qualification transcript exceeded 4 MiB")


def run(args):
    if os.name != "posix":
        raise RuntimeError("This QEMU/GDB pipe qualification harness requires a POSIX host")
    qemu, gdb, image, config, pynuttx, output = [getattr(args, key).expanduser().resolve()
                                               for key in ("qemu", "gdb", "image", "config", "pynuttx", "output")]
    for path in (qemu, gdb, image, config, pynuttx / "gdbinit.py"):
        if not path.is_file():
            raise FileNotFoundError(path)
    config_text = config.read_text()
    required = ("CONFIG_ARCH_BOARD_MPS2_AN521=y", "CONFIG_DEBUG_SYMBOLS=y")
    if not all(line in config_text.splitlines() for line in required) or "CONFIG_SMP=y" in config_text.splitlines():
        raise RuntimeError("Qualification requires the single-core debug-enabled MPS2 profile")
    if "CONFIG_MM_TLSF_MANAGER=y" in config_text.splitlines():
        raise RuntimeError("The qualification adapter supports the default allocator")
    if any(char in str(output) for char in (",", "\n", "\r")):
        raise ValueError("The output path must not contain QEMU option delimiters")
    qemu_version, gdb_version = version(qemu), version(gdb)
    output.mkdir(parents=True, exist_ok=False)
    serial = output / "uart"
    in_path, out_path = Path(str(serial) + ".in"), Path(str(serial) + ".out")
    os.mkfifo(in_path)
    os.mkfifo(out_path)
    in_fd = os.open(in_path, os.O_RDWR | os.O_NONBLOCK)
    out_fd = os.open(out_path, os.O_RDWR | os.O_NONBLOCK)
    command = None if args.boot_only else "std_demo --case channels"
    settings = {
        "schema_version": 1, "image": str(image), "output": str(output), "pynuttx": str(pynuttx),
        "exporter": str(Path(__file__).resolve().parents[1] / "gdb_export.py"),
        "config": str(config), "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
        "elf_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
        "qemu_version": qemu_version, "gdb_version": gdb_version, "command": command,
        "qemu_command": [str(qemu), "-machine", "mps2-an521", "-kernel", str(image),
                         "-display", "none", "-monitor", "none", "-nic", "none", "-no-reboot",
                         "-serial", "pipe:" + str(serial), "-S", "-gdb", "stdio"],
    }
    (output / "run.json").write_text(json.dumps(settings, indent=2) + "\n")
    (output / "resolved.config").write_text(config_text)
    environment = os.environ.copy()
    environment["NXRS_MEMORY_QEMU_RUN"] = str(output / "run.json")
    gdb_command = [str(gdb), "-nx", "-q", "--batch", str(image), "-x", str(Path(__file__).with_name("qemu_capture_gdb.py").resolve())]
    process = None
    console = bytearray()
    sent = False
    try:
        with (output / "gdb.log").open("wb") as log:
            process = subprocess.Popen(gdb_command, stdout=log, stderr=subprocess.STDOUT,
                                       env=environment, start_new_session=True)
            deadline = time.monotonic() + args.timeout
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"QEMU/GDB qualification exceeded {args.timeout}s")
                if select.select([out_fd], [], [], 0.05)[0]:
                    console.extend(os.read(out_fd, 65536))
                if len(console) > 4 * 1024 * 1024 or log.tell() > 4 * 1024 * 1024:
                    raise RuntimeError("Qualification transcript exceeded 4 MiB")
                if (output / "failure.json").is_file():
                    failure = json.loads((output / "failure.json").read_text())
                    raise RuntimeError(f"GDB capture failed: {failure['error']}; inspect {output / 'gdb.log'}")
                if command and not sent and b"nsh>" in console and (output / "send-command").exists():
                    time.sleep(0.02)
                    for byte in (command + "\n").encode():
                        os.write(in_fd, bytes((byte,)))
                        time.sleep(0.01)
                    sent = True
                if process.poll() is not None:
                    drain_console(out_fd, console, log, deadline)
                    break
        if process.returncode != 0:
            raise RuntimeError(f"GDB capture failed with exit {process.returncode}; inspect {output / 'gdb.log'}")
        report = json.loads((output / "gdb-report.json").read_text())
        workload_passed = b"STD_DEMO PASS cases=1" in console and b"STD_DEMO case=channels status=pass" in console
        if command and not workload_passed:
            raise RuntimeError("Expected std-demo channels success markers were not observed")
        if command:
            raw = [json.loads((output / point["raw_file"]).read_text()) for point in report["snapshots"]]
            report["lifecycle"] = validate_lifecycle(raw)
        report.update({"command": command, "workload_passed": workload_passed if command else None,
                       "qemu_version": qemu_version, "gdb_version": gdb_version,
                       "config_sha256": settings["config_sha256"]})
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"report": str(output / "report.json"), "snapshots": len(report["snapshots"]),
                          "workload_passed": report["workload_passed"], "elf_sha256": report["elf_sha256"]}))
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        os.close(in_fd)
        os.close(out_fd)
        in_path.unlink(missing_ok=True)
        out_path.unlink(missing_ok=True)
        (output / "console.log").write_bytes(console)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("qemu", "gdb", "image", "config", "pynuttx", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--boot-only", action="store_true")
    parser.add_argument("--timeout", type=float, default=90)
    run(parser.parse_args())

#!/usr/bin/env python3
"""Diagnose an archived boot assertion without changing qualification results.

The original check remains failed. This bounded experiment uses its exact ELF
and records the first complete assertion, or the full observation window if the
assertion does not reproduce. It sends no workload commands.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import time


ELF_SHA256 = "df780f2e12bed92342f0fdf9693cd46a9cba95c17ebd157bf4f5e48ada38ad78"
CONFIG_SHA256 = "ada256ac803291d57aad9e317045c43c9212e4ed461d7540aa57cbd0170ea486"
QEMU_VERSION = "QEMU emulator version 10.2.1 (Debian 1:10.2.1+ds-1ubuntu3.3)"
LIMIT = 4 * 1024 * 1024


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def boot(command, directory):
    console = bytearray()
    started = time.monotonic()
    deadline = started + 10
    assertion_at = None
    status = "host_timeout"
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, start_new_session=True)
    try:
        with (directory / "console.log").open("wb") as log:
            while time.monotonic() < deadline:
                if assertion_at is not None and time.monotonic() >= assertion_at + 2:
                    break
                if not select.select([process.stdout], [], [], 0.05)[0]:
                    continue
                data = os.read(process.stdout.fileno(), 65536)
                if not data:
                    if assertion_at is None:
                        status = "unexpected_eof"
                    break
                available = LIMIT - len(console)
                console.extend(data[:available])
                log.write(data[:available])
                log.flush()
                if b"Assertion failed" in console and assertion_at is None:
                    assertion_at = time.monotonic()
                    status = "native_assertion"
                if len(data) > available:
                    if assertion_at is None:
                        status = "console_limit"
                    break
                if assertion_at is None and any(
                    console.endswith(suffix)
                    for suffix in (b"nsh> ", b"nsh> \x1b[K")
                ):
                    status = "prompt_observed"
                    break
    finally:
        exited_before_cleanup = process.poll()
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
        process.stdin.close()
        process.stdout.close()
    report = {"status": status, "host_elapsed_seconds": time.monotonic() - started,
              "console_bytes": len(console),
              "console_sha256": hashlib.sha256(console).hexdigest(),
              "assertion_seen": assertion_at is not None,
              "exit_status_before_cleanup": exited_before_cleanup,
              "cleanup_returncode": process.returncode,
              "workload_commands_sent": 0}
    (directory / "boot.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("image", "config", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    if digest(args.image) != ELF_SHA256 or digest(args.config) != CONFIG_SHA256:
        raise RuntimeError("Retained image/configuration identity mismatch")
    qemu = shutil.which("qemu-system-arm")
    if qemu is None:
        raise RuntimeError("QEMU executable is unavailable")
    qemu_version = subprocess.check_output([qemu, "--version"], text=True).splitlines()[0]
    if qemu_version != QEMU_VERSION:
        raise RuntimeError(f"Original emulator version mismatch: {qemu_version!r}")
    command = [qemu, "-machine", "mps2-an521", "-kernel", str(args.image.resolve()),
               "-display", "none", "-serial", "stdio", "-monitor", "none", "-nic", "none", "-no-reboot"]
    report = {"purpose": "Native startup diagnosis; not replacement qualification",
              "original_head": "b423765645d2eb5d92d4a54652ff2d074f2987fd",
              "original_run": 46110830976, "original_artifact": 15250340281,
              "diagnostic_commit": os.environ.get("GITHUB_SHA"),
              "elf_sha256": ELF_SHA256, "config_sha256": CONFIG_SHA256,
              "qemu_version": qemu_version, "qemu_sha256": digest(Path(qemu)),
              "os_release": Path("/etc/os-release").read_text(),
              "command": command, "maximum_boots": 50,
              "host_deadline_seconds_per_boot": 10, "assertion_tail_seconds": 2,
              "boots": []}
    for index in range(1, 51):
        directory = args.output / f"boot-{index:03d}"
        directory.mkdir()
        result = boot(command, directory)
        report["boots"].append(result)
        (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(f"boot {index}: {result['status']} ({result['console_bytes']} bytes)", flush=True)
        if result["status"] != "prompt_observed":
            print((directory / "console.log").read_text(errors="replace"), flush=True)
            raise SystemExit(1)
    print("No assertion in this 50-boot diagnostic window; original failure remains unresolved.")


if __name__ == "__main__":
    main()

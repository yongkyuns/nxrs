#!/usr/bin/env python3
"""Capture one P1 diagnostic per fresh boot; preserve every rejected transcript."""
from __future__ import annotations

import argparse
import errno
import json
import math
import os
from pathlib import Path
import pty
import re
import select
import shutil
import subprocess
import termios
import time

from analyze import (FIFO_MODES, STD_TRANSFER_MODES, analyze, attach_execution,
                     bind_build, build_receipt, decode_json)

MODES = ("direct-reserved", "direct-published", "select-reserved", "select-published",
         "send-reserved", "send-reclaimed", *STD_TRANSFER_MODES, *FIFO_MODES)
PROMPT = b"nsh> "
BOOT_BANNERS = (b"NuttShell", b"*** Booting NuttX ***")
FAULT = re.compile(rb'PANIC|ASSERTION|HardFault|UsageFault|BusFault|"record"\s*:\s*"(?:fatal|failure)"')
MAX_LOG = 2 * 1024 * 1024


def fresh_boot(data):
    return PROMPT in data and any(banner in data for banner in BOOT_BANNERS)


def command_for(mode):
    if mode not in MODES:
        raise ValueError("unknown progress mode")
    return f"channel_progress {mode}"


def read_until(fd, transcript, predicate, deadline, process=None):
    pending = bytearray()
    while not predicate(pending):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("external console deadline; no progress conclusion")
        ready, _, _ = select.select([fd], [], [], min(.1, remaining))
        if ready:
            try:
                data = os.read(fd, 65536)
            except BlockingIOError:
                continue
            except OSError as error:
                if error.errno == errno.EIO:
                    raise RuntimeError("console closed before result and prompt") from error
                raise
            if not data:
                raise RuntimeError("console EOF before result and prompt")
            transcript.extend(data)
            pending.extend(data)
            if len(transcript) > MAX_LOG:
                raise ValueError("console size limit")
            if FAULT.search(pending):
                raise RuntimeError("guest fixture failure or kernel fault")
        elif process is not None and process.poll() is not None:
            raise RuntimeError("emulator exited before result and prompt")
    return bytes(pending)


def write_command(fd, command, deadline):
    for byte in (command + "\n").encode():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("console transmit deadline")
        _, writable, _ = select.select([], [fd], [], min(.1, remaining))
        if not writable or os.write(fd, bytes([byte])) != 1:
            raise RuntimeError("console command transmission failed")
        time.sleep(.005)


def serial_open(port):
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        saved = termios.tcgetattr(fd)
        attrs = termios.tcgetattr(fd)
        attrs[0] = attrs[1] = attrs[3] = 0
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[4] = attrs[5] = termios.B115200
        attrs[6][termios.VMIN] = attrs[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        termios.tcflush(fd, termios.TCIFLUSH)
        return fd, saved
    except BaseException:
        os.close(fd)
        raise


def retain_build(receipt, directory, output):
    # A captured run stays independently re-analyzable after build caches are
    # pruned. The validator already rejects escaping paths and verifies hashes.
    for relative in receipt["artifacts_sha256"]:
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(directory / relative, target)
    (output / "build.json").write_text(json.dumps(receipt, indent=2) + "\n")


def capture(args):
    receipt = build_receipt(args.build_directory / "build.json", args.mode)
    if args.output.exists():
        raise ValueError("capture output must be new; preserve previous evidence")
    args.output.mkdir(parents=True)
    transcript = bytearray()
    fd = slave = process = saved = None
    session = None
    session_bytes = None
    report = {
        "schema": "channel-progress-capture-v1", "capture_complete": False,
        "runtime_kind": args.runtime, "mode": args.mode, "command": command_for(args.mode),
        "build_id": receipt["build_id"], "image_sha256": receipt["image_sha256"],
        "one_command_per_boot": True, "physical_timing_qualified": False,
        "priority_progress_qualified": False, "failure_stage": "setup", "error": None,
    }
    try:
        retain_build(receipt, args.build_directory, args.output)
        if args.runtime == "qemu":
            if receipt["platform"] != "mps2-an521-mock":
                raise ValueError("QEMU capture requires the MPS2 AN521 image")
            argv = [args.qemu, "-machine", "mps2-an521", "-kernel",
                    str(args.build_directory / receipt["elf_file"]), "-display", "none",
                    "-serial", "stdio", "-monitor", "none", "-nic", "none", "-no-reboot"]
            report["emulator_command"] = argv
            fd, slave = pty.openpty()
            process = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
            os.close(slave)
            slave = None
            session = {"schema": "channel-progress-session-v1", "runtime_kind": "qemu",
                       "build_id": receipt["build_id"], "image_sha256": receipt["image_sha256"],
                       "board": "mps2-an521"}
        else:
            session_bytes = args.session.read_bytes()
            session = decode_json(session_bytes.decode("utf-8", errors="strict"))
            if (session.get("schema") != "channel-progress-session-v1"
                    or session.get("runtime_kind") != "physical"
                    or session.get("build_id") != receipt["build_id"]
                    or session.get("image_sha256") != receipt["image_sha256"]):
                raise ValueError("physical session does not identify the selected firmware")
            fd, saved = serial_open(args.port)
            print("Serial capture ready; reset the board after arranging exclusive access for this session.",
                  flush=True)
        deadline = time.monotonic() + args.timeout
        report["failure_stage"] = "fresh-boot"
        boot = read_until(fd, transcript, fresh_boot, deadline, process)
        report["fresh_boot_observed"] = True
        report["boot_rom_digest_warning_observed"] = b"SHA-256 comparison failed:" in boot
        report["failure_stage"] = "command"
        write_command(fd, report["command"], deadline)
        read_until(fd, transcript, lambda data: PROMPT in data, deadline, process)
        report["failure_stage"] = "analysis"
        summary = analyze(transcript.decode("utf-8", errors="strict"))
        if summary["mode"] != args.mode:
            raise ValueError("runtime case differs from requested capture mode")
        summary = bind_build(summary, receipt)
        summary = attach_execution(summary, session, fresh_boot_observed=True)
        (args.output / "analysis.json").write_text(json.dumps(summary, indent=2) + "\n")
        report.update(capture_complete=True, failure_stage=None,
                      outcome=summary["outcome"],
                      progress_contract_satisfied=summary["progress_contract_satisfied"])
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        if process is not None:
            report["emulator_returncode_before_cleanup"] = process.poll()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        if saved is not None and fd is not None:
            try:
                termios.tcsetattr(fd, termios.TCSANOW, saved)
            except OSError as error:
                report.update(capture_complete=False, serial_restore_error=str(error))
        for descriptor in (fd, slave):
            if descriptor is not None:
                os.close(descriptor)
        (args.output / "console.log").write_bytes(transcript)
        (args.output / "capture.json").write_text(json.dumps(report, indent=2) + "\n")
        if session_bytes is not None:
            (args.output / "session.json").write_bytes(session_bytes)
        elif session is not None:
            (args.output / "session.json").write_text(json.dumps(session, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime", choices=("qemu", "physical"))
    parser.add_argument("--build-directory", type=Path, required=True)
    parser.add_argument("--mode", choices=MODES, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--qemu", default="qemu-system-arm")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--port")
    parser.add_argument("--session", type=Path)
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or not 1 <= args.timeout <= 600:
        parser.error("timeout must be finite and between 1 and 600 seconds")
    if args.runtime == "physical" and (not args.port or not args.session):
        parser.error("physical capture requires --port and --session")
    if args.runtime == "qemu" and (args.port or args.session):
        parser.error("QEMU does not accept a physical session or port")
    try:
        result = capture(args)
    except (OSError, ValueError) as error:
        parser.exit(1, f"progress capture rejected: {error}\n")
    if not result["capture_complete"]:
        parser.exit(1, f'progress capture incomplete: {result.get("error")}\n')
    print("CHANNEL_PROGRESS_CAPTURE_COMPLETE", result["outcome"])


if __name__ == "__main__":
    main()

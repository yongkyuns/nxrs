#!/usr/bin/env python3
"""Fresh-boot ARM NuttX functional captures; no hardware or timing claims."""
import argparse
from contextlib import suppress
import json
import os
from pathlib import Path
import re
import secrets
import select
import shutil
import subprocess
import time

import workflow as W


def require_fixed_mps2_uart(version):
    """Admit release branches containing QEMU's MPS2 RX/TX wiring fix."""
    match = re.match(r"QEMU emulator version (\d+)\.(\d+)\.(\d+)(?:\s|$)", version)
    if match is not None:
        major, minor, micro = map(int, match.groups())
        if ((major, minor) == (8, 2) and micro >= 7
                or (major, minor) == (9, 0) and micro >= 3
                or (major, minor) >= (9, 1)):
            return
    raise ValueError(
        "MPS2 qualification requires QEMU 8.2.7+, 9.0.3+, or 9.1+ "
        "within those release branches; older MPS2 emulators reverse UART RX/TX "
        "interrupts and can stall after the NSH banner "
        "(upstream fix 5a558be93ad628e5bed6e0ee062870f49251725c). "
        "Use tests/owner-qualification/install-qemu.sh or a supported release; "
        f"reported version: {version.splitlines()[0] if version else '<empty>'}")


def quit_qemu(process):
    # A private pair of inherited pipes works without a listening socket.
    pending = bytearray()

    def reply(deadline):
        while b"\n" not in pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
                raise ValueError("QEMU control reply deadline")
            chunk = os.read(process.stdout.fileno(), 4096)
            if not chunk or len(pending) + len(chunk) > 65536:
                raise ValueError("incomplete or oversized QEMU control reply")
            pending.extend(chunk)
        line, _, rest = pending.partition(b"\n")
        pending[:] = rest
        return json.loads(line)

    if "QMP" not in reply(time.monotonic() + 5):
        raise ValueError("missing QMP greeting")
    for index, command in enumerate(("qmp_capabilities", "quit")):
        process.stdin.write((json.dumps({"execute": command, "id": index}) + "\n").encode())
        process.stdin.flush()
        deadline = time.monotonic() + 5
        while True:
            if time.monotonic() >= deadline:
                raise ValueError("QEMU control command deadline")
            message = reply(deadline)
            if message.get("id") == index:
                if "return" not in message:
                    raise ValueError("QEMU refused clean shutdown")
                break


def capture_case(executable, version, root, build, case):
    import pty

    run = root / "runs" / case
    run.mkdir(parents=True)
    token = secrets.token_hex(12)
    image = W.contained(root, build["executable"])
    data = bytearray()
    receipt = {"schema": "owner-qualification-capture-v1", "case": case, "token": token,
               "build_directory": "../..", "build_sha256": W.digest(root / "build.json"),
               "runtime": "qemu", "machine": "mps2-an521", "qemu_version": version,
               "qemu_sha256": W.digest(executable), "exit_code": None,
               "capture_complete": False, "physical_execution_verified": False}
    try:
        master, slave = pty.openpty()
        command = [str(executable), "-machine", "mps2-an521", "-kernel", str(image),
                   "-display", "none", "-serial", os.ttyname(slave), "-monitor", "none",
                   "-nic", "none", "-no-reboot", "-qmp", "stdio"]
        receipt["command"] = command
        process = None
        pending = ""

        def read_chunk(seconds):
            nonlocal pending
            if select.select([master], [], [], seconds)[0]:
                chunk = os.read(master, 65536)
                if not chunk:
                    raise ValueError("QEMU console ended before capture completed")
                data.extend(chunk)
                if len(data) > 1_048_576:
                    raise ValueError("QEMU console output limit")
                text = data.decode("utf-8", errors="strict")
                if W.FAULT.search(text):
                    raise ValueError("native/fixture failure in QEMU console")
                pending += chunk.decode("utf-8", errors="strict").replace("\r", "")
            elif process.poll() is not None:
                raise ValueError(f"QEMU exited before capture completed: {process.returncode}")

        def until(marker, seconds=30):
            nonlocal pending
            deadline = time.monotonic() + seconds
            while marker not in pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError(f"QEMU console deadline waiting for {marker!r}")
                read_chunk(min(0.2, remaining))
            pending = pending.split(marker, 1)[1]

        def send(text):
            # MPS2's emulated UART can lose a burst even after NSH prompts.
            # Pacing is transport setup, outside the firmware experiment.
            time.sleep(0.02)
            for byte in (text + "\n").encode():
                if os.write(master, bytes((byte,))) != 1:
                    raise ValueError("partial QEMU console command")
                time.sleep(0.01)

        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=slave)
            os.close(slave)
            slave = None
            until("nsh>")
            if not W.fresh_shell_boot(data.decode()):
                raise ValueError("QEMU did not produce a fresh NuttX shell boot")
            send(f"owner_qualification {case} {token}")
            until(f"OWNER_QUALIFICATION_END {build['build_id']} {case} {token}")
            until("nsh>")
            send(f"echo OQ_STATUS_{token}:$?")
            until(f"OQ_STATUS_{token}:0")
            until("nsh>")
            quiet_until = time.monotonic() + 1
            while time.monotonic() < quiet_until:
                read_chunk(max(0, min(0.1, quiet_until - time.monotonic())))
            analysis = W.parse(data.decode(), build, case, token, physical=True)
            quit_qemu(process)
            receipt["exit_code"] = process.wait(timeout=10)
            if receipt["exit_code"] != 0:
                raise ValueError("QEMU did not exit cleanly")
            W.save(run / "analysis.json", analysis)
            receipt["capture_complete"] = True
        finally:
            try:
                if process is not None and process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
            finally:
                if slave is not None:
                    with suppress(OSError):
                        os.close(slave)
                if process is not None:
                    for stream in (process.stdin, process.stdout):
                        with suppress(OSError):
                            stream.close()
                with suppress(OSError):
                    os.close(master)
    except Exception as error:
        receipt["error"] = str(error)
        raise
    finally:
        (run / "console.log").write_bytes(data)
        receipt["console_sha256"] = W.digest(run / "console.log")
        W.save(run / "capture.json", receipt)
    W.replay(run, root)
    print(f"Passed {case} on fresh-boot MPS2 QEMU; {run}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--qemu", default="qemu-system-arm")
    args = parser.parse_args()
    source = args.build.resolve()
    build = W.validate_build(source)
    if build["inputs"]["kind"] != "nuttx-target" or build["inputs"]["profile"] != "mps2-an521-mock":
        raise ValueError("QEMU runner requires the MPS2 ARM NuttX profile")
    executable = shutil.which(args.qemu)
    if executable is None:
        raise ValueError("qemu-system-arm is unavailable")
    executable = Path(executable).resolve()
    version = subprocess.check_output([str(executable), "--version"], text=True, timeout=10).strip()
    require_fixed_mps2_uart(version)
    root = args.out.resolve()
    root.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(source / "build.json", root / "build.json")
    shutil.copytree(source / "retained", root / "retained")
    for case in W.selected_cases(build["inputs"]):
        capture_case(executable, version, root, build, case)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as error:
        raise SystemExit(f"owner QEMU qualification refused: {error}") from error

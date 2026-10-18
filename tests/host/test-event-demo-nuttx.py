#!/usr/bin/env python3
"""Boot an event-demo NuttX image in QEMU and verify the real app transcript."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtos_harness.nuttx import verify_identity


PASS = re.compile(
    r"^EVENT_DEMO PASS observed=(\d+) phases=\(true, true\) "
    r"imu=\{produced:(\d+),dropped:(\d+),errors:(\d+)\} "
    r"gnss=\{produced:(\d+),dropped:(\d+),errors:(\d+)\} "
    r"fusion=\{inputs:(\d+),outputs:(\d+),dropped:(\d+)\}$"
)


def validate(text: str) -> None:
    lines = text.replace("\r", "").splitlines()
    assert not any("panic" in line.lower() for line in lines), text
    assert lines.count("EVENT_DEMO topology:") == 1, text
    assert sum(line.startswith("CONTROL phase=1 ") for line in lines) == 1, text
    assert sum(line.startswith("CONTROL phase=2 ") for line in lines) == 1, text
    assert sum(line.startswith("NAV ") for line in lines) >= 2, text

    phase1 = next(line for line in lines if line.startswith("CONTROL phase=1 "))
    phase2 = next(line for line in lines if line.startswith("CONTROL phase=2 "))
    assert "imu_period_ms=10" in phase1 and "gnss_sampling=false" in phase1, phase1
    assert "gnss_period_ms=100" in phase2 and "gnss_sampling=true" in phase2, phase2

    matches = [PASS.match(line) for line in lines]
    matches = [match for match in matches if match]
    assert len(matches) == 1, text
    values = [int(value) for value in matches[0].groups()]
    observed, imu, imu_drop, imu_error, gnss, gnss_drop, gnss_error, inputs, outputs, output_drop = values
    assert observed >= 2
    assert imu > 0 and gnss > 0 and inputs >= imu + gnss - imu_drop - gnss_drop
    assert outputs >= observed
    assert (imu_drop, imu_error, gnss_drop, gnss_error, output_drop) == (0, 0, 0, 0, 0)


def qmp_quit(path: Path) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect(str(path))
        with connection.makefile("rwb", buffering=0) as stream:
            assert "QMP" in json.loads(stream.readline(65536))
            for index, command in enumerate(("qmp_capabilities", "quit")):
                stream.write((json.dumps({"execute": command, "id": index}) + "\n").encode())
                while True:
                    reply = json.loads(stream.readline(65536))
                    if reply.get("id") == index:
                        assert "return" in reply, reply
                        break


def run(qemu: Path, image: Path, log_path: Path, machine: str) -> None:
    with tempfile.TemporaryDirectory(prefix="nxrs-event-demo-qmp-") as directory:
        monitor = Path(directory) / "control.sock"
        debug_port = None
        if machine == "mps2-an521":
            with socket.socket() as reserved:
                reserved.bind(("127.0.0.1", 0))
                debug_port = reserved.getsockname()[1]
        common = [
            str(qemu.resolve()),
            "-display", "none",
            "-serial", "stdio",
            "-monitor", "none",
            "-nic", "none",
            "-no-reboot",
            "-qmp", f"unix:{monitor},server=on,wait=off",
        ]
        if machine == "esp32s3":
            command = common + [
                "-machine", "esp32s3",
                "-snapshot",
                "-drive", f"file={image.resolve()},if=mtd,format=raw",
            ]
        elif machine == "mps2-an521":
            command = common + [
                "-machine", "mps2-an521",
                "-kernel", str(image.resolve()),
                "-gdb", f"tcp:127.0.0.1:{debug_port}",
            ]
        else:
            raise ValueError(f"unsupported QEMU machine: {machine}")
        master, slave = pty.openpty()
        try:
            process = subprocess.Popen(
                command,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                cwd=image.parent,
                start_new_session=True,
            )
        finally:
            os.close(slave)

        pending = ""
        transcript: list[str] = []
        total = 0

        def until(marker: str, seconds: float = 60) -> str:
            nonlocal pending, total
            deadline = time.monotonic() + seconds
            while marker not in pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"NuttX did not produce {marker!r}; exit={process.poll()}")
                if select.select([master], [], [], min(remaining, 0.2))[0]:
                    data = os.read(master, 65536)
                    if not data:
                        raise RuntimeError("NuttX console EOF")
                    total += len(data)
                    if total > 2 * 1024 * 1024:
                        raise RuntimeError("console exceeded bounded transcript size")
                    text = data.decode("utf-8", errors="replace").replace("\r", "")
                    transcript.append(text)
                    sys.stdout.write(text)
                    sys.stdout.flush()
                    pending += text
                elif process.poll() is not None:
                    raise RuntimeError(f"NuttX exited early: {process.returncode}")
            end = pending.index(marker) + len(marker)
            result, pending = pending[:end], pending[end:]
            return result

        def send_nsh(command: str) -> None:
            data = (command + "\n").encode()
            if machine == "mps2-an521":
                # QEMU's MPS2 CMSDK UART RX path can lose host input even after
                # NSH has printed a prompt. Give the emulated UART a short
                # settling interval, then pace each byte conservatively. This
                # affects only the QEMU test transport, never firmware timing.
                time.sleep(0.02)
                for byte in data:
                    os.write(master, bytes((byte,)))
                    time.sleep(0.01)
            else:
                os.write(master, data)

        try:
            until("nsh>")
            def shell_command(value):
                send_nsh(value)
                return until("nsh>")
            verify_identity(shell_command)

            send_nsh("event_demo --duration-ms 900")
            output = until("nsh>", seconds=30)
            validate(output)
            print(f"\nPASS: event-demo ran on {machine} NuttX with service commands")

            send_nsh("event_demo --duration-ms 0")
            failure = until("nsh>", seconds=10)
            assert "duration must be one positive integer" in failure, failure
            assert not any(PASS.match(line) for line in failure.replace("\r", "").splitlines()), failure
            print("PASS: invalid runtime configuration is rejected by the app")

            qmp_quit(monitor)
            assert process.wait(timeout=10) == 0
        except Exception:
            if machine == "mps2-an521" and debug_port is not None:
                debug_path = log_path.with_name(log_path.stem + "-debug.log")
                try:
                    debugger = subprocess.run(
                        [
                            "gdb-multiarch", "--batch", "-q", str(image.resolve()),
                            "-ex", f"target remote 127.0.0.1:{debug_port}",
                            "-x", str(Path(__file__).with_name("debug-nuttx-tasks.gdb")),
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        timeout=20,
                        check=False,
                    )
                    debug_path.parent.mkdir(parents=True, exist_ok=True)
                    debug_path.write_bytes(debugger.stdout)
                    sys.stdout.write(debugger.stdout.decode("utf-8", errors="replace"))
                    sys.stdout.flush()
                except (OSError, subprocess.SubprocessError) as diagnostic_error:
                    print(f"ARM failed-state snapshot unavailable: {diagnostic_error}", file=sys.stderr)
            raise
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            os.close(master)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("".join(transcript), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("qemu", type=Path)
    parser.add_argument("--machine", choices=("esp32s3", "mps2-an521"), default="esp32s3")
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()
    run(args.qemu, args.image, args.log, args.machine)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

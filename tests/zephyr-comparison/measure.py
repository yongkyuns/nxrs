#!/usr/bin/env python3
"""Explicit local flash/run measurement; save and restore firmware separately."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service-footprint"))
from measure_device import open_serial
from evidence import marker_rows


def read_prompt(fd, timeout, prompt=b"zephyr> "):
    deadline = time.monotonic() + timeout
    data = bytearray()
    while time.monotonic() < deadline:
        readable, _, _ = select.select([fd], [], [], 0.1)
        if readable:
            try:
                data.extend(os.read(fd, 4096))
            except BlockingIOError:
                continue
            if prompt in data:
                return bytes(data)
    raise TimeoutError(f"{prompt!r} prompt missing: {data[-1200:]!r}")


def validate(output, command, mode):
    text = output.decode(errors="replace")
    # The prompt has no newline and the driver does not echo input.
    text = text.replace("zephyr> ", "").replace("\r", "")
    exit_row = marker_rows(text, "ZEPHYR_COMMAND_EXIT", 1)[0]
    if exit_row != {"status": 0} or "FAIL" in text:
        raise ValueError("Zephyr command failed")
    memory = marker_rows(text, "ZEPHYR_MEMORY", 1)[0]
    if not 0 <= memory["kernel_heap_used"] <= memory["kernel_heap_peak"] <= memory["kernel_heap_reserved"]:
        raise ValueError("kernel heap accounting mismatch")
    if command == "baseline":
        if text.splitlines().count("ZEPHYR_BASELINE_PASS") != 1:
            raise ValueError("baseline marker missing")
        return {"baseline": True, "memory": memory}
    resources = marker_rows(text, "ZEPHYR_RESOURCES", 1)[0]
    expected_buffers = 45 * 248 + 15 * 4 * (16 if mode == "wire" else 28)
    if (resources["heap_allocated"] != 0 or resources["stack_storage"] != 83968 or
            resources["queue_buffers"] != expected_buffers):
        raise ValueError("resource contract mismatch")
    if command == "threads":
        row = marker_rows(text, "CQ_ZEPHYR_THREADS_PASS", 1)[0]
        if row["threads"] != 20:
            raise ValueError("thread count mismatch")
        return {"threads": row, "resources": resources, "memory": memory}
    transport = marker_rows(text, "CQ_TRANSPORT_PASS", 1)[0]
    expected = {"language": "zephyr", "mode": "large", "event_bytes": 248,
                "ack_bytes": 16 if mode == "wire" else 28}
    if any(transport.get(k) != v for k, v in expected.items()) or not 0 < transport["cycles"] < 3840000000:
        raise ValueError("clock/transport contract mismatch")
    scale = marker_rows(text, "CQ_ZEPHYR_SCALE_PASS", 1)[0]
    expected = {"mode": "large", "queues": 60, "logical_streams": 60,
                "threads": 20, "messages": 5760, "digest": 441445568}
    if any(scale.get(k) != v for k, v in expected.items()):
        raise ValueError("workload contract mismatch")
    return {"transport": transport, "scale": scale, "resources": resources, "memory": memory}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, type=Path)
    parser.add_argument("--port", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--mode", required=True, choices=("wire", "packet", "baseline"))
    parser.add_argument("--command", choices=("large", "threads", "baseline"), default="large")
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--flasher", default="esptool.py")
    args = parser.parse_args()
    if args.runs < 1 or args.out.exists() or not args.image.is_file():
        parser.error("positive runs, a real image and a fresh output directory are required")
    if (args.mode == "baseline") != (args.command == "baseline"):
        parser.error("baseline mode and command must be selected together")
    args.out.mkdir(parents=True)
    record = {"schema": 1, "image_sha256": hashlib.sha256(args.image.read_bytes()).hexdigest(),
              "mode": args.mode, "command": args.command, "requested_runs": args.runs,
              "completed_runs": 0, "runs": [], "failure": None}
    transcript = bytearray()
    fd = None
    try:
        flash = subprocess.run([args.flasher, "--chip", "esp32s3", "--port", args.port,
            "--baud", "460800", "write_flash", "--flash_size", "detect", "0x0", str(args.image)],
            capture_output=True, text=True, timeout=180)
        (args.out / "flash.log").write_text(flash.stdout + flash.stderr)
        flash.check_returncode()
        fd = open_serial(args.port)
        try:
            transcript.extend(read_prompt(fd, 10))
        except TimeoutError:
            os.write(fd, b"baseline\r" if args.mode == "baseline" else b"threads\r")
            transcript.extend(read_prompt(fd, 20))
        for _ in range(args.runs):
            os.write(fd, args.command.encode() + b"\r")
            output = read_prompt(fd, 30)
            transcript.extend(output)
            row = validate(output, args.command, args.mode)
            record["runs"].append(row)
            record["completed_runs"] += 1
    except Exception as exc:
        record["failure"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if fd is not None:
            os.close(fd)
        (args.out / "serial.log").write_bytes(transcript)
        (args.out / "measurement.json").write_text(json.dumps(record, indent=2) + "\n")
    print(f"ZEPHYR_MEASUREMENT_PASS runs={args.runs} out={args.out}")


if __name__ == "__main__":
    main()

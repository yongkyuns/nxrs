#!/usr/bin/env python3
"""Replay exported numerical calls with pinned Xtensa binaries in isolated QEMU.
No radio, physical device access, network interface, or full sensing FSM."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import select
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]


def calls(path):
    with path.open() as stream:
        header = json.loads(stream.readline(65537))
        if header.get("csi_lab_kernels") != 1:
            raise ValueError("kernel export version")
        for index, line in enumerate(iter(lambda: stream.readline(65537), "")):
            if index >= 50000 or len(line) > 65536:
                raise ValueError("kernel replay bound")
            c = json.loads(line)
            kind = c["kind"]
            cols, a, b = c["cols"], c["rows0"], c["rows1"]
            if (
                kind not in ("motion", "wander")
                or type(cols) is not int
                or not 1 <= cols <= (64 if kind == "motion" else 256)
            ):
                raise ValueError("native shape")
            if any(type(x) is not int or not 0 <= x <= 4096 for x in (a, b)):
                raise ValueError("native rows")
            sizes = [cols * a, cols * b] if kind == "motion" else [cols, cols]
            if sum(sizes) > 4096 or sum(sizes) == 0 or (kind == "wander" and (a or b)):
                raise ValueError("native input bound")
            for key, n in zip(
                ("input0_bits", "input1_bits", "output_bits"),
                sizes + [(cols if kind == "motion" else 1) if c["status"] == 0 else 0],
            ):
                if len(c[key]) != n or any(
                    type(w) is not int or not 0 <= w <= 0xFFFFFFFF for w in c[key]
                ):
                    raise ValueError("native bit-vector schema")
            if (
                type(c["id"]) is not int
                or not 0 <= c["id"] <= 0xFFFFFFFF
                or type(c["status"]) is not int
                or not -(2**31) <= c["status"] < 2**31
            ):
                raise ValueError("native result schema")
            yield c


def verify(line, call):
    m = re.fullmatch(
        r"CSI_LAB_KERNEL_RESULT id=(\d+) status=(-?\d+) words=(\d+) bits=([0-9a-f]*)",
        line,
    )
    if (
        not m
        or int(m[1]) != call["id"]
        or int(m[2]) != call["status"]
        or int(m[3]) != len(call["output_bits"])
    ):
        raise ValueError("native result identity/status/length mismatch")
    if m[4] != "".join(f"{w:08x}" for w in call["output_bits"]):
        raise ValueError("native result differs from recorded bits")


def send_call(call, send, read_until):
    send(f"{'M' if call['kind']=='motion' else 'W'} {call['id']} {call['cols']} {call['rows0']} {call['rows1']}")
    values = call["input0_bits"] + call["input1_bits"]
    for start in range(0, len(values), 4):
        chunk = values[start : start + 4]
        send(" ".join(f"{w:08x}" for w in chunk))
        ack = read_until(b"CSI_LAB_KERNEL_WORDS ", 30) + read_until(b"\n", 30)
        lines = [line for line in ack.splitlines() if line.startswith("CSI_LAB_KERNEL_WORDS ")]
        if lines != [f"CSI_LAB_KERNEL_WORDS id={call['id']} words={start + len(chunk)}"]:
            raise ValueError("native input acknowledgement mismatch")


def run(build, qemu, export, transcript):
    receipt = json.loads((build / "csi-build.json").read_text())
    pins = json.loads((ROOT / "tests/csi-radio/pins.json").read_text())
    image = build / "nuttx/nuttx.merged.bin"
    digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    if (
        receipt["mode"] != "kernel"
        or receipt["pins"] != pins
        or receipt["image_sha256"] != digest(image)
    ):
        raise ValueError("pinned kernel image receipt mismatch")
    config = build / "nuttx/.config"
    if (
        not config.is_file()
        or receipt.get("config_sha256") != digest(config)
        or "CONFIG_ESP32S3_RX_FIFO_THRD=1" not in config.read_text().splitlines()
    ):
        raise ValueError("rebuild kernel with byte-triggered UART receive")
    for name in ("kernel.c", "lab-replay.c", "build.py"):
        key = "tests/csi-radio/" + name
        if receipt["source_sha256"].get(key) != digest(ROOT / key):
            raise ValueError("rebuild kernel with current replay witness")
    # Validate every call before starting QEMU; the export stays private.
    version = subprocess.check_output([str(qemu), "--version"], text=True).splitlines()[
        0
    ]
    if "esp_develop_9.0.0_20240606" not in version:
        raise ValueError("requires pinned Espressif QEMU 9.0.0_20240606")
    count = sum(1 for _ in calls(export))
    if not count:
        raise ValueError("recording has no numerical calls")
    command = [
        str(qemu),
        "-machine",
        "esp32s3",
        "-smp",
        "2",
        "-display",
        "none",
        "-serial",
        "stdio",
        "-monitor",
        "none",
        "-nic",
        "none",
        "-no-reboot",
        "-snapshot",
        "-drive",
        f"file={image},if=mtd,format=raw",
    ]
    process = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    buffer = bytearray()
    trace = bytearray()

    def read_until(marker, seconds=45):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if marker in buffer:
                end = buffer.index(marker) + len(marker)
                data = bytes(buffer[:end])
                del buffer[:end]
                return data.decode(errors="replace").replace("\r", "")
            if select.select([process.stdout], [], [], 0.1)[0]:
                data = os.read(process.stdout.fileno(), 65536)
                if not data:
                    raise RuntimeError("QEMU EOF")
                buffer.extend(data)
                trace.extend(data)
                if len(buffer) > 1024 * 1024 or len(trace) > 128 * 1024 * 1024:
                    raise ValueError("QEMU transcript bound")
        raise TimeoutError("QEMU replay deadline")

    def send(value):
        process.stdin.write((value + "\n").encode())
        process.stdin.flush()

    try:
        read_until(b"nsh> ")
        send("csikernel replay")
        read_until(b"CSI_LAB_KERNEL_READY v=2")
        read_until(b"\n")
        for c in calls(export):
            send_call(c, send, read_until)
            result = read_until(b"CSI_LAB_KERNEL_RESULT ", 30) + read_until(b"\n", 30)
            line = next(
                (
                    line
                    for line in result.splitlines()
                    if line.startswith("CSI_LAB_KERNEL_RESULT ")
                ),
                "",
            )
            verify(line, c)
        send("END")
        end = read_until(b"nsh> ")
        if f"CSI_LAB_KERNEL_END calls={count}" not in end:
            raise ValueError("incomplete native replay")
        send("echo CSI_LAB_STATUS_$?")
        status = read_until(b"nsh> ")
        if not re.search(r"^CSI_LAB_STATUS_0$", status, re.M):
            raise ValueError("native replay exit status")
        return {
            "passed": True,
            "emulator": version,
            "native_calls_bit_exact": count,
            "full_sensing_fsm_replayed": False,
            "physical_radio_qualified": False,
            "image_sha256": receipt["image_sha256"],
            "export_sha256": digest(export),
        }
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        fd = os.open(transcript, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(trace)
        if digest(image) != receipt["image_sha256"]:
            raise ValueError("input flash changed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("build", "qemu", "export", "transcript"):
        parser.add_argument("--" + arg, type=Path, required=True)
    a = parser.parse_args()
    print(
        json.dumps(
            run(a.build.resolve(), a.qemu.resolve(), a.export, a.transcript),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()

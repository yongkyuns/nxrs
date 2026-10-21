#!/usr/bin/env python3
"""Qualify an already flashed physical camera and save one private raw/JPEG image."""
import argparse
import os
from pathlib import Path
import re
import time

FRAME_BYTES = 320 * 240 * 2
PASS = b"NXRS_CAMERA_PASS cycles=3 frames=9 stopped=1"


def parse_heap(transcript):
    rows = re.findall(rb"^\s*\d+\s+(\d+)\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+(Kmem|Umem)\s*$",
                      transcript, re.M)
    used = {name.decode("ascii"): int(value) for value, name in rows}
    if set(used) != {"Kmem", "Umem"} or len(rows) != 2:
        raise ValueError("expected complete kernel/user heap snapshots")
    return used


def validate_frames(transcript, jpeg=False):
    if PASS not in transcript or re.search(rb"NXRS_CAMERA_FAIL|PANIC|Assertion failed", transcript):
        raise ValueError("camera test did not pass")
    frames = re.findall(rb"NXRS_CAMERA_FRAME cycle=(\d+) sequence=(\d+) bytes=(\d+) checksum=([0-9a-f]{8})", transcript)
    if [(int(c), int(s)) for c, s, _, _ in frames] != [(c, s) for c in range(3) for s in range(3)]:
        raise ValueError("missing, duplicated or reordered frames")
    if any((not 4 <= int(size) <= FRAME_BYTES) if jpeg else int(size) != FRAME_BYTES
           for _, _, size, _ in frames):
        raise ValueError("unexpected frame size")
    return frames


def frame_bytes(transcript, jpeg=False):
    """Require complete lifecycle evidence and an exact, checksum-matched dump."""
    frames = validate_frames(transcript, jpeg)
    blocks = re.findall(rb"NXRS_CAMERA_HEX_BEGIN bytes=(\d+)\r?\n(.*?)NXRS_CAMERA_HEX_END", transcript, re.S)
    if len(blocks) != 1 or int(blocks[0][0]) != int(frames[2][2]):
        raise ValueError("expected exactly one complete frame dump")
    body = re.sub(rb"[\r\n]", b"", blocks[0][1])
    if len(body) != int(frames[2][2]) * 2 or re.fullmatch(rb"[0-9a-f]+", body) is None:
        raise ValueError("invalid frame hex")
    raw = bytes.fromhex(body.decode("ascii"))
    checksum = 2166136261
    for byte in raw:
        checksum = ((checksum ^ byte) * 16777619) & 0xffffffff
    if checksum != int(frames[2][3], 16):
        raise ValueError("dump does not match the captured frame")
    if jpeg and (not raw.startswith(b'\xff\xd8') or not raw.endswith(b'\xff\xd9')):
        raise ValueError("invalid JPEG framing; full codec validation is still required")
    return raw


def decode(transcript):
    raw = frame_bytes(transcript)
    rgb = bytearray()
    for index in range(0, len(raw), 2):
        word = raw[index] | (raw[index + 1] << 8)
        red, green, blue = word >> 11, (word >> 5) & 63, word & 31
        rgb.extend(((red << 3) | (red >> 2), (green << 2) | (green >> 4),
                    (blue << 3) | (blue >> 2)))
    return b"P6\n320 240\n255\n" + rgb


def qualify(port, repeats=1, jpeg=False):
    import serial  # Same environment as esptool; no image library required.

    console = serial.Serial(port=None, baudrate=115200, timeout=0.1, write_timeout=2)
    console.dtr = console.rts = False
    console.port = port
    console.open()
    with console:
        def until(marker, timeout, limit):
            data = bytearray()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                data.extend(console.read(4096))
                if len(data) > limit:
                    raise RuntimeError("camera console exceeded its bounded transcript")
                if marker in data:
                    return bytes(data)
                if re.search(rb"NXRS_CAMERA_FAIL|PANIC|Assertion failed", data):
                    raise RuntimeError(data[-1200:].decode("utf-8", errors="replace"))
            raise RuntimeError("camera console deadline expired: " + data[-1200:].decode("utf-8", errors="replace"))

        console.write(b"\n\n\n")
        until(b"nsh> ", 10, 16384)
        first = None
        baseline = None
        for index in range(repeats):
            console.write(b"camera_test" + (b" --jpeg" if jpeg else b"")
                          + (b" --dump" if index == 0 else b"") + b"\n")
            transcript = until(PASS, 45, FRAME_BYTES * 2 + 32768)
            console.write(b"echo NXRS_CAMERA_STATUS_$?\n")
            status = until(b"nsh> ", 5, 4096)
            # A command-completion prompt can arrive before the status command.
            if b"NXRS_CAMERA_STATUS_0" not in status:
                status += until(b"NXRS_CAMERA_STATUS_0", 5, 4096)
            if re.search(rb"(?:^|[\r\n])NXRS_CAMERA_STATUS_0(?:[\r\n]|$)", status) is None:
                raise RuntimeError("camera command did not return status 0")
            validate_frames(transcript, jpeg)
            if first is None:
                first = transcript + status
            if repeats > 1 and index in (0, repeats - 1):
                # Compare after warm-up, with one open console and no reboot.
                # Let the native delayed-free worker finish task teardown.
                time.sleep(0.1)
                console.write(b"free\necho NXRS_CAMERA_HEAP_END\n")
                snapshot = parse_heap(until(b"\r\nNXRS_CAMERA_HEAP_END\r\n", 5, 8192))
                if baseline is None:
                    baseline = snapshot
                else:
                    delta = {name: snapshot[name] - baseline[name] for name in baseline}
                    if any(value > 0 for value in delta.values()):
                        raise RuntimeError(f"heap use grew after warm-up: {delta}")
                    print(f"RAM after warm-up: used-byte deltas {delta}")
        return first


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--output", type=Path, required=True, help="new private .ppm path, or .jpg with --jpeg")
    parser.add_argument("--repeat", type=int, default=1, help="1–100 invocations in one boot")
    parser.add_argument("--jpeg", action="store_true", help="capture JPEG; inspect with a real decoder")
    args = parser.parse_args()
    suffix = ".jpg" if args.jpeg else ".ppm"
    if args.output.suffix != suffix or args.output.exists():
        parser.error(f"--output must be a new {suffix} path; keep home images outside Git")
    if not 1 <= args.repeat <= 100:
        parser.error("--repeat must be between 1 and 100")
    transcript = qualify(args.port, args.repeat, args.jpeg)
    image = frame_bytes(transcript, True) if args.jpeg else decode(transcript)
    with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as output:
        output.write(image)
    print(f"PASS: {args.repeat * 9} frames, {args.repeat * 3} start/stop cycles in one boot; inspect {args.output}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Generate explicitly synthetic virtual-time cases; never physical evidence."""
import argparse
import os
from pathlib import Path
import struct

ROOT = Path(__file__).resolve().parents[2]
CASES = (
    "motion-cycle",
    "source-failure",
    "silence",
    "holdout-motion",
    "vendor-unavailable",
    "duplicate-stop",
)


def generate(case, case_id=1):
    if case not in CASES or type(case_id) is not int or not 1 <= case_id <= 16:
        raise ValueError("named synthetic probe case and bounded identity required")
    out = bytearray(b"CSIMP001")

    def record(tag, words):
        out.extend(struct.pack("<II", tag, len(words)))
        out.extend(struct.pack(f"<{len(words)}I", *words))

    record(1, [case_id])
    record(3, [0, 0, 1])
    frames = {
        "silence": 4400,
        "holdout-motion": 2200,
        "vendor-unavailable": 40,
        "duplicate-stop": 40,
    }.get(case, 5600)
    for n in range(1, frames + 1):
        now = n * 25_000
        if n == 4 or (case == "duplicate-stop" and n == 8):
            record(4, [now, 0, 1])
        if now == 100_000_000:
            record(3, [now, 0, 2])
        if now == 130_000_000:
            record(3, [now, 0, 1])
        fault = case == "source-failure" and n >= 4100
        record(2, [now, 0, int(fault)])
        walking = (
            50_000_000 <= now < 82_000_000
            if case == "holdout-motion"
            else 100_000_000 <= now < 130_000_000
        )
        values = (
            [1.0, 4.0, 2.0, 3.0]
            if (walking and n % 2 == 0) or (not walking and now >= 50_000_000)
            else [1.0, 2.0, 3.0, 4.0]
        )
        bits = [struct.unpack("<I", struct.pack("<f", value))[0] for value in values]
        ready = int(not (case == "vendor-unavailable" and n >= 8))
        record(5, [now, 0, n, n, now - 5000, 0, now, 0, ready, 1, 4, int(fault), *bits])
    now = frames * 25_000
    if case == "silence":
        now += 1_000_001
        record(2, [now, 0, 0])
    record(7, [now + 1, 0])
    return bytes(out)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--case", choices=CASES, required=True)
    p.add_argument("--case-id", type=int, default=1)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    if a.output.resolve().is_relative_to(ROOT):
        raise ValueError("keep probe inputs outside checkout")
    fd = os.open(a.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(generate(a.case, a.case_id))


if __name__ == "__main__":
    main()

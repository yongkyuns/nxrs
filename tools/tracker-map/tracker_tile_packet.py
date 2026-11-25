"""Lossless row packing for the tracker tile reply."""

from itertools import groupby
import struct

MAGIC = b"NXM5"
REQUEST = struct.Struct(">4sBII")
SIDE = 170
PALETTE_BYTES = 32
ROW_BYTES = SIDE // 2
BODY_BYTES = PALETTE_BYTES + SIDE * ROW_BYTES


def _row(packed):
    colors = (index for byte in packed for index in (byte >> 4, byte & 15))
    runs = bytearray()
    for color, group in groupby(colors):
        remaining = sum(1 for _ in group)
        while remaining:
            count = min(remaining, 16)
            runs.append(((count - 1) << 4) | color)
            remaining -= count
    return runs if len(runs) < ROW_BYTES else packed


def pack(pixels):
    if len(pixels) != BODY_BYTES:
        raise ValueError("unexpected packed tile size")
    rows = [_row(pixels[offset:offset + ROW_BYTES])
            for offset in range(PALETTE_BYTES, BODY_BYTES, ROW_BYTES)]
    return pixels[:PALETTE_BYTES] + bytes(map(len, rows)) + b"".join(rows)

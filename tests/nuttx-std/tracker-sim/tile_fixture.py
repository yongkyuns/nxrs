"""Byte layout for the tracker tile transport witnesses."""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tools" / "tracker-map"))
from tracker_tile_packet import BODY_BYTES, MAGIC, PALETTE_BYTES, REQUEST, SIDE, pack


def solid(color):
    return struct.pack(">H", color) + bytes(BODY_BYTES - 2)


def color_at(payload, x, y):
    byte = payload[PALETTE_BYTES + (y * SIDE + x) // 2]
    index = (byte >> (4 if x % 2 == 0 else 0)) & 15
    return struct.unpack_from(">H", payload, index * 2)[0]

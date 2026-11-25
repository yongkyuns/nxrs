"""Byte layout for the tracker tile transport witnesses."""
import math
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


def tile_center(zoom, latitude, longitude):
    world = 1 << zoom
    x = int((longitude + 180) * world / 360)
    y = int((1 - math.asinh(math.tan(math.radians(latitude))) / math.pi) * world / 2)
    return {
        "latitude_deg": math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 0.5) / world)))),
        "longitude_deg": (x + 0.5) * 360 / world - 180,
    }

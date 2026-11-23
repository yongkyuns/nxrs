"""Byte layout for the tracker tile transport witnesses."""
import struct

MAGIC = b"NXM4"
REQUEST = struct.Struct(">4sBII")
SIDE = 170
PALETTE_BYTES = 32
BODY_BYTES = PALETTE_BYTES + SIDE * SIDE // 2


def solid(color):
    return struct.pack(">H", color) + bytes(BODY_BYTES - 2)


def color_at(payload, x, y):
    byte = payload[PALETTE_BYTES + (y * SIDE + x) // 2]
    index = (byte >> (4 if x % 2 == 0 else 0)) & 15
    return struct.unpack_from(">H", payload, index * 2)[0]

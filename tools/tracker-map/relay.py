"""Serve one visible OpenStreetMap tile per pinned-TLS tracker connection."""

import argparse
from io import BytesIO
from pathlib import Path
import socket
import ssl
import struct

from PIL import Image, ImageFilter
from requests import RequestException
from requests_cache import CachedSession

REQUEST = struct.Struct(">4sBII")
SIDE = 170
MAX_PNG = 256 * 1024
USER_AGENT = "NxrsTrackerTileRelay/1 (+https://github.com/yongkyuns/nxrs)"

# Shared starting palette for the standard map style. The wire carries the
# actual RGB565 palette so an independent peer can choose other colors.
MAP_COLORS = (
    0xFAF8F6, 0xF2E9E3, 0xD9D0C9, 0xC4B6AA,
    0xA89A90, 0x8F8B87, 0x626262, 0x333333,
    0xAAD3DF, 0x80ABBF, 0xCDEBB1, 0xAAD09B,
    0xFCD6A4, 0xECA77B, 0xE4B7BA, 0xBFC3D5,
)
SHARPEN = ImageFilter.Kernel((3, 3), (0, -1, 0, -1, 12, -1, 0, -1, 0), scale=8)


def _rgb565(color):
    return (color >> 8 & 0xF800) | (color >> 5 & 0x07E0) | (color >> 3 & 0x001F)


def raster(png):
    with Image.open(BytesIO(png), formats=["PNG"]) as tile:
        if tile.size != (256, 256):
            raise ValueError("unexpected tile dimensions")
        source = tile.convert("RGBA")
        white = Image.new("RGBA", source.size, "white")
        white.alpha_composite(source)
        # Restore contrast to thin labels softened by the resize before quantizing.
        small = white.convert("RGB").resize((SIDE, SIDE), Image.Resampling.LANCZOS).filter(SHARPEN)
        palette = Image.new("P", (1, 1))
        palette.putpalette(bytes(value >> shift & 255 for value in MAP_COLORS for shift in (16, 8, 0)))
        indexes = iter(small.quantize(palette=palette, dither=Image.Dither.NONE).tobytes())
        return struct.pack(">16H", *map(_rgb565, MAP_COLORS)) + bytes(
            left << 4 | right for left, right in zip(indexes, indexes)
        )


def reply(request, fetch):
    magic, zoom, x, y = REQUEST.unpack(request)
    if magic != b"NXM4" or not (15 <= zoom <= 18 and x < 1 << zoom and y < 1 << zoom):
        raise ValueError("invalid tile request")
    try:
        pixels = fetch(zoom, x, y)
    except (OSError, ValueError, RequestException, Image.DecompressionBombError):
        return request + b"\x01"
    return request + b"\x00" + pixels


def fetcher(session):
    def fetch(zoom, x, y):
        url = f"https://tile.openstreetmap.org/{zoom}/{x}/{y}.png"
        with session.get(url, timeout=(2, 4), allow_redirects=False) as response:
            if (response.status_code != 200
                    or response.headers.get("Content-Type", "").split(";")[0] != "image/png"
                    or len(response.content) > MAX_PNG):
                raise ValueError("unexpected OpenStreetMap response")
            return raster(response.content)

    return fetch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="127.0.0.1", help="private interface IPv4 address")
    parser.add_argument("--port", type=int, default=8444)
    parser.add_argument("--cert", type=Path, required=True, help="P-256 PEM certificate")
    parser.add_argument("--key", type=Path, required=True, help="PEM private key")
    parser.add_argument("--cache", type=Path, default=Path("tracker-tiles.sqlite"))
    args = parser.parse_args()

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(args.cert, args.key)
    with (CachedSession(args.cache, cache_control=True, expire_after=7 * 86400, serializer="json") as session,
          socket.create_server((args.bind, args.port), backlog=4) as listener):
        session.headers["User-Agent"] = USER_AGENT
        fetch = fetcher(session)
        print(f"Tracker tile relay listening on {args.bind}:{args.port}", flush=True)
        while True:
            conn, _ = listener.accept()
            with conn:
                conn.settimeout(3)
                try:
                    with context.wrap_socket(conn, server_side=True) as peer:
                        with peer.makefile("rb") as incoming:
                            request = incoming.read(REQUEST.size)
                        packet = reply(request, fetch)
                        # Each TLS write fits the firmware's input record buffer.
                        for offset in range(0, len(packet), 1024):
                            peer.sendall(packet[offset:offset + 1024])
                except (OSError, ValueError, struct.error):
                    continue


if __name__ == "__main__":
    main()

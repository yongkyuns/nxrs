#!/usr/bin/env python3
"""Stage hash-pinned CYW43439 firmware; no SDK clone or firmware in Git."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
REVISION = "c1075d4bc440422cf2b2fd12c64a1f53f77660ee"  # Pico SDK 2.1.1
BASE_URL = f"https://raw.githubusercontent.com/georgerobotics/cyw43-driver/{REVISION}"
HEADER = "w43439A0_7_95_49_00_combined.h"
HEADER_SHA256 = "120bb63db1c5cf42cd44cf9a057d6f14601038a9413badf5a3b737f6552bc9ea"
BT_HEADER = "cyw43_btfw_43439.h"
BT_HEADER_SHA256 = "11fc817e33a875e4f619c6939f934e916be0dfa083ce4af073867f340779a6c9"
BT_HEADER_LEN = 38130
LICENSE_SHA256 = "67aca4f10d9edf489871f64cd8f0dcd6c5df3e4ce75bd39e1914fc54f99e40b3"
WIFI_LEN = 224190
CLM_LEN = 984
BT_LEN = 6164


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    # Concurrent local/CI builds must never consume a partially written blob.
    descriptor, name = tempfile.mkstemp(dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def fetch_checked(path: Path, url: str, expected: str,
                  expected_length: int | None = None) -> bytes:
    if path.exists():
        with path.open("rb") as stream:
            data = stream.read(2_000_001)
    else:
        with urlopen(url, timeout=30) as response:
            data = response.read(2_000_001)
    if len(data) > 2_000_000:
        raise ValueError("radio input exceeds expected size")
    if expected_length is not None and len(data) != expected_length:
        raise ValueError(f"radio input length mismatch: {path}")
    if digest(data) != expected:
        raise ValueError(f"radio input checksum mismatch: {path}")
    if not path.exists():
        atomic_write(path, data)
    return data


def decode_header(data: bytes) -> bytes:
    text = data.decode("ascii")
    match = re.search(r"static const unsigned char "
                      r"w43439A0_7_95_49_00_combined\[\] CYW43_RESOURCE_ATTRIBUTE = \{"
                      r"(?P<bytes>.*?)\n\};", text, re.DOTALL)
    if match is None:
        raise ValueError("missing CYW43439 byte array")
    values = match["bytes"]
    if not re.fullmatch(r"\s*0x[0-9a-fA-F]{2}(?:\s*,\s*0x[0-9a-fA-F]{2})*\s*,?\s*", values):
        raise ValueError("unexpected CYW43439 array syntax")
    for macro, expected in (("CYW43_WIFI_FW_LEN", WIFI_LEN), ("CYW43_CLM_LEN", CLM_LEN)):
        declared = re.search(rf"^#define {macro} \((\d+)\)", text, re.MULTILINE)
        if declared is None or int(declared[1]) != expected:
            raise ValueError(f"unexpected {macro}")
    blob = bytes(int(value, 16) for value in re.findall(r"0x([0-9a-fA-F]{2})", values))
    clm_offset = (WIFI_LEN + 511) & ~511
    if len(blob) != clm_offset + CLM_LEN or any(blob[WIFI_LEN:clm_offset]):
        raise ValueError("invalid firmware/CLM boundary or padding")
    return blob


def decode_bt_header(data: bytes) -> bytes:
    text = data.decode("ascii")
    match = re.search(r"const unsigned char cyw43_btfw_43439\[\] "
                      r"CYW43_RESOURCE_ATTRIBUTE = \{(?P<bytes>.*?)\n\};",
                      text, re.DOTALL)
    if match is None:
        raise ValueError("missing CYW43439 Bluetooth byte array")
    values = match["bytes"]
    if not re.fullmatch(r"\s*0x[0-9a-fA-F]{2}(?:\s*,\s*0x[0-9a-fA-F]{2})*\s*,?\s*", values):
        raise ValueError("unexpected CYW43439 Bluetooth array syntax")
    blob = bytes(int(value, 16) for value in re.findall(r"0x([0-9a-fA-F]{2})", values))

    if not blob:
        raise ValueError("empty CYW43439 Bluetooth firmware")
    version_length = blob[0]
    count_offset = version_length + 1
    if version_length < 1 or count_offset >= len(blob):
        raise ValueError("truncated CYW43439 Bluetooth version header")
    version = blob[1:count_offset]
    if version[-1] != 0 or b"\0" in version[:-1]:
        raise ValueError("invalid CYW43439 Bluetooth version string")
    try:
        version[:-1].decode("ascii")
    except UnicodeDecodeError as error:
        raise ValueError("invalid CYW43439 Bluetooth version string") from error

    cursor = count_offset + 1  # The following record-count byte is informational.
    eof = False
    while cursor < len(blob):
        if len(blob) - cursor < 4:
            raise ValueError("truncated CYW43439 Bluetooth record header")
        length = blob[cursor]
        address = int.from_bytes(blob[cursor + 1:cursor + 3], "big")
        record_type = blob[cursor + 3]
        cursor += 4

        if record_type not in (0, 1, 2, 4, 5):
            raise ValueError(f"unsupported CYW43439 Bluetooth record type: {record_type}")
        expected_payload = {1: 0, 2: 2, 4: 2, 5: 4}.get(record_type)
        if expected_payload is not None and length != expected_payload:
            raise ValueError("invalid CYW43439 Bluetooth address/EOF record length")
        if record_type == 0 and length == 0:
            raise ValueError("empty CYW43439 Bluetooth data record")
        if cursor + length > len(blob):
            raise ValueError("truncated CYW43439 Bluetooth record payload")
        cursor += length

        if record_type == 1:
            if address != 0:
                raise ValueError("invalid CYW43439 Bluetooth EOF address")
            if any(blob[cursor:]):
                raise ValueError("nonpadding bytes after CYW43439 Bluetooth EOF")
            eof = True
            break

    if not eof:
        raise ValueError("missing CYW43439 Bluetooth EOF record")
    return blob


def prepare(output: Path, record: Path, bluetooth: bool = False) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    header_url = f"{BASE_URL}/firmware/{HEADER}"
    header = fetch_checked(output / HEADER, header_url, HEADER_SHA256)
    fetch_checked(output / "LICENSE.RP", f"{BASE_URL}/LICENSE.RP", LICENSE_SHA256)
    blob = decode_header(header)
    firmware = output / "cyw43439.bin"
    # Regenerate from verified source, including when a binary cache was changed.
    atomic_write(firmware, blob)
    record.parent.mkdir(parents=True, exist_ok=True)
    provenance = {
        "schema": 1, "revision": REVISION, "source": header_url,
        "header_sha256": HEADER_SHA256, "binary": str(firmware),
        "binary_sha256": digest(blob), "wifi_length": WIFI_LEN,
        "clm_offset": (WIFI_LEN + 511) & ~511, "clm_length": CLM_LEN,
        "license": str(output / "LICENSE.RP"), "license_sha256": LICENSE_SHA256,
    }
    if bluetooth:
        bt_url = f"{BASE_URL}/firmware/{BT_HEADER}"
        bt_header = fetch_checked(output / BT_HEADER, bt_url, BT_HEADER_SHA256,
                                  BT_HEADER_LEN)
        bt_blob = decode_bt_header(bt_header)
        if len(bt_blob) != BT_LEN:
            raise ValueError(f"unexpected CYW43439 Bluetooth binary length: {len(bt_blob)}")
        bt_firmware = output / "cyw43439-bt.bin"
        atomic_write(bt_firmware, bt_blob)
        provenance["bluetooth"] = {
            "source": bt_url, "header_sha256": BT_HEADER_SHA256,
            "header_length": BT_HEADER_LEN, "binary": str(bt_firmware),
            "binary_sha256": digest(bt_blob), "binary_length": len(bt_blob),
        }
    atomic_write(record, (json.dumps(provenance, indent=2) + "\n").encode())
    return firmware


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "target/radio-deps/cyw43439")
    parser.add_argument("--record", type=Path)
    parser.add_argument("--bluetooth", action="store_true",
                        help="also stage CYW43439 Bluetooth patchram firmware")
    args = parser.parse_args()
    output = args.out.resolve()
    record = (args.record or output / "firmware.json").resolve()
    try:
        print(prepare(output, record, bluetooth=args.bluetooth))
    except (OSError, ValueError) as error:
        parser.exit(1, f"CYW43439 preparation failed: {error}\n")


if __name__ == "__main__":
    main()

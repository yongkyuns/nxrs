#!/usr/bin/env python3
"""Gap-free transport packages for the comparison's ESP32-S3 flash layouts.

Only format-defined padding is omitted, never zero-filled application data.
The original address-preserving image is reconstructed and hash-checked before
flashing. This changes distribution size, not the occupied flash address span.
"""
import argparse
import functools
import hashlib
import importlib.util
import json
import operator
from pathlib import Path
import re
import struct
import zipfile

HERE = Path(__file__).resolve().parent
MAX_IMAGE_BYTES = 16 * 1024 * 1024
LAYOUTS = ("simple-boot", "idf-merged")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _slice(data, offset, size):
    if offset < 0 or size < 0 or offset + size > len(data):
        raise ValueError("truncated image structure")
    return data[offset:offset + size]


def analyze(data, layout):
    """Validate the known image format and identify explicit zero/FF gaps."""
    if layout not in LAYOUTS or not 24 <= len(data) <= MAX_IMAGE_BYTES:
        raise ValueError("unsupported layout or image size")
    gaps, components = [], []

    def gap(offset, size, fill, kind):
        if size:
            if _slice(data, offset, size) != bytes([fill]) * size:
                raise ValueError("padding contains non-padding data")
            gaps.append(dict(offset=offset, length=size, fill=fill, kind=kind))

    def segment(offset, *, flash_only=False):
        address, size = struct.unpack("<II", _slice(data, offset, 8))
        payload = _slice(data, offset + 8, size)
        if address == 0:
            gap(offset + 8, size, 0, "MMU alignment")
        elif flash_only and not (0x3C000000 <= address < 0x3E000000
                                 or 0x42000000 <= address < 0x44000000):
            raise ValueError("unexpected simple-boot flash segment")
        return offset + 8 + size, payload

    def image(start, name):
        header = _slice(data, start, 24)
        if (header[0] != 0xE9 or not 1 <= header[1] <= 16
                or struct.unpack_from("<H", header, 12)[0] != 9
                or header[23] not in (0, 1)):
            raise ValueError("invalid ESP32-S3 image header")
        offset, checksum = start + 24, 0xEF
        for _ in range(header[1]):
            offset, payload = segment(offset)
            checksum = functools.reduce(operator.xor, payload, checksum)
        checksum_end = (offset + 16) // 16 * 16
        footer = _slice(data, offset, checksum_end - offset)
        if footer[:-1] != bytes(len(footer) - 1) or footer[-1] != checksum:
            raise ValueError("invalid image checksum/footer")
        end = checksum_end
        if header[23]:
            if _slice(data, end, 32) != hashlib.sha256(data[start:end]).digest():
                raise ValueError("invalid image SHA-256")
            end += 32
        components.append(dict(name=name, offset=start, length=end - start))
        return end

    end = image(0, "application" if layout == "simple-boot" else "bootloader")
    if layout == "simple-boot":
        # esptool --ram-only-header places mapped-flash segments after the
        # ROM-visible checksum. They are not included in the header's count.
        while end < len(data):
            end, _ = segment(end, flash_only=True)
        components[0]["length"] = end
    else:
        if end > 0x8000:
            raise ValueError("bootloader overlaps partition table")
        gap(end, 0x8000 - end, 255, "between components")
        position, factory = 0x8000, []
        while position < 0x8C00:
            entry = _slice(data, position, 32)
            if entry[:2] == b"\xeb\xeb":
                if (entry[2:16] != bytes([255]) * 14 or entry[16:] !=
                        hashlib.md5(data[0x8000:position]).digest()):
                    raise ValueError("invalid partition table MD5")
                position += 32
                break
            if entry[:2] != b"\xaa\x50":
                raise ValueError("invalid partition table entry")
            if entry[2:4] == b"\x00\x00":
                factory.append(struct.unpack_from("<II", entry, 4))
            position += 32
        else:
            raise ValueError("partition table MD5 is missing")
        if len(factory) != 1:
            raise ValueError("one factory application partition is required")
        start, capacity = factory[0]
        if start < 0x8C00 or start % 0x10000:
            raise ValueError("unsupported application partition offset")
        components.append(dict(name="partition table", offset=0x8000,
                               length=position - 0x8000))
        gap(position, start - position, 255, "between components")
        end = image(start, "application")
        if end - start > capacity or end != len(data):
            raise ValueError("application exceeds partition or has an unexplained tail")
    gaps.sort(key=lambda row: row["offset"])
    gap_bytes = sum(row["length"] for row in gaps)
    return dict(schema=1, layout=layout, image_sha256=digest(data),
                flash_address_span_bytes=len(data), gap_bytes=gap_bytes,
                stored_image_bytes=len(data) - gap_bytes,
                gaps=gaps, components=components)


def unpack(package):
    """Reconstruct in memory; reject corrupt, oversized or ambiguous packages."""
    with zipfile.ZipFile(package) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or len(names) > 257:
            raise ValueError("duplicate or excessive archive members")
        metadata = archive.getinfo("manifest.json")
        if metadata.file_size > 65536 or metadata.compress_type != zipfile.ZIP_STORED:
            raise ValueError("manifest size or encoding is invalid")
        manifest = json.loads(archive.read("manifest.json"))
        if not isinstance(manifest, dict):
            raise ValueError("manifest must be an object")
        size = manifest.get("flash_address_span_bytes")
        if (manifest.get("schema") != 1 or manifest.get("layout") not in LAYOUTS
                or type(size) is not int or not 24 <= size <= MAX_IMAGE_BYTES
                or not isinstance(manifest.get("image_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", manifest["image_sha256"])):
            raise ValueError("invalid package manifest")
        parts = manifest.get("parts")
        if not isinstance(parts, list) or not 1 <= len(parts) <= 256:
            raise ValueError("invalid package parts")
        data, used = bytearray(), {"manifest.json"}
        for index, part in enumerate(parts):
            if not isinstance(part, dict):
                raise ValueError("package part must be an object")
            length = part.get("length")
            if (type(part.get("offset")) is not int or part["offset"] != len(data)
                    or type(length) is not int
                    or not 0 < length <= size - len(data)):
                raise ValueError("package parts do not cover the image in order")
            if "fill" in part:
                if (set(part) != {"offset", "length", "fill"}
                        or type(part["fill"]) is not int or part["fill"] not in (0, 255)):
                    raise ValueError("invalid gap fill")
                payload = bytes([part["fill"]]) * length
            else:
                name = f"chunks/{index:03d}.bin"
                if set(part) != {"offset", "length", "file", "sha256"} or part.get("file") != name:
                    raise ValueError("invalid chunk name")
                member = archive.getinfo(name)
                if member.file_size != length or member.compress_type != zipfile.ZIP_STORED:
                    raise ValueError("invalid chunk size or encoding")
                payload = archive.read(name)
                if digest(payload) != part.get("sha256"):
                    raise ValueError("chunk SHA-256 mismatch")
                used.add(name)
            data.extend(payload)
        if set(names) != used or len(data) != size or digest(data) != manifest["image_sha256"]:
            raise ValueError("package contents or image SHA-256 mismatch")
    analyze(data, manifest["layout"])
    return bytes(data)


def pack(data, layout, output):
    accounting = analyze(data, layout)
    parts, chunks, offset = [], {}, 0
    for gap in [*accounting["gaps"], dict(offset=len(data), length=0)]:
        if gap["offset"] > offset:
            payload = data[offset:gap["offset"]]
            name = f"chunks/{len(parts):03d}.bin"
            parts.append(dict(offset=offset, length=len(payload), file=name, sha256=digest(payload)))
            chunks[name] = payload
        if gap["length"]:
            parts.append({key: gap[key] for key in ("offset", "length", "fill")})
        offset = gap["offset"] + gap["length"]
    manifest = {key: accounting[key] for key in
                ("schema", "layout", "image_sha256", "flash_address_span_bytes")}
    manifest["parts"] = parts
    # Fixed timestamps and no compression: only removal of explicit address
    # gaps changes the payload size, not different compressibility of C/Rust.
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_STORED) as archive:
        for name, payload in [("manifest.json", json.dumps(manifest, separators=(",", ":")).encode()),
                              *sorted(chunks.items())]:
            member = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            member.external_attr = 0o600 << 16
            archive.writestr(member, payload)
    Path(output).chmod(0o600)
    if unpack(output) != data:
        raise ValueError("package round trip changed the firmware")
    return {**accounting, "package_bytes": Path(output).stat().st_size,
            "package_sha256": digest(Path(output).read_bytes()), "round_trip_verified": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    packing = modes.add_parser("pack")
    packing.add_argument("--artifacts", required=True, type=Path)
    packing.add_argument("--out", required=True, type=Path)
    packing.add_argument("--report", required=True, type=Path)
    packing.add_argument("--case", action="append")
    restoring = modes.add_parser("unpack")
    restoring.add_argument("--package", required=True, type=Path)
    restoring.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if args.out.exists() or (args.mode == "pack" and args.report.exists()):
        parser.error("outputs must be fresh")
    if args.mode == "unpack":
        data = unpack(args.package)
        with args.out.open("xb") as stream:
            stream.write(data)
        args.out.chmod(0o600)
        print("IMAGE_PACKAGE_UNPACK_PASS SHA-256 verified")
        return
    spec = importlib.util.spec_from_file_location("package_matrix", HERE / "run_matrix.py")
    matrix = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(matrix)
    cases = args.case or ["nuttx-c-three", "nuttx-rust-three", "zephyr-c-three", "embassy-three"]
    if len(set(cases)) != len(cases):
        parser.error("cases must be distinct")
    frozen = {case: matrix.frozen_image(matrix.case_directory(args.artifacts, case), case)
              for case in cases}
    args.out.mkdir(parents=True)
    report = dict(schema=1, kind="event-services-image-packages",
                  implementation_sha256=digest(Path(__file__).read_bytes()), cases={})
    for case, artifact in frozen.items():
        provenance = artifact["provenance"]
        layout = "idf-merged" if provenance["platform"] == "embassy" else "simple-boot"
        row = pack(artifact["image"].read_bytes(), layout, args.out / (case + ".zip"))
        row["elf_sha256"] = digest(artifact["elf"].read_bytes())
        row["code_and_initialized_data_bytes"] = provenance["section_accounting"]["loadbearing_flash_bytes"]
        report["cases"][case] = row
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open("x") as stream:
        stream.write(json.dumps(report, separators=(",", ":")) + "\n")
    print(f"IMAGE_PACKAGE_PACK_PASS cases={len(cases)} all round trips verified")


if __name__ == "__main__":
    main()

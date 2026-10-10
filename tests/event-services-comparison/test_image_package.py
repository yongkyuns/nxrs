"""Host-only validation for gap-free ESP image packages."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zipfile
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("image_package", HERE / "image_package.py")
image_package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(image_package)


def image(segments, *, digest=False):
    header = bytearray(24)
    header[0], header[1] = 0xE9, len(segments)
    struct.pack_into("<H", header, 12, 9)
    header[23] = int(digest)
    body, checksum = bytearray(header), 0xEF
    for address, payload in segments:
        body.extend(struct.pack("<II", address, len(payload)))
        body.extend(payload)
        if address:
            for byte in payload:
                checksum ^= byte
    body.extend(bytes((-len(body) - 1) % 16))
    body.append(checksum)
    if digest:
        body.extend(hashlib.sha256(body).digest())
    return bytes(body)


def simple_image():
    # The all-zero mapped segment is real data; only the address-zero segment
    # in the uncounted tail is removable padding.
    return (image([(0x3FC90000, bytes(7)), (0x3FC91000, b"RAM!")])
            + struct.pack("<II", 0, 5) + bytes(5)
            + struct.pack("<II", 0x3C100000, 5) + b"FLASH")


def idf_image(*, boot_payload=b"boot", app_offset=0x10000,
              table_md5=True, app_payload=b"app"):
    boot = image([(0x3FC90000, boot_payload)], digest=True)
    table_entry = (b"\xaa\x50\x00\x00" + struct.pack("<II", app_offset, 0x10000)
                   + bytes(20))
    md5 = hashlib.md5(table_entry).digest()
    if not table_md5:
        md5 = bytes([md5[0] ^ 1]) + md5[1:]
    marker = b"\xeb\xeb" + bytes([255]) * 14 + md5
    table = table_entry + marker
    app = image([(0x42000020, app_payload)], digest=True)
    return (boot + bytes([255]) * (0x8000 - len(boot)) + table
            + bytes([255]) * (app_offset - 0x8000 - len(table)) + app)


def raw_package(data, layout):
    """Make a syntactically valid one-chunk package, even for invalid images."""
    manifest = dict(schema=1, layout=layout, flash_address_span_bytes=len(data),
                    image_sha256=hashlib.sha256(data).hexdigest(),
                    parts=[dict(offset=0, length=len(data), file="chunks/000.bin",
                                sha256=hashlib.sha256(data).hexdigest())])
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("chunks/000.bin", data)
    return stream.getvalue()


def rewrite_package(package, *, manifest_change=None, chunk_change=None,
                    extra=(), duplicate=None):
    source = io.BytesIO(package)
    target = io.BytesIO()
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(
            target, "w", compression=zipfile.ZIP_STORED) as changed:
        members = [(item.filename, original.read(item.filename))
                   for item in original.infolist()]
        for index, (name, payload) in enumerate(members):
            if name == "manifest.json" and manifest_change:
                manifest = json.loads(payload)
                manifest_change(manifest)
                payload = json.dumps(manifest).encode()
            elif name == "chunks/000.bin" and chunk_change:
                payload = chunk_change(payload)
            changed.writestr(name, payload)
        for name in extra:
            changed.writestr(name, b"extra")
        if duplicate:
            changed.writestr(duplicate, b"duplicate")
    return target.getvalue()


class ImagePackageTests(unittest.TestCase):
    def test_simple_analysis_preserves_real_zero_data_and_counts_gaps(self):
        data = simple_image()
        result = image_package.analyze(data, "simple-boot")
        padding = 5
        self.assertEqual(result["gap_bytes"], padding)
        self.assertEqual(result["stored_image_bytes"], len(data) - padding)
        self.assertEqual(result["flash_address_span_bytes"], len(data))
        self.assertEqual([(gap["length"], gap["fill"]) for gap in result["gaps"]],
                         [(padding, 0)])

    def test_idf_analysis_counts_ff_component_gaps_and_round_trips(self):
        data = idf_image()
        result = image_package.analyze(data, "idf-merged")
        self.assertEqual(result["flash_address_span_bytes"], len(data))
        self.assertEqual(result["stored_image_bytes"] + result["gap_bytes"], len(data))
        self.assertEqual([gap["fill"] for gap in result["gaps"]], [255, 255])
        with tempfile.TemporaryDirectory() as temporary:
            first, second = Path(temporary) / "one.zip", Path(temporary) / "two.zip"
            packed = image_package.pack(data, "idf-merged", first)
            image_package.pack(data, "idf-merged", second)
            self.assertTrue(packed["round_trip_verified"])
            self.assertEqual(packed["package_bytes"], first.stat().st_size)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(image_package.unpack(first), data)
            with zipfile.ZipFile(first) as archive:
                self.assertTrue(all(item.compress_type == zipfile.ZIP_STORED
                                    for item in archive.infolist()))
                self.assertEqual(sum(item.file_size for item in archive.infolist()
                                     if item.filename != "manifest.json"),
                                 result["stored_image_bytes"])

    def test_simple_tail_unknown_ram_address_is_rejected(self):
        data = image([(0x3FC90000, b"ok")]) + struct.pack("<II", 0x3FC92000, 2) + b"no"
        with self.assertRaises(ValueError):
            image_package.analyze(data, "simple-boot")

    def test_bad_image_checksum_hash_and_zero_padding_are_rejected(self):
        checksum_bad = bytearray(image([(0x3FC90000, b"data")]))
        checksum_bad[-1] ^= 1
        hashed = bytearray(image([(0x42000020, b"data")], digest=True))
        hashed[-1] ^= 1
        zero_padding_bad = (image([(0x3FC90000, b"ok")])
                            + struct.pack("<II", 0, 2) + b"\0X")
        for data in (bytes(checksum_bad), bytes(hashed), zero_padding_bad):
            with self.subTest(size=len(data)), self.assertRaises(ValueError):
                image_package.unpack(io.BytesIO(raw_package(data, "simple-boot")))

    def test_empty_truncated_and_invalid_headers_are_rejected(self):
        invalid = [b"", b"\xe9" * 23, b"\0" * 24,
                   bytes([0xE9, 0]) + bytes(22),
                   bytes([0xE9, 1]) + bytes(10) + b"\x08\0" + bytes(9)]
        for data in invalid:
            with self.subTest(data=data[:3]), self.assertRaises(ValueError):
                image_package.analyze(data, "simple-boot")

    def test_idf_overlap_bad_table_digest_and_unsupported_offset_rejected(self):
        invalid = [idf_image(boot_payload=bytes(0x7FF0)),
                   idf_image(table_md5=False), idf_image(app_offset=0x9000)]
        for data in invalid:
            with self.subTest(size=len(data)), self.assertRaises(ValueError):
                image_package.analyze(data, "idf-merged")

    def test_pack_requires_fresh_output_and_leaves_existing_bytes_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "existing.zip"
            output.write_bytes(b"keep")
            with self.assertRaises(FileExistsError):
                image_package.pack(simple_image(), "simple-boot", output)
            self.assertEqual(output.read_bytes(), b"keep")

    def test_unpack_rejects_chunk_hash_image_hash_incomplete_duplicate_extra_and_oversize(self):
        good = raw_package(simple_image(), "simple-boot")
        malformed = [
            rewrite_package(good, chunk_change=lambda data: data[:-1] + b"X"),
            rewrite_package(good, manifest_change=lambda m: m.update(image_sha256="0" * 64)),
            rewrite_package(good, manifest_change=lambda m: m["parts"].clear()),
            rewrite_package(good, duplicate="manifest.json"),
            rewrite_package(good, extra=("unexpected.bin",)),
            rewrite_package(good, manifest_change=lambda m: m.update(
                flash_address_span_bytes=image_package.MAX_IMAGE_BYTES + 1)),
        ]
        for package in malformed:
            with self.subTest(length=len(package)), self.assertRaises((ValueError, KeyError, zipfile.BadZipFile)):
                image_package.unpack(io.BytesIO(package))

    def test_malformed_manifest_types_offsets_and_chunk_paths_are_rejected(self):
        good = raw_package(simple_image(), "simple-boot")
        changes = (
            lambda m: m.update(image_sha256=123),
            lambda m: m.update(parts=[None]),
            lambda m: m["parts"][0].update(offset=False),
            lambda m: m["parts"][0].update(file="../image.bin"),
            lambda m: m["parts"][0].update(length=image_package.MAX_IMAGE_BYTES + 1),
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                image_package.unpack(io.BytesIO(rewrite_package(good, manifest_change=change)))

    def test_unpack_cli_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "existing.bin"
            output.write_bytes(b"keep")
            argv = ["image_package.py", "unpack", "--package", "unused.zip", "--out", str(output)]
            with patch("sys.argv", argv), self.assertRaises(SystemExit):
                image_package.main()
            self.assertEqual(output.read_bytes(), b"keep")


if __name__ == "__main__":
    unittest.main()

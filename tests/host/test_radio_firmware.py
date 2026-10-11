"""CYW43439 staging fails closed without downloading in host tests."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("radio_firmware", ROOT / "tools/prepare-pico2w-radio.py")
RADIO = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RADIO)


def header(blob, wifi=RADIO.WIFI_LEN, clm=RADIO.CLM_LEN):
    array = ",".join(f"0x{value:02x}" for value in blob)
    return ("static const unsigned char w43439A0_7_95_49_00_combined[] "
            f"CYW43_RESOURCE_ATTRIBUTE = {{\n{array}\n}};\n"
            f"#define CYW43_WIFI_FW_LEN ({wifi})\n#define CYW43_CLM_LEN ({clm})\n").encode()


def bt_record(length, address, record_type, payload=b""):
    return bytes((length,)) + address.to_bytes(2, "big") + bytes((record_type,)) + payload


def bt_header(records, version=b"test", count=0):
    version_with_nul = version + b"\0"
    blob = bytes((len(version_with_nul),)) + version_with_nul + bytes((count,)) + records
    array = ",".join(f"0x{value:02x}" for value in blob)
    return ("const unsigned char cyw43_btfw_43439[] CYW43_RESOURCE_ATTRIBUTE = {\n"
            f"{array}\n}};\n").encode(), blob


class RadioFirmwareTests(unittest.TestCase):
    def test_exact_lengths_and_zero_padding(self):
        blob = b"\x01" * RADIO.WIFI_LEN
        blob += bytes(((RADIO.WIFI_LEN + 511) & ~511) - RADIO.WIFI_LEN)
        blob += b"\x02" * RADIO.CLM_LEN
        self.assertEqual(RADIO.decode_header(header(blob)), blob)
        bad_padding = bytearray(blob)
        bad_padding[RADIO.WIFI_LEN] = 1
        for invalid in (header(blob[:-1]), header(blob, clm=985),
                        header(bad_padding), header(blob).replace(b"0x01", b"256", 1)):
            with self.subTest(invalid=invalid[:80]):
                with self.assertRaises(ValueError):
                    RADIO.decode_header(invalid)

    def test_corrupt_cached_input_is_rejected_without_download(self):
        with tempfile.TemporaryDirectory() as directory:
            cached = Path(directory) / "cached.h"
            cached.write_bytes(b"corrupted")
            with patch.object(RADIO, "urlopen") as download:
                with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                    RADIO.fetch_checked(cached, "https://example.invalid", RADIO.HEADER_SHA256)
                download.assert_not_called()
            self.assertEqual(cached.read_bytes(), b"corrupted")

    def test_bad_download_never_enters_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            cached = Path(directory) / "cached.h"
            with patch.object(RADIO, "urlopen") as download:
                download.return_value.__enter__.return_value.read.return_value = b"bad"
                with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                    RADIO.fetch_checked(cached, "https://example.invalid", RADIO.HEADER_SHA256)
            self.assertFalse(cached.exists())

    def test_bluetooth_records_decode_all_supported_types(self):
        records = b"".join((
            bt_record(3, 0x1234, 0, b"abc"),
            bt_record(2, 0, 2, b"\x12\x34"),
            bt_record(2, 0, 4, b"\x56\x78"),
            bt_record(4, 0, 5, b"\x12\x34\x56\x78"),
            bt_record(0, 0, 1),
            b"\0\0",  # Zero padding after EOF is accepted.
        ))
        source, expected = bt_header(records, count=255)
        self.assertEqual(RADIO.decode_bt_header(source), expected)

    def test_bluetooth_rejects_malformed_framing(self):
        valid = bt_record(1, 0x1234, 0, b"x") + bt_record(0, 0, 1)
        cases = {
            "truncated record header": b"\x01\x00",
            "truncated payload": b"\x02\x00\x01\x00x",
            "unknown record type": bt_record(0, 0, 3),
            "invalid address record length": bt_record(1, 0, 2, b"x"),
            "empty data record": bt_record(0, 0, 0),
            "missing EOF": bt_record(1, 0, 0, b"x"),
            "nonpadding trailing bytes": valid + b"\x01",
            "nonempty EOF": bt_record(1, 0, 1, b"x"),
        }
        for name, records in cases.items():
            source, _ = bt_header(records)
            with self.subTest(case=name), self.assertRaises(ValueError):
                RADIO.decode_bt_header(source)

        source, _ = bt_header(bt_record(0, 0, 1))
        source = source.replace(b"0x00,0x00", b"0x01,0x00", 1)
        with self.assertRaises(ValueError):
            RADIO.decode_bt_header(source)
        with self.assertRaises(ValueError):
            RADIO.decode_bt_header(b"const unsigned char cyw43_btfw_43439[] = { 0x00 };\n")

    def test_checked_download_enforces_exact_source_length(self):
        with tempfile.TemporaryDirectory() as directory:
            cached = Path(directory) / "source.h"
            payload = b"pinned source"
            with patch.object(RADIO, "urlopen") as download:
                download.return_value.__enter__.return_value.read.return_value = payload
                with self.assertRaisesRegex(ValueError, "length mismatch"):
                    RADIO.fetch_checked(cached, "https://example.invalid", RADIO.digest(payload),
                                        len(payload) + 1)
            self.assertFalse(cached.exists())

            cached.write_bytes(payload)
            with patch.object(RADIO, "urlopen") as download:
                self.assertEqual(RADIO.fetch_checked(cached, "https://example.invalid",
                                                     RADIO.digest(payload), len(payload)), payload)
                download.assert_not_called()

    def test_prepare_keeps_wifi_default_and_stages_optional_bluetooth(self):
        wifi_blob = b"\x01" * RADIO.WIFI_LEN
        wifi_blob += bytes(((RADIO.WIFI_LEN + 511) & ~511) - RADIO.WIFI_LEN)
        wifi_blob += b"\x02" * RADIO.CLM_LEN
        wifi_source = header(wifi_blob)
        bt_source, bt_bytes = bt_header(
            bt_record(0, 0, 1) + bytes(RADIO.BT_LEN - 11))
        self.assertEqual(len(bt_bytes), RADIO.BT_LEN)

        def checked(path, _url, _digest, _length=None):
            if path.name == RADIO.HEADER:
                content = wifi_source
            elif path.name == RADIO.BT_HEADER:
                content = bt_source
            else:
                content = b"retained license"
            RADIO.atomic_write(path, content)
            return content

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "radio"
            record = output / "firmware.json"
            with patch.object(RADIO, "fetch_checked", side_effect=checked):
                RADIO.prepare(output, record)
            metadata = json.loads(record.read_text())
            self.assertNotIn("bluetooth", metadata)
            self.assertFalse((output / "cyw43439-bt.bin").exists())
            self.assertTrue((output / "LICENSE.RP").exists())

            with patch.object(RADIO, "fetch_checked", side_effect=checked):
                RADIO.prepare(output, record, bluetooth=True)
            metadata = json.loads(record.read_text())
            bt = metadata["bluetooth"]
            self.assertEqual((output / "cyw43439-bt.bin").read_bytes(), bt_bytes)
            self.assertEqual(bt["source"], f"{RADIO.BASE_URL}/firmware/{RADIO.BT_HEADER}")
            self.assertEqual(bt["header_sha256"], RADIO.BT_HEADER_SHA256)
            self.assertEqual(bt["header_length"], RADIO.BT_HEADER_LEN)
            self.assertEqual(bt["binary_length"], RADIO.BT_LEN)
            self.assertEqual(bt["binary_sha256"], RADIO.digest(bt_bytes))


if __name__ == "__main__":
    unittest.main()

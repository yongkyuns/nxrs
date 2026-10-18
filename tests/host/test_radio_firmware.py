"""CYW43439 staging fails closed without downloading in host tests."""
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("radio_firmware", ROOT / "tools/prepare-pico2w-radio.py")
RADIO = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RADIO)


def header(blob, bluetooth=False, wifi=None, clm=RADIO.CLM_LEN):
    name = (RADIO.WIFI_BT_HEADER if bluetooth else RADIO.HEADER).removesuffix(".h")
    if wifi is None:
        wifi = RADIO.WIFI_BT_LEN if bluetooth else RADIO.WIFI_LEN
    array = ",".join(f"0x{value:02x}" for value in blob)
    return (f"static const unsigned char {name}[] "
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

    def test_bluetooth_wifi_header_exact_lengths_and_array_selection(self):
        blob = b"\x03" * RADIO.WIFI_BT_LEN
        padding_length = ((RADIO.WIFI_BT_LEN + 511) & ~511) - RADIO.WIFI_BT_LEN
        blob += bytes(padding_length) + b"\x04" * RADIO.CLM_LEN
        source = header(blob, bluetooth=True)
        self.assertEqual(RADIO.decode_header(source, bluetooth=True), blob)

        bad_padding = bytearray(blob)
        bad_padding[RADIO.WIFI_BT_LEN] = 1
        invalid_sources = (
            header(blob, bluetooth=True, wifi=RADIO.WIFI_LEN),
            header(blob, bluetooth=True, clm=RADIO.CLM_LEN + 1),
            header(blob[:-1], bluetooth=True),
            header(bad_padding, bluetooth=True),
        )
        for invalid in invalid_sources:
            with self.subTest(invalid=invalid[:100]), self.assertRaises(ValueError):
                RADIO.decode_header(invalid, bluetooth=True)

        wifi_blob = b"\x05" * RADIO.WIFI_LEN
        wifi_blob += bytes(((RADIO.WIFI_LEN + 511) & ~511) - RADIO.WIFI_LEN
                           ) + b"\x06" * RADIO.CLM_LEN
        with self.assertRaisesRegex(ValueError, "missing CYW43439 byte array"):
            RADIO.decode_header(header(wifi_blob), bluetooth=True)
        with self.assertRaisesRegex(ValueError, "missing CYW43439 byte array"):
            RADIO.decode_header(source)

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
        wifi_bt_blob = b"\x03" * RADIO.WIFI_BT_LEN
        wifi_bt_blob += bytes(((RADIO.WIFI_BT_LEN + 511) & ~511) - RADIO.WIFI_BT_LEN)
        wifi_bt_blob += b"\x04" * RADIO.CLM_LEN
        wifi_bt_source = header(wifi_bt_blob, bluetooth=True)
        bt_source, bt_bytes = bt_header(
            bt_record(0, 0, 1) + bytes(RADIO.BT_LEN - 11))
        self.assertEqual(len(bt_bytes), RADIO.BT_LEN)

        def checked(path, _url, _digest, _length=None):
            if path.name == RADIO.HEADER:
                expected = (f"{RADIO.BASE_URL}/firmware/{RADIO.HEADER}",
                            RADIO.HEADER_SHA256, None)
                content = wifi_source
            elif path.name == RADIO.WIFI_BT_HEADER:
                expected = (f"{RADIO.BASE_URL}/firmware/{RADIO.WIFI_BT_HEADER}",
                            RADIO.WIFI_BT_HEADER_SHA256, None)
                content = wifi_bt_source
            elif path.name == RADIO.BT_HEADER:
                expected = (f"{RADIO.BASE_URL}/firmware/{RADIO.BT_HEADER}",
                            RADIO.BT_HEADER_SHA256, RADIO.BT_HEADER_LEN)
                content = bt_source
            else:
                expected = (f"{RADIO.BASE_URL}/LICENSE.RP", RADIO.LICENSE_SHA256, None)
                content = b"retained license"
            self.assertEqual((_url, _digest, _length), expected)
            RADIO.atomic_write(path, content)
            return content

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "radio"
            record = output / "firmware.json"
            with patch.object(RADIO, "fetch_checked", side_effect=checked):
                wifi_firmware = RADIO.prepare(output, record)
            metadata = json.loads(record.read_text())
            self.assertFalse(metadata["wifi_bluetooth"])
            self.assertEqual(metadata["wifi_length"], RADIO.WIFI_LEN)
            self.assertEqual(metadata["source"], f"{RADIO.BASE_URL}/firmware/{RADIO.HEADER}")
            self.assertEqual(metadata["header_sha256"], RADIO.HEADER_SHA256)
            self.assertEqual(wifi_firmware, output / "cyw43439.bin")
            self.assertEqual(wifi_firmware.read_bytes(), wifi_blob)
            self.assertFalse((output / "cyw43439-bt.bin").exists())
            self.assertTrue((output / "LICENSE.RP").exists())

            with patch.object(RADIO, "fetch_checked", side_effect=checked):
                wifi_bt_firmware = RADIO.prepare(output, record, bluetooth=True)
            metadata = json.loads(record.read_text())
            self.assertTrue(metadata["wifi_bluetooth"])
            self.assertEqual(metadata["wifi_length"], RADIO.WIFI_BT_LEN)
            self.assertEqual(metadata["source"],
                             f"{RADIO.BASE_URL}/firmware/{RADIO.WIFI_BT_HEADER}")
            self.assertEqual(metadata["header_sha256"], RADIO.WIFI_BT_HEADER_SHA256)
            self.assertEqual(wifi_bt_firmware, output / "cyw43439-wb.bin")
            self.assertEqual(wifi_bt_firmware.read_bytes(), wifi_bt_blob)
            self.assertNotEqual(wifi_firmware, wifi_bt_firmware)
            self.assertEqual(wifi_firmware.read_bytes(), wifi_blob)
            bt = metadata["bluetooth"]
            self.assertEqual((output / "cyw43439-bt.bin").read_bytes(), bt_bytes)
            self.assertEqual(bt["source"], f"{RADIO.BASE_URL}/firmware/{RADIO.BT_HEADER}")
            self.assertEqual(bt["header_sha256"], RADIO.BT_HEADER_SHA256)
            self.assertEqual(bt["header_length"], RADIO.BT_HEADER_LEN)
            self.assertEqual(bt["binary_length"], RADIO.BT_LEN)
            self.assertEqual(bt["binary_sha256"], RADIO.digest(bt_bytes))
            self.assertEqual(bt["version"], "test")

            with patch.object(RADIO, "fetch_checked", side_effect=checked):
                RADIO.prepare(output, record, bluetooth=False)
            metadata = json.loads(record.read_text())
            self.assertFalse(metadata["wifi_bluetooth"])
            self.assertEqual(wifi_firmware.read_bytes(), wifi_blob)
            self.assertEqual(wifi_bt_firmware.read_bytes(), wifi_bt_blob)

    def test_cli_uses_revision_cache_without_overwriting_legacy_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            legacy = cache / "cyw43439-bt.bin"
            legacy.write_bytes(b"retained older image input")
            expected = cache.resolve() / RADIO.REVISION
            with patch("sys.argv", ["prepare-pico2w-radio.py", "--out", str(cache), "--bluetooth",
                                    "--release", "pico-sdk-2.3.1"]), \
                 patch.object(RADIO, "prepare", return_value=expected / "cyw43439-wb.bin") as prepare, \
                 patch("builtins.print"):
                RADIO.main()
            prepare.assert_called_once_with(expected, expected / "firmware.json", bluetooth=True,
                                            release=RADIO.DEFAULT_RELEASE)
            self.assertEqual(legacy.read_bytes(), b"retained older image input")

    def test_older_release_is_explicit_and_gets_its_own_checked_metadata(self):
        older = RADIO.RELEASES["pico-sdk-2.1.1"]
        source, expected = bt_header(bt_record(0, 0, 1) + bytes(older.bt_length - 11))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            record = output / "record.json"
            def checked(path, url, digest, length=None):
                if path.name == RADIO.BT_HEADER:
                    self.assertIn(older.revision, url)
                    self.assertEqual((digest, length), (older.bt_header_sha256, older.bt_header_length))
                    return source
                if path.name == RADIO.WIFI_BT_HEADER:
                    self.assertIn(older.revision, url)
                    return b"mocked Wi-Fi header"
                return b"retained licence"
            with patch.object(RADIO, "fetch_checked", side_effect=checked), \
                 patch.object(RADIO, "decode_header", return_value=b"Wi-Fi bytes"):
                RADIO.prepare(output, record, bluetooth=True, release=older)
            metadata = json.loads(record.read_text())
            self.assertEqual(metadata["revision"], older.revision)
            self.assertEqual(metadata["sdk_release"], older.sdk)
            self.assertEqual(metadata["bluetooth"]["binary_length"], older.bt_length)
            self.assertEqual((output / "cyw43439-bt.bin").read_bytes(), expected)

    def test_build_environment_selects_the_retained_release(self):
        older = RADIO.RELEASES["pico-sdk-2.1.1"]
        with tempfile.TemporaryDirectory() as directory:
            expected = Path(directory).resolve() / older.revision
            with patch.dict("os.environ", {"NXRS_PICO_RADIO_RELEASE": "pico-sdk-2.1.1"}), \
                 patch("sys.argv", ["prepare-pico2w-radio.py", "--out", directory]), \
                 patch.object(RADIO, "prepare", return_value=expected / "cyw43439.bin") as prepare, \
                 patch("builtins.print"):
                RADIO.main()
            prepare.assert_called_once_with(expected, expected / "firmware.json", bluetooth=False,
                                            release=older)

    def test_invalid_build_release_fails_before_staging(self):
        with patch.dict("os.environ", {"NXRS_PICO_RADIO_RELEASE": "invalid-pin-value"}), \
             patch("sys.argv", ["prepare-pico2w-radio.py"]), \
             patch.object(RADIO, "prepare") as prepare, \
             patch("sys.stderr", new_callable=io.StringIO) as output:
            with self.assertRaises(SystemExit) as error:
                RADIO.main()
            self.assertEqual(error.exception.code, 2)
            prepare.assert_not_called()
            self.assertNotIn("invalid-pin-value", output.getvalue())


if __name__ == "__main__":
    unittest.main()

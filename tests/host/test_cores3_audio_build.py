"""Negative controls for post-link audio qualification evidence."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location(
    "audio_build", Path(__file__).with_name("check_cores3_audio_build.py"))
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


class AudioBuildEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)
        self.native = self.out / "nuttx"
        self.native.mkdir()
        # Synthetic artifacts exercise the evidence validator, not a compiler.
        for name, data in (("nuttx", b"\x7fELFfixture"), ("nuttx.bin", b"image"), ("nuttx.map", b"map")):
            (self.native / name).write_bytes(data)
        self.base = {"ARCH_BOARD_ESP32S3_M5_CORES3": "y", "ESP32S3_FLASH_16M": "y",
                     "BUILD_FLAT": "y", "DEBUG_LINK_MAP": "y"}
        self.mic = dict(self.base, **CHECK.PINS)
        self.mic.update({key: "y" for key in (
            "ESP32S3_M5_CORES3_MIC", "AUDIO_ES7210", "AUDIO", "SYSTEM_NXRECORDER",
            "ESPRESSIF_I2S0_RX", "ESPRESSIF_I2S0_MCLK", "ESPRESSIF_I2S0_ROLE_MASTER")})
        self.mic.update({"ESPRESSIF_I2S0_SAMPLE_RATE": "16000",
                         "ES7210_BUFFER_SIZE": "1024", "ES7210_NUM_BUFFERS": "4"})
        self.symbols = "\n".join("40000000 T " + name for name in CHECK.SYMBOLS)

    def config(self, settings):
        (self.native / ".config").write_text("\n".join(f"CONFIG_{k}={v}" for k, v in settings.items()))

    def test_both_expected_images_record_hashes_but_not_physical_success(self):
        for profile, config, symbols in (
            ("cores3-microphone", self.mic, self.symbols),
            ("m5stack-cores3-gnss", self.base, "40000000 T event_demo_main")):
            with self.subTest(profile=profile):
                self.config(config)
                record = CHECK.verify(self.out, profile, symbols)
                self.assertEqual(len(record["sha256"]), 4)
                self.assertFalse(record["physical_capture_verified"])
                self.assertEqual(record["microphone_linked"], profile == "cores3-microphone")

    def test_missing_or_changed_required_config_is_rejected(self):
        for key in self.mic:
            for value in (None, "wrong"):
                with self.subTest(key=key, value=value):
                    config = dict(self.mic)
                    if value is None:
                        config.pop(key)
                    else:
                        config[key] = value
                    self.config(config)
                    with self.assertRaises(ValueError):
                        CHECK.verify(self.out, "cores3-microphone", self.symbols)

    def test_tx_and_unqualified_features_are_rejected(self):
        for key in ("ESPRESSIF_I2S0_TX", "ESPRESSIF_I2S1", "AUDIO_EXCLUDE_STOP",
                    "AUDIO_MULTI_SESSION", "ESP32S3_SPIRAM", "ESP32S3_M5_CORES3_LCD",
                    "ESPRESSIF_WIFI", "ESPRESSIF_BLE"):
            with self.subTest(key=key):
                self.config(dict(self.mic, **{key: "y"}))
                with self.assertRaisesRegex(ValueError, "unexpected enabled"):
                    CHECK.verify(self.out, "cores3-microphone", self.symbols)

    def test_control_rejects_audio_config_or_linked_symbols(self):
        self.config(self.base)
        with self.assertRaisesRegex(ValueError, "linked audio symbols"):
            CHECK.verify(self.out, "m5stack-cores3-gnss", self.symbols)
        for key in ("ESP32S3_M5_CORES3_MIC", "AUDIO_ES7210", "SYSTEM_NXRECORDER"):
            self.config(dict(self.base, **{key: "y"}))
            with self.assertRaisesRegex(ValueError, "unexpected enabled"):
                CHECK.verify(self.out, "m5stack-cores3-gnss", "")

    def test_missing_linked_microphone_symbol_is_rejected(self):
        self.config(self.mic)
        for symbol in CHECK.SYMBOLS:
            with self.assertRaisesRegex(ValueError, "linked audio symbols"):
                CHECK.verify(self.out, "cores3-microphone", "40000000 T " + symbol)

    def test_missing_empty_or_non_elf_artifacts_are_rejected(self):
        self.config(self.mic)
        for name in (".config", "nuttx", "nuttx.bin", "nuttx.map"):
            path = self.native / name
            original = path.read_bytes()
            for data in (None, b""):
                with self.subTest(name=name, data=data):
                    path.unlink()
                    if data is not None:
                        path.write_bytes(data)
                    with self.assertRaisesRegex(ValueError, "missing or empty"):
                        CHECK.verify(self.out, "cores3-microphone", self.symbols)
                    path.write_bytes(original)
        (self.native / "nuttx").write_bytes(b"not an ELF")
        with self.assertRaisesRegex(ValueError, "not an ELF"):
            CHECK.verify(self.out, "cores3-microphone", self.symbols)

    def test_duplicate_config_and_unknown_profile_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            CHECK.configuration("CONFIG_AUDIO=y\n# CONFIG_AUDIO is not set\n")
        with self.assertRaisesRegex(ValueError, "unknown qualification"):
            CHECK.verify(self.out, "typo", "")


if __name__ == "__main__":
    unittest.main()

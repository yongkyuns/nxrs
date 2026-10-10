import unittest

from evidence import kernel_header_identity, psram_enabled, psram_identity, require_restored


HEADER = "a" * 64


class EvidenceTests(unittest.TestCase):
    def test_resolved_psram_setting_is_read_exactly(self):
        self.assertTrue(psram_enabled("CONFIG_ESP32S3_SPIRAM=y\n"))
        for text in ("", "# CONFIG_ESP32S3_SPIRAM is not set\n",
                     "CONFIG_ESP32S3_SPIRAM=n\n", "CONFIG_ESP32S3_SPIRAM_BOOT_INIT=y\n"):
            with self.subTest(config=text):
                self.assertFalse(psram_enabled(text))

    def test_psram_identity_distinguishes_legacy_unknown_from_disabled(self):
        self.assertIsNone(psram_identity({"c": {}, "rust": {}}))
        for state in (False, True):
            builds = {language: {"psram_enabled": state} for language in ("c", "rust")}
            self.assertIs(psram_identity(builds), state)

    def test_psram_identity_rejects_missing_mismatched_or_non_boolean_states(self):
        for state in (None, 0, 1, "false", True):
            builds = {"c": {"psram_enabled": False}, "rust": {"psram_enabled": state}}
            with self.subTest(state=repr(state)), self.assertRaisesRegex(ValueError, "PSRAM"):
                psram_identity(builds)
        with self.assertRaisesRegex(ValueError, "PSRAM"):
            psram_identity({"c": {"psram_enabled": False}, "rust": {}})

    def test_kernel_header_identity_requires_exact_paired_inventory(self):
        builds = {language: {"kernel_header_sha256": HEADER} for language in ("c", "rust")}
        self.assertEqual(kernel_header_identity(builds), HEADER)
        for invalid in (
            None, "", "a" * 63, "a" * 63 + "g", HEADER + "\n", 7, True,
        ):
            bad = {language: {"kernel_header_sha256": invalid} for language in ("c", "rust")}
            with self.subTest(header=repr(invalid)), self.assertRaises(ValueError):
                kernel_header_identity(bad)
        for bad in (
            {"c": {}, "rust": {}},
            {"c": {"kernel_header_sha256": HEADER}, "rust": {}},
            {"c": {"kernel_header_sha256": HEADER}, "rust": {"kernel_header_sha256": "b" * 64}},
            {"c": {"kernel_header_sha256": HEADER}, "rust": {"kernel_header_sha256": HEADER}, "extra": {}},
            {"c": {"kernel_header_sha256": HEADER}},
        ):
            with self.subTest(builds=bad), self.assertRaises(ValueError):
                kernel_header_identity(bad)

    def test_restoration_gate_accepts_historical_absence_and_null(self):
        require_restored({"failure": None, "restoration_verified": True})
        require_restored({"failure": None, "restoration_error": None, "restoration_verified": True})

    def test_restoration_gate_rejects_failure_error_or_unverified_state(self):
        for bad in (
            {"failure": "capture failed", "restoration_error": None, "restoration_verified": True},
            {"failure": None, "restoration_error": "restore failed", "restoration_verified": True},
            {"failure": None, "restoration_error": None, "restoration_verified": False},
            {"failure": None, "restoration_verified": 1},
        ):
            with self.subTest(report=bad), self.assertRaises(ValueError):
                require_restored(bad)


if __name__ == "__main__":
    unittest.main()

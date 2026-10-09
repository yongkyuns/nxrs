import unittest

from evidence import kernel_header_identity, require_restored


HEADER = "a" * 64


class EvidenceTests(unittest.TestCase):
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

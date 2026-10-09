import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from layout_report import HOT_SYMBOLS, addresses, public_build, public_cases


def symbols(base):
    return "\n".join(f"{base + i * 64:08x} T {name}" for i, name in enumerate(HOT_SYMBOLS))


class LayoutReportTests(unittest.TestCase):
    def test_address_parser_requires_complete_unique_hot_path(self):
        text = symbols(0x42010000)
        self.assertEqual(addresses(text)["nxrs_sq_worker"], 0x42010000)
        for bad in ("", text + "\n42020000 T poll"):
            with self.assertRaises(ValueError):
                addresses(bad)

    def test_export_rejects_wrong_padding_and_unmoved_hot_code(self):
        row = dict(artifacts={}, diagnostic_layout_padding_bytes=16,
                   diagnostic_hot_iram=False, compiler_input=None)
        text = symbols(0x42010000) + "\n42010000 T _stext\n42020000 T nxrs_sq_layout_padding"
        with patch("layout_report.subprocess.check_output", return_value=text):
            with self.assertRaisesRegex(ValueError, "beginning"):
                public_build(row, Path("unused"), "unused-")
        row.update(diagnostic_layout_padding_bytes=None, diagnostic_hot_iram=True)
        with patch("layout_report.subprocess.check_output", return_value=text):
            with self.assertRaisesRegex(ValueError, "IRAM"):
                public_build(row, Path("unused"), "unused-")

    def test_export_rehashes_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "app.elf").write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "changed firmware"):
                public_build({"artifacts": {"app.elf": "stale"}}, folder, "unused-")

    def test_case_validation_and_private_data_exclusion(self):
        build = dict(config_identity="same", kernel_archives={"kernel": "sha"},
                     c_flags=["-O2"], c_compiler_sha256="gcc", thread_stack=4096,
                     kernel_header_sha256="a" * 64, diagnostic_perfmon=True)
        pm = dict(mode="fetch", select0=4, mask0=35, value0=42,
                  select1=5, mask1=32, value1=12, overflow=0)
        report = dict(failure=None, restoration_verified=True, source="messages",
                      period_us=2000, builds={"c": build, "rust": copy.deepcopy(build)},
                      restoration_error=None,
                      runs=[dict(language="c", block=0, done={"status": 0}, perfmon=pm,
                                 result=dict(errors=0, received=1000, events=1000, services=3,
                                             mean_cycles=240, max_cycles=480, misses_1ms=0),
                                 transcript="private serial", private_path="private host")],
                      backup_sha256="private backup")
        with tempfile.TemporaryDirectory() as directory, \
                patch("layout_report.public_build", return_value={"safe": True,
                                                                   "kernel_header_sha256": "a" * 64}):
            path = Path(directory) / "report.json"

            def export(value):
                path.write_text(json.dumps(value))
                return public_cases([("fetch", path, directory, directory)], "unused-")

            public = export(report)
            self.assertTrue(public["cases"][0]["paired_kernel_headers_verified"])
            for build_row in public["cases"][0]["builds"].values():
                self.assertEqual(build_row["kernel_header_sha256"], "a" * 64)
            self.assertNotIn("private", json.dumps(public))
            self.assertEqual(public["cases"][0]["runs"][0]["perfmon"], pm)
            for field, value in (("failure", "failed"), ("restoration_verified", False),
                                 ("restoration_error", "restore failed"),
                                 ("diagnostic_trace", True)):
                bad = copy.deepcopy(report)
                bad[field] = value
                with self.assertRaises(ValueError):
                    export(bad)
            for language, header in (("c", "header"), ("rust", "b" * 64)):
                bad = copy.deepcopy(report)
                bad["builds"][language]["kernel_header_sha256"] = header
                with self.subTest(language=language), self.assertRaises(ValueError):
                    export(bad)
            for field in ("kernel_archives", "kernel_header_sha256", "thread_stack"):
                bad = copy.deepcopy(report)
                bad["builds"]["rust"][field] = "different"
                with self.assertRaises(ValueError):
                    export(bad)
            for field, value in (("overflow", 1), ("mask0", 1), ("select1", 4)):
                bad = copy.deepcopy(report)
                bad["runs"][0]["perfmon"][field] = value
                with self.assertRaises(ValueError):
                    export(bad)
            bad = copy.deepcopy(report)
            bad["runs"][0]["result"]["received"] -= 1
            with self.assertRaises(ValueError):
                export(bad)


if __name__ == "__main__":
    unittest.main()

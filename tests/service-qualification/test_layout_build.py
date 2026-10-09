import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import build


class LayoutBuildTests(unittest.TestCase):
    def test_default_staging_and_flags_remain_unchanged(self):
        self.assertEqual(build.source_inputs("c"), build.source_inputs("c", layout_pad_bytes=None))
        self.assertEqual(
            build.make_variables("compiler-", "c", []),
            build.make_variables("compiler-", "c", [], layout_pad_bytes=None),
        )
        with tempfile.TemporaryDirectory() as folder:
            app = Path(folder) / "apps/examples/nxrs_bench"
            build.stage_native_app(Path(folder), "c")
            self.assertFalse((app / "layout_padding.c").exists())
            self.assertFalse((app / "sq_layout_padding.h").exists())

    def test_staging_generates_deterministic_header_and_precedes_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            sources, native = build.stage_native_app(Path(folder), "c", layout_pad_bytes=64)
            app = Path(folder) / "apps/examples/nxrs_bench"
            self.assertEqual((app / "sq_layout_padding.h").read_text(),
                             "#define SQ_LAYOUT_PADDING_BYTES 64\n")
            self.assertEqual(sources[0], build.HERE / "layout_padding.c")
            self.assertEqual(native[:2], ["layout_padding.c", "runtime.c"])
            self.assertEqual((app / "layout_padding.c").read_bytes(),
                             (build.HERE / "layout_padding.c").read_bytes())

    def test_link_retention_composes_with_trace_wrappers(self):
        plain = build.make_variables("compiler-", "c", [], layout_pad_bytes=0)
        self.assertIn("EXTRALINKCMDS=--undefined=nxrs_sq_layout_padding", plain)
        traced = build.make_variables("compiler-", "rust", [], bundle=Path("/bundle"), trace=True,
                                      dual_worker=True, layout_pad_bytes=2048)
        link = next(item for item in traced if item.startswith("EXTRALINKCMDS="))
        for wrapped in (*build.TRACE_WRAPS, "nxrs_sq_worker"):
            self.assertIn("--wrap=" + wrapped, link)
        self.assertTrue(link.endswith("--undefined=nxrs_sq_layout_padding"))
        self.assertIn("NXRS_TARGET_C_FLAGS=-std=c11 -O2", traced)

    def test_cli_rejects_out_of_range_and_unaligned_padding_without_building(self):
        for amount in ("-4", "2", "2049"):
            with self.subTest(amount=amount):
                result = subprocess.run(
                    [sys.executable, str(build.HERE / "build.py"), "link",
                     "--tree", "/unused", "--prefix", "xtensa-", "--out", "/unused-out",
                     "--language", "c", "--baseline", "/unused-config",
                     "--layout-pad-bytes", amount],
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn("must be between 0 and 2048", result.stderr)

    def test_cli_accepts_padding_boundaries_without_building(self):
        for amount in ("0", "2048"):
            with self.subTest(amount=amount):
                result = subprocess.run(
                    [sys.executable, str(build.HERE / "build.py"), "link",
                     "--tree", "/unused", "--prefix", "xtensa-", "--out", "/unused-out",
                     "--language", "c", "--baseline", "/unused-config",
                     "--layout-pad-bytes", amount],
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                # Valid arguments advance to filesystem validation in final_link.
                self.assertNotEqual(result.returncode, 2)
                self.assertNotIn("must be between 0 and 2048", result.stderr)


if __name__ == "__main__":
    unittest.main()

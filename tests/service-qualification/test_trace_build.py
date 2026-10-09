import unittest
from pathlib import Path
import tempfile

import build


class TraceBuildTests(unittest.TestCase):
    def test_entry_switch_stages_a_distinct_native_entry_without_editing_upstream_template(self):
        template = build.ROOT / "platform/nuttx/std-app/Makefile"
        original = template.read_text()
        with tempfile.TemporaryDirectory() as folder:
            sources, native = build.stage_native_app(Path(folder), "rust", trace=True,
                                                     dual_worker=True, entry_switch=True)
            app = Path(folder) / "apps/examples/nxrs_std_app"
            self.assertIn("--redefine-sym main=sq_std_entry", (app / "Makefile").read_text())
            self.assertIn("entry_switch.c", native)
            self.assertNotIn("worker.c", native)
            self.assertIn(build.HERE / "entry_switch.c", sources)
        self.assertEqual(template.read_text(), original)

    def test_normal_inputs_and_flags_do_not_include_diagnostics(self):
        self.assertNotIn(build.HERE / "trace.c", build.source_inputs("rust"))
        self.assertFalse(any(value.startswith("EXTRALINKCMDS=")
                             for value in build.make_variables("compiler-", "c", [])))

    def test_dual_worker_reuses_reference_source_and_wraps_only_diagnostic_link(self):
        sources = build.source_inputs("rust", trace=True, dual_worker=True)
        for name in ("trace.c", "worker_switch.c", "worker.c"):
            self.assertIn(build.HERE / name, sources)
        flags = build.make_variables("compiler-", "c", [], trace=True, dual_worker=True)
        link = next(value for value in flags if value.startswith("EXTRALINKCMDS="))
        for name in (*build.TRACE_WRAPS, "nxrs_sq_worker"):
            self.assertIn("--wrap=" + name, link)
        self.assertIn("NXRS_TARGET_C_FLAGS=-std=c11 -O2", flags)


if __name__ == "__main__":
    unittest.main()

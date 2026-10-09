import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import build


class PulseSnapshotTest(unittest.TestCase):
    def test_snapshot_header_is_staged_and_inventoried_for_both_targets(self):
        header = Path(__file__).resolve().parent / "pulse_snapshot.h"
        for language in ("c", "rust"):
            with self.subTest(language=language), tempfile.TemporaryDirectory() as folder:
                sources, _ = build.stage_native_app(Path(folder), language)
                self.assertIn(header, sources)
                app = "nxrs_std_app" if language == "rust" else "nxrs_bench"
                staged = Path(folder) / "apps/examples" / app / header.name
                self.assertEqual(staged.read_bytes(), header.read_bytes())

    def test_shared_header_regressions(self):
        compiler = shutil.which("cc")
        if compiler is None:
            self.skipTest("a C compiler named 'cc' is required")

        test_dir = Path(__file__).resolve().parent
        source = test_dir / "pulse_snapshot_test.c"
        with tempfile.TemporaryDirectory(prefix="pulse-snapshot-") as temp_dir:
            executable = Path(temp_dir) / "pulse_snapshot_test"
            compile_result = subprocess.run(
                [
                    compiler,
                    "-std=c11",
                    "-O2",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-pthread",
                    "-I",
                    str(test_dir),
                    str(source),
                    "-o",
                    str(executable),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(
                compile_result.returncode,
                0,
                msg=f"C compilation failed:\n{compile_result.stdout}{compile_result.stderr}",
            )
            self.assertEqual(compile_result.stdout, "")
            self.assertEqual(compile_result.stderr, "")

            run_result = subprocess.run(
                [str(executable)],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(
                run_result.returncode,
                0,
                msg=f"C regression executable failed:\n{run_result.stdout}{run_result.stderr}",
            )
            self.assertEqual(run_result.stdout, "pulse snapshot tests: PASS\n")
            self.assertEqual(run_result.stderr, "")


if __name__ == "__main__":
    unittest.main()

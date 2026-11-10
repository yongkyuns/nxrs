"""Compile the actual shared-stack configuration and observation calculations."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent


class EmbassyStackTests(unittest.TestCase):
    def test_shared_stack_boundaries_and_observation_failures(self):
        with tempfile.TemporaryDirectory() as temporary:
            for name in ("stack_config", "stack_watermark"):
                binary = Path(temporary) / name
                subprocess.run(
                    ["rustc", "+1.90.0", "--edition=2024", "-Adead_code", "--test",
                     str(HERE / f"{name}.rs"), "-o", str(binary)],
                    check=True, capture_output=True, text=True,
                )
                subprocess.run([binary], check=True, capture_output=True, text=True)

    def test_real_build_script_generates_matching_linker_and_rust_sizes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "build-script"
            subprocess.run(
                ["rustc", "+1.90.0", "--edition=2024", str(HERE / "build.rs"),
                 "-o", str(binary)],
                check=True, capture_output=True, text=True,
            )
            env = dict(os.environ, OUT_DIR=str(root))
            env.pop("NXRS_EVENT_STACK_BYTES", None)
            env.pop("NXRS_EVENT_STACK_WATERMARK", None)
            env.pop("NXRS_EVENT_CASE", None)
            subprocess.run([binary], cwd=HERE, env=env, check=True, capture_output=True, text=True)
            self.assertIn(". += 8192;", (root / "stack.x").read_text())
            self.assertIn("STACK_WATERMARK: bool = false", (root / "event_stack.rs").read_text())
            env.update(NXRS_EVENT_STACK_BYTES="4096", NXRS_EVENT_STACK_WATERMARK="1")
            result = subprocess.run(
                [binary], cwd=HERE, env=env, check=True, capture_output=True, text=True,
            )
            self.assertIn("rerun-if-env-changed=NXRS_EVENT_STACK_BYTES", result.stdout)
            self.assertIn("LENGTH(RWDATA) - 4096", (root / "stack.x").read_text())
            self.assertIn(". += 4096;", (root / "stack.x").read_text())
            self.assertIn("SHARED_STACK_BYTES: usize = 4096", (root / "event_stack.rs").read_text())
            self.assertIn("STACK_WATERMARK: bool = true", (root / "event_stack.rs").read_text())


if __name__ == "__main__":
    unittest.main()

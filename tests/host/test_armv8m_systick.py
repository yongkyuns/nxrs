"""Execute pinned timer functions with scripted MMIO; no hardware timing claim."""

import os
from pathlib import Path
import re
import runpy
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SYSTICK = "arch/arm/src/armv8-m/arm_systick.c"
TIMER = "drivers/timers/arch_timer.c"
PATCH = "0149-armv8m-systick-coherent-status.patch"


def function(source, name):
    match = re.search(rf"(?m)^static[^;\n]*\b{name}\([^;]*?\)\n\{{", source)
    if match is None:
        raise AssertionError(f"missing pinned native function: {name}")
    return source[match.start():source.index("\n}\n", match.end()) + 3]


def structure(source, name):
    match = re.search(rf"(?m)^struct {name}\n\{{.*?^}};", source, re.S)
    if match is None:
        raise AssertionError(f"missing pinned native structure: {name}")
    return match.group() + "\n"


class Armv8mSystickTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory(prefix="nxrs-systick-")
        cls.addClassCleanup(temporary.cleanup)
        root = Path(temporary.name)
        revision = subprocess.check_output(
            ["git", "rev-parse", ":external/nuttx"], cwd=ROOT, text=True,
        ).strip()
        for path in (SYSTICK, TIMER):
            source = subprocess.check_output(
                ["git", "-C", str(ROOT / "external/nuttx"), "show", f"{revision}:{path}"],
                text=True,
            )
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source)

        series = runpy.run_path(str(ROOT / "tools/apply-nuttx-patches.py"))["SERIES"]["nuttx"]
        if series.count(PATCH) != 1:
            raise AssertionError("tested SysTick patch must occur once in the firmware series")
        env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(root.parent)}

        def apply(name):
            subprocess.run(
                ["git", "apply", "--whitespace=error", "--include=" + SYSTICK,
                 "--include=" + TIMER, str(ROOT / "upstream/nuttx/patches" / name)],
                cwd=root, env=env, check=True, capture_output=True,
            )

        boundary = series.index(PATCH)
        for name in series[:boundary]:
            apply(name)
        cls.binaries = {}
        for label in ("original", "patched"):
            if label == "patched":
                for name in series[boundary:]:
                    apply(name)
            source = (root / SYSTICK).read_text()
            timer = (root / TIMER).read_text()
            # Compile the real lower-half arithmetic AND the upper-half read/
            # timebase retry. Only registers, IRQ entry and callback delivery
            # are injected. The two independent bug timelines must fail before
            # this patch; a hand-written clock model would not establish that.
            reload_macro = re.search(r"(?m)^#define RELOAD2TIMEOUT.*$", source)
            if reload_macro is None:
                raise AssertionError("missing pinned reload conversion")
            native = reload_macro.group() + "\n"
            native += structure(source, "systick_lowerhalf_s")
            native += structure(timer, "arch_timer_s")
            native += "static struct arch_timer_s g_timer;\n"
            native += "\n".join(function(source, name) for name in (
                "usec_from_count", "systick_is_running", "systick_irq_pending", "systick_getstatus",
            ))
            native += function(timer, "current_usec")
            (root / "systick.inc").write_text(native)
            executable = root / label
            built = subprocess.run(
                [os.environ.get("CC", "cc"), "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                 "-fsanitize=undefined", "-fno-sanitize-recover=all", "-I", str(root),
                 str(ROOT / "tests/host/armv8m_systick.c"), "-o", str(executable)],
                capture_output=True, text=True,
            )
            if built.returncode:
                raise AssertionError(built.stdout + built.stderr)
            cls.binaries[label] = executable

    def cases(self, names, original_failures=None):
        for name in names:
            with self.subTest(case=name):
                for label in ("original", "patched"):
                    result = subprocess.run(
                        [str(self.binaries[label]), name], capture_output=True,
                        text=True, timeout=5,
                    )
                    if label == "original" and original_failures is not None:
                        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                        self.assertIn("clock contract failed", result.stderr)
                        self.assertEqual(result.stdout.strip(), original_failures[name])
                    else:
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_wrap_between_current_and_pending_cannot_add_an_extra_tick(self):
        self.cases(("rollover",), {"rollover": "13999 13002"})

    def test_nonzero_fractional_count_is_not_a_reset_counter(self):
        self.cases(("fractional",), {"fractional": "12999 12000 13000 13001"})

    def test_pending_fractional_count_includes_the_completed_period(self):
        self.cases(("pending-fractional",), {"pending-fractional": "13000"})

    def test_normal_pending_and_exact_zero_boundaries(self):
        self.cases(("normal", "pending", "pending-zero", "reset-zero", "stopped"))

    def test_active_callback_and_timebase_retry(self):
        self.cases(("callback", "timebase-retry"))


if __name__ == "__main__":
    unittest.main()

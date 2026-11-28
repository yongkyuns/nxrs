"""Actual pinned software-clock regressions; host arithmetic, not IRQ/RTC timing.

The counter and RTC calls are mechanical stubs. Both clock implementations and
public wrappers come from the registered NuttX patch series, not a copied model.
Symbols are renamed so no test can call or alter the host operating clock.
"""

import ctypes as C
import errno
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
PATCH = "0150-clock-timekeeping-adjustment.patch"
SOURCES = tuple("sched/clock/" + name for name in (
    "clock_timekeeping.c", "clock_gettime.c", "clock_settime.c",
))
STUB_HEADERS = (
    "nuttx/config.h", "nuttx/debug.h", "nuttx/irq.h", "nuttx/arch.h",
    "nuttx/clock_notifier.h", "nuttx/fs/fs.h", "nuttx/nuttx.h", "nuttx/sched.h",
    "nuttx/spinlock.h", "nuttx/timers/ptp_clock.h", "sched/sched.h",
    "clock/clock.h", "clock/clock_timekeeping.h",
)
NS = 1_000_000_000
MAX64 = 2**63 - 1
MIN64 = -(2**63)
MASK = 2**64 - 1
BASE = 1_800_000_000 * NS
SENTINEL = (1234567, 7654321)


class Timespec(C.Structure):
    _fields_ = [("seconds", C.c_long), ("nanoseconds", C.c_long)]


class Timeval(C.Structure):
    _fields_ = [("seconds", C.c_long), ("microseconds", C.c_long)]


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def signed_timeval(microseconds):
    sign = -1 if microseconds < 0 else 1
    magnitude = abs(microseconds)
    return sign * (magnitude // 1_000_000), sign * (magnitude % 1_000_000)


def pending_timeval(nanoseconds):
    sign = -1 if nanoseconds < 0 else 1
    return signed_timeval(sign * ((abs(nanoseconds) + 999) // 1000))


class Witness:
    def __init__(self, path, tick_us):
        check(C.sizeof(C.c_long) == 8, "witness requires the recorded LP64 host ABI")
        self.lib = C.CDLL(str(path), use_errno=True)
        self.tick_ns = tick_us * 1000
        self.lib.clock_probe_test_timer.argtypes = [C.c_uint64, C.c_uint64, C.c_int]
        if hasattr(self.lib, "clock_probe_test_adjtime"):
            self.lib.clock_probe_test_adjtime.argtypes = [C.POINTER(Timeval), C.POINTER(Timeval)]
            self.lib.clock_probe_test_adjtime.restype = C.c_int
        self.lib.clock_inittimekeeping.argtypes = [C.POINTER(Timespec)]
        self.lib.clock_timekeeping_set_wall_time.argtypes = [C.POINTER(Timespec)]
        self.lib.clock_timekeeping_set_wall_time.restype = C.c_int
        self.lib.clock_timekeeping_get_wall_time.argtypes = [C.POINTER(Timespec)]
        self.lib.clock_timekeeping_get_wall_time.restype = C.c_int
        for name in ["gettime", "nxgettime", "settime"]:
            function = getattr(self.lib, "clock_probe_test_" + name)
            function.argtypes = [C.c_int, C.POINTER(Timespec)]
            function.restype = C.c_int
        self.lib.clock_probe_test_rtc_writes.restype = C.c_uint

    def timer(self, counter=None, error=0):
        if counter is not None:
            self.counter = counter
        self.lib.clock_probe_test_timer(self.counter & self.mask, self.mask, error)

    def reset(self, base=BASE, counter=0, mask=MASK):
        self.counter, self.mask = counter, mask
        self.timer()
        value = Timespec(*divmod(base, NS))
        self.lib.clock_inittimekeeping(C.byref(value))
        check(self.lib.clock_timekeeping_set_wall_time(C.byref(value)) == 0, "reset failed")

    def advance(self, nanoseconds):
        check(nanoseconds % self.tick_ns == 0, "test interval is not a whole raw tick")
        self.timer(self.counter + nanoseconds // self.tick_ns)

    def read_result(self):
        value = Timespec(*SENTINEL)
        result = self.lib.clock_timekeeping_get_wall_time(C.byref(value))
        return result, (value.seconds, value.nanoseconds)

    def read(self):
        result, value = self.read_result()
        check(result == 0, f"wall read failed: {result}")
        check(0 <= value[1] < NS, f"unnormalized timespec: {value}")
        return value[0] * NS + value[1]

    def update(self):
        self.lib.clock_update_wall_time()

    def call(self, fields=None, old=True):
        value = Timeval(*fields) if fields is not None else None
        previous = Timeval(*SENTINEL)
        C.set_errno(0)
        result = self.lib.clock_probe_test_adjtime(
            C.byref(value) if value is not None else None,
            C.byref(previous) if old else None)
        return result, C.get_errno(), (previous.seconds, previous.microseconds)

    def adjust(self, microseconds):
        result = self.call(signed_timeval(microseconds))
        check(result[0] == 0, f"adjustment rejected: {result}")
        return result[2]

    def pending(self, nanoseconds):
        result = self.call()
        expected = pending_timeval(nanoseconds)
        check(result[0] == 0 and result[2] == expected,
              f"pending query: {result}, expected {expected}")


def baseline_steps(h):
    h.reset()
    h.advance(250_000_000)
    check(h.read() == BASE + 250_000_000, "baseline clock rate")
    for target in [BASE + 3 * NS, BASE - 2 * NS]:
        value = Timespec(*divmod(target, NS))
        check(h.lib.clock_timekeeping_set_wall_time(C.byref(value)) == 0, "step rejected")
        check(h.read() == target, "step not visible")
        h.advance(250_000_000)
        check(h.read() == target + 250_000_000, "step changed elapsed rate")


def rate_and_phase(h, sign):
    for phase in [0, 499_900_000, 999_950_000]:
        h.reset(BASE + phase)
        h.adjust(sign * 2000)
        for index in range(1, 21):
            h.advance(250_000_000)
            elapsed = index * 250_000_000
            correction = sign * min(elapsed // 2000, 2_000_000)
            expected = BASE + phase + elapsed + correction
            before = h.read()
            check(before == expected, f"phase={phase}, elapsed={elapsed}, error={before - expected}")
            h.update()
            check(h.read() == before, "commit disagrees with preceding projection")
            h.pending(sign * 2_000_000 - correction)


def read_update_interleave(h):
    h.reset(BASE + 999_950_000)
    h.adjust(-2000)
    previous = h.read()
    for index in range(1, 250_000_000 // h.tick_ns + 1):
        h.advance(h.tick_ns)
        before = h.read()
        check(before >= previous, f"read went backward at raw tick {index}")
        h.update()
        after = h.read()
        check(after == before, f"read/update discontinuity at raw tick {index}: {after - before} ns")
        previous = after


def fractional_budget(h, sign):
    h.reset()
    h.adjust(sign * 2)
    for index in range(1, 4_000_000 // h.tick_ns + 1):
        h.advance(h.tick_ns)
        expected = BASE + index * h.tick_ns + sign * min(index * h.tick_ns // 2000, 2000)
        check(h.read() == expected, f"fractional projection at tick {index}")
        h.update()
        check(h.read() == expected, f"fractional commit at tick {index}")
        if index % 37 == 0:
            h.pending(sign * (2000 - min(index * h.tick_ns // 2000, 2000)))
    h.pending(0)


def query_and_normalization(h):
    h.reset()
    h.adjust(2000)
    h.advance(250_000_000)
    h.pending(1_875_000)
    h.pending(1_875_000)
    check(h.call(old=False)[0] == 0, "null/null query rejected")
    h.advance(250_000_000)
    h.pending(1_750_000)
    for amount in [1_500_000, -1_500_000, -1, 1]:
        h.adjust(amount)
        h.pending(amount * 1000)
    check(h.call((-1, 500000))[0] == 0, "mixed-sign valid input rejected")
    h.pending(-500_000_000)


def aliased_replacement(h):
    for amount in [1_234_567, -1_234_567]:
        h.reset()
        h.adjust(2000)
        h.advance(250_000_000)
        value = Timeval(*signed_timeval(amount))
        result = h.lib.clock_probe_test_adjtime(C.byref(value), C.byref(value))
        check(result == 0, "aliased adjustment rejected")
        check((value.seconds, value.microseconds) == (0, 1875), "aliased olddelta was not normalized")
        h.pending(amount * 1000)


def replace(h):
    h.reset()
    h.adjust(20_000)
    h.advance(1_250_000_000)
    before = h.read()
    previous = h.adjust(-2000)
    check(previous == (0, 19_375), f"replacement lost prior pending amount: {previous}")
    check(h.read() == before == BASE + 1_250_625_000, "replacement failed to materialize prior progress")
    h.advance(250_000_000)
    check(h.read() == before + 249_875_000, "replacement used old direction/rate")
    h.pending(-1_875_000)


def cancel(h):
    for sign in [1, -1]:
        h.reset()
        h.adjust(sign * 20_000)
        h.advance(1_250_000_000)
        before = h.read()
        previous = h.adjust(0)
        check(previous == (0, sign * 19_375), f"cancel pending amount: {previous}")
        check(h.read() == before, "cancel changed already-observed time")
        h.advance(3 * NS)
        check(h.read() == before + 3 * NS, "cancel left residual drift")
        h.pending(0)


def input_bounds(h):
    h.reset()
    h.adjust(2000)
    for fields, expected_errno in [((0, 1_000_000), errno.EINVAL),
                                   ((0, -1_000_000), errno.EINVAL),
                                   ((MAX64, 0), errno.EOVERFLOW),
                                   ((MIN64, 0), errno.EOVERFLOW),
                                   (signed_timeval(MAX64 // 1000 + 1), errno.EOVERFLOW),
                                   (signed_timeval(-(MAX64 // 1000 + 1)), errno.EOVERFLOW)]:
        result = h.call(fields)
        check(result == (-1, expected_errno, SENTINEL), f"invalid input {fields}: {result}")
        h.pending(2_000_000)
    for amount in [3_000_000_000, -3_000_000_000, MAX64 // 1000, -(MAX64 // 1000)]:
        h.adjust(amount)
        h.pending(amount * 1000)


def argument_overflow(h):
    h.reset()
    h.adjust(2000)
    result = h.call((MAX64, 0))
    check(result == (-1, errno.EOVERFLOW, SENTINEL), f"huge seconds were not rejected: {result}")
    h.pending(2_000_000)


def timer_failure(h):
    h.reset()
    h.adjust(2000)
    h.advance(NS)
    h.timer(error=errno.EIO)
    check(h.read_result() == (-errno.EIO, SENTINEL), "read failure changed output")
    check(h.call((0, -2000)) == (-1, errno.EIO, SENTINEL), "failed replacement changed output")
    h.update()
    h.timer()
    check(h.read() == BASE + NS + 500_000, "failed timer read changed accumulated state")
    h.pending(1_500_000)


def counter_wrap(h):
    h.reset(counter=65530, mask=65535)
    h.adjust(-2000)
    h.advance(20 * h.tick_ns)
    expected = BASE + 20 * h.tick_ns - 20 * h.tick_ns // 2000
    check(h.read() == expected, "masked counter wrap projection")
    h.update()
    check(h.read() == expected, "masked counter wrap commit")


def setter_cancels(h):
    h.reset()
    h.adjust(2000)
    h.advance(h.tick_ns)
    h.update()
    target = Timespec(100, 123)
    check(h.lib.clock_timekeeping_set_wall_time(C.byref(target)) == 0, "setter failed")
    h.pending(0)
    h.advance(NS)
    check(h.read() == 101 * NS + 123, "setter left a correction or fractional budget")


def fractional_query_and_replacement(h):
    h.reset()
    h.adjust(-1)
    h.advance(h.tick_ns)
    remaining = -(1000 - min(h.tick_ns // 2000, 1000))
    h.pending(remaining)
    check(h.read() == BASE + h.tick_ns - min(h.tick_ns // 2000, 1000), "query changed projection")
    if h.tick_ns == 1000:
        h.adjust(1)
        before = h.read()
        h.advance(1000)
        check(h.read() == before + 1000, "replacement inherited old fractional credit")
        h.update()
        h.advance(1000)
        check(h.read() == before + 2001, "new request lost fractional credit")


def elapsed_overflow(h):
    h.reset()
    h.timer(MASK // h.tick_ns + 1)
    check(h.read_result() == (-errno.EOVERFLOW, SENTINEL), "overflowed elapsed multiplication")
    check(h.call((0, 1)) == (-1, errno.EOVERFLOW, SENTINEL), "overflowed elapsed adjustment")
    h.update()
    h.timer(1)
    check(h.read() == BASE + h.tick_ns, "overflow failure committed invalid epoch")


def epoch_overflow(h):
    h.reset(MAX64 * NS + NS - 1)
    h.advance(h.tick_ns)
    check(h.read_result() == (-errno.EOVERFLOW, SENTINEL), "overflowed wall seconds")
    h.reset(MIN64 * NS)
    h.advance(h.tick_ns)
    check(h.read() == MIN64 * NS + h.tick_ns, "minimum representable epoch")


def public_get_failure(h):
    h.reset()
    h.timer(error=errno.EIO)
    value = Timespec(*SENTINEL)
    check(h.lib.clock_probe_test_nxgettime(0, C.byref(value)) == -errno.EIO,
          "nxclock_gettime discarded software clock error")
    C.set_errno(0)
    check(h.lib.clock_probe_test_gettime(0, C.byref(value)) == -1 and C.get_errno() == errno.EIO,
          "clock_gettime failed to report software clock error")
    check((value.seconds, value.nanoseconds) == SENTINEL, "failed public read modified output")
    h.timer()


def public_set_failure(h):
    h.reset()
    h.timer(error=errno.EIO)
    target = Timespec(100, 123)
    C.set_errno(0)
    result = h.lib.clock_probe_test_settime(0, C.byref(target))
    check(result == -1 and C.get_errno() == errno.EIO, "clock_settime discarded software clock error")
    h.timer()
    check(h.read() == BASE, "failed public setter changed epoch")


def public_set_failure_rtc(h):
    h.reset()
    writes = h.lib.clock_probe_test_rtc_writes()
    h.timer(error=errno.EIO)
    target = Timespec(100, 123)
    h.lib.clock_probe_test_settime(0, C.byref(target))
    check(h.lib.clock_probe_test_rtc_writes() == writes, "failed software setter still updated RTC")
    h.timer()


def public_set_success(h):
    h.reset()
    writes = h.lib.clock_probe_test_rtc_writes()
    target = Timespec(100, 123)
    check(h.lib.clock_probe_test_settime(0, C.byref(target)) == 0, "public setter failed")
    check(h.read() == 100 * NS + 123, "public setter did not change epoch")
    check(h.lib.clock_probe_test_rtc_writes() == writes + 1, "successful setter skipped RTC")


def monotonic_untouched(h):
    h.reset()
    h.advance(NS)
    value = Timespec()
    check(h.lib.clock_probe_test_gettime(1, C.byref(value)) == 0, "monotonic read rejected")
    before = (value.seconds, value.nanoseconds)
    h.adjust(-2000)
    target = Timespec(100, 123)
    h.lib.clock_probe_test_settime(0, C.byref(target))
    check(h.lib.clock_probe_test_gettime(1, C.byref(value)) == 0, "monotonic reread rejected")
    check((value.seconds, value.nanoseconds) == before == (1, 0), "wall controls changed monotonic time")


CASES = {
    "baseline_steps": baseline_steps,
    "positive_rate_and_phase": lambda h: rate_and_phase(h, 1),
    "negative_rate_and_phase": lambda h: rate_and_phase(h, -1),
    "read_update_interleave": read_update_interleave,
    "positive_fractional_budget": lambda h: fractional_budget(h, 1),
    "negative_fractional_budget": lambda h: fractional_budget(h, -1),
    "query_and_normalization": query_and_normalization,
    "aliased_replacement": aliased_replacement,
    "replacement_materializes": replace,
    "cancellation_materializes": cancel,
    "input_bounds": input_bounds,
    "argument_overflow": argument_overflow,
    "timer_failure_atomicity": timer_failure,
    "counter_wrap": counter_wrap,
    "setter_cancels": setter_cancels,
    "fractional_query_and_replacement": fractional_query_and_replacement,
    "elapsed_overflow": elapsed_overflow,
    "epoch_overflow": epoch_overflow,
    "public_get_failure": public_get_failure,
    "public_set_failure": public_set_failure,
    "public_set_failure_rtc": public_set_failure_rtc,
    "public_set_success": public_set_success,
    "monotonic_untouched": monotonic_untouched,
}



def native_case(library, tick_us, name):
    witness = Witness(library, tick_us)
    try:
        if name == "disabled_adjustment":
            check(not hasattr(witness.lib, "clock_probe_test_adjtime"),
                  "disabled adjustment still exports adjtime")
        else:
            CASES[name](witness)
    except AssertionError as error:
        print(f"clock contract failed: {name}: {error}", file=sys.stderr)
        return 1
    print(f"clock contract passed: {name}")
    return 0


class ClockTimekeepingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if C.sizeof(C.c_long) != 8:
            raise unittest.SkipTest("native clock witness requires LP64 host types")
        temporary = tempfile.TemporaryDirectory(prefix="nxrs-clock-timekeeping-")
        cls.addClassCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "source"
        revision = subprocess.check_output(
            ["git", "rev-parse", ":external/nuttx"], cwd=ROOT, text=True,
        ).strip()
        for relative in SOURCES:
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(subprocess.check_output(
                ["git", "-C", str(ROOT / "external/nuttx"), "show", f"{revision}:{relative}"],
            ))
        for name in STUB_HEADERS:
            path = root / "include" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("/* Dependencies supplied by clock_timekeeping.h. */\n")

        series = runpy.run_path(str(ROOT / "tools/apply-nuttx-patches.py"))["SERIES"]["nuttx"]
        if series.count(PATCH) != 1:
            raise AssertionError("tested clock patch must occur once in the firmware series")
        env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(root.parent)}

        def apply(name):
            subprocess.run(
                ["git", "apply", "--whitespace=error", *["--include=" + path for path in SOURCES],
                 str(ROOT / "upstream/nuttx/patches" / name)],
                cwd=source, env=env, check=True, capture_output=True,
            )

        boundary = series.index(PATCH)
        for name in series[:boundary]:
            apply(name)
        cls.libraries = {}
        for label in ("original", "patched"):
            if label == "patched":
                for name in series[boundary:]:
                    apply(name)
            snapshot = root / label
            for relative in SOURCES:
                path = snapshot / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / relative, path)
            variants = [(tick, True) for tick in (1000, 100, 1)]
            if label == "patched":
                variants.append((1000, False))
            for tick, adjustment in variants:
                library = root / f"{label}-{tick}-{adjustment}.so"
                command = [
                    os.environ.get("CC", "cc"), "-std=c11", "-O2", "-g", "-shared", "-fPIC",
                    "-Wall", "-Wextra", "-Werror", "-fsanitize=undefined",
                    "-fno-sanitize-recover=all", "-D_POSIX_C_SOURCE=200809L",
                    f"-DWITNESS_TICK_US={tick}", "-I", str(root / "include"),
                    "-include", str(ROOT / "tests/host/clock_timekeeping.h"),
                    *[str(snapshot / path) for path in SOURCES],
                    str(ROOT / "tests/host/clock_timekeeping.c"), "-o", str(library),
                ]
                if adjustment:
                    command.append("-DCONFIG_CLOCK_ADJTIME=1")
                if label == "original":
                    # The old source passes unsigned pointers to clock_t APIs.
                    # Keep those warnings enabled while testing its behavior.
                    command.append("-Wno-error=pointer-sign")
                built = subprocess.run(command, capture_output=True, text=True)
                if built.returncode:
                    raise AssertionError(built.stdout + built.stderr)
                cls.libraries[label, tick, adjustment] = library

    def invoke(self, label, tick, name, adjustment=True):
        return subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--native-case",
             str(self.libraries[label, tick, adjustment]), str(tick), name],
            capture_output=True, text=True, timeout=15,
        )

    def cases(self, names, original_passes=False):
        for name in names:
            for tick in (1000, 100, 1):
                for label in ("original", "patched"):
                    with self.subTest(case=name, tick_us=tick, source=label):
                        result = self.invoke(label, tick, name)
                        evidence = result.stdout + result.stderr
                        if label == "original" and not original_passes:
                            self.assertEqual(result.returncode, 1, evidence)
                            if name in ("argument_overflow", "epoch_overflow"):
                                self.assertIn("runtime error: signed integer overflow", result.stderr)
                                self.assertIn("clock_timekeeping.c", result.stderr)
                            else:
                                self.assertIn(f"clock contract failed: {name}:", result.stderr)
                        else:
                            self.assertEqual(result.returncode, 0, evidence)
                            self.assertEqual(result.stderr, "", evidence)
                            self.assertEqual(result.stdout.strip(), f"clock contract passed: {name}")

    def test_steps_rtc_success_and_monotonic_clock_are_preserved(self):
        self.cases(("baseline_steps", "public_set_success", "monotonic_untouched"), original_passes=True)

    def test_both_slew_signs_follow_elapsed_time_independent_of_wall_phase(self):
        self.cases(("positive_rate_and_phase", "negative_rate_and_phase"))

    def test_reads_and_committed_updates_cannot_disagree(self):
        self.cases(("read_update_interleave",))

    def test_fractional_budget_survives_frequent_updates(self):
        self.cases(("positive_fractional_budget", "negative_fractional_budget"))

    def test_queries_preserve_progress_and_normalize_both_timeval_fields(self):
        self.cases(("query_and_normalization", "fractional_query_and_replacement", "aliased_replacement"))

    def test_replacement_and_cancellation_materialize_previous_progress(self):
        self.cases(("replacement_materializes", "cancellation_materializes"))

    def test_invalid_and_overflowing_requests_preserve_pending_adjustment(self):
        self.cases(("input_bounds", "argument_overflow"))

    def test_masked_counter_wrap_preserves_elapsed_time(self):
        self.cases(("counter_wrap",))

    def test_setting_wall_time_cancels_all_pending_progress(self):
        self.cases(("setter_cancels",))

    def test_timer_errors_do_not_commit_partial_state(self):
        self.cases(("timer_failure_atomicity",))

    def test_elapsed_and_wall_arithmetic_overflow_are_reported(self):
        self.cases(("elapsed_overflow", "epoch_overflow"))

    def test_public_getters_report_software_errors(self):
        self.cases(("public_get_failure",))

    def test_failed_public_setter_reports_error_and_does_not_write_rtc(self):
        self.cases(("public_set_failure", "public_set_failure_rtc"))

    def test_timekeeping_without_adjustment_keeps_clocks_and_error_reporting(self):
        for name in ("disabled_adjustment", "baseline_steps", "public_get_failure",
                     "public_set_failure", "public_set_failure_rtc", "public_set_success"):
            with self.subTest(case=name):
                result = self.invoke("patched", 1000, name, adjustment=False)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "--native-case":
        raise SystemExit(native_case(Path(sys.argv[2]), int(sys.argv[3]), sys.argv[4]))
    unittest.main()

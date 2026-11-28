"""Time HAL header/configuration translation; all clock calls are renamed fakes.

Host headers exercise native call shape, not NuttX ABI or real clock timing.
The separate NuttX fixture compiles the same helper against target headers.
"""
import ctypes as C
import errno
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "hal/time/src/platform/nuttx/ffi/time.c"
FAKES = r"""
#include <errno.h>
#include <stdint.h>
#include <sys/time.h>
#include <time.h>
int test_error, test_set_calls, test_adjust_calls, test_replace;
int64_t test_seconds, test_previous_seconds;
int32_t test_fraction, test_previous_microseconds;

int time_hal_test_set(clockid_t clock, const struct timespec *value)
{
  ++test_set_calls;
  if (clock != CLOCK_REALTIME)
    return -1;
  test_seconds = value->tv_sec;
  test_fraction = value->tv_nsec;
  errno = test_error;
  return test_error ? -1 : 0;
}

int time_hal_test_adjust(const struct timeval *delta, struct timeval *previous)
{
  ++test_adjust_calls;
  test_replace = delta != NULL;
  if (delta)
    {
      test_seconds = delta->tv_sec;
      test_fraction = delta->tv_usec;
    }
  errno = test_error;
  if (test_error)
    return -1;
  previous->tv_sec = test_previous_seconds;
  previous->tv_usec = test_previous_microseconds;
  return 0;
}
"""


class NativeTimeTests(unittest.TestCase):
    def test_only_noncoupled_software_backend_admits_slew(self):
        cc = shutil.which("cc")
        self.assertIsNotNone(cc, "a host C compiler is required")
        variants = [
            ([], 0, 0),
            (["CLOCK_TIMEKEEPING"], 0, 0),
            (["CLOCK_ADJTIME"], 0, 0),
            (["CLOCK_TIMEKEEPING", "CLOCK_ADJTIME"], 0, 500),
            (["CLOCK_TIMEKEEPING", "CLOCK_ADJTIME", "RTC_HIRES"], 1, 0),
            (["RTC_HIRES"], 1, 0),
        ]
        with tempfile.TemporaryDirectory(prefix="nxrs-time-native-") as temporary:
            directory = Path(temporary)
            (directory / "nuttx").mkdir()
            (directory / "fakes.c").write_text(FAKES)
            for index, (flags, admission, rate) in enumerate(variants):
                with self.subTest(flags=flags):
                    (directory / "nuttx/config.h").write_text("".join(
                        f"#define CONFIG_{flag} 1\n" for flag in flags))
                    library = directory / f"time-{index}.so"
                    subprocess.run([
                        cc, "-std=c11", "-Wall", "-Wextra", "-Werror", "-fPIC", "-shared",
                        "-D_DEFAULT_SOURCE", "-Dclock_settime=time_hal_test_set",
                        "-Dadjtime=time_hal_test_adjust", "-I", str(directory),
                        str(SOURCE), str(directory / "fakes.c"), "-o", str(library),
                    ], check=True, capture_output=True, text=True)
                    self.exercise(C.CDLL(str(library)), admission, rate)

    def exercise(self, native, admission, rate):
        def scalar(name, kind=C.c_int):
            return kind.in_dll(native, "test_" + name)

        native.time_control_capabilities.argtypes = [C.POINTER(C.c_uint32)]
        native.time_set_realtime.argtypes = [C.c_int64, C.c_uint32]
        native.time_adjust_realtime.argtypes = [
            C.c_bool, C.c_int64, C.c_int32, C.POINTER(C.c_int64), C.POINTER(C.c_int32)]
        actual_rate = C.c_uint32(0)
        self.assertEqual(native.time_control_capabilities(C.byref(actual_rate)), admission)
        self.assertEqual(actual_rate.value, rate)
        previous_seconds, previous_microseconds = C.c_int64(47), C.c_int32(53)

        def adjust(replace=True):
            return native.time_adjust_realtime(replace, -2, -123,
                C.byref(previous_seconds), C.byref(previous_microseconds))

        if not rate:
            self.assertEqual(adjust(), 1)
            self.assertEqual(adjust(False), 1)
            self.assertEqual(scalar("adjust_calls").value, 0)
            self.assertEqual((previous_seconds.value, previous_microseconds.value), (47, 53))
        else:
            scalar("previous_seconds", C.c_int64).value = -3
            scalar("previous_microseconds").value = -456
            self.assertEqual(adjust(), 0)
            self.assertEqual(scalar("replace").value, 1)
            self.assertEqual((scalar("seconds", C.c_int64).value, scalar("fraction").value), (-2, -123))
            self.assertEqual((previous_seconds.value, previous_microseconds.value), (-3, -456))
            self.assertEqual(adjust(False), 0)
            self.assertEqual(scalar("replace").value, 0)

        if not admission:
            self.assertEqual(native.time_set_realtime(-2, 876_543_211), 0)
            self.assertEqual((scalar("seconds", C.c_int64).value, scalar("fraction").value), (-2, 876_543_211))
            for error, status in [(errno.ENOSYS, 1), (errno.ENOTSUP, 1),
                                  (errno.EINVAL, 2), (errno.ERANGE, 2),
                                  (errno.EOVERFLOW, 2), (errno.EIO, -1)]:
                scalar("error").value = error
                self.assertEqual(native.time_set_realtime(0, 0), status)
                if rate:
                    previous_seconds.value, previous_microseconds.value = 47, 53
                    self.assertEqual(adjust(), status)
                    self.assertEqual((previous_seconds.value, previous_microseconds.value), (47, 53))


if __name__ == "__main__":
    unittest.main()

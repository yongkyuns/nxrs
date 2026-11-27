"""Execute the production Apps capture patch with bounded native-call oracles.

The new C translation unit is extracted byte-for-byte from the committed patch,
then compiled on Linux. Fake devices/clocks exercise failure paths; QEMU
qualification separately executes the patched builtin against actual NuttX
noteram. These compiled controls do not qualify macOS or Windows native APIs.
"""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
PATCH = ROOT / "upstream/nuttx-apps/patches/0007-trace-capture-window.patch"


def added_file(name):
    lines = PATCH.read_text().splitlines(keepends=True)
    active = False
    result = []
    for line in lines:
        if line.startswith("diff --git "):
            active = line.rstrip() == f"diff --git a/{name} b/{name}"
        elif active and line.startswith("+") and not line.startswith("+++"):
            result.append(line[1:])
    if not result:
        raise AssertionError(f"missing production source {name}")
    return "".join(result)


CONFIG = r"""
#define FAR
#define ERROR (-1)
#define OK 0
#define CONFIG_SYSTEM_TRACE_CAPTURE 1
#define CONFIG_BUILD_FLAT 1
#define CONFIG_DRIVERS_NOTERAM_BUFSIZE 4096
#define CONFIG_SCHED_INSTRUMENTATION_SWITCH 1
#define CONFIG_SCHED_INSTRUMENTATION_IRQHANDLER 1
#define CONFIG_SCHED_INSTRUMENTATION_DUMP 1
#define CONFIG_SCHED_INSTRUMENTATION_HEAP 1
#define CONFIG_SCHED_INSTRUMENTATION_SYSCALL 1
"""

NATIVE = r"""
#ifndef CAPTURE_TEST_NATIVE
#define CAPTURE_TEST_NATIVE
#include <stdint.h>
#include <time.h>
typedef unsigned int irqstate_t;
#define NOTE_FILTER_MODE_FLAG_ENABLE 1u
#define NOTE_FILTER_MODE_FLAG_SWITCH 2u
#define NOTE_FILTER_MODE_FLAG_SYSCALL 4u
#define NOTE_FILTER_MODE_FLAG_IRQ 8u
#define NOTE_FILTER_MODE_FLAG_DUMP 16u
#define NOTE_FILTER_MODE_FLAG_SYSCALL_ARGS 32u
#define NOTECTL_GETMODE 101
#define NOTECTL_SETMODE 102
#define NOTECTL_GETIRQFILTER 103
#define NOTECTL_GETSYSCALLFILTER 104
#define NOTERAM_GETMODE 201
#define NOTERAM_SETMODE 202
#define NOTERAM_CLEAR 203
#define NOTERAM_MODE_OVERWRITE_DISABLE 0u
#define NOTERAM_MODE_OVERWRITE_ENABLE 1u
#define NOTERAM_MODE_OVERWRITE_OVERFLOW 2u
struct note_filter_mode_s { unsigned int flag; };
struct note_filter_named_mode_s
{ char name[32]; struct note_filter_mode_s mode; };
struct note_filter_named_irq_s
{ char name[32]; struct { uint8_t bytes[4]; } irq_mask; };
struct note_filter_named_tag_s
{ char name[32]; struct { uint8_t bytes[4]; } tag_mask; };
struct note_filter_named_syscall_s
{ char name[32]; struct { uint8_t bytes[4]; } syscall_mask; };
#endif
"""

HARNESS = r"""
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <limits.h>
#include <pthread.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <time.h>
#include <unistd.h>
#include <nuttx/config.h>
#include <nuttx/sched_note.h>

static const char *scenario;
static unsigned int flags = 16;
static unsigned int ram_mode = 1;
static unsigned int mask = 0x40;
static unsigned int opens, closes, clears, sleeps, reads, writes, flushes;
static uint64_t perf_ns = 1000000000, mono_ns = 1000000000;
static uint64_t enabled_ns, disabled_ns;
static unsigned long frequency = 1000000;
static bool enabled_once;
static unsigned int critical, mutex_inits, mutex_tries, mutex_repairs;
static unsigned int attributes_destroyed;
static bool mutex_held, mutex_inconsistent, mutex_reset;

static bool is(const char *value) { return strcmp(value, scenario) == 0; }
static irqstate_t enter_critical_section(void) { return critical++; }
static void leave_critical_section(irqstate_t saved)
{ if (critical != saved + 1) abort(); critical = saved; }
static int guard_attr_init(pthread_mutexattr_t *attr)
{ (void)attr; if (!critical) abort(); return is("guard_attr_error") ? EIO : 0; }
static int guard_attr_robust(pthread_mutexattr_t *attr, int robust)
{ (void)attr; if (!critical || robust != PTHREAD_MUTEX_ROBUST) abort();
  return is("guard_robust_error") ? EIO : 0; }
static int guard_attr_destroy(pthread_mutexattr_t *attr)
{ (void)attr; if (!critical) abort(); attributes_destroyed++; return 0; }
static int guard_mutex_init(pthread_mutex_t *mutex, const pthread_mutexattr_t *attr)
{ (void)mutex; (void)attr; if (!critical) abort(); mutex_inits++;
  if (is("guard_init_error") || (is("guard_init_retry") && mutex_inits == 1) ||
      ((is("guard_reinit_error") || is("guard_reinit_retry")) && mutex_inits == 2)) return EIO;
  mutex_held = mutex_inconsistent = mutex_reset = false; return 0; }
static int guard_mutex_trylock(pthread_mutex_t *mutex)
{ (void)mutex; if (!critical) abort(); mutex_tries++;
  if (mutex_inconsistent) return EOWNERDEAD;
  /* Pinned nxsem_trywait accepts NO_MHOLDER but rejects the post-exit RESET
   * sentinel, even though no task owns it. Clearing INCONSISTENT alone cannot
   * make this fast path usable again.
   */
  if (mutex_held || mutex_reset) return EBUSY;
  mutex_held = true; return 0; }
static int guard_mutex_consistent(pthread_mutex_t *mutex)
{ (void)mutex; if (!critical || mutex_held || !mutex_inconsistent) abort();
  mutex_repairs++; if (is("guard_repair_error")) return EIO;
  mutex_inconsistent = false; return 0; }
static int guard_mutex_destroy(pthread_mutex_t *mutex)
{ (void)mutex;
  if (!critical || mutex_held || !mutex_inconsistent || !mutex_reset) abort();
  mutex_repairs++;
  if (is("guard_repair_error") ||
      (is("guard_repair_retry") && mutex_repairs == 1)) return EIO;
  mutex_reset = false; return 0; }
static int guard_mutex_unlock(pthread_mutex_t *mutex)
{ (void)mutex; if (!mutex_held) abort(); mutex_held = false; return 0; }
static size_t strlcpy(char *dest, const char *source, size_t size)
{
  size_t length = strlen(source);
  if (size) { size_t n = length < size - 1 ? length : size - 1;
    memcpy(dest, source, n); dest[n] = 0; }
  return length;
}
static int device_open(const char *path, int mode)
{
  (void)mode;
  if (strcmp(path, "/dev/note/ram")) abort();
  opens++;
  if (is("open_error")) { errno = EIO; return -1; }
  return 8;
}
static int device_close(int fd)
{ if (fd != 8) abort(); closes++; return 0; }

static int device_ioctl(int fd, int command, unsigned long arg)
{
  if (fd != 7 && fd != 8) abort();
  struct note_filter_named_mode_s *mode = (void *)arg;
  switch (command)
    {
      case NOTECTL_GETMODE:
        if (strcmp(mode->name, "ram")) abort();
        if (is("getmode_error")) { errno = EIO; return -1; }
        if (!is("missing_driver")) mode->mode.flag = flags;
        if (is("enable_mismatch") && (flags & 1)) mode->mode.flag ^= 16;
        return 0;
      case NOTECTL_SETMODE:
        if (strcmp(mode->name, "ram")) abort();
        if (is("enable_error") && (mode->mode.flag & 1))
          { errno = EIO; return -1; }
        if (mode->mode.flag & 1)
          { enabled_once = true; perf_ns += 2000000; enabled_ns = perf_ns; }
        else if ((flags & 1) != 0)
          { disabled_ns = perf_ns; perf_ns += 5000000; }
        flags = mode->mode.flag;
        return 0;
      case NOTECTL_GETIRQFILTER:
        if (is("filter_read_error")) { errno = EIO; return -1; }
        ((struct note_filter_named_irq_s *)arg)->irq_mask.bytes[0] = mask;
        return 0;
      case NOTECTL_GETSYSCALLFILTER:
        ((struct note_filter_named_syscall_s *)arg)->syscall_mask.bytes[0] = 4;
        return 0;
      case NOTERAM_GETMODE:
        if (is("ram_getmode_error")) { errno = EIO; return -1; }
        *(unsigned int *)arg = ram_mode;
        if (is("ram_mode_mismatch") && clears) *(unsigned int *)arg = 1;
        return 0;
      case NOTERAM_SETMODE:
        if (is("ram_setmode_error") && *(unsigned int *)arg == 0)
          { errno = EIO; return -1; }
        ram_mode = *(unsigned int *)arg;
        return 0;
      case NOTERAM_CLEAR:
        if (is("clear_error")) { errno = EIO; return -1; }
        clears++;
        return 0;
    }
  abort();
}

static void sched_note_filter_tag(struct note_filter_named_tag_s *old,
                                 const struct note_filter_named_tag_s *new)
{ (void)new; if (strcmp(old->name, "ram")) abort(); old->tag_mask.bytes[0] = 2; }
static unsigned long perf_getfreq(void) { return frequency; }
static clock_t perf_gettime(void) { return (clock_t)(perf_ns / 1000); }
static void perf_convert(clock_t ticks, struct timespec *ts)
{
  ts->tv_sec = ticks / 1000000;
  ts->tv_nsec = (ticks % 1000000) * 1000;
  if (is("bad_conversion")) ts->tv_nsec = 1000000000;
  if (is("conversion_overflow")) ts->tv_sec = INT64_MAX;
}
static int measured_monotonic(clockid_t clock, struct timespec *ts)
{
  if (clock != CLOCK_MONOTONIC) abort();
  if (is("monotonic_error")) { errno = EIO; return -1; }
  ts->tv_sec = mono_ns / 1000000000;
  ts->tv_nsec = mono_ns % 1000000000;
  return 0;
}
static int measured_sleep(const struct timespec *duration, struct timespec *left)
{
  (void)left;
  if (duration->tv_sec != 0 || duration->tv_nsec <= 0 ||
      duration->tv_nsec > 20000000) abort();
  sleeps++;
  mono_ns += duration->tv_nsec;
  if (!is("stuck_clock")) perf_ns += 30000000;
  if (sleeps == 3)
    {
      if (is("rollback")) perf_ns = 1;
      if (is("monotonic_rollback")) mono_ns = 1;
      if (is("frequency_change")) frequency++;
      if (is("filter_change")) mask ^= 2;
      if (is("flags_change")) flags ^= 2;
      if (is("overflow")) ram_mode = 2;
      if (is("interrupted")) { errno = EINTR; return -1; }
      if (is("sleep_error")) { errno = EIO; return -1; }
      if (is("signal")) raise(SIGINT);
      if (is("term_signal")) raise(SIGTERM);
    }
  return 0;
}
static ssize_t device_read(int fd, void *buffer, size_t length)
{
  static const char note[] =
    " worker-7 [0] 1.500000000: tracing_mark_write: I|7|fixture/tick\n";
  if (fd != 8 || (flags & 1) || length < sizeof(note)) abort();
  reads++;
  if (is("read_error")) { errno = EIO; return -1; }
  if (is("large_read")) return length + 1;
  if (is("frequency_change_dump")) frequency++;
  if (is("endless_dump")) { memset(buffer, 'x', length); return length; }
  if (is("empty")) return 0;
  if (reads > 1) return 0;
  memcpy(buffer, note, sizeof(note) - 1);
  return sizeof(note) - 1;
}
static size_t checked_write(const void *data, size_t size, size_t count, FILE *f)
{
  writes++;
  if (is("write_error")) return 0;
  return fwrite(data, size, count, f);
}
static int checked_flush(FILE *f)
{
  flushes++;
  if (is("flush_error") && flushes == 1) { errno = EIO; return EOF; }
  return fflush(f);
}
static int checked_sigaction(int sig, const struct sigaction *action,
                             struct sigaction *old)
{
  if (is("signal_setup_error") && sig == SIGTERM && old)
    { errno = EIO; return -1; }
  return sigaction(sig, action, old);
}

#define open device_open
#define close device_close
#define ioctl device_ioctl
#define clock_gettime measured_monotonic
#define nanosleep measured_sleep
#define read device_read
#define fwrite checked_write
#define fflush checked_flush
#define sigaction(...) checked_sigaction(__VA_ARGS__)
#define pthread_mutexattr_init guard_attr_init
#define pthread_mutexattr_setrobust guard_attr_robust
#define pthread_mutexattr_destroy guard_attr_destroy
#define pthread_mutex_init guard_mutex_init
#define pthread_mutex_trylock guard_mutex_trylock
#define pthread_mutex_consistent guard_mutex_consistent
#define pthread_mutex_destroy guard_mutex_destroy
#define pthread_mutex_unlock guard_mutex_unlock
#include "trace_capture.c"
#undef sigaction

int main(int argc, char **argv)
{
  scenario = argc > 1 ? argv[1] : "normal";
  if (is("already_enabled")) flags |= 1;
  if (is("zero_frequency")) frequency = 0;
  if (is("sentinel_frequency")) frequency = ULONG_MAX;
  if (is("bad_initial_mode")) ram_mode = 99;
  if (is("prior_overflow")) ram_mode = 2;
  char *args[] = {"trace", "capture", argc > 2 ? argv[2] : "1",
                  argc > 3 ? argv[3] : "run-7"};
  int locked = trace_capture_lock();
  if (is("guard_init_retry"))
    { if (locked >= 0) abort(); locked = trace_capture_lock(); }
  if (is("guard_busy"))
    {
      if (locked < 0) abort();
      locked = trace_capture_lock();
      if (locked >= 0) abort();
      trace_capture_unlock();
    }
  if (is("guard_owner_death") || is("guard_repair_error") ||
      is("guard_repair_retry") || is("guard_reinit_error") ||
      is("guard_reinit_retry"))
    {
      if (locked < 0) abort();
      /* Model the pinned exit state, including RESET rather than NO_MHOLDER.
       * The QEMU gate kills an actual owner and inspects the real exit path.
       */
      mutex_held = false;
      mutex_inconsistent = true;
      mutex_reset = true;
      locked = trace_capture_lock();
      if (is("guard_repair_retry") || is("guard_reinit_retry"))
        { if (locked >= 0) abort(); locked = trace_capture_lock(); }
    }
  int result = locked < 0 ? ERROR :
    trace_capture(argc > 4 ? argv[4] : "", 2, 4, args, 7);
  if (locked >= 0 && trace_capture_unlock() < 0) result = ERROR;
  fprintf(stderr, "STATE result=%d flags=%u mode=%u opens=%u closes=%u "
    "clears=%u sleeps=%u reads=%u writes=%u enabled=%u "
    "enable_ns=%" PRIu64 " disable_ns=%" PRIu64 " "
    "critical=%u mutex_inits=%u mutex_tries=%u mutex_repairs=%u "
    "attributes_destroyed=%u mutex_held=%u mutex_reset=%u\n", result, flags,
    ram_mode, opens, closes, clears, sleeps, reads, writes, enabled_once,
    enabled_ns, disabled_ns, critical, mutex_inits, mutex_tries, mutex_repairs,
    attributes_destroyed, mutex_held, mutex_reset);
  return result < 0 ? 1 : 0;
}
"""


class TraceCaptureProducer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sys.platform != "linux":
            raise unittest.SkipTest("compiled native-call controls require Linux")
        cls.compiler = shutil.which(os.environ.get("CC", "cc"))
        if not cls.compiler:
            raise unittest.SkipTest("native C compiler unavailable")
        cls.temp = tempfile.TemporaryDirectory(prefix="nxrs-capture-host-")
        cls.directory = Path(cls.temp.name)
        (cls.directory / "trace_capture.c").write_text(added_file("system/trace/trace_capture.c"))
        (cls.directory / "trace.h").write_text("")
        (cls.directory / "harness.c").write_text(HARNESS)
        for name in ["config.h", "clock.h", "sched_note.h", "irq.h", "note/notectl_driver.h", "note/noteram_driver.h"]:
            path = cls.directory / "nuttx" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(CONFIG if name == "config.h" else NATIVE)
        cls.binaries = {}
        for variant, extra in [("normal", []), ("smp", ["-DCONFIG_SMP=1"]),
                               ("watchdog", ["-DCONFIG_SCHED_INSTRUMENTATION_WDOG=1"]),
                               ("unsafe_mutex", ["-DCONFIG_PTHREAD_MUTEX_UNSAFE=1"]),
                               ("no_pthread", ["-DCONFIG_DISABLE_PTHREAD=1"])]:
            binary = cls.directory / variant
            command = [cls.compiler, "-std=c11", "-Wall", "-Wextra", "-Werror",
                       "-Wno-unused-function", "-I", str(cls.directory), *extra,
                       str(cls.directory / "harness.c"), "-o", str(binary)]
            result = subprocess.run(command, text=True, capture_output=True, timeout=30)
            if result.returncode:
                raise AssertionError(result.stderr)
            cls.binaries[variant] = binary

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def run_capture(self, scenario="normal", duration="1", identifier="run-7", name="", variant="normal"):
        result = subprocess.run([str(self.binaries[variant]), scenario, duration, identifier, name],
                                text=True, capture_output=True, timeout=5)
        state = re.search(r"^STATE (.*)$", result.stderr, re.MULTILINE)
        self.assertIsNotNone(state, result.stderr)
        state = {k: int(v) for k, v in (field.split("=", 1) for field in state[1].split())}
        frames = {}
        for line in result.stdout.splitlines():
            if line.startswith("NXRS_NOTE_"):
                key, *fields = line.split()
                frames[key] = dict(field.split("=", 1) for field in fields)
        return result, state, frames

    def test_real_elapsed_window_preserves_quiet_edges_and_configuration(self):
        result, state, frames = self.run_capture()
        self.assertEqual(result.returncode, 0, result.stderr)
        begin, end = frames["NXRS_NOTE_BEGIN"], frames["NXRS_NOTE_END"]
        self.assertEqual(begin["schema"], "3")
        self.assertEqual(begin["clock"], "nuttx-perf")
        self.assertEqual(int(begin["capture_start_ns"]), state["enable_ns"])
        self.assertEqual(int(begin["capture_end_ns"]), state["disable_ns"])
        self.assertEqual(int(begin["requested_duration_ns"]), 1_000_000_000)
        self.assertEqual(int(begin["capture_end_ns"]) - int(begin["capture_start_ns"]), 1_020_000_000)
        self.assertEqual(begin["categories"], "app,heap")
        self.assertEqual(begin["filter_flags"], "17")
        self.assertEqual(begin["irq_mask"], "40000000")
        self.assertEqual(begin["tag_mask"], "02000000")
        self.assertEqual(begin["syscall_mask"], "04000000")
        self.assertEqual(end["complete"], "1")
        self.assertEqual((state["flags"], state["mode"], state["opens"], state["closes"]), (16, 1, 1, 1))

    def test_empty_ring_keeps_a_complete_measured_window(self):
        result, _, frames = self.run_capture("empty")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(frames["NXRS_NOTE_END"]["complete"], "1")

    def test_bounds_and_recording_identity_rejected_before_mutation(self):
        for duration in ["0", "31", "-1", "+2", "1x", "", "9999999999999999999999"]:
            with self.subTest(duration=duration):
                result, state, frames = self.run_capture(duration=duration)
                self.assertEqual(result.returncode, 1)
                self.assertEqual((state["opens"], state["enabled"]), (0, 0))
                self.assertFalse(frames)
        for identifier in ["", "has space", "bad\nframe", "x" * 129, "\u00e9"]:
            with self.subTest(identifier=identifier):
                result, state, frames = self.run_capture(identifier=identifier)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(state["opens"], 0)
                self.assertFalse(frames)
        result, state, frames = self.run_capture(name="ram")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(state["opens"], 0)
        self.assertFalse(frames)

    def test_unsupported_targets_refuse_without_touching_devices(self):
        for variant in ["smp", "watchdog", "unsafe_mutex", "no_pthread"]:
            with self.subTest(variant=variant):
                result, state, frames = self.run_capture(variant=variant)
                self.assertEqual(result.returncode, 1)
                self.assertEqual((state["opens"], state["enabled"]), (0, 0))
                self.assertFalse(frames)

    def test_guard_rejects_concurrent_owner_without_recorder_mutation(self):
        result, state, frames = self.run_capture("guard_busy")
        self.assertEqual(result.returncode, 1)
        self.assertEqual((state["opens"], state["critical"], state["mutex_held"]), (0, 0, 0))
        self.assertFalse(frames)

    def test_guard_recovers_native_owner_death_then_acquires_again(self):
        result, state, _ = self.run_capture("guard_owner_death")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((state["mutex_inits"], state["mutex_repairs"], state["mutex_tries"]), (2, 1, 3))
        self.assertEqual((state["critical"], state["mutex_held"], state["mutex_reset"]), (0, 0, 0))

    def test_guard_recovery_failure_is_retryable_without_a_stale_owner(self):
        for scenario, expected in [("guard_repair_retry", (2, 2, 4)),
                                   ("guard_reinit_retry", (3, 1, 3))]:
            with self.subTest(scenario=scenario):
                result, state, _ = self.run_capture(scenario)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((state["mutex_inits"], state["mutex_repairs"], state["mutex_tries"]), expected)
                self.assertEqual((state["critical"], state["mutex_held"], state["mutex_reset"]), (0, 0, 0))

    def test_guard_initialization_and_recovery_fail_closed(self):
        for scenario in ["guard_attr_error", "guard_robust_error", "guard_init_error",
                         "guard_repair_error", "guard_reinit_error"]:
            with self.subTest(scenario=scenario):
                result, state, frames = self.run_capture(scenario)
                self.assertEqual(result.returncode, 1)
                self.assertEqual((state["opens"], state["critical"], state["mutex_held"]), (0, 0, 0))
                self.assertFalse(frames)
                expected = 0 if scenario == "guard_attr_error" else (2 if scenario == "guard_reinit_error" else 1)
                self.assertEqual(state["attributes_destroyed"], expected)
        result, state, _ = self.run_capture("guard_init_retry")
        self.assertEqual(result.returncode, 0)
        self.assertEqual((state["mutex_inits"], state["attributes_destroyed"]), (2, 2))

    def test_clock_change_during_text_conversion_prevents_success_envelope(self):
        result, state, frames = self.run_capture("frequency_change_dump")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("NXRS_NOTE_END", frames)
        self.assertEqual((state["flags"], state["mode"], state["mutex_held"]), (16, 1, 0))

    def test_existing_owner_and_unavailable_clock_refused_before_open(self):
        for scenario in ["already_enabled", "missing_driver", "getmode_error", "filter_read_error",
                         "zero_frequency", "sentinel_frequency", "bad_conversion", "conversion_overflow",
                         "monotonic_error"]:
            with self.subTest(scenario=scenario):
                result, state, frames = self.run_capture(scenario)
                self.assertEqual(result.returncode, 1)
                self.assertEqual((state["opens"], state["enabled"]), (0, 0))
                self.assertEqual(state["flags"], 17 if scenario == "already_enabled" else 16)
                self.assertFalse(frames)

    def test_partial_setup_failures_leave_stopped_configuration(self):
        for scenario in ["open_error", "ram_getmode_error", "bad_initial_mode", "ram_setmode_error",
                         "clear_error", "ram_mode_mismatch", "signal_setup_error", "enable_error", "enable_mismatch"]:
            with self.subTest(scenario=scenario):
                result, state, frames = self.run_capture(scenario)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(state["flags"], 16)
                self.assertEqual(state["closes"], 0 if scenario == "open_error" else 1)
                self.assertFalse(frames)

    def test_clock_and_filter_changes_abort_and_disable(self):
        for scenario in ["stuck_clock", "rollback", "monotonic_rollback", "frequency_change",
                         "filter_change", "flags_change", "sleep_error"]:
            with self.subTest(scenario=scenario):
                result, state, frames = self.run_capture(scenario)
                self.assertEqual(result.returncode, 1)
                self.assertEqual((state["flags"], state["mode"], state["closes"]), (16, 1, 1))
                self.assertLessEqual(state["sleeps"], 202)
                self.assertFalse(frames)

    def test_overflow_records_loss_despite_complete_dump(self):
        result, state, frames = self.run_capture("overflow")
        self.assertEqual(result.returncode, 1)
        for key in ["NXRS_NOTE_BEGIN", "NXRS_NOTE_END"]:
            self.assertEqual(frames[key]["mode"], "2")
            self.assertEqual(frames[key]["notes_lost"], "1")
        self.assertEqual(frames["NXRS_NOTE_END"]["complete"], "1")
        self.assertEqual((state["flags"], state["mode"]), (16, 1))

    def test_prior_overflow_restores_no_overwrite_preference(self):
        result, state, _ = self.run_capture("prior_overflow")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(state["mode"], 0)

    def test_interrupted_window_has_actual_shorter_bounds(self):
        for scenario in ["interrupted", "signal", "term_signal"]:
            with self.subTest(scenario=scenario):
                result, state, frames = self.run_capture(scenario)
                self.assertEqual(result.returncode, 1)
                begin = frames["NXRS_NOTE_BEGIN"]
                self.assertEqual(begin["stop_reason"], "interrupted")
                self.assertLess(int(begin["capture_end_ns"]) - int(begin["capture_start_ns"]),
                                int(begin["requested_duration_ns"]))
                self.assertEqual(frames["NXRS_NOTE_END"]["complete"], "1")
                self.assertEqual((state["flags"], state["mode"]), (16, 1))

    def test_read_output_and_budget_failures_emit_incomplete_dump(self):
        for scenario in ["read_error", "large_read", "write_error", "flush_error", "endless_dump"]:
            with self.subTest(scenario=scenario):
                result, state, frames = self.run_capture(scenario)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(frames["NXRS_NOTE_END"]["complete"], "0")
                self.assertLessEqual(state["reads"], 69)
                self.assertEqual((state["flags"], state["mode"], state["closes"]), (16, 1, 1))


class TraceCapturePatch(unittest.TestCase):
    def test_patch_is_registered_as_a_product_input(self):
        manifest = json.loads((ROOT / "upstream/build-patches.json").read_text())
        self.assertEqual(manifest["nuttx-apps"]["patches"].count(PATCH.name), 1)


if __name__ == "__main__":
    unittest.main()

"""Run the pinned NuttX arc4random implementation against injected read failures."""
import hashlib
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


def preimage(text, source, digest):
    start = text.index("--- a/" + source + "\n")
    lines = text[start:].splitlines()
    lines = lines[2:]
    original = []
    for line in lines:
        if line.startswith("diff --git "):
            break
        if line.startswith("@@"):
            if original or not re.match(r"@@ -1(?:,\d+)? ", line):
                raise AssertionError("libc fixture must contain its full, single-hunk preimage")
        elif line.startswith((" ", "-")):
            original.append(line[1:])
    value = "\n".join(original) + "\n"
    if hashlib.sha256(value.encode()).hexdigest() != digest:
        raise AssertionError("libc preimage does not match pinned NuttX source")
    return value


ARC_SOURCE = "libs/libc/stdlib/lib_arc4random.c"
ARC_PATCH = ROOT / "upstream/nuttx/patches/0091-arc4random-fail-closed.patch"
ARC_BEFORE = "117117ae5b99c6a949a9e02f93fd20d2e5490e5f575db1aee27c2c49acc64ced"
ARC_STUBS = {
    "nuttx/config.h": "#define FAR\n#ifndef NO_RNG\n#define CONFIG_DEV_RANDOM 1\n#define CONFIG_DEV_URANDOM 1\n#endif\n",
    "errno.h": "#include_next <errno.h>\n#define get_errno() errno\n#define set_errno(value) (errno = (value))\n",
    "sys/random.h": "#include <sys/types.h>\n#define GRND_RANDOM 1\nssize_t test_getrandom(void *, size_t, unsigned int);\n",
}
ARC_HARNESS = r"""
#include "nuttx/config.h"
#include <assert.h>
#include <errno.h>
#include <setjmp.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
static unsigned calls, primary_calls, secondary_calls, mode, expected_abort;
static jmp_buf stopped;
static unsigned char *output;
static size_t output_size;
static void test_abort(void) __attribute__((noreturn));
#define abort test_abort
#define getrandom test_getrandom
#define arc4random_buf tested_arc4random_buf
#define arc4random tested_arc4random
#include "libs/libc/stdlib/lib_arc4random.c"
#undef abort

static void test_abort(void)
{
  assert(expected_abort);
  for (size_t i = 0; i < output_size; i++) assert(output[i] == 0);
  longjmp(stopped, 1);
}
ssize_t test_getrandom(void *bytes, size_t length, unsigned int flags)
{
  assert(++calls <= 1000);
  unsigned local = flags == GRND_RANDOM ? ++primary_calls : ++secondary_calls;
  if (mode == 1 || (mode == 2 && flags != GRND_RANDOM))
    {
      memset(bytes, 0x6c, length); return length;
    }
  if (mode == 3) return 0;
  if (mode == 4) { errno = EINTR; return -1; }
  if (mode == 5 && flags == GRND_RANDOM && local == 1)
    { memset(bytes, 0x6c, 1); return 1; }
  if (mode == 6) return (ssize_t)(length + 1);
  if (mode == 7 && flags == GRND_RANDOM)
    {
      if (local % 8 != 0) { errno = EINTR; return -1; }
      memset(bytes, 0x6c, 1); return 1;
    }
  errno = EIO; return -1;
}
int main(int argc, char **argv)
{
  assert(argc == 2);
  unsigned char bytes[32]; memset(bytes, 0x5a, sizeof(bytes));
  output = bytes; output_size = sizeof(bytes);
  if (strcmp(argv[1], "zero-size") == 0)
    {
      tested_arc4random_buf(NULL, 0); assert(calls == 0); return 0;
    }
  if (strcmp(argv[1], "success") == 0) mode = 1;
  else if (strcmp(argv[1], "secondary") == 0) mode = 2;
  else if (strcmp(argv[1], "zero-progress") == 0) mode = 3;
  else if (strcmp(argv[1], "interrupts") == 0) mode = 4;
  else if (strcmp(argv[1], "partial-failure") == 0) mode = 5;
  else if (strcmp(argv[1], "oversize-result") == 0) mode = 6;
  else if (strcmp(argv[1], "interrupt-progress") == 0) mode = 7;
  else if (strcmp(argv[1], "absent") == 0) mode = 0;
  else abort();
  expected_abort = !(mode == 1 || mode == 2 || mode == 7);
#ifdef NO_RNG
  expected_abort = 1;
#endif
  if (setjmp(stopped) == 0)
    {
      tested_arc4random_buf(bytes, sizeof(bytes));
      assert(!expected_abort);
      for (size_t i = 0; i < sizeof(bytes); i++) assert(bytes[i] == 0x6c);
      if (mode == 1) assert(calls == 1 && secondary_calls == 0);
      if (mode == 2) assert(calls == 2 && primary_calls == 1);
      if (mode == 7) assert(calls == 8 * sizeof(bytes) && secondary_calls == 0);
    }
  else
    {
      assert(expected_abort);
      if (mode == 4) assert(calls == 16);
      if (mode == 3 || mode == 6) assert(calls == 2);
#ifdef NO_RNG
      assert(calls == 0);
#endif
    }
  return 0;
}
"""


class Arc4RandomTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which("cc") or shutil.which("clang")
        if compiler is None:
            raise unittest.SkipTest("host C compiler required")
        cls.temporary = tempfile.TemporaryDirectory(prefix="nxrs-arc4random-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        series = runpy.run_path(str(ROOT / 'tools/apply-nuttx-patches.py'))['SERIES']['nuttx']
        if ARC_PATCH.name not in series:
            raise AssertionError('tested patch is not registered in the firmware build')
        path = cls.root / ARC_SOURCE
        path.parent.mkdir(parents=True)
        path.write_text(preimage(ARC_PATCH.read_text(), ARC_SOURCE, ARC_BEFORE))
        env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(cls.root.parent)}
        subprocess.run(["git", "apply", "--include=" + ARC_SOURCE, str(ARC_PATCH)],
                       cwd=cls.root, env=env, check=True, capture_output=True)
        for name, content in ARC_STUBS.items():
            path = cls.root / "include" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        (cls.root / "arc-test.c").write_text(ARC_HARNESS)
        cls.binaries = []
        for unconfigured in (False, True):
            binary = cls.root / ("arc-absent" if unconfigured else "arc-test")
            command = [compiler, "-std=c11", "-O2", "-Wall", "-Wextra", "-fsanitize=address,undefined",
                       "-fno-sanitize-recover=all", "-fno-omit-frame-pointer",
                       "-I", str(cls.root / "include"), "-I", str(cls.root),
                       str(cls.root / "arc-test.c"), "-o", str(binary)]
            if unconfigured:
                command += ["-DNO_RNG"]
            subprocess.run(command, check=True, capture_output=True, text=True)
            cls.binaries.append(binary)

    def run_cases(self, *names, unconfigured=False):
        for name in names:
            with self.subTest(name=name):
                result = subprocess.run([str(self.binaries[int(unconfigured)]), name], timeout=5,
                                        capture_output=True, text=True,
                                        env={**os.environ, "ASAN_OPTIONS": "detect_leaks=0:abort_on_error=1"})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_fill_and_secondary_source_fallback_still_work(self):
        self.run_cases("success", "secondary", "interrupt-progress", "zero-size")

    def test_failure_cannot_return_clock_data_or_partial_entropy(self):
        self.run_cases("absent", "zero-progress", "interrupts", "partial-failure", "oversize-result")

    def test_unconfigured_source_fails_closed_but_empty_request_succeeds(self):
        self.run_cases("absent", "zero-size", unconfigured=True)


if __name__ == "__main__":
    unittest.main()

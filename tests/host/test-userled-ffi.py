#!/usr/bin/env python3
"""Host-only behavioral test of the actual USERLED shim with fake headers.

This does NOT qualify NuttX headers, target ABI, driver behavior or hardware.
No production source is rewritten. The fake ioctl checks requests and payloads.
"""
from pathlib import Path
import os
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
HEADERS = {
    "nuttx/config.h": "/* Deliberately fake host test configuration. */\n",
    "sys/ioctl.h": "int ioctl(int fd, int request, ...);\n",
    "nuttx/leds/userled.h": """
#include <stdint.h>
#include <stdbool.h>
#define ULEDIOC_SUPPORTED 0x5101
#define ULEDIOC_SETLED 0x5102
typedef uint32_t userled_set_t;
struct userled_s { uint8_t ul_led; bool ul_on; };
""",
}
HARNESS = r"""
#include <assert.h>
#include <errno.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <nuttx/leds/userled.h>
#include <sys/ioctl.h>

int nxrs_userled_supported(int, uint32_t *);
int nxrs_userled_set(int, uint8_t, int);
static int failure;
static unsigned calls;
static int last_request;
static uint8_t last_index;
static int last_on;

int ioctl(int fd, int request, ...)
{
  assert(fd == 42);
  ++calls;
  last_request = request;
  va_list args;
  va_start(args, request);
  unsigned long argument = va_arg(args, unsigned long);
  va_end(args);
  assert(argument != 0);
  if (failure)
    {
      errno = failure;
      return -1;
    }
  if (request == ULEDIOC_SUPPORTED)
    {
      *(userled_set_t *)(uintptr_t)argument = UINT32_C(0x80000005);
    }
  else
    {
      assert(request == ULEDIOC_SETLED);
      const struct userled_s *p = (const void *)(uintptr_t)argument;
      last_index = p->ul_led;
      last_on = p->ul_on;
    }
  return 0;
}

int main(void)
{
  uint32_t mask = 0;
  errno = EIO; /* Stale errno must not make successful calls fail. */
  assert(nxrs_userled_supported(42, &mask) == 0);
  assert(mask == UINT32_C(0x80000005));
  assert(last_request == ULEDIOC_SUPPORTED);
  assert(nxrs_userled_set(42, 31, 1) == 0);
  assert(last_request == ULEDIOC_SETLED && last_index == 31 && last_on);
  assert(nxrs_userled_set(42, 2, 0) == 0);
  assert(last_index == 2 && !last_on);
  failure = EIO;
  mask = UINT32_C(0x12345678);
  assert(nxrs_userled_supported(42, &mask) == EIO);
  assert(mask == UINT32_C(0x12345678)); /* Do not publish failed output. */
  failure = EAGAIN;
  assert(nxrs_userled_set(42, 0, 1) == EAGAIN);
  assert(calls == 5); /* No hidden retry loop. */
  puts("PASS: USERLED shim host request/payload/error tests (fake headers)");
  return 0;
}
"""


def main():
    with tempfile.TemporaryDirectory(prefix="nxrs-userled-ffi-") as folder:
        work = Path(folder)
        for name, text in HEADERS.items():
            path = work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        source = work / "harness.c"
        source.write_text(HARNESS)
        executable = work / "check"
        compiler = shlex.split(os.environ.get("CC", "cc"))
        subprocess.run(compiler + ["-std=c11", "-Wall", "-Wextra", "-Werror",
                       "-I", str(work), str(source),
                       str(ROOT / "hal/led/src/providers/nuttx/ffi/nxrs_userled.c"),
                       "-o", str(executable)], check=True, timeout=60)
        subprocess.run([str(executable)], check=True, timeout=10)


if __name__ == "__main__":
    main()

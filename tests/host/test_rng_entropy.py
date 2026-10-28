"""Execute the patched NuttX TRNG driver with injected registers, never hardware."""
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
SOURCE = "arch/arm/src/rp23xx/rp23xx_rng.c"
PATCH = ROOT / "upstream/nuttx/patches/0090-rp23xx-rng-bounded-failure.patch"
BEFORE_SHA256 = "7abab16b0ecee39c3d0642c8320788cb25262ae13e664e4de5fa27737a6b0117"

STUBS = {
    "nuttx/config.h": "#define CONFIG_DEV_RANDOM 1\n#define CONFIG_DEV_URANDOM_ARCH 1\n#define CONFIG_RP23XX_RNG_POLL_TIMEOUT 1000\n#define OK 0\n",
    "nuttx/fs/fs.h": r"""
#include <sys/types.h>
struct file { int f_oflags; };
struct file_operations {
 void *open; void *close;
 ssize_t (*read)(struct file *, char *, size_t);
 void *write; void *seek; void *ioctl;
};
int register_driver(const char *, const struct file_operations *, unsigned, void *);
""",
    "nuttx/mutex.h": "typedef int mutex_t;\n#define NXMUTEX_INITIALIZER 0\nint nxmutex_lock(mutex_t *);\nint nxmutex_unlock(mutex_t *);\nint nxmutex_trylock(mutex_t *);\n",
    "nuttx/drivers/drivers.h": "",
    "arm_internal.h": "#include <stdint.h>\nuint32_t getreg32(uintptr_t);\nvoid putreg32(uint32_t, uintptr_t);\nvoid up_udelay(unsigned int);\n",
    "hardware/rp23xx_trng.h": r"""
#define RP23XX_TRNG_RNG_ISR 0x104
#define RP23XX_TRNG_RNG_ICR 0x108
#define RP23XX_TRNG_RND_SOURCE_ENABLE 0x12c
#define RP23XX_TRNG_EHR_DATA(n) (0x114 + (n) * 4)
#define RP23XX_TRNG_RNG_ISR_VN_ERR (1u << 3)
#define RP23XX_TRNG_RNG_ISR_CRNGT_ERR (1u << 2)
#define RP23XX_TRNG_RNG_ISR_AUTOCORR_ERR (1u << 1)
#define RP23XX_TRNG_RNG_ISR_EHR_VALID (1u << 0)
""",
}

HARNESS = r"""
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "arch/arm/src/rp23xx/rp23xx_rng.c"

enum mode { NORMAL, STALLED, HEALTH, TRANSIENT, PARTIAL, REPEATED, DELAYED };
static enum mode behavior;
static struct file device;
static unsigned blocking_locks, try_locks;
static unsigned status_reads, word_reads, blocks, waits, enabled, enables, disables;
static unsigned registrations, lock_held, bad_lock, error_bit;

int nxmutex_lock(mutex_t *mutex)
{
  (void)mutex; blocking_locks++;
  if (bad_lock) return -EBUSY;
  assert(!lock_held); lock_held = 1; return 0;
}
int nxmutex_trylock(mutex_t *mutex)
{
  (void)mutex; try_locks++;
  if (bad_lock) return -EAGAIN;
  assert(!lock_held); lock_held = 1; return 0;
}
int nxmutex_unlock(mutex_t *mutex)
{ (void)mutex; assert(lock_held); lock_held = 0; return 0; }
void up_udelay(unsigned int us)
{ assert(us == 10); waits++; assert(waits <= 400); }
uint32_t getreg32(uintptr_t address)
{
  assert(enabled);
  if (address == RP23XX_TRNG_RNG_ISR)
    {
      status_reads++;
      assert(status_reads <= 400);
      if (behavior == STALLED) return 0;
      if (behavior == HEALTH || (behavior == PARTIAL && blocks > 0))
        return error_bit | RP23XX_TRNG_RNG_ISR_EHR_VALID;
      if (behavior == TRANSIENT && status_reads == 1)
        return error_bit | RP23XX_TRNG_RNG_ISR_EHR_VALID;
      if (behavior == DELAYED && status_reads < 100) return 0;
      return RP23XX_TRNG_RNG_ISR_EHR_VALID;
    }
  assert(address >= RP23XX_TRNG_EHR_DATA(0) && address <= RP23XX_TRNG_EHR_DATA(5));
  unsigned index = (unsigned)(address - RP23XX_TRNG_EHR_DATA(0)) / 4;
  assert(index == word_reads % 6);
  uint32_t word = 0x10203040u + index + (behavior == REPEATED ? 0 : blocks * 17);
  word_reads++;
  if (index == 5) blocks++;
  return word;
}
void putreg32(uint32_t value, uintptr_t address)
{
  if (address == RP23XX_TRNG_RND_SOURCE_ENABLE)
    {
      assert(value <= 1); enabled = value;
      if (value) enables++; else disables++;
    }
  else { assert(address == RP23XX_TRNG_RNG_ICR); assert(value == 0xffffffffu); }
}
int register_driver(const char *path, const struct file_operations *ops, unsigned mode, void *priv)
{
  assert(strcmp(path, "/dev/random") == 0 || strcmp(path, "/dev/urandom") == 0);
  assert(ops->read == rp23xx_rng_read && mode == 0444 && priv == NULL);
  registrations++;
  return 0;
}
static void reset(enum mode next, int healthy)
{
  device.f_oflags = 0; blocking_locks = try_locks = 0;
  behavior = next; status_reads = word_reads = blocks = waits = enabled = 0;
  enables = disables = registrations = lock_held = bad_lock = 0;
  error_bit = RP23XX_TRNG_RNG_ISR_VN_ERR;
  g_rng_healthy = healthy; g_rng_inited = false;
}
static void clean(void) { assert(!enabled && !lock_held); }
static void sticky(void)
{
  char bytes[24];
  unsigned before = status_reads;
  assert(!g_rng_healthy);
  behavior = NORMAL;
  assert(rp23xx_rng_read(&device, bytes, sizeof(bytes)) == -EIO);
  assert(status_reads == before); clean();
}
int main(int argc, char **argv)
{
  assert(argc == 2);
  char output[49]; memset(output, 0x5a, sizeof(output));
  if (strcmp(argv[1], "normal") == 0)
    {
      reset(NORMAL, 1);
      assert(rp23xx_rng_read(&device, output, sizeof(output)) == sizeof(output));
      assert(blocks == 3 && word_reads == 18 && waits == 0 && g_rng_healthy);
      assert(output[0] == 0x40 && output[24] == 0x51 && output[48] == 0x62);
      clean();
    }
  else if (strcmp(argv[1], "stalled") == 0)
    {
      reset(STALLED, 1);
      assert(rp23xx_rng_read(&device, output, sizeof(output)) == -ETIMEDOUT);
      assert(status_reads == 100 && waits == 100 && word_reads == 0);
      for (unsigned i = 0; i < sizeof(output); i++) assert(output[i] == 0);
      clean(); sticky();
    }
  else if (strcmp(argv[1], "health") == 0)
    {
      for (unsigned bit = 1u << 1; bit <= 1u << 3; bit <<= 1)
        {
          reset(HEALTH, 1); error_bit = bit;
          assert(rp23xx_rng_read(&device, output, sizeof(output)) == -EIO);
          assert(status_reads == 8 && waits == 7 && word_reads == 0);
          assert(enables == 9 && disables == 9);
          clean(); sticky();
        }
    }
  else if (strcmp(argv[1], "transient") == 0)
    {
      reset(TRANSIENT, 1);
      assert(rp23xx_rng_read(&device, output, 24) == 24);
      assert(status_reads == 2 && blocks == 1 && word_reads == 6 && waits == 1);
      assert(g_rng_healthy); clean();
    }
  else if (strcmp(argv[1], "partial") == 0)
    {
      reset(PARTIAL, 1);
      assert(rp23xx_rng_read(&device, output, sizeof(output)) == -EIO);
      assert(blocks == 1 && word_reads == 6);
      for (unsigned i = 0; i < sizeof(output); i++) assert(output[i] == 0);
      clean(); sticky();
    }
  else if (strcmp(argv[1], "startup-stalled") == 0)
    {
      reset(STALLED, 0); devrandom_register();
      assert(registrations == 1 && g_rng_inited && !g_rng_healthy);
      assert(status_reads == 100 && word_reads == 0);
      clean(); sticky();
      devurandom_register(); assert(status_reads == 100 && registrations == 2);
    }
  else if (strcmp(argv[1], "startup-health") == 0)
    {
      reset(HEALTH, 0); devurandom_register();
      assert(registrations == 1 && g_rng_inited && !g_rng_healthy);
      assert(status_reads == 8 && word_reads == 0); clean(); sticky();
    }
  else if (strcmp(argv[1], "startup-repeated") == 0)
    {
      reset(REPEATED, 0); devrandom_register();
      assert(blocks == 2 && !g_rng_healthy && g_rng_inited);
      clean(); sticky();
    }
  else if (strcmp(argv[1], "startup-normal") == 0)
    {
      reset(NORMAL, 0); devrandom_register();
      assert(blocks == 4 && g_rng_healthy && g_rng_inited && registrations == 1);
      clean();
      devurandom_register(); assert(blocks == 4 && registrations == 2);
      assert(rp23xx_rng_read(&device, output, sizeof(output)) == sizeof(output));
      clean();
    }
  else if (strcmp(argv[1], "mutex") == 0)
    {
      reset(NORMAL, 1); bad_lock = 1;
      assert(rp23xx_rng_read(&device, output, sizeof(output)) == -EBUSY);
      assert(status_reads == 0 && word_reads == 0 && output[0] == 0x5a);
      clean();
    }
  else if (strcmp(argv[1], "nonblocking") == 0)
    {
      reset(NORMAL, 1); bad_lock = 1; device.f_oflags = O_NONBLOCK;
      assert(rp23xx_rng_read(&device, output, sizeof(output)) == -EAGAIN);
      assert(blocking_locks == 0 && try_locks == 1 && enables == 0 && status_reads == 0);
      assert(g_rng_healthy); clean();
      bad_lock = 0;
      assert(rp23xx_rng_read(&device, output, 24) == 24);
      assert(blocking_locks == 0 && try_locks == 2 && blocks == 1); clean();
    }
  else if (strcmp(argv[1], "delayed") == 0)
    {
      reset(DELAYED, 1);
      assert(rp23xx_rng_read(&device, output, 24) == 24);
      assert(status_reads == 100 && waits == 99 && blocks == 1 && g_rng_healthy);
      clean();
    }
  else abort();
  return 0;
}
"""


def preimage(text, source=SOURCE, digest=BEFORE_SHA256):
    start = text.index("--- a/" + source + "\n")
    lines = text[start:].splitlines()
    lines = lines[2:]
    original = []
    for line in lines:
        if line.startswith("diff --git "):
            break
        if line.startswith("@@"):
            if original or not re.match(r"@@ -1(?:,\d+)? ", line):
                raise AssertionError("driver fixture must contain its full, single-hunk preimage")
        elif line.startswith((" ", "-")):
            original.append(line[1:])
    value = "\n".join(original) + "\n"
    if hashlib.sha256(value.encode()).hexdigest() != digest:
        raise AssertionError("driver preimage does not match pinned NuttX source")
    return value


class RngEntropyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which("cc") or shutil.which("clang")
        if compiler is None:
            raise unittest.SkipTest("host C compiler required")
        cls.temporary = tempfile.TemporaryDirectory(prefix="nxrs-rng-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        series = runpy.run_path(str(ROOT / "tools/apply-nuttx-patches.py"))["SERIES"]["nuttx"]
        if PATCH.name not in series:
            raise AssertionError("tested RNG patch is not registered in the firmware build")
        path = cls.root / SOURCE
        path.parent.mkdir(parents=True)
        path.write_text(preimage(PATCH.read_text()))
        env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(cls.root.parent)}
        subprocess.run(["git", "apply", "--check", "--include=" + SOURCE, str(PATCH)],
                       cwd=cls.root, env=env, check=True, capture_output=True)
        subprocess.run(["git", "apply", "--include=" + SOURCE, str(PATCH)],
                       cwd=cls.root, env=env, check=True, capture_output=True)
        for name, content in STUBS.items():
            path = cls.root / "include" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        (cls.root / "driver-test.c").write_text(HARNESS)
        cls.binary = cls.root / "driver-test"
        subprocess.run([compiler, "-std=c11", "-O2", "-Wall", "-Wextra", "-Wno-unused-parameter",
                        "-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-fno-omit-frame-pointer",
                        "-I", str(cls.root / "include"), "-I", str(cls.root),
                        str(cls.root / "driver-test.c"), "-o", str(cls.binary)],
                       check=True, capture_output=True, text=True)

    def test_normal_partial_words_and_startup(self):
        self.run_cases("normal", "startup-normal", "delayed")

    def test_failed_source_retires_with_bounded_polling(self):
        self.run_cases("stalled", "health", "partial", "transient")

    def test_startup_failure_is_sticky_and_quiescent(self):
        self.run_cases("startup-stalled", "startup-health", "startup-repeated", "mutex", "nonblocking")

    def run_cases(self, *names):
        for name in names:
            with self.subTest(name=name):
                result = subprocess.run([str(self.binary), name], timeout=5, capture_output=True,
                                        text=True, env={**os.environ, "ASAN_OPTIONS": "detect_leaks=0:abort_on_error=1"})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

if __name__ == "__main__":
    unittest.main()

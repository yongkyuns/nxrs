"""Compile the pinned ES7210 startup routines with fault-injected native calls.

Use a pristine NuttX tree, not a build tree with the Nxrs patches already applied.
The dedicated CI job requires the complete pinned source. Other host-only runs
may skip when the optional submodule is absent. These are behavioral witnesses,
not evidence of target ABI, physical audio, or DMA/stop correctness.
"""
from pathlib import Path
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / "upstream/nuttx/patches/0125-es7210-startup-errors.patch"
PINNED_BLOB = "10591f85038df00227d9415830a58de7d676d891"

HARNESS = r'''
#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <errno.h>
#include <sys/types.h>
#define FAR
#define OK 0
#define CHECK(x) do { if (!(x)) { fprintf(stderr, "check failed: %s at %d\n", #x, __LINE__); exit(1); } } while (0)
#define audinfo(...) ((void)0)
#define auderr(...) ((void)0)
#define AUDIOIOC_START 1
struct audio_lowerhalf_s { int unused; };
struct i2c_master_s { int unused; };
struct i2s_dev_s { int unused; };
struct i2c_msg_s {
  uint32_t frequency;
  uint16_t addr, flags;
  uint8_t *buffer;
  ssize_t length;
};
struct es7210_dev_s {
  struct audio_lowerhalf_s dev;
  struct i2c_master_s *i2c;
  struct i2s_dev_s *i2s;
  struct { uint32_t frequency; uint8_t address; } lower;
  uint32_t samprate;
  uint8_t nchannels, bpsamp;
  volatile bool running;
  volatile int result;
};
static const uint8_t expected[][2] = {
  {0x00,0xff}, {0x00,0x32}, {0x09,0x30}, {0x0a,0x30},
  {0x23,0x2a}, {0x22,0x0a}, {0x21,0x2a}, {0x20,0x0a},
  {0x11,0x60}, {0x12,0x00}, {0x40,0xc3}, {0x41,0x70},
  {0x42,0x70}, {0x43,0x1a}, {0x44,0x1a}, {0x45,0x1a},
  {0x46,0x1a}, {0x47,0x08}, {0x48,0x08}, {0x49,0x08},
  {0x4a,0x08}, {0x07,0x20}, {0x02,0x81}, {0x04,0x01},
  {0x05,0x00}, {0x06,0x04}, {0x4b,0x0f}, {0x4c,0x0f},
  {0x00,0x71}, {0x00,0x41}
};
static int fail_at, failure, success, writes, i2s_calls, submissions, delay;
static struct i2c_master_s bus;
static struct i2s_dev_s i2s;
static int transfer(struct i2c_master_s *p, struct i2c_msg_s *m, int n) {
  CHECK(p == &bus && n == 1 && m->frequency == 100000);
  CHECK(m->addr == 0x40 && m->flags == 0 && m->length == 2);
  CHECK(writes < (int)(sizeof(expected) / sizeof(expected[0])));
  CHECK(m->buffer[0] == expected[writes][0]);
  CHECK(m->buffer[1] == expected[writes][1]);
  ++writes;
  return writes == fail_at ? failure : success;
}
#define I2C_TRANSFER(p,m,n) transfer(p,m,n)
static int i2s_call(struct i2s_dev_s *p, unsigned value, int operation) {
  CHECK(p == &i2s && !fail_at && writes == 30);
  CHECK(operation == i2s_calls++);
  CHECK(value == (operation == 0 ? 1u : operation == 1 ? 2u : operation == 2 ? 16u : 16000u));
  return 0;
}
#define I2S_IOCTL(p,c,a) i2s_call(p,c,0)
#define I2S_RXCHANNELS(p,n) i2s_call(p,n,1)
#define I2S_RXDATAWIDTH(p,n) i2s_call(p,n,2)
#define I2S_RXSAMPLERATE(p,n) i2s_call(p,n,3)
static void up_mdelay(unsigned ms) {
  CHECK(ms == 10 || ms == 50);
  if (ms == 50) CHECK(!fail_at && writes == 30 && i2s_calls == 4);
  delay += ms;
}
static void es7210_processbegin(struct es7210_dev_s *p) {
  CHECK(p->running && !fail_at && i2s_calls == 4 && delay == 60);
  ++submissions;
}
/* The driver routines and register macros are inserted here. */
@@SOURCE@@
static int start(struct es7210_dev_s *p) {
#ifdef CONFIG_AUDIO_MULTI_SESSION
  return es7210_start(&p->dev, NULL);
#else
  return es7210_start(&p->dev);
#endif
}
int main(int argc, char **argv) {
  CHECK(argc == 4);
  fail_at = atoi(argv[1]); failure = -atoi(argv[2]); success = atoi(argv[3]);
  CHECK(fail_at >= 0 && fail_at <= 30 && failure < 0);
  struct es7210_dev_s p = {
    .i2c = &bus, .i2s = &i2s, .lower = {100000, 0x40},
    .samprate = 16000, .nchannels = 2, .bpsamp = 16
  };
  int result = start(&p);
  if (fail_at) {
    CHECK(result == failure && p.result == failure && !p.running);
    CHECK(writes == fail_at && i2s_calls == 0 && submissions == 0);
    CHECK(delay == (fail_at == 1 ? 0 : 10));
    /* Clearing the transient bus failure must allow a fresh complete reset. */
    fail_at = 0; writes = 0; delay = 0;
    result = start(&p);
  }
  CHECK(result == 0 && p.result == 0 && p.running);
  CHECK(writes == 30 && i2s_calls == 4 && submissions == 1 && delay == 60);
  puts("PASS");
  return 0;
}
'''


def function(source, name):
    """Select a real definition, never a prototype or a handwritten model."""
    match = re.search(r"static\s+int\s+" + name + r"\([^;{]+\n\{", source)
    if match is None:
        raise ValueError(f"missing native definition: {name}")
    opening = source.index("{", match.start())
    depth = 1
    end = opening + 1
    while depth and end < len(source):
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    if depth:
        raise ValueError(f"unbalanced native definition: {name}")
    text = source[match.start():end]
    if name == "es7210_start":
        # The two signature alternatives share the same body in the SDK.
        if "#else" not in text or "#endif" not in text:
            raise ValueError("ES7210 session signature changed")
        text = "#ifdef CONFIG_AUDIO_MULTI_SESSION\n" + text
    return text


def translation_unit(source, header):
    defines = "\n".join(re.findall(r"^#define ES7210_.*$", header + "\n" + source, re.M))
    routines = "\n\n".join(function(source, name) for name in (
        "es7210_writereg", "es7210_setmicgain", "es7210_reset", "es7210_start"))
    return HARNESS.replace("@@SOURCE@@", defines + "\n" + routines)


class Es7210StartupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source_root = Path(os.environ.get("NXRS_NUTTX_SOURCE", ROOT / "external/nuttx"))
        native = source_root / "drivers/audio/es7210.c"
        required = os.environ.get("NXRS_REQUIRE_ES7210_SOURCE") == "1"
        if not native.is_file():
            if required:
                raise RuntimeError("required pinned ES7210 source is absent")
            raise unittest.SkipTest("initialize external/nuttx or set NXRS_NUTTX_SOURCE")
        data = native.read_bytes()
        blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        if required and blob != PINNED_BLOB:
            raise RuntimeError(f"expected pristine pinned ES7210 blob {PINNED_BLOB}, got {blob}")
        print(f"ES7210 source: {native}; blob={blob}; full pinned source={blob == PINNED_BLOB}", flush=True)
        compiler = shlex.split(os.environ.get("CC", "cc"))
        if not compiler or not shutil.which(compiler[0]):
            raise RuntimeError("a C compiler is required for the native startup witness")
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        root = Path(cls.temp.name)
        target = root / "drivers/audio/es7210.c"
        target.parent.mkdir(parents=True)
        target.write_bytes(data)
        subprocess.run(["git", "apply", "--check", str(PATCH)], cwd=root, check=True,
                       capture_output=True, text=True, timeout=10)
        subprocess.run(["git", "apply", str(PATCH)], cwd=root, check=True,
                       capture_output=True, text=True, timeout=10)
        header = (source_root / "drivers/audio/es7210.h").read_text()
        cls.binaries = {}
        for patched, source in ((False, data.decode()), (True, target.read_text())):
            for multi in (False, True):
                label = f"{'patched' if patched else 'original'}-{'multi' if multi else 'single'}"
                cfile = root / (label + ".c")
                binary = root / label
                cfile.write_text(translation_unit(source, header))
                # The upstream multi-session signature does not use its session argument.
                command = compiler + ["-std=c11", "-Wall", "-Wextra", "-Werror",
                                      "-Wno-unused-parameter"]
                if multi:
                    command += ["-DCONFIG_AUDIO_MULTI_SESSION=1"]
                subprocess.run(command + [str(cfile), "-o", str(binary)], check=True,
                               capture_output=True, text=True, timeout=30)
                cls.binaries[patched, multi] = binary

    def run_case(self, patched, multi, fail_at, error, success):
        return subprocess.run([str(self.binaries[patched, multi]), str(fail_at),
                               str(error), str(success)], capture_output=True,
                              text=True, timeout=5)

    def test_success_preserves_all_register_values_order_and_delays(self):
        for patched in (False, True):
            for multi in (False, True):
                for success in (0, 1):
                    with self.subTest(patched=patched, multi=multi, success=success):
                        result = self.run_case(patched, multi, 0, 5, success)
                        self.assertEqual(result.returncode, 0, result.stderr)

    def test_every_write_failure_stops_before_i2s_and_allows_fresh_retry(self):
        for multi in (False, True):
            for success in (0, 1):
                for error in (5, 110):  # EIO and ETIMEDOUT on the Linux witness.
                    for fail_at in range(1, 31):
                        with self.subTest(multi=multi, success=success, error=error, fail_at=fail_at):
                            result = self.run_case(True, multi, fail_at, error, success)
                            self.assertEqual(result.returncode, 0, result.stderr)
                            self.assertIn("PASS", result.stdout)

    def test_unpatched_driver_is_rejected_by_the_same_failure_oracle(self):
        for multi in (False, True):
            with self.subTest(multi=multi):
                result = self.run_case(False, multi, 1, 5, 0)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("check failed", result.stderr)


if __name__ == "__main__":
    unittest.main()

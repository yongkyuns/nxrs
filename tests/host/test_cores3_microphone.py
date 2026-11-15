"""Host logic witnesses for CoreS3 board bring-up, not NuttX ABI/audio proof."""
from pathlib import Path
import os
import shlex
import shutil
import subprocess
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / "upstream/nuttx/patches/0124-esp32s3-cores3-microphone.patch"
BOARD = "boards/xtensa/esp32s3/esp32s3-m5-cores3/"

CONFIG = """
#define CONFIG_ESP32S3_I2C0_SDAPIN 12
#define CONFIG_ESP32S3_I2C0_SCLPIN 11
#define CONFIG_ESPRESSIF_I2S0_MCLKPIN 0
#define CONFIG_ESPRESSIF_I2S0_BCLKPIN 34
#define CONFIG_ESPRESSIF_I2S0_WSPIN 33
#define CONFIG_ESPRESSIF_I2S0_DINPIN 14
#define CONFIG_ESPRESSIF_I2S0_RX 1
#define CONFIG_ESPRESSIF_I2S0_MCLK 1
#define CONFIG_ESPRESSIF_I2S0_ROLE_MASTER 1
"""
HEADER = """
#ifndef TEST_NATIVE_H
#define TEST_NATIVE_H
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#define I2C_SPEED_STANDARD 100000
#define LOG_ERR 3
struct i2c_master_s { int unused; };
struct i2s_dev_s { int unused; };
struct audio_lowerhalf_s { int unused; };
struct i2c_config_s { uint32_t frequency; uint16_t address; uint8_t addrlen; };
struct es7210_lower_s { uint32_t frequency; uint8_t address; };
int i2c_writeread(struct i2c_master_s *, const struct i2c_config_s *,
                  const uint8_t *, int, uint8_t *, int);
int i2c_write(struct i2c_master_s *, const struct i2c_config_s *,
              const uint8_t *, int);
struct i2c_master_s *esp32s3_i2cbus_initialize(int);
int esp32s3_i2cbus_uninitialize(struct i2c_master_s *);
struct i2s_dev_s *esp_i2sbus_initialize(int);
struct audio_lowerhalf_s *es7210_initialize(struct i2c_master_s *,
    struct i2s_dev_s *, const struct es7210_lower_s *);
int audio_register(const char *, struct audio_lowerhalf_s *);
void up_mdelay(unsigned int);
void syslog(int, const char *, ...);
#endif
"""
HARNESS = r'''
#include "test_native.h"
#include <assert.h>
#include <errno.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
static struct i2c_master_s bus;
static struct i2s_dev_s i2s;
static struct audio_lowerhalf_s codec;
static unsigned char pmic[256];
static int mode, fail_at, apply_failed_write, operations, writes;
static int opens, closes, starts, codecs, registrations, logs;
static bool poisoned;
int cores3_microphone_initialize(void);
static int fault(void) {
  ++operations;
  return poisoned || operations == fail_at;
}
struct i2c_master_s *esp32s3_i2cbus_initialize(int port) {
  assert(port == 0); ++opens; return mode == 1 ? NULL : &bus;
}
int esp32s3_i2cbus_uninitialize(struct i2c_master_s *p) {
  assert(p == &bus); ++closes; return 0;
}
int i2c_writeread(struct i2c_master_s *p, const struct i2c_config_s *c,
                 const uint8_t *w, int nw, uint8_t *r, int nr) {
  assert(p == &bus && nw == 1 && nr == 1);
  assert(c->frequency == 100000 && c->addrlen == 7);
  if (fault()) return -EIO;
  if (c->address == 0x40) {
    assert(*w == 0 && (pmic[0x90] & 2) && (pmic[0x93] & 31) == 28);
    if (mode == 2 || mode == 8 || mode == 9) {
      if (mode == 8) poisoned = true;
      if (mode == 9) pmic[0x90] ^= 0x20; /* unrelated owner update */
      return -ENXIO;
    }
    *r = 0; return 0;
  }
  assert(c->address == 0x34 && (*w == 0x90 || *w == 0x93));
  *r = pmic[*w]; return 0;
}
int i2c_write(struct i2c_master_s *p, const struct i2c_config_s *c,
              const uint8_t *w, int n) {
  assert(p == &bus && c->address == 0x34 && n == 2);
  assert(w[0] == 0x90 || w[0] == 0x93); ++writes;
  int fail = fault();
  if (!fail || apply_failed_write) pmic[w[0]] = w[1];
  return fail ? -EIO : 0;
}
struct i2s_dev_s *esp_i2sbus_initialize(int port) {
  assert(port == 0); ++starts; return mode == 3 ? NULL : &i2s;
}
struct audio_lowerhalf_s *es7210_initialize(struct i2c_master_s *p,
    struct i2s_dev_s *i, const struct es7210_lower_s *c) {
  assert(p == &bus && i == &i2s && c->address == 0x40);
  assert(c->frequency == 100000); ++codecs;
  return mode == 4 ? NULL : &codec;
}
int audio_register(const char *name, struct audio_lowerhalf_s *p) {
  assert(!strcmp(name, "pcm_in0") && p == &codec); ++registrations;
  return mode == 5 ? -EEXIST : 0;
}
void up_mdelay(unsigned int ms) { assert(ms == 10); }
void syslog(int priority, const char *format, ...) {
  assert(priority == LOG_ERR && strstr(format, "restore failed")); ++logs;
}
int main(int argc, char **argv) {
  assert(argc == 4);
  mode = atoi(argv[1]); fail_at = atoi(argv[2]);
  apply_failed_write = atoi(argv[3]);
  pmic[0x90] = mode == 6 || mode == 7 ? 0xa7 : 0xa5;
  pmic[0x93] = mode == 6 ? 0xdc : 0xc7;
  unsigned char original_enable = pmic[0x90], original_volt = pmic[0x93];
  int result = cores3_microphone_initialize();
  if (!fail_at && (mode == 0 || mode == 6)) {
    assert(result == 0 && registrations == 1 && closes == 0);
    assert(pmic[0x90] == (original_enable | 2) && pmic[0x93] == 0xdc);
    if (mode == 6) assert(writes == 0);
  } else {
    int expected = fail_at ? -EIO : mode == 1 || mode == 3 ? -ENODEV :
        mode == 4 ? -ENOMEM : mode == 5 ? -EEXIST : mode == 7 ? -EINVAL : -ENXIO;
    assert(result == expected);
    if (mode == 8) {
      assert(logs == 1 && pmic[0x90] == (original_enable | 2));
    } else {
      assert(logs == 0);
      assert(pmic[0x90] == (original_enable ^ (mode == 9 ? 0x20 : 0)));
      assert(pmic[0x93] == original_volt);
    }
    assert(closes == (mode == 1 || mode == 5 ? 0 : 1));
    if (mode == 1 || mode == 2 || mode == 7 || fail_at) assert(starts == 0);
  }
  int previous_ops = operations;
  assert(cores3_microphone_initialize() == result);
  assert(operations == previous_ops && opens == 1);
  puts("PASS"); return 0;
}
'''


def new_source():
    section = PATCH.read_text().split("+++ b/" + BOARD + "src/esp32s3_microphone.c\n", 1)[1]
    section = section.split("diff --git ", 1)[0]
    return "".join(line[1:] for line in section.splitlines(keepends=True)
                   if line.startswith("+"))


class CoreS3MicrophoneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compiler = shlex.split(os.environ.get("CC", "cc"))
        if not cls.compiler or not shutil.which(cls.compiler[0]):
            raise unittest.SkipTest("C compiler required for board logic witness")
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        (cls.root / "test_native.h").write_text(HEADER)
        for name in ("nuttx/arch.h", "nuttx/audio/audio.h", "nuttx/audio/es7210.h",
                     "nuttx/audio/i2s.h", "nuttx/i2c/i2c_master.h", "syslog.h",
                     "esp32s3_i2c.h", "espressif/esp_i2s.h"):
            p = cls.root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('#include "test_native.h"\n')
        (cls.root / "nuttx/config.h").write_text(CONFIG)
        (cls.root / "board.c").write_text(new_source())
        (cls.root / "harness.c").write_text(HARNESS)
        cls.command = cls.compiler + ["-std=c11", "-Wall", "-Wextra", "-Werror",
                         "-I", str(cls.root), str(cls.root / "board.c"),
                         str(cls.root / "harness.c"), "-o", str(cls.root / "witness")]
        subprocess.run(cls.command, check=True, capture_output=True, text=True, timeout=30)

    def test_bringup_and_native_failures(self):
        for mode in range(10):
            with self.subTest(mode=mode):
                result = subprocess.run([str(self.root / "witness"), str(mode), "0", "0"],
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("PASS", result.stdout)

    def test_every_initial_i2c_operation_and_ambiguous_write(self):
        for transfer in range(1, 8):
            for applied in (0, 1):
                with self.subTest(transfer=transfer, applied=applied):
                    result = subprocess.run([str(self.root / "witness"), "0", str(transfer),
                                             str(applied)], capture_output=True, text=True, timeout=5)
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_wrong_pins_and_transmit_mode_do_not_compile(self):
        configs = [CONFIG.replace("MCLKPIN 0", "MCLKPIN 1"),
                   CONFIG + "\n#define CONFIG_ESPRESSIF_I2S0_TX 1\n"]
        try:
            for config in configs:
                (self.root / "nuttx/config.h").write_text(config)
                result = subprocess.run(self.command, capture_output=True, text=True, timeout=30)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("CoreS3 microphone", result.stderr)
        finally:
            (self.root / "nuttx/config.h").write_text(CONFIG)

    def test_qualification_profile_pins_are_required_and_capture_only(self):
        profile = tomllib.loads((ROOT / "tests/profiles/cores3-microphone.toml").read_text())
        self.assertEqual(profile["hal"], {})
        self.assertFalse(profile["requires-qemu"])
        config = profile["kconfig"]
        self.assertFalse(set(config["enable"]) & set(config["disable"]))
        for setting in ("ESP32S3_I2C0_SDAPIN=12", "ESP32S3_I2C0_SCLPIN=11",
                        "ESPRESSIF_I2S0_MCLKPIN=0", "ESPRESSIF_I2S0_BCLKPIN=34",
                        "ESPRESSIF_I2S0_WSPIN=33", "ESPRESSIF_I2S0_DINPIN=14"):
            self.assertIn(setting, config["set"])
            self.assertIn("CONFIG_" + setting, config["require"])
        for symbol in ("ESP32S3_M5_CORES3_MIC", "AUDIO_ES7210", "SYSTEM_NXRECORDER"):
            self.assertIn(symbol, config["enable"])
            self.assertIn("CONFIG_" + symbol + "=y", config["require"])
        self.assertIn("ESPRESSIF_I2S0_TX", config["disable"])
        self.assertIn("# CONFIG_ESPRESSIF_I2S0_TX is not set", config["require"])


if __name__ == "__main__":
    unittest.main()

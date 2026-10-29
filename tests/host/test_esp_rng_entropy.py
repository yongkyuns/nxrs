"""Run the patched driver with injected SDK/lock state; not hardware proof."""
import hashlib
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import tempfile
import unittest

from test_esp_mbedtls_allocators import source_preimage

ROOT = Path(__file__).resolve().parents[2]
SOURCE = 'arch/xtensa/src/esp32s3/esp32s3_rng.c'
PATCH = ROOT/'upstream/nuttx/patches/0093-esp32s3-guard-rf-entropy.patch'
ADAPTER = 'arch/xtensa/src/esp32s3/esp32s3_wifi_adapter.c'
PHY = 'components/esp_phy/src/phy_init.c'
PHY_PATCH = ROOT/'upstream/esp-hal-3rdparty/patches/0003-esp32s3-serialize-wifi-phy-state.patch'


def function(source, name):
    match = re.search(r'(?:static )?(?:ssize_t|void) ' + name +
                      r'\([^;{}]*\)\n\{.*?\n\}', source, re.S)
    if not match:
        raise AssertionError('missing complete driver function: ' + name)
    return match.group()


HARNESS = r'''
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <sys/types.h>
#define MIN(a,b) ((a) < (b) ? (a) : (b))
#define OK 0
#define PHY_MODEM_WIFI 1
#define CONFIG_IDF_TARGET_ESP32S3 1
#define CONFIG_ESP_PHY_DISABLE_PLL_TRACK 1
#define PHY_INIT_MODEM_CLOCK_REQUIRED_BITS 0
struct file { int f_oflags; };
typedef int mutex_t;
typedef mutex_t *_lock_t;
typedef unsigned esp_phy_modem_t;
static struct rng_dev_s { mutex_t rd_lock; } g_rngdev;
static mutex_t phy_mutex;
static _lock_t s_phy_access_lock;
static bool s_is_phy_calibrated;
static unsigned wifi_bit, power_updates;
static int initialized, busy, held, raw_held, radio, guard;
static unsigned tries, blocks, samples, releases;
static _lock_t phy_get_lock(void) { return initialized ? &phy_mutex : NULL; }
static void _lock_acquire(_lock_t *lock)
{ assert(!held); *lock = &phy_mutex; initialized = 1; held = 1; }
static int _lock_try_acquire(_lock_t *lock)
{
  assert(*lock == &phy_mutex); tries++;
  if (busy || held) return -EAGAIN;
  held = 1; return 0;
}
static void _lock_release(_lock_t *lock)
{ assert(*lock == &phy_mutex && held); held = 0; releases++; }
static unsigned phy_get_modem_flag(void) { assert(held); return radio; }
static int nxmutex_lock(mutex_t *lock)
{ assert(lock == &g_rngdev.rd_lock); blocks++; if (busy) return -EINTR; raw_held = 1; return 0; }
static int nxmutex_trylock(mutex_t *lock)
{ assert(lock == &g_rngdev.rd_lock); tries++; if (busy) return -EAGAIN; raw_held = 1; return 0; }
static int nxmutex_unlock(mutex_t *lock)
{ assert(lock == &g_rngdev.rd_lock && raw_held); raw_held = 0; releases++; return 0; }
static uint32_t esp_random(void) { samples++; return 0x33333333; }
static void esp_phy_common_clock_enable(void) { assert(held); }
static void esp_phy_common_clock_disable(void) { assert(held); }
static void phy_module_enable(void) { assert(held); }
static void phy_module_disable(void) { assert(held); }
static bool phy_module_has_clock_bits(unsigned bits) { assert(held && !bits); return true; }
static void esp_phy_load_cal_and_init(void) { assert(held); }
static void phy_wakeup_init(void) { assert(held); }
static bool phy_ant_need_update(void) { return false; }
static void phy_ant_update(void) { assert(held); }
static void phy_ant_clr_update_flag(void) { assert(held); }
static void phy_set_modem_flag(unsigned value) { assert(held); radio |= value; }
static void phy_clr_modem_flag(unsigned value) { assert(held); radio &= ~value; }
static void phy_close_rf(void) { assert(held); }
static void phy_xpd_tsens(void) { assert(held); }
static void phy_wifi_enable_set(unsigned value);
static void esp_fill_random(void *bytes, size_t size)
{
  assert(size <= 32);
  if (guard)
    {
      _lock_t lock = phy_get_lock();
      assert(held && radio == PHY_MODEM_WIFI);
      /* Sleep/shutdown cannot acquire the PHY mutex during any sample. */
      assert(_lock_try_acquire(&lock) == -EAGAIN);
    }
  else assert(raw_held);
  for (size_t i = 0; i < size; i++) ((char *)bytes)[i] = 0x33;
  samples += (size + 3) / 4;
}
/* DRIVER */
/* POWER */
static void phy_wifi_enable_set(unsigned value)
{ assert(held); wifi_bit = value; power_updates++; }
int main(void)
{
  struct file device = { .f_oflags = O_NONBLOCK };
  char bytes[65]; memset(bytes, 0x5a, sizeof(bytes));
  busy = 1;
  assert(esp32s3_rng_read(&device, bytes, sizeof(bytes)) == -EAGAIN);
  assert(tries == 1 && blocks == 0 && samples == 0 && bytes[0] == 0x5a);
  assert(esp32s3_rng_read(&device, bytes, 0) == 0 && tries == 1 && !blocks);
  device.f_oflags = 0;
  assert(esp32s3_rng_read(&device, bytes, 1) == -EINTR && blocks == 1);
  busy = 0;
  unsigned sizes[] = { 1, 3, 4, 31, 32, 33, sizeof(bytes) };
  for (unsigned i = 0; i < sizeof(sizes)/sizeof(sizes[0]); i++)
    {
      memset(bytes, 0x5a, sizeof(bytes));
      unsigned count = MIN(sizes[i], 32), before = samples;
      assert(esp32s3_rng_read(&device, bytes, sizes[i]) == count);
      assert(!raw_held && samples - before == (count + 3) / 4);
      for (unsigned j = 0; j < sizeof(bytes); j++) assert(bytes[j] == (j < count ? 0x33 : 0x5a));
    }
  guard = 1; unsigned before = samples; tries = 0;
  assert(esp32s3_trng_read(&device, bytes, 0) == 0 && !tries);
#if !defined(CONFIG_ESPRESSIF_WIFI) || defined(CONFIG_SMP) || defined(CONFIG_PM) || defined(CONFIG_ESP32S3_AUTO_SLEEP)
  assert(esp32s3_trng_read(&device, bytes, 1) == -ENOTSUP);
#else
  assert(esp32s3_trng_read(&device, bytes, 1) == -EAGAIN && !tries);
  initialized = 1; busy = 1;
  assert(esp32s3_trng_read(&device, bytes, 1) == -EAGAIN && !held);
  busy = 0;
  for (radio = 0; radio <= 2; radio += 2)
    assert(esp32s3_trng_read(&device, bytes, 1) == -EAGAIN && !held);
  assert(samples == before);
  radio = PHY_MODEM_WIFI;
  memset(bytes, 0x5a, sizeof(bytes));
  assert(esp32s3_trng_read(&device, bytes, sizeof(bytes)) == 32);
  assert(samples == before + 8 && !held && bytes[31] == 0x33 && bytes[32] == 0x5a);
  radio = 0; before = samples;
  assert(esp32s3_trng_read(&device, bytes, 1) == -EAGAIN);
  esp_phy_enable_wrapper();
  assert(radio == PHY_MODEM_WIFI && wifi_bit == 1 && power_updates == 1 && !held);
  esp_phy_disable_wrapper();
  assert(!radio && !wifi_bit && power_updates == 2 && !held);
#endif
  assert(samples == before && !held && !raw_held && releases);
  return 0;
}
'''


class EspRngTests(unittest.TestCase):
    def test_applied_driver_bounds_reads_and_holds_the_active_source(self):
        compiler = shutil.which('cc')
        if not compiler:
            self.skipTest('native C compiler required')
        series = runpy.run_path(str(ROOT/'tools/apply-nuttx-patches.py'))['SERIES']
        self.assertIn(PATCH.name, series['nuttx'])
        self.assertIn(PHY_PATCH.name, series['esp-hal-3rdparty'])
        with tempfile.TemporaryDirectory(prefix='nxrs-esp-rng-') as temporary:
            root = Path(temporary)
            source = root/SOURCE
            source.parent.mkdir(parents=True)
            original = source_preimage(PATCH.read_text(), SOURCE, expected_lines=None)
            old_read = function(original, 'esp32s3_rng_read')
            self.assertEqual(hashlib.sha256(old_read.encode()).hexdigest(),
                             '949448c6c147c3760a21adc66465c75f87601ca42f7a295c6d4c8d034c4b7640')
            source.write_text(original)
            adapter = root/ADAPTER
            adapter.write_text(source_preimage(PATCH.read_text(), ADAPTER, expected_lines=None))
            phy = root/PHY
            phy.parent.mkdir(parents=True)
            phy.write_text(source_preimage(PHY_PATCH.read_text(), PHY, expected_lines=None))
            old_power = '\n'.join(function(phy.read_text(), name) for name in ('esp_phy_enable', 'esp_phy_disable'))
            old_power += '\n' + '\n'.join(function(adapter.read_text(), name) for name in
                                          ('esp_phy_enable_wrapper', 'esp_phy_disable_wrapper'))
            subprocess.run(['git', 'apply', str(PATCH), str(PHY_PATCH)], cwd=root, check=True, capture_output=True,
                           env={**os.environ, 'GIT_CEILING_DIRECTORIES': str(root.parent)})
            patched = source.read_text()
            read = function(patched, 'esp32s3_rng_read')
            trng = function(patched, 'esp32s3_trng_read')
            power = '\n'.join(function(phy.read_text(), name) for name in ('esp_phy_enable', 'esp_phy_disable'))
            power += '\n' + '\n'.join(function(adapter.read_text(), name) for name in
                                      ('esp_phy_enable_wrapper', 'esp_phy_disable_wrapper'))
            early_unlock = trng.replace('  _lock_release(&lock);\n', '').replace(
                '      esp_fill_random(buffer, ret);',
                '      _lock_release(&lock);\n      esp_fill_random(buffer, ret);')
            # The unchanged driver and removed RF/lock guards must fail this witness.
            cases = [(read, trng, ['CONFIG_ESPRESSIF_WIFI'], True),
                     (old_read, trng, ['CONFIG_ESPRESSIF_WIFI'], False),
                     (read, trng.replace('lock == NULL || ', ''), ['CONFIG_ESPRESSIF_WIFI'], False),
                     (read, early_unlock, ['CONFIG_ESPRESSIF_WIFI'], False),
                     (read, trng.replace('!= 0)\n    {\n      ret = MIN', '== 0)\n    {\n      ret = MIN'), ['CONFIG_ESPRESSIF_WIFI'], False)]
            cases += [(read, trng, flags, True) for flags in
                      ([], ['CONFIG_ESPRESSIF_WIFI', 'CONFIG_SMP'],
                       ['CONFIG_ESPRESSIF_WIFI', 'CONFIG_PM'],
                       ['CONFIG_ESPRESSIF_WIFI', 'CONFIG_ESP32S3_AUTO_SLEEP'])]
            cases = [(*case, power) for case in cases]
            cases.append((read, trng, ['CONFIG_ESPRESSIF_WIFI'], False, old_power))
            for index, (reader, entropy, flags, success, transitions) in enumerate(cases):
                with self.subTest(case=index):
                    c = root/'test.c'
                    c.write_text(HARNESS.replace('/* DRIVER */', reader + '\n' + entropy)
                                 .replace('/* POWER */', transitions))
                    binary = root/'test'
                    compiled = subprocess.run([compiler, '-std=c11', '-Wall', '-Wextra', '-Werror',
                                    '-Wno-unused-function', '-Wno-unused-parameter', '-Wno-unused-variable',
                                    '-fsanitize=address,undefined', '-fno-sanitize-recover=all',
                                    '-D__NuttX__', *['-D'+flag for flag in flags], str(c), '-o', str(binary)],
                                              capture_output=True, text=True)
                    self.assertEqual(compiled.returncode, 0, compiled.stderr)
                    result = subprocess.run([str(binary)], capture_output=True, timeout=5,
                                            env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0:abort_on_error=1'})
                    self.assertEqual(result.returncode == 0, success, result.stderr.decode())

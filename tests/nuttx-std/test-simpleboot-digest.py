"""Compile the pinned loader's actual parsing block with flash-read stubs.

The original parser is a required failing control for digest-bearing layouts.
This is native source qualification, not an Xtensa link, ROM execution or boot.
"""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tests/std-channel-perf'))
import simpleboot_digest as candidate
from inspect_esp_image import parse_image
from test_simpleboot_digest import two_mapped_image

HARNESS = r'''
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <inttypes.h>
#define CONFIG_ESPRESSIF_SIMPLE_BOOT 1
#define CONFIG_BOOTLOADER_OFFSET_IN_FLASH 0
#define ESP_OK 0
#define WORD_ALIGNED_ATTR
#define MMU_FLASH_MASK 0xffff0000u
#define SOC_DRAM_LOW 0x3fc88000u
#define SOC_DRAM_HIGH 0x3fd00000u
#define SOC_IRAM_LOW 0x40370000u
#define SOC_IRAM_HIGH 0x403f0000u
#define SOC_IROM_LOW 0x42000000u
#define SOC_IROM_HIGH 0x44000000u
#define SOC_DROM_LOW 0x3c000000u
#define SOC_DROM_HIGH 0x3e000000u
#define SOC_RTC_SLOW_MEM_SUPPORTED 1
#define SOC_RTC_DATA_LOW 0x50000000u
#define SOC_RTC_DATA_HIGH 0x50002000u
/* SOURCE_MACROS */
typedef struct {
  uint8_t magic, segment_count, reserved[21], hash_appended;
} esp_image_header_t;
typedef struct { uint32_t load_addr, data_len; } esp_image_segment_header_t;
_Static_assert(sizeof(esp_image_header_t) == 24, "header size");
_Static_assert(sizeof(esp_image_segment_header_t) == 8, "segment size");
static uint8_t *image;
static size_t image_size;
static uint32_t _image_irom_vma, _image_drom_vma;
static int ets_printf(const char *format, ...) { (void)format; return 0; }
static int bootloader_flash_read(size_t offset, void *dest, size_t size, bool mapped)
{
  (void)mapped;
  if (offset > image_size || size > image_size - offset) return -1;
  memcpy(dest, image + offset, size);
  return ESP_OK;
}
static unsigned parse(uint32_t *irom, uint32_t *drom)
{
  uint32_t app_irom_start = 0, app_drom_start = 0;
  uint32_t app_irom_start_aligned = 0, app_drom_start_aligned = 0;
/* SOURCE_BODY */
  *irom = app_irom_start; *drom = app_drom_start;
  (void)app_irom_start_aligned; (void)app_drom_start_aligned;
  return rom_segments;
}
int main(int argc, char **argv)
{
  if (argc != 6) return 64;
  FILE *file = fopen(argv[1], "rb");
  if (!file || fseek(file, 0, SEEK_END)) return 65;
  long length = ftell(file);
  if (length < 24 || length > 64 * 1024 * 1024 || fseek(file, 0, SEEK_SET)) return 66;
  image_size = (size_t)length;
  image = malloc(image_size);
  if (!image || fread(image, 1, image_size, file) != image_size) return 67;
  fclose(file);
  _image_irom_vma = (uint32_t)strtoul(argv[2], NULL, 0);
  _image_drom_vma = (uint32_t)strtoul(argv[3], NULL, 0);
  uint32_t irom, drom;
  unsigned count = parse(&irom, &drom);
  free(image);
  printf("found=%u irom=%" PRIu32 " drom=%" PRIu32 "\n", count, irom, drom);
  return !(count == 2 && irom == strtoul(argv[4], NULL, 0)
           && drom == strtoul(argv[5], NULL, 0));
}
'''


def compile_parser(source, output):
    """Extract code from a checked pinned source, not a rewritten Python model."""
    start = source.index('#define CHECKSUM_ALIGN')
    end = source.index('\n/****************************************************************************', start)
    macros = source[start:end]
    start = source.index('#ifdef CONFIG_ESPRESSIF_SIMPLE_BOOT\n  esp_image_header_t image_header',
                         source.index('int map_rom_segments('))
    end = source.index('\n#endif', start) + len('\n#endif')
    body = source[start:end]
    text = HARNESS.replace('/* SOURCE_MACROS */', macros).replace('/* SOURCE_BODY */', body)
    output = Path(output)
    c_file = output.with_suffix('.c')
    c_file.write_text(text)
    subprocess.run(['cc', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                    str(c_file), '-o', str(output)], check=True, capture_output=True, text=True)
    return output


def run_parser(binary, image_file, layout):
    mapped = [s for s in layout['hidden_segments'] if s['kind'] == 'mapped']
    irom = next(s for s in mapped if 0x42000000 <= s['load_address'] < 0x44000000)
    drom = next(s for s in mapped if 0x3c000000 <= s['load_address'] < 0x3e000000)
    return subprocess.run([str(binary), str(image_file), str(irom['load_address']),
                           str(drom['load_address']), str(irom['header_offset'] + 8),
                           str(drom['header_offset'] + 8)], capture_output=True, text=True,
                          timeout=5)


def patched_source(directory):
    path = ROOT / 'external/nuttx' / candidate.LOADER_PATH
    original = path.read_bytes()
    if hashlib.sha256(original).hexdigest() != candidate.LOADER_BEFORE:
        raise ValueError('native regression requires exact pinned upstream loader')
    destination = Path(directory) / candidate.LOADER_PATH
    destination.parent.mkdir(parents=True)
    destination.write_bytes(original)
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(Path(directory).parent))
    subprocess.run(['git', 'apply', '--check', str(candidate.PATCH)], cwd=directory,
                   env=env, check=True, capture_output=True, text=True)
    subprocess.run(['git', 'apply', str(candidate.PATCH)], cwd=directory,
                   env=env, check=True, capture_output=True, text=True)
    patched = destination.read_bytes()
    if hashlib.sha256(patched).hexdigest() != candidate.LOADER_AFTER:
        raise ValueError('patched loader hash disagrees with candidate contract')
    return original.decode(), patched.decode()


class NativeLoaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.directory = Path(cls.tmp.name)
        original, patched = patched_source(cls.directory)
        cls.original = compile_parser(original, cls.directory / 'original-parser')
        cls.patched = compile_parser(patched, cls.directory / 'patched-parser')
        cls.data, _ = two_mapped_image()
        cls.new_data, _ = candidate.repack(cls.data)
        cls.legacy_file = cls.directory / 'legacy.bin'
        cls.digest_file = cls.directory / 'digest.bin'
        cls.legacy_file.write_bytes(cls.data)
        cls.digest_file.write_bytes(cls.new_data)

    def test_original_loader_accepts_legacy_layout(self):
        result = run_parser(self.original, self.legacy_file, parse_image(self.data))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_original_loader_fails_digest_layout_control(self):
        result = run_parser(self.original, self.digest_file, parse_image(self.new_data))
        self.assertNotEqual(result.returncode, 0, 'control unexpectedly parsed digest as segments')

    def test_patched_loader_preserves_legacy_layout(self):
        result = run_parser(self.patched, self.legacy_file, parse_image(self.data))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_patched_loader_parses_both_mapped_segments_after_digest(self):
        result = run_parser(self.patched, self.digest_file, parse_image(self.new_data))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()

/* Actual NuttX alignment and upstream flag writer over write-once program units. */
#include <assert.h>
#include <stdint.h>
#include <string.h>
#include <mcuboot_config/mcuboot_config.h>
#ifndef MCUBOOT_BOOT_MAX_ALIGN
#  define MCUBOOT_BOOT_MAX_ALIGN 8
#endif
#define BOOT_MAX_ALIGN MCUBOOT_BOOT_MAX_ALIGN
#define BOOT_LOG_INF(...) ((void)0)
#define BOOT_LOG_DBG(...) ((void)0)
#define BOOT_EFLASH 1
#define ALIGN_UP(n, a) (((n) + (a) - 1) / (a) * (a))
struct flash_area { unsigned unused; };
static uint8_t flash[128];
static unsigned programmed;
static uint8_t flash_area_erased_val(const struct flash_area *area)
{ (void)area; return 0xff; }
static int flash_area_write(const struct flash_area *area, uint32_t off,
                            const void *data, uint32_t len)
{
  (void)area;
  if (off % 32 || len != 32 || off > sizeof flash - len ||
      (programmed & (1u << (off / 32)))) return -1;
  memcpy(flash + off, data, len);
  programmed |= 1u << (off / 32);
  return 0;
}
#include "alignment.inc"
#include "trailer.inc"
int main(void)
{
  struct flash_area area = {0};
  const uint8_t flag = 1;
  if (BOOT_MAX_ALIGN != 32 || flash_area_align(&area) != 32) return 1;
  memset(flash, 0xff, sizeof flash);
  assert(boot_write_trailer(&area, 0, &flag, 1) == 0);
  assert(boot_write_trailer(&area, 32, &flag, 1) == 0);
  assert(programmed == 3 && flash[0] == 1 && flash[32] == 1);
  for (unsigned i = 0; i < sizeof flash; ++i)
    if (i != 0 && i != 32) assert(flash[i] == 0xff);
  assert(boot_write_trailer(&area, 0, &flag, 1) == BOOT_EFLASH);
  return 0;
}

/* Pinned SDK erase function with a partition ioctl witness; no target claim. */
#include <assert.h>
#include <stdint.h>
#include <stddef.h>
#define OK 0
#define ERROR (-1)
#define MTDIOC_ERASESECTORS 1
#define BIOC_DISCARD 2
struct flash_area { uint8_t fa_id; uint32_t fa_size; };
struct mtd_erase_s { uint32_t startblock, nblocks; };
struct flash_device_s { int fd; struct { uint32_t erasesize; } mtdgeo; };
static struct flash_device_s device = {7, {4096}};
static unsigned calls;
static int failure;
static struct flash_device_s *lookup_flash_device_by_id(uint8_t id)
{ return id == 0 ? &device : NULL; }
static int ioctl(int fd, int command, unsigned long argument)
{
  assert(fd == 7);
  if (calls++ == 0) {
    const struct mtd_erase_s *erase = (const void *)(uintptr_t)argument;
    assert(command == MTDIOC_ERASESECTORS);
    assert(erase->startblock == 1 && erase->nblocks == 2);
    return failure == 1 ? ERROR : failure == 3 ? 1 : failure == 4 ? OK : 2;
  }
  assert(calls == 2 && command == BIOC_DISCARD && argument == 0);
  return failure == 2 ? ERROR : OK;
}
#include "erase.inc"
int main(void)
{
  struct flash_area area = {0, 16384};
  const uint32_t invalid[][2] = {
    {1, 4096}, {0, 1}, {16384, 4096}, {20480, 0}, {4096, UINT32_MAX}
  };
  for (size_t i = 0; i < sizeof invalid / sizeof invalid[0]; ++i)
    assert(flash_area_erase(&area, invalid[i][0], invalid[i][1]) == ERROR);
  assert(flash_area_erase(&area, 16384, 0) == OK && calls == 0);
  area.fa_id = 1;
  assert(flash_area_erase(&area, 4096, 8192) == ERROR && calls == 0);
  area.fa_id = 0;
  device.fd = -1;
  assert(flash_area_erase(&area, 4096, 8192) == ERROR && calls == 0);
  device.fd = 7;
  device.mtdgeo.erasesize = 0;
  assert(flash_area_erase(&area, 4096, 8192) == ERROR && calls == 0);
  device.mtdgeo.erasesize = 4096;
  for (failure = 0; failure <= 4; ++failure) {
    calls = 0;
    assert(flash_area_erase(&area, 4096, 8192) ==
           (failure > 0 && failure < 4 ? ERROR : OK));
    assert(calls == 2);
  }
  return 0;
}

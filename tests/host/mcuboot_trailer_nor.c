/* Actual NuttX FTL function over a NOR/power-cut model; not a target ABI test. */
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <sys/types.h>
#ifndef O_DIRECT
# define O_DIRECT (1 << 20)
#endif
#define FAR
#define MIN(a, b) ((a) < (b) ? (a) : (b))
#define ferr(...) ((void)0)
static uint8_t flash[4096];
static size_t cut;
static unsigned erases;
struct mtd_dev_s { int (*erase)(void); };
struct ftl_struct_s {
  struct mtd_dev_s *mtd;
  struct { size_t blocksize; } geo;
  unsigned blkper;
  int oflags;
};
static int erase(void) { memset(flash, 0xff, sizeof flash); ++erases; return 0; }
static int ftl_mtd_erase(struct ftl_struct_s *dev, off_t block)
{ (void)dev; (void)block; return erase(); }
static ssize_t program(off_t block, size_t count, const uint8_t *data)
{
  for (size_t i = 0; i < cut; ++i) flash[block * 256 + i] &= data[i];
  return cut == count * 256 ? (ssize_t)count : -EIO;
}
#define MTD_BWRITE(mtd, block, count, data) program(block, count, data)
#include "ftl.inc"
#include "open_flags.inc"
int main(void)
{
  uint8_t before[sizeof flash], page[256];
  struct mtd_dev_s mtd = {erase};
  struct ftl_struct_s dev = {&mtd, {256}, 16, SDK_OPEN_FLAGS};
  if ((dev.oflags & (O_DIRECT | O_SYNC)) != (O_DIRECT | O_SYNC)) return 1;
  for (cut = 0; cut <= sizeof page; ++cut) {
    memset(flash, 0x5a, sizeof flash);
    memset(flash, 0xff, sizeof page);
    memcpy(before, flash, sizeof flash);
    memcpy(page, flash, sizeof page);
    page[32] = 1; /* MCUboot image_ok; all other trailer bytes are retained. */
    erases = 0;
    (void)ftl_flush_direct(&dev, page, 0, 1);
    if (erases || memcmp(flash + 256, before + 256, sizeof flash - 256)) return 2;
    for (size_t i = 0; i < sizeof page; ++i)
      if (i != 32 && flash[i] != before[i]) return 3;
    if (cut == sizeof page && flash[32] != 1) return 4;
  }
  return 0;
}

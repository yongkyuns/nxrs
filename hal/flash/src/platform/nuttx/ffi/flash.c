#include <fcntl.h>
#include <nuttx/config.h>
#include <nuttx/mtd/mtd.h>
#include <sys/ioctl.h>

#if !defined(CONFIG_BCH) || !defined(CONFIG_MTD_PARTITION) || \
    defined(CONFIG_BCH_DEVICE_READONLY) || defined(CONFIG_BCH_ENCRYPTION) || \
    defined(CONFIG_BCH_FORCE_INDIRECT) || defined(CONFIG_FTL_BBM) || \
    defined(CONFIG_FTL_WRITEBUFFER) || defined(CONFIG_FTL_READAHEAD)
#error "Raw NOR requires a writable direct MTD partition proxy"
#endif

int flash_direct_flags(void)
{
  return O_DIRECT | O_SYNC;
}

int flash_geometry(int fd, uint32_t *program, uint32_t *erase, uint32_t *sectors)
{
  struct mtd_geometry_s native = {0};
  int status = ioctl(fd, MTDIOC_GEOMETRY, (unsigned long)&native);
  if (status < 0)
    return status;

  *program = native.blocksize;
  *erase = native.erasesize;
  *sectors = native.neraseblocks;
  return 0;
}

int flash_erase_sectors(int fd, uint32_t start, uint32_t count)
{
  struct mtd_erase_s range = { .startblock = start, .nblocks = count };
  return ioctl(fd, MTDIOC_ERASESECTORS, (unsigned long)&range);
}

int flash_erased_byte(int fd, uint8_t *value)
{
  return ioctl(fd, MTDIOC_ERASESTATE, (unsigned long)value);
}

int flash_discard_cache(int fd)
{
  return ioctl(fd, BIOC_DISCARD, 0);
}

/* Actual pinned SDK functions; native I/O injection, not a target ABI test. */
#include <assert.h>
#include <errno.h>
#include <stdint.h>
#include <stddef.h>
#include <sys/types.h>
#define BOOT_LOG_INF(...) ((void)0)
#define BOOT_LOG_ERR(...) ((void)0)
#define DEBUGASSERT assert
#define ERROR -1
#define OK 0
#define SEEK_SET 0
struct flash_area { uint8_t fa_id; uint32_t fa_off, fa_size; const char *fa_mtd_path; };
struct flash_sector { uint32_t fs_off, fs_size; };
struct flash_device_s { int fd; struct { uint32_t erasesize; } mtdgeo; };
static struct flash_device_s dev = {1, {4096}};
static unsigned calls;
static ssize_t amount;
static struct flash_device_s *lookup_flash_device_by_id(uint8_t id) { return id == 1 ? &dev : NULL; }
static struct flash_device_s *lookup_flash_device_by_offset(uint32_t offset) __attribute__((unused));
static struct flash_device_s *lookup_flash_device_by_offset(uint32_t offset) { return offset == 0x220000 ? &dev : NULL; }
static off_t lseek(int fd, off_t off, int whence) { (void)fd; (void)whence; ++calls; return off; }
static ssize_t read(int fd, void *data, size_t len) { (void)fd; (void)data; (void)len; ++calls; return amount; }
static ssize_t write(int fd, const void *data, size_t len) { (void)fd; (void)data; (void)len; ++calls; return amount; }
#include "io.inc"
int main(void)
{
  struct flash_area area = {1, 0x220000, 8192, "fixture"};
  struct flash_sector sector;
  uint8_t data[32];
  for (amount = -1; amount <= 33; ++amount) {
    int expected = amount == 32 ? 0 : -1;
    if (flash_area_read(&area, 0, data, 32) != expected ||
        flash_area_write(&area, 0, data, 32) != expected) return 1;
  }
  for (unsigned i = 0; i < 2; ++i) {
    uint32_t off = i ? UINT32_MAX : 8191;
    calls = 0;
    if (flash_area_read(&area, off, data, 2) != -1 ||
        flash_area_write(&area, off, data, 2) != -1 || calls) return 1;
  }
  if (flash_area_get_sector(&area, 4097, &sector) || sector.fs_off != 4096 || sector.fs_size != 4096 ||
      flash_area_get_sector(&area, -1, &sector) != -1 || flash_area_get_sector(&area, 8192, &sector) != -1) return 1;
#ifdef CONFIG_ESP32S3_OTA_PARTITION_ENCRYPT
  if (flash_area_align(&area) != 32) return 1;
#elif defined(CONFIG_ESP32S3_APP_FORMAT_MCUBOOT)
  if (flash_area_align(&area) != 4) return 1;
#else
  if (flash_area_align(&area) != 1) return 1;
#endif
  return 0;
}

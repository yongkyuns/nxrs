/* Actual raw-MTD SDK calls and target fields only. Rust owns partition views,
 * admission, bounds, sequencing, buffers, deadlines and retirement. A file open
 * may create an FTL/BCH proxy and does not express these raw NOR operations. */
#include <nuttx/config.h>
#include <nuttx/mtd/mtd.h>
#include <nuttx/kmalloc.h>
#include <errno.h>
#include <stdint.h>

#if !defined(CONFIG_ESP32S3_SPIFLASH) || !defined(CONFIG_ESP32S3_MTD) || !defined(CONFIG_MTD_BYTE_WRITE)
#error "Firmware staging requires the configured ESP32-S3 raw MTD resource"
#endif
#if defined(CONFIG_ESP32S3_SPIFLASH_AUTO_BRINGUP) || defined(CONFIG_ESP32S3_SPIFLASH_SMARTFS) || \
    defined(CONFIG_ESP32S3_SPIFLASH_NXFFS) || defined(CONFIG_ESP32S3_SPIFLASH_SPIFFS) || \
    defined(CONFIG_ESP32S3_SPIFLASH_LITTLEFS)
#error "Firmware staging requires raw MTD without filesystem auto-bring-up"
#endif
#if defined(CONFIG_ESP32S3_SPIRAM) && \
    (!defined(CONFIG_MM_KERNEL_HEAP) || !defined(CONFIG_ESP32S3_SPIRAM_USER_HEAP) || \
     !defined(CONFIG_ESP32S3_SPI_FLASH_SUPPORT_PSRAM_STACK))
#error "Firmware staging requires an internal kernel heap and the PSRAM-stack flash path"
#endif
#if defined(CONFIG_ESP32S3_OTA_PARTITION_ENCRYPT) || defined(CONFIG_ESPRESSIF_SECURE_FLASH_ENC_ENABLED)
#error "Encrypted flash requires separate target qualification"
#endif
#if defined(CONFIG_ESP32S3_SPIRAM) || defined(CONFIG_ESP32S3_PARTITION_TABLE)
#include "arch/xtensa/src/esp32s3/esp32s3_spiflash_mtd.h"
struct mtd_dev_s *nxrs_update_root(void) { return esp32s3_spiflash_mtd(); }
#else
#include "arch/xtensa/src/common/espressif/esp_spiflash_mtd.h"
struct mtd_dev_s *nxrs_update_root(void) { return esp_spiflash_mtd(); }
#endif
int nxrs_update_geometry(struct mtd_dev_s *device, uint32_t *values)
{
  struct mtd_geometry_s native = {0};
  int result = MTD_IOCTL(device, MTDIOC_GEOMETRY, (unsigned long)(uintptr_t)&native);
  if (result < 0) return result;
  values[0] = native.blocksize;
  values[1] = native.erasesize;
  values[2] = native.neraseblocks;
  return result;
}
int nxrs_update_has_write(struct mtd_dev_s *device)
{
  return device->write != NULL ? 0 : -ENOSYS;
}
ssize_t nxrs_update_read(struct mtd_dev_s *device, uint32_t offset, uint8_t *bytes, uint32_t length)
{
  return MTD_READ(device, offset, length, bytes);
}
ssize_t nxrs_update_write(struct mtd_dev_s *device, uint32_t offset, const uint8_t *bytes, uint32_t length)
{
  return MTD_WRITE(device, offset, length, bytes);
}
int nxrs_update_erase(struct mtd_dev_s *device, uint32_t block)
{
  return MTD_ERASE(device, block, 1);
}
void *nxrs_update_internal_alloc(void) { return kmm_memalign(8, 4096); }
void nxrs_update_internal_free(void *memory) { kmm_free(memory); }

void nxrs_update_storage_config(uint32_t *values)
{
  values[0] = CONFIG_ESP32S3_STORAGE_MTD_OFFSET;
  values[1] = CONFIG_ESP32S3_STORAGE_MTD_SIZE;
}

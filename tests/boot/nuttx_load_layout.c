/* SDK target-header witness only; never linked into either boot image. */
#include <stddef.h>
#include <stdint.h>
#include "esp_mcuboot_image.h"
#include "soc/soc.h"
_Static_assert(sizeof(((esp_image_load_header_t *)0)->entry_addr) == 4, "entry field ABI");
const uint32_t nxrs_nuttx_load_layout[] = {
    ESP_LOAD_HEADER_MAGIC,
    sizeof(esp_image_load_header_t),
    sizeof(((esp_image_load_header_t *)0)->entry_addr),
    offsetof(esp_image_load_header_t, header_magic),
    offsetof(esp_image_load_header_t, entry_addr),
    offsetof(esp_image_load_header_t, iram_dest_addr),
    offsetof(esp_image_load_header_t, iram_flash_offset),
    offsetof(esp_image_load_header_t, iram_size),
    offsetof(esp_image_load_header_t, dram_dest_addr),
    offsetof(esp_image_load_header_t, dram_flash_offset),
    offsetof(esp_image_load_header_t, dram_size),
    SOC_IRAM_LOW, SOC_IRAM_HIGH, SOC_DRAM_LOW, SOC_DRAM_HIGH,
};

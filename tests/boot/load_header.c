/* Test-only target-header data translation; no C orchestration or policy. */
#include <stdint.h>
#include "esp_mcuboot_image.h"
_Static_assert(sizeof(esp_image_load_header_t) == 96, "qualified load-header ABI");
__attribute__((section(".load_header"), used))
const esp_image_load_header_t nxrs_load_header = {
    .header_magic = ESP_LOAD_HEADER_MAGIC,
    .entry_addr = NXRS_ENTRY,
    .iram_dest_addr = NXRS_IRAM_ADDRESS,
    .iram_flash_offset = NXRS_MCUBOOT_HEADER + sizeof(esp_image_load_header_t),
    .iram_size = NXRS_IRAM_BYTES,
    .dram_dest_addr = NXRS_DRAM_ADDRESS,
    .dram_flash_offset = NXRS_MCUBOOT_HEADER + sizeof(esp_image_load_header_t) + NXRS_IRAM_BYTES,
    .dram_size = NXRS_DRAM_BYTES,
};

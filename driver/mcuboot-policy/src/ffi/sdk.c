/* Private mechanical ABI access; compile with the actual boot SDK/config. */
#include <stddef.h>
#include <stdint.h>
#include <bootutil/boot_hooks.h>
#include <bootutil/sign_key.h>
#include <flash_map_backend/flash_map_backend.h>
#include <mbedtls/sha256.h>

#if defined(CONFIG_ESP_MCUBOOT_WDT_ENABLE) && !defined(CONFIG_BOOTLOADER_WDT_ENABLE)
#error "nxrs ESP32-S3 watchdog requires actual SDK watchdog enablement"
#endif

#if !defined(MCUBOOT_FIH_PROFILE_OFF) || defined(FIH_ENABLE_CFI) || defined(FIH_ENABLE_DOUBLE_VARS)
#error "nxrs Rust callback requires the qualified FIH-off ABI"
#endif
#if defined(MBEDTLS_SHA256_ALT) || defined(MBEDTLS_USE_PSA_CRYPTO)
#error "nxrs requires the synchronous software SHA-256 context"
#endif
_Static_assert(sizeof(fih_ret) == sizeof(int), "callback return ABI");
_Static_assert(sizeof(mbedtls_sha256_context) <= 256, "SHA storage capacity");
_Static_assert(_Alignof(mbedtls_sha256_context) <= 8, "SHA storage alignment");

size_t nxrs_boot_sha_size(void) { return sizeof(mbedtls_sha256_context); }
size_t nxrs_boot_sha_align(void) { return _Alignof(mbedtls_sha256_context); }
int nxrs_boot_key_count(void) { return bootutil_key_cnt; }
const unsigned char *nxrs_boot_key_data(void) { return bootutil_keys[0].key; }
unsigned int nxrs_boot_key_length(void) { return *bootutil_keys[0].len; }
uint32_t nxrs_boot_area_size(const struct flash_area *a) { return flash_area_get_size(a); }
uint32_t nxrs_boot_area_offset(const struct flash_area *a) { return flash_area_get_off(a); }

void nxrs_boot_watchdog_feed(void) { MCUBOOT_WATCHDOG_FEED(); }

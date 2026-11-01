/* SDK witness only; it exports no production C HAL interface. */
#include <bootutil/bootutil_public.h>
#include <nuttx/arch.h>
#include <stddef.h>
#include <sys/types.h>
#include <flash_map_backend/flash_map_backend.h>
_Static_assert(sizeof(void *) == 4 && sizeof(off_t) == 8, "SDK pointer/offset ABI");
_Static_assert(sizeof(struct flash_area) == 16 && _Alignof(struct flash_area) == 4 &&
               offsetof(struct flash_area, fa_id) == 0 && offsetof(struct flash_area, fa_size) == 8 &&
               offsetof(struct flash_area, fa_mtd_path) == 12, "Rust private flash area layout");
_Static_assert(sizeof(struct flash_sector) == 8 && offsetof(struct flash_sector, fs_size) == 4,
               "Rust private sector layout");
_Static_assert(__builtin_types_compatible_p(__typeof__(&flash_area_get_sector),
                 int (*)(const struct flash_area *, off_t, struct flash_sector *)), "sector ABI");
_Static_assert(__builtin_types_compatible_p(__typeof__(&flash_area_read),
                 int (*)(const struct flash_area *, uint32_t, void *, uint32_t)), "read ABI");
_Static_assert(__builtin_types_compatible_p(__typeof__(&flash_area_write),
                 int (*)(const struct flash_area *, uint32_t, const void *, uint32_t)), "write ABI");
_Static_assert(MCUBOOT_IMAGE_NUMBER == 1, "qualified single-image geometry");
#ifdef CONFIG_ESP32S3_OTA_PARTITION_ENCRYPT
_Static_assert(BOOT_MAX_ALIGN == 32 && CONFIG_ESP32S3_SPIFLASH_MTD_BLKSIZE == 32,
               "encrypted SDK layout and MTD program units must match");
#else
_Static_assert(BOOT_MAX_ALIGN == 8, "unencrypted trailer geometry");
#endif
_Static_assert(sizeof(int) == 4, "Rust c_int ABI");
/* Include the noreturn type attribute used by the target header. */
extern void reset_abi_expected(void) noreturn_function;
_Static_assert(__builtin_types_compatible_p(__typeof__(&up_systemreset), __typeof__(&reset_abi_expected)),
               "reset ABI");
_Static_assert(BOOT_SWAP_TYPE_NONE == 1 && BOOT_SWAP_TYPE_TEST == 2 &&
               BOOT_SWAP_TYPE_REVERT == 4, "MCUboot state binding drift");
_Static_assert(__builtin_types_compatible_p(__typeof__(&boot_swap_type_multi),
                                         int (*)(int)), "state ABI");
_Static_assert(__builtin_types_compatible_p(__typeof__(&boot_set_pending_multi),
                                         int (*)(int, int)), "trial ABI");
_Static_assert(__builtin_types_compatible_p(__typeof__(&boot_set_confirmed_multi),
                                         int (*)(int)), "confirm ABI");

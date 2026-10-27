/* Mechanical SDK continuation defaults for unrelated native callbacks. */
#include <bootutil/boot_hooks.h>
#include <bootutil/boot_public_hooks.h>
int boot_read_image_header_hook(int image, int slot, struct image_header *header)
{ (void)image; (void)slot; (void)header; return BOOT_HOOK_REGULAR; }
int boot_read_swap_state_primary_slot_hook(int image, struct boot_swap_state *state)
{ (void)image; (void)state; return BOOT_HOOK_REGULAR; }
int boot_perform_update_hook(int image, struct image_header *header, const struct flash_area *area)
{ (void)image; (void)header; (void)area; return BOOT_HOOK_REGULAR; }
int boot_copy_region_post_hook(int image, const struct flash_area *area, size_t size)
{ (void)image; (void)area; (void)size; return 0; }

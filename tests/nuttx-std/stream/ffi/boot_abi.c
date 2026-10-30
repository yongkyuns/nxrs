/* SDK witness only; it exports no production C HAL interface. */
#include <bootutil/bootutil_public.h>
_Static_assert(sizeof(int) == 4, "Rust c_int ABI");
_Static_assert(BOOT_SWAP_TYPE_NONE == 1 && BOOT_SWAP_TYPE_TEST == 2 &&
               BOOT_SWAP_TYPE_REVERT == 4, "MCUboot state binding drift");
_Static_assert(__builtin_types_compatible_p(__typeof__(&boot_swap_type_multi),
                                         int (*)(int)), "state ABI");
_Static_assert(__builtin_types_compatible_p(__typeof__(&boot_set_pending_multi),
                                         int (*)(int, int)), "trial ABI");
_Static_assert(__builtin_types_compatible_p(__typeof__(&boot_set_confirmed_multi),
                                         int (*)(int)), "confirm ABI");

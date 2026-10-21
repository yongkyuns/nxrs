/* SPDX-License-Identifier: MIT
 * Compile as target code with the selected, configured NuttX headers.
 * Only fixed-width/scalar values cross this private ABI. No fd ownership.
 */
#include <nuttx/config.h>
#include <stdint.h>
#include <stdbool.h>
#include <errno.h>
#include <sys/ioctl.h>
#include <nuttx/leds/userled.h>

_Static_assert(sizeof(userled_set_t) == sizeof(uint32_t),
               "USERLED mask no longer matches the 32-bit LED capability");

/* Return 0 on success, otherwise errno captured at the failing ioctl. */
int nxrs_userled_supported(int fd, uint32_t *supported)
{
  userled_set_t native = 0;
  int result = ioctl(fd, ULEDIOC_SUPPORTED,
                     (unsigned long)(uintptr_t)&native);
  if (result < 0)
    {
      return errno;
    }

  *supported = (uint32_t)native;
  return 0;
}

int nxrs_userled_set(int fd, uint8_t index, int on)
{
  struct userled_s request = {.ul_led = index, .ul_on = on != 0};
  int result = ioctl(fd, ULEDIOC_SETLED,
                     (unsigned long)(uintptr_t)&request);
  return result < 0 ? errno : 0;
}

/* SPDX-License-Identifier: MIT
 * Core-only NuttX simulator time helper. Std-based integration uses std::time
 * and std::thread::sleep directly and does not call these functions.
 */
#include <nuttx/config.h>
#include <errno.h>
#include <stdint.h>
#include <time.h>
#include "bridge.h"
#include "status.h"

int rc_test_now_ms(uint64_t *value)
{
  struct timespec now;
  if (clock_gettime(CLOCK_MONOTONIC, &now) < 0) return rc_errno(errno);
  *value = (uint64_t)now.tv_sec * 1000 + (uint64_t)now.tv_nsec / 1000000;
  return 0;
}

int rc_test_sleep_ms(uint32_t milliseconds)
{
  struct timespec delay;
  delay.tv_sec = milliseconds / 1000;
  delay.tv_nsec = (milliseconds % 1000) * 1000000;
  while (nanosleep(&delay, &delay) < 0)
    if (errno != EINTR) return rc_errno(errno);
  return 0;
}

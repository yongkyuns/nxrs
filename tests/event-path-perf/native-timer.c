/* SPDX-License-Identifier: MIT */
/* P2-only absolute sem_clockwait ABI translation. Rust owns deadlines,
 * sequencing, cancellation, payload construction, handlers and all lifecycle. */
#ifndef __NuttX__
#  define _GNU_SOURCE 1
#endif
#include <errno.h>
#include <semaphore.h>
#include <stdint.h>
#include <time.h>

int event_path_timer_wait_abs(void *storage, uint64_t absolute_ns, int *expired)
{
  const uint64_t seconds = absolute_ns / UINT64_C(1000000000);
  struct timespec deadline;
  int error;

  if (storage == NULL || expired == NULL) return EINVAL;
  deadline.tv_sec = (time_t)seconds;
  deadline.tv_nsec = (long)(absolute_ns % UINT64_C(1000000000));
  if ((uint64_t)deadline.tv_sec != seconds) return EOVERFLOW;
  *expired = 0;

  /* Exactly one wait. A posted cancellation token is not a timer expiry.
   * Signals and other errors are returned to Rust without retrying. */
  if (sem_clockwait((sem_t *)storage, CLOCK_MONOTONIC, &deadline) == 0)
    {
      return 0;
    }
  error = errno;
  if (error == ETIMEDOUT)
    {
      *expired = 1;
      return 0;
    }
  return error;
}

/* Qualification-only clock controls. All measurement loops live in Rust.
 * Compile with the image's NuttX headers, alongside memory-timing's APB timer.
 */
#include <nuttx/config.h>
#include <nuttx/clock.h>
#include <errno.h>
#include <stdint.h>
#include <sys/time.h>
#include <time.h>

#if defined(CONFIG_CLOCK_TIMEKEEPING) && !defined(CONFIG_CLOCK_ADJTIME)
/* The pinned software implementation is compiled, but its declaration and
 * protected syscall are not admitted by CLOCK_ADJTIME. This explicit test-only
 * call evaluates that implementation; it does not establish a supported API.
 */
extern int adjtime(const struct timeval *delta, struct timeval *olddelta);
#endif

extern clock_t __wrap_up_perf_gettime(void);
extern unsigned long __wrap_up_perf_getfreq(void);

uint64_t clock_probe_reference_ns(void)
{
  return (uint64_t)(uint32_t)__wrap_up_perf_gettime() *
         (1000000000ul / __wrap_up_perf_getfreq());
}

unsigned int clock_probe_features(void)
{
  unsigned int flags = 0;
#ifdef CONFIG_CLOCK_TIMEKEEPING
  flags |= 1;
#endif
#ifdef CONFIG_CLOCK_ADJTIME
  flags |= 2;
#endif
#ifdef CONFIG_RTC
  flags |= 4;
#endif
#ifdef CONFIG_RTC_HIRES
  flags |= 8;
#endif
  return flags;
}

unsigned int clock_probe_tick_us(void)
{
  return CONFIG_USEC_PER_TICK;
}

int64_t clock_probe_resolution_ns(void)
{
  struct timespec value;
  if (clock_getres(CLOCK_REALTIME, &value) < 0)
    return -errno;
  return (int64_t)value.tv_sec * 1000000000ll + value.tv_nsec;
}

int clock_probe_set(int64_t nanoseconds, int *error)
{
  struct timespec value =
  {
    .tv_sec = nanoseconds / 1000000000ll,
    .tv_nsec = nanoseconds % 1000000000ll
  };
  int result;
  errno = 0;
  result = clock_settime(CLOCK_REALTIME, &value);
  *error = errno;
  return result;
}

int clock_probe_adjust(int present, int64_t microseconds,
                       int64_t *old_seconds, int64_t *old_microseconds,
                       int *error)
{
#if defined(CONFIG_CLOCK_TIMEKEEPING) || defined(CONFIG_CLOCK_ADJTIME)
  const struct timeval delta =
  {
    .tv_sec = microseconds / 1000000ll,
    .tv_usec = microseconds % 1000000ll
  };
  struct timeval old = {.tv_sec = 1234567, .tv_usec = 7654321};
  int result;
  errno = 0;
  result = adjtime(present ? &delta : NULL, &old);
  *error = errno;
  *old_seconds = old.tv_sec;
  *old_microseconds = old.tv_usec;
  return result;
#else
  (void)present;
  (void)microseconds;
  *old_seconds = 1234567;
  *old_microseconds = 7654321;
  *error = ENOSYS;
  return -1;
#endif
}

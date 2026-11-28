#include <nuttx/config.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <sys/time.h>
#include <time.h>

_Static_assert(sizeof(time_t) == sizeof(int64_t) && (time_t)-1 < 0,
               "time control requires the pinned signed 64-bit NuttX time_t");

static int time_status(int status)
{
  if (status < 0)
    {
      if (errno == ENOSYS || errno == ENOTSUP)
        return 1;
      if (errno == EINVAL || errno == ERANGE || errno == EOVERFLOW)
        return 2;
    }

  return status;
}

int time_control_capabilities(uint32_t *slew_rate_ppm)
{
#if defined(CONFIG_RTC_HIRES)
  (void)slew_rate_ppm;
  return 1;
#else
#  if defined(CONFIG_CLOCK_TIMEKEEPING) && defined(CONFIG_CLOCK_ADJTIME)
  *slew_rate_ppm = 500;
#  else
  *slew_rate_ppm = 0;
#  endif
  return 0;
#endif
}

int time_set_realtime(int64_t seconds, uint32_t nanoseconds)
{
  const struct timespec value = {
    .tv_sec = seconds,
    .tv_nsec = nanoseconds
  };

  return time_status(clock_settime(CLOCK_REALTIME, &value));
}

int time_adjust_realtime(bool replace, int64_t seconds, int32_t microseconds,
                         int64_t *previous_seconds,
                         int32_t *previous_microseconds)
{
#if defined(CONFIG_CLOCK_TIMEKEEPING) && defined(CONFIG_CLOCK_ADJTIME) && \
    !defined(CONFIG_RTC_HIRES)
  const struct timeval delta = {
    .tv_sec = seconds,
    .tv_usec = microseconds
  };
  struct timeval previous = {0};
  int status = adjtime(replace ? &delta : NULL, &previous);
  if (status < 0)
    return time_status(status);

  *previous_seconds = previous.tv_sec;
  *previous_microseconds = previous.tv_usec;
  return 0;
#else
  (void)replace;
  (void)seconds;
  (void)microseconds;
  (void)previous_seconds;
  (void)previous_microseconds;
  return 1;
#endif
}

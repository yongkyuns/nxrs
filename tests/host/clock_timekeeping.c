/* Deterministic raw timer only; never calls or changes a host OS clock. */
#include "clock_timekeeping.h"
static uint64_t test_counter;
static uint64_t test_mask = UINT64_MAX;
static int test_error;
void clock_probe_test_timer(uint64_t counter, uint64_t mask, int error)
{
  test_counter = counter;
  test_mask = mask;
  test_error = error;
}
int up_timer_gettick(clock_t *counter)
{
  if (test_error != 0)
    return -test_error;
  *counter = (clock_t)test_counter;
  return 0;
}
void up_timer_getmask(clock_t *mask)
{
  *mask = (clock_t)test_mask;
}
int clock_basetime(struct timespec *time)
{
  time->tv_sec = 0;
  time->tv_nsec = 0;
  return 0;
}
void clock_notifier_call_chain(int clockid, const struct timespec *time)
{
  (void)clockid;
  (void)time;
}

bool g_rtc_enabled = true;
static unsigned int rtc_writes;
unsigned int clock_probe_test_rtc_writes(void)
{
  return rtc_writes;
}
int up_rtc_settime(const struct timespec *time)
{
  (void)time;
  rtc_writes++;
  return 0;
}
void clock_systime_timespec(struct timespec *time)
{
  uint64_t nanoseconds = (test_counter & test_mask) * NSEC_PER_TICK;
  time->tv_sec = nanoseconds / NSEC_PER_SEC;
  time->tv_nsec = nanoseconds % NSEC_PER_SEC;
}

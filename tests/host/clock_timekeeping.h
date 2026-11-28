/* Mechanical host dependencies for actual NuttX software clock sources.
 * These host types do not qualify the target ABI or interrupt execution.
 */
#ifndef CLOCK_TIMEKEEPING_WITNESS_H
#define CLOCK_TIMEKEEPING_WITNESS_H
#include <assert.h>
#include <errno.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <sys/time.h>
#include <time.h>
/* Rename only after the host type declarations: their attributes are not
 * the NuttX public contract and must not affect compiled wrapper checks.
 */
#define adjtime clock_probe_test_adjtime
#define nxclock_gettime clock_probe_test_nxgettime
#define clock_gettime clock_probe_test_gettime
#define clock_settime clock_probe_test_settime
#define CONFIG_CLOCK_TIMEKEEPING 1
#define CONFIG_SCHED_CRITMONITOR_MAXTIME_THREAD -1
#define CONFIG_RTC 1
#define FAR
#define OK 0
#define ERROR (-1)
#ifndef CLOCK_BOOTTIME
#define CLOCK_BOOTTIME 7
#endif
#define USEC_PER_SEC 1000000L
#define NSEC_PER_USEC 1000L
#define NSEC_PER_SEC 1000000000L
#define NSEC_PER_TICK (WITNESS_TICK_US * NSEC_PER_USEC)
#define DEBUGASSERT assert
#define set_errno(value) (errno = (value))
typedef int irqstate_t;
typedef int spinlock_t;
#define SP_UNLOCKED 0
static inline irqstate_t spin_lock_irqsave(spinlock_t *lock)
{
  assert(*lock == 0);
  *lock = 1;
  return 0;
}
static inline void spin_unlock_irqrestore(spinlock_t *lock, irqstate_t flags)
{
  (void)flags;
  assert(*lock == 1);
  *lock = 0;
}
int up_timer_gettick(clock_t *counter);
void up_timer_getmask(clock_t *mask);
int clock_basetime(struct timespec *time);
void clock_notifier_call_chain(int clockid, const struct timespec *time);
void clock_systime_timespec(struct timespec *time);
int clock_timekeeping_get_wall_time(struct timespec *time);
int clock_timekeeping_set_wall_time(const struct timespec *time);
int up_rtc_settime(const struct timespec *time);
extern bool g_rtc_enabled;
#endif

/* Qualification-only adapter to an actual free-running emulated APB timer.
 * QEMU v10.0 hw/arm/armsse.c maps CMSDK timer1 at 0x40001000 with its secure
 * alias at 0x50001000; hw/arm/mps2-tz.c supplies AN521 MAINCLK at 20 MHz.
 * hw/timer/cmsdk-apb-timer.c defines CTRL/VALUE/RELOAD and the peripheral ID.
 * This is not a physical CPU cycle measurement. Its 32-bit count wraps after
 * 214.7483648 seconds; the qualification window must finish before that.
 * Link with --wrap=up_perf_gettime --wrap=up_perf_getfreq
 * --wrap=up_perf_convert; retain those flags in the build evidence.
 */
#include <nuttx/config.h>
#include <nuttx/arch.h>
#include <nuttx/clock.h>
#include <stdbool.h>
#include <stdint.h>

#define TIMER_BASE 0x50001000u
#define TIMER_FREQ 20000000ul
#define TIMER_REG(offset) (*(volatile uint32_t *)(TIMER_BASE + (offset)))

static volatile bool g_clock_ready;

int timing_clock_initialize(void)
{
  /* Refuse an unexpected device or an already-running timer. Only this test
   * owns timer1; the normal scheduler continues to use SysTick unchanged.
   */
  if (TIMER_REG(0) != 0 || (TIMER_REG(0xfe0) & 0xff) != 0x22 ||
      (TIMER_REG(0xfe4) & 0xff) != 0xb8)
    {
      return -1;
    }
  TIMER_REG(0x0c) = 1;
  TIMER_REG(0x08) = UINT32_MAX;
  TIMER_REG(0x00) = 1; /* Enable; IRQ and external clock/gate remain disabled. */
  g_clock_ready = true;
  return 0;
}

void timing_clock_stop(void)
{
  TIMER_REG(0) = 0;
  g_clock_ready = false;
}

clock_t __wrap_up_perf_gettime(void)
{
  return g_clock_ready ? (clock_t)(UINT32_MAX - TIMER_REG(0x04)) : 0;
}

unsigned long __wrap_up_perf_getfreq(void)
{
  return TIMER_FREQ;
}

void __wrap_up_perf_convert(clock_t elapsed, struct timespec *ts)
{
  ts->tv_sec = elapsed / TIMER_FREQ;
  ts->tv_nsec = (elapsed % TIMER_FREQ) * (NSEC_PER_SEC / TIMER_FREQ);
}

/* Actual SDK clock-init prefix: no RTC clock, deadline or feed replacement. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>

#define CONFIG_BOOTLOADER_WDT_ENABLE 1
#define CONFIG_BOOTLOADER_WDT_TIME_MS 9000
#define SOC_XTAL_FREQ_40M 40
#define RTC_XTAL_FREQ_40M 40
#define SOC_RTC_FAST_CLK_SRC_RC_FAST 1
#define WDT_RWDT 1
#define WDT_STAGE0 0
#define WDT_STAGE_ACTION_RESET_RTC 4
#define SLOW_CLK_RTC 0
#define CLK_CAL_RTC_SLOW 0
#define RTC_CLK_CAL_FRACT 19
#ifndef SLOW_CLK_CAL_CYCLES
#define SLOW_CLK_CAL_CYCLES 1024
#endif
typedef struct { int inst; int *rwdt_dev; } wdt_hal_context_t;
int RTCCNTL;
static unsigned feeds, stages, clock_changes;
static unsigned measurements, stored_cal = 77;
static bool failed;
int rtc_clk_xtal_freq_get(void) { return 40; }
bool rtc_clk_8md256_enabled(void) { return false; }
void rtc_clk_8m_enable(bool enabled, bool divided) { assert(enabled); (void)divided; }
void rtc_clk_fast_src_set(int source) { assert(source == SOC_RTC_FAST_CLK_SRC_RC_FAST); }
unsigned rtc_clk_slow_freq_get_hz(void) { return failed ? 0 : 136000; }
unsigned rtc_clk_cal(int source, unsigned cycles)
{ assert(source == CLK_CAL_RTC_SLOW && cycles > 0); ++measurements; return failed ? 0 : 12345; }
void esp_clk_slowclk_cal_set(unsigned cal) { assert(cal > 0); stored_cal = cal; }
void select_rtc_slow_clk(int source) { assert(source == SLOW_CLK_RTC); ++clock_changes; }
void wdt_hal_write_protect_disable(wdt_hal_context_t *ctx) { (void)ctx; }
void wdt_hal_write_protect_enable(wdt_hal_context_t *ctx) { (void)ctx; }
void wdt_hal_feed(wdt_hal_context_t *ctx) { (void)ctx; ++feeds; }
void wdt_hal_config_stage(wdt_hal_context_t *ctx, int stage, unsigned ticks, int action)
{ (void)ctx; assert(stage == 0 && ticks > 0 && action == 4); ++stages; }

#include "clock.c"

int main(int argc, char **argv)
{
  (void)argv;
  failed = argc > 1;
  esp_clk_init();
#ifdef CONFIG_ESP32S3_RWDT_INHERIT
  unsigned expected = SLOW_CLK_CAL_CYCLES > 0 ? 12345 :
                      (unsigned)(((1ULL << RTC_CLK_CAL_FRACT) * 1000000) / 136000);
  return feeds != 0 || stages != 0 || clock_changes != 0 ||
         measurements != (SLOW_CLK_CAL_CYCLES > 0) || stored_cal != expected;
#else
  return feeds != 2 || stages != 2 || clock_changes != 1;
#endif
}

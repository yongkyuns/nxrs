/* Protected-register model for the exact pinned NuttX early handoff function. */
#include <stdbool.h>
#include <stdint.h>

#define RTC_CNTL_WDTCONFIG0_REG 0
#define RTC_CNTL_WDTWPROTECT_REG 1
#define RTC_CNTL_WDT_EN (1u << 31)
#define KEY 0x50d83aa1u
static uint32_t config;
static bool locked, denied;
int RTCCNTL;

uint32_t getreg32(unsigned address) { (void)address; return config; }
void putreg32(uint32_t value, unsigned address)
{
  if (address == RTC_CNTL_WDTWPROTECT_REG) locked = value != KEY;
  else if (locked) denied = true;
  else config = value;
}
void rwdt_ll_write_protect_disable(int *device)
{ (void)device; putreg32(KEY, RTC_CNTL_WDTWPROTECT_REG); }
void rwdt_ll_disable(int *device)
{ (void)device; putreg32(config & ~RTC_CNTL_WDT_EN, RTC_CNTL_WDTCONFIG0_REG); }
void rwdt_ll_write_protect_enable(int *device)
{ (void)device; putreg32(0, RTC_CNTL_WDTWPROTECT_REG); }

#include "early.c"

int main(void)
{
  for (unsigned state = 0; state < 4; ++state) {
    config = 0x1234567u | ((state & 1) ? RTC_CNTL_WDT_EN : 0);
    locked = (state & 2) != 0;
    denied = false;
    for (unsigned repeat = 0; repeat < 2; ++repeat) {
      esp32s3_wdt_early_deinit();
      if (config != 0x1234567u || !locked || denied) return 1;
    }
  }
  return 0;
}

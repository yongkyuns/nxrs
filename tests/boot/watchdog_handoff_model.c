/* Test-only protected-register model; no hardware or target ABI claim. */
#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include "constants.h"

typedef struct { uint32_t config; bool locked; unsigned denied; char trace[16]; unsigned count; } rwdt_dev_t;
static rwdt_dev_t device;
#define RWDT_DEV_GET() (&device)

uint32_t getreg32(uintptr_t address)
{
  if (address != RTC_CNTL_WDTCONFIG0_REG) return 0;
  return device.config;
}
void putreg32(uint32_t value, uintptr_t address)
{
  if (device.count >= 15) return;
  if (address == RTC_CNTL_WDTWPROTECT_REG) {
    device.locked = value != RWDT_LL_WDT_WKEY_VALUE;
    device.trace[device.count++] = device.locked ? 'L' : 'U';
  } else if (address == RTC_CNTL_WDTCONFIG0_REG) {
    if (device.locked) { device.denied++; return; }
    device.config = value;
    device.trace[device.count++] = 'D';
  }
}
void rwdt_ll_write_protect_disable(rwdt_dev_t *dev)
{ (void)dev; putreg32(RWDT_LL_WDT_WKEY_VALUE, RTC_CNTL_WDTWPROTECT_REG); }
void rwdt_ll_disable(rwdt_dev_t *dev)
{ (void)dev; putreg32(device.config & ~RTC_CNTL_WDT_EN, RTC_CNTL_WDTCONFIG0_REG); }
void rwdt_ll_write_protect_enable(rwdt_dev_t *dev)
{ (void)dev; putreg32(0, RTC_CNTL_WDTWPROTECT_REG); }

#include "early-function.c"

int main(void)
{
  for (unsigned scenario = 0; scenario < 4; scenario++) {
    memset(&device, 0, sizeof(device));
    uint32_t unrelated = 0x1234567u & ~RTC_CNTL_WDT_EN;
    device.config = unrelated | ((scenario & 1) ? 0 : RTC_CNTL_WDT_EN);
    device.locked = !(scenario & 2);
    esp32s3_wdt_early_deinit();
    if (device.config & RTC_CNTL_WDT_EN) {
      puts("WDT_HANDOFF_PROTECTED_WRITE_REFUSED"); return 1;
    }
    if (device.config != unrelated || !device.locked || device.denied || strcmp(device.trace, "UDL")) return 2;
    /* Repeated handoff must preserve unrelated bits and protection. */
    esp32s3_wdt_early_deinit();
    if (device.config != unrelated || !device.locked || device.denied || strcmp(device.trace, "UDLUDL")) return 3;
  }
  puts("WDT_HANDOFF_MODEL_PASSED 4");
  return 0;
}

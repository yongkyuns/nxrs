/* Actual SDK header constants, compiled separately and never linked into firmware. */
#include <stdint.h>
#include "sdkconfig.h"
#include "soc/soc.h"
#include "soc/rtc_cntl_reg.h"
#include "hal/wdt_types.h"
#include "hal/misc.h"
#include "hal/rwdt_ll.h"
#if !CONFIG_ESP_MCUBOOT_WDT_ENABLE || !CONFIG_BOOTLOADER_WDT_ENABLE
#error "both product and actual SDK watchdog configuration are required"
#endif
const uint32_t nxrs_runtime_layout[] = {
    SOC_ROM_STACK_START,
    SOC_ROM_STACK_SIZE,
    CONFIG_BOOTLOADER_WDT_TIME_MS,
    RTC_CNTL_WDTCONFIG0_REG,
    RTC_CNTL_WDT_EN_M,
    RTC_CNTL_WDT_STG0_M,
    RTC_CNTL_WDT_STG0_S,
    WDT_STAGE_ACTION_RESET_RTC,
    RTC_CNTL_WDTCONFIG1_REG,
    WDT_RWDT,
    RTC_CNTL_WDTWPROTECT_REG,
    RWDT_LL_WDT_WKEY_VALUE,
};

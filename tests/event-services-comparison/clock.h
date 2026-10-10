/* SPDX-License-Identifier: MIT
 * ESP32-S3 wall clock shared by both C RTOS adapters. Register definitions:
 * Espressif soc/esp32s3/include/soc/{reg_base,systimer_reg}.h. No timer value
 * or alarm is reset; only counter0's CPU-stall policy is made explicit.
 */
#ifndef NXRS_EVENT_SERVICES_CLOCK_H
#define NXRS_EVENT_SERVICES_CLOCK_H
#include <stdint.h>
#ifdef __XTENSA__
#define ES_SYSTIMER_CONF ((volatile uint32_t *)0x60023000u)
#define ES_SYSTIMER_UNIT0_OP ((volatile uint32_t *)0x60023004u)
#define ES_SYSTIMER_UNIT0_LO ((volatile uint32_t *)0x60023044u)
static inline void es_clock_prepare(void) {
  uint32_t previous;
  __asm__ volatile("rsil %0, 5" : "=a"(previous)::"memory");
  *ES_SYSTIMER_CONF =
      (*ES_SYSTIMER_CONF | (1u << 30)) & ~((1u << 28) | (1u << 27));
  __asm__ volatile("wsr.ps %0\nrsync" ::"a"(previous) : "memory");
}
static inline uint32_t es_clock_cycles(void) {
  *ES_SYSTIMER_UNIT0_OP = 1u << 30;
  while (!(*ES_SYSTIMER_UNIT0_OP & (1u << 29))) {
  }
  // Only low32 is needed: multiplying by 15 preserves modulo-u32 elapsed
  // arithmetic. Another accessor may refresh this shared latch; that merely
  // advances the observed timestamp, without combining mismatched high/low.
  return *ES_SYSTIMER_UNIT0_LO * 15u;
}
#endif
#endif

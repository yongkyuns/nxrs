/* SPDX-License-Identifier: MIT
 * Functional test double only. Never used for device or latency evidence. */
#include <stdlib.h>
static int open_led;
int es_hal_init(void) {
  if (getenv("SQ_FAIL_LED_OPEN") || open_led) return -1;
  open_led = 1;
  return 0;
}
int es_hal_apply(unsigned level) {
  return !open_led || level > 1 || getenv("SQ_FAIL_LED_APPLY") ? -1 : 0;
}
int es_hal_close(void) { open_led = 0; return 0; }

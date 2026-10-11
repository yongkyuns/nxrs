/* Test actual pinned and patched interrupt-state functions. No hardware. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#define OK 0
typedef unsigned irqstate_t;
typedef struct {
  unsigned gpio_intr;
  bool irq_enabled, transferring;
} rp23xx_gspi_t;
typedef struct { void *io_dev; } gspi_dev_t;
static bool enabled[2];
#ifndef EXPECT_BASELINE_BUG
static unsigned critical;
static irqstate_t enter_critical_section(void) { assert(critical++ == 0); return 0; }
static void leave_critical_section(irqstate_t f) { (void)f; assert(critical-- == 1); }
#endif
static void rp23xx_gpio_enable_irq(unsigned pin) {
#ifndef EXPECT_BASELINE_BUG
  assert(critical == 1);
#endif
  enabled[pin] = true;
}
static void rp23xx_gpio_disable_irq(unsigned pin) {
#ifndef EXPECT_BASELINE_BUG
  assert(critical == 1);
#endif
  enabled[pin] = false;
}
#include "cyw43439-irq-functions.h"
#ifdef EXPECT_BASELINE_BUG
static void my_transfer_irq(gspi_dev_t *gspi, bool active) {
  my_interrupt_enable(gspi, !active); /* Original transfer call sites. */
}
#endif
int main(void) {
  rp23xx_gspi_t io[2] = {{.gpio_intr = 0}, {.gpio_intr = 1}};
  gspi_dev_t devices[2] = {{&io[0]}, {&io[1]}};
  my_interrupt_enable(&devices[0], true);
  my_interrupt_enable(&devices[0], true); /* Unpaired, valid repeated enable. */
  my_transfer_irq(&devices[0], true);
  assert(!enabled[0]);
  my_interrupt_enable(&devices[0], false); /* Daemon ISR requests masking. */
  my_transfer_irq(&devices[0], false);
#ifdef EXPECT_BASELINE_BUG
  assert(enabled[0]); /* Witness: transfer cleanup overrode the ISR request. */
#else
  assert(!enabled[0]);
  my_transfer_irq(&devices[0], true);
  my_interrupt_enable(&devices[0], true);
  assert(!enabled[0]); /* Re-enable must wait for shared-data transfer end. */
  my_transfer_irq(&devices[0], false);
  assert(enabled[0]);
  my_interrupt_enable(&devices[0], false);
  my_transfer_irq(&devices[1], true);
  my_interrupt_enable(&devices[1], true);
  assert(!enabled[1] && !enabled[0]); /* State is per device. */
  my_transfer_irq(&devices[1], false);
  assert(enabled[1] && !enabled[0] && critical == 0);
#endif
  puts("CYW43439 IRQ state witness: PASS");
  return 0;
}

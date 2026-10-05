/* SPDX-License-Identifier: MIT */
#include "hal.h"

#if defined(__ZEPHYR__)
#include "platform.h"

#include <zephyr/device.h>
#include <zephyr/devicetree.h>
#include <zephyr/drivers/gpio.h>

#define ES_LED_PIN 2u
#define ES_SETTLE_CYCLES 240u

static const struct device *gpio0;
static int initialized;

static void settle_one_us(void) {
  uint32_t start = es_platform_cycles();
  while ((uint32_t)(es_platform_cycles() - start) < ES_SETTLE_CYCLES) {
  }
}

int es_hal_init(void) {
  if (initialized) {
    return -1;
  }

  gpio0 = DEVICE_DT_GET(DT_NODELABEL(gpio0));
  if (!device_is_ready(gpio0) ||
      gpio_pin_configure(gpio0, ES_LED_PIN,
                         GPIO_INPUT | GPIO_OUTPUT | GPIO_OUTPUT_INACTIVE) < 0) {
    gpio0 = NULL;
    return -1;
  }

  initialized = 1;
  if (es_hal_apply(0) < 0) {
    (void)es_hal_close();
    return -1;
  }
  return 0;
}

int es_hal_apply(unsigned level) {
  if (!initialized || level > 1 ||
      gpio_pin_set(gpio0, ES_LED_PIN, (int)level) < 0) {
    return -1;
  }

  settle_one_us();
  int actual = gpio_pin_get(gpio0, ES_LED_PIN);
  return actual < 0 || (unsigned)actual != level ? -1 : 0;
}

int es_hal_close(void) {
  if (!initialized) {
    return 0;
  }
  int result = es_hal_apply(0);
  initialized = 0;
  gpio0 = NULL;
  return result;
}

#else
/* Keep host-only builds independent of platform headers and hardware APIs. */
int es_hal_init(void) { return -1; }
int es_hal_apply(unsigned level) {
  (void)level;
  return -1;
}
int es_hal_close(void) { return -1; }
#endif

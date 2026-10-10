/* SPDX-License-Identifier: MIT */
#include "hal.h"

#if defined(__NuttX__)
#include "platform.h"

#include <fcntl.h>
#include <nuttx/leds/userled.h>
#include <stdint.h>
#include <sys/ioctl.h>
#include <unistd.h>

#define ES_LED_BIT ((userled_set_t)1u)
#define ES_SETTLE_CYCLES 240u

static int led_fd = -1;

static void settle_one_us(void) {
  uint32_t start = es_platform_cycles();
  while ((uint32_t)(es_platform_cycles() - start) < ES_SETTLE_CYCLES) {
  }
}

static int read_led_level(unsigned *level) {
  userled_set_t state = 0;
  if (ioctl(led_fd, ULEDIOC_GETALL, (unsigned long)(uintptr_t)&state) < 0) {
    return -1;
  }
  *level = (state & ES_LED_BIT) != 0;
  return 0;
}

int es_hal_init(void) {
  if (led_fd >= 0) {
    return -1;
  }

  int fd = open("/dev/userleds", O_RDWR);
  if (fd < 0) {
    return -1;
  }

  userled_set_t supported = 0;
  if (ioctl(fd, ULEDIOC_SUPPORTED, (unsigned long)(uintptr_t)&supported) < 0 ||
      (supported & ES_LED_BIT) == 0) {
    (void)close(fd);
    return -1;
  }

  led_fd = fd;
  if (es_hal_apply(0) < 0) {
    (void)es_hal_close();
    return -1;
  }
  return 0;
}

int es_hal_apply(unsigned level) {
  if (led_fd < 0 || level > 1) {
    return -1;
  }

  userled_set_t state = level ? ES_LED_BIT : 0;
  if (ioctl(led_fd, ULEDIOC_SETALL, (unsigned long)state) < 0) {
    return -1;
  }

  settle_one_us();
  unsigned actual = 0;
  if (read_led_level(&actual) < 0 || actual != level) {
    return -1;
  }
  return 0;
}

int es_hal_close(void) {
  if (led_fd < 0) {
    return 0;
  }

  int result = 0;
  if (ioctl(led_fd, ULEDIOC_SETALL, (unsigned long)0) < 0) {
    result = -1;
  }
  settle_one_us();
  unsigned actual = 1;
  if (read_led_level(&actual) < 0 || actual != 0) {
    result = -1;
  }

  int fd = led_fd;
  led_fd = -1;
  if (close(fd) < 0) {
    result = -1;
  }
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

/* SPDX-License-Identifier: MIT */
#include "runtime.h"
#include <stdio.h>
#include <zephyr/device.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/kernel.h>
int main(void) {
  const struct device *uart = DEVICE_DT_GET(DT_CHOSEN(zephyr_console));
  if (!device_is_ready(uart))
    return 1;
  puts("EVENT_SERVICES_READY platform=zephyr-c");
  for (;;) {
    char command[16];
    unsigned length = 0;
    unsigned char byte;
    printf("event> ");
    for (;;) {
      if (uart_poll_in(uart, &byte)) {
        k_msleep(1);
        continue;
      }
      if (byte == '\r' || byte == '\n')
        break;
      if (length < sizeof(command) - 1)
        command[length++] = byte;
    }
    command[length] = 0;
    if (!length)
      continue;
    const char *argv[] = {"es_c", command};
    int status = es_run_main(2, argv);
    printf("ES_COMMAND_EXIT status=%d\n", status);
    if (status) {
      puts("EVENT_SERVICES_FAILED");
      k_sleep(K_FOREVER);
    }
  }
}

/* SPDX-License-Identifier: MIT */
#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/uart.h>
#include <stdio.h>
#include <string.h>
#include "memory.h"
#ifndef NXRS_BASELINE
#include "native_adapter.h"
extern int nxrs_c_scale_control_main(int, char **);
#endif

int main(void)
{
  const struct device *uart = DEVICE_DT_GET(DT_CHOSEN(zephyr_console));
  if (!device_is_ready(uart)) return 1;
  printf("ZEPHYR_COMPARISON_READY mode=%s cpu_hz=%u main_stack=%u irq_stack=%u\n",
         NXRS_MODE, CONFIG_SYS_CLOCK_HW_CYCLES_PER_SEC, CONFIG_MAIN_STACK_SIZE,
         CONFIG_ISR_STACK_SIZE);
  for (;;) {
    char command[16]; unsigned length = 0; unsigned char byte;
    printf("zephyr> ");
    for (;;) {
      if (uart_poll_in(uart, &byte)) { k_msleep(1); continue; }
      if (byte == '\r' || byte == '\n') break;
      if (length < sizeof(command) - 1) command[length++] = byte;
    }
    command[length] = 0;
    int rc = 0;
#ifdef NXRS_BASELINE
    if (strcmp(command, "baseline") == 0) puts("ZEPHYR_BASELINE_PASS");
    else rc = 1;
#else
    if (strcmp(command, "large") == 0 || strcmp(command, "threads") == 0) {
      nxrs_adapter_reset();
      char *argv[] = {"cq_scale", command};
      rc = nxrs_c_scale_control_main(2, argv);
      nxrs_adapter_report();
    } else rc = 1;
#endif
    nxrs_memory_report();
    printf("ZEPHYR_COMMAND_EXIT status=%d\n", rc);
    if (rc) {
      /* Startup/validation failures are terminal; do not reuse live resources. */
      puts("ZEPHYR_COMPARISON_FAILED");
      k_sleep(K_FOREVER);
    }
  }
}

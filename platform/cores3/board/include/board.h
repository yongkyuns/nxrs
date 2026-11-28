/* SPDX-License-Identifier: Apache-2.0 */
#ifndef __BOARDS_ESP32S3_M5_CORES3_BOARD_H
#define __BOARDS_ESP32S3_M5_CORES3_BOARD_H
#include <nuttx/config.h>
#define BOARD_XTAL_FREQUENCY 40000000
#define BOARD_CLOCK_FREQUENCY (CONFIG_ESP32S3_DEFAULT_CPU_FREQ_MHZ * 1000000)
#if defined(CONFIG_I2C_DRIVER) && !defined(__ASSEMBLY__)
int board_i2c_init(void);
#endif
#endif

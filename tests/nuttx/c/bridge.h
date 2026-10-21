/* SPDX-License-Identifier: MIT
 * Core-only NuttX fixture declarations. Production capability bridges live
 * with camera/storage; ordinary std time needs no bridge.
 */
#ifndef NXRS_NUTTX_TEST_BRIDGE_H
#define NXRS_NUTTX_TEST_BRIDGE_H
#include <stddef.h>
#include <stdint.h>
#include "camera.h"
#include "status.h"
#include "storage.h"

int rc_test_now_ms(uint64_t *);
int rc_test_sleep_ms(uint32_t);

#endif

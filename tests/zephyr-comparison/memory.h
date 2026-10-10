/* SPDX-License-Identifier: MIT */
#ifndef NXRS_ZEPHYR_MEMORY_H
#define NXRS_ZEPHYR_MEMORY_H
#include <stdint.h>
uint32_t nxrs_cq_heap_used(void);
void nxrs_memory_report(void);
#endif

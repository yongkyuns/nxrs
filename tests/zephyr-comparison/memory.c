/* SPDX-License-Identifier: MIT */
#include "memory.h"
#include <zephyr/kernel.h>
#include <zephyr/sys/sys_heap.h>
#include <stdio.h>

/* The pinned ESP32 board reserves a system heap even when the application's
 * requested pool size is zero. Account for its actual boot allocations too. */
extern struct k_heap _system_heap;
static struct sys_memory_stats stats(void)
{
  struct sys_memory_stats out;
  k_spinlock_key_t key = k_spin_lock(&_system_heap.lock);
  int rc = sys_heap_runtime_stats_get(&_system_heap.heap, &out);
  k_spin_unlock(&_system_heap.lock, key);
  __ASSERT_NO_MSG(rc == 0);
  return out;
}
uint32_t nxrs_cq_heap_used(void) { return stats().allocated_bytes; }
void nxrs_memory_report(void)
{
  struct sys_memory_stats out = stats();
  printf("ZEPHYR_MEMORY kernel_heap_reserved=%u kernel_heap_used=%u "
         "kernel_heap_peak=%u kernel_heap_free=%u\n",
         K_HEAP_MEM_POOL_SIZE, (unsigned)out.allocated_bytes,
         (unsigned)out.max_allocated_bytes, (unsigned)out.free_bytes);
}

/* SPDX-License-Identifier: MIT */
/* Fixture-only platform observations, not camera or streaming policy. */

#include <nuttx/config.h>
#include <nuttx/clock.h>
#include <nuttx/mm/mm.h>
#include <nuttx/sched.h>
#include <nuttx/arch.h>
#include <nuttx/spinlock.h>
#include <malloc.h>
#include <limits.h>
#include <stdint.h>
#include <time.h>
#include <string.h>
#include <netinet/in.h>
#include <netutils/netlib.h>
#include <unistd.h>
#include <sched.h>
#ifdef CONFIG_ARCH_CHIP_ESP32S3
#include "arch/xtensa/src/esp32s3/esp-hal-3rdparty/components/esp_hw_support/include/esp_private/esp_clk.h"
#endif

uint32_t nxrs_stream_cpu_frequency(void)
{
#ifdef CONFIG_ARCH_CHIP_ESP32S3
  return esp_clk_cpu_freq();
#else
  return 0;
#endif
}
/* Scalar-only fixture observation; target TCB layout never crosses into Rust. */
int nxrs_stream_worker_stack(uint32_t values[2])
{
#ifdef CONFIG_STACK_COLORATION
  struct tcb_s *tcb = nxsched_get_tcb(gettid());
  if (tcb == NULL) return -1;
  values[0] = tcb->adj_stack_size;
  values[1] = up_check_tcbstack(tcb, tcb->adj_stack_size);
  return 0;
#else
  return -1;
#endif
}

int nxrs_stream_address(uint8_t address[4])
{
  struct in_addr value;
  /* The physical Wi-Fi profiles and their provider use the station wlan0. */
  if (netlib_get_ipv4addr("wlan0", &value) < 0 || value.s_addr == INADDR_ANY)
    {
      return -1;
    }

  memcpy(address, &value.s_addr, 4);
  return 0;
}

int nxrs_stream_metrics(uint32_t values[2], uint64_t times[3])
{
  irqstate_t flags;
  clock_t idle_runtime[CONFIG_SMP_NCPUS];
  clock_t caller_runtime;
  uint64_t idle_us = 0;
  struct timespec wall;
  struct timespec idle_time;
  struct timespec cpu;
  if (perf_getfreq() == 0 || perf_getfreq() == ULONG_MAX)
    {
      return -1;
    }

  flags = enter_critical_section();
  clock_t current = perf_gettime();
  FAR struct tcb_s *caller = nxsched_get_tcb(gettid());
  if (caller == NULL) { leave_critical_section(flags); return -1; }
  caller_runtime = caller->run_time + current - caller->run_start;
  for (int cpu_index = 0; cpu_index < CONFIG_SMP_NCPUS; ++cpu_index)
    {
      FAR struct tcb_s *idle = nxsched_get_tcb(cpu_index);
      if (idle == NULL) { leave_critical_section(flags); return -1; }
      idle_runtime[cpu_index] = idle->run_time;
      if (idle->task_state == TSTATE_TASK_RUNNING)
        idle_runtime[cpu_index] += current - idle->run_start;
    }
  leave_critical_section(flags);
  if (clock_gettime(CLOCK_MONOTONIC, &wall) < 0)
    {
      return -1;
    }

  values[0] = mallinfo().uordblks;
  values[1] = kmm_mallinfo().uordblks;
  /* NuttX CLOCK_THREAD_CPUTIME_ID calls up_perf_convert directly, which does
   * not convert the shared tick fallback. Match perf_gettime with perf_convert. */
  perf_convert(caller_runtime, &cpu);
  /* Mean idle time per CPU, including a currently running remote idle slice.
   * The Rust fixture computes utilization against one wall-time interval.
   * Thread CPU below observes only the caller, not the numerical helper. */
  for (int cpu_index = 0; cpu_index < CONFIG_SMP_NCPUS; ++cpu_index)
    {
      perf_convert(idle_runtime[cpu_index], &idle_time);
      idle_us += (uint64_t)idle_time.tv_sec * 1000000 + idle_time.tv_nsec / 1000;
    }
  times[0] = (uint64_t)wall.tv_sec * 1000000 + wall.tv_nsec / 1000;
  times[1] = idle_us / CONFIG_SMP_NCPUS;
  times[2] = (uint64_t)cpu.tv_sec * 1000000 + cpu.tv_nsec / 1000;
  return 0;
}

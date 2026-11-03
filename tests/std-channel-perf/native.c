/* SPDX-License-Identifier: MIT */
/* Mechanical scheduler/TCB, native semaphore storage and clock translation.
 * This file contains no benchmark orchestration or channel implementation. */
#ifndef __NuttX__
#  define _GNU_SOURCE 1
#endif
#include "native.h"
#include <errno.h>
#include <limits.h>
#include <sched.h>
#include <semaphore.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>

#ifdef __NuttX__
#  include <nuttx/config.h>
#  include <nuttx/arch.h>
#  include <nuttx/clock.h>
#  include <nuttx/sched.h>
#  include <nuttx/spinlock.h>
#  if defined(CONFIG_ARCH_CHIP_ESP32S3)
#    include <arch/xtensa/core_macros.h>
#  elif defined(CONFIG_ARCH_BOARD_RASPBERRYPI_PICO_2)
#    include <arch/board/board.h>
#  endif
#  if !defined(CONFIG_BUILD_FLAT) || defined(CONFIG_SMP)
#    error "std-channel-perf TCB witnesses require single-core flat NuttX"
#  endif
#elif !defined(__linux__)
#  error "std-channel-perf supports native Linux and single-core flat NuttX"
#endif

_Static_assert(sizeof(sem_t) <= SCP_GATE_BYTES, "gate storage too small");
_Static_assert(_Alignof(sem_t) <= SCP_GATE_ALIGN, "gate storage under-aligned");

/* Selected during Rust-owned setup, before any measurement workers exist. */
static unsigned g_counter_source;

int scp_has_tcb_witness(void)
{
#ifdef __NuttX__
  return 1;
#else
  return 0;
#endif
}

int scp_current_tid(void) { return (int)gettid(); }

int scp_schedule_get(int tid, int *policy, int *priority)
{
  struct sched_param parameter;
  int raw = sched_getscheduler((pid_t)tid);
  if (raw < 0 || sched_getparam((pid_t)tid, &parameter) < 0)
    {
      return errno;
    }
  if (raw == SCHED_FIFO) *policy = 1;
  else if (raw == SCHED_RR) *policy = 2;
  else if (raw == SCHED_OTHER) *policy = 0;
  else return ENOTSUP;
  *priority = parameter.sched_priority;
  return 0;
}

int scp_schedule_set(int tid, int policy, int priority)
{
  int raw;
  struct sched_param parameter = {0};
  if (policy == 1) raw = SCHED_FIFO;
  else if (policy == 2) raw = SCHED_RR;
  else if (policy == 0) raw = SCHED_OTHER;
  else return EINVAL;
  parameter.sched_priority = priority;
  return sched_setscheduler((pid_t)tid, raw, &parameter) < 0 ? errno : 0;
}

int scp_thread_state(int tid, int *priority)
{
#ifdef __NuttX__
  int state;
  irqstate_t flags = enter_critical_section();
  struct tcb_s *tcb = nxsched_get_tcb((pid_t)tid);
  if (tcb == NULL)
    {
      state = 5;
      *priority = -1;
    }
  else
    {
      *priority = tcb->sched_priority;
      switch (tcb->task_state)
        {
          case TSTATE_WAIT_SEM: state = 1; break;
          case TSTATE_TASK_READYTORUN: state = 2; break;
          case TSTATE_TASK_RUNNING: state = 3; break;
          default: state = 4; break;
        }
    }
  leave_critical_section(flags);
  return state;
#else
  (void)tid;
  *priority = -1;
  return 0;
#endif
}

int scp_gate_init(void *storage)
{
  sem_t *sem = storage;
  if (sem_init(sem, 0, 0) < 0) return errno;
#ifdef __NuttX__
  /* These are notifications, never ownership/priority-inheritance mutexes. */
  if (sem_setprotocol(sem, SEM_PRIO_NONE) < 0)
    {
      int error = errno;
      sem_destroy(sem);
      return error;
    }
#endif
  return 0;
}

int scp_gate_post(void *storage)
{
  return sem_post((sem_t *)storage) < 0 ? errno : 0;
}

int scp_gate_wait_ms(void *storage, unsigned timeout_ms)
{
  struct timespec deadline;
  if (clock_gettime(CLOCK_MONOTONIC, &deadline) < 0) return errno;
  deadline.tv_sec += timeout_ms / 1000;
  deadline.tv_nsec += (long)(timeout_ms % 1000) * 1000000L;
  if (deadline.tv_nsec >= 1000000000L)
    {
      deadline.tv_sec++;
      deadline.tv_nsec -= 1000000000L;
    }
  /* One native wait. Interrupt/error handling belongs to the Rust fixture. */
  return sem_clockwait((sem_t *)storage, CLOCK_MONOTONIC, &deadline) < 0
         ? errno : 0;
}

int scp_gate_destroy(void *storage)
{
  return sem_destroy((sem_t *)storage) < 0 ? errno : 0;
}

int scp_monotonic_ns(uint64_t *value)
{
  struct timespec now;
  if (clock_gettime(CLOCK_MONOTONIC, &now) < 0) return errno;
  *value = (uint64_t)now.tv_sec * UINT64_C(1000000000) + now.tv_nsec;
  return 0;
}

int scp_monotonic_resolution_ns(uint64_t *value)
{
  struct timespec resolution;
  if (clock_getres(CLOCK_MONOTONIC, &resolution) < 0) return errno;
  *value = (uint64_t)resolution.tv_sec * UINT64_C(1000000000)
           + resolution.tv_nsec;
  return 0;
}

int scp_counter_info(unsigned request, uint64_t *frequency, unsigned *source,
                     unsigned *native_bits)
{
  if (request > 1) return EINVAL;
#ifdef __NuttX__
  if (request == 1)
    {
      /* These frequencies come from the image's native configuration, not
       * command-line assertions. They still require physical calibration and
       * a fixed-frequency run; the Rust probe checks only plausibility. */
#  if defined(CONFIG_ARCH_CHIP_ESP32S3)
      *frequency = (uint64_t)CONFIG_ESP32S3_DEFAULT_CPU_FREQ_MHZ * 1000000;
      *source = 5;
      *native_bits = 32;
#  elif defined(CONFIG_ARCH_BOARD_RASPBERRYPI_PICO_2) && \
        defined(CONFIG_ARCH_PERF_EVENTS)
      up_perf_init((void *)(uintptr_t)BOARD_SYS_FREQ);
      if (up_perf_getfreq() != BOARD_SYS_FREQ) return EINVAL;
      *frequency = BOARD_SYS_FREQ;
      *source = 6;
      *native_bits = 32;
#  else
      return ENOTSUP;
#  endif
      g_counter_source = *source;
      return *frequency != 0 ? 0 : EINVAL;
    }
  unsigned long hz = perf_getfreq();
  if (hz == 0 || hz == ULONG_MAX)
    {
      /* Some baseline images route perf_getfreq to an uninitialized arch
       * implementation. Keep functional witnesses usable with the existing
       * monotonic clock, explicitly identified and never timing-qualified. */
      *frequency = UINT64_C(1000000000);
      *source = 7;
      *native_bits = 64;
      g_counter_source = *source;
      return 0;
    }
  *frequency = hz;
#  ifdef CONFIG_ARCH_PERF_EVENTS
  *source = 1;
#  elif defined(CONFIG_TIMER_ARCH) || defined(CONFIG_ALARM_ARCH)
  *source = 2;
#  else
  *source = 3;
#  endif
#  ifdef CONFIG_ARCH_PERF_COUNT_BITWIDTH
  *native_bits = CONFIG_ARCH_PERF_COUNT_BITWIDTH;
#  else
  *native_bits = sizeof(clock_t) * CHAR_BIT;
#  endif
#else
  if (request != 0) return ENOTSUP;
  *frequency = UINT64_C(1000000000);
  *source = 4;
  *native_bits = 64;
#endif
  g_counter_source = *source;
  return *native_bits >= 32 ? 0 : ENOTSUP;
}

uint32_t scp_counter(void)
{
#ifdef __NuttX__
  if (g_counter_source == 7)
    {
      uint64_t now = 0;
      if (scp_monotonic_ns(&now) != 0) abort();
      return (uint32_t)now;
    }
#  if defined(CONFIG_ARCH_CHIP_ESP32S3)
  if (g_counter_source == 5) return (uint32_t)XTHAL_GET_CCOUNT();
#  elif defined(CONFIG_ARCH_BOARD_RASPBERRYPI_PICO_2) && \
        defined(CONFIG_ARCH_PERF_EVENTS)
  if (g_counter_source == 6) return (uint32_t)up_perf_gettime();
#  endif
  return (uint32_t)perf_gettime();
#else
  uint64_t now = 0;
  if (scp_monotonic_ns(&now) != 0) abort();
  return (uint32_t)now;
#endif
}

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
#include <poll.h>
#include <unistd.h>
#include <errno.h>
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
/* Fixture-only scheduler ABI. Standard Rust has no priority setter. Sampling,
 * queues, model execution and alert behavior remain in Rust. */
int nxrs_stream_worker_priority(int priority)
{
  struct sched_param param = { .sched_priority = priority };
  return sched_setparam(0, &param);
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

/* Mechanical, fixture-only target pollfd/event translation. */
int nxrs_stream_console_ready(void)
{
  struct pollfd fd = { .fd = STDIN_FILENO, .events = POLLIN };
  int result = poll(&fd, 1, 0);
  if (result < 0) return errno == EINTR ? 0 : -1;
  if (fd.revents & (POLLERR | POLLHUP | POLLNVAL)) return -1;
  return (fd.revents & POLLIN) != 0;
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
  FAR struct tcb_s *idle;
  irqstate_t flags;
  clock_t idle_runtime;
  struct timespec wall;
  struct timespec idle_time;
  struct timespec cpu;
  if (perf_getfreq() == 0 || perf_getfreq() == ULONG_MAX)
    {
      return -1;
    }

  flags = enter_critical_section();
  idle = nxsched_get_tcb(0);
  idle_runtime = idle ? idle->run_time : 0;
  leave_critical_section(flags);
  if (!idle || clock_gettime(CLOCK_MONOTONIC, &wall) < 0 ||
      clock_gettime(CLOCK_THREAD_CPUTIME_ID, &cpu) < 0)
    {
      return -1;
    }

  values[0] = mallinfo().uordblks;
  values[1] = kmm_mallinfo().uordblks;
  /* This task is running, so the idle thread's last slice is already charged.
   * Use full run-time precision, not per-switch integer scheduler ticks. ISR
   * time remains attributed to the interrupted task by this accounting. */
  perf_convert(idle_runtime, &idle_time);
  times[0] = (uint64_t)wall.tv_sec * 1000000 + wall.tv_nsec / 1000;
  times[1] = (uint64_t)idle_time.tv_sec * 1000000 + idle_time.tv_nsec / 1000;
  times[2] = (uint64_t)cpu.tv_sec * 1000000 + cpu.tv_nsec / 1000;
  return 0;
}

/* Serial-only fixture ABI: save/restore termios fields; Rust owns the session. */
#include <termios.h>
int nxrs_detector_console_mode(uint32_t saved[6], int raw)
{
  struct termios value;
  _Static_assert(sizeof(tcflag_t) <= sizeof(uint32_t), "termios flags");
  if (tcgetattr(STDIN_FILENO, &value) < 0) return -1;
  if (raw)
    {
      saved[0] = value.c_iflag; saved[1] = value.c_oflag;
      saved[2] = value.c_lflag; saved[3] = value.c_cflag;
      saved[4] = value.c_cc[VMIN]; saved[5] = value.c_cc[VTIME];
      cfmakeraw(&value);
      value.c_cc[VMIN] = 1; value.c_cc[VTIME] = 0;
    }
  else
    {
      value.c_iflag = saved[0]; value.c_oflag = saved[1];
      value.c_lflag = saved[2]; value.c_cflag = saved[3];
      value.c_cc[VMIN] = saved[4]; value.c_cc[VTIME] = saved[5];
    }
  return tcsetattr(STDIN_FILENO, TCSANOW, &value);
}

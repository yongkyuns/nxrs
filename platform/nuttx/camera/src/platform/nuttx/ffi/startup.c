/* SPDX-License-Identifier: MIT */
/* Mechanical product-startup translations against the selected NuttX headers. */
#include <nuttx/config.h>
#include <netutils/netinit.h>
#include <errno.h>
#include <stdint.h>
#include <poll.h>
#include <sched.h>
#include <termios.h>

int camera_network_initialize(void)
{
#ifdef CONFIG_NETINIT_THREAD
  return -ENOTSUP;
#else
  return netinit_bringup();
#endif
}

int camera_worker_priority(int priority)
{
  struct sched_param param = { .sched_priority = priority };
  return sched_setparam(0, &param);
}

int camera_console_ready(int descriptor)
{
  struct pollfd fd = { .fd = descriptor, .events = POLLIN };
  int result = poll(&fd, 1, 0);
  if (result < 0) return errno == EINTR ? 0 : -1;
  if (fd.revents & (POLLERR | POLLHUP | POLLNVAL)) return -1;
  return (fd.revents & POLLIN) != 0;
}

int camera_console_mode(int descriptor, uint32_t saved[6], int raw)
{
  struct termios value;
  _Static_assert(sizeof(tcflag_t) <= sizeof(uint32_t), "termios flags");
  if (tcgetattr(descriptor, &value) < 0) return -1;
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
  return tcsetattr(descriptor, TCSANOW, &value);
}

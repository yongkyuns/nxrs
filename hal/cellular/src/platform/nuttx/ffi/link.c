/* Target termios, poll and TUN control ABI. */
#include <nuttx/config.h>
#include <net/if.h>
#include <netinet/in.h>
#include <netutils/netlib.h>
#include <nuttx/net/tun.h>
#include <sys/ioctl.h>
#include <fcntl.h>
#include <poll.h>
#include <stdint.h>
#include <string.h>
#include <termios.h>

_Static_assert(IFNAMSIZ == 16, "interface name ABI");

int cellular_nonblock(void)
{
  return O_NONBLOCK;
}

int cellular_raw(int fd)
{
  struct termios term;
  if (tcgetattr(fd, &term) < 0)
    {
      return -1;
    }
  cfmakeraw(&term);
  term.c_cflag = (term.c_cflag & ~(CSIZE | CSTOPB | PARENB)) | CS8 | CLOCAL | CREAD;
#ifdef CRTSCTS
  term.c_cflag &= ~CRTSCTS;
#endif
  term.c_cc[VMIN] = 1;
  term.c_cc[VTIME] = 0;
  return tcsetattr(fd, TCSANOW, &term);
}

int cellular_poll(int uart, int tun, int stop, int write_fd, int ms)
{
  struct pollfd fds[3] =
  {
    { .fd = write_fd < 0 ? uart : write_fd,
      .events = write_fd < 0 ? POLLIN : POLLOUT },
    { .fd = write_fd < 0 ? tun : -1, .events = POLLIN },
    { .fd = stop, .events = POLLIN }
  };
  return poll(fds, 3, ms);
}

int cellular_tun(int fd, char name[IFNAMSIZ])
{
  struct ifreq req = { .ifr_flags = IFF_TUN };
  int ret = ioctl(fd, TUNSETIFF, (unsigned long)(uintptr_t)&req);
  if (ret == 0)
    {
      memcpy(name, req.ifr_name, IFNAMSIZ);
    }
  return ret;
}

int cellular_addr(const char *name, const uint8_t addr[4])
{
  struct in_addr value;
  memcpy(&value.s_addr, addr, 4);
  return netlib_set_ipv4addr(name, &value);
}

int cellular_gateway(const char *name, const uint8_t addr[4])
{
  struct in_addr value;
  memcpy(&value.s_addr, addr, 4);
  return netlib_set_dripv4addr(name, &value);
}

int cellular_mask(const char *name, const uint8_t addr[4])
{
  struct in_addr value;
  memcpy(&value.s_addr, addr, 4);
  return netlib_set_ipv4netmask(name, &value);
}

int cellular_up(const char *name)
{
  return netlib_ifup(name);
}

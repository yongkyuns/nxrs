/* SPDX-License-Identifier: MIT */
/* Native socket control; descriptors stay owned by the Rust fixture. */
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

#ifdef __NuttX__
#  include <nuttx/config.h>
#  include <nuttx/fs/fs.h>
#  include <nuttx/net/net.h>
#  ifdef CONFIG_NET_TCP
#    include "net/tcp/tcp.h"
#    include "net/inet/inet.h"
#    include "net/utils/utils.h"
#  endif
#endif

#define NXRS_RADIO_TCP_TIMEOUT_SECONDS 2
#define NXRS_RADIO_TCP_MAX_TRANSFER 513

/* Test-only snapshot of this descriptor, never addresses or packet contents.
 * Compile against native headers rather than copying the TCP control layout.
 */

int nxrs_radio_tcp_state(int fd, uint32_t *values, size_t count)
{
  if (values == NULL || count != 8 || fd < 0)
    {
      return -EINVAL;
    }

#if defined(__NuttX__) && defined(NET_TCP_HAVE_STACK)
  struct file *filep;
  struct socket *socketp;
  struct tcp_conn_s *connection;
  const struct sock_intf_s *native_interface;
  int result;

  result = sockfd_socket(fd, &filep, &socketp);
  if (result < 0)
    {
      return result;
    }

  if (socketp->s_type != SOCK_STREAM || socketp->s_conn == NULL ||
      (socketp->s_domain != AF_INET && socketp->s_domain != AF_INET6))
    {
      result = -ENOTCONN;
    }
  else
    {
      native_interface = inet_sockif(socketp->s_domain, SOCK_STREAM,
                                      IPPROTO_TCP);
      if (native_interface == NULL || socketp->s_sockif != native_interface)
        {
          file_put(filep);
          return -ENOSYS;
        }

      connection = socketp->s_conn;
      conn_dev_lock(&connection->sconn, connection->dev);
      values[0] = connection->tcpstateflags & TCP_STATE_MASK;
      values[1] = connection->rto;
      values[2] = connection->timer;
      values[3] = connection->nrtx;
      values[4] = connection->tx_unacked;
      values[5] = connection->snd_wnd;
      values[6] = connection->sa;
      values[7] = connection->sv;
      conn_dev_unlock(&connection->sconn, connection->dev);
      result = 0;
    }

  file_put(filep);
  return result;
#else
  return -ENOSYS;
#endif
}

static int nxrs_radio_socket_open(int domain, int type, int protocol)
{
  int fd = socket(domain, type, protocol);
  int flags;

  if (fd < 0)
    {
      return -(errno != 0 ? errno : EIO);
    }

  flags = fcntl(fd, F_GETFD, 0);
  if (flags < 0 || fcntl(fd, F_SETFD, flags | FD_CLOEXEC) < 0)
    {
      int result = errno != 0 ? errno : EIO;
      close(fd);
      return -result;
    }

  return fd;
}

static int nxrs_radio_tcp_deadline(struct timespec *deadline)
{
  if (clock_gettime(CLOCK_MONOTONIC, deadline) < 0)
    {
      return errno != 0 ? errno : EIO;
    }

  deadline->tv_sec += NXRS_RADIO_TCP_TIMEOUT_SECONDS;
  return 0;
}

static int nxrs_radio_tcp_remaining(const struct timespec *deadline,
                                    struct timeval *timeout)
{
  struct timespec now;
  time_t seconds;
  long nanoseconds;
  long microseconds;

  if (clock_gettime(CLOCK_MONOTONIC, &now) < 0)
    {
      return errno != 0 ? errno : EIO;
    }

  seconds = deadline->tv_sec - now.tv_sec;
  nanoseconds = deadline->tv_nsec - now.tv_nsec;
  if (nanoseconds < 0)
    {
      seconds--;
      nanoseconds += 1000000000L;
    }

  if (seconds < 0 || (seconds == 0 && nanoseconds == 0))
    {
      return ETIMEDOUT;
    }

  /* Round upward so sub-microsecond time is never turned into zero. */
  microseconds = (nanoseconds + 999L) / 1000L;
  if (microseconds == 1000000L)
    {
      seconds++;
      microseconds = 0;
    }

  timeout->tv_sec = seconds;
  timeout->tv_usec = microseconds;
  return 0;
}

static int nxrs_radio_tcp_set_timeout(int fd, int option,
                                      const struct timespec *deadline)
{
  struct timeval timeout;
  int result = nxrs_radio_tcp_remaining(deadline, &timeout);

  if (result != 0)
    {
      return result;
    }

  if (setsockopt(fd, SOL_SOCKET, option, &timeout, sizeof(timeout)) < 0)
    {
      return errno != 0 ? errno : EIO;
    }

  return 0;
}

static int nxrs_radio_tcp_poll_ready(int fd, short events,
                                     const struct timespec *deadline,
                                     short *revents)
{
  struct pollfd descriptor = {0};

  descriptor.fd = fd;
  descriptor.events = events;
  for (;;)
    {
      struct timeval remaining;
      int result = nxrs_radio_tcp_remaining(deadline, &remaining);
      int timeout_ms;

      if (result != 0)
        {
          return result;
        }

      timeout_ms = (int)(remaining.tv_sec * 1000 +
                         (remaining.tv_usec + 999) / 1000);
      result = poll(&descriptor, 1, timeout_ms);
      if (result < 0)
        {
          if (errno == EINTR)
            {
              continue;
            }

          return errno != 0 ? errno : EIO;
        }

      if (result == 0)
        {
          return ETIMEDOUT;
        }

      if (descriptor.revents & POLLNVAL)
        {
          return EBADF;
        }

      if (descriptor.revents & (events | POLLERR | POLLHUP))
        {
          *revents = descriptor.revents;
          return 0;
        }
    }
}

int nxrs_radio_tcp_open(const unsigned char address[4], uint16_t port)
{
  struct sockaddr_in peer = {0};
  struct timespec deadline;
  short revents;
  int fd;
  int flags;
  int result;
  int connect_result;
  int connect_error;

  if (address == NULL)
    {
      return -EINVAL;
    }

  fd = nxrs_radio_socket_open(AF_INET, SOCK_STREAM, 0);
  if (fd < 0)
    {
      return fd;
    }

  flags = fcntl(fd, F_GETFL, 0);
  if (flags < 0)
    {
      result = errno != 0 ? errno : EIO;
      goto failed;
    }

  if (fcntl(fd, F_SETFL, flags | O_NONBLOCK) < 0)
    {
      result = errno != 0 ? errno : EIO;
      goto failed;
    }

  peer.sin_family = AF_INET;
  peer.sin_port = htons(port);
  memcpy(&peer.sin_addr.s_addr, address, sizeof(peer.sin_addr.s_addr));
  connect_result = connect(fd, (const struct sockaddr *)&peer, sizeof(peer));
  connect_error = connect_result < 0 ? errno : 0;

  /* Restore the socket's original blocking mode as soon as connect returns. */
  if (fcntl(fd, F_SETFL, flags) < 0)
    {
      result = errno != 0 ? errno : EIO;
      goto failed;
    }

  if (connect_result < 0)
    {
      if (connect_error != EINPROGRESS && connect_error != EALREADY &&
          connect_error != EINTR &&
          connect_error != EWOULDBLOCK)
        {
          result = connect_error != 0 ? connect_error : EIO;
          goto failed;
        }

      /* Match std: the poll budget starts after connect and flag restore. */
      result = nxrs_radio_tcp_deadline(&deadline);
      if (result != 0)
        {
          goto failed;
        }

      result = nxrs_radio_tcp_poll_ready(fd, POLLOUT, &deadline, &revents);
      if (result != 0)
        {
          goto failed;
        }

      {
        int socket_error = 0;
        socklen_t length = sizeof(socket_error);

        if (getsockopt(fd, SOL_SOCKET, SO_ERROR, &socket_error,
                       &length) < 0)
          {
            result = errno != 0 ? errno : EIO;
            goto failed;
          }

        if (socket_error != 0)
          {
            result = socket_error;
            goto failed;
          }
      }
    }

  {
    int enabled = 1;
    if (setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &enabled,
                   sizeof(enabled)) < 0)
      {
        result = errno != 0 ? errno : EIO;
        goto failed;
      }
  }

  return fd;

failed:
  close(fd);
  return -result;
}

int nxrs_radio_tcp_transfer(int fd, unsigned char *data, size_t length,
                            int sending)
{
  struct timespec deadline;
  size_t transferred = 0;
  int option;
  int result;

  if (fd < 0 || (length != 0 && data == NULL) ||
      length > NXRS_RADIO_TCP_MAX_TRANSFER ||
      (sending != 0 && sending != 1))
    {
      return -EINVAL;
    }

  if (length == 0)
    {
      return 0;
    }

  result = nxrs_radio_tcp_deadline(&deadline);
  if (result != 0)
    {
      return -result;
    }

  option = sending ? SO_SNDTIMEO : SO_RCVTIMEO;
  while (transferred < length)
    {
      ssize_t count;

      result = nxrs_radio_tcp_set_timeout(fd, option, &deadline);
      if (result != 0)
        {
          return -result;
        }

      if (sending)
        {
          count = send(fd, data + transferred, length - transferred,
                       MSG_NOSIGNAL);
        }
      else
        {
          count = recv(fd, data + transferred, length - transferred, 0);
        }

      {
        int syscall_error = errno;
        struct timeval remaining;
        result = nxrs_radio_tcp_remaining(&deadline, &remaining);
        if (result != 0)
          {
            return -result;
          }

        errno = syscall_error;
      }

      if (count > 0)
        {
          transferred += (size_t)count;
          continue;
        }

      if (count == 0)
        {
          return sending ? -EPIPE :
                 (transferred == 0 ? 0 : -ECONNRESET);
        }

      if (errno == EINTR)
        {
          continue;
        }

      if (errno == EAGAIN || errno == EWOULDBLOCK)
        {
          return -ETIMEDOUT;
        }

      return -(errno != 0 ? errno : EIO);
    }

  return (int)transferred;
}

int nxrs_radio_tcp_finish(int fd)
{
  struct timespec deadline;
  unsigned char trailing;
  short revents;
  ssize_t count;
  int result;

  if (fd < 0)
    {
      return -EBADF;
    }

  result = nxrs_radio_tcp_deadline(&deadline);
  if (result != 0)
    {
      return -result;
    }

  result = nxrs_radio_tcp_set_timeout(fd, SO_RCVTIMEO, &deadline);
  if (result != 0)
    {
      return -result;
    }

  for (;;)
    {
      result = shutdown(fd, SHUT_WR);
      if (result == 0)
        {
          break;
        }

      if (errno != EINTR)
        {
          return -(errno != 0 ? errno : EIO);
        }

      {
        struct timeval remaining;
        result = nxrs_radio_tcp_remaining(&deadline, &remaining);
        if (result != 0)
          {
            return -result;
          }
      }
    }

  for (;;)
    {
      result = nxrs_radio_tcp_poll_ready(fd, POLLIN, &deadline, &revents);
      if (result != 0)
        {
          return -result;
        }

      count = recv(fd, &trailing, sizeof(trailing), 0);
      {
        int syscall_error = errno;
        struct timeval remaining;
        result = nxrs_radio_tcp_remaining(&deadline, &remaining);
        if (result != 0)
          {
            return -result;
          }

        errno = syscall_error;
      }

      if (count == 0)
        {
          return 0;
        }

      if (count > 0)
        {
          return -EPROTO;
        }

      if (errno == EINTR)
        {
          continue;
        }

      if (errno == EAGAIN || errno == EWOULDBLOCK)
        {
          return -ETIMEDOUT;
        }

      return -(errno != 0 ? errno : EIO);
    }
}

int nxrs_radio_udp_open(const unsigned char address[4], uint16_t port)
{
  struct sockaddr_in local = {0};
  struct sockaddr_in peer = {0};
  struct timeval timeout = {NXRS_RADIO_TCP_TIMEOUT_SECONDS, 0};
  int fd;

  if (address == NULL)
    {
      return -EINVAL;
    }

  fd = nxrs_radio_socket_open(AF_INET, SOCK_DGRAM, 0);
  if (fd < 0)
    {
      return fd;
    }

  local.sin_family = AF_INET;
  local.sin_addr.s_addr = htonl(INADDR_ANY);
  local.sin_port = htons(0);
  if (bind(fd, (const struct sockaddr *)&local, sizeof(local)) < 0)
    {
      int result = errno != 0 ? errno : EIO;
      close(fd);
      return -result;
    }

  peer.sin_family = AF_INET;
  peer.sin_port = htons(port);
  memcpy(&peer.sin_addr.s_addr, address, sizeof(peer.sin_addr.s_addr));
  if (connect(fd, (const struct sockaddr *)&peer, sizeof(peer)) < 0)
    {
      int result = errno != 0 ? errno : EIO;
      close(fd);
      return -result;
    }

  if (setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)) < 0 ||
      setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout)) < 0)
    {
      int result = errno != 0 ? errno : EIO;
      close(fd);
      return -result;
    }

  return fd;
}

int nxrs_radio_udp_send(int fd, const unsigned char *data, size_t length)
{
  ssize_t count;

  if (fd < 0 || (length != 0 && data == NULL) ||
      length > NXRS_RADIO_TCP_MAX_TRANSFER)
    {
      return -EINVAL;
    }

  count = send(fd, data, length, MSG_NOSIGNAL);
  return count < 0 ? -(errno != 0 ? errno : EIO) : (int)count;
}

int nxrs_radio_udp_receive(int fd, unsigned char *data, size_t length)
{
  ssize_t count;

  if (fd < 0 || (length != 0 && data == NULL) ||
      length > NXRS_RADIO_TCP_MAX_TRANSFER)
    {
      return -EINVAL;
    }

  count = recv(fd, data, length, 0);
  return count < 0 ? -(errno != 0 ? errno : EIO) : (int)count;
}

int nxrs_radio_socket_snapshot(int fd, uint32_t *values, size_t count)
{
  int queued_bytes = 0;
  unsigned char head[4] = {0};
  ssize_t peek_bytes;

  if (fd < 0 || values == NULL || count != 5)
    {
      return -EINVAL;
    }

  memset(values, 0, 5 * sizeof(*values));

  /* These independent probes are not an atomic view of socket state. */
#ifdef __NuttX__
  if (ioctl(fd, FIONREAD,
            (unsigned long)(uintptr_t)&queued_bytes) < 0)
#else
  if (ioctl(fd, FIONREAD, &queued_bytes) < 0)
#endif
    {
      values[1] = (uint32_t)(errno != 0 ? errno : EIO);
    }
  else
    {
      values[0] = (uint32_t)queued_bytes;
    }

  peek_bytes = recv(fd, head, sizeof(head), MSG_PEEK | MSG_DONTWAIT);
  if (peek_bytes < 0)
    {
      values[3] = (uint32_t)(errno != 0 ? errno : EIO);
    }
  else
    {
      values[2] = (uint32_t)peek_bytes;
      if (peek_bytes >= (ssize_t)sizeof(head))
        {
          values[4] = ((uint32_t)head[0] << 24) |
                      ((uint32_t)head[1] << 16) |
                      ((uint32_t)head[2] << 8) |
                      (uint32_t)head[3];
        }
    }

  return 0;
}

int nxrs_radio_socket_close(int fd)
{
  if (close(fd) < 0)
    {
      return -(errno != 0 ? errno : EIO);
    }

  return 0;
}

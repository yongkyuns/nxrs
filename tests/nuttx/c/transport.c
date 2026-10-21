/* SPDX-License-Identifier: MIT
 * Fixture-only compatibility bridge for tests/nuttx's core-only Rust image.
 * Production NuttX packet output uses Rust std::net directly.
 */
#include <nuttx/config.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <fcntl.h>
#include <stdint.h>
#include <string.h>
#include <unistd.h>
#include "status.h"
#include "transport.h"

int rc_nx_udp_open(const uint8_t *address, uint16_t port)
{
  struct sockaddr_in peer;
  int fd = socket(AF_INET, SOCK_DGRAM, 0);
  int error;
  if (fd < 0) return rc_errno(errno);
  memset(&peer, 0, sizeof(peer));
  peer.sin_family = AF_INET;
  peer.sin_port = htons(port);
  memcpy(&peer.sin_addr.s_addr, address, 4);
  if (fcntl(fd, F_SETFL, O_NONBLOCK) < 0 ||
      connect(fd, (struct sockaddr *)&peer, sizeof(peer)) < 0)
    {
      error = rc_errno(errno);
      close(fd);
      return error;
    }
  return fd;
}

int rc_nx_send(int fd, const uint8_t *bytes, size_t length)
{
  ssize_t result = send(fd, bytes, length, 0);
  if (result < 0) return rc_errno(errno);
  return (size_t)result == length ? 0 : -6;
}

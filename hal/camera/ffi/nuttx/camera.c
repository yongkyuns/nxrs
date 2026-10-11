/* SPDX-License-Identifier: MIT
 * Compile with NuttX headers inside the target image, never as HOSTSRC.
 */
#include <nuttx/config.h>
#include <sys/ioctl.h>
#include <fcntl.h>
#include <limits.h>
#include <stdint.h>
#include <unistd.h>
#include "nuttx_support.h"
#include "camera.h"

int rc_nx_camera_open(const char *path, uint16_t *width, uint16_t *height,
                      uint8_t *pixels)
{
  struct rc_device_format format;
  int fd = open(path, O_RDONLY | O_NONBLOCK);
  if (fd < 0) return rc_errno(errno);
  if (ioctl(fd, RCIOC_FORMAT, (unsigned long)(uintptr_t)&format) < 0)
    {
      int error = rc_errno(errno);
      close(fd);
      return error;
    }
  *width = format.width;
  *height = format.height;
  *pixels = format.pixels;
  return fd;
}

int rc_nx_read(int fd, uint8_t *data, size_t capacity)
{
  ssize_t result;
  if (capacity > INT_MAX) return -5;
  result = read(fd, data, capacity);
  if (result >= 0) return (int)result;
  if (errno == EAGAIN || errno == EINTR) return 0;
  return rc_errno(errno);
}

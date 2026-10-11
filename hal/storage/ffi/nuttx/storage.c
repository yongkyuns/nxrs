/* SPDX-License-Identifier: MIT
 * Compile with configured NuttX headers inside the target image.
 */
#include <nuttx/config.h>
#include <fcntl.h>
#include <stdint.h>
#include <unistd.h>
#include "nuttx_support.h"
#include "storage.h"

int rc_nx_file_create(const char *path)
{
  int fd = open(path, O_CREAT | O_EXCL | O_RDWR, 0600);
  return fd < 0 ? rc_errno(errno) : fd;
}

static int write_all(int fd, const uint8_t *bytes, size_t length)
{
  while (length > 0)
    {
      ssize_t written = write(fd, bytes, length);
      if (written < 0 && errno == EINTR) continue;
      if (written <= 0) return written < 0 ? rc_errno(errno) : -6;
      bytes += written;
      length -= (size_t)written;
    }
  return 0;
}

int rc_nx_append(int fd, const uint8_t *header, size_t header_len,
                 const uint8_t *bytes, size_t length)
{
  off_t start = lseek(fd, 0, SEEK_END);
  int result;
  if (start < 0) return rc_errno(errno);
  result = write_all(fd, header, header_len);
  if (result == 0) result = write_all(fd, bytes, length);
  if (result != 0)
    {
      /* Recoverable call-level atomicity, not power-loss/crash durability. */
      if (ftruncate(fd, start) < 0 || lseek(fd, start, SEEK_SET) != start)
        return -7; /* Poison: rollback could not be established. */
    }
  return result;
}

int rc_nx_flush(int fd)
{
  return fsync(fd) == 0 ? 0 : rc_errno(errno);
}

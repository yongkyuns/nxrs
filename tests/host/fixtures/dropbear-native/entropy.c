#include <assert.h>
#include <errno.h>
#include <stddef.h>
#include <stdbool.h>
#include <stdarg.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
static int mode, opens, reads, closes;
static int entropy_open(const char *path, int flags, ...)
{
  assert(strcmp(path, "/dev/random") == 0);
  assert(flags == (O_RDONLY | O_NONBLOCK));
  opens++;
  if (mode == 6) { errno = ENOENT; return -1; }
  return 51;
}
static ssize_t entropy_read(int fd, void *buffer, size_t length)
{
  assert(fd == 51 && length <= 32 && ++reads < 300);
  if (mode == 1) { errno = EAGAIN; return -1; }
  if (mode == 2) { errno = EINTR; return -1; }
  if (mode == 3 && reads > 1) { errno = EIO; return -1; }
  if (mode == 4) return 0;
  if (mode == 5) return (ssize_t)length + 1;
  size_t n = length > 7 ? 7 : length;
  memset(buffer, 0xa5, n); return (ssize_t)n;
}
static int entropy_close(int fd) { assert(fd == 51); closes++; return 0; }
bool dropbear_service_stopping(void) { return mode == 7; }
#define open entropy_open
#define read entropy_read
#define close entropy_close
#include "netutils/dropbear/port/dropbear_entropy.c"
#undef open
#undef read
#undef close
int main(void)
{
  unsigned char buffer[65];
  assert(dropbear_entropy_read(NULL, 0) == 0 && opens == 0);
  for (mode = 0; mode <= 7; mode++)
    {
      opens = reads = closes = 0;
      memset(buffer, 0x55, sizeof(buffer));
      int result = dropbear_entropy_read(buffer, sizeof(buffer));
#ifdef UNQUALIFIED_SOURCE
      assert(result == -ENOTSUP && opens == 0 && reads == 0 && closes == 0);
#else
      if (mode == 0) assert(result == 0 && reads == 10);
      else assert(result < 0);
      if (mode == 2) assert(reads == 8);
      if (mode == 3) assert(reads == 2);
      if (mode == 6) assert(opens == 1 && closes == 0);
      else assert(opens == 1 && closes == 1);
#endif
      for (size_t i = 0; i < sizeof(buffer); i++)
        assert(buffer[i] == (result == 0 ? 0xa5 : 0));
    }
  return 0;
}

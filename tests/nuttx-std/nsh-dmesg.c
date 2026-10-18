/* Exercise the actual NSH command and NuttX getopt with mocked log I/O. */
#include <assert.h>
#include <stdbool.h>
#include <string.h>

#define FAR
#define ERROR (-1)
#define OK 0
#define DEBUGASSERT assert
#define CONFIG_SYSLOG_DEVPATH "/dev/ramlog"
#define O_RDONLY 0
#define BIOC_FLUSH 0x1234
#define NSH_ERRNO 5
#define g_fmtcmdfailed "cmd failed"

#include "nsh-getopt-types.h"
static struct getopt_s parser;
static struct getopt_s *getoptvars(void) { return &parser; }
#define optind parser.go_optind
#define getopt nxrs_getopt
#include "nsh-getopt-functions.h"

struct nsh_vtbl_s { int unused; };
static int reads, flushes, opens, closes, errors;
static int open_result, ioctl_result, read_result;

static void reset_fixture(void)
{
  memset(&parser, 0, sizeof(parser));
  reads = flushes = opens = closes = errors = 0;
  open_result = ioctl_result = read_result = 0;
}

static int nsh_catfile(struct nsh_vtbl_s *vtbl, const char *name,
                       const char *path)
{
  (void)vtbl; (void)name; (void)path;
  reads++;
  return read_result;
}

static int dmesg_open(const char *path, int flags)
{
  (void)path; (void)flags;
  opens++;
  return open_result < 0 ? open_result : 7;
}

static int dmesg_ioctl(int fd, int request, unsigned long arg)
{
  (void)fd; (void)arg;
  assert(request == BIOC_FLUSH);
  /* Read-and-clear must dump before flushing. */
  if (reads) assert(reads > flushes);
  flushes++;
  return ioctl_result;
}

static int dmesg_close(int fd)
{
  (void)fd;
  closes++;
  return 0;
}

static void nsh_error(struct nsh_vtbl_s *vtbl, const char *fmt, ...)
{
  (void)vtbl; (void)fmt;
  errors++;
}

#define open dmesg_open
#define ioctl dmesg_ioctl
#define close dmesg_close
#include "nsh-dmesg-function.h"

int main(void)
{
  struct nsh_vtbl_s vtbl = { 0 };
  char *read_clear[] = { "dmesg", "-c", 0 };
  char *clear[] = { "dmesg", "-C", 0 };
  char *plain[] = { "dmesg", 0 };
  char *invalid[] = { "dmesg", "-x", 0 };

  reset_fixture();
  for (int i = 0; i < 4; i++)
    assert(cmd_dmesg(&vtbl, 2, read_clear) == 0);
  assert(reads == 4);
#ifdef EXPECT_BASELINE
  assert(flushes == 2);
#else
  assert(flushes == 4);
#endif

  reset_fixture();
#ifdef EXPECT_BASELINE
  assert(cmd_dmesg(&vtbl, 2, clear) == ERROR);
  assert(reads == 0 && flushes == 0);
#else
  for (int i = 0; i < 4; i++)
    assert(cmd_dmesg(&vtbl, 2, clear) == 0);
  assert(reads == 0 && flushes == 4);
#endif

  reset_fixture();
  assert(cmd_dmesg(&vtbl, 1, plain) == 0);
  assert(reads == 1 && flushes == 0);
  assert(cmd_dmesg(&vtbl, 2, invalid) == ERROR);

  reset_fixture();
  open_result = -13;
  assert(cmd_dmesg(&vtbl, 2, read_clear) == -13);
  assert(reads == 1 && opens == 1 && flushes == 0 && errors == 1);

  reset_fixture();
  ioctl_result = -7;
  assert(cmd_dmesg(&vtbl, 2, read_clear) == -7);
  assert(reads == 1 && opens == 1 && flushes == 1 && closes == 1 && errors == 1);

  reset_fixture();
  read_result = -12;
#ifdef EXPECT_BASELINE
  assert(cmd_dmesg(&vtbl, 2, read_clear) == 0);
  assert(reads == 1 && opens == 1 && flushes == 1);
#else
  assert(cmd_dmesg(&vtbl, 2, read_clear) == -12);
  assert(reads == 1 && opens == 0 && flushes == 0);
#endif
  return 0;
}

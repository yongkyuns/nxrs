/* Target-header scalar wrappers for the opt-in S8 GPIO marker fixture. */
#include <nuttx/config.h>

#include <errno.h>
#include <sys/ioctl.h>
#include <time.h>

#ifdef CONFIG_DEV_GPIO
#include <nuttx/ioexpander/gpio.h>
#endif

int s8_marker_write(int fd, int high)
{
#ifdef CONFIG_DEV_GPIO
  if (high != 0 && high != 1)
    {
      errno = EINVAL;
      return -1;
    }

  return ioctl(fd, GPIOC_WRITE, (unsigned long)high);
#else
  (void)fd;
  (void)high;
  errno = ENOSYS;
  return -1;
#endif
}

int s8_marker_clock_resolution_ns(void)
{
  struct timespec resolution;

  if (clock_getres(CLOCK_MONOTONIC, &resolution) < 0)
    {
      return -1;
    }

  if (resolution.tv_sec != 0 || resolution.tv_nsec <= 0 ||
      resolution.tv_nsec >= 1000000000L)
    {
      errno = ERANGE;
      return -1;
    }

  return (int)resolution.tv_nsec;
}

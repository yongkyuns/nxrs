/* Native header layouts and individual watchdog ioctls. */
#include <errno.h>
#include <nuttx/config.h>
#include <nuttx/timers/watchdog.h>
#include <stdbool.h>
#include <sys/ioctl.h>

#if defined(CONFIG_WATCHDOG_AUTOMONITOR) || defined(CONFIG_WATCHDOG_PANIC_NOTIFIER)
#error "Inherited reset deadlines require automatic feeding and panic-stop disabled"
#endif

int watchdog_reset_status(int fd, bool *active, bool *reset)
{
  struct watchdog_status_s status;
  int result = ioctl(fd, WDIOC_GETSTATUS, (unsigned long)&status);
  if (result < 0)
    return result;
  *active = (status.flags & WDFLAGS_ACTIVE) != 0;
  *reset = (status.flags & WDFLAGS_RESET) != 0;
  return 0;
}

int watchdog_disarm(int fd)
{
  return ioctl(fd, WDIOC_STOP, 0);
}

int watchdog_set_timeout(int fd, uint32_t ms)
{
  int result = ioctl(fd, WDIOC_SETTIMEOUT, (unsigned long)ms);
  return result < 0 && (errno == EINVAL || errno == ERANGE) ? 1 : result;
}

int watchdog_start(int fd)
{
  return ioctl(fd, WDIOC_START, 0);
}

int watchdog_feed(int fd)
{
  return ioctl(fd, WDIOC_KEEPALIVE, 0);
}

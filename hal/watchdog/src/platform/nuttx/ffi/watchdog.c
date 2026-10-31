/* Native header layouts/constants only. Never arm, feed or restart a timer. */
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

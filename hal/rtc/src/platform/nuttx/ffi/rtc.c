#include <errno.h>
#include <nuttx/timers/rtc.h>
#include <stdint.h>
#include <sys/ioctl.h>
#include <time.h>

#if defined(CONFIG_LIBC_LOCALTIME)
#error "RTC lower halves using mktime must interpret device time as UTC"
#endif

static int rtc_status(int status)
{
  if (status < 0)
    {
      if (errno == ENOSYS || errno == ENOTTY)
        return 1;
      if (errno == EINVAL || errno == ERANGE)
        return 2;
    }

  return status;
}

int rtc_is_set(int fd, bool *value)
{
  return rtc_status(ioctl(fd, RTC_HAVE_SET_TIME, (unsigned long)value));
}

int rtc_read_seconds(int fd, int64_t *seconds)
{
  struct rtc_time native = {0};
  int status = ioctl(fd, RTC_RD_TIME, (unsigned long)&native);
  if (status < 0)
    return rtc_status(status);

  errno = 0;
  time_t value = timegm((struct tm *)&native);
  if (value == (time_t)-1 && errno != 0)
    return rtc_status(-1);

  *seconds = value;
  return 0;
}

int rtc_set_seconds(int fd, int64_t seconds)
{
  time_t value = seconds;
  struct rtc_time native = {0};
  if (gmtime_r(&value, (struct tm *)&native) == NULL)
    return 2;

  return rtc_status(ioctl(fd, RTC_SET_TIME, (unsigned long)&native));
}

#include <fcntl.h>
#include <nuttx/analog/ioctl.h>
#include <sys/ioctl.h>

int adc_nonblocking_flag(void)
{
  return O_NONBLOCK;
}

int adc_reset_fifo(int fd)
{
  return ioctl(fd, ANIOC_RESET_FIFO, 0);
}

int adc_trigger(int fd)
{
  return ioctl(fd, ANIOC_TRIGGER, 0);
}

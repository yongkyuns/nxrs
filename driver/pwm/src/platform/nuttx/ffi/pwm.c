#include <nuttx/config.h>
#include <stdint.h>
#include <sys/ioctl.h>

#include <nuttx/timers/pwm.h>

int pwm_configure(int fd, uint32_t frequency, uint16_t duty)
{
#if defined(CONFIG_PWM) && CONFIG_PWM_NCHANNELS == 1
  struct pwm_info_s info = { .frequency = frequency };
  info.channels[0].duty = duty;
  return ioctl(fd, PWMIOC_SETCHARACTERISTICS, (unsigned long)&info);
#else
  (void)fd;
  (void)frequency;
  (void)duty;
  return 1;
#endif
}

int pwm_start_output(int fd)
{
  return ioctl(fd, PWMIOC_START, 0);
}

int pwm_stop_output(int fd)
{
  return ioctl(fd, PWMIOC_STOP, 0);
}

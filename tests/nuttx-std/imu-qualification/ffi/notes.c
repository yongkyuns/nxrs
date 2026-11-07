/* Target-header ioctl bindings for the S8 scheduler-note fixture. */
#include <nuttx/config.h>
#include <errno.h>
#include <string.h>
#include <sys/ioctl.h>
#include <nuttx/irq.h>
#include <nuttx/note/notectl_driver.h>
#include <nuttx/note/noteram_driver.h>

#if defined(CONFIG_DRIVERS_NOTECTL) && defined(CONFIG_DRIVERS_NOTERAM) && \
    defined(CONFIG_ARCH_PERF_EVENTS) && defined(CONFIG_PERF_OVERFLOW_CORRECTION) && \
    !defined(CONFIG_SMP)
#  define IMU_NOTES_AVAILABLE
#endif

#if defined(IMU_NOTES_AVAILABLE) && \
    defined(CONFIG_SCHED_INSTRUMENTATION_IRQHANDLER) && \
    defined(CONFIG_SCHED_INSTRUMENTATION_SWITCH) && \
    defined(CONFIG_ESP32S3_DEVKIT_GPIO_IRQ_RISING)
#  define IMU_GPIO_NOTES
#endif

int imu_notes_gpio_irq(void)
{
#ifdef IMU_GPIO_NOTES
  return ESP32S3_IRQ_GPIO_INT_CPU;
#else
  return -1;
#endif
}

int imu_notes_i2c(void)
{
#if defined(IMU_NOTES_AVAILABLE) && defined(CONFIG_I2C_SCHED_NOTES)
  return 1;
#else
  return 0;
#endif
}

int imu_notes_enable(int fd, int enabled)
{
#ifdef IMU_NOTES_AVAILABLE
  struct note_filter_mode_s mode = {0};

  if (enabled)
    {
#ifdef IMU_GPIO_NOTES
      struct note_filter_irq_s mask;
      memset(&mask, 0xff, sizeof(mask));
      NOTE_FILTER_IRQMASK_CLR(ESP32S3_IRQ_GPIO_INT_CPU, &mask);
      if (ioctl(fd, NOTECTL_SETIRQFILTER, (unsigned long)&mask) < 0)
        {
          return -1;
        }

      mode.flag |= NOTE_FILTER_MODE_FLAG_SWITCH | NOTE_FILTER_MODE_FLAG_IRQ;
#endif
#ifdef CONFIG_I2C_SCHED_NOTES
      mode.flag |= NOTE_FILTER_MODE_FLAG_DUMP;
#endif
      mode.flag |= NOTE_FILTER_MODE_FLAG_ENABLE;
    }

  return ioctl(fd, NOTECTL_SETMODE, (unsigned long)&mode);
#else
  errno = ENOSYS;
  return -1;
#endif
}

int imu_notes_clear(int fd)
{
#ifdef IMU_NOTES_AVAILABLE
  return ioctl(fd, NOTERAM_CLEAR, 0ul);
#else
  errno = ENOSYS;
  return -1;
#endif
}

int imu_notes_mode(int fd)
{
#ifdef IMU_NOTES_AVAILABLE
  unsigned int mode;
  return ioctl(fd, NOTERAM_GETMODE, (unsigned long)&mode) < 0 ? -1 : (int)mode;
#else
  errno = ENOSYS;
  return -1;
#endif
}

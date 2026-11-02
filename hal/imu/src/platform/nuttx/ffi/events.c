/* Target poll/pipe ABI only. Rust owns waits, cancellation and device policy. */
#include <nuttx/config.h>
#include <poll.h>
#include <fcntl.h>
#include <unistd.h>
#include <errno.h>
int imu_event_pipe(int descriptors[2])
{
  return pipe2(descriptors, O_NONBLOCK | O_CLOEXEC);
}
int imu_event_wait(int device, int cancel)
{
  struct pollfd fds[2] = {
    { .fd = device, .events = POLLIN },
    { .fd = cancel, .events = POLLIN }
  };
  int result = poll(fds, 2, -1);
  if (result < 0) return -1;
  if (fds[1].revents & POLLIN) return 0;
  if ((fds[0].revents | fds[1].revents) & (POLLERR | POLLHUP | POLLNVAL))
    { errno = EIO; return -1; }
  if (fds[0].revents & POLLIN) return 1;
  errno = EINTR;
  return -1;
}

#include <nuttx/ioexpander/gpio.h>
#include <stdbool.h>
#include <stdint.h>
#include <sys/ioctl.h>
int imu_irq_check(int fd)
{
  enum gpio_pintype_e type;
  if (ioctl(fd, GPIOC_PINTYPE, (unsigned long)(uintptr_t)&type) < 0) return -1;
  if (type != GPIO_INTERRUPT_RISING_PIN) { errno = EINVAL; return -1; }
  return 0;
}
int imu_irq_register(int fd) { return ioctl(fd, GPIOC_REGISTER, 0); }
int imu_irq_unregister(int fd) { return ioctl(fd, GPIOC_UNREGISTER, 0); }
int imu_irq_read(int fd)
{
  bool value = false;
  if (ioctl(fd, GPIOC_READ, (unsigned long)(uintptr_t)&value) < 0) return -1;
  return value ? 1 : 0;
}

/* Individual target GPIO ioctls; ownership and failure policy stay in Rust. */
#include <nuttx/config.h>
#include <errno.h>
#include <stdbool.h>
#ifdef CONFIG_DEV_GPIO
#include <nuttx/ioexpander/gpio.h>
#include <stdint.h>
#include <sys/ioctl.h>
int gpio_resource_irq_check(int fd)
{
  enum gpio_pintype_e type;
  if (ioctl(fd, GPIOC_PINTYPE, (unsigned long)(uintptr_t)&type) < 0) return -1;
  if (type != GPIO_INTERRUPT_RISING_PIN) { errno = EINVAL; return -1; }
  return 0;
}
int gpio_check_pin(int fd, bool output)
{
  enum gpio_pintype_e type;
  if (ioctl(fd, GPIOC_PINTYPE, (unsigned long)(uintptr_t)&type) < 0) return -1;

  bool compatible = output ? type == GPIO_OUTPUT_PIN ||
                             type == GPIO_OUTPUT_PIN_OPENDRAIN
                           : type == GPIO_INPUT_PIN ||
                             type == GPIO_INPUT_PIN_PULLUP ||
                             type == GPIO_INPUT_PIN_PULLDOWN ||
                             (type >= GPIO_INTERRUPT_PIN && type < GPIO_NPINTYPES);
  if (!compatible) { errno = EINVAL; return -1; }
  return 0;
}
int gpio_resource_irq_register(int fd) { return ioctl(fd, GPIOC_REGISTER, 0); }
int gpio_resource_irq_unregister(int fd) { return ioctl(fd, GPIOC_UNREGISTER, 0); }
int gpio_resource_irq_read(int fd)
{
  bool value = false;
  if (ioctl(fd, GPIOC_READ, (unsigned long)(uintptr_t)&value) < 0) return -1;
  return value ? 1 : 0;
}
#else
/* The package can be linked by mock-only products without the GPIO ABI. */
int gpio_check_pin(int fd, bool output)
{ (void)fd; (void)output; errno = ENOTSUP; return -1; }
int gpio_resource_irq_check(int fd)
{ (void)fd; errno = ENOTSUP; return -1; }
int gpio_resource_irq_register(int fd)
{ (void)fd; errno = ENOTSUP; return -1; }
int gpio_resource_irq_unregister(int fd)
{ (void)fd; errno = ENOTSUP; return -1; }
int gpio_resource_irq_read(int fd)
{ (void)fd; errno = ENOTSUP; return -1; }
#endif

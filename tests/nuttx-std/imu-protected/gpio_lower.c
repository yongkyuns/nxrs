/* Test-only GPIO lower half. A one-shot kernel timer supplies the edge. */
#include <nuttx/config.h>
#include <nuttx/clock.h>
#include <nuttx/ioexpander/gpio.h>
#include <nuttx/irq.h>
#include <nuttx/wdog.h>

#ifdef CONFIG_SMP
#  error "The one-shot GPIO witness requires a single-core test image"
#endif

static struct wdog_s edge_timer;
static pin_interrupt_t edge_handler;
static bool high;

static int read_pin(struct gpio_dev_s *dev, bool *value)
{
  irqstate_t flags = enter_critical_section();
  (void)dev;
  *value = high;
  leave_critical_section(flags);
  return 0;
}

static int attach_pin(struct gpio_dev_s *dev, pin_interrupt_t callback)
{
  (void)dev;
  edge_handler = callback;
  return 0;
}

static void send_edge(wdparm_t arg)
{
  struct gpio_dev_s *dev = (void *)(uintptr_t)arg;
  high = true;
  edge_handler(dev, 0);
}

static int enable_pin(struct gpio_dev_s *dev, bool enabled)
{
  if (enabled)
    {
      return wd_start(&edge_timer, MSEC2TICK(100), send_edge,
                      (wdparm_t)(uintptr_t)dev);
    }

  irqstate_t flags = enter_critical_section();
  wd_cancel(&edge_timer);
  high = false;
  leave_critical_section(flags);
  return 0;
}

static const struct gpio_operations_s gpio_ops =
{
  .go_read = read_pin,
  .go_attach = attach_pin,
  .go_enable = enable_pin,
};

static struct gpio_dev_s gpio_pin =
{
  .gp_pintype = GPIO_INTERRUPT_RISING_PIN,
  .gp_ops = &gpio_ops,
};

int imu_witness_gpio_register(void)
{
  return gpio_pin_register_byname(&gpio_pin, "gpio-imu-witness");
}

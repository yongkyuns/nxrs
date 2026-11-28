/* SPDX-License-Identifier: Apache-2.0 */
/* Board registration only; device protocols belong to Rust capability HALs. */
#include <nuttx/config.h>
#include <nuttx/board.h>
#include <nuttx/arch.h>
#include <arch/board/board.h>
#include <nuttx/fs/fs.h>
#include <syslog.h>
#include "esp32s3_i2c.h"
#ifdef CONFIG_ESP32S3_M5_CORES3_GPIO10_IRQ
#  include <nuttx/ioexpander/gpio.h>
#  include "espressif/esp_gpio.h"
#endif
#ifdef CONFIG_WATCHDOG
#  include "esp32s3_board_wdt.h"
#endif
#ifdef CONFIG_ESP32S3_M5_CORES3_LCD
int cores3_lcd_bus_initialize(void);
#endif
#ifdef CONFIG_ESP32S3_M5_CORES3_TOUCH
int cores3_touch_initialize(void);
#endif
#if defined(CONFIG_ESP32S3_MTD) && defined(CONFIG_MTD_CONFIG_NVS)
int board_spiflash_init(void);
#endif
#ifdef CONFIG_ESPRESSIF_BLE
int esp32s3_ble_initialize(void);
#endif
#ifdef CONFIG_ESP32S3_M5_CORES3_GPIO10_IRQ
static pin_interrupt_t g_gpio10_callback;

static void gpio10_interrupt(void *arg)
{
  g_gpio10_callback(arg, 10);
}

static int gpio10_read(struct gpio_dev_s *dev, bool *value)
{
  *value = esp_gpioread(10);
  return 0;
}

static int gpio10_attach(struct gpio_dev_s *dev, pin_interrupt_t callback)
{
  if (callback != NULL)
    {
      g_gpio10_callback = callback;
    }

  return esp_gpio_isr(10, callback ? gpio10_interrupt : NULL, dev);
}

static int gpio10_enable(struct gpio_dev_s *dev, bool enable)
{
  return enable ? esp_gpioirqenable(10) : esp_gpioirqdisable(10);
}

static const struct gpio_operations_s g_gpio10_ops =
{
  .go_read = gpio10_read,
  .go_attach = gpio10_attach,
  .go_enable = gpio10_enable,
};

static struct gpio_dev_s g_gpio10 =
{
  .gp_pintype = GPIO_INTERRUPT_RISING_PIN,
  .gp_ops = &g_gpio10_ops,
};
#endif

void esp32s3_board_initialize(void) {}
#ifdef CONFIG_BOARD_LATE_INITIALIZE
void board_late_initialize(void)
{
#ifdef CONFIG_ESP32S3_M5_CORES3_GPIO10_IRQ
  int gpio10 = esp_configgpio(10, INPUT_FUNCTION_2 | PULLDOWN | RISING);
  if (gpio10 >= 0)
    {
      gpio10 = gpio_pin_register(&g_gpio10, 10);
    }

  if (gpio10 < 0)
    {
      syslog(LOG_ERR, "CoreS3 GPIO10 registration failed: %d\n", gpio10);
    }
#endif
#ifdef CONFIG_WATCHDOG
  int watchdog = board_wdt_init();
  if (watchdog < 0)
    {
      syslog(LOG_ERR, "CoreS3 watchdog registration failed: %d\n",
             watchdog);
    }
#endif
#ifdef CONFIG_I2C_DRIVER
  int result = board_i2c_init();
  if (result < 0) syslog(LOG_ERR, "CoreS3 I2C registration failed: %d\n", result);
#endif
#ifdef CONFIG_FS_PROCFS
  int mounted = nx_mount(NULL, "/proc", "procfs", 0, NULL);
  if (mounted < 0) syslog(LOG_ERR, "CoreS3 procfs mount failed: %d\n", mounted);
#endif
#if defined(CONFIG_ESP32S3_MTD) && defined(CONFIG_MTD_CONFIG_NVS)
  int storage = board_spiflash_init();
  if (storage < 0)
    {
      syslog(LOG_ERR, "CoreS3 settings partition registration failed: %d\n",
             storage);
    }

#endif
#ifdef CONFIG_ESPRESSIF_BLE
  int radio = esp32s3_ble_initialize();
  if (radio < 0)
    {
      syslog(LOG_ERR, "CoreS3 BLE registration failed: %d\n", radio);
    }
#endif
#ifdef CONFIG_ESP32S3_M5_CORES3_LCD
  int lcd = cores3_lcd_bus_initialize();
  if (lcd < 0) syslog(LOG_ERR, "CoreS3 LCD bus registration failed: %d\n", lcd);
#endif
#ifdef CONFIG_ESP32S3_M5_CORES3_TOUCH
  int touch = cores3_touch_initialize();
  if (touch < 0)
    {
      syslog(LOG_ERR, "CoreS3 touch registration failed: %d\n", touch);
    }
#endif
}
#endif
#ifdef CONFIG_BOARDCTL_RESET
int board_reset(int status)
{
  (void)status;
  up_systemreset();
  return 0;
}
#endif

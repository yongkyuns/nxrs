/* SPDX-License-Identifier: Apache-2.0 */

#include <nuttx/arch.h>
#include <nuttx/i2c/i2c_master.h>
#ifdef CONFIG_ESP32S3_M5_CORES3_TOUCH
#  include <nuttx/input/ft5x06.h>
#endif
#include <nuttx/spi/spi.h>
#include <nuttx/spi/spi_transfer.h>
#include <errno.h>

#include "espressif/esp_gpio.h"
#include "esp32s3_i2c.h"
#include "esp32s3_spi.h"

#define LCD_DC        35
#define SD_CS         4
#define AW9523_ADDR   0x58
#define AXP2101_ADDR  0x34
#define LCD_RESET     (1 << 1) /* AW9523 P1_1 */
#define LCD_BACKLIGHT (1 << 7) /* AXP2101 DLDO1 */

static int set_bits(struct i2c_master_s *bus, uint8_t address, uint8_t reg,
                    uint8_t mask, uint8_t bits)
{
  struct i2c_config_s config =
    {
      .frequency = I2C_SPEED_STANDARD,
      .address = address,
      .addrlen = 7,
    };
  uint8_t data[2] = {reg};
  int ret = i2c_writeread(bus, &config, &reg, 1, &data[1], 1);

  if (ret < 0)
    {
      return ret;
    }

  data[1] = (data[1] & ~mask) | bits;
  return i2c_write(bus, &config, data, sizeof(data));
}

uint8_t esp32s3_spi2_status(struct spi_dev_s *dev, uint32_t devid)
{
  return devid == SPIDEV_DISPLAY(0) ? SPI_STATUS_PRESENT : 0;
}

int esp32s3_spi2_cmddata(struct spi_dev_s *dev, uint32_t devid, bool command)
{
  if (devid != SPIDEV_DISPLAY(0))
    {
      return -ENODEV;
    }

  esp_gpiowrite(LCD_DC, !command);
  return OK;
}

int cores3_lcd_bus_initialize(void)
{
  struct i2c_master_s *i2c = esp32s3_i2cbus_initialize(0);
  struct spi_dev_s *spi = NULL;
  int ret;

  if (i2c == NULL)
    {
      return -ENODEV;
    }

  /* Prevent the SD slot from driving GPIO35 while the LCD owns that pin. */

  esp_gpiowrite(SD_CS, true);
  esp_configgpio(SD_CS, OUTPUT | PULLUP);
  esp_gpiowrite(LCD_DC, true);
  esp_configgpio(LCD_DC, OUTPUT);

  /* Keep the backlight off while pulsing the external active-low reset.
   * Preserve the other power rails and expander outputs.
   */

  ret = set_bits(i2c, AXP2101_ADDR, 0x90, LCD_BACKLIGHT, 0);
  if (ret < 0)
    {
      goto out;
    }

  ret = set_bits(i2c, AW9523_ADDR, 0x03, LCD_RESET, 0);
  if (ret < 0)
    {
      goto out;
    }

  ret = set_bits(i2c, AW9523_ADDR, 0x13, LCD_RESET, LCD_RESET);
  if (ret < 0)
    {
      goto out;
    }

  ret = set_bits(i2c, AW9523_ADDR, 0x05, LCD_RESET, 0);
  if (ret < 0)
    {
      goto out;
    }

  up_mdelay(10);
  ret = set_bits(i2c, AW9523_ADDR, 0x03, LCD_RESET, LCD_RESET);
  if (ret < 0)
    {
      goto out;
    }

  up_mdelay(120);
  spi = esp32s3_spibus_initialize(ESP32S3_SPI2);
  if (spi == NULL)
    {
      ret = -ENODEV;
      goto out;
    }

  /* 2.6 V is within the CoreS3 panel backlight supply range. */

  ret = set_bits(i2c, AXP2101_ADDR, 0x99, 0x1f, 21);
  if (ret >= 0)
    {
      ret = set_bits(i2c, AXP2101_ADDR, 0x90, LCD_BACKLIGHT, LCD_BACKLIGHT);
    }

  if (ret >= 0)
    {
      ret = spi_register(spi, ESP32S3_SPI2);
    }

  if (ret < 0)
    {
      set_bits(i2c, AXP2101_ADDR, 0x90, LCD_BACKLIGHT, 0);
      esp32s3_spibus_uninitialize(spi);
    }

out:
  esp32s3_i2cbus_uninitialize(i2c);
  return ret;
}

#ifdef CONFIG_ESP32S3_M5_CORES3_TOUCH

int cores3_touch_initialize(void)
{
  const uint8_t reset = 1 << 0; /* AW9523 P0_0 */
  static const struct ft5x06_config_s config =
  {
    .lower = { .xres = 320, .yres = 240, .maxpoint = 1 },
    .address = 0x38,
    .frequency = I2C_SPEED_FAST,
  };
  struct i2c_master_s *i2c = esp32s3_i2cbus_initialize(0);
  int ret;

  if (i2c == NULL)
    {
      return -ENODEV;
    }

  ret = set_bits(i2c, AW9523_ADDR, 0x02, reset, 0);
  if (ret >= 0)
    {
      ret = set_bits(i2c, AW9523_ADDR, 0x04, reset, 0);
    }

  if (ret >= 0)
    {
      up_mdelay(5);
      ret = set_bits(i2c, AW9523_ADDR, 0x02, reset, reset);
    }

  if (ret >= 0)
    {
      up_mdelay(50);
      ret = ft5x06_register(i2c, &config, 0);
    }

  if (ret < 0)
    {
      esp32s3_i2cbus_uninitialize(i2c);
    }

  return ret;
}
#endif

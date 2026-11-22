/* Tracker device models below the real NuttX I2C, SPI, touch and MTD drivers. */
#include <nuttx/config.h>
#include <nuttx/clock.h>
#include <nuttx/i2c/i2c_master.h>
#include <nuttx/input/ft5x06.h>
#include <nuttx/mtd/configdata.h>
#include <nuttx/mtd/mtd.h>
#include <nuttx/mutex.h>
#include <nuttx/spi/spi.h>
#include <nuttx/spi/spi_transfer.h>
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

struct bmi270
{
  uint8_t registers[128];
  size_t image_bytes;
};

static struct bmi270 g_imus[2];
static mutex_t g_i2c_lock = NXMUTEX_INITIALIZER;
static mutex_t g_spi_lock = NXMUTEX_INITIALIZER;
static bool g_touched;
static bool g_lcd_cmd;
static uint8_t g_lcd_register;
static uint8_t g_pixel[2];
static uint8_t g_flash[16384];

#ifdef CONFIG_ARCH_SIM
static clock_t g_imu_failure_at;
static bool g_stall_panel;
#endif

#ifdef CONFIG_BLUETOOTH_UART_SHIM
#define WIDTH 320
#define HEIGHT 240

/* QTest reads an RGB565 palette and four-bit indices from guest memory.
 * An odd sequence means that a full-panel redraw is in progress.
 */
uint8_t tracker_pixels[WIDTH * HEIGHT / 2];
uint16_t tracker_colors[16];
volatile uint32_t tracker_frame_sequence;
volatile uint32_t tracker_touch_input = 1u << 30;
volatile uint32_t tracker_touch_reads;
volatile uint32_t tracker_model_error;
static unsigned int g_color_count;
static unsigned int g_column, g_last_column, g_page, g_last_page;
static unsigned int g_x, g_y;

static unsigned int read_be16(const uint8_t *bytes)
{
  return ((unsigned int)bytes[0] << 8) | bytes[1];
}

static void draw_pixels(const uint8_t *bytes, size_t size)
{
  unsigned int color = 0;
  for (size_t i = 0; i + 1 < size; i += 2)
    {
      uint16_t rgb = read_be16(bytes + i);
      if (g_x > g_last_column || g_y > g_last_page ||
          g_x >= WIDTH || g_y >= HEIGHT)
        {
          tracker_model_error = 1;
          return;
        }
      if (color >= g_color_count || tracker_colors[color] != rgb)
        {
          for (color = 0; color < g_color_count; color++)
            {
              if (tracker_colors[color] == rgb)
                {
                  break;
                }
            }
        }
      if (color == g_color_count)
        {
          if (color >= 16)
            {
              tracker_model_error = 2;
              return;
            }
          tracker_colors[g_color_count++] = rgb;
        }
      if (g_x == 0 && (tracker_frame_sequence & 1) == 0)
        {
          tracker_frame_sequence++;
        }
      unsigned int index = g_y * WIDTH + g_x;
      unsigned int shift = (index & 1) * 4;
      tracker_pixels[index / 2] =
        (tracker_pixels[index / 2] & ~(15 << shift)) | (color << shift);
      if (g_x == WIDTH - 1 && g_y == HEIGHT - 1)
        {
          tracker_frame_sequence++;
        }
      if (g_x++ == g_last_column)
        {
          g_x = g_column;
          g_y++;
        }
    }
}

static void panel_exchange(const uint8_t *bytes, size_t size)
{
  if (g_lcd_cmd && size == 1 && bytes[0] == 0x2c)
    {
      g_x = g_column;
      g_y = g_page;
    }
  else if (!g_lcd_cmd && size == 4 && g_lcd_register == 0x2a)
    {
      g_column = read_be16(bytes);
      g_last_column = read_be16(bytes + 2);
    }
  else if (!g_lcd_cmd && size == 4 && g_lcd_register == 0x2b)
    {
      g_page = read_be16(bytes);
      g_last_page = read_be16(bytes + 2);
    }
  else if (!g_lcd_cmd && g_lcd_register == 0x2c)
    {
      draw_pixels(bytes, size);
    }
}
#endif

static int bmi_write(struct bmi270 *chip, uint8_t reg,
                     const uint8_t *data, size_t size)
{
  if (reg == 0x7e && size == 1 && data[0] == 0xb6)
    {
      memset(chip, 0, sizeof(*chip));
      chip->registers[0] = 0x24; /* CHIP_ID */
      chip->registers[3] = 0xc0; /* STATUS: accel and gyro ready */
    }
  else if (reg == 0x5e)
    {
      /* INIT_DATA follows INIT_ADDR and writes the contiguous Bosch image. */
      size_t offset = (chip->registers[0x5b] |
                       (chip->registers[0x5c] << 4)) * 2;
      if (offset != chip->image_bytes || offset + size > 8192)
        {
          return -EIO;
        }
      chip->image_bytes += size;
    }
  else if (reg + size <= sizeof(chip->registers))
    {
      memcpy(&chip->registers[reg], data, size);
      if (reg == 0x59 && data[0] == 1 && chip->image_bytes == 8192)
        {
          chip->registers[0x21] = 1;
        }
    }
  else
    {
      return -EINVAL;
    }
  return 0;
}

static int model_read(int address, uint8_t reg, uint8_t *data, size_t size)
{
  memset(data, 0, size);
  if (address == 0x68 || address == 0x69)
    {
      const struct bmi270 *chip = &g_imus[address - 0x68];
      if (reg == 0x24 && size == 2)
        {
#ifdef CONFIG_ARCH_SIM
          /* Let the primary report live samples before failing its FIFO read. */
          if (address == 0x69 && g_imu_failure_at &&
              clock_systime_ticks() >= g_imu_failure_at)
            {
              return -EIO;
            }
#endif
          data[0] = chip->registers[0x49] == 0xc0 ? 12 : 0;
        }
      else if (reg == 0x26 && size == 12)
        {
          data[7] = address == 0x68 ? 0x20 : 0xe0; /* opposite accel X */
          data[11] = 0x20;
        }
      else if (reg + size <= sizeof(chip->registers))
        {
          memcpy(data, &chip->registers[reg], size);
        }
      else
        {
          return -EINVAL;
        }
      return 0;
    }
  if (address == 0x34)
    {
      if (reg == 0 && size == 2)
        {
          data[0] = 0x28; /* USB present and battery detected */
          data[1] = 0x20; /* charging */
        }
      else if (reg == 0x18 && size == 1)
        {
          data[0] = 8;
        }
      else if (reg == 0xa4 && size == 1)
        {
          data[0] = 73;
        }
      else
        {
          return -EINVAL;
        }
      return 0;
    }
  if (address == 0x38 && reg == 1 && size >= 8)
    {
#ifdef CONFIG_BLUETOOTH_UART_SHIM
      uint32_t touch = tracker_touch_input;
      unsigned int event = touch >> 30;
      data[1] = event == 0 || event == 2;
      data[2] = ((touch >> 8) & 0x0f) | (event << 6);
      data[3] = touch;
      data[4] = (touch >> 24) & 0x0f;
      data[5] = touch >> 16;
      tracker_touch_reads++;
#else
      data[1] = g_touched ? 1 : 0;
      data[2] = g_touched ? 0 : 0x40;
      data[3] = 87;
      data[5] = 112;
#endif
      return 0;
    }
  return -ENXIO;
}

static int i2c_transfer(struct i2c_master_s *dev, struct i2c_msg_s *msgs,
                        int count)
{
  int ret = -EINVAL;
  if (count < 1 || msgs[0].length < 1 || (msgs[0].flags & I2C_M_READ))
    {
      return ret;
    }
  int address = msgs[0].addr;
  uint8_t reg = msgs[0].buffer[0];
  nxmutex_lock(&g_i2c_lock);
  if (count == 2 && msgs[0].length == 1 && msgs[1].addr == address &&
      (msgs[1].flags & I2C_M_READ))
    {
      ret = model_read(address, reg, msgs[1].buffer, msgs[1].length);
    }
  else if (count == 1 && msgs[0].length > 1)
    {
      if (address == 0x68 || address == 0x69)
        {
          ret = bmi_write(&g_imus[address - 0x68], reg, msgs[0].buffer + 1,
                          msgs[0].length - 1);
        }
      else if (address == 0x38 && reg == 0)
        {
          ret = 0;
        }
      else
        {
          ret = -ENXIO;
        }
    }
  nxmutex_unlock(&g_i2c_lock);
  return ret;
}

static const struct i2c_ops_s g_i2c_ops = { .transfer = i2c_transfer };
static struct i2c_master_s g_i2c = { .ops = &g_i2c_ops };

static int spi_lock(struct spi_dev_s *dev, bool lock)
{
  return lock ? nxmutex_lock(&g_spi_lock) : nxmutex_unlock(&g_spi_lock);
}

static void spi_select(struct spi_dev_s *dev, uint32_t id, bool selected)
{
}

static uint32_t spi_frequency(struct spi_dev_s *dev, uint32_t frequency)
{
  return frequency;
}

static int spi_cmddata(struct spi_dev_s *dev, uint32_t id, bool cmd)
{
  g_lcd_cmd = cmd;
  return 0;
}

static void spi_exchange(struct spi_dev_s *dev, const void *tx, void *rx,
                         size_t size)
{
  const uint8_t *bytes = tx;
  if (bytes == NULL)
    {
      return;
    }
#ifdef CONFIG_BLUETOOTH_UART_SHIM
  panel_exchange(bytes, size);
#endif
  if (g_lcd_cmd && size == 1)
    {
      g_lcd_register = bytes[0];
#ifdef CONFIG_ARCH_SIM
      if (g_lcd_register == 0x01) /* Each app launch resets the panel. */
        {
          const char *scenario = getenv("TRACKER_SIM_CASE");
          nxmutex_lock(&g_i2c_lock);
          g_imu_failure_at = scenario && strcmp(scenario, "imu") == 0
                            ? clock_systime_ticks() + SEC2TICK(2) : 0;
          nxmutex_unlock(&g_i2c_lock);
          g_stall_panel = scenario && strcmp(scenario, "queue") == 0;
        }
      else if (g_stall_panel && g_lcd_register == 0x2c)
        {
          /* Delay the app's first paint while acquisition threads keep running. */
          g_stall_panel = false;
          puts("TRACKER_PANEL_STALL");
          usleep(1000000);
        }
#endif
    }
  else if (!g_lcd_cmd && g_lcd_register == 0x2c && size == 2)
    {
      memcpy(g_pixel, bytes, sizeof(g_pixel));
    }
}

static const struct spi_ops_s g_spi_ops =
{
  .lock = spi_lock,
  .select = spi_select,
  .setfrequency = spi_frequency,
  .cmddata = spi_cmddata,
  .exchange = spi_exchange,
};
static struct spi_dev_s g_spi = { .ops = &g_spi_ops };

int tracker_sim_devices(void)
{
  static const struct ft5x06_config_s touch =
  {
    .lower = { .xres = 320, .yres = 240, .maxpoint = 1 },
    .address = 0x38,
    .frequency = I2C_SPEED_FAST,
  };
  g_imus[0].registers[0] = g_imus[1].registers[0] = 0x24;
  int ret = i2c_register(&g_i2c, 0);
  if (ret >= 0)
    {
      ret = ft5x06_register(&g_i2c, &touch, 0);
    }
  if (ret >= 0)
    {
      ret = spi_register(&g_spi, 2);
    }
  if (ret >= 0)
    {
      memset(g_flash, 0xff, sizeof(g_flash));
      struct mtd_dev_s *memory = rammtd_initialize(g_flash, sizeof(g_flash));
      ret = memory ? mtdconfig_register(memory) : -ENOMEM;
    }
  return ret;
}

uint16_t tracker_sim_pixel(void)
{
  return ((uint16_t)g_pixel[0] << 8) | g_pixel[1];
}

void tracker_sim_touch(void)
{
  nxmutex_lock(&g_i2c_lock);
  g_touched = true;
  nxmutex_unlock(&g_i2c_lock);
}

/* Tracker device models below the real NuttX I2C, SPI, touch and MTD drivers. */
#include <nuttx/config.h>
#include <nuttx/arch.h>
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
#include <time.h>
#include <unistd.h>
#ifdef CONFIG_RAMLOG_SYSLOG
#  include <syslog.h>
#endif

#define BMI_FRAME_BYTES 12
#define BMI_FIFO_BYTES 2048
#define BMI_PERIOD_US 10000
#define BMI_PERIOD_TICKS 256 /* 25.6 kHz sensor clock at 100 Hz ODR */

struct bmi270
{
  uint8_t registers[128];
  size_t image_bytes;
  uint64_t clock_start_us;
  uint64_t newest_slot;
  uint32_t ticks;
  size_t fifo_bytes;
};

#ifdef CONFIG_INPUT_FT5X06
#  define FIRST_IMU 0x68
#  define IMU_COUNT 2
#else
#  define FIRST_IMU 0x69
#  define IMU_COUNT 1
#endif

static struct bmi270 g_imus[IMU_COUNT];
static mutex_t g_i2c_lock = NXMUTEX_INITIALIZER;
#ifdef CONFIG_INPUT_FT5X06
static mutex_t g_spi_lock = NXMUTEX_INITIALIZER;
static bool g_touched;
static bool g_lcd_cmd;
static uint8_t g_lcd_register;
static uint8_t g_pixel[2];
static uint8_t g_flash[16384];
#endif

#ifdef CONFIG_ARCH_SIM
static bool g_inject_imu_failure;
static bool g_stall_panel;

/* Host replay preconverts samples to little-endian time and FIFO bytes. */
struct replay_imu
{
  FILE *file;
  uint64_t origin;
  uint8_t next[20];
  uint8_t fifo[2040]; /* Largest whole-frame BMI270 FIFO payload. */
  size_t length;
  size_t consumed;
  size_t samples;
  bool eof;
  bool reported;
};

static struct replay_imu g_replay[2];
static bool g_replay_mode;
static bool g_replay_started;
static uint64_t g_replay_start_ms;

static uint64_t read_le64(const uint8_t *bytes)
{
  uint64_t result = 0;
  for (unsigned int i = 0; i < 8; i++)
    {
      result |= (uint64_t)bytes[i] << (i * 8);
    }
  return result;
}

static uint64_t replay_ms(void)
{
  struct timespec now;
  clock_gettime(CLOCK_MONOTONIC, &now);
  return (uint64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000;
}

static int replay_next(struct replay_imu *imu)
{
  size_t size = fread(imu->next, 1, sizeof(imu->next), imu->file);
  if (size == sizeof(imu->next))
    {
      return 0;
    }
  imu->eof = true;
  bool end = size == 0 && feof(imu->file);
  fclose(imu->file);
  imu->file = NULL;
  return end ? 0 : -EIO;
}

static void replay_report(unsigned int index)
{
  struct replay_imu *imu = &g_replay[index];
  if (g_replay_started && imu->eof && !imu->reported && imu->consumed == imu->length)
    {
      imu->reported = true;
      printf("TRACKER_REPLAY_IMU_DONE %u %zu\n", index, imu->samples);
      fflush(stdout);
    }
}

static int replay_length(unsigned int index, uint8_t *data)
{
  struct replay_imu *imu = &g_replay[index];
  if (!imu->file && !imu->eof)
    {
      const char *path = index == 0 ? "/tracker-fixture/imu-secondary.bin" :
                                     "/tracker-fixture/imu-primary.bin";
      imu->file = fopen(path, "rb");
      uint8_t origin[8];
      if (!imu->file || fread(origin, 1, sizeof(origin), imu->file) != sizeof(origin))
        {
          if (imu->file)
            {
              fclose(imu->file);
              imu->file = NULL;
            }
          return -EIO;
        }
      imu->origin = read_le64(origin);
      if (replay_next(imu) < 0)
        {
          return -EIO;
        }
    }
  if (!g_replay_started && (g_replay[0].file || g_replay[0].eof) &&
      (g_replay[1].file || g_replay[1].eof) &&
      access("/tracker-fixture/replay-ready", F_OK) == 0)
    {
      g_replay_start_ms = replay_ms();
      g_replay_started = true;
      puts("TRACKER_REPLAY_START");
      fflush(stdout);
    }
  if (g_replay_started && imu->consumed == imu->length)
    {
      uint64_t elapsed = replay_ms() - g_replay_start_ms;
      imu->length = 0;
      imu->consumed = 0;
      while (!imu->eof && imu->length < sizeof(imu->fifo) &&
             read_le64(imu->next) - imu->origin <= elapsed)
        {
          memcpy(imu->fifo + imu->length, imu->next + 8, 12);
          imu->length += 12;
          if (replay_next(imu) < 0)
            {
              return -EIO;
            }
        }
    }
  size_t remaining = imu->length - imu->consumed;
  data[0] = remaining & 0xff;
  data[1] = remaining >> 8;
  replay_report(index);
  return 0;
}
#endif

#ifdef CONFIG_BLUETOOTH_UART_SHIM
#define WIDTH 320
#define HEIGHT 240

/* QTest reads an RGB565 palette and four-bit indices from guest memory.
 * An odd sequence means that a full-panel redraw is in progress.
 */
#ifdef TRACKER_MAP_PANEL
/* The CoreS3 map square fits inside this region with the menu open or closed.
 * Retain the map and UI colors in separate RGB565 palettes.
 */
#define MAP_X 75
#define MAP_Y 38
#define MAP_SIDE 170
uint8_t tracker_panel_center[MAP_SIDE * MAP_SIDE];
uint16_t tracker_center_colors[64];
static unsigned int g_center_count;
uint8_t tracker_pixels[(WIDTH * HEIGHT - sizeof(tracker_panel_center)) / 2];
#else
uint8_t tracker_pixels[WIDTH * HEIGHT / 2];
#endif
uint16_t tracker_colors[16];
volatile uint32_t tracker_frame_sequence;
volatile uint32_t tracker_touch_input = 1u << 30;
volatile uint32_t tracker_touch_reads;
volatile uint32_t tracker_model_error;
volatile uint32_t tracker_panel_work_us;
volatile uint32_t tracker_panel_busy_rows;
static unsigned int g_color_count;
static unsigned int g_column, g_last_column, g_page, g_last_page;
static unsigned int g_x, g_y;

static unsigned int read_be16(const uint8_t *bytes)
{
  return ((unsigned int)bytes[0] << 8) | bytes[1];
}

static unsigned int make_index(uint16_t rgb, uint16_t *palette,
                               unsigned int *count, unsigned int capacity,
                               unsigned int *last_color)
{
  unsigned int color = *last_color;
  if (color >= *count || palette[color] != rgb)
    {
      for (color = 0; color < *count; color++)
        {
          if (palette[color] == rgb)
            {
              break;
            }
        }
    }
  if (color == *count && color < capacity)
    {
      palette[(*count)++] = rgb;
    }
  *last_color = color;
  return color;
}

static bool store_pixel(uint16_t rgb, unsigned int index,
                        unsigned int *last_colors)
{
#ifdef TRACKER_MAP_PANEL
  if (g_y >= MAP_Y && g_y < MAP_Y + MAP_SIDE)
    {
      if (g_x >= MAP_X && g_x < MAP_X + MAP_SIDE)
        {
          unsigned int color = make_index(rgb, tracker_center_colors,
                                          &g_center_count, 64, &last_colors[1]);
          if (color >= 64)
            {
              return false;
            }
          tracker_panel_center[(g_y - MAP_Y) * MAP_SIDE + g_x - MAP_X] = color;
          return true;
        }
      index -= (g_y - MAP_Y) * MAP_SIDE;
      if (g_x >= MAP_X + MAP_SIDE)
        {
          index -= MAP_SIDE;
        }
    }
  else if (g_y >= MAP_Y + MAP_SIDE)
    {
      index -= sizeof(tracker_panel_center);
    }
#endif

  unsigned int color = make_index(rgb, tracker_colors, &g_color_count, 16,
                                  &last_colors[0]);
  if (color >= 16)
    {
      return false;
    }
  unsigned int shift = (index & 1) * 4;
  tracker_pixels[index / 2] =
    (tracker_pixels[index / 2] & ~(15 << shift)) | (color << shift);
  return true;
}

static void draw_pixels(const uint8_t *bytes, size_t size)
{
  unsigned int colors[2] = {0};
  for (size_t i = 0; i + 1 < size; i += 2)
    {
      uint16_t rgb = read_be16(bytes + i);
      if (g_x > g_last_column || g_y > g_last_page ||
          g_x >= WIDTH || g_y >= HEIGHT)
        {
          tracker_model_error = 1;
          return;
        }
      if (g_x == 0 && (tracker_frame_sequence & 1) == 0)
        {
          tracker_frame_sequence++;
        }
      if (!store_pixel(rgb, g_y * WIDTH + g_x, colors))
        {
          tracker_model_error = 2;
          return;
        }
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
      if (tracker_panel_work_us != 0 && g_y % 8 == 0)
        {
          tracker_panel_busy_rows++;
          up_udelay(tracker_panel_work_us);
        }

      draw_pixels(bytes, size);
    }
}
#endif

static int bmi_write(struct bmi270 *chip, uint8_t reg,
                     const uint8_t *data, size_t size)
{
  if (reg == 0x7e && size == 1 && data[0] == 0xb6)
    {
#ifdef CONFIG_ARCH_SIM
      const char *scenario = getenv("TRACKER_SIM_CASE");
      g_replay_mode = IMU_COUNT == 2 && scenario && strcmp(scenario, "replay") == 0;
#endif
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
#ifdef CONFIG_RAMLOG_SYSLOG
          syslog(LOG_NOTICE, "log witness: BMI270 configured\n");
#endif
        }
    }
  else
    {
      return -EINVAL;
    }
  return 0;
}

/* Materialize the samples produced since the last read on the same guest
 * monotonic clock used to bracket the BMI270 sensor-time registers.
 */
static int bmi_update_fifo(struct bmi270 *chip)
{
#ifdef CONFIG_ARCH_SIM
  if (g_replay_mode)
    {
      return 0;
    }
#endif
  if (chip->registers[0x49] != 0xc0)
    {
      return 0;
    }

  struct timespec now;
  if (clock_gettime(CLOCK_MONOTONIC, &now) < 0)
    {
      return -errno;
    }

  uint64_t now_us = (uint64_t)now.tv_sec * 1000000 + now.tv_nsec / 1000;
  if (chip->clock_start_us == 0)
    {
      chip->clock_start_us = now_us;
    }

  uint64_t elapsed = now_us - chip->clock_start_us;
  uint64_t slot = elapsed / BMI_PERIOD_US;
  uint64_t produced = slot - chip->newest_slot;
  uint64_t pending = chip->fifo_bytes + produced * BMI_FRAME_BYTES;
  chip->ticks = elapsed * BMI_PERIOD_TICKS / BMI_PERIOD_US;
  chip->newest_slot = slot;
  chip->fifo_bytes = pending < BMI_FIFO_BYTES ? pending : BMI_FIFO_BYTES;
  return 0;
}

static int model_read(int address, uint8_t reg, uint8_t *data, size_t size)
{
  memset(data, 0, size);
  if (address >= FIRST_IMU && address < FIRST_IMU + IMU_COUNT)
    {
      struct bmi270 *chip = &g_imus[address - FIRST_IMU];
      int ret = bmi_update_fifo(chip);
      if (ret < 0)
        {
          return ret;
        }

      if (reg == 0x24 && size == 2)
        {
#ifdef CONFIG_ARCH_SIM
          /* The host signals only after seeing a healthy tracker report. */
          if (address == 0x69 && g_inject_imu_failure &&
              access("/tracker-fixture/fail-imu", F_OK) == 0)
            {
              return -EIO;
            }
          if (g_replay_mode && chip->fifo_bytes == 0 &&
              chip->registers[0x49] == 0xc0)
            {
              /* Replay uses the normal 4 g / 2000 dps profile. */
              if ((chip->registers[0x41] & 3) != 1 ||
                  (chip->registers[0x43] & 7) != 0)
                {
                  return -EINVAL;
                }

              ret = replay_length(address - FIRST_IMU, data);
              if (ret < 0)
                {
                  return ret;
                }

              chip->fifo_bytes = data[0] | (data[1] << 8);
              chip->ticks += BMI_PERIOD_TICKS *
                             (chip->fifo_bytes / BMI_FRAME_BYTES);
            }
#endif
          data[0] = chip->fifo_bytes & 0xff;
          data[1] = chip->fifo_bytes >> 8;
        }
      else if (reg == 0x18 && size == 3)
        {
          data[0] = chip->ticks;
          data[1] = chip->ticks >> 8;
          data[2] = chip->ticks >> 16;
        }
#ifdef CONFIG_ARCH_SIM
      else if (reg == 0x26 && g_replay_mode)
        {
          struct replay_imu *imu = &g_replay[address - FIRST_IMU];
          if (size > imu->length - imu->consumed)
            {
              return -EIO;
            }
          memcpy(data, imu->fifo + imu->consumed, size);
          imu->consumed += size;
          imu->samples += size / 12;
          chip->fifo_bytes -= size;
          replay_report(address - FIRST_IMU);
        }
#endif
      else if (reg == 0x1d && size == 1)
        {
          data[0] = chip->fifo_bytes == BMI_FIFO_BYTES ? 1 : 0; /* FIFO full */
        }
      else if (reg == 0x26 && size % BMI_FRAME_BYTES == 0 &&
               size <= chip->fifo_bytes)
        {
          chip->fifo_bytes -= size;
          for (size_t offset = 0; offset < size; offset += BMI_FRAME_BYTES)
            {
              /* The two sensors report opposite acceleration on X. */
              data[offset + 7] = address == 0x68 ? 0x20 : 0xe0;
              data[offset + 11] = 0x20;
            }
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
#ifdef CONFIG_INPUT_FT5X06
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
#endif
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
      if (address >= FIRST_IMU && address < FIRST_IMU + IMU_COUNT)
        {
          ret = bmi_write(&g_imus[address - FIRST_IMU], reg, msgs[0].buffer + 1,
                          msgs[0].length - 1);
        }
#ifdef CONFIG_INPUT_FT5X06
      else if (address == 0x38 && reg == 0)
        {
          ret = 0;
        }
#endif
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

#ifdef CONFIG_INPUT_FT5X06
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
          g_inject_imu_failure = scenario && strcmp(scenario, "imu") == 0;
          nxmutex_unlock(&g_i2c_lock);
          g_stall_panel = scenario && strcmp(scenario, "paint") == 0;
        }
      else if (g_stall_panel && g_lcd_register == 0x2c &&
               access("/tracker-fixture/stall-panel", F_OK) == 0)
        {
          /* Delay one paint after the host sees live telemetry. */
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
#endif

int tracker_sim_devices(void)
{
  for (unsigned int i = 0; i < IMU_COUNT; i++)
    {
      g_imus[i].registers[0] = 0x24;
    }
  int ret = i2c_register(&g_i2c, 0);
#ifdef CONFIG_INPUT_FT5X06
  static const struct ft5x06_config_s touch =
  {
    .lower = { .xres = 320, .yres = 240, .maxpoint = 1 },
    .address = 0x38,
    .frequency = I2C_SPEED_FAST,
  };
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
#endif
  return ret;
}

#ifdef CONFIG_INPUT_FT5X06
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
#endif

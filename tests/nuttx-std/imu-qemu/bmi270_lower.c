/* Test-only BMI270 register/FIFO model behind the NuttX I2C and GPIO drivers. */

#include <nuttx/config.h>
#include <nuttx/i2c/i2c_master.h>
#include <nuttx/ioexpander/gpio.h>
#include <nuttx/irq.h>
#include <errno.h>
#include <string.h>

#ifdef CONFIG_SMP
#  error "The BMI270 fixture uses a single-core critical section"
#endif

#define CHIP_ID         0x00
#define STATUS          0x03
#define INT_STATUS      0x1d
#define INTERNAL_STATUS 0x21
#define FIFO_LENGTH     0x24
#define FIFO_DATA       0x26
#define ACC_CONF        0x40
#define FIFO_WATERMARK  0x46
#define FIFO_CONFIG     0x49
#define INT1_CONFIG     0x53
#define INT_LATCH       0x55
#define INT_MAP         0x58
#define INIT_CTRL       0x59
#define INIT_ADDR       0x5b
#define INIT_DATA       0x5e
#define POWER_CONF      0x7c
#define POWER_CTRL      0x7d
#define COMMAND         0x7e

#define UPLOAD_BYTES 8192
#define FRAME_BYTES  12
#define FRAMES       6

static uint8_t registers[128];
static uint8_t fifo[FRAMES * FRAME_BYTES];
static size_t uploaded;
static size_t fifo_offset;
static size_t fifo_count;
static bool uploading;
static bool overflow;
static bool bus_open;
static bool irq_enabled;
static pin_interrupt_t irq_handler;

static void reset_sensor(void)
{
  memset(registers, 0, sizeof(registers));
  registers[CHIP_ID] = 0x24;
  registers[POWER_CONF] = 1;
  uploaded = 0;
  uploading = false;
  fifo_offset = 0;
  fifo_count = 0;
  overflow = false;
}

static bool int1_high(void)
{
  return (registers[INT1_CONFIG] & 8) != 0 &&
         (((registers[INT_MAP] & 1) != 0 && overflow) ||
          ((registers[INT_MAP] & 2) != 0 && registers[FIFO_WATERMARK] != 0 &&
           fifo_count >= registers[FIFO_WATERMARK]));
}

static bool config_range(uint8_t reg, size_t len)
{
  return (reg >= ACC_CONF && reg <= FIFO_CONFIG &&
          len <= (size_t)(FIFO_CONFIG + 1 - reg)) ||
         (len == 1 && (reg == INT1_CONFIG || reg == INT_LATCH ||
                       reg == INT_MAP || reg == POWER_CONF ||
                       reg == POWER_CTRL));
}

static int write_registers(uint8_t reg, const uint8_t *data, size_t len)
{
  if (reg == COMMAND && len == 1 && data[0] == 0xb6)
    {
      reset_sensor();
    }
  else if (reg == COMMAND && len == 1 && data[0] == 0xb0)
    {
      fifo_count = 0;
      fifo_offset = 0;
      overflow = false;
    }
  else if (reg == INIT_CTRL && len == 1 && data[0] == 0 && !uploading &&
           registers[INTERNAL_STATUS] == 0)
    {
      uploading = true;
    }
  else if (reg == INIT_ADDR && len == 2 && uploading)
    {
      memcpy(registers + reg, data, len);
    }
  else if (reg == INIT_DATA && uploading)
    {
      /* INIT_ADDR addresses 16-bit words. Verify contiguous, even chunks. */
      size_t offset = 2 * ((registers[INIT_ADDR] & 15) |
                           (registers[INIT_ADDR + 1] << 4));
      if (offset != uploaded || (len & 1) || len > UPLOAD_BYTES - uploaded)
        {
          return -EINVAL;
        }

      uploaded += len;
    }
  else if (reg == INIT_CTRL && len == 1 && data[0] == 1 && uploading &&
           uploaded == UPLOAD_BYTES)
    {
      uploading = false;
      registers[INIT_CTRL] = 1;
      registers[INTERNAL_STATUS] = 1;
    }
  else if (config_range(reg, len))
    {
      memcpy(registers + reg, data, len);
    }
  else
    {
      return -EINVAL;
    }

  return 0;
}

static int read_registers(uint8_t reg, uint8_t *data, size_t len)
{
  if (reg == STATUS && len == 1)
    {
      *data = (registers[POWER_CTRL] & 6) == 6 ? 0xc0 : 0;
    }
  else if (reg == INT_STATUS && len == 1)
    {
      *data = overflow ? 1 : int1_high() ? 2 : 0;
    }
  else if (reg == FIFO_LENGTH && len == 2)
    {
      data[0] = fifo_count & 255;
      data[1] = fifo_count >> 8;
    }
  else if (reg == FIFO_DATA && len <= fifo_count && len % FRAME_BYTES == 0)
    {
      memcpy(data, fifo + fifo_offset, len);
      fifo_offset += len;
      fifo_count -= len;
    }
  else if (config_range(reg, len) ||
           (len == 1 && (reg == CHIP_ID || reg == INIT_CTRL ||
                         reg == INTERNAL_STATUS)))
    {
      memcpy(data, registers + reg, len);
    }
  else
    {
      return -EINVAL;
    }

  return 0;
}

static int bus_setup(struct i2c_master_s *dev)
{
  (void)dev;
  irqstate_t flags = enter_critical_section();
  bus_open = true;
  leave_critical_section(flags);
  return 0;
}

static int bus_shutdown(struct i2c_master_s *dev)
{
  (void)dev;
  irqstate_t flags = enter_critical_section();
  bus_open = false;
  leave_critical_section(flags);
  return 0;
}

static int transfer(struct i2c_master_s *dev, struct i2c_msg_s *msgs,
                    int count)
{
  (void)dev;
  if (count != 1 && count != 2)
    {
      return -EINVAL;
    }

  for (int i = 0; i < count; i++)
    {
      if (msgs[i].addr != 0x68 || msgs[i].frequency != I2C_SPEED_FAST ||
          msgs[i].buffer == NULL || msgs[i].length <= 0 ||
          msgs[i].length > 64)
        {
          return -EINVAL;
        }
    }

  irqstate_t flags = enter_critical_section();
  int ret = -EINVAL;
  if (bus_open && count == 1 && msgs[0].flags == 0 && msgs[0].length > 1)
    {
      ret = write_registers(msgs[0].buffer[0], msgs[0].buffer + 1,
                            msgs[0].length - 1);
    }
  else if (bus_open && count == 2 && msgs[0].flags == I2C_M_NOSTOP &&
           msgs[0].length == 1 && msgs[1].flags == I2C_M_READ)
    {
      ret = read_registers(msgs[0].buffer[0], msgs[1].buffer, msgs[1].length);
    }

  leave_critical_section(flags);
  return ret;
}

static const struct i2c_ops_s bus_ops =
{
  .transfer = transfer,
  .setup = bus_setup,
  .shutdown = bus_shutdown,
};

static struct i2c_master_s bus =
{
  .ops = &bus_ops,
};

static int gpio_read(struct gpio_dev_s *dev, bool *high)
{
  (void)dev;
  irqstate_t flags = enter_critical_section();
  *high = int1_high();
  leave_critical_section(flags);
  return 0;
}

static int gpio_attach(struct gpio_dev_s *dev, pin_interrupt_t callback)
{
  (void)dev;
  irqstate_t flags = enter_critical_section();
  irq_handler = callback;
  leave_critical_section(flags);
  return 0;
}

static int gpio_enable(struct gpio_dev_s *dev, bool enable)
{
  (void)dev;
  irqstate_t flags = enter_critical_section();
  irq_enabled = enable;
  leave_critical_section(flags);
  return 0;
}

static const struct gpio_operations_s irq_ops =
{
  .go_read = gpio_read,
  .go_attach = gpio_attach,
  .go_enable = gpio_enable,
};

static struct gpio_dev_s int1 =
{
  .gp_pintype = GPIO_INTERRUPT_RISING_PIN,
  .gp_ops = &irq_ops,
};

int bmi270_fixture_install(void)
{
  reset_sensor();
  int ret = i2c_register(&bus, 9);
  return ret < 0 ? ret : gpio_pin_register_byname(&int1, "gpio-sim-imu");
}

bool bmi270_fixture_irq_waiting(void)
{
  irqstate_t flags = enter_critical_section();
  /* Observe the upper half's waiter without retaining the pollfd. */
  bool waiting = int1.fds[0] != NULL;
  leave_critical_section(flags);
  return waiting;
}

int bmi270_fixture_push(bool full)
{
  irqstate_t flags = enter_critical_section();
  if (!bus_open || !irq_enabled || irq_handler == NULL || int1.fds[0] == NULL ||
      fifo_count != 0 ||
      overflow || registers[INTERNAL_STATUS] != 1 ||
      registers[POWER_CTRL] != 6 || registers[FIFO_CONFIG] != 0xc0 ||
      registers[INT_MAP] != 0x0b || registers[INT1_CONFIG] != 0x0a ||
      registers[FIFO_WATERMARK] != FRAME_BYTES)
    {
      leave_critical_section(flags);
      return -EINVAL;
    }

  fifo_offset = 0;
  overflow = full;
  if (!full)
    {
      for (int i = 0; i < FRAMES; i++)
        {
          const int16_t axes[] =
          {
            1024 + 128 * i, -2048 - 128 * i, 512 + 64 * i,
            -4096 - 256 * i, 2048 + 256 * i, 8192 - 512 * i,
          };
          for (size_t axis = 0; axis < sizeof(axes) / sizeof(axes[0]); axis++)
            {
              uint16_t raw = (uint16_t)axes[axis];
              fifo[i * FRAME_BYTES + 2 * axis] = raw & 255;
              fifo[i * FRAME_BYTES + 2 * axis + 1] = raw >> 8;
            }
        }

      fifo_count = sizeof(fifo);
    }

  /* Same callback the NuttX upper half installs on a physical pin. */
  irq_handler(&int1, 0);
  leave_critical_section(flags);
  return 0;
}

bool bmi270_fixture_idle(void)
{
  irqstate_t flags = enter_critical_section();
  bool idle = !bus_open && !irq_enabled && fifo_count == 0 &&
              int1.fds[0] == NULL && int1.register_count == 0 &&
              registers[POWER_CTRL] == 0 && registers[INT_MAP] == 0 &&
              (registers[INT1_CONFIG] & 8) == 0;
  leave_critical_section(flags);
  return idle;
}

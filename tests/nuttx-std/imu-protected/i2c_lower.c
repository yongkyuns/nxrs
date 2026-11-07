/* Test-only deterministic NuttX I2C lower half. No physical bus access. */
#include <nuttx/config.h>
#include <nuttx/i2c/i2c_master.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>

#define WITNESS_BUS 42

static bool g_open;
static unsigned g_transfers;

static int witness_setup(FAR struct i2c_master_s *dev)
{
  (void)dev;
  if (g_open)
    {
      return -EBUSY;
    }

  g_open = true;
  g_transfers = 0;
  return 0;
}

static int witness_shutdown(FAR struct i2c_master_s *dev)
{
  (void)dev;
  if (!g_open)
    {
      return -EINVAL;
    }

  g_open = false;
  return 0;
}

static int witness_transfer(FAR struct i2c_master_s *dev,
                            FAR struct i2c_msg_s *msgs, int count)
{
  (void)dev;
  if (!g_open || msgs == NULL || count != 2 ||
      msgs[0].frequency != I2C_SPEED_FAST || msgs[1].frequency != I2C_SPEED_FAST ||
      msgs[0].addr != 0x68 || msgs[1].addr != 0x68 ||
      msgs[0].flags != I2C_M_NOSTOP || msgs[1].flags != I2C_M_READ ||
      msgs[0].length != 1 || msgs[1].length != 2 ||
      msgs[0].buffer == NULL || msgs[1].buffer == NULL ||
      msgs[0].buffer[0] != 0x21)
    {
      return -EINVAL;
    }

  /* Use each transfer slot only after validating its complete descriptor set.
   * Never retain the caller's descriptor or buffer pointers after returning.
   */
  g_transfers++;
  switch (g_transfers)
    {
      case 1:
      case 4:
        msgs[1].buffer[0] = 0xa5;
        msgs[1].buffer[1] = 0x5a;
        return 0;

      case 2:
        return -ETIMEDOUT;

      case 3:
        return -EIO;

      default:
        return -EIO;
    }
}

static const struct i2c_ops_s g_ops =
{
  .transfer = witness_transfer,
  .setup = witness_setup,
  .shutdown = witness_shutdown,
};

static struct i2c_master_s g_master =
{
  .ops = &g_ops,
};

int imu_witness_i2c_register(void)
{
  return i2c_register(&g_master, WITNESS_BUS);
}

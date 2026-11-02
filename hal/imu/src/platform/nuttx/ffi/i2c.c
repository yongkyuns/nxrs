/* Target I2C ioctl/message construction only. */
#include <nuttx/config.h>
#include <nuttx/i2c/i2c_master.h>
#include <sys/ioctl.h>
#include <stdint.h>
#include <stddef.h>
#include <errno.h>
int imu_i2c_transfer(int fd, uint8_t address, uint32_t frequency,
                     const uint8_t *write, size_t write_len,
                     uint8_t *read, size_t read_len)
{
  struct i2c_msg_s messages[2];
  struct i2c_transfer_s transfer;
  int count = 0;
  if (write_len > 64 || read_len > 64) { errno = EINVAL; return -1; }
  if (write_len)
    messages[count++] = (struct i2c_msg_s){ .frequency = frequency,
      .addr = address, .flags = read_len ? I2C_M_NOSTOP : 0, .buffer = (uint8_t *)write, .length = write_len };
  if (read_len)
    messages[count++] = (struct i2c_msg_s){ .frequency = frequency,
      .addr = address, .flags = I2C_M_READ, .buffer = read, .length = read_len };
  if (!count) return 0;
  transfer.msgv = messages;
  transfer.msgc = count;
  return ioctl(fd, I2CIOC_TRANSFER, (unsigned long)(uintptr_t)&transfer);
}

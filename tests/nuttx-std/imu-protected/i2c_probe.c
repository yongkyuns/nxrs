/* Protected user-mode witness for the production NuttX I2C bridge. */
#include <nuttx/config.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stddef.h>
#include <unistd.h>

int i2c_resource_transfer(int fd, uint8_t address, uint32_t frequency,
                     const uint8_t *write, size_t write_len,
                     uint8_t *read, size_t read_len);

#define DEVICE_PATH "/dev/i2c42"
#define WITNESS_FREQUENCY 400000

static int transfer(int fd, const uint8_t *reg, uint8_t *data)
{
  return i2c_resource_transfer(fd, 0x68, WITNESS_FREQUENCY, reg, 1, data, 2);
}

int imu_witness_i2c(void)
{
  struct
  {
    uint8_t before;
    uint8_t value;
    uint8_t after;
  } reg = {0x39, 0x21, 0xc6};
  struct
  {
    uint8_t before;
    uint8_t bytes[2];
    uint8_t after;
  } reply = {0x17, {0, 0}, 0xe2};
  uint8_t oversized[65] = {0};
  int fd = -1;
  int result = 0;

  fd = open(DEVICE_PATH, O_RDWR);
  if (fd < 0)
    {
      return 1;
    }

  if (transfer(fd, &reg.value, reply.bytes) < 0 ||
      reply.before != 0x17 || reply.bytes[0] != 0xa5 ||
      reply.bytes[1] != 0x5a || reply.after != 0xe2 ||
      reg.before != 0x39 || reg.value != 0x21 || reg.after != 0xc6)
    {
      result = 2;
      goto close_first;
    }

  /* The bridge must reject oversize buffers before issuing an ioctl. */
  errno = 0;
  if (i2c_resource_transfer(fd, 0x68, WITNESS_FREQUENCY,
                       oversized, sizeof(oversized), reply.bytes, 2) != -1 ||
      errno != EINVAL || reply.before != 0x17 || reply.after != 0xe2)
    {
      result = 3;
      goto close_first;
    }

  /* The lower half injects timeout and I/O errors on transfers 2 and 3. */
  reply.bytes[0] = 0x3c;
  reply.bytes[1] = 0xc3;
  errno = 0;
  if (transfer(fd, &reg.value, reply.bytes) != -1 || errno != ETIMEDOUT ||
      reply.before != 0x17 || reply.bytes[0] != 0x3c ||
      reply.bytes[1] != 0xc3 || reply.after != 0xe2)
    {
      result = 4;
      goto close_first;
    }

  reply.bytes[0] = 0x69;
  reply.bytes[1] = 0x96;
  errno = 0;
  if (transfer(fd, &reg.value, reply.bytes) != -1 || errno != EIO ||
      reply.before != 0x17 || reply.bytes[0] != 0x69 ||
      reply.bytes[1] != 0x96 || reply.after != 0xe2)
    {
      result = 5;
      goto close_first;
    }

  /* Verify the next transaction still works after both injected failures. */
  reply.bytes[0] = 0;
  reply.bytes[1] = 0;
  if (transfer(fd, &reg.value, reply.bytes) < 0 ||
      reply.before != 0x17 || reply.bytes[0] != 0xa5 ||
      reply.bytes[1] != 0x5a || reply.after != 0xe2 ||
      reg.before != 0x39 || reg.value != 0x21 || reg.after != 0xc6)
    {
      result = 6;
    }

close_first:
  if (close(fd) < 0 && result == 0)
    {
      result = 7;
    }
  if (result != 0)
    {
      return result;
    }

  /* Reopen proves close reached the lower-half shutdown path. */
  fd = open(DEVICE_PATH, O_RDWR);
  if (fd < 0)
    {
      return 8;
    }
  if (close(fd) < 0)
    {
      return 9;
    }

  return 0;
}

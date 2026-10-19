/*
 * Native witness for the production OV3660 imgsensor lower-half.
 *
 * Compile against an isolated NuttX source copy containing the file added by
 * patch 0067. Put that copy's drivers/video on the include path as "ov3660.c";
 * use host-compatible NuttX/mock headers, not generated Xtensa build headers.
 * No source-tree or tracked external checkout modification is needed.
 * Example: cc -std=c11 -I<isolated>/drivers/video -I<mock-include>
 *          -I<isolated>/include tests/nuttx-std/ov3660-image-sensor.c
 *          -o /tmp/ov3660-image-sensor
 *
 * The production source is included directly (not copied into this harness).
 * This intentionally has no board/CAM dependencies and does not touch hardware.
 */
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <nuttx/config.h>
#include <nuttx/i2c/i2c_master.h>
#include <nuttx/video/imgsensor.h>
#include <nuttx/video/video.h>

static unsigned int g_delay_calls;

static void test_delay(unsigned int ms)
{
  assert(ms > 0);
  g_delay_calls++;
}

static void *test_zalloc(size_t size)
{
  return calloc(1, size);
}

#define CONFIG_VIDEO_OV3660 1
#define up_mdelay(ms) test_delay(ms)
#define kmm_zalloc(size) test_zalloc(size)

/* Include the actual production file from the isolated patch-source copy. */
#include "ov3660.c"

#define REG_ID_HIGH 0x300a
#define REG_ID_LOW  0x300b
#define REG_SYSCTRL 0x3008
#define REG_WIDTH_H 0x3808
#define REG_HEIGHT_H 0x380a

struct fake_i2c_s
{
  struct i2c_master_s master;
  uint8_t regs[65536];
  uint16_t fail_write_reg;
  bool fail_all;
  unsigned int reads;
  unsigned int writes;
  unsigned int stream_writes;
};

static struct fake_i2c_s *fake_from_master(struct i2c_master_s *master)
{
  return (struct fake_i2c_s *)master;
}

static int fake_transfer(struct i2c_master_s *master,
                         struct i2c_msg_s *msgs, int count)
{
  struct fake_i2c_s *fake = fake_from_master(master);
  uint16_t reg;

  if (fake->fail_all)
    {
      return -EIO;
    }

  if (count == 2 && (msgs[1].flags & I2C_M_READ) != 0 &&
      msgs[0].length == 2 && msgs[1].length == 1)
    {
      reg = (uint16_t)msgs[0].buffer[0] << 8 | msgs[0].buffer[1];
      *msgs[1].buffer = fake->regs[reg];
      fake->reads++;
      return OK;
    }

  if (count == 1 && msgs[0].length == 3 &&
      (msgs[0].flags & I2C_M_READ) == 0)
    {
      reg = (uint16_t)msgs[0].buffer[0] << 8 | msgs[0].buffer[1];
      if (fake->fail_write_reg == reg)
        {
          return -EIO;
        }

      fake->regs[reg] = msgs[0].buffer[2];
      fake->writes++;
      if (reg == REG_SYSCTRL)
        {
          fake->stream_writes++;
        }

      return OK;
    }

  return -EINVAL;
}

static const struct i2c_ops_s g_fake_i2c_ops =
{
  .transfer = fake_transfer,
};

static void fake_init(struct fake_i2c_s *fake, uint16_t id)
{
  memset(fake, 0, sizeof(*fake));
  fake->master.ops = &g_fake_i2c_ops;
  fake->regs[REG_ID_HIGH] = id >> 8;
  fake->regs[REG_ID_LOW] = id & 0xff;
}

static struct imgsensor_s *make_sensor(struct fake_i2c_s *fake)
{
  return ov3660_initialize(&fake->master);
}

static imgsensor_format_t qvga_rgb565x(void)
{
  imgsensor_format_t format =
  {
    .width = 320,
    .height = 240,
    .pixelformat = IMGSENSOR_PIX_FMT_RGB565X,
  };

  return format;
}

static void test_real_and_wrong_identity(void)
{
  struct fake_i2c_s fake;
  struct imgsensor_s *sensor;

  fake_init(&fake, 0x3660);
  sensor = make_sensor(&fake);
  assert(sensor != NULL);
  assert(sensor->ops->is_available(sensor));
  assert(fake.reads == 2);
  free(sensor);

  fake_init(&fake, 0x5640);
  sensor = make_sensor(&fake);
  assert(sensor != NULL);
  assert(!sensor->ops->is_available(sensor));
  assert(sensor->ops->init(sensor) == -ENODEV);
  assert(fake.writes == 0);
  free(sensor);
}

static void test_i2c_failures_are_propagated(void)
{
  struct fake_i2c_s fake;
  struct imgsensor_s *sensor;

  fake_init(&fake, 0x3660);
  fake.fail_all = true;
  sensor = make_sensor(&fake);
  assert(sensor != NULL);
  assert(!sensor->ops->is_available(sensor));
  assert(sensor->ops->init(sensor) == -EIO);
  free(sensor);

  fake_init(&fake, 0x3660);
  fake.fail_write_reg = REG_WIDTH_H;
  sensor = make_sensor(&fake);
  assert(sensor != NULL);
  assert(sensor->ops->init(sensor) == -EIO);
  free(sensor);
}

static void test_qvga_format_negotiation(void)
{
  struct fake_i2c_s fake;
  struct imgsensor_s *sensor;
  imgsensor_format_t format = qvga_rgb565x();
  imgsensor_interval_t interval = { .numerator = 4, .denominator = 41 };
  imgsensor_interval_t actual = {0};

  fake_init(&fake, 0x3660);
  sensor = make_sensor(&fake);
  assert(sensor != NULL);
  assert(sensor->fmtdescs_num == 1);
  assert(sensor->fmtdescs[0].pixelformat == V4L2_PIX_FMT_RGB565X);
  assert(sensor->frmsizes_num == 1);
  assert(sensor->frmsizes[0].discrete.width == 320);
  assert(sensor->frmsizes[0].discrete.height == 240);
  assert(sensor->ops->get_frame_interval(sensor,
          IMGSENSOR_STREAM_TYPE_VIDEO, &actual) == OK);
  assert(actual.numerator == 4 && actual.denominator == 41);
  assert(sensor->ops->validate_frame_setting(sensor,
          IMGSENSOR_STREAM_TYPE_VIDEO, 1, &format, &interval) == OK);
  interval.numerator = 8;
  interval.denominator = 82;
  assert(sensor->ops->validate_frame_setting(sensor,
          IMGSENSOR_STREAM_TYPE_VIDEO, 1, &format, &interval) == OK);
  interval.numerator = 1;
  interval.denominator = 10;
  assert(sensor->ops->validate_frame_setting(sensor,
          IMGSENSOR_STREAM_TYPE_VIDEO, 1, &format, &interval) == -EINVAL);
  interval.numerator = 0;
  interval.denominator = 0;
  assert(sensor->ops->validate_frame_setting(sensor,
          IMGSENSOR_STREAM_TYPE_VIDEO, 1, &format, &interval) == -EINVAL);
  interval.numerator = 4;
  interval.denominator = 41;

  format.width = 640;
  assert(sensor->ops->validate_frame_setting(sensor,
          IMGSENSOR_STREAM_TYPE_VIDEO, 1, &format, &interval) == -EINVAL);
  format = qvga_rgb565x();
  format.pixelformat = IMGSENSOR_PIX_FMT_JPEG;
  assert(sensor->ops->validate_frame_setting(sensor,
          IMGSENSOR_STREAM_TYPE_VIDEO, 1, &format, &interval) == -EINVAL);
  format = qvga_rgb565x();
  assert(sensor->ops->validate_frame_setting(sensor,
          IMGSENSOR_STREAM_TYPE_VIDEO, 2, &format, &interval) == -EINVAL);
  free(sensor);
}

static void test_init_failure_and_streaming_lifecycle(void)
{
  struct fake_i2c_s fake;
  struct imgsensor_s *sensor;
  imgsensor_format_t format = qvga_rgb565x();
  imgsensor_interval_t interval = { .numerator = 4, .denominator = 41 };
  unsigned int writes;
  unsigned int reads;

  fake_init(&fake, 0x3660);
  fake.fail_write_reg = REG_WIDTH_H;
  sensor = make_sensor(&fake);
  assert(sensor != NULL);
  assert(sensor->ops->init(sensor) == -EIO);
  /* Failed configuration must leave the sensor in standby, not streaming. */
  assert(fake.regs[REG_SYSCTRL] == 0x42);
  free(sensor);

  fake_init(&fake, 0x3660);
  sensor = make_sensor(&fake);
  assert(sensor != NULL);
  assert(sensor->ops->init(sensor) == OK);
  assert(fake.regs[0x303d] == 0x02); /* 16 MHz preset: predivider 0, seld5 2. */
  /* Sensor DVP stays enabled; logical capture callbacks must be ISR-safe. */
  assert(fake.regs[REG_SYSCTRL] == 0x02);
  writes = fake.writes;
  reads = fake.reads;
  assert(sensor->ops->start_capture(sensor, IMGSENSOR_STREAM_TYPE_VIDEO,
          1, &format, &interval) == OK);
  assert(fake.writes == writes && fake.reads == reads);
  assert(sensor->ops->start_capture(sensor, IMGSENSOR_STREAM_TYPE_VIDEO,
          1, &format, &interval) == -EBUSY);
  writes = fake.writes;
  reads = fake.reads;
  assert(sensor->ops->stop_capture(sensor, IMGSENSOR_STREAM_TYPE_VIDEO) == OK);
  assert(fake.writes == writes && fake.reads == reads);
  assert(fake.regs[REG_SYSCTRL] == 0x02);
  for (unsigned int frame = 0; frame < 8; frame++)
    {
      assert(sensor->ops->start_capture(sensor, IMGSENSOR_STREAM_TYPE_VIDEO,
              1, &format, &interval) == OK);
      assert(sensor->ops->stop_capture(sensor,
              IMGSENSOR_STREAM_TYPE_VIDEO) == OK);
    }

  assert(fake.writes == writes && fake.reads == reads);
  assert(fake.regs[REG_SYSCTRL] == 0x02);
  assert(sensor->ops->start_capture(sensor, IMGSENSOR_STREAM_TYPE_VIDEO,
          1, &format, &interval) == OK);

  /* Thread-context uninit owns sensor standby. On failure, preserve the
   * initialized/capture cache and allow the caller to retry.
   */
  fake.fail_write_reg = REG_SYSCTRL;
  assert(sensor->ops->uninit(sensor) == -EIO);
  assert(((struct ov3660_dev_s *)sensor)->initialized);
  assert(((struct ov3660_dev_s *)sensor)->capture_active);
  fake.fail_write_reg = 0;
  assert(sensor->ops->uninit(sensor) == OK);
  assert(!((struct ov3660_dev_s *)sensor)->initialized);
  assert(!((struct ov3660_dev_s *)sensor)->capture_active);
  assert(fake.regs[REG_SYSCTRL] == 0x42);
  free(sensor);
}

int main(void)
{
  test_real_and_wrong_identity();
  test_i2c_failures_are_propagated();
  test_qvga_format_negotiation();
  test_init_failure_and_streaming_lifecycle();
  printf("OV3660 lower-half native harness passed; delay_calls=%u\n",
         g_delay_calls);
  return 0;
}

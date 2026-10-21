/* Native witnesses for the actual scalar-to-extended V4L2 control wrappers. */
#include <assert.h>
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#define FAR
#define OK 0
#define V4L2_CTRL_CLASS_USER 1

struct file { int unused; };
struct v4l2_control { uint16_t id; int32_t value; };
struct v4l2_ext_control {
  uint16_t id, size;
  union { int32_t value; int64_t value64; };
};
struct v4l2_ext_controls {
  uint16_t ctrl_class, count, error_idx;
  struct v4l2_ext_control *controls;
};
static unsigned int calls;
static int return_code;

static void check(const struct v4l2_ext_controls *controls)
{
  assert(controls->ctrl_class == V4L2_CTRL_CLASS_USER && controls->count == 1);
  assert(controls->error_idx == 0 && controls->controls->id == 100);
  assert(controls->controls->size == sizeof(int32_t));
  const unsigned char *bytes = (const void *)&controls->controls->value64;
  for (unsigned int i = sizeof(int32_t); i < sizeof(int64_t); i++) assert(bytes[i] == 0);
  calls++;
}

static int capture_g_ext_ctrls(struct file *file, struct v4l2_ext_controls *controls)
{
  (void)file;
  check(controls);
  controls->controls->value = 90;
  return return_code;
}

static int capture_s_ext_ctrls(struct file *file, struct v4l2_ext_controls *controls)
{
  (void)file;
  check(controls);
  assert(controls->controls->value == 81);
  return return_code;
}

/* FUNCTIONS */

int main(void)
{
  struct file file = {0};
  struct v4l2_control control = { .id = 100, .value = 81 };
  assert(capture_s_ctrl(&file, &control) == 0 && calls == 1);
  assert(capture_g_ctrl(&file, &control) == 0 && control.value == 90 && calls == 2);
  return_code = -EIO;
  control.value = 81;
  assert(capture_s_ctrl(&file, &control) == -EIO);
  assert(capture_g_ctrl(&file, &control) == -EIO && control.value == 81);
  assert(capture_s_ctrl(&file, NULL) == -EINVAL);
  assert(capture_g_ctrl(&file, NULL) == -EINVAL);
  puts("V4L2 scalar controls: initialized delegation, value and error propagation pass");
  return 0;
}

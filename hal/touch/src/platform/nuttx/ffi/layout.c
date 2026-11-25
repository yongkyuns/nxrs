/* Translate the NuttX touchscreen record layout. Rust owns descriptor I/O. */
#include <fcntl.h>
#include <stddef.h>
#include <stdint.h>
#include <nuttx/input/touchscreen.h>

struct touch_layout
{
  int nonblock;
  uint8_t size;
  uint8_t point;
  uint8_t id;
  uint8_t flags;
  uint8_t x;
  uint8_t y;
  uint8_t down;
  uint8_t up;
  uint8_t move;
  uint8_t position;
};

_Static_assert(sizeof(struct touch_sample_s) <= 64, "touch record too large");
_Static_assert(offsetof(struct touch_sample_s, npoints) == 0 &&
               sizeof(((struct touch_sample_s *)0)->npoints) == 4,
               "unexpected touch count layout");

struct touch_layout touch_record_layout(void)
{
  return (struct touch_layout)
    {
      O_NONBLOCK, sizeof(struct touch_sample_s),
      offsetof(struct touch_sample_s, point),
      offsetof(struct touch_point_s, id), offsetof(struct touch_point_s, flags),
      offsetof(struct touch_point_s, x), offsetof(struct touch_point_s, y),
      TOUCH_DOWN, TOUCH_UP, TOUCH_MOVE, TOUCH_POS_VALID,
    };
}

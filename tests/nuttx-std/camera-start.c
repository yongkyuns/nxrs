#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>

#define FAR
#define OK 0
#define ASSERT(condition) do { if (!(condition)) abort(); } while (0)
#define MAX_CAPTURE_FMT 2
#define CAPTURE_FMT_MAIN 0
#define CAPTURE_FMT_SUB 1
#define IMGDATA_FMT_MAIN 0
#define IMGDATA_FMT_SUB 1
#define IMGSENSOR_FMT_MAIN 0
#define IMGSENSOR_FMT_SUB 1

enum v4l2_buf_type
{
  V4L2_BUF_TYPE_VIDEO_CAPTURE = 1,
  V4L2_BUF_TYPE_STILL_CAPTURE = 2
};

enum imgsensor_stream_type_e
{
  IMGSENSOR_STREAM_TYPE_VIDEO,
  IMGSENSOR_STREAM_TYPE_STILL
};

enum v4l2_buf_mode
{
  V4L2_BUF_MODE_RING,
  V4L2_BUF_MODE_FIFO
};

#define V4L2_MEMORY_MMAP 1
#define V4L2_MEMORY_USERPTR 2

struct v4l2_buffer
{
  enum v4l2_buf_type type;
  unsigned memory;
  unsigned index;
  uint32_t length;
  union
  {
    unsigned long userptr;
  } m;
};

typedef int irqstate_t;

static int nxmutex_lock(int *lock)
{
  ASSERT(*lock == 0);
  *lock = 1;
  return OK;
}

static int nxmutex_unlock(int *lock)
{
  ASSERT(*lock == 1);
  *lock = 0;
  return OK;
}

static irqstate_t spin_lock_irqsave(int *lock)
{
  nxmutex_lock(lock);
  return 0;
}

static void spin_unlock_irqrestore(int *lock, irqstate_t flags)
{
  (void)flags;
  nxmutex_unlock(lock);
}

typedef struct
{
  uint16_t width;
  uint16_t height;
  uint32_t pixelformat;
} video_format_t;

struct v4l2_rect
{
  int32_t left;
  int32_t top;
  uint32_t width;
  uint32_t height;
};

struct v4l2_fract
{
  uint32_t numerator;
  uint32_t denominator;
};

typedef struct
{
  uint16_t width;
  uint16_t height;
  uint32_t pixelformat;
} imgdata_format_t;

typedef struct
{
  uint32_t numerator;
  uint32_t denominator;
} imgdata_interval_t;

typedef struct
{
  uint16_t width;
  uint16_t height;
  uint32_t pixelformat;
} imgsensor_format_t;

typedef struct
{
  uint32_t numerator;
  uint32_t denominator;
} imgsensor_interval_t;

typedef int (*imgdata_capture_t)(uint8_t, uint32_t,
                                 const struct timeval *, void *);

struct imgdata_s;
struct imgsensor_s;

struct imgdata_ops_s
{
  int (*set_buf)(struct imgdata_s *, uint8_t, imgdata_format_t *, uint8_t *,
                 uint32_t);
  int (*start_capture)(struct imgdata_s *, uint8_t, imgdata_format_t *,
                       imgdata_interval_t *, imgdata_capture_t, void *);
  int (*stop_capture)(struct imgdata_s *);
};

struct imgdata_s
{
  const struct imgdata_ops_s *ops;
};

struct imgsensor_ops_s
{
  int (*start_capture)(struct imgsensor_s *, enum imgsensor_stream_type_e,
                       uint8_t,
                       imgsensor_format_t *, imgsensor_interval_t *);
  int (*stop_capture)(struct imgsensor_s *, enum imgsensor_stream_type_e);
};

struct imgsensor_s
{
  const struct imgsensor_ops_s *ops;
};

#define IMGDATA_SET_BUF(data, count, formats, address, size) \
  ((data)->ops->set_buf((data), (count), (formats), (address), (size)))
#define IMGDATA_START_CAPTURE(data, count, formats, interval, callback, arg) \
  ((data)->ops->start_capture((data), (count), (formats), (interval), \
                              (callback), (arg)))
#define IMGDATA_STOP_CAPTURE(data) ((data)->ops->stop_capture((data)))
#define IMGSENSOR_START_CAPTURE(sensor, type, count, formats, interval) \
  ((sensor)->ops->start_capture((sensor), (type), (count), (formats), \
                                (interval)))
#define IMGSENSOR_STOP_CAPTURE(sensor, type) \
  ((sensor)->ops->stop_capture((sensor), (type)))

enum capture_state_e
{
  CAPTURE_STATE_STREAMOFF,
  CAPTURE_STATE_STREAMON,
  CAPTURE_STATE_CAPTURE
};

struct vbuf_container_s
{
  struct v4l2_buffer buf;
  struct vbuf_container_s *next;
};

typedef struct vbuf_container_s vbuf_container_t;

typedef struct
{
  enum v4l2_buf_mode mode;
  int lock_empty;
  int lock_queue;
  vbuf_container_t *vbuf_empty;
  vbuf_container_t *vbuf_top;
  vbuf_container_t *vbuf_tail;
  vbuf_container_t *vbuf_next;
} video_framebuff_t;

typedef struct
{
  enum capture_state_e state;
  bool lower_active;
  uint32_t seqnum;
  uint8_t nr_fmt;
  video_format_t *fmt;
  struct v4l2_rect clip;
  struct v4l2_fract frame_interval;
  video_framebuff_t bufinf;
  int lock_state;
  uint8_t *bufheap;
} capture_info_t;

typedef capture_info_t capture_type_inf_t;

typedef struct capture_mng_s
{
  struct imgsensor_s *imgsensor;
  struct imgdata_s *imgdata;
  capture_info_t capture_inf;
  capture_info_t still_inf;
} capture_mng_t;

struct inode
{
  capture_mng_t *i_private;
};

struct file
{
  struct inode *f_inode;
};

enum capture_state_cause_e
{
  CAUSE_CAPTURE_START
};

static char events[32];
static size_t event_count;
static int set_buf_result;
static int sensor_start_result;
static int data_start_result;

static void event(char value)
{
  events[event_count++] = value;
  events[event_count] = '\0';
}

static int mock_set_buf(struct imgdata_s *data, uint8_t count,
                        imgdata_format_t *formats, uint8_t *address,
                        uint32_t size)
{
  (void)data;
  (void)count;
  (void)formats;
  (void)address;
  (void)size;
  event('B');
  return set_buf_result;
}

static int mock_data_start(struct imgdata_s *data, uint8_t count,
                           imgdata_format_t *formats,
                           imgdata_interval_t *interval,
                           imgdata_capture_t callback, void *arg)
{
  (void)data;
  (void)count;
  (void)formats;
  (void)interval;
  (void)callback;
  (void)arg;
  event('D');
  return data_start_result;
}

static int mock_data_stop(struct imgdata_s *data)
{
  (void)data;
  event('d');
  return OK;
}

static int mock_sensor_start(struct imgsensor_s *sensor,
                             enum imgsensor_stream_type_e type,
                             uint8_t count, imgsensor_format_t *formats,
                             imgsensor_interval_t *interval)
{
  (void)sensor;
  (void)type;
  (void)count;
  (void)formats;
  (void)interval;
  event('S');
  return sensor_start_result;
}

static int mock_sensor_stop(struct imgsensor_s *sensor,
                            enum imgsensor_stream_type_e type)
{
  (void)sensor;
  (void)type;
  event('s');
  return OK;
}

static const struct imgdata_ops_s data_ops =
{
  .set_buf = mock_set_buf,
  .start_capture = mock_data_start,
  .stop_capture = mock_data_stop
};

static const struct imgsensor_ops_s sensor_ops =
{
  .start_capture = mock_sensor_start,
  .stop_capture = mock_sensor_stop
};

static struct imgdata_s data = { .ops = &data_ops };
static struct imgsensor_s sensor = { .ops = &sensor_ops };

static void get_clipped_format(uint8_t count, video_format_t *format,
                               struct v4l2_rect *clip,
                               video_format_t *clipped)
{
  (void)clip;
  memcpy(clipped, format, count * sizeof(*format));
}

static void convert_to_imgdatafmt(video_format_t *source,
                                  imgdata_format_t *destination)
{
  destination->width = source->width;
  destination->height = source->height;
  destination->pixelformat = source->pixelformat;
}

static void convert_to_imgdatainterval(struct v4l2_fract *source,
                                       imgdata_interval_t *destination)
{
  destination->numerator = source->numerator;
  destination->denominator = source->denominator;
}

static void convert_to_imgsensorfmt(video_format_t *source,
                                    imgsensor_format_t *destination)
{
  destination->width = source->width;
  destination->height = source->height;
  destination->pixelformat = source->pixelformat;
}

static void convert_to_imgsensorinterval(struct v4l2_fract *source,
                                         imgsensor_interval_t *destination)
{
  destination->numerator = source->numerator;
  destination->denominator = source->denominator;
}

static int complete_capture(uint8_t error, uint32_t size,
                            const struct timeval *timestamp, void *arg)
{
  (void)error;
  (void)size;
  (void)timestamp;
  (void)arg;
  return OK;
}

static capture_type_inf_t *get_capture_type_inf(capture_mng_t *manager,
                                                enum v4l2_buf_type type)
{
  return type == V4L2_BUF_TYPE_VIDEO_CAPTURE ? &manager->capture_inf :
         type == V4L2_BUF_TYPE_STILL_CAPTURE ? &manager->still_inf : NULL;
}

static bool is_bufsize_sufficient(capture_mng_t *manager, uint32_t length)
{
  (void)manager;
  return length >= 384;
}

static uint32_t get_bufsize(video_format_t *format)
{
  return format->width * format->height * 2;
}

static enum capture_state_e
estimate_next_capture_state(capture_mng_t *manager,
                             enum capture_state_cause_e cause)
{
  (void)manager;
  (void)cause;
  return CAPTURE_STATE_CAPTURE;
}

/* FRAMEBUFF_FUNCTIONS */

/* FUNCTIONS */

static void reset(int set_result, int sensor_result, int data_result)
{
  memset(events, 0, sizeof(events));
  event_count = 0;
  set_buf_result = set_result;
  sensor_start_result = sensor_result;
  data_start_result = data_result;
}

static void check(bool condition, const char *label)
{
  if (!condition)
    {
      fprintf(stderr, "FAIL: %s (events=%s)\n", label, events);
      exit(1);
    }
}

static int run_start_capture(void)
{
  video_format_t format[MAX_CAPTURE_FMT] = { { 16, 12, 7 }, { 0 } };
  struct v4l2_rect clip = { 0 };
  struct v4l2_fract interval = { 1, 30 };
  capture_mng_t manager = { .imgsensor = &sensor, .imgdata = &data };

  return start_capture(&manager, V4L2_BUF_TYPE_VIDEO_CAPTURE, 1, format,
                       &clip, &interval, 0x1000, 384);
}

static void test_remove_container(enum v4l2_buf_mode mode)
{
  video_framebuff_t queue = { .mode = mode };
  vbuf_container_t a = {0}, b = {0}, c = {0}, absent = {0};

  check(video_framebuff_remove_container(&queue, &absent) == NULL,
        "remove from empty queue");
  video_framebuff_queue_container(&queue, &a);
  video_framebuff_queue_container(&queue, &b);
  video_framebuff_queue_container(&queue, &c);
  check(video_framebuff_remove_container(&queue, &absent) == NULL,
        "absent buffer does not change FIFO or ring");
  check(video_framebuff_remove_container(&queue, &b) == &b &&
        queue.vbuf_top == &a && queue.vbuf_tail == &c &&
        queue.vbuf_next == &a && a.next == &c && b.next == NULL,
        "remove middle preserves pending capture and list order");
  check(video_framebuff_remove_container(&queue, &a) == &a &&
        queue.vbuf_top == &c && queue.vbuf_tail == &c &&
        queue.vbuf_next == &c && a.next == NULL,
        "remove head advances pending capture");
  check(c.next == (mode == V4L2_BUF_MODE_RING ? &c : NULL),
        "repair ring or FIFO tail");
  check(video_framebuff_remove_container(&queue, &c) == &c &&
        queue.vbuf_top == NULL && queue.vbuf_tail == NULL &&
        queue.vbuf_next == NULL && c.next == NULL,
        "remove sole buffer empties queue");

  video_framebuff_queue_container(&queue, &a);
  video_framebuff_queue_container(&queue, &b);
  queue.vbuf_next = &b;
  check(video_framebuff_remove_container(&queue, &b) == &b &&
        queue.vbuf_top == &a && queue.vbuf_tail == &a &&
        queue.vbuf_next == NULL &&
        a.next == (mode == V4L2_BUF_MODE_RING ? &a : NULL),
        "remove pending tail does not revive already completed buffers");
}

static void test_qbuf(enum v4l2_buf_mode mode, enum v4l2_buf_type type,
                      int existing, int set_result, int sensor_result,
                      int data_result)
{
  video_format_t format[MAX_CAPTURE_FMT] = { {16, 12, 7}, {0} };
  capture_mng_t manager = { .imgsensor = &sensor, .imgdata = &data };
  struct inode inode = { .i_private = &manager };
  struct file file = { .f_inode = &inode };
  struct v4l2_buffer buffer = { .type = type, .memory = V4L2_MEMORY_USERPTR,
                               .length = 384, .m = { .userptr = 0x1000 } };
  vbuf_container_t pool[2] = {0};
  vbuf_container_t older = { .buf = { .length = 384,
                                    .m = { .userptr = 0x2000 } } };
  capture_type_inf_t *info = get_capture_type_inf(&manager, type);
  video_framebuff_t *queue = &info->bufinf;
  int expected = set_result ? set_result :
                 sensor_result ? sensor_result : data_result;

  info->state = CAPTURE_STATE_STREAMON;
  info->nr_fmt = 1;
  info->fmt = format;
  info->frame_interval = (struct v4l2_fract){1, 30};
  queue->mode = mode;
  queue->vbuf_empty = &pool[0];
  pool[0].next = &pool[1];
  if (existing)
    {
      video_framebuff_queue_container(queue, &older);
      if (existing == 1)
        {
          queue->vbuf_next = NULL; /* Completed, but not yet dequeued. */
        }
    }

  reset(set_result, sensor_result, data_result);
  check(capture_qbuf(&file, &buffer) == expected, "QBUF returns start result");
  check(info->lock_state == 0 && manager.still_inf.lock_state == 0,
        "QBUF releases state locks on every path");
  if (expected < 0)
    {
      check(info->state == CAPTURE_STATE_STREAMON &&
            queue->vbuf_empty == &pool[0] && pool[0].next == &pool[1],
            "failed QBUF returns its container and preserves state");
      check(queue->vbuf_top == (existing ? &older : NULL) &&
            queue->vbuf_tail == (existing ? &older : NULL) &&
            queue->vbuf_next == (existing == 2 ? &older : NULL),
            "failed QBUF retains only earlier queued/completed buffers");
      check(!existing || (older.buf.m.userptr == 0x2000 &&
            older.next == (mode == V4L2_BUF_MODE_RING ? &older : NULL)),
            "rollback preserves older payload ownership and queue mode");
      reset(0, 0, 0);
      check(capture_qbuf(&file, &buffer) == OK,
            "caller can safely retry failed QBUF");
    }

  check(info->state == CAPTURE_STATE_CAPTURE &&
        queue->vbuf_tail == &pool[0] && queue->vbuf_empty == &pool[1] &&
        queue->vbuf_top == (existing ? &older : &pool[0]) &&
        queue->vbuf_next == (existing == 2 ? &older : &pool[0]) &&
        pool[0].buf.m.userptr == buffer.m.userptr,
        "successful QBUF transfers exactly one buffer, without duplicate retry");
}

int main(void)
{
  int ret;
  capture_mng_t manager;
  video_format_t format[MAX_CAPTURE_FMT] = { { 16, 12, 7 }, { 0 } };
  struct v4l2_rect clip = { 0 };
  struct v4l2_fract interval = { 1, 30 };
  struct vbuf_container_s container = { .buf = { .m = { .userptr = 0x1000 },
                                                  .length = 384 } };

  reset(0, 0, 0);
  ret = run_start_capture();
  check(ret == OK && strcmp(events, "BSD") == 0,
        "configure sensor before starting DMA");

  reset(-EFBIG, 0, 0);
  ret = run_start_capture();
  check(ret == -EFBIG && strcmp(events, "B") == 0,
        "propagate buffer setup failure without starting devices");

  reset(0, -EIO, 0);
  ret = run_start_capture();
  check(ret == -EIO && strcmp(events,
#ifdef STOP_ACKNOWLEDGED
                             "BSds"
#else
                             "BSs"
#endif
                             ) == 0, "unwind sensor start failure");

  reset(0, 0, -EAGAIN);
  ret = run_start_capture();
  check(ret == -EAGAIN && strcmp(events,
#ifdef STOP_ACKNOWLEDGED
                                "BSDds"
#else
                                "BSDsd"
#endif
                                ) == 0, "stop both lower halves after data start failure");

  memset(&manager, 0, sizeof(manager));
  manager.imgsensor = &sensor;
  manager.imgdata = &data;
  manager.capture_inf.state = CAPTURE_STATE_STREAMON;
  manager.capture_inf.nr_fmt = 1;
  manager.capture_inf.fmt = format;
  manager.capture_inf.clip = clip;
  manager.capture_inf.frame_interval = interval;
  manager.capture_inf.bufinf.vbuf_next = &container;

  reset(-EFBIG, 0, 0);
  ret = change_capture_state(&manager, CAPTURE_STATE_CAPTURE);
  check(ret == -EFBIG && manager.capture_inf.state == CAPTURE_STATE_STREAMON,
        "do not commit capture state when start fails");

  reset(0, 0, 0);
  ret = change_capture_state(&manager, CAPTURE_STATE_CAPTURE);
  check(ret == OK && manager.capture_inf.state == CAPTURE_STATE_CAPTURE,
        "commit capture state after successful start");

#ifdef STOP_ACKNOWLEDGED
  check(stop_capture(&manager, V4L2_BUF_TYPE_VIDEO_CAPTURE) == OK,
        "acknowledge stop before resetting the simulated stream state");
#endif
  manager.capture_inf.state = CAPTURE_STATE_STREAMOFF;
  manager.capture_inf.bufinf.vbuf_next = NULL;
  ret = change_capture_state(&manager, CAPTURE_STATE_CAPTURE);
  check(ret == OK && manager.capture_inf.state == CAPTURE_STATE_STREAMON,
        "leave stream on when no queued buffer can start capture");

  for (int mode = V4L2_BUF_MODE_RING; mode <= V4L2_BUF_MODE_FIFO; mode++)
    {
      test_remove_container(mode);
      for (int type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
           type <= V4L2_BUF_TYPE_STILL_CAPTURE; type++)
        {
          for (int existing = 0; existing < 3; existing++)
            {
              test_qbuf(mode, type, existing, 0, 0, 0);
              test_qbuf(mode, type, existing, -EFBIG, 0, 0);
              test_qbuf(mode, type, existing, 0, -EIO, 0);
              test_qbuf(mode, type, existing, 0, 0, -EAGAIN);
            }
        }
    }

#ifdef STOP_ACKNOWLEDGED
  puts("camera start errors: order=BSD, unwind=BSds/BSDds, state=preserved, qbuf=transactional");
#else
  puts("camera start errors: order=BSD, unwind=BSs/BSDsd, state=preserved, qbuf=transactional");
#endif
  return 0;
}

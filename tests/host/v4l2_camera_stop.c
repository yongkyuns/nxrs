/* Actual V4L2 functions against controlled lower-half failures. */
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#define FAR
#define OK 0
#define ASSERT assert
#define MAX_CAPTURE_FMT 2
#define CAPTURE_FMT_MAIN 0
#define CAPTURE_FMT_SUB 1
#define IMGDATA_FMT_MAIN 0
#define IMGDATA_FMT_SUB 1
#define IMGSENSOR_FMT_MAIN 0
#define IMGSENSOR_FMT_SUB 1
#define IMGSENSOR_STREAM_TYPE_VIDEO 0
#define IMGSENSOR_STREAM_TYPE_STILL 1
#define REMAINING_CAPNUM_INFINITY (-1)
#define V4L2_BUF_FLAG_ERROR 1
#define V4L2_MEMORY_MMAP 1
#define V4L2_REQBUFS_COUNT_MAX 8
#define POLLIN 1
#define WAITEND_CAUSE_CAPTUREDONE 0
#define WAITEND_CAUSE_STILLSTOP 1
#define CAUSE_CAPTURE_STOP 0
#define CAUSE_STILL_STOP 1
#define CAUSE_CAPTURE_START 2
enum v4l2_buf_type { V4L2_BUF_TYPE_VIDEO_CAPTURE, V4L2_BUF_TYPE_STILL_CAPTURE };
enum capture_state_e { CAPTURE_STATE_STREAMOFF, CAPTURE_STATE_STREAMON, CAPTURE_STATE_CAPTURE };
typedef unsigned irqstate_t;
struct v4l2_rect { int unused; };
struct v4l2_fract { int unused; };
typedef struct { int unused; } video_format_t;
typedef video_format_t imgdata_format_t;
typedef video_format_t imgsensor_format_t;
typedef struct v4l2_fract imgdata_interval_t;
typedef struct v4l2_fract imgsensor_interval_t;
struct v4l2_buffer { unsigned index, flags, bytesused, sequence, length, memory; enum v4l2_buf_type type; struct timeval timestamp; union { uintptr_t userptr; unsigned offset; } m; };
typedef struct { struct v4l2_buffer buf; } vbuf_container_t;
typedef struct { vbuf_container_t *vbuf_next; unsigned container_size; } video_framebuff_t;
typedef struct capture_type_inf_s {
  int lock_state;
  enum capture_state_e state;
  bool lower_active;
  int remaining_capnum;
  struct { int dqbuf_wait_flg; vbuf_container_t *done_container; int waitend_cause; } wait_capture;
  uint8_t nr_fmt;
  video_format_t fmt[2];
  struct v4l2_rect clip;
  struct v4l2_fract frame_interval;
  video_framebuff_t bufinf;
  uint8_t *bufheap;
  void *fds;
  unsigned seqnum;
} capture_type_inf_t;
struct imgdata_s { struct { void (*free)(struct imgdata_s *, void *); void *(*alloc)(struct imgdata_s *,unsigned,unsigned); } *ops; };
struct imgsensor_s { int unused; };
typedef struct capture_mng_s {
  unsigned open_num;
  bool shutdown_failed;
  capture_type_inf_t capture_inf, still_inf;
  struct imgdata_s *imgdata;
  struct imgsensor_s *imgsensor;
  int mutex, lock;
  bool unlinked;
} capture_mng_t;
struct inode { void *i_private; };
struct file { struct inode *f_inode; };
struct v4l2_requestbuffers { unsigned count, memory, mode; enum v4l2_buf_type type; };
static int stop_result, sensor_stop_result, start_result, sensor_start_result, uninit_result;
static unsigned stops, sensor_stops, starts, uninits, cleanups, queue_removes, queue_frees, reallocs, context_frees;
static bool vacant;
static vbuf_container_t container;
static int lower_stop(void) { stops++; return stop_result; }
static int sensor_stop(void) { sensor_stops++; return sensor_stop_result; }
static int lower_start(void) { starts++; return start_result; }
static int lower_uninit(void) { uninits++; return uninit_result; }
#define IMGDATA_STOP_CAPTURE(...) lower_stop()
#define IMGSENSOR_STOP_CAPTURE(...) sensor_stop()
static int stub_ok(void) { return 0; }
#define IMGDATA_SET_BUF(...) stub_ok()
#define IMGSENSOR_START_CAPTURE(...) sensor_start_result
#define IMGDATA_START_CAPTURE(...) lower_start()
#define IMGDATA_UNINIT(...) lower_uninit()
#define IMGSENSOR_UNINIT(...) stub_ok()
#define IMGDATA_INIT(...) 0
#define IMGSENSOR_INIT(...) 0
static capture_type_inf_t *get_capture_type_inf(capture_mng_t *c, enum v4l2_buf_type t)
{ return t == V4L2_BUF_TYPE_VIDEO_CAPTURE ? &c->capture_inf : &c->still_inf; }
static int estimate_next_capture_state(capture_mng_t *c, int cause) { (void)c; return cause == CAUSE_CAPTURE_START ? CAPTURE_STATE_CAPTURE : CAPTURE_STATE_STREAMOFF; }
static void nxmutex_lock(int *p) { (void)p; }
static void nxmutex_unlock(int *p) { (void)p; }
static void nxmutex_destroy(int *p) { (void)p; }
static irqstate_t spin_lock_irqsave_nopreempt(int *p) { (void)p; return 0; }
static void spin_unlock_irqrestore_nopreempt(int *p, unsigned f) { (void)p; (void)f; }
static void nxsem_post(int *p) { (void)p; }
static bool is_sem_waited(int *p) { (void)p; return false; }
static void poll_notify(void *p, int count, int flags) { (void)p; (void)count; (void)flags; }
static vbuf_container_t *video_framebuff_get_vacant_container(video_framebuff_t *b) { (void)b; return vacant ? &container : NULL; }
static vbuf_container_t *video_framebuff_pop_curr_container(video_framebuff_t *b) { (void)b; return &container; }
static void video_framebuff_capture_done(video_framebuff_t *b) { (void)b; }
static vbuf_container_t *video_framebuff_get_container(video_framebuff_t *b) { (void)b; return &container; }
static void video_framebuff_queue_container(video_framebuff_t *b, vbuf_container_t *v) { (void)b; (void)v; }
static vbuf_container_t *video_framebuff_remove_container(video_framebuff_t *b, vbuf_container_t *v) { (void)b; queue_removes++; return v; }
static void video_framebuff_free_container(video_framebuff_t *b, vbuf_container_t *v) { (void)b; (void)v; queue_frees++; }
static void video_framebuff_change_mode(video_framebuff_t *b, unsigned m) { (void)b; (void)m; }
static int video_framebuff_realloc_container(video_framebuff_t *b, unsigned n) { (void)b; (void)n; reallocs++; return 0; }
static unsigned get_bufsize(video_format_t *f) { (void)f; return 64; }
static bool is_bufsize_sufficient(capture_mng_t *c, unsigned n) { (void)c; return n >= 64; }
static void get_clipped_format(int n, video_format_t *f, struct v4l2_rect *r, video_format_t *out) { (void)n; (void)f; (void)r; (void)out; }
static void convert_to_imgdatafmt(video_format_t *f, imgdata_format_t *d) { (void)f; (void)d; }
static void convert_to_imgsensorfmt(video_format_t *f, imgsensor_format_t *d) { (void)f; (void)d; }
static void convert_to_imgdatainterval(struct v4l2_fract *f, imgdata_interval_t *d) { (void)f; (void)d; }
static void convert_to_imgsensorinterval(struct v4l2_fract *f, imgsensor_interval_t *d) { (void)f; (void)d; }
static void cleanup_streamresources(capture_type_inf_t *t, capture_mng_t *c) { (void)t; (void)c; cleanups++; }
static void cleanup_scenes_parameter(capture_mng_t *c) { (void)c; cleanups++; }
static void initialize_resources(capture_mng_t *c) { (void)c; }
static void kumm_free(void *p) { (void)p; queue_frees++; }
static void *kumm_memalign(unsigned a, unsigned n) { (void)a; (void)n; return &container; }
static void kmm_free(void *p) { (void)p; context_frees++; }
static int complete_capture(uint8_t e, uint32_t n, const struct timeval *t, void *a);
#include "v4l2_stop.inc"
int main(int argc, char **argv)
{
  assert(argc == 2);
  struct imgdata_s data = {0}; struct imgsensor_s sensor = {0};
  capture_mng_t c = {.open_num=1, .imgdata=&data, .imgsensor=&sensor};
  c.capture_inf.bufinf.vbuf_next = &container;
  c.capture_inf.remaining_capnum = -1;
  struct inode inode = {.i_private=&c}; struct file f = {.f_inode=&inode};
  enum v4l2_buf_type type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
  switch (atoi(argv[1])) {
    case 0: /* Both automatic stop branches retain state until explicit retry. */
      for (int remaining = 0; remaining >= -1; remaining--) {
        c.capture_inf.state = CAPTURE_STATE_CAPTURE;
        c.capture_inf.lower_active = true;
        c.capture_inf.remaining_capnum = remaining;
        stop_result = -EAGAIN;
        assert(complete_capture(0, 64, NULL, &c) == -EAGAIN);
        assert(c.capture_inf.state == CAPTURE_STATE_CAPTURE && c.capture_inf.lower_active);
        assert(capture_streamoff(&f, &type) == -EAGAIN);
        stop_result = 0;
        assert(capture_streamoff(&f, &type) == 0);
        assert(c.capture_inf.state == CAPTURE_STATE_STREAMOFF && !c.capture_inf.lower_active);
      }
      break;
    case 1: /* Partial START rollback failure cannot be hidden by STREAMOFF state. */
      vacant = true; start_result = -EIO; stop_result = -EAGAIN;
      assert(change_capture_state(&c, CAPTURE_STATE_CAPTURE) == -EIO);
      assert(c.capture_inf.state == CAPTURE_STATE_STREAMOFF && c.capture_inf.lower_active);
      assert(capture_streamoff(&f, &type) == -EAGAIN);
      assert(change_capture_state(&c, CAPTURE_STATE_CAPTURE) == -EAGAIN && starts == 1);
      stop_result = 0;
      assert(capture_streamoff(&f, &type) == 0 && !c.capture_inf.lower_active);
      break;
    case 2: /* Consuming close pins queue/callback owners; reopen/unlink cannot free. */
      c.capture_inf.lower_active = true; stop_result = -EAGAIN;
      assert(capture_close(&f) == -EAGAIN);
      assert(cleanups == 0 && uninits == 0 && c.shutdown_failed);
      assert(capture_open(&f) == -EBUSY);
      assert(capture_unlink(&inode) == 0 && context_frees == 0 && inode.i_private == &c);
      break;
    case 3: /* Even an uninit refusal after successful stop preserves upper storage. */
      c.capture_inf.lower_active = true; uninit_result = -EAGAIN;
      assert(capture_close(&f) == -EAGAIN);
      assert(stops == 1 && uninits == 1 && cleanups == 0 && c.shutdown_failed);
      break;
    case 4: { /* Failed QBUF cannot discard a potentially DMA-owned container. */
      c.capture_inf.state = CAPTURE_STATE_STREAMON;
      vacant = true; start_result = -EIO; stop_result = -EAGAIN;
      struct v4l2_buffer b = {.length=64, .type=type};
      assert(capture_qbuf(&f, &b) == -EIO);
      assert(c.capture_inf.lower_active && queue_removes == 0 && queue_frees == 0);
      assert(capture_qbuf(&f, &b) == -EAGAIN);
      stop_result = 0;
      assert(capture_streamoff(&f, &type) == 0);
      assert(capture_close(&f) == 0 && cleanups == 3 && !c.shutdown_failed);
      break;
    }
    case 5: /* Still stop cannot advance its state or resume video on refusal. */
      c.still_inf.state = CAPTURE_STATE_CAPTURE;
      c.still_inf.lower_active = true; c.still_inf.remaining_capnum = 3;
      sensor_stop_result = -EAGAIN;
      assert(capture_takepict_stop(&f, false) == -EAGAIN);
      assert(c.still_inf.state == CAPTURE_STATE_CAPTURE && c.still_inf.remaining_capnum == 3);
      assert(c.still_inf.lower_active && stops == 1 && sensor_stops == 1);
      sensor_stop_result = 0;
      assert(capture_takepict_stop(&f, false) == 0 && !c.still_inf.lower_active);
      break;
    case 6: { /* Pending partial start forbids queue/storage reallocation. */
      c.capture_inf.lower_active = true;
      struct v4l2_requestbuffers b = {.count=2, .type=type};
      assert(capture_reqbufs(&f, &b) == -EPERM && reallocs == 0 && queue_frees == 0);
      break;
    }
    case 7: /* A clean start failure still rolls QBUF back normally. */
      c.capture_inf.state = CAPTURE_STATE_STREAMON;
      vacant = true; start_result = -EIO;
      struct v4l2_buffer b = {.length=64, .type=type};
      assert(capture_qbuf(&f, &b) == -EIO);
      assert(!c.capture_inf.lower_active && queue_removes == 1 && queue_frees == 1);
      break;
    default: return 2;
  }
  return 0;
}

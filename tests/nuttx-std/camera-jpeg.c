/* Bounds and marker tests for the actual ESP32-S3 JPEG extent helper. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <sys/time.h>
#define IRAM_ATTR
#define ESP32S3_CAM_DMA_BUFLEN 4096
#define LCD_CAM_CAM_VSYNC_INT_CLR_M 4
#define LCD_CAM_CAM_VSYNC_INT_ST_M 2
#define LCD_CAM_CAM_HS_INT_ST_M 8
#define LCD_CAM_CAM_HS_INT_CLR_M 16
#define CONFIG_ESP32S3_CAM_HREF_PIN 1
#define CONFIG_ESP32S3_CAM_VSYNC_PIN 2
#define CAM_V_SYNC_IDX 3
#define CAM_H_ENABLE_IDX 4
#define GPIO_MATRIX_CONST_ZERO_INPUT 0x3c
#define ESP32S3_DMA_CTRL_DATALEN_S 0
#define ESP32S3_DMA_CTRL_DATALEN_V 0xfff
#define IMGDATA_PIX_FMT_JPEG 1
#define IMGDATA_FMT_MAIN 0
#define ESP32S3_CAM_DMA_ALIGN 64
#define CONFIG_ESP32S3_CAM_DMA_BUFSIZE 256
#define ESP32S3_CAM_DMADESC_NUM 1
#define OK 0
#define kmm_memalign(align, size) aligned_alloc(align, size)
#define snerr(...) ((void)0)
#include <errno.h>

struct fake_hw {
  bool running;
  uint32_t pending;
  struct { bool cam_vsync_int_ena; } lc_dma_int_ena;
  struct { bool cam_vs_eof_en, cam_update; } cam_ctrl;
};
struct imgdata_s { int unused; };
typedef struct { uint16_t width, height; uint32_t pixelformat; } imgdata_format_t;
struct esp32s3_dmadesc_s { uint32_t ctrl; struct esp32s3_dmadesc_s *next; };
struct esp32s3_cam_s {
  struct imgdata_s data;
  struct fake_hw *hw;
  int dma_channel;
  struct esp32s3_dmadesc_s *dmadesc;
  bool capturing;
  uint8_t vsync_cnt;
  uint8_t *fb;
  uint32_t fb_size, pixfmt, width, height;
  bool fb_allocated;
  void (*cb)(int, uint32_t, struct timeval *, void *);
  void *cb_arg;
};
static char events[128];
static unsigned int event_count;
static bool dma_enabled;
static bool external_memory;
static uintptr_t invalidated;
static unsigned int dma_setups;
static bool href_active;
static bool vsync_active = true;
static bool href_gated;
static unsigned int href_rise_on_delay;
static unsigned int vsync_fall_on_delay;
static unsigned int loss_on_arm;
static unsigned int synthetic_edges;
static unsigned int delay_calls;
static struct fake_hw *sync_hw;
static void event(char value)
{
  assert(event_count + 1 < sizeof(events));
  events[event_count++] = value;
  events[event_count] = 0;
}
#define cam_ll_stop(hw) ((hw)->running = false, event('s'))
#define cam_ll_reset(hw) ((void)(hw), event('r'))
#define cam_ll_fifo_reset(hw) ((void)(hw), event('f'))
#define esp32s3_dma_reset_channel(channel, tx) \
  ((void)(channel), (void)(tx), dma_enabled = false, event('d'))
#define cam_ll_set_recv_data_bytelen(hw, size) \
  ((void)(hw), assert((size) == 4095), event('b'))
#define esp32s3_dma_load(desc, channel, tx) \
  ((void)(desc), (void)(channel), (void)(tx), event('l'))
#define esp32s3_dma_enable(channel, tx) \
  ((void)(channel), (void)(tx), fake_dma_enable())
#define cam_ll_clear_interrupt_status(hw, mask) \
  fake_clear_interrupt_status((hw), (mask))
#define cam_ll_start(hw) \
  ((hw)->running = true, (hw)->cam_ctrl.cam_update = true, event('t'))
#define cam_ll_get_interrupt_status(hw) ((hw)->pending)
#define esp_gpioread(pin) fake_gpioread(pin)
#define cam_ll_enable_vsync_generate_eof(hw, enable) \
  fake_enable_vsync_eof((hw), (enable))
#define esp_gpio_matrix_in(pin, signal, invert) \
  fake_gpio_matrix_in((pin), (signal), (invert))
#define up_udelay(usec) fake_udelay(usec)
#define esp32s3_ptr_extram(ptr) ((void)(ptr), external_memory)
#define up_invalidate_dcache(begin, end) \
  (assert(!dma_enabled), invalidated = (end) - (begin))
#define up_flush_dcache(begin, end) \
  (assert(!dma_enabled && (end) > (begin)), event('w'))

static bool fake_gpioread(int pin)
{
  assert(pin == CONFIG_ESP32S3_CAM_HREF_PIN ||
         pin == CONFIG_ESP32S3_CAM_VSYNC_PIN);
  return pin == CONFIG_ESP32S3_CAM_HREF_PIN ? href_active : vsync_active;
}

static void fake_dma_enable(void)
{
  dma_enabled = true;
  if (loss_on_arm == 1) href_active = true;
  if (loss_on_arm == 2) vsync_active = false;
  event('e');
}

static void fake_clear_interrupt_status(struct fake_hw *hw, uint32_t mask)
{
  assert(mask == LCD_CAM_CAM_VSYNC_INT_CLR_M || mask == LCD_CAM_CAM_HS_INT_CLR_M);
  if (mask == LCD_CAM_CAM_VSYNC_INT_CLR_M) hw->pending = 0;
  event('c');
}

static void fake_enable_vsync_eof(struct fake_hw *hw, bool enable)
{
  hw->cam_ctrl.cam_vs_eof_en = enable;
  event(enable ? 'V' : 'v');
}

static void fake_gpio_matrix_in(int pin, int signal, bool invert)
{
  assert(sync_hw != NULL);
  assert(!sync_hw->lc_dma_int_ena.cam_vsync_int_ena);
  assert(!sync_hw->cam_ctrl.cam_vs_eof_en);
  if (signal == CAM_H_ENABLE_IDX)
    {
      assert(!invert);
      if (pin == GPIO_MATRIX_CONST_ZERO_INPUT)
        {
          assert(!sync_hw->running && !dma_enabled);
          href_gated = true;
          event('h');
        }
      else
        {
          assert(pin == CONFIG_ESP32S3_CAM_HREF_PIN && href_gated);
          assert(!sync_hw->running ||
                 (dma_enabled && vsync_active && !href_active));
          href_gated = false;
          event('H');
        }
      return;
    }
  assert(pin == CONFIG_ESP32S3_CAM_VSYNC_PIN && signal == CAM_V_SYNC_IDX);
  assert(href_gated && !dma_enabled);
  synthetic_edges++;
  /* Raw VSYNC may latch even with its interrupt masked. The production
   * path must clear it before restoring the interrupt enable.
   */
  sync_hw->pending = LCD_CAM_CAM_VSYNC_INT_ST_M;
  event(invert ? '+' : '-');
}

static void fake_udelay(unsigned int usec)
{
  assert(usec == 10);
  assert(sync_hw != NULL);
  assert(!sync_hw->lc_dma_int_ena.cam_vsync_int_ena);
  assert(!sync_hw->cam_ctrl.cam_vs_eof_en);
  assert(href_gated && !dma_enabled);
  delay_calls++;
  if (delay_calls == href_rise_on_delay) href_active = true;
  if (delay_calls == vsync_fall_on_delay) vsync_active = false;
  event('1');
}

static uint32_t esp32s3_dma_setup(struct esp32s3_dmadesc_s *desc, int count,
                                 uint8_t *data, uint32_t size, bool tx, int channel)
{
  (void)desc; (void)data; (void)tx; (void)channel;
  assert(count == ESP32S3_CAM_DMADESC_NUM);
  dma_setups++;
  event('u');
  return size;
}

/* GATING */

/* FUNCTION */

/* INTERRUPT */

/* SET_BUFFER */

static unsigned int completions;
static unsigned int stop_at;
static int expected_error;
static uint32_t expected_size;

static void complete(int error, uint32_t bytes, struct timeval *ts, void *arg)
{
  struct esp32s3_cam_s *camera = arg;
  assert(!dma_enabled); /* No DMA alias while the upper half replaces storage. */
  assert(error == expected_error && bytes == expected_size && ts != NULL);
  if (++completions == stop_at) camera->capturing = false;
}

static void boundary(struct esp32s3_cam_s *camera)
{
  event_count = 0;
  camera->hw->pending = LCD_CAM_CAM_VSYNC_INT_ST_M;
  assert(cam_interrupt(0, NULL, camera) == 0);
}

int main(void)
{
  struct fake_hw hw = {0};
  struct esp32s3_cam_s camera = { .hw = &hw };
  sync_hw = &hw;
  cam_prepare_frame(&camera, false);
  assert(hw.running && !dma_enabled && strcmp(events, "srfdbct") == 0);
  assert(synthetic_edges == 0 && delay_calls == 0);

  _Alignas(64) uint8_t dma_frame[256];
  memset(dma_frame, 0x55, sizeof(dma_frame));
  imgdata_format_t format = { .width = 2, .height = 6, .pixelformat = IMGDATA_PIX_FMT_JPEG };
  external_memory = true;
  event_count = 0;
  assert(esp32s3_cam_set_buf(&camera.data, 1, &format, dma_frame, sizeof(dma_frame)) == 0);
  assert(dma_setups == 1 && strcmp(events, "wu") == 0);
  for (unsigned int i = 0; i < sizeof(dma_frame); i++) assert(dma_frame[i] == 0x55);
  assert(esp32s3_cam_set_buf(&camera.data, 1, &format, dma_frame + 1, sizeof(dma_frame)) == -EINVAL);
  assert(esp32s3_cam_set_buf(&camera.data, 1, &format, dma_frame, 257) == -EFBIG);
  assert(dma_setups == 1);
  assert(esp32s3_cam_set_buf(&camera.data, 1, &format, NULL, 0) == 0);
  assert(camera.fb_size == 64 && camera.fb_allocated);
  free(camera.fb);
  event_count = 0;
  cam_prepare_frame(&camera, true);
  assert(hw.running && dma_enabled);
  assert(strcmp(events, "srfdbcvh+t1-1leHcv") == 0);
  assert(synthetic_edges == 2 && delay_calls == 2);
  event_count = 0;
  cam_prepare_frame(&camera, false);
  assert(hw.running && !dma_enabled && strcmp(events, "srfdbct") == 0);

  /* Priming restores the caller's interrupt and EOF settings exactly. */
  for (unsigned int irq = 0; irq < 2; irq++)
    for (unsigned int eof = 0; eof < 2; eof++)
      {
        hw.lc_dma_int_ena.cam_vsync_int_ena = irq != 0;
        hw.cam_ctrl.cam_vs_eof_en = eof != 0;
        hw.cam_ctrl.cam_update = false;
        hw.pending = LCD_CAM_CAM_VSYNC_INT_ST_M;
        synthetic_edges = 0;
        delay_calls = 0;
        event_count = 0;
        cam_prepare_frame(&camera, true);
        assert(hw.lc_dma_int_ena.cam_vsync_int_ena == (irq != 0));
        assert(hw.cam_ctrl.cam_vs_eof_en == (eof != 0));
        assert(hw.cam_ctrl.cam_update && hw.pending == 0);
        assert(synthetic_edges == 2 && delay_calls == 2);
        assert(strcmp(events, eof ? "srfdbcvh+t1-1leHcV" :
                                  "srfdbcvh+t1-1leHcv") == 0);
      }

  /* Initial active data or horizontal blanking must wait with DMA off. */
  href_active = true;
  hw.lc_dma_int_ena.cam_vsync_int_ena = true;
  hw.cam_ctrl.cam_vs_eof_en = true;
  hw.cam_ctrl.cam_update = false;
  synthetic_edges = delay_calls = 0;
  event_count = 0;
  cam_prepare_frame(&camera, true);
  assert(hw.running);
  assert(hw.lc_dma_int_ena.cam_vsync_int_ena && hw.cam_ctrl.cam_vs_eof_en);
  assert(!dma_enabled && camera.vsync_cnt == 0);
  assert(synthetic_edges == 0 && delay_calls == 0);
  assert(strcmp(events, "srfdbct") == 0);
  href_active = false;
  vsync_active = false;
  camera.vsync_cnt = 1;
  event_count = 0;
  cam_prepare_frame(&camera, true);
  assert(!dma_enabled && camera.vsync_cnt == 0 && !href_gated);
  assert(synthetic_edges == 0 && delay_calls == 0);
  assert(strcmp(events, "srfdbct") == 0);
  vsync_active = true;

  /* Lose the blanking window during either delay. Physical HREF can rise
   * but cannot enter the gated receiver; no partial DMA buffer is published.
   */
  for (unsigned int change = 1; change <= 3; change++)
    for (unsigned int href = 0; href < 2; href++)
      {
        href_active = false;
        vsync_active = true;
        href_rise_on_delay = href && change <= 2 ? change : 0;
        vsync_fall_on_delay = !href && change <= 2 ? change : 0;
        loss_on_arm = change == 3 ? (href ? 1 : 2) : 0;
        camera.vsync_cnt = 1;
        hw.lc_dma_int_ena.cam_vsync_int_ena = true;
        hw.cam_ctrl.cam_vs_eof_en = true;
        event_count = synthetic_edges = delay_calls = 0;
        cam_prepare_frame(&camera, true);
        assert(hw.running && !dma_enabled && !href_gated);
        assert(camera.vsync_cnt == 0 && hw.pending == 0);
        assert(hw.lc_dma_int_ena.cam_vsync_int_ena);
        assert(hw.cam_ctrl.cam_vs_eof_en);
        assert(synthetic_edges == 2 && delay_calls == 2);
        assert(strcmp(events, "srfdbcvh+t1-1lesrfdHcVt") == 0);
      }
  href_rise_on_delay = vsync_fall_on_delay = 0;
  loss_on_arm = 0;
  href_active = false;
  vsync_active = true;

  /* APP data includes a false EOI; scan data includes stuffed FF00 D9 and
   * restart markers. The final zero padding must never be published.
   */
  const uint8_t jpeg[] = {
    0xff, 0xd8, 0xff, 0xe0, 0x00, 0x06, 0x01, 0xff, 0xd9, 0x02,
    0xff, 0xda, 0x00, 0x02, 0x44, 0xff, 0x00, 0xd9,
    0xff, 0xd0, 0x12, 0xff, 0xff, 0xd9, 0x00, 0x00
  };
  uint8_t data[sizeof(jpeg)];
  assert(cam_jpeg_length(jpeg, sizeof(jpeg)) == 24);
  for (unsigned int size = 0; size < 24; size++)
    {
      assert(cam_jpeg_length(jpeg, size) == 0);
    }

  memcpy(data, jpeg, sizeof(data));
  data[0] = 0;
  assert(cam_jpeg_length(data, sizeof(data)) == 0);
  memcpy(data, jpeg, sizeof(data));
  data[4] = 0xff;
  assert(cam_jpeg_length(data, sizeof(data)) == 0);
  memcpy(data, jpeg, sizeof(data));
  data[5] = 1;
  assert(cam_jpeg_length(data, sizeof(data)) == 0);
  memcpy(data, jpeg, sizeof(data));
  data[13] = 1;
  assert(cam_jpeg_length(data, sizeof(data)) == 0);

  struct esp32s3_dmadesc_s descriptor = { .ctrl = 24 };
  camera.dmadesc = &descriptor;
  memcpy(dma_frame, jpeg, sizeof(jpeg));
  camera.fb = dma_frame;
  camera.fb_size = sizeof(dma_frame);
  camera.pixfmt = IMGDATA_PIX_FMT_JPEG;
  camera.cb = complete;
  camera.cb_arg = &camera;
  camera.capturing = true;
  completions = 0;
  stop_at = 2;
  expected_size = 24;
  expected_error = 0;
  boundary(&camera); /* Initial boundary, not a completed image. */
  assert(completions == 0 && camera.vsync_cnt == 1 && dma_enabled);
  boundary(&camera); /* A queued replacement receives the immediately next frame. */
  assert(completions == 1 && camera.vsync_cnt == 1 && dma_enabled);
  assert(invalidated == 64); /* Actual JPEG extent, not its 256-byte capacity. */
  boundary(&camera); /* FIFO exhausted/stop in callback must never rearm DMA. */
  assert(completions == 2 && camera.vsync_cnt == 0 && !dma_enabled && !hw.running);

  /* A late rearm after a valid completion must retry synchronization, not
   * miscount the next boundary as a completed empty/partial frame.
   */
  camera.capturing = true;
  camera.vsync_cnt = 1;
  stop_at = 4;
  href_rise_on_delay = delay_calls + 1;
  boundary(&camera);
  assert(completions == 3 && camera.vsync_cnt == 0 && !dma_enabled);
  href_rise_on_delay = 0;
  href_active = false;
  boundary(&camera);
  assert(completions == 3 && camera.vsync_cnt == 1 && dma_enabled);
  boundary(&camera);
  assert(completions == 4 && !dma_enabled && !hw.running);

  camera.capturing = true;
  camera.vsync_cnt = 1;
  camera.pixfmt = 0;
  camera.width = 2;
  camera.height = 6;
  descriptor.ctrl = 23;
  expected_size = 23;
  expected_error = EIO;
  stop_at = 5;
  boundary(&camera);
  assert(completions == 5 && !dma_enabled); /* Raw short frames remain errors. */
  puts("JPEG extent: APP/entropy/truncation/padding bounds pass");
  return 0;
}

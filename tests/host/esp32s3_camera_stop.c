/* Actual patched camera functions, with controlled hardware acknowledgments.
 * This is a source/ownership regression, not a hardware bus timing model.
 */
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <sys/time.h>
#include "dma_registers.h"
#define IRAM_ATTR
#define OK 0
#define ESP32S3_CAM_DMA_ALIGN 64
#define ESP32S3_CAM_DMA_BUFLEN 4096
#define ESP32S3_DMA_CTRL_DATALEN_S 0
#define ESP32S3_DMA_CTRL_DATALEN_V 4095
#define IMGDATA_PIX_FMT_JPEG 1
#define LCD_CAM_CAM_VSYNC_INT_ST_M 1
#define LCD_CAM_CAM_VSYNC_INT_CLR_M 1
#define LCD_CAM_CAM_HS_INT_ST_M 2
#define LCD_CAM_CAM_HS_INT_CLR_M 2
#define CONFIG_ESP32S3_CAM_VSYNC_PIN 1
#define CONFIG_ESP32S3_CAM_HREF_PIN 2
#define GPIO_MATRIX_CONST_ZERO_INPUT 0
#define CAM_H_ENABLE_IDX 0
#define CAM_V_SYNC_IDX 1
#define ESP32S3_IRQ_LCD_CAM 0
#define ESP32S3_PERIPH_LCD_CAM 0
#define GET_GDMA_CH_REG(reg, chan) read_dma(reg, chan)
typedef unsigned irqstate_t;
struct imgdata_s { int unused; };
typedef void (*imgdata_capture_t)(int, uint32_t, const struct timeval *, void *);
struct esp32s3_dmadesc_s { uint32_t ctrl; struct esp32s3_dmadesc_s *next; };
struct hw_s {
  struct { unsigned cam_vsync_int_ena; uint32_t val; } lc_dma_int_ena;
  struct { unsigned cam_vs_eof_en, cam_update; } cam_ctrl;
};
struct esp32s3_cam_s {
  struct imgdata_s data;
  struct hw_s *hw;
  bool capturing, stop_pending;
  int cpuint, dma_channel, lock;
  struct esp32s3_dmadesc_s *dmadesc;
  uint8_t *fb;
  uint32_t fb_size;
  bool fb_allocated;
  uint8_t vsync_cnt;
  imgdata_capture_t cb;
  void *cb_arg;
  uint16_t width, height;
  uint32_t pixfmt;
};
static unsigned park, fifo, reset_fifo, reads, resets, releases, frees;
static unsigned callbacks, enables, irq_teardowns, stop_commands;
static uint32_t read_dma(uint32_t reg, int channel)
{
  assert(channel == 2); assert(++reads <= 20);
  if (reg == DMA_IN_LINK_CH0_REG) return park;
  assert(reg == DMA_INFIFO_STATUS_CH0_REG); return fifo;
}
static irqstate_t spin_lock_irqsave(int *lock) { (void)lock; return 0; }
static void spin_unlock_irqrestore(int *lock, irqstate_t f) { (void)lock; (void)f; }
static void cam_ll_stop(struct hw_s *hw) { (void)hw; }
static void cam_ll_reset(struct hw_s *hw) { (void)hw; }
static void cam_ll_fifo_reset(struct hw_s *hw) { (void)hw; }
static void esp32s3_dma_disable(int ch, bool tx) { assert(ch == 2 && !tx); stop_commands++; }
static void esp32s3_dma_reset_channel(int ch, bool tx)
{ assert(ch == 2 && !tx); resets++; fifo = reset_fifo; }
static void esp32s3_dma_release(int ch) { assert(ch == 2); releases++; }
static void cam_ll_clear_interrupt_status(struct hw_s *hw, unsigned s) { (void)hw; (void)s; }
static void cam_ll_set_recv_data_bytelen(struct hw_s *hw, unsigned s) { (void)hw; (void)s; }
static void cam_ll_enable_vsync_generate_eof(struct hw_s *hw, bool v) { (void)hw; (void)v; }
static void cam_ll_start(struct hw_s *hw) { (void)hw; }
static unsigned cam_ll_get_interrupt_status(struct hw_s *hw) { (void)hw; return 1; }
static int esp_gpioread(int pin) { return pin == CONFIG_ESP32S3_CAM_VSYNC_PIN; }
static void esp_gpio_matrix_in(int pin, int sig, bool inv) { (void)pin; (void)sig; (void)inv; }
static void up_udelay(unsigned us) { (void)us; }
static void esp32s3_dma_load(void *desc, int ch, bool tx) { (void)desc; (void)ch; (void)tx; }
static void esp32s3_dma_enable(int ch, bool tx) { (void)ch; (void)tx; enables++; }
static bool esp32s3_ptr_extram(void *p) { (void)p; return false; }
static void up_invalidate_dcache(uintptr_t p, uintptr_t end) { (void)p; (void)end; }
static unsigned cam_jpeg_length(void *p, unsigned len) { (void)p; return len; }
static void up_disable_irq(int irq) { (void)irq; }
static void esp_teardown_irq(int p, int irq) { (void)p; (void)irq; irq_teardowns++; }
static void kmm_free(void *p) { assert(p); frees++; }
static int esp32s3_cam_stop_capture(struct imgdata_s *data);
#include "camera_stop.inc"
static void capture_done(int error, uint32_t len, const struct timeval *ts, void *arg)
{
  (void)error; (void)len; (void)ts;
  struct esp32s3_cam_s *p = arg;
  callbacks++; p->capturing = false;
}
int main(int argc, char **argv)
{
  assert(argc == 2);
  struct hw_s hw = {0};
  struct esp32s3_dmadesc_s descriptor = {.ctrl = 2};
  uint8_t buffer[64];
  struct esp32s3_cam_s p = {.hw = &hw, .dma_channel = 2, .capturing = true,
    .cb = capture_done, .dmadesc = &descriptor, .fb = buffer, .fb_size = 64,
    .fb_allocated = true, .width = 1, .height = 1, .vsync_cnt = 1};
  p.cb_arg = &p;
  unsigned empty = DMA_INFIFO_EMPTY_L1_CH0 | DMA_INFIFO_EMPTY_L2_CH0 | DMA_INFIFO_EMPTY_L3_CH0;
  reset_fifo = empty;
  switch (atoi(argv[1])) {
    case 0: /* Delayed PARK: one snapshot, no reset/free, then explicit retry. */
      assert(esp32s3_cam_stop_capture(&p.data) == -EAGAIN);
      assert(!p.capturing && !p.cb && !p.cb_arg && p.stop_pending);
      assert(reads == 1 && resets == 0 && frees == 0 && releases == 0);
      park = DMA_INLINK_PARK_CH0;
      assert(esp32s3_cam_stop_capture(&p.data) == 0 && !p.stop_pending);
      assert(reads == 4 && resets == 1 && stop_commands == 2);
      break;
    case 1: /* Every FIFO level participates in the abort acknowledgment. */
      park = DMA_INLINK_PARK_CH0;
      for (unsigned bit = 1; bit <= 5; bit += 2) {
        reset_fifo = empty & ~(1u << bit); reads = 0;
        assert(esp32s3_cam_stop_capture(&p.data) == -EAGAIN && p.stop_pending);
        assert(reads == 3 && releases == 0 && frees == 0);
      }
      reset_fifo = empty;
      assert(esp32s3_cam_stop_capture(&p.data) == 0);
      break;
    case 2: /* Failed uninit retains IRQ context, channel and both allocations. */
      assert(esp32s3_cam_uninit(&p.data) == -EAGAIN);
      assert(p.dmadesc == &descriptor && p.fb == buffer && p.dma_channel == 2);
      assert(frees == 0 && releases == 0 && irq_teardowns == 0);
      park = DMA_INLINK_PARK_CH0;
      assert(esp32s3_cam_uninit(&p.data) == 0);
      assert(!p.dmadesc && !p.fb && p.dma_channel == -1);
      assert(frees == 2 && releases == 1 && irq_teardowns == 1);
      break;
    case 3: /* An ISR cannot publish/rearm pending DMA storage. */
      assert(cam_interrupt(0, NULL, &p) == -EAGAIN);
      assert(!p.capturing && !p.cb && callbacks == 0 && enables == 0);
      assert(cam_interrupt(0, NULL, &p) == 0 && callbacks == 0);
      park = DMA_INLINK_PARK_CH0;
      assert(esp32s3_cam_stop_capture(&p.data) == 0);
      break;
    case 4: /* Acknowledged frame still publishes once, without rearming after stop. */
      park = DMA_INLINK_PARK_CH0;
      assert(cam_interrupt(0, NULL, &p) == 0);
      assert(callbacks == 1 && enables == 0 && !p.capturing);
      break;
    default: return 2;
  }
  return 0;
}

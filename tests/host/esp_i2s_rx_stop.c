/* Run the pinned receive and stop functions with controlled DMA and callbacks. */

#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>

#define FAR
#define inline_function inline
#define CONFIG_LIBC_INLINE_QUEUE 1
#include "queue.h"
#include "dma_registers.h"

#define CONFIG_ARCH_CHIP_ESP32S3 1
#define I2S_HAVE_RX 1
#define ESPRESSIF_DMA_BUFLEN_MAX 4095
#define I2S_DMADESC_NUM 2
#define AUDIO_APB_FINAL 1
#define AUDIOIOC_START 1
#define AUDIOIOC_STOP 2
#define AUDIOIOC_ALLOCBUFFER 3
#define AUDIOIOC_FREEBUFFER 4
#define DEBUGASSERT assert
#define MIN(a, b) ((a) < (b) ? (a) : (b))
#define i2serr(...) ((void)0)
#define i2sinfo(...) ((void)0)
#define OK 0

struct i2s_dev_s { int unused; };
struct ap_buffer_s
{
  uint8_t *samp;
  unsigned int nmaxbytes, nbytes, curbyte, flags, references;
};
struct audio_buf_desc_s { union { struct ap_buffer_s *buffer; } u; };
struct esp_dmadesc_s
{
  struct { unsigned int suc_eof, length; } dw0;
  struct esp_dmadesc_s *next;
};
typedef struct esp_dmadesc_s dma_descriptor_t;
typedef unsigned int irqstate_t;
typedef void (*i2s_callback_t)(struct i2s_dev_s *, struct ap_buffer_s *, void *, int);

struct esp_buffer_s
{
  sq_entry_t link;
  struct esp_dmadesc_s dma_link[I2S_DMADESC_NUM];
  struct ap_buffer_s *apb;
  void *arg;
  i2s_callback_t callback;
  uint32_t timeout, nbytes;
  int result;
  bool taken;
};

struct esp_i2s_config_s { bool rx_en, tx_en; };

#ifdef RETRYABLE_RX_STOP
#include "rx_state.inc"
#endif

struct esp_i2s_s
{
  struct i2s_dev_s dev;
  struct esp_i2s_config_s *config;
  struct { sq_queue_t act, pend, done; } rx;
  unsigned int dma_channel, channels, data_width, rx_users;
  int rx_state;
  bool rx_started, streaming, lock, slock;
};

static struct esp_i2s_config_s config = { .rx_en = true };
static struct esp_i2s_s device = { .config = &config, .dma_channel = 2,
                                   .channels = 2, .data_width = 32 };
static struct esp_buffer_s containers[3];
static struct ap_buffer_s audio[4];
static uint8_t samples[4][2560];
static bool rx_parked = true, tx_parked = true, stop_in_setup, stop_in_callback;
static bool check_reentry, assert_try_allocate, exhaust, lock_busy;
static unsigned int starts, stops, deliveries, canceled, final_flags;
static unsigned int cancellation_order[3];

static int i2s_ioctl(struct i2s_dev_s *dev, int cmd, unsigned long arg);
static int i2s_receive(struct i2s_dev_s *dev, struct ap_buffer_s *apb,
                       i2s_callback_t callback, void *arg, uint32_t timeout);

static irqstate_t spin_lock_irqsave(bool *lock)
{
  assert(!*lock);
  *lock = true;
  return 0;
}

static void spin_unlock_irqrestore(bool *lock, irqstate_t flags)
{
  assert(*lock && flags == 0);
  *lock = false;
}

static int nxmutex_lock(bool *lock)
{
  assert(!*lock);
  *lock = true;
  return OK;
}

static int nxmutex_trylock(bool *lock)
{
  return lock_busy ? -EBUSY : nxmutex_lock(lock);
}

static void nxmutex_unlock(bool *lock)
{
  assert(*lock);
  *lock = false;
}

#ifdef RETRYABLE_RX_STOP
static struct esp_buffer_s *i2s_buf_allocate(struct esp_i2s_s *priv, bool wait)
#else
static struct esp_buffer_s *i2s_buf_allocate(struct esp_i2s_s *priv)
#endif
{
#ifdef RETRYABLE_RX_STOP
  assert(!assert_try_allocate || !wait);
#endif
  if (!exhaust)
    {
      for (unsigned int i = 0; i < 3; i++)
        {
          if (!containers[i].taken)
            {
              containers[i].taken = true;
              return &containers[i];
            }
        }
    }

  return NULL;
}

static void i2s_buf_free(struct esp_i2s_s *priv, struct esp_buffer_s *buffer)
{
  assert(buffer->taken && !priv->slock);
  buffer->apb = NULL;
  buffer->taken = false;
}

static int apb_alloc(struct audio_buf_desc_s *desc) { return -ENOSYS; }
static void apb_reference(struct ap_buffer_s *apb) { apb->references++; }

static void apb_free(struct ap_buffer_s *apb)
{
  assert(apb->references == 2);
  apb->references--;
}

static void i2s_rx_channel_stop(struct esp_i2s_s *priv)
{
  assert(priv->slock);
  if (priv->rx_started)
    {
      priv->rx_started = false;
      stops++;
    }
}

static int i2s_rxdma_start(struct esp_i2s_s *priv)
{
  assert(priv->slock);
  if (sq_empty(&priv->rx.act) && !sq_empty(&priv->rx.pend))
    {
      sq_entry_t *next = sq_remfirst(&priv->rx.pend);
      sq_addlast(next, &priv->rx.act);
      priv->rx_started = true;
      priv->streaming = true;
      starts++;
    }

  return 0;
}

static uint32_t esp_dma_setup(struct esp_dmadesc_s *descriptors,
                             unsigned int count, uint8_t *sample,
                             uint32_t size, bool tx, unsigned int channel)
{
  assert(!tx && channel == device.dma_channel);
  if (stop_in_setup)
    {
      stop_in_setup = false;
      assert(i2s_ioctl(&device.dev, AUDIOIOC_STOP, 0) == -EAGAIN);
    }

  descriptors[0].dw0.suc_eof = 1;
  descriptors[0].dw0.length = size;
  return size;
}

#define GET_GDMA_CH_REG(reg, chan) read_dma_register(reg, chan)
static uint32_t read_dma_register(uint32_t reg, int chan)
{
  assert((unsigned int)chan == device.dma_channel);
  assert(reg == DMA_IN_LINK_CH0_REG || reg == DMA_OUT_LINK_CH0_REG);
  if (reg == DMA_IN_LINK_CH0_REG)
    {
      return rx_parked ? DMA_INLINK_PARK_CH0 : 0;
    }

  return tx_parked ? DMA_OUTLINK_PARK_CH0 : 0;
}

#include "rx_stop.inc"

static void completion(struct i2s_dev_s *dev, struct ap_buffer_s *apb,
                       void *arg, int result)
{
  assert(!device.slock && apb->references == 2);
  if (result == -ECANCELED)
    {
      assert(rx_parked && apb->nbytes == 0 && apb->curbyte == 0);
      assert(canceled < 3);
      cancellation_order[canceled++] = apb - audio;
      final_flags += (apb->flags & AUDIO_APB_FINAL) != 0;
      if (check_reentry)
        {
          assert(i2s_receive(dev, &audio[3], completion, NULL, 0) == -EBUSY);
          assert(i2s_ioctl(dev, AUDIOIOC_START, 0) == -EBUSY);
          assert(i2s_ioctl(dev, AUDIOIOC_STOP, 0) == -EAGAIN);
          assert(audio[3].references == 1 && starts == 0);
        }
    }
  else
    {
      assert(result == 0 && apb->nbytes == sizeof(samples[0]));
      deliveries++;
      if (stop_in_callback)
        {
          assert(i2s_ioctl(dev, AUDIOIOC_STOP, 0) == -EAGAIN);
        }
    }
}

static void add_buffer(sq_queue_t *queue, int id)
{
  struct esp_buffer_s *buf = &containers[id];
  buf->apb = &audio[id];
  buf->callback = completion;
  buf->taken = true;
  apb_reference(buf->apb);
  buf->apb->nbytes = 8;
  buf->apb->curbyte = 8;
  buf->dma_link[0].dw0.suc_eof = 1;
  buf->dma_link[0].dw0.length = sizeof(samples[0]);
  sq_addlast(&buf->link, queue);
}

static void assert_pool_released(void)
{
  assert(sq_empty(&device.rx.act) && sq_empty(&device.rx.pend));
  assert(sq_empty(&device.rx.done) && !device.slock && !device.lock);
  assert(device.rx_users == 0 && device.rx_state == 0);
  for (unsigned int i = 0; i < 3; i++)
    {
      assert(!containers[i].taken && audio[i].references == 1);
    }
}

int main(int argc, char **argv)
{
  assert(argc == 2);
  for (unsigned int i = 0; i < 4; i++)
    {
      audio[i].samp = samples[i];
      audio[i].nmaxbytes = sizeof(samples[i]);
      audio[i].references = 1;
    }

  switch (atoi(argv[1]))
    {
      case 0:
        device.rx_started = device.streaming = true;
        add_buffer(&device.rx.act, 0);
        add_buffer(&device.rx.pend, 1);
        add_buffer(&device.rx.done, 2);
        rx_parked = false;
        for (int retry = 0; retry < 3; retry++)
          {
            assert(i2s_ioctl(&device.dev, AUDIOIOC_STOP, 0) == -EAGAIN);
            assert(canceled == 0 && audio[0].references == 2 && stops == 1);
            assert(!sq_empty(&device.rx.act) && !sq_empty(&device.rx.pend));
            assert(!sq_empty(&device.rx.done) && starts == 0);
          }

        rx_parked = true;
        assert(i2s_ioctl(&device.dev, AUDIOIOC_STOP, 0) == OK);
        assert(canceled == 3 && final_flags == 1 && stops == 1);
        assert(cancellation_order[0] == 2 && cancellation_order[1] == 0 &&
               cancellation_order[2] == 1 && (audio[1].flags & AUDIO_APB_FINAL));
        assert_pool_released();
        assert(i2s_ioctl(&device.dev, AUDIOIOC_START, 0) == OK);
        audio[0].flags = 0;
        assert(i2s_receive(&device.dev, &audio[0], completion, NULL, 0) == OK);
        assert(starts == 1 && sq_peek(&device.rx.act) == &containers[0].link);
        break;
      case 1:
        device.rx_started = device.streaming = true;
        add_buffer(&device.rx.done, 0);
        add_buffer(&device.rx.pend, 1);
        stop_in_callback = true;
        i2s_rx_worker(&device);
        assert(deliveries == 1 && canceled == 0 && starts == 0);
        assert(audio[0].references == 1 && audio[1].references == 2);
        assert(i2s_ioctl(&device.dev, AUDIOIOC_STOP, 0) == OK);
        assert(canceled == 1 && cancellation_order[0] == 1);
        assert_pool_released();
        break;
      case 2:
        stop_in_setup = true;
        assert(i2s_receive(&device.dev, &audio[0], completion, NULL, 0) == -EBUSY);
        assert(canceled == 0 && starts == 0 && audio[0].references == 1);
        assert(i2s_ioctl(&device.dev, AUDIOIOC_STOP, 0) == OK);
        assert_pool_released();
        break;
      case 3:
        device.rx_started = device.streaming = true;
        check_reentry = true;
        add_buffer(&device.rx.act, 0);
        assert(i2s_ioctl(&device.dev, AUDIOIOC_STOP, 0) == OK);
        assert(canceled == 1 && final_flags == 1);
        assert_pool_released();
        break;
      case 4:
        device.streaming = true;
        add_buffer(&device.rx.done, 0);
        i2s_rx_worker(&device);
        assert(deliveries == 1 && canceled == 0 && starts == 0);
        assert_pool_released();
        break;
      case 5:
        assert_try_allocate = exhaust = true;
        assert(i2s_receive(&device.dev, &audio[0], completion, NULL, 0) == -EAGAIN);
        exhaust = false;
        lock_busy = true;
        assert(i2s_receive(&device.dev, &audio[0], completion, NULL, 0) == -EBUSY);
        assert(starts == 0);
        assert_pool_released();
        break;
#ifdef RETRYABLE_RX_STOP
      case 6:
        rx_parked = false;
        assert(!esp32s3_dma_is_idle(2, false) && esp32s3_dma_is_idle(2, true));
        rx_parked = true;
        tx_parked = false;
        assert(esp32s3_dma_is_idle(2, false) && !esp32s3_dma_is_idle(2, true));
        break;
#endif
      default:
        abort();
    }

  return 0;
}

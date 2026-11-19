/* Execute the pinned RX submission functions with injected DMA and mutex results. */

#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>

#include "esp32s3_dma.h"

#define CONFIG_ARCH_CHIP_ESP32S3 1
#define CONFIG_ARCH_DCACHE 1
#define ESPRESSIF_DMA_BUFLEN_MAX ESP32S3_DMA_BUFLEN_MAX
#define I2S_DMADESC_NUM 2
#define DEBUGASSERT assert
#define OK 0
#define MIN(a, b) ((a) < (b) ? (a) : (b))
#define ALIGN_UP(n, a) (((n) + (a) - 1) & ~((a) - 1))
#define GDMA_CH_REG_ADDR(reg, channel) 0u
#define REG_GET_FIELD(addr, field) ((void)(addr), ESP32S3_DMA_EXT_MEMBLK_64B)
#define esp_dmadesc_s esp32s3_dmadesc_s
#define i2serr(...) ((void)0)
#define i2sinfo(...) ((void)0)

struct i2s_dev_s { int unused; };
struct ap_buffer_s { uint8_t *samp; unsigned int nmaxbytes, references; };
typedef void (*i2s_callback_t)(struct i2s_dev_s *, struct ap_buffer_s *, void *, int);
typedef struct { unsigned int entries; } sq_queue_t;
typedef struct esp_buffer_s sq_entry_t;
typedef unsigned int irqstate_t;

struct esp_buffer_s
{
  struct esp_dmadesc_s dma_link[I2S_DMADESC_NUM];
  struct ap_buffer_s *apb;
  void *arg;
  i2s_callback_t callback;
  uint32_t timeout, nbytes;
  int result;
};

struct esp_i2s_config_s { bool rx_en; };
struct esp_i2s_s
{
  struct i2s_dev_s dev;
  struct { sq_queue_t pend; } rx;
  struct esp_i2s_config_s *config;
  unsigned int data_width, channels, dma_channel;
  bool lock, slock;
};

static struct esp_buffer_s container;
static bool allocated;
static int lock_error;
static unsigned int dma_bytes, cache_cleans, starts;

static struct esp_buffer_s *i2s_buf_allocate(struct esp_i2s_s *priv)
{
  assert(!allocated);
  allocated = true;
  return &container;
}

static void i2s_buf_free(struct esp_i2s_s *priv, struct esp_buffer_s *buffer)
{
  assert(allocated && buffer == &container);
  allocated = false;
  buffer->apb = NULL;
}

static int nxmutex_lock(bool *lock)
{
  assert(!*lock);
  if (lock_error)
    {
      return lock_error;
    }

  *lock = true;
  return OK;
}

static void nxmutex_unlock(bool *lock)
{
  assert(*lock);
  *lock = false;
}

static irqstate_t spin_lock_irqsave(bool *lock)
{
  assert(!*lock);
  *lock = true;
  return 0;
}

static void spin_unlock_irqrestore(bool *lock, irqstate_t flags)
{
  assert(*lock);
  *lock = false;
}

static void apb_reference(struct ap_buffer_s *apb)
{
  apb->references++;
}

void apb_free(struct ap_buffer_s *apb)
{
  assert(apb->references == 2);
  apb->references--;
}

static unsigned int esp_dma_setup(struct esp_dmadesc_s *descriptors,
                                  unsigned int count, uint8_t *bytes,
                                  unsigned int length, bool tx,
                                  unsigned int channel)
{
  assert(descriptors == container.dma_link && count == I2S_DMADESC_NUM);
  assert(!tx && dma_bytes <= length);
  return dma_bytes;
}

static void up_clean_dcache(uintptr_t begin, uintptr_t end)
{
  assert(begin == (uintptr_t)container.dma_link &&
         end - begin == sizeof(container.dma_link));
  cache_cleans++;
}

static void sq_addlast(sq_entry_t *entry, sq_queue_t *queue)
{
  assert(entry == &container);
  queue->entries++;
}

static int i2s_rxdma_start(struct esp_i2s_s *priv)
{
  assert(priv->lock && priv->slock && priv->rx.pend.entries == 1);
  starts++;
  return OK;
}

static bool esp32s3_ptr_extram(const void *address)
{
  return true;
}

#include "i2s_rx.inc"

static void check_descriptor_end(bool full)
{
  uint8_t payload[4096];
  struct esp32s3_dmadesc_s descriptors[2] = {0};
  unsigned int size = full ? 2560 : 4088;

  /* The second element detects writes outside the declared one-entry list. */
  descriptors[1].next = &descriptors[1];
  assert(esp32s3_dma_setup(descriptors, 1, payload, size, false, 0) ==
         (full ? size : 4032));
  assert(descriptors[1].next == &descriptors[1]);
  assert(descriptors[0].next == NULL);
}

static void check_tx_descriptor_list(void)
{
  uint8_t payload[4096];
  struct esp32s3_dmadesc_s descriptors[2] = {0};

  assert(esp32s3_dma_setup(descriptors, 2, payload, sizeof(payload), true, 0)
         == sizeof(payload));
  assert(descriptors[0].next == &descriptors[1] && descriptors[1].next == NULL);
  assert((descriptors[0].ctrl & ESP32S3_DMA_CTRL_EOF) == 0);
  assert((descriptors[1].ctrl & ESP32S3_DMA_CTRL_EOF) != 0);
}

int main(int argc, char **argv)
{
  struct esp_i2s_config_s config = { .rx_en = true };
  struct esp_i2s_s priv = { .config = &config, .data_width = 32, .channels = 2 };
  uint8_t samples[2563];
  struct ap_buffer_s apb = { .samp = samples, .nmaxbytes = sizeof(samples),
                             .references = 1 };
  int expected;

  assert(argc == 2);
  switch (atoi(argv[1]))
    {
      case 0:
        apb.nmaxbytes = 7;
        expected = -EINVAL;
        break;
      case 1:
        priv.channels = 0;
        expected = -EINVAL;
        break;
      case 2:
        lock_error = -EINTR;
        expected = -EINTR;
        break;
      case 3:
        expected = -EIO;
        break;
      case 4:
        dma_bytes = 640;
        expected = -EIO;
        break;
      case 5:
        dma_bytes = 2560;
        expected = OK;
        break;
      case 6:
        check_descriptor_end(false);
        return 0;
      case 7:
        check_descriptor_end(true);
        return 0;
      case 8:
        check_tx_descriptor_list();
        return 0;
      default:
        abort();
    }

  assert(i2s_receive(&priv.dev, &apb, NULL, NULL, 0) == expected);
  assert(!priv.lock && !priv.slock);
  if (expected != OK)
    {
      assert(!allocated && apb.references == 1);
      assert(cache_cleans == 0 && starts == 0 && priv.rx.pend.entries == 0);
      lock_error = 0;
      apb.nmaxbytes = sizeof(samples);
      priv.channels = 2;
      dma_bytes = 2560;
      assert(i2s_receive(&priv.dev, &apb, NULL, NULL, 0) == OK);
    }

  assert(!priv.lock && !priv.slock);
  assert(allocated && container.nbytes == 2560 && apb.references == 2);
  assert(cache_cleans == 1 && starts == 1 && priv.rx.pend.entries == 1);
  return 0;
}

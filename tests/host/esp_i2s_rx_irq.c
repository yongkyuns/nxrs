/* Exercise the pinned ESP32-S3 I2S interrupt and channel functions. */

#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>

#include "dma_registers.h"

#define CONFIG_ARCH_CHIP_ESP32S3 1
#define I2S_HAVE_RX 1
#define I2S_HAVE_TX 1
#define GDMA_LL_EVENT_RX_SUC_EOF DMA_IN_SUC_EOF_CH0_INT_ST
#define GDMA_LL_EVENT_TX_TOTAL_EOF DMA_OUT_TOTAL_EOF_CH0_INT_ST
#define I2S_RX_CONF_REG(port) (port)
#define I2S_RX_RESET 1u
#define I2S_RX_FIFO_RESET 2u
#define I2S_RX_UPDATE 4u
#define ESP32S3_DMA_PERIPH_I2S0 0
#define ESP32S3_DMA_PERIPH_I2S1 1
#define ESP_IRQ_TRIGGER_LEVEL 0
#define ESP_SOURCE2IRQ(source) (source)
#define ASSERT assert
#define OK 0
#define ERROR (-1)
#define i2serr(...) ((void)0)
#define i2sinfo(...) ((void)0)
#define i2swarn(...) ((void)0)

struct esp_dmadesc_s { unsigned int id; };
struct esp_i2s_config_s { bool rx_en, tx_en; int port; void *ctx; };
struct esp_i2s_s
{
  struct esp_i2s_config_s *config;
  int dma_channel, cpu;
  int rx_irq, tx_irq;
  bool rx_started, slock;
};
typedef unsigned int irqstate_t;

static const int rx_irq = 2;
static const int tx_irq = 3;
static struct esp_dmadesc_s descriptors[2];
static struct esp_dmadesc_s *rx_descriptor = &descriptors[0];
static bool rx_masked, require_rx_lock, restart_before_read, setting_up;
static uint32_t rx_status, tx_status;
static unsigned int rx_reads, tx_reads, rx_events, tx_events;
static unsigned int cpu_changes, dma_stops, dma_resets, clears_after_mask;

static irqstate_t spin_lock_irqsave(bool *lock)
{
  assert(!*lock);
  *lock = true;

  /* Model restarting DMA before the interrupt acquires the RX lock. */

  if (restart_before_read)
    {
      rx_descriptor = &descriptors[1];
    }

  return 0;
}

static void spin_unlock_irqrestore(bool *lock, irqstate_t flags)
{
  assert(*lock && flags == 0);
  *lock = false;
}

static void up_enable_irq(int irq)
{
  assert(irq == rx_irq);
  assert(!setting_up || (rx_masked && rx_status == 0));
  cpu_changes++;
}

static void up_disable_irq(int irq)
{
  assert(irq == rx_irq);
  cpu_changes++;
}

static void i2s_hal_rx_stop(void *ctx) {}
static void modifyreg32(uint32_t address, uint32_t clear, uint32_t set) {}
static uint32_t getreg32(uint32_t address) { return 0; }

static void esp32s3_dma_reset_channel(int channel, bool tx)
{
  assert(!tx);
  dma_resets++;
}

static void esp32s3_dma_disable(int channel, bool tx)
{
  assert(!tx);
  dma_stops++;
}

static void esp32s3_dma_enable_interrupt(int channel, bool tx,
                                        uint32_t mask, bool enable)
{
  assert(!tx && mask == GDMA_LL_EVENT_RX_SUC_EOF);
  rx_masked = !enable;
}

static uint32_t esp32s3_dma_get_interrupt(int channel, bool tx)
{
  if (tx)
    {
      tx_reads++;
      return tx_status;
    }

  rx_reads++;
  return rx_masked ? 0 : rx_status;
}

static void esp32s3_dma_clear_interrupt(int channel, bool tx, uint32_t mask)
{
  if (tx)
    {
      tx_status &= ~mask;
    }
  else
    {
      rx_status &= ~mask;
      clears_after_mask += rx_masked && mask == GDMA_LL_EVENT_RX_SUC_EOF;
    }
}

static uintptr_t esp32s3_dma_get_desc_addr(int channel, bool tx)
{
  return (uintptr_t)(tx ? &descriptors[0] : rx_descriptor);
}

static void i2s_rx_schedule(struct esp_i2s_s *priv, struct esp_dmadesc_s *desc)
{
  assert(!require_rx_lock || priv->slock);
  assert(!restart_before_read || desc == &descriptors[1]);
  rx_events++;
}

static void i2s_tx_schedule(struct esp_i2s_s *priv, struct esp_dmadesc_s *desc)
{
  assert(desc == &descriptors[0]);
  tx_events++;
}

static struct
{
  struct { struct { int rx_irq_id, tx_irq_id; } pairs[1]; } groups[1];
} gdma_periph_signals = { .groups[0].pairs[0] = { 2, 3 } };

static void esp_dma_init(void) {}
static int esp_dma_request(int dev, int tx_prio, int rx_prio, bool burst) { return 0; }
static int this_cpu(void) { return 1; }

static int esp_setup_irq(int source, int priority, int type,
                         int (*handler)(int, void *, void *), void *arg)
{
  struct esp_i2s_s *priv = arg;
  assert(source == rx_irq && priv->cpu == this_cpu() && priv->slock);
  return 4;
}

#include "i2s_rx_irq.inc"

int main(int argc, char **argv)
{
  struct esp_i2s_config_s config = { .rx_en = true, .tx_en = true };
  struct esp_i2s_s priv = { .config = &config, .rx_irq = rx_irq,
                            .tx_irq = tx_irq };
  assert(argc == 2);
  switch (atoi(argv[1]))
    {
      case 0:
        /* The permanent route was enabled on CPU 0. A CPU 1 restart changes
         * the I2S and GDMA state while leaving that CPU route alone.
         */
        rx_masked = true;
        i2s_rx_channel_start(&priv);
        assert(priv.rx_started && dma_resets == 1);
        assert(cpu_changes == 0 && rx_masked);
        rx_masked = false;
        rx_status = GDMA_LL_EVENT_RX_SUC_EOF;
        i2s_rx_channel_stop(&priv);
        assert(!priv.rx_started && dma_stops == 1);
        assert(cpu_changes == 0 && rx_masked && rx_status == 0);
        assert(clears_after_mask == 1);
        break;
      case 1:
        priv.rx_started = true;
        rx_status = GDMA_LL_EVENT_RX_SUC_EOF;
        i2s_rx_channel_stop(&priv);
        assert(cpu_changes == 0 && rx_masked && dma_stops == 1);
        assert(clears_after_mask == 1 && rx_status == 0);
        break;
      case 2:
        rx_status = GDMA_LL_EVENT_RX_SUC_EOF;
        tx_status = GDMA_LL_EVENT_TX_TOTAL_EOF;
        assert(i2s_interrupt(tx_irq, NULL, &priv) == 0);
        assert(rx_reads == 0 && rx_events == 0);
        assert(rx_status == GDMA_LL_EVENT_RX_SUC_EOF);
        assert(tx_reads == 1 && tx_events == 1 && tx_status == 0);
        break;
      case 3:
        require_rx_lock = true;
        restart_before_read = true;
        rx_status = GDMA_LL_EVENT_RX_SUC_EOF;
        tx_status = GDMA_LL_EVENT_TX_TOTAL_EOF;
        assert(i2s_interrupt(rx_irq, NULL, &priv) == 0);
        assert(rx_reads == 1 && rx_events == 1 && rx_status == 0);
        assert(tx_reads == 0 && tx_events == 0);
        assert(tx_status == GDMA_LL_EVENT_TX_TOTAL_EOF);
        break;
      case 4:
        rx_status = GDMA_LL_EVENT_RX_SUC_EOF;
        assert(i2s_interrupt(rx_irq, NULL, &priv) == 0);
        assert(rx_events == 1 && rx_status == 0 && tx_events == 0);
        break;
      case 5:
        rx_masked = true;
        rx_status = GDMA_LL_EVENT_RX_SUC_EOF;
        assert(i2s_interrupt(rx_irq, NULL, &priv) == 0);
        assert(rx_events == 0 && tx_events == 0);
        break;
      case 6:
        config.tx_en = false;
        priv.rx_irq = 0;
        setting_up = true;
        rx_status = GDMA_LL_EVENT_RX_SUC_EOF;
        priv.slock = true;
        assert(i2s_dma_setup(&priv) == 0);
        priv.slock = false;
        assert(cpu_changes == 1 && priv.rx_irq == rx_irq && rx_masked);
        assert(rx_status == 0 && clears_after_mask == 1);
        break;
      default:
        abort();
    }

  assert(!priv.slock);
  return 0;
}

/* SPDX-License-Identifier: MIT */
/* Test-only observer. It never drives or remuxes a pin or retries a transfer.
 * The hooks run under the native gSPI lock. One separately claimed PIO block
 * samples the shared data pin once per clock; a separate DMA stores the words.
 * This sees digital pad inputs, not analogue integrity or controller internals.
 */
#include <nuttx/config.h>
#include <nuttx/arch.h>
#include <nuttx/irq.h>
#include <arch/barriers.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "arch/arm/src/rp23xx/rp23xx_cyw43439.h"
#include "arch/arm/src/rp23xx/rp23xx_dmac.h"
#include "arch/arm/src/rp23xx/rp23xx_pio.h"
#include "arch/arm/src/rp23xx/rp23xx_pio_instructions.h"

#ifndef CONFIG_RP23XX_CYW43439_READ_OBSERVER
#error "SPI capture requires the explicit diagnostic Kconfig option"
#endif

#define CAPTURE_WORDS (2048 / 4 + 2) /* Command, F1 delay, rounded data. */
#define CAPTURE_DRAIN_US 16

struct capture_counts_s
{
  uint32_t transfers;
  uint32_t unavailable;
  uint32_t incomplete;
  uint32_t command_bad;
  uint32_t data_bad;
  uint32_t pattern_ok;
  uint32_t pattern_wait;
  uint32_t pattern_bad;
  uint32_t driver_error;
};

static struct capture_counts_s g_counts;
#ifdef CONFIG_RP23XX_CYW43439_WRITE_OBSERVER
static struct capture_counts_s g_write_counts;
#endif
static uint32_t g_words[CAPTURE_WORDS];
static DMA_HANDLE g_dma;
static uint32_t g_dma_irqmask;
static unsigned int g_pio;
static unsigned int g_sm;
static unsigned int g_offset;
static unsigned int g_expected;
static unsigned int g_data_offset;
static unsigned int g_length;
static uint32_t g_command;
static uint32_t g_pattern;
static int g_initialized;
static int g_armed;
static int g_test_pattern;

static void capture_count(uint32_t *counter)
{
  irqstate_t flags = enter_critical_section();
  (*counter)++;
  leave_critical_section(flags);
}

/* Compare DMA byte order without casting a possibly partial data extent.
 * PIO shifts left; the native receive DMA byte-swaps every complete word.
 * Only requested bytes are compared, never caller-buffer padding.
 */
static int capture_matches(const uint32_t *wire, const uint8_t *dma,
                           unsigned int length)
{
  for (unsigned int i = 0; i < length; i++)
    {
      if ((uint8_t)(wire[i / 4] >> (24 - 8 * (i % 4))) != dma[i])
        {
          return 0;
        }
    }

  return 1;
}

static int capture_init(const rp23xx_gspi_t *io)
{
  uint16_t instructions[] =
    {
      pio_encode_wait_gpio(true, io->gpio_clock),
      pio_encode_in(pio_pins, 1),
      pio_encode_wait_gpio(false, io->gpio_clock),
    };
  rp23xx_pio_program_t program =
    {
      .instructions = instructions,
      .length = 3,
      .origin = -1,
    };
  rp23xx_pio_sm_config config = rp23xx_pio_get_default_sm_config();
  int sm = -1;
  irqstate_t flags;

  /* Use another block so synchronizer configuration cannot affect the driver.
   * Claim resources normally; do not assume PIO0/SM0 is the active bus.
   */

  flags = enter_critical_section();
  for (g_pio = 0; g_pio < RP23XX_PIO_NUM; g_pio++)
    {
      if (g_pio == io->pio || !rp23xx_pio_can_add_program(g_pio, &program))
        {
          continue;
        }

      sm = rp23xx_pio_claim_unused_sm(g_pio, false);
      if (sm >= 0)
        {
          g_sm = sm;
          g_offset = rp23xx_pio_add_program(g_pio, &program);
          break;
        }
    }

  leave_critical_section(flags);
  if (sm < 0)
    {
      return 0;
    }

  /* GPIO input enable is already established by bus initialization. No GPIO
   * init, mux, pulls, output-direction, clock-divider or bus changes here.
   */

  rp23xx_sm_config_set_in_pins(&config, io->gpio_data);
  rp23xx_sm_config_set_in_shift(&config, false, true, 32);
  rp23xx_sm_config_set_fifo_join(&config, RP23XX_PIO_FIFO_JOIN_RX);
  rp23xx_sm_config_set_wrap(&config, g_offset, g_offset + 2);
  rp23xx_pio_sm_init(g_pio, g_sm, g_offset, &config);
  rp23xx_pio_set_input_sync_bypass(g_pio, io->gpio_data, true);
  rp23xx_pio_set_input_sync_bypass(g_pio, io->gpio_clock, true);
  g_dma = rp23xx_dmachannel();
  if (g_dma != NULL)
    {
      unsigned int channel = (rp23xx_dma_register(g_dma, 0) - RP23XX_DMA_CH(0)) /
                              (RP23XX_DMA_CH(1) - RP23XX_DMA_CH(0));
      g_dma_irqmask = UINT32_C(1) << channel;
      /* Poll this owned channel. The shared IRQ handler otherwise clears its
       * DMA error flags before this observer can inspect them.
       */
      clrbits_reg32(g_dma_irqmask, RP23XX_DMA_INTE0);
    }

  return g_dma != NULL;
}

static void capture_begin(const rp23xx_gspi_t *io, unsigned int function,
                          uint32_t address, uint16_t length,
                          uint32_t wire_command, bool write,
                          struct capture_counts_s *counts)
{
  dma_config_t config;

  g_armed = 0;
  capture_count(&counts->transfers);
  if (!g_initialized)
    {
      /* Remember a failed allocation rather than consuming further resources. */
      g_initialized = capture_init(io) ? 1 : -1;
    }

  if (g_initialized < 0 || length == 0 || length > 2048)
    {
      capture_count(&counts->unavailable);
      return;
    }

  g_data_offset = !write && function == gspi_f1_backplane ? 2 : 1;
  g_length = length;
  g_expected = g_data_offset + (length + 3) / 4;
  g_command = wire_command;
  g_test_pattern = !write && (function == gspi_f0_bus ||
                    function == gspi_f0_bus_rev16) &&
                   address == 0x14 && length == 4;
  g_pattern = function == gspi_f0_bus_rev16 ? 0xedfeadbe : 0xfeedbead;
  memset(g_words, 0, g_expected * sizeof(g_words[0]));

  rp23xx_pio_sm_set_enabled(g_pio, g_sm, false);
  rp23xx_pio_sm_clear_fifos(g_pio, g_sm);
  rp23xx_pio_sm_restart(g_pio, g_sm);
  rp23xx_pio_sm_exec(g_pio, g_sm, pio_encode_jmp(g_offset));
  putreg32((UINT32_C(1) << (RP23XX_PIO_FDEBUG_RXSTALL_SHIFT + g_sm)) |
           (UINT32_C(1) << (RP23XX_PIO_FDEBUG_RXUNDER_SHIFT + g_sm)),
           RP23XX_PIO_FDEBUG(g_pio));
  config.dreq = rp23xx_pio_get_dreq(g_pio, g_sm, false);
  config.size = RP23XX_DMA_SIZE_WORD;
  config.noincr = 0;
  rp23xx_rxdmasetup(g_dma, RP23XX_PIO_RXF(g_pio, g_sm),
                    (uintptr_t)g_words, g_expected * sizeof(g_words[0]), config);
  rp23xx_dmastart(g_dma, NULL, NULL);
  UP_DMB();
  rp23xx_pio_sm_set_enabled(g_pio, g_sm, true);
  g_armed = 1;
}

static void capture_end(const uint32_t *buffer, int result,
                        struct capture_counts_s *counts)
{
  uint32_t remaining;
  uint32_t errors;
  uint32_t stalled;
  unsigned int wait;

  if (!g_armed)
    {
      return;
    }

  /* A native error may stop the clock early. Never wait without a bound. */

  for (wait = 0; wait < CAPTURE_DRAIN_US; wait++)
    {
      if (getreg32(rp23xx_dma_register(g_dma,
                       RP23XX_DMA_TRANS_COUNT_OFFSET)) == 0)
        {
          break;
        }

      up_udelay(1);
    }

  rp23xx_pio_sm_set_enabled(g_pio, g_sm, false);
  remaining = getreg32(rp23xx_dma_register(g_dma,
                         RP23XX_DMA_TRANS_COUNT_OFFSET));
  errors = getreg32(rp23xx_dma_register(g_dma, RP23XX_DMA_CTRL_TRIG_OFFSET)) &
             (RP23XX_DMA_CTRL_TRIG_READ_ERROR | RP23XX_DMA_CTRL_TRIG_WRITE_ERROR);
  stalled = getreg32(RP23XX_PIO_FDEBUG(g_pio)) &
              ((UINT32_C(1) << (RP23XX_PIO_FDEBUG_RXSTALL_SHIFT + g_sm)) |
               (UINT32_C(1) << (RP23XX_PIO_FDEBUG_RXUNDER_SHIFT + g_sm)));
  putreg32(0, rp23xx_dma_register(g_dma, RP23XX_DMA_CTRL_TRIG_OFFSET));
  rp23xx_dmastop(g_dma);
  putreg32(g_dma_irqmask, RP23XX_DMA_INTS0);
  UP_DMB();
  g_armed = 0;
  if (result != 0)
    {
      capture_count(&counts->driver_error);
    }
  if (remaining != 0 || errors != 0 || stalled != 0)
    {
      capture_count(&counts->incomplete);
      return;
    }

  if (g_words[0] != g_command)
    {
      capture_count(&counts->command_bad);
      return; /* Without frame alignment, data differences are not evidence. */
    }

  if (result != 0)
    {
      return;
    }

  if (!capture_matches(g_words + g_data_offset,
                        (const uint8_t *)buffer, g_length))
    {
      capture_count(&counts->data_bad);
    }
  if (g_test_pattern)
    {
      int matches = __builtin_bswap32(g_words[g_data_offset]) == g_pattern &&
                    buffer[0] == g_pattern;
      if (matches)
        {
          capture_count(&counts->pattern_ok);
        }
      else if (counts->pattern_ok == 0 &&
               capture_matches(g_words + g_data_offset,
                                (const uint8_t *)buffer, g_length))
        {
          /* The native driver polls this register until the chip is ready.
           * A matching capture of a pre-ready value is not corruption. A
           * later ready-pattern match is still required for calibration.
           */
          capture_count(&counts->pattern_wait);
        }
      else
        {
          capture_count(&counts->pattern_bad);
        }
    }
}

void rp23xx_cyw_read_begin(const rp23xx_gspi_t *io, unsigned int function,
                           uint32_t address, uint16_t length,
                           uint32_t wire_command)
{
  capture_begin(io, function, address, length, wire_command, false, &g_counts);
}

void rp23xx_cyw_read_end(const uint32_t *buffer, int result)
{
  capture_end(buffer, result, &g_counts);
}

#ifdef CONFIG_RP23XX_CYW43439_WRITE_OBSERVER
void rp23xx_cyw_write_begin(const rp23xx_gspi_t *io, uint16_t length,
                            uint32_t wire_command)
{
  /* Writes have neither an F1 response delay nor a read-ready pattern. */

  capture_begin(io, 0, 0, length, wire_command, true, &g_write_counts);
}

void rp23xx_cyw_write_end(const uint32_t *buffer, int result)
{
  /* The native bus lock keeps the caller's source live through this hook.
   * Compare requested bytes only: DMA also transmits word-rounded padding,
   * which is outside the command's byte count and is not logical payload.
   */

  capture_end(buffer, result, &g_write_counts);
}
#endif

void nxrs_radio_spi_capture_report(void)
{
  struct capture_counts_s counts;
  irqstate_t flags = enter_critical_section();
  counts = g_counts;
  leave_critical_section(flags);

  /* Counters only. No command words, addresses or sampled data leave RAM. */

  printf("NXRS_SPI_CAPTURE reads=%lu unavailable=%lu incomplete=%lu "
         "command_bad=%lu data_bad=%lu pattern_ok=%lu pattern_wait=%lu pattern_bad=%lu "
         "driver_error=%lu\n", (unsigned long)counts.transfers,
         (unsigned long)counts.unavailable, (unsigned long)counts.incomplete,
         (unsigned long)counts.command_bad, (unsigned long)counts.data_bad,
         (unsigned long)counts.pattern_ok, (unsigned long)counts.pattern_wait,
         (unsigned long)counts.pattern_bad,
         (unsigned long)counts.driver_error);
#ifdef CONFIG_RP23XX_CYW43439_WRITE_OBSERVER
  flags = enter_critical_section();
  counts = g_write_counts;
  leave_critical_section(flags);
  printf("NXRS_SPI_WRITE_CAPTURE writes=%lu unavailable=%lu incomplete=%lu "
         "command_bad=%lu data_bad=%lu driver_error=%lu\n",
         (unsigned long)counts.transfers, (unsigned long)counts.unavailable,
         (unsigned long)counts.incomplete, (unsigned long)counts.command_bad,
         (unsigned long)counts.data_bad, (unsigned long)counts.driver_error);
#endif
}

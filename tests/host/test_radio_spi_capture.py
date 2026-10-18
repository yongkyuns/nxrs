"""Native, sanitized coverage of the test-only SPI capture decisions."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "spi_capture_extract", ROOT / "tests/nuttx-std/test-patches.py"
)
extract = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(extract)


HARNESS = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>

#define CAPTURE_DRAIN_US 16
#define RP23XX_DMA_TRANS_COUNT_OFFSET 4
#define RP23XX_DMA_CTRL_TRIG_OFFSET 8
#define RP23XX_DMA_INTS0 12
#define RP23XX_DMA_CTRL_TRIG_READ_ERROR (1u << 1)
#define RP23XX_DMA_CTRL_TRIG_WRITE_ERROR (1u << 2)
#define RP23XX_PIO_FDEBUG_RXSTALL_SHIFT 8
#define RP23XX_PIO_FDEBUG_RXUNDER_SHIFT 16
#define RP23XX_PIO_FDEBUG(pio) (0x100u + (pio))
#define UP_DMB() ((void)0)

typedef void *DMA_HANDLE;
typedef unsigned int irqstate_t;
static irqstate_t enter_critical_section(void) { return 0; }
static void leave_critical_section(irqstate_t flags) { assert(flags == 0); }

struct capture_counts_s {
  uint32_t transfers, unavailable, incomplete, command_bad, data_bad;
  uint32_t pattern_ok, pattern_wait, pattern_bad, driver_error;
};

static struct capture_counts_s g_counts;
static struct capture_counts_s g_write_counts;
static uint32_t g_words[514];
static DMA_HANDLE g_dma;
static uint32_t g_dma_irqmask = 1u << 3;
static unsigned int g_pio, g_sm, g_data_offset, g_length;
static uint32_t g_command, g_pattern;
static int g_armed, g_test_pattern;

static uint32_t mock_remaining, mock_dma_errors, mock_stalled;
static unsigned int mock_count_reads, mock_delays, mock_stops, mock_disables;
static unsigned int mock_clear_writes;

static uintptr_t rp23xx_dma_register(DMA_HANDLE dma, unsigned int offset)
{
  (void)dma;
  return offset;
}

static uint32_t getreg32(uintptr_t address)
{
  if (address == RP23XX_DMA_TRANS_COUNT_OFFSET)
    {
      mock_count_reads++;
      return mock_remaining;
    }

  if (address == RP23XX_DMA_CTRL_TRIG_OFFSET)
    {
      return mock_dma_errors;
    }

  if (address == RP23XX_PIO_FDEBUG(g_pio))
    {
      return mock_stalled;
    }

  assert(!"unexpected register read");
  return 0;
}

static void putreg32(uint32_t value, uintptr_t address)
{
  if (address == RP23XX_DMA_INTS0)
    {
      assert(value == g_dma_irqmask);
      return;
    }
  assert(address == RP23XX_DMA_CTRL_TRIG_OFFSET);
  assert(value == 0);
  mock_clear_writes++;
}

static void rp23xx_pio_sm_set_enabled(unsigned int pio, unsigned int sm,
                                      int enabled)
{
  (void)pio;
  (void)sm;
  if (!enabled)
    {
      mock_disables++;
    }
}

static void rp23xx_dmastop(DMA_HANDLE dma)
{
  (void)dma;
  mock_stops++;
}

static void up_udelay(unsigned int usec)
{
  assert(usec == 1);
  mock_delays++;
}

/* Extracted production function bodies follow these native hardware stubs. */
CAPTURE_COUNT

CAPTURE_MATCHES

CAPTURE_READ_END

static void reset_end(void)
{
  memset(&g_counts, 0, sizeof(g_counts));
  memset(&g_write_counts, 0, sizeof(g_write_counts));
  memset(g_words, 0, sizeof(g_words));
  g_dma = (DMA_HANDLE)(uintptr_t)1;
  g_pio = 2;
  g_sm = 1;
  g_data_offset = 1;
  g_length = 5;
  g_command = 0x12345678;
  g_pattern = 0xfeedbead;
  g_armed = 1;
  g_test_pattern = 0;
  mock_remaining = mock_dma_errors = mock_stalled = 0;
  mock_count_reads = mock_delays = mock_stops = mock_disables = 0;
  mock_clear_writes = 0;
}

static void wire_from_buffer(uint32_t *wire, const uint8_t *bytes,
                             unsigned int length)
{
  memset(wire, 0xa5, ((length + 3) / 4) * sizeof(*wire));
  for (unsigned int i = 0; i < length; i++)
    {
      unsigned int shift = 24 - 8 * (i % 4);
      uint32_t mask = UINT32_C(0xff) << shift;
      wire[i / 4] = (wire[i / 4] & ~mask) | ((uint32_t)bytes[i] << shift);
    }
}

static void prepare_good_read(uint32_t *buffer)
{
  g_words[0] = g_command;
  wire_from_buffer(g_words + g_data_offset,
                   (const uint8_t *)buffer, g_length);
}

static void test_capture_matches(void)
{
  static const unsigned int lengths[] = {1, 3, 4, 5, 2048};
  uint32_t wire[512];
  uint8_t dma[2048];

  for (unsigned int n = 0; n < sizeof(lengths) / sizeof(lengths[0]); n++)
    {
      unsigned int length = lengths[n];
      memset(wire, 0xdeadbeef, sizeof(wire));
      for (unsigned int i = 0; i < length; i++)
        {
          uint8_t byte = (uint8_t)(i * 37u + 11u);
          unsigned int shift = 24 - 8 * (i % 4);
          uint32_t mask = UINT32_C(0xff) << shift;
          wire[i / 4] = (wire[i / 4] & ~mask) | ((uint32_t)byte << shift);
          dma[i] = byte;
        }

      assert(capture_matches(wire, dma, length));

      /* Distinct unrequested bytes must not turn a partial read into a mismatch. */
      if (length % 4 != 0)
        {
          unsigned int shift = 24 - 8 * (length % 4);
          uint8_t padding = (uint8_t)(wire[length / 4] >> shift);
          dma[length] = padding ^ 0xff;
        }
      assert(capture_matches(wire, dma, length));

      dma[length - 1] ^= 1;
      assert(!capture_matches(wire, dma, length));
    }
}

static void test_read_end(void)
{
  uint32_t buffer[512];
  uint8_t *bytes = (uint8_t *)buffer;
  memset(buffer, 0, sizeof(buffer));
  for (unsigned int i = 0; i < 2048; i++)
    {
      bytes[i] = (uint8_t)(i * 13u + 7u);
    }

  reset_end();
  prepare_good_read(buffer);
  rp23xx_cyw_read_end(buffer, 0);
  assert(g_counts.data_bad == 0 && g_counts.command_bad == 0);
  assert(g_counts.incomplete == 0 && mock_count_reads == 2);
  assert(mock_disables == 1 && mock_stops == 1 && mock_clear_writes == 1);

  reset_end();
  g_data_offset = 2; /* F1's leading response-delay word is not payload. */
  g_words[1] = 0xdeadbeef;
  prepare_good_read(buffer);
  rp23xx_cyw_read_end(buffer, 0);
  assert(g_counts.data_bad == 0 && g_counts.command_bad == 0);

  reset_end();
  prepare_good_read(buffer);
  g_words[0] ^= 1;
  memset(bytes, 0xff, g_length);
  rp23xx_cyw_read_end(buffer, 0);
  assert(g_counts.command_bad == 1 && g_counts.data_bad == 0);

  reset_end();
  prepare_good_read(buffer);
  mock_remaining = 3;
  rp23xx_cyw_read_end(buffer, 0);
  assert(g_counts.incomplete == 1 && mock_delays == CAPTURE_DRAIN_US);
  assert(mock_count_reads == CAPTURE_DRAIN_US + 1);
  assert(g_counts.command_bad == 0 && g_counts.data_bad == 0);

  reset_end();
  prepare_good_read(buffer);
  mock_dma_errors = RP23XX_DMA_CTRL_TRIG_READ_ERROR;
  rp23xx_cyw_read_end(buffer, 0);
  assert(g_counts.incomplete == 1 && g_counts.data_bad == 0);

  reset_end();
  prepare_good_read(buffer);
  mock_dma_errors = RP23XX_DMA_CTRL_TRIG_WRITE_ERROR;
  rp23xx_cyw_read_end(buffer, 0);
  assert(g_counts.incomplete == 1 && g_counts.data_bad == 0);

  reset_end();
  prepare_good_read(buffer);
  mock_stalled = UINT32_C(1) << (RP23XX_PIO_FDEBUG_RXSTALL_SHIFT + g_sm);
  rp23xx_cyw_read_end(buffer, 0);
  assert(g_counts.incomplete == 1 && g_counts.data_bad == 0);

  reset_end();
  prepare_good_read(buffer);
  mock_stalled = UINT32_C(1) << (RP23XX_PIO_FDEBUG_RXUNDER_SHIFT + g_sm);
  rp23xx_cyw_read_end(buffer, 0);
  assert(g_counts.incomplete == 1 && g_counts.data_bad == 0);

  reset_end();
  prepare_good_read(buffer);
  memset(bytes, 0xff, g_length);
  rp23xx_cyw_read_end(buffer, -5);
  assert(g_counts.driver_error == 1 && g_counts.data_bad == 0);
  assert(g_counts.incomplete == 0 && g_counts.command_bad == 0);

  reset_end();
  g_data_offset = 1;
  g_length = 4;
  g_test_pattern = 1;
  g_pattern = 0xfeedbead;
  buffer[0] = g_pattern;
  g_words[0] = g_command;
  g_words[1] = __builtin_bswap32(g_pattern);
  rp23xx_cyw_read_end(buffer, 0);
  assert(g_counts.pattern_ok == 1 && g_counts.pattern_bad == 0);

  reset_end();
  g_data_offset = 1;
  g_length = 4;
  g_test_pattern = 1;
  g_pattern = 0xfeedbead;
  buffer[0] = g_pattern ^ 1;
  g_words[0] = g_command;
  g_words[1] = __builtin_bswap32(g_pattern);
  rp23xx_cyw_read_end(buffer, 0);
  assert(g_counts.pattern_ok == 0 && g_counts.pattern_bad == 1);

  reset_end();
  g_length = 4;
  g_test_pattern = 1;
  buffer[0] = g_pattern ^ 1;
  prepare_good_read(buffer);
  rp23xx_cyw_read_end(buffer, 0);
  assert(g_counts.data_bad == 0 && g_counts.pattern_wait == 1);
  assert(g_counts.pattern_ok == 0 && g_counts.pattern_bad == 0);
  /* A bad value after readiness is not a startup wait. */
  g_counts.pattern_ok = 1;
  g_armed = 1;
  rp23xx_cyw_read_end(buffer, 0);
  assert(g_counts.pattern_bad == 1 && g_counts.pattern_wait == 1);

  reset_end();
  g_length = 4;
  g_test_pattern = 1;
  g_pattern = 0xedfeadbe; /* Native pre-setup REV16 mode. */
  buffer[0] = g_pattern;
  prepare_good_read(buffer);
  rp23xx_cyw_read_end(buffer, 0);
  assert(g_counts.pattern_ok == 1 && g_counts.pattern_bad == 0);

  reset_end();
  g_armed = 0;
  rp23xx_cyw_read_end(buffer, 0);
  assert(mock_count_reads == 0 && mock_stops == 0 && mock_clear_writes == 0);
}

static void test_write_end(void)
{
  uint32_t buffer[512];
  static const unsigned int lengths[] = {1, 3, 4, 5, 2048};
  for (unsigned int i = 0; i < sizeof(buffer); i++)
    {
      ((uint8_t *)buffer)[i] = (uint8_t)(i * 19u + 3u);
    }
  for (unsigned int n = 0; n < sizeof(lengths) / sizeof(lengths[0]); n++)
    {
      reset_end();
      g_length = lengths[n];
      prepare_good_read(buffer);
      rp23xx_cyw_write_end(buffer, 0);
      assert(g_write_counts.data_bad == 0 && g_write_counts.incomplete == 0);
      assert(g_write_counts.command_bad == 0 && mock_stops == 1);
      assert(g_counts.data_bad == 0 && g_counts.incomplete == 0);
      g_armed = 1;
      g_words[g_data_offset] ^= UINT32_C(1) << 24;
      rp23xx_cyw_write_end(buffer, 0);
      assert(g_write_counts.data_bad == 1 && g_counts.data_bad == 0);
    }
  reset_end();
  prepare_good_read(buffer);
  g_words[0] ^= 1;
  rp23xx_cyw_write_end(buffer, 0);
  assert(g_write_counts.command_bad == 1 && g_write_counts.data_bad == 0);
  reset_end();
  prepare_good_read(buffer);
  mock_remaining = 1;
  rp23xx_cyw_write_end(buffer, 0);
  assert(g_write_counts.incomplete == 1 && mock_delays == CAPTURE_DRAIN_US);
  reset_end();
  prepare_good_read(buffer);
  rp23xx_cyw_write_end(buffer, -5);
  assert(g_write_counts.driver_error == 1 && g_counts.driver_error == 0);
}

int main(void)
{
  test_capture_matches();
  test_read_end();
  test_write_end();
  return 0;
}
'''


class RadioSpiCaptureTests(unittest.TestCase):
    def test_extracted_capture_functions_under_sanitizers(self):
        source = (ROOT / "tests/nuttx-std/radio/ffi/spi_capture.c").read_text()
        matches = extract.function_definition(source, "static int capture_matches(")
        read_end = "\n".join(extract.function_definition(source, signature) for signature in (
            "static void capture_end(", "void rp23xx_cyw_read_end(",
            "void rp23xx_cyw_write_end(",
        ))
        count = extract.function_definition(source, "static void capture_count(")
        program_text = HARNESS.replace("CAPTURE_COUNT", count).replace("CAPTURE_MATCHES", matches).replace(
            "CAPTURE_READ_END", read_end
        )

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            program = directory / "spi_capture.c"
            program.write_text(program_text)
            binary = directory / "spi_capture"
            subprocess.run(
                ["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                 "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                 str(program), "-o", str(binary)],
                check=True, timeout=20,
            )
            subprocess.run([str(binary)], check=True, timeout=10)


if __name__ == "__main__":
    unittest.main()

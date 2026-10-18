/* Exercise the native counter setup and the actual two-instruction RX loop.
 * This models PIO instruction count, not physical timing or DMA delivery.
 */
#include <assert.h>
#include <stdint.h>

enum { gspi_f1_backplane = 1 };
static uint32_t observed_y;
struct io_s { unsigned pio, pio_sm; };
static struct io_s io;
static struct io_s *rp_io = &io;

static void rp23xx_pio_sm_put(unsigned pio, unsigned sm, uint32_t value)
{
  assert(pio == 0 && sm == 0);
  observed_y = value;
}

static uint32_t configure(unsigned function, unsigned length)
{
  uint32_t bit_length;
#include "cyw43439-pio-count-setup.h"
  return observed_y;
}

#include "cyw43439-pio-program.h"

static uint32_t samples(uint32_t y)
{
  uint32_t count = 0;

  /* IN samples one bit, then JMP Y-- tests the pre-decrement value.
   * Guard the opcodes so a different native program needs a fresh review.
   */
  assert(cyw_program_instructions[4] == 0x4001);
  assert(cyw_program_instructions[5] == 0x1084);
  for (;;)
    {
      count++;
      if (y-- == 0)
        {
          return count;
        }
    }
}

int main(void)
{
  static const unsigned lengths[] = {1, 2, 3, 4, 5, 7, 8, 13, 64, 255, 511, 2048};
  for (unsigned function = 0; function < 3; function++)
    {
      for (unsigned index = 0; index < sizeof(lengths) / sizeof(lengths[0]); index++)
        {
          unsigned length = lengths[index];
          uint32_t expected = 32 * ((length + 3) / 4);
          if (function == gspi_f1_backplane)
            {
              expected += 32;
            }
#ifdef EXPECT_BASELINE_BUG
          assert(samples(configure(function, length)) == expected + 1);
#else
          assert(samples(configure(function, length)) == expected);
#endif
        }
    }
  return 0;
}

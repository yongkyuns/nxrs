/* Host witness for the extracted rp23xx_pio_get_dreq() and real DREQ defs.
 * The test driver supplies rp23xx-pio-dreq.h from the pinned NuttX sources.
 */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>

static void check_pio_param_mock(uint32_t pio)
{
  assert(pio < 3);
}

static void check_sm_param_mock(uint32_t sm)
{
  assert(sm < 4);
}

#define check_pio_param(pio) check_pio_param_mock(pio)
#define check_sm_param(sm) check_sm_param_mock(sm)
#include "rp23xx-pio-dreq.h"

int main(void)
{
  static const uint32_t base[] = {
    RP23XX_DMA_DREQ_PIO0_TX0,
    RP23XX_DMA_DREQ_PIO1_TX0,
    RP23XX_DMA_DREQ_PIO2_TX0
  };

#ifdef EXPECT_BASELINE_BUG
  for (uint32_t sm = 0; sm < RP23XX_PIO_SM_NUM; sm++)
    {
      for (uint32_t tx = 0; tx < 2; tx++)
        {
          uint32_t offset = sm + (tx ? 0 : RP23XX_PIO_SM_NUM);
          assert(rp23xx_pio_get_dreq(1, sm, tx) == base[2] + offset);
        }
    }
#else
  for (uint32_t pio = 0; pio < 3; pio++)
    {
      for (uint32_t sm = 0; sm < RP23XX_PIO_SM_NUM; sm++)
        {
          for (uint32_t tx = 0; tx < 2; tx++)
            {
              uint32_t offset = sm + (tx ? 0 : RP23XX_PIO_SM_NUM);
              assert(rp23xx_pio_get_dreq(pio, sm, tx) == base[pio] + offset);
            }
        }
    }
#endif
  return 0;
}

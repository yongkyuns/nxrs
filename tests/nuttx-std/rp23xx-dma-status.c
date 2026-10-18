/*
 * Host witness for the RP23xx DMA interrupt-status writes in
 * rp23xx_dmachannel() and rp23xx_dmafree().
 *
 * test-patches.py supplies rp23xx-dma-definitions.h from the real DMA
 * headers and rp23xx-dma-functions.h from those two real driver functions.
 * Include the actual RP23xx atomic-alias helpers: they are not software
 * read/modify/write. Only normal INTS0 writes model acknowledgement; alias
 * writes are recorded without inventing their hardware effect. Baseline
 * checks identify those addresses, not an unproven RP2350 failure.
 */

#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>

#include "rp23xx-dma-definitions.h"

#define OK 0
#define DEBUGASSERT(condition) assert(condition)
#define dmainfo(...) ((void)0)

struct dma_channel_s
{
  uint8_t chan;
  bool inuse;
  dma_callback_t callback;
  void *arg;
};

struct dma_controller_s
{
  int lock;
  int chansem;
};

static struct dma_controller_s g_dmac = {
  .chansem = RP23XX_DMA_NCHANNELS
};
static struct dma_channel_s g_dmach[RP23XX_DMA_NCHANNELS];

static uint32_t status0;
static uint32_t inte0;
static uint32_t status_write_address;
static uint32_t status_write_mask;

static uint32_t getreg32(uint32_t address)
{
  if (address == RP23XX_DMA_INTS0)
    {
      return status0 & inte0;
    }

  if (address == RP23XX_DMA_INTE0)
    {
      return inte0;
    }

  return 0;
}

static void putreg32(uint32_t value, uint32_t address)
{
  uint32_t alias = address & 0x3000;
  uint32_t base = address & ~0x3000u;
  if (base == RP23XX_DMA_INTS0)
    {
      status_write_address = address;
      status_write_mask = value;
      if (alias == 0)
        {
          status0 &= ~value; /* Documented normal write-one-to-clear. */
        }
    }
  else if (base == RP23XX_DMA_INTE0)
    {
      if (alias == RP23XX_ATOMIC_SET_REG_OFFSET) inte0 |= value;
      else if (alias == RP23XX_ATOMIC_CLR_REG_OFFSET) inte0 &= ~value;
      else { assert(alias == 0); inte0 = value; }
    }
}

static int nxsem_wait_uninterruptible(int *sem)
{
  assert(*sem > 0);
  (*sem)--;
  return 0;
}

static int nxsem_post(int *sem)
{
  (*sem)++;
  return 0;
}

static int nxmutex_lock(int *mutex)
{
  (void)mutex;
  return 0;
}

static int nxmutex_unlock(int *mutex)
{
  (void)mutex;
  return 0;
}

#include "rp23xx-dma-functions.h"

static void reset_state(void)
{
  unsigned int ch;

  status0 = 0;
  inte0 = 0;
  status_write_address = status_write_mask = 0;
  g_dmac.chansem = RP23XX_DMA_NCHANNELS;
  for (ch = 0; ch < RP23XX_DMA_NCHANNELS; ch++)
    {
      g_dmach[ch].chan = ch;
      g_dmach[ch].inuse = false;
      g_dmach[ch].callback = NULL;
      g_dmach[ch].arg = NULL;
    }
}

static int test_allocation_preserves_another_channel_completion(void)
{
  const uint32_t other_completed = 1u << 1;
  DMA_HANDLE handle;

  reset_state();
  g_dmach[1].inuse = true;
  g_dmac.chansem--;
  inte0 = other_completed;
  status0 = other_completed | 1;
  handle = rp23xx_dmachannel();

  assert(handle == (DMA_HANDLE)&g_dmach[0]);
  assert(g_dmach[0].inuse);
#ifdef EXPECT_BASELINE_ALIAS
  assert(status_write_address == (RP23XX_DMA_INTS0 | RP23XX_ATOMIC_SET_REG_OFFSET));
#else
  assert(status_write_address == RP23XX_DMA_INTS0);
  assert(status0 == other_completed); /* Clear only the allocated channel. */
#endif
  assert(status_write_mask == 1);
  assert(getreg32(RP23XX_DMA_INTE0) == (1 | other_completed));
  return 0;
}

static int test_release_clears_own_status_only(void)
{
  const uint32_t released = 1u << 2;
  const uint32_t other_completed = 1u << 3;

  reset_state();
  g_dmach[2].inuse = true;
  g_dmach[3].inuse = true;
  g_dmac.chansem -= 2;
  inte0 = released | other_completed;
  status0 = released | other_completed;
  rp23xx_dmafree((DMA_HANDLE)&g_dmach[2]);

  assert(!g_dmach[2].inuse);
#ifdef EXPECT_BASELINE_ALIAS
  assert(status_write_address == (RP23XX_DMA_INTS0 | RP23XX_ATOMIC_CLR_REG_OFFSET));
#else
  assert(status_write_address == RP23XX_DMA_INTS0);
  assert(status0 == other_completed); /* Clear the released channel only. */
#endif
  assert(status_write_mask == released);
  assert(getreg32(RP23XX_DMA_INTE0) == other_completed);
  assert(g_dmac.chansem == RP23XX_DMA_NCHANNELS - 1);
  return 0;
}

int main(void)
{
  assert(test_allocation_preserves_another_channel_completion() == 0);
  assert(test_release_clears_own_status_only() == 0);
  puts("RP23xx DMA interrupt-status witness: PASS");
  return 0;
}

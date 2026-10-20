/*
 * Host witness for CYW43439 DMA lifetime and error handling.
 *
 * The extraction driver supplies these headers from the real NuttX sources:
 *
 *   cyw43439-dma-types.h       dma_info_t, extracted from rp23xx_cyw43439.c
 *   cyw43439-dma-functions.h   dma_complete(), my_read(), and my_write(),
 *                              extracted verbatim from that driver
 *   cyw43439-dma-wait.h        nxsem_wait_uninterruptible(), extracted
 *                              verbatim from libs/libc/semaphore/sem_wait.c
 *   cyw43439-dma-end.h         real IRQ_QUIET and chain terminator macros
 *
 * The types header is included after the host sem_t declaration.  The
 * functions header is included after platform types, constants, and mock
 * declarations.  Compile once with EXPECT_BASELINE_BUGS for the pinned
 * pre-fix driver and once without it for the patched driver.  The extractor
 * must preserve the function bodies and the wait wrapper; it must not
 * substitute a host reimplementation.  Define __asm as a variadic macro
 * before including the extracted functions to discard ARM-only asm
 * statements on the host compiler.
 *
 * This exercises software error contracts and object lifetime only. It does
 * not model RP23xx hardware timing or establish a physical bus-fault cause.
 */

#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define OK 0
#define ERROR (-1)
#define FAR
#define PRINT_GSPI(block) do { } while (0);
#define RP23XX_DMA_SIZE_WORD 2
#define RP23XX_DMA_CTRL_TRIG_OFFSET 0
#define RP23XX_DMA_CTRL_TRIG_BSWAP (1u << 24)
#define RP23XX_DMA_CTRL_TRIG_EN (1u << 0)
#define RP23XX_DMA_CTRL_TRIG_INCR_WRITE (1u << 6)
#include "cyw43439-dma-end.h"
#define RP23XX_PIO_FDEBUG(pio) 0x6000u
#define RP23XX_PIO_FDEBUG_RXUNDER_SHIFT 8
#define wlerr(...) ((void)0)
#define RP23XX_IO_BANK0_GPIO_STATUS(pin) (0x1000u + (uint32_t)(pin) * 4u)
#define RP23XX_IO_BANK0_GPIO_STATUS_OETOPAD (1u << 13)
#define RP23XX_PIO_TXF(pio, sm) (0x2000u + (uintptr_t)(pio) * 0x100u + (sm))
#define RP23XX_PIO_RXF(pio, sm) (0x3000u + (uintptr_t)(pio) * 0x100u + (sm))
#define UP_DMB() ((void)0)
#define __asm(...) ((void)0)

typedef struct
{
  int count;
  bool initialized;
  bool destroyed;
} sem_t;

#include "cyw43439-dma-types.h"

typedef int irqstate_t;
typedef void *DMA_HANDLE;

/* NuttX declares this as an enum; the generated functions use its tag. */
enum gspi_cmd_func_e
{
  gspi_f0_bus = 0,
  gspi_f0_bus_rev16 = 4,
  gspi_f1_backplane = 1,
  GSPI_FUNC_F0 = gspi_f0_bus,
  GSPI_FUNC_F1 = gspi_f1_backplane
};

typedef struct
{
  unsigned size;
  bool noincr;
  uint32_t dreq;
} dma_config_t;

typedef struct
{
  uint32_t ctrl_trig;
  uintptr_t read_addr;
  uintptr_t write_addr;
  uint32_t xfer_count;
} dma_control_block_t;

typedef struct
{
  unsigned id;
} mock_pio_t;

typedef struct
{
  mock_pio_t *pio;
  unsigned pio_sm;
  unsigned pio_location;
  unsigned gpio_on;
  unsigned gpio_select;
  unsigned gpio_data;
  unsigned gpio_clock;
  unsigned gpio_intr;
} rp23xx_gspi_t;

struct gspi_dev_s
{
  void *io_dev;
  int lock;
};

typedef struct gspi_dev_s gspi_dev_s;
typedef void (*dma_callback_t)(DMA_HANDLE, uint8_t, void *);

struct mock_dma_channel
{
  bool allocated;
  bool active;
  bool retired;
  unsigned id;
  dma_callback_t callback;
  void *callback_arg;
  dma_control_block_t *control_blocks;
  struct mock_dma_channel *transfer;
};

static struct mock_dma_channel channels[4];
static unsigned channel_allocations;
static unsigned allocation_calls;
static unsigned fail_allocation_at;
static unsigned free_calls;
static unsigned free_while_dma_active;
static unsigned stop_calls;
static unsigned sem_destroy_calls;
static unsigned sem_destroy_while_callback_live;
static unsigned pio_calls;
static unsigned pio_disable_calls;
static unsigned output_direction_calls;
static unsigned input_direction_calls;
static uint32_t pindir_state;
static unsigned critical_depth;
static unsigned critical_sequence;
static unsigned dma_enable_clear_calls;
static unsigned last_dma_enable_clear_sequence;
static unsigned first_stop_sequence;
static unsigned disable_sequence[4];
static uint32_t dma_enable_clear_mask;
static bool check_error_cleanup;
static unsigned mutex_lock_calls;
static unsigned mutex_unlock_calls;
static unsigned unlock_without_lock;
static unsigned pio_write_selected;
static bool mutex_held;
static int mutex_lock_result;
static int wait_mode;
static int wait_calls;
static uint8_t completion_status;
static uint32_t transferred_word;
static sem_t *active_sem;
static bool transfer_completed;
static uint32_t ctrl_registers[4];
static uint32_t pio_debug;
static bool inject_rx_underflow;

enum
{
  WAIT_COMPLETE = 0,
  WAIT_CANCEL,
  WAIT_DMA_ERROR
};

static __attribute__((unused)) irqstate_t enter_critical_section(void)
{
  critical_depth++;
  critical_sequence++;
  return 0;
}

static __attribute__((unused)) void leave_critical_section(irqstate_t flags)
{
  (void)flags;
  assert(critical_depth > 0);
  critical_depth--;
  critical_sequence++;
}

static int nxmutex_lock(int *mutex)
{
  (void)mutex;
  mutex_lock_calls++;
  if (mutex_lock_result < 0)
    {
      return mutex_lock_result;
    }

  assert(!mutex_held);
  mutex_held = true;
  return 0;
}

static int nxmutex_unlock(int *mutex)
{
  (void)mutex;
  mutex_unlock_calls++;
  if (!mutex_held)
    {
      unlock_without_lock++;
      return -EPERM;
    }

  mutex_held = false;
  return 0;
}

static int nxsem_init(sem_t *sem, int pshared, unsigned value)
{
  (void)pshared;
  sem->count = (int)value;
  sem->initialized = true;
  sem->destroyed = false;
  if (!active_sem)
    {
      active_sem = sem;
    }

  return 0;
}

static int nxsem_post(sem_t *sem)
{
  assert(sem && sem->initialized && !sem->destroyed);
  sem->count++;
  return 0;
}

static int nxsem_destroy(sem_t *sem)
{
  unsigned i;
  assert(sem && sem->initialized && !sem->destroyed);
  if (check_error_cleanup)
    {
      assert(critical_depth > 0);
    }
  for (i = 0; i < channel_allocations; i++)
    {
      if (channels[i].active && channels[i].callback_arg)
        {
          sem_destroy_while_callback_live++;
        }
    }

  sem->destroyed = true;
  sem_destroy_calls++;
  return 0;
}

static void complete_dma(void)
{
  unsigned i;
  struct mock_dma_channel *owner = NULL;

  for (i = 0; i < 4; i++)
    {
      if (channels[i].active && channels[i].callback)
        {
          owner = &channels[i];
          break;
        }
    }

  assert(owner != NULL);
  if (owner->control_blocks)
    {
      for (dma_control_block_t *block = owner->control_blocks;
           block->xfer_count != 0; block++)
        {
          uint32_t *destination = (uint32_t *)block->write_addr;
          for (unsigned word = 0; word < block->xfer_count; word++)
            destination[word] = transferred_word;
        }
    }

  transfer_completed = true;
  if (inject_rx_underflow) pio_debug |= 1u << RP23XX_PIO_FDEBUG_RXUNDER_SHIFT;
  for (i = 0; i < channel_allocations; i++) channels[i].active = false;
  owner->callback(owner, completion_status, owner->callback_arg);
}

static int nxsem_wait(sem_t *sem)
{
  wait_calls++;
  if (wait_calls == 1)
    {
      return -EINTR;
    }

  if (wait_mode == WAIT_CANCEL)
    {
      return -ECANCELED;
    }

  if (!transfer_completed)
    {
      complete_dma();
    }

  if (sem->count > 0)
    {
      sem->count--;
    }

  return 0;
}

#include "cyw43439-dma-wait.h"

static DMA_HANDLE rp23xx_dmachannel(void)
{
  struct mock_dma_channel *channel;
  allocation_calls++;
  if (fail_allocation_at == allocation_calls)
    {
      return NULL;
    }

  assert(channel_allocations < 4);
  channel = &channels[channel_allocations++];
  memset(channel, 0, sizeof(*channel));
  channel->id = channel_allocations - 1;
  channel->allocated = true;
  return channel;
}

static void rp23xx_dmafree(DMA_HANDLE handle)
{
  struct mock_dma_channel *channel = handle;
  assert(channel != NULL && channel->allocated);
  if (check_error_cleanup && wait_calls > 0)
    {
      assert(critical_depth > 0);
      assert(disable_sequence[channel->id] != 0);
    }
  if (channel->active && !channel->retired)
    {
      free_while_dma_active++;
    }
  channel->allocated = false;
  free_calls++;
}

static void rp23xx_dmastop(DMA_HANDLE handle)
{
  struct mock_dma_channel *channel = handle;
  assert(channel != NULL);
  if (check_error_cleanup && wait_calls > 0)
    {
      assert(critical_depth > 0);
      assert(disable_sequence[channel->id] > 0);
      if (!first_stop_sequence)
        {
          first_stop_sequence = ++critical_sequence;
        }
    }
  channel->active = false;
  channel->retired = true;
  channel->callback = NULL;
  channel->callback_arg = NULL;
  if (wait_calls > 0) stop_calls++;
}

static void rp23xx_dmastart(DMA_HANDLE handle, dma_callback_t callback,
                            void *arg)
{
  struct mock_dma_channel *channel = handle;
  assert(channel != NULL);
  channel->active = true;
  channel->retired = false;
  if (channel->transfer)
    {
      channel->transfer->active = true;
      channel->transfer->retired = false;
    }
  if (callback)
    {
      channel->callback = callback;
      channel->callback_arg = arg;
    }
  active_sem = arg ? &((dma_info_t *)arg)->sem : active_sem;
}

static void rp23xx_txdmasetup(DMA_HANDLE handle, uintptr_t paddr,
                              uintptr_t maddr, size_t nbytes,
                              dma_config_t config)
{
  (void)paddr; (void)maddr; (void)nbytes; (void)config;
  assert(handle != NULL);
}

static void rp23xx_ctrl_dmasetup(DMA_HANDLE control, DMA_HANDLE transfer,
                                 dma_control_block_t *blocks,
                                 dma_callback_t callback, void *arg)
{
  struct mock_dma_channel *channel = transfer;
  unsigned end = 0;

  /* Check the real descriptor marker without modeling hardware IRQ timing. */

  while (end < 2 && blocks[end].xfer_count != 0) end++;
  assert(end > 0 && blocks[end].xfer_count == 0);
  assert(blocks[end].ctrl_trig == RP23XX_DMA_CTRL_TRIG_IRQ_QUIET);
  assert(blocks[end].read_addr == 0 && blocks[end].write_addr == 0);
  ((struct mock_dma_channel *)control)->transfer = channel;
  channel->callback = callback;
  channel->callback_arg = arg;
  channel->control_blocks = blocks;
}

static uint32_t rp23xx_pio_get_dreq(mock_pio_t *pio, unsigned sm, bool tx)
{
  (void)pio; (void)sm; (void)tx;
  return 0;
}

static uint32_t rp23xx_pio_sm_get_rx_fifo_level(mock_pio_t *pio, unsigned sm)
{
  (void)pio;
  (void)sm;
  return 0;
}

static uint32_t rp23xx_dma_ctrl_blk_ctrl(DMA_HANDLE channel, unsigned size,
                                         uint32_t pacing, uint32_t flags)
{
  (void)channel;
  return size | pacing | flags;
}

static void rp23xx_pio_sm_set_enabled(mock_pio_t *pio, unsigned sm,
                                      bool enabled)
{
  (void)pio; (void)sm;
  pio_calls++;
  if (!enabled)
    {
      pio_disable_calls++;
    }
}

static void rp23xx_pio_sm_clear_fifos(mock_pio_t *pio, unsigned sm)
{
  (void)pio; (void)sm;
}

static void rp23xx_pio_sm_restart(mock_pio_t *pio, unsigned sm)
{
  (void)pio; (void)sm;
}

static void rp23xx_pio_sm_clkdiv_restart(mock_pio_t *pio, unsigned sm)
{
  (void)pio; (void)sm;
}

static void rp23xx_pio_sm_exec(mock_pio_t *pio, unsigned sm, uint16_t instr)
{
  (void)pio; (void)sm; (void)instr;
}

static void rp23xx_pio_sm_put(mock_pio_t *pio, unsigned sm, uint32_t value)
{
  (void)pio; (void)sm; (void)value;
}

static void rp23xx_pio_sm_set_pindirs_with_mask(mock_pio_t *pio, unsigned sm,
                                                uint32_t values,
                                                uint32_t mask)
{
  (void)pio; (void)sm;
  pindir_state = (pindir_state & ~mask) | (values & mask);
  if (values & mask)
    {
      output_direction_calls++;
    }
  else
    {
      input_direction_calls++;
    }
}

static void rp23xx_gpio_put(unsigned pin, bool value)
{
  (void)pin;
  if (value)
    {
      pio_write_selected = 0;
    }
  else
    {
      pio_write_selected = 1;
    }
}

static uint32_t rp23xx_dma_register(DMA_HANDLE handle, uint32_t offset)
{
  struct mock_dma_channel *channel = handle;
  return 0x4000u + channel->id * 4u + offset;
}

static uint32_t getreg32(uint32_t address)
{
  if (address == RP23XX_PIO_FDEBUG(0)) return pio_debug;
  if (address >= 0x4800u && address < 0x4810u) return 4;
  if (address >= 0x4000u && address < 0x4010u)
    {
      return ctrl_registers[(address - 0x4000u) / 4u];
    }

  /* OETOPAD is always zero to keep this host fixture finite. */
  return 0;
}

static void putreg32(uint32_t value, uint32_t address)
{
  if (address == RP23XX_PIO_FDEBUG(0)) pio_debug &= ~value;
  if (address >= 0x4000u && address < 0x4010u)
    {
      if (check_error_cleanup && wait_calls > 0 && value == 0)
        {
          unsigned id = (address - 0x4000u) / 4u;
          assert(critical_depth > 0);
          dma_enable_clear_calls++;
          last_dma_enable_clear_sequence = ++critical_sequence;
          disable_sequence[id] = last_dma_enable_clear_sequence;
          dma_enable_clear_mask |= 1u << id;
        }
      ctrl_registers[(address - 0x4000u) / 4u] = value;
    }
}

static void modifyreg32(uint32_t address, uint32_t clearbits,
                        uint32_t setbits)
{
  uint32_t value = getreg32(address);
  if ((clearbits & RP23XX_DMA_CTRL_TRIG_EN) != 0 &&
      address >= 0x4000u && address < 0x4010u)
    {
      assert(critical_depth > 0);
      dma_enable_clear_calls++;
      last_dma_enable_clear_sequence = ++critical_sequence;
      dma_enable_clear_mask |= 1u << ((address - 0x4000u) / 4u);
    }

  putreg32((value & ~clearbits) | setbits, address);
}

static uint16_t pio_encode_jmp(unsigned address)
{
  return (uint16_t)address;
}

static uint16_t pio_encode_out(unsigned destination, unsigned count)
{
  return (uint16_t)(destination + count);
}

enum
{
  pio_x = 1,
  pio_y = 2
};

static void my_transfer_irq(struct gspi_dev_s *gspi, bool active)
{
  (void)gspi; (void)active;
}

static __attribute__((unused)) void set_errno(int value)
{
  errno = value;
}

#include "cyw43439-dma-functions.h"

static void reset_fixture(void)
{
  memset(channels, 0, sizeof(channels));
  memset(ctrl_registers, 0, sizeof(ctrl_registers));
  memset(disable_sequence, 0, sizeof(disable_sequence));
  channel_allocations = allocation_calls = fail_allocation_at = 0;
  free_calls = free_while_dma_active = stop_calls = sem_destroy_calls = 0;
  sem_destroy_while_callback_live = 0;
  pio_calls = pio_disable_calls = 0;
  output_direction_calls = input_direction_calls = 0;
  pindir_state = critical_depth = critical_sequence = 0;
  dma_enable_clear_calls = first_stop_sequence = 0;
  last_dma_enable_clear_sequence = 0;
  dma_enable_clear_mask = 0;
  check_error_cleanup = false;
  mutex_lock_calls = mutex_unlock_calls = unlock_without_lock = 0;
  mutex_held = false;
  mutex_lock_result = 0;
  wait_mode = WAIT_COMPLETE;
  wait_calls = 0;
  completion_status = 0;
  transferred_word = 0x78563412u;
  active_sem = NULL;
  transfer_completed = false;
  pio_debug = 0;
  inject_rx_underflow = false;
}

static int run_read_config(uint32_t *buffer, enum gspi_cmd_func_e function,
                            uint16_t length)
{
  static mock_pio_t pio;
  static rp23xx_gspi_t io;
  static struct gspi_dev_s dev;
  io.pio = &pio;
  io.pio_sm = 0;
  io.gpio_data = 2;
  io.gpio_clock = 3;
  io.gpio_select = 4;
  dev.io_dev = &io;
  return my_read(&dev, false, function, 0x1234, length, buffer);
}

static int run_read(uint32_t *buffer)
{
  return run_read_config(buffer, GSPI_FUNC_F0, 4);
}

static int run_write(const uint32_t *data)
{
  static mock_pio_t pio;
  static rp23xx_gspi_t io;
  static struct gspi_dev_s dev;
  io.pio = &pio;
  io.pio_sm = 0;
  io.gpio_data = 2;
  io.gpio_clock = 3;
  io.gpio_select = 4;
  dev.io_dev = &io;
  return my_write(&dev, false, GSPI_FUNC_F0, 0x1234, 4, data);
}

static void test_mutex_error(void)
{
  uint32_t output = 0x0200001fu;
  const uint32_t input = 0x12345678u;
  int ret;
  (void)input;
  reset_fixture();
  mutex_lock_result = -EINTR;
#ifdef EXPECT_BASELINE_BUGS
  ret = run_read(&output);
  (void)ret;
  assert(pio_calls != 0);
  assert(unlock_without_lock == 1);
#else
  ret = run_read(&output);
  assert(ret == -EINTR);
  assert(allocation_calls == 0 && channel_allocations == 0);
  assert(pio_calls == 0 && mutex_unlock_calls == 0);
  reset_fixture();
  mutex_lock_result = -EINTR;
  ret = run_write(&input);
  assert(ret == -EINTR);
  assert(allocation_calls == 0 && pio_calls == 0);
#endif
}

static void test_signal_then_completion(void)
{
  uint32_t output = 0x0200001fu;
  const uint32_t input = 0x12345678u;
  int ret;
  reset_fixture();
  ret = run_read(&output);
#ifdef EXPECT_BASELINE_BUGS
  assert(wait_calls == 1);
  assert(output == 0x0200001fu);
  assert(free_calls == 2 && sem_destroy_calls == 1);
  assert(sem_destroy_while_callback_live != 0);
  assert(free_while_dma_active != 0);
  (void)ret;
#else
  assert(ret == 0);
  assert(wait_calls == 2 && transfer_completed);
  assert(output == transferred_word);
  assert(free_calls == 2 && sem_destroy_calls == 1);
  assert(sem_destroy_while_callback_live == 0);
  assert(free_while_dma_active == 0);
  assert(!mutex_held && unlock_without_lock == 0);
#endif

  reset_fixture();
  ret = run_write(&input);
#ifdef EXPECT_BASELINE_BUGS
  assert(wait_calls == 1);
  assert(free_calls == 1 && sem_destroy_calls == 1);
  assert(sem_destroy_while_callback_live != 0);
  assert(free_while_dma_active != 0);
  (void)ret;
#else
  assert(ret == 0 && wait_calls == 2 && transfer_completed);
  assert(free_calls == 1 && sem_destroy_calls == 1);
  assert(sem_destroy_while_callback_live == 0);
  assert(free_while_dma_active == 0);
#endif
}

#ifndef EXPECT_BASELINE_BUGS
static void test_backplane_completion(void)
{
  uint32_t output[16];
  for (unsigned words = 1; words <= 16; words *= 16)
    {
      memset(output, 0, sizeof(output));
      reset_fixture();
      assert(run_read_config(output, GSPI_FUNC_F1, 4 * words) == 0);
      assert(wait_calls == 2 && transfer_completed);
      for (unsigned word = 0; word < words; word++)
        assert(output[word] == transferred_word);
      assert(free_while_dma_active == 0 && sem_destroy_while_callback_live == 0);
    }
}

static void test_cancel_retires_dma(void)
{
  uint32_t output = 0x0200001fu;
  int ret;
  reset_fixture();
  wait_mode = WAIT_CANCEL;
  check_error_cleanup = true;
  ret = run_read(&output);
  assert(ret == -ECANCELED);
  assert(output == 0x0200001fu);
  assert((pindir_state & (1u << 2)) == 0);
  assert(stop_calls == 2 && free_calls == 2 && sem_destroy_calls == 1);
  assert(sem_destroy_while_callback_live == 0);
  assert(dma_enable_clear_calls == 3);
  assert(dma_enable_clear_mask == 0x3u);
  assert(disable_sequence[1] > 0 &&
         disable_sequence[1] < first_stop_sequence &&
         disable_sequence[0] > first_stop_sequence &&
         critical_depth == 0);
  assert(!mutex_held);
}

static void test_dma_status_error(void)
{
  uint32_t output = 0x0200001fu;
  int ret;
  reset_fixture();
  completion_status = EIO;
  wait_mode = WAIT_DMA_ERROR;
  check_error_cleanup = true;
  ret = run_read(&output);
  assert(ret == -EIO);
  assert(free_calls == 2 && sem_destroy_calls == 1);
  assert(sem_destroy_while_callback_live == 0);
  assert(dma_enable_clear_calls == 3);
  assert(dma_enable_clear_mask == 0x3u);
  assert(disable_sequence[1] > 0 &&
         disable_sequence[1] < first_stop_sequence &&
         disable_sequence[0] > first_stop_sequence &&
         critical_depth == 0);
}

static void test_write_dma_status_error_sets_data_input(void)
{
  const uint32_t input = 0x12345678u;
  int ret;
  reset_fixture();
  completion_status = EIO;
  wait_mode = WAIT_DMA_ERROR;
  check_error_cleanup = true;
  ret = run_write(&input);
  assert(ret == -EIO);
  assert((pindir_state & (1u << 2)) == 0);
  assert(free_calls == 1 && sem_destroy_calls == 1);
  assert(dma_enable_clear_calls == 1);
  assert(dma_enable_clear_mask == 0x1u);
  assert(last_dma_enable_clear_sequence > 0 &&
         last_dma_enable_clear_sequence < first_stop_sequence &&
         critical_depth == 0);
}

static void test_allocation_failures(void)
{
  uint32_t output = 0x0200001fu;
  const uint32_t input = 0x12345678u;
  int ret;
  reset_fixture();
  fail_allocation_at = 1;
  ret = run_read(&output);
  assert(ret == -ENOMEM && channel_allocations == 0);
  assert(pio_calls == 0 && mutex_held == false);

  reset_fixture();
  fail_allocation_at = 2;
  ret = run_read(&output);
  assert(ret == -ENOMEM && channel_allocations == 1);
  assert(free_calls == 1 && pio_calls == 0 && !mutex_held);

  reset_fixture();
  fail_allocation_at = 1;
  ret = run_write(&input);
  assert(ret == -ENOMEM && channel_allocations == 0);
  assert(pio_calls == 0 && !mutex_held);
}

#ifdef EXPECT_RX_FIFO_GUARD
static void test_rx_underflow_is_not_success(void)
{
  uint32_t output;
  reset_fixture();
  inject_rx_underflow = true;
  check_error_cleanup = true;
  assert(run_read_config(&output, GSPI_FUNC_F1, 4) == -EIO);
  assert(free_while_dma_active == 0 && sem_destroy_while_callback_live == 0);
  assert(!mutex_held && critical_depth == 0);
  reset_fixture();
  pio_debug = 1u << RP23XX_PIO_FDEBUG_RXUNDER_SHIFT;
  assert(run_read_config(&output, GSPI_FUNC_F1, 4) == 0);
  assert(pio_debug == 0); /* A previous transfer's sticky flag is cleared. */
}
#endif
#endif

int main(void)
{
  test_mutex_error();
  test_signal_then_completion();
#ifndef EXPECT_BASELINE_BUGS
  test_backplane_completion();
  test_cancel_retires_dma();
  test_dma_status_error();
  test_write_dma_status_error_sets_data_input();
  test_allocation_failures();
#ifdef EXPECT_RX_FIFO_GUARD
  test_rx_underflow_is_not_success();
#endif
#endif
  puts("CYW43439 DMA lifetime witness: PASS");
  return 0;
}

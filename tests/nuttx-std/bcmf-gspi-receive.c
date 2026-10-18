/* Exercise the patched gSPI receive function with an eight-frame pool. */
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#define FAR
#define OK 0
#define DEBUGPANIC() assert(false)
#define MIN(a, b) ((a) < (b) ? (a) : (b))
#define wlerr(...) ((void)0)
#define wlinfo(...) ((void)0)
#define begin_packed_struct
#define end_packed_struct __attribute__((packed))
#define FC_UPDATE_PKT_LENGTH 12
#define F2_FRAME_CONTROL_CHANNEL 0
#define F2_FRAME_EVENT_CHANNEL 1
#define F2_FRAME_DATA_CHANNEL 2
#define gspi_f2_dma 2
/* This witness exercises the diagnostic-disabled receive path. */
#define BCMF_TRACE_RX(dev, event) ((void)0)

#include "bcmf-gspi-header.h"

struct gspi_dev_s
{
  int (*read)(struct gspi_dev_s *, bool, int, uint32_t, uint16_t, uint32_t *);
};
typedef struct gspi_dev_s gspi_dev_t;
typedef struct
{
  gspi_dev_t *gspi;
  uint8_t max_seq;
  int queue_lock;
  int rx_queue;
} bcmf_gspi_dev_t;
typedef bcmf_gspi_dev_t bcmf_interface_dev_t;
struct bcmf_dev_s { bcmf_gspi_dev_t *bus; };
struct bcmf_frame_s { uint8_t *base; uint8_t *data; int len; };
typedef struct
{
  struct bcmf_frame_s header;
  int list_entry;
  uint32_t data[64];
} bcmf_interface_frame_t;

static bcmf_interface_frame_t frame;
static uint32_t input[64];
static int allocated, frees, reads, aborts, queued, notifications;
static int read_error, dispatch_error;
static unsigned int dispatched;
static unsigned int dump_length;

static bcmf_interface_frame_t *bcmf_interface_allocate_frame(
    struct bcmf_dev_s *priv, bool block, bool tx)
{
  (void)priv;
  assert(!block && !tx);
  if (allocated == 8) return NULL;
  allocated++;
  frame.header.base = frame.header.data = (uint8_t *)frame.data;
  frame.header.len = sizeof(frame.data);
  return &frame;
}

static void bcmf_interface_free_frame(struct bcmf_dev_s *priv,
                                       bcmf_interface_frame_t *owned)
{
  (void)priv;
  assert(owned == &frame && allocated > 0);
  allocated--;
  frees++;
}

static int read_frame(gspi_dev_t *gspi, bool increment, int function,
                      uint32_t address, uint16_t length, uint32_t *data)
{
  (void)gspi;
  assert(increment && function == gspi_f2_dma && address == 0);
  reads++;
  if (read_error) return read_error;
  memcpy(data, input, length);
  return 0;
}

static int f2_frame_rx_fail(bcmf_gspi_dev_t *bus, bool retry)
{
  (void)bus;
  assert(!retry);
  aborts++;
  return 0;
}
static void bcmf_hexdump(uint8_t *data, unsigned int len, unsigned long offset)
{ (void)data; (void)offset; dump_length = len; }
static int nxmutex_lock(int *lock) { (void)lock; return 0; }
static void nxmutex_unlock(int *lock) { (void)lock; }
static void list_add_tail(int *queue, int *entry)
{ (void)queue; assert(entry == &frame.list_entry); queued++; }
static void bcmf_netdev_notify_rx(struct bcmf_dev_s *priv)
{ (void)priv; notifications++; }
static int bcmf_cdc_process_control_frame(struct bcmf_dev_s *priv,
                                         struct bcmf_frame_s *owned)
{ (void)priv; assert(owned == &frame.header); dispatched++; return dispatch_error; }
static int bcmf_bdc_process_event_frame(struct bcmf_dev_s *priv,
                                       struct bcmf_frame_s *owned)
{ return bcmf_cdc_process_control_frame(priv, owned); }

#include "bcmf-gspi-functions.h"

static void packet(uint16_t size, uint8_t channel)
{
  f2_frame_header_t *header = (void *)input;
  memset(input, 0, sizeof(input));
  header->size = size;
  header->checksum = (uint16_t)~size;
  header->data_offset = sizeof(*header);
  header->channel = channel;
  header->credit = 37;
}

int main(void)
{
  gspi_dev_t gspi = { .read = read_frame };
  bcmf_gspi_dev_t bus = { .gspi = &gspi };
  struct bcmf_dev_s priv = { .bus = &bus };
  f2_frame_header_t *header = (void *)input;

#ifdef EXPECT_BASELINE_BUGS
  /* Witness the exact failures in the untouched dependency, not a mock fix. */
  packet(12, F2_FRAME_CONTROL_CHANNEL);
  assert(bcmf_gspi_read_f2_frame(&priv, 12) == -EINVAL && reads == 0);
  assert(bcmf_gspi_read_f2_frame(&priv, 16) == 0);
  assert(allocated == 1 && frees == 0);
  packet(16, F2_FRAME_EVENT_CHANNEL);
  header->checksum = 0;
  assert(bcmf_gspi_read_f2_frame(&priv, 16) == -EINVAL);
  assert(allocated == 2 && frees == 0);
  return 0;
#endif

#ifdef EXPECT_PRE_FRAMING_BUGS
  /* Isolate the framing regression after earlier ownership fixes. */
  packet(16, F2_FRAME_CONTROL_CHANNEL);
  header->checksum = 0xffff;
  assert(bcmf_gspi_read_f2_frame(&priv, 16) == 0);
  assert(dispatched == 1 && bus.max_seq == 37 && allocated == 0);
  packet(64, F2_FRAME_CONTROL_CHANNEL);
  header->credit = 91;
  assert(bcmf_gspi_read_f2_frame(&priv, 16) == 0);
  assert(dispatched == 2 && bus.max_seq == 91 && allocated == 0);
  assert(bcmf_gspi_read_f2_frame(&priv, 0) == -EINVAL && aborts == 0);
  return 0;
#endif

  /* Header-only updates must be consumed and cannot exhaust the pool. */
  packet(12, F2_FRAME_CONTROL_CHANNEL);
  for (int i = 0; i < 64; i++)
    {
      assert(bcmf_gspi_read_f2_frame(&priv, 12) == 0);
      assert(allocated == 0 && reads == i + 1 && frees == i + 1);
      assert(bus.max_seq == 37);
    }
  /* The same credit-only packet may have transport padding. */
  assert(bcmf_gspi_read_f2_frame(&priv, 16) == 0 && allocated == 0);
  assert(bcmf_gspi_read_f2_frame(&priv, 11) == -EINVAL && allocated == 0);
  assert(aborts == 1);
  aborts = 0;

  /* OR is not a one's-complement check: overlapping one bits must fail.
   * Truncated headers must not update credits or dispatch unread bytes.
   */
  for (int channel = 0; channel <= F2_FRAME_DATA_CHANNEL; channel++)
    {
      packet(16, channel);
      header->checksum = 0xffff;
      header->credit = 91;
      unsigned int old_dispatch = dispatched;
      assert(bcmf_gspi_read_f2_frame(&priv, 16) == -EINVAL);
      assert(allocated == 0 && bus.max_seq == 37 && dispatched == old_dispatch);
      packet(64, channel);
      header->credit = 91;
      assert(bcmf_gspi_read_f2_frame(&priv, 16) == -EINVAL);
      assert(allocated == 0 && bus.max_seq == 37 && dispatched == old_dispatch);
      assert(dump_length == 16 && queued == 0 && notifications == 0);
    }
  assert(aborts == 6);
  aborts = 0;

  packet(16, F2_FRAME_EVENT_CHANNEL);
  header->checksum = 0;
  assert(bcmf_gspi_read_f2_frame(&priv, 16) == -EINVAL);
  assert(allocated == 0 && aborts == 1);
  read_error = -EIO;
  assert(bcmf_gspi_read_f2_frame(&priv, 16) == -EIO && allocated == 0);
  read_error = 0;
  packet(16, F2_FRAME_EVENT_CHANNEL);
  header->data_offset = 17;
  assert(bcmf_gspi_read_f2_frame(&priv, 16) == -EINVAL && allocated == 0);
  packet(16, F2_FRAME_CONTROL_CHANNEL);
  dispatch_error = -EPROTO;
  assert(bcmf_gspi_read_f2_frame(&priv, 16) == -EPROTO && allocated == 0);
  packet(16, F2_FRAME_EVENT_CHANNEL);
  assert(bcmf_gspi_read_f2_frame(&priv, 16) == -EPROTO && allocated == 0);
  packet(16, 7);
  assert(bcmf_gspi_read_f2_frame(&priv, 16) == -EINVAL && allocated == 0);

  /* Data frames transfer ownership to the network queue, not the free pool. */
  packet(16, F2_FRAME_DATA_CHANNEL);
  assert(bcmf_gspi_read_f2_frame(&priv, 16) == 0 && allocated == 1);
  assert(queued == 1 && notifications == 1);
  bcmf_interface_free_frame(&priv, &frame);
  assert(allocated == 0);
  return 0;
}

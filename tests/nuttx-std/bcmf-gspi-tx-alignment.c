/* SPDX-License-Identifier: MIT */
/* Actual F2 sender; transport stand-in records the command/word extent. */
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define FAR
#define OK 0
#define DEBUGPANIC() abort()
#define wlwarn(...) ((void)0)
#define wlinfo(...) ((void)0)
#define begin_packed_struct
#define end_packed_struct __attribute__((packed))

#include "bcmf-gspi-tx-constants.h"
#include "bcmf-gspi-tx-header.h"

typedef struct gspi_dev_s gspi_dev_t;
struct gspi_dev_s
{
  int (*read)(gspi_dev_t *, bool, uint8_t, uint32_t, uint16_t, uint32_t *);
  int (*write)(gspi_dev_t *, bool, uint8_t, uint32_t, uint16_t, uint32_t *);
};

struct bcmf_frame_s
{
  uint8_t *base;
  uint8_t *data;
  unsigned int len;
};

typedef struct
{
  struct bcmf_frame_s header;
  bool tx;
  int list_entry;
  uint8_t storage[2064] __attribute__((aligned(4)));
} bcmf_interface_frame_t;

struct tx_queue_s { bcmf_interface_frame_t *queued; };

typedef struct
{
  gspi_dev_t *gspi;
  uint8_t tx_seq;
  uint8_t max_seq;
  int queue_lock;
  struct tx_queue_s tx_queue;
} bcmf_gspi_dev_t;

struct bcmf_dev_s { bcmf_gspi_dev_t *bus; };

#define list_is_empty(head) ((head)->queued == NULL)
#define list_remove_head_type(head, type, member) \
  take_frame(head)

static bcmf_interface_frame_t *active_frame;
static unsigned int write_calls, read_calls, dequeue_calls, free_calls, notify_calls;
static unsigned int observed_length;
static uint8_t wire[2064];
static int write_result;
static int read_result;
static uint32_t read_value = CYW_REG_STATUS_F2_RECEIVE_RDY;

static int nxmutex_lock(int *lock) { (void)lock; return 0; }
static void nxmutex_unlock(int *lock) { (void)lock; }

static bcmf_interface_frame_t *take_frame(struct tx_queue_s *queue)
{
  bcmf_interface_frame_t *frame = queue->queued;
  assert(frame == active_frame);
  queue->queued = NULL;
  dequeue_calls++;
  return frame;
}

static int read_status(gspi_dev_t *gspi, bool increment, uint8_t function,
                       uint32_t address, uint16_t length, uint32_t *value)
{
  (void)gspi;
  assert(increment && function == gspi_f0_bus && address == CYW_REG_STATUS);
  assert(length == sizeof(*value));
  read_calls++;
  *value = read_value;
  return read_result;
}

static int write_f2(gspi_dev_t *gspi, bool increment, uint8_t function,
                    uint32_t address, uint16_t length, uint32_t *words)
{
  (void)gspi;
  assert(increment && function == gspi_f2_dma && address == 0);
  write_calls++;
  observed_length = length;
  assert(((length + 3u) & ~3u) <= sizeof(wire));
  memcpy(wire, words, (length + 3u) & ~3u);
  return write_result;
}

static void bcmf_interface_free_frame(struct bcmf_dev_s *priv,
                                      bcmf_interface_frame_t *frame)
{
  (void)priv;
  assert(frame == active_frame);
  free_calls++;
}

static void bcmf_netdev_notify_tx(struct bcmf_dev_s *priv)
{
  (void)priv;
  notify_calls++;
}

#include "bcmf-gspi-tx-function.h"

static void run_case(unsigned int length, bool is_tx, int result)
{
  gspi_dev_t gspi = { .read = read_status, .write = write_f2 };
  bcmf_gspi_dev_t bus = { .gspi = &gspi, .tx_seq = 7, .max_seq = 99 };
  struct bcmf_dev_s priv = { .bus = &bus };
  bcmf_interface_frame_t frame;
  f2_frame_header_t *header = (void *)frame.storage;
  unsigned int rounded = (length + 3u) & ~3u;

  memset(&frame, 0xa5, sizeof(frame));
  frame.header.base = frame.storage;
  frame.header.data = frame.storage;
  frame.header.len = length;
  frame.tx = is_tx;
  header->size = (uint16_t)length;
  header->checksum = (uint16_t)~length;
  header->sequence = 0xee;
  header->channel = is_tx ? 2 : 0;
  active_frame = &frame;
  bus.tx_queue.queued = &frame;
  write_result = result;
  uint8_t expected[sizeof(frame.storage)];
  memcpy(expected, frame.storage, sizeof(expected));
  expected[offsetof(f2_frame_header_t, sequence)] = bus.tx_seq;

  unsigned int previous_writes = write_calls;
  unsigned int previous_frees = free_calls;
  unsigned int previous_notifies = notify_calls;
  unsigned int previous_seq = bus.tx_seq;
  unsigned int previous_dequeues = dequeue_calls;
  int ret = bcmf_gspi_send_f2_frame(&priv);
#ifdef EXPECT_PREPATCH_MISMATCH
  if ((length & 3u) != 0)
    {
      assert(observed_length == length);
      assert(observed_length != rounded);
    }
  else
#endif
    {
      assert(observed_length == rounded);
      assert(observed_length == ((rounded + 3u) & ~3u));
      for (unsigned int i = length; i < rounded; i++) assert(wire[i] == 0);
    }

  assert(memcmp(wire, expected, length) == 0);
  assert(memcmp(frame.storage, expected, length) == 0);
  for (unsigned int i = rounded; i < sizeof(frame.storage); i++)
    assert(frame.storage[i] == 0xa5);

  assert(ret == result);
  assert(write_calls == previous_writes + 1);
  assert(free_calls == previous_frees + 1);
  assert(dequeue_calls == previous_dequeues + 1);
  assert(bus.tx_seq == previous_seq + 1);
  assert(notify_calls == previous_notifies + (result == 0 && is_tx));
  assert(header->size == length);
  assert(header->checksum == (uint16_t)~length);
  assert(bus.tx_queue.queued == NULL);
}

static void blocked_cases(void)
{
  gspi_dev_t gspi = { .read = read_status, .write = write_f2 };
  bcmf_gspi_dev_t bus = { .gspi = &gspi, .tx_seq = 7, .max_seq = 99 };
  struct bcmf_dev_s priv = { .bus = &bus };
  bcmf_interface_frame_t frame;
  memset(&frame, 0xa5, sizeof(frame));
  active_frame = &frame;
  unsigned int writes = write_calls, frees = free_calls, dequeues = dequeue_calls;
  unsigned int reads = read_calls;
  assert(bcmf_gspi_send_f2_frame(&priv) == -ENODATA);
  assert(read_calls == reads);
  bus.tx_queue.queued = &frame;
  bus.max_seq = bus.tx_seq;
  assert(bcmf_gspi_send_f2_frame(&priv) == -EAGAIN);
  assert(read_calls == reads);
  bus.max_seq = 99;
  read_value = 0;
  assert(bcmf_gspi_send_f2_frame(&priv) == -EAGAIN);
  read_value = CYW_REG_STATUS_F2_RECEIVE_RDY;
  read_result = -ETIMEDOUT;
  assert(bcmf_gspi_send_f2_frame(&priv) == -ETIMEDOUT);
  read_result = 7;
  assert(bcmf_gspi_send_f2_frame(&priv) == -EIO);
  read_result = 0;
  assert(read_calls == reads + 3 && bus.tx_seq == 7);
  assert(write_calls == writes && free_calls == frees && dequeue_calls == dequeues);
  assert(bus.tx_queue.queued == &frame);
  for (size_t i = 0; i < sizeof(frame.storage); i++) assert(frame.storage[i] == 0xa5);
}

int main(void)
{
  static const unsigned int lengths[] =
    { 12, 13, 14, 15, 16, 134, 528, 2045, 2046, 2047, 2048 };
  for (size_t i = 0; i < sizeof(lengths) / sizeof(lengths[0]); i++)
    {
      run_case(lengths[i], true, 0);
      run_case(lengths[i], false, 0);
      run_case(lengths[i], true, -EIO);
    }
  blocked_cases();
  return 0;
}

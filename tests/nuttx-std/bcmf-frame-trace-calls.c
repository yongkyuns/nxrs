/* SPDX-License-Identifier: MIT */
/* Real function logic with platform stand-ins: call ordering, not native ABI. */
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <stdlib.h>
#include <stdio.h>

#define FAR
#define OK 0
#define CONFIG_NET_IPv4 1
#define ETHTYPE_IP 0x0800
#define TPID_8021QVLAN 0x8100
/* Native packet layouts are covered separately; this stand-in is host-order. */
#undef HTONS
#define HTONS(x) (x)
#define BUF ((struct eth_header_s *)priv->bc_dev.d_buf)
#define NETDEV_TXPACKETS(dev) ((void)(dev))
#define NETDEV_RXIPV4(dev) ((void)(dev))
#define NETDEV_RXDROPPED(dev) ((void)(dev))
#define ninfo(...) ((void)0)
#define wlerr(...) ((void)0)
#define wlinfo(...) ((void)0)
#define wlwarn(...) ((void)0)
#define DEBUGPANIC() assert(0)
#define MIN(a, b) ((a) < (b) ? (a) : (b))
#define nxsched_usleep(usec) ((void)(usec))
#define BCMF_GSPI_LOWPOWER_TIMEOUT_TICK 200
#define MSEC2TICK(ms) (ms)
#ifdef EXPECT_BASELINE_ERROR_IRQS
#  define SCRIPTED_FAULT_WAIT_LONG 1
#else
#  define SCRIPTED_FAULT_WAIT_LONG 0
#endif

#include <nuttx/wireless/ieee80211/bcmf_trace.h>

struct net_driver_s
{
  uint8_t *d_buf;
  unsigned int d_len;
};

struct bcmf_frame_s
{
  uint8_t *base;
  uint8_t *data;
  unsigned int len;
};

struct bcmf_dev_s;
struct bcmf_bus_s
{
  void (*free_frame)(struct bcmf_dev_s *, struct bcmf_frame_s *);
};

struct bcmf_dev_s
{
  struct net_driver_s bc_dev;
  struct bcmf_bus_s *bus;
  struct bcmf_frame_s *cur_tx_frame;
  bool bc_bifup;
  int bc_rxwork;
};

struct eth_header_s { uint8_t addresses[12]; uint16_t type; };
struct f2_frame_header_s
{
  uint16_t size;
  uint16_t checksum;
  uint8_t channel;
  uint8_t data_offset;
  uint8_t sequence;
};
typedef struct f2_frame_header_s f2_frame_header_t;

struct interface_header_s { uint8_t *base, *data; int len; };
struct bcmf_interface_frame_s
{
  struct interface_header_s header;
  uint8_t *data;
  bool tx;
  int list_entry;
};
typedef struct bcmf_interface_frame_s bcmf_interface_frame_t;
struct gspi_dev_s
{
  struct bcmf_dev_s *priv;
  int (*read)(struct gspi_dev_s *, bool, int, int, int, uint32_t *);
  int (*write)(struct gspi_dev_s *, bool, int, int, int, uint32_t *);
  int (*interrupt_enable)(struct gspi_dev_s *, bool);
};
typedef struct gspi_dev_s gspi_dev_t;
struct bcmf_gspi_dev_s
{
  gspi_dev_t *gspi;
  int queue_lock;
  uint8_t tx_seq, max_seq;
  int rx_queue, tx_queue;
  uint32_t trace_status;
  uint32_t trace_ticket;
  bool trace_written;
  bool irq_pending;
  bool thread_run;
  int thread_signal;
};
typedef struct bcmf_gspi_dev_s bcmf_gspi_dev_t;

#define F2_FRAME_CONTROL_CHANNEL 0
#define F2_FRAME_EVENT_CHANNEL 1
#define F2_FRAME_DATA_CHANNEL 2
#define FC_UPDATE_PKT_LENGTH 0x7fff
#include "cyw-ready.h"
#define nxmutex_lock(lock) (0)
#define nxmutex_unlock(lock) ((void)0)
#define list_add_tail(queue, entry) (++(*(queue)))
#define list_is_empty(queue) (*(queue) == 0)
#define list_remove_head_type(queue, type, member) take_tx_frame()

static struct bcmf_interface_frame_s *active_iframe;
static int dequeue_calls;
static struct bcmf_interface_frame_s *take_tx_frame(void)
{
  dequeue_calls++;
  return active_iframe;
}

static int event_count;
static enum bcmf_trace_stage_e events[16];
static const uint8_t *observed_data[16];
static size_t observed_len[16];
static int observed_result[16];
#ifdef CONFIG_IEEE80211_BROADCOM_FRAME_TRACE
static struct bcmf_trace_bus_s observed_bus;
static int bus_present;
static int post_calls, post_result;
static uint32_t post_ticket, post_flags;
static struct net_driver_s *expected_post_dev;
#endif
static struct net_driver_s *observed_dev[16];
static int *rx_queue_probe;
static int rx_queue_at_hook = -1;
static int write_returned;
static int frame_freed;
static int free_calls;
static uint32_t rx_events[BCMF_TRACE_RX_COUNT];
#ifdef CONFIG_IEEE80211_BROADCOM_FRAME_TRACE
static uint32_t daemon_events[4], daemon_flags, daemon_length;
static int daemon_result;
void bcmf_trace_daemon(struct net_driver_s *dev, enum bcmf_trace_daemon_e event,
                       int result, uint32_t flags, uint32_t length)
{
  assert(dev != NULL && (unsigned int)event < 4);
  daemon_events[event]++;
  if (event == BCMF_TRACE_DAEMON_POLL)
    {
      daemon_result = result;
      daemon_flags = flags;
      daemon_length = length;
    }
}

void bcmf_trace_rx(struct net_driver_s *dev, enum bcmf_trace_rx_e event)
{
  assert(dev != NULL && (unsigned int)event < BCMF_TRACE_RX_COUNT);
  rx_events[event]++;
}
#endif
static void record(enum bcmf_trace_stage_e stage, struct net_driver_s *dev,
                   const uint8_t *data, size_t len, int result)
{
  assert(event_count < 16);
  events[event_count] = stage;
  observed_dev[event_count] = dev;
  observed_data[event_count] = data;
  observed_len[event_count] = len;
  observed_result[event_count++] = result;
  if (stage == BCMF_TRACE_BUS_RX && rx_queue_probe != NULL)
    rx_queue_at_hook = *rx_queue_probe;
  if (stage == BCMF_TRACE_BUS_TX)
    assert(write_returned && !frame_freed);
}

#ifdef CONFIG_IEEE80211_BROADCOM_FRAME_TRACE
void bcmf_trace_status(struct net_driver_s *dev, uint32_t ticket,
                       int result, uint32_t flags)
{
  assert(dev == expected_post_dev);
  assert(write_returned && frame_freed);
  post_calls++;
  post_result = result;
  post_ticket = ticket;
  post_flags = flags;
}

void bcmf_trace_frame(struct net_driver_s *dev, enum bcmf_trace_stage_e stage,
                      const uint8_t *data, size_t len, int result,
                      const struct bcmf_trace_bus_s *bus)
{
  assert(bus == NULL);
  record(stage, dev, data, len, result);
}
void bcmf_bdc_trace_frame(struct bcmf_dev_s *priv,
                          enum bcmf_trace_stage_e stage,
                          const uint8_t *data, size_t len, int result,
                          const struct bcmf_trace_bus_s *bus)
{
  if (stage == BCMF_TRACE_BUS_TX)
    {
      assert(bus != NULL);
      observed_bus = *bus;
      bus_present++;
    }
  else
    {
      assert(bus == NULL);
    }
  record(stage, &priv->bc_dev, data, len, result);
}
#endif

static int tx_return;
static int bdc_calls;
static int bcmf_bdc_transmit_frame(struct bcmf_dev_s *priv,
                                   struct bcmf_frame_s *frame)
{
  (void)priv; (void)frame;
  bdc_calls++;
#ifdef CONFIG_IEEE80211_BROADCOM_FRAME_TRACE
  assert(event_count == 1 && events[0] == BCMF_TRACE_NET_TX);
#else
  assert(event_count == 0);
#endif
  return tx_return;
}
static void bcmf_netdev_notify_tx(struct bcmf_dev_s *priv) { (void)priv; }
static struct bcmf_frame_s *rx_frame;
static int rx_taken;
static struct bcmf_frame_s *bcmf_bdc_rx_frame(struct bcmf_dev_s *priv)
{
  (void)priv;
  return rx_taken++ == 0 ? rx_frame : NULL;
}
static void free_frame(struct bcmf_dev_s *priv, struct bcmf_frame_s *frame)
{
  (void)priv; (void)frame;
}
static int ipv4_order;
static void ipv4_input(struct net_driver_s *dev)
{
  ipv4_order = event_count;
  dev->d_len = 0;
}

static struct bcmf_interface_frame_s *allocated_iframe;
static int abort_calls;
static struct bcmf_interface_frame_s *bcmf_interface_allocate_frame(
    struct bcmf_dev_s *priv, bool a, bool b)
{
  (void)priv; (void)a; (void)b;
  return allocated_iframe;
}
static void bcmf_interface_free_frame(struct bcmf_dev_s *priv,
                                      struct bcmf_interface_frame_s *frame)
{
  (void)priv; (void)frame;
  frame_freed = 1;
  free_calls++;
}
static int process_f2_frame_header(bcmf_gspi_dev_t *bus,
                                   f2_frame_header_t *header)
{
  (void)bus; (void)header;
  return header->data_offset < sizeof(*header) ? -ENXIO : OK;
}
static void f2_frame_rx_fail(bcmf_gspi_dev_t *bus, bool retry)
{
  (void)bus; (void)retry;
  abort_calls++;
}
static void bcmf_hexdump(uint8_t *data, size_t len, int offset)
{
  (void)data; (void)len; (void)offset;
}
static int bcmf_cdc_process_control_frame(struct bcmf_dev_s *priv,
                                          struct interface_header_s *frame)
{
  (void)priv; (void)frame;
  return OK;
}
static int bcmf_bdc_process_event_frame(struct bcmf_dev_s *priv,
                                        struct interface_header_s *frame)
{
  (void)priv; (void)frame;
  return OK;
}
#define BCMFWORK 0
#define netdev_lock(dev) ((void)(dev))
#define netdev_unlock(dev) ((void)(dev))
static int queue_result, queue_calls;
static bool work_ready = true;
static bool work_available(int *work) { (void)work; return work_ready; }
static int work_queue(int queue, int *work, void (*worker)(void *), void *arg, int delay)
{
  assert(queue == BCMFWORK && work && worker && arg && delay == 0);
  queue_calls++;
  return queue_result;
}
static int gspi_read_result, gspi_write_result;
static uint32_t read_status;
static int status_reads;
static int status_fail_at;
static int write_calls;
static bool status_on_read_failure;
static int sem_value, sem_posts, interrupt_calls;
static bcmf_gspi_dev_t *worker_bus;
static int timed_waits, untimed_waits, lowpower_calls;
static bool bt_running, last_interrupt_enable;
static bool worker_fault, stop_after_bt;
static int bt_polls, interrupt_reads, interrupt_writes;
static int interrupt_read_result, interrupt_write_result;
static uint16_t interrupt_flags;
static bool setup_mode;
static int diagnostics_calls;
static bool scripted_worker;
static unsigned int scripted_iteration, scripted_count;
static uint32_t scripted_status[8];
static bool scripted_status_valid[8];

#if defined(CONFIG_DEBUG_WIRELESS_ERROR) && \
    defined(CONFIG_IEEE80211_INFINEON_CYW43439_BLUETOOTH)
static void bcmf_gspi_bus_diagnostics(bcmf_gspi_dev_t *gbus)
{
  assert(gbus == worker_bus);
  diagnostics_calls++;
}
#endif
static uint16_t setup_clear, setup_enable;
static int nxsem_get_value(int *sem, int *value)
{ (void)sem; *value = sem_value; return OK; }
static int nxsem_post(int *sem)
{ (void)sem; sem_posts++; return OK; }
static int interrupt_enable(gspi_dev_t *gspi, bool enable)
{ (void)gspi; last_interrupt_enable = enable; interrupt_calls++; return OK; }
static int bcmf_gspi_bus_lowpower(bcmf_gspi_dev_t *bus, bool enable, bool force)
{ assert(bus == worker_bus); (void)enable; (void)force; lowpower_calls++; return OK; }
static bool bcmf_gspi_bt_active(bcmf_gspi_dev_t *bus)
{ assert(bus == worker_bus); return bt_running; }
static int bcmf_gspi_bt_poll(bcmf_gspi_dev_t *bus)
{
  assert(bus == worker_bus);
  bt_polls++;
  if (stop_after_bt) bus->thread_run = false;
  return 0;
}
static int nxsem_tickwait_uninterruptible(int *sem, int ticks)
{
  assert(sem == &worker_bus->thread_signal && last_interrupt_enable);
  assert(ticks == (scripted_worker ?
                   (scripted_status_valid[scripted_iteration] &&
                    (scripted_status[scripted_iteration] == 0 ||
                     SCRIPTED_FAULT_WAIT_LONG) ?
                    BCMF_GSPI_LOWPOWER_TIMEOUT_TICK : MSEC2TICK(10) + 1) :
                   worker_fault ? MSEC2TICK(10) + 1 :
                   bt_running ? MSEC2TICK(5) + 1 : BCMF_GSPI_LOWPOWER_TIMEOUT_TICK));
  timed_waits++;
  if (scripted_worker && ++scripted_iteration == scripted_count)
    worker_bus->thread_run = false;
  if (bt_running || worker_fault) worker_bus->thread_run = false;
  return -ETIMEDOUT;
}
static int nxsem_wait_uninterruptible(int *sem)
{
  assert(sem == &worker_bus->thread_signal && last_interrupt_enable);
  untimed_waits++;
  if (scripted_worker && ++scripted_iteration == scripted_count)
    worker_bus->thread_run = false;
  if (!scripted_worker) worker_bus->thread_run = false;
  return OK;
}
static int bus_read(gspi_dev_t *g, bool a, int b, int c, int len, uint32_t *p)
{
  (void)g;
  int result = gspi_read_result;
  if (b == gspi_f0_bus && c == CYW_REG_STATUS)
    {
      assert(a && len == 4);
      status_reads++;
      if (scripted_worker)
        {
          unsigned int iteration = scripted_iteration;
          assert(iteration < scripted_count);
          result = scripted_status_valid[iteration] ? OK : -EIO;
          if (result == OK) *p = scripted_status[iteration];
        }
      else
        {
          if (status_reads == status_fail_at) result = -EIO;
          if (result == 0 || status_on_read_failure) *p = read_status;
        }
    }
  else if (b == gspi_f0_bus && c == CYW_REG_INTERRUPT)
    {
      assert(a && len == 2);
      interrupt_reads++;
      *p = interrupt_flags; /* Also model partial DMA data on an error. */
      return interrupt_read_result;
    }
  return result;
}
static int bus_write(gspi_dev_t *g, bool a, int b, int c, int len, uint32_t *p)
{
  (void)a; (void)b; (void)c; (void)len; (void)p;
  if (setup_mode)
    {
      assert(a && b == gspi_f0_bus && len == 2);
      if (c == CYW_REG_INTERRUPT) setup_clear = (uint16_t)*p;
      else { assert(c == CYW_REG_INTR_ENA); setup_enable = (uint16_t)*p; }
      return OK;
    }
  if (b == gspi_f0_bus && c == CYW_REG_INTERRUPT)
    {
      assert(a && len == 2 && *p == interrupt_flags);
      interrupt_writes++;
      return interrupt_write_result;
    }
  ((bcmf_gspi_dev_t *)g->priv->bus)->trace_status ^= CYW_REG_STATUS_F2_RECEIVE_RDY;
  write_returned = 1;
  write_calls++;
  return gspi_write_result;
}

#include "bcmf-frame-trace-calls-functions.h"

static void reset_observations(void)
{
  event_count = 0;
  ipv4_order = -1;
}

int main(void)
{
  uint8_t payload[80] = {0};
  struct bcmf_dev_s priv = {0};
  struct bcmf_bus_s bus = {.free_frame = free_frame};
  struct bcmf_frame_s frame = {.base = payload, .data = payload + 8};
  priv.bus = &bus;
  priv.bc_dev.d_buf = payload + 8;
  priv.bc_dev.d_len = 42;

  tx_return = OK;
  assert(bcmf_transmit(&priv, &frame) == OK);
#ifdef CONFIG_IEEE80211_BROADCOM_FRAME_TRACE
  assert(event_count == 1 && events[0] == BCMF_TRACE_NET_TX);
  assert(observed_data[0] == payload + 8 && observed_len[0] == 42);
  assert(observed_result[0] == 0 && observed_dev[0] == &priv.bc_dev);
  tx_return = -EIO;
  reset_observations();
  assert(bcmf_transmit(&priv, &frame) == -EIO);
  assert(event_count == 1 && events[0] == BCMF_TRACE_NET_TX);
  assert(observed_data[0] == payload + 8 && observed_len[0] == 42);
  assert(observed_result[0] == 0 && bdc_calls == 2);
#else
  assert(event_count == 0 && frame.len == 50);
#endif

  /* NET_RX must observe the borrowed Ethernet frame before ipv4_input. */
  struct eth_header_s *eth = (struct eth_header_s *)payload;
  eth->type = ETHTYPE_IP;
  struct bcmf_frame_s incoming = {
    .base = payload, .data = payload, .len = sizeof(payload)
  };
  rx_frame = &incoming;
  rx_taken = 0;
  priv.bc_bifup = true;
  priv.bc_dev.d_len = 0;
  reset_observations();
  bcmf_receive(&priv);
#ifdef CONFIG_IEEE80211_BROADCOM_FRAME_TRACE
  assert(event_count == 1 && events[0] == BCMF_TRACE_NET_RX);
  assert(observed_data[0] == payload && observed_len[0] == sizeof(payload));
  assert(observed_result[0] == 0 && ipv4_order == 1);
#else
  assert(event_count == 0 && ipv4_order == 0);
#endif

  gspi_dev_t gspi = {.priv = &priv, .read = bus_read, .write = bus_write,
                     .interrupt_enable = interrupt_enable};
  bcmf_gspi_dev_t gbus = {.gspi = &gspi, .max_seq = 10, .tx_queue = 1};
  uint32_t observed_status = 0;
  priv.bus = (struct bcmf_bus_s *)&gbus;
  assert(bcmf_gspi_thread_isr(0, NULL, &gbus) == OK);
  assert(gbus.irq_pending && sem_posts == 1 && interrupt_calls == 1);
  assert(!last_interrupt_enable);
  sem_value = 1;
  assert(bcmf_gspi_thread_isr(0, NULL, &gbus) == OK);
  assert(sem_posts == 1 && interrupt_calls == 2);
#ifdef CONFIG_IEEE80211_BROADCOM_FRAME_TRACE
  assert(daemon_events[BCMF_TRACE_DAEMON_IRQ] == 2);
  expected_post_dev = &priv.bc_dev;
  read_status = CYW_REG_STATUS_F2_PKT_AVAIL | (64u << CYW_REG_STATUS_F2_PKT_LEN_SHIFT);
  int old_post_calls = post_calls;
  status_reads = 0;
  assert(bcmf_gspi_read_status(&priv, &observed_status, false) == OK);
  assert(observed_status == read_status);
  assert(status_reads == 1 && post_calls == old_post_calls);
  assert(daemon_events[BCMF_TRACE_DAEMON_POLL] == 1 && daemon_result == 0);
  assert(daemon_flags == 32 && daemon_length == 64);
  gspi_read_result = 7;
  status_on_read_failure = true;
  observed_status = 0x1234;
  assert(bcmf_gspi_read_status(&priv, &observed_status, false) == -EIO);
  assert(observed_status == 0x1234);
  assert(daemon_result == -EIO && daemon_flags == 0 && daemon_length == 0);
  status_on_read_failure = false;
  gspi_read_result = OK;
#endif
  struct bcmf_interface_frame_s tx = {0};
  tx.header.base = payload;
  tx.header.data = payload + 4;
  tx.header.len = 50;
  tx.data = payload;
  tx.tx = true;
  active_iframe = &tx;
  f2_frame_header_t *txheader = (f2_frame_header_t *)payload;
  txheader->channel = F2_FRAME_DATA_CHANNEL;
  /* Empty and backpressured sends leave the queued frame and sequence alone. */
  uint8_t saved_seq = (uint8_t)gbus.tx_seq;
  int old_dequeues = dequeue_calls, old_frees = free_calls;
  gbus.tx_queue = 0;
  status_reads = 0;
  assert(bcmf_gspi_send_f2_frame(&priv) == -ENODATA);
  assert(write_calls == 0);
  assert(status_reads == 0 && dequeue_calls == old_dequeues && free_calls == old_frees);
  gbus.tx_queue = 1;
  gbus.tx_seq = gbus.max_seq;
  assert(bcmf_gspi_send_f2_frame(&priv) == -EAGAIN);
  assert(write_calls == 0);
  assert(status_reads == 0 && dequeue_calls == old_dequeues && free_calls == old_frees);
  assert(gbus.tx_seq == gbus.max_seq && gbus.tx_queue == 1);
  gbus.tx_seq = saved_seq;

  read_status = 0;
  gspi_read_result = OK;
  status_reads = 0;
  gbus.trace_written = false;
  assert(bcmf_gspi_send_f2_frame(&priv) == -EAGAIN);
  assert(write_calls == 0);
  assert(status_reads == 1 && dequeue_calls == old_dequeues && free_calls == old_frees);
  assert(gbus.tx_seq == saved_seq && gbus.tx_queue == 1 && !gbus.trace_written);

  for (int read_failure = -1; read_failure <= 1; read_failure += 2)
    {
      gspi_read_result = read_failure < 0 ? -ETIMEDOUT : 7;
      read_status = CYW_REG_STATUS_F2_RECEIVE_RDY;
      status_on_read_failure = true;
      status_reads = 0;
      assert(bcmf_gspi_send_f2_frame(&priv) == (read_failure < 0 ? -ETIMEDOUT : -EIO));
      assert(write_calls == 0);
      assert(status_reads == 1 && dequeue_calls == old_dequeues && free_calls == old_frees);
      assert(gbus.tx_seq == saved_seq && gbus.tx_queue == 1);
    }
  status_on_read_failure = false;

  for (int failure = 0; failure < 2; failure++)
    {
      event_count = 0;
      write_returned = 0;
      frame_freed = 0;
      gspi_write_result = failure ? -EIO : OK;
      gspi_read_result = OK;
      read_status = CYW_REG_STATUS_F2_RECEIVE_RDY;
      gbus.trace_status = failure ? 0 : CYW_REG_STATUS_F2_RECEIVE_RDY;
      gbus.trace_written = false;
      assert(bcmf_gspi_send_f2_frame(&priv) == gspi_write_result);
#ifdef CONFIG_IEEE80211_BROADCOM_FRAME_TRACE
      assert(event_count == 1 && events[0] == BCMF_TRACE_BUS_TX);
      assert(observed_data[0] == payload + 4 && observed_len[0] == 46);
      assert(observed_result[0] == gspi_write_result);
      assert(bus_present == failure + 1);
      assert(observed_bus.sequence == failure && observed_bus.credit == 10);
      assert(observed_bus.cached_ready);
      assert(observed_bus.ticket == (uint32_t)failure + 1 && gbus.trace_written);
#endif
      read_status = CYW_STATUS_F2_RECEIVE_RDY | CYW_STATUS_FIFO_OVERFLOW |
                    CYW_STATUS_CMD_DATA_ERROR | CYW_STATUS_F2_PKT_AVAIL |
                    CYW_STATUS_FIFO_UNDERFLOW | CYW_STATUS_DATA_NOT_AVAIL;
      status_reads = 0;
#ifdef CONFIG_IEEE80211_BROADCOM_FRAME_TRACE
      gspi_read_result = failure ? -EIO : OK;
      observed_status = 0x1234;
      assert(bcmf_gspi_read_status(&priv, &observed_status, true) == gspi_read_result);
      assert(observed_status == (failure ? 0x1234 : read_status));
      assert(status_reads == 1 && post_calls == failure + 1);
      assert(post_result == gspi_read_result && post_ticket == observed_bus.ticket);
      assert(post_flags == (failure ? 0 : 63));
#else
      assert(event_count == 0);
      gspi_read_result = OK;
      assert(bcmf_gspi_read_reg_32(&gspi, gspi_f0_bus, CYW_REG_STATUS,
                                  &observed_status) == OK);
      assert(observed_status == read_status);
      assert(status_reads == 1);
#endif
      gbus.tx_queue = 1;
    }

  gspi_read_result = OK;
  read_status = CYW_REG_STATUS_F2_RECEIVE_RDY;
  gbus.tx_seq = 255;
  gbus.max_seq = 4;
  gbus.tx_queue = 1;
  gspi_write_result = OK;
  write_returned = 0;
  frame_freed = 0;
  event_count = 0;
  assert(bcmf_gspi_send_f2_frame(&priv) == OK);
  assert(txheader->sequence == 255 && gbus.tx_seq == 0);
#ifdef CONFIG_IEEE80211_BROADCOM_FRAME_TRACE
  assert(observed_bus.sequence == 255);
#endif
  gbus.max_seq = 10;
  txheader->channel = F2_FRAME_CONTROL_CHANNEL;
  event_count = 0;
  gspi_write_result = OK;
  write_returned = 0;
  frame_freed = 0;
  assert(bcmf_gspi_send_f2_frame(&priv) == OK);
  assert(event_count == 0);

#ifdef CONFIG_IEEE80211_BROADCOM_FRAME_TRACE
  int calls = post_calls;
  gbus.trace_written = false;
  gspi_read_result = OK;
  assert(bcmf_gspi_read_status(&priv, &observed_status, true) == OK &&
         observed_status == read_status && post_calls == calls);
  gbus.trace_written = true;
  gspi_read_result = 7; /* Backend DMA status can be positive, not only errno. */
  assert(bcmf_gspi_read_status(&priv, &observed_status, true) == -EIO &&
         post_result == -EIO && post_flags == 0);
#endif

  struct bcmf_interface_frame_s incoming_iframe = {0};
  incoming_iframe.header.base = payload;
  incoming_iframe.header.data = payload;
  incoming_iframe.header.len = sizeof(payload);
  incoming_iframe.data = payload;
  allocated_iframe = &incoming_iframe;
  f2_frame_header_t *rxheader = (f2_frame_header_t *)payload;
  rxheader->size = 40;
  rxheader->checksum = (uint16_t)~rxheader->size;
  rxheader->channel = F2_FRAME_DATA_CHANNEL;
  rxheader->data_offset = sizeof(*rxheader);
  gspi_read_result = OK;
  event_count = 0;
  rx_queue_probe = &gbus.rx_queue;
  rx_queue_at_hook = -1;
  assert(bcmf_gspi_read_f2_frame(&priv, 40) == OK);
#ifdef CONFIG_IEEE80211_BROADCOM_FRAME_TRACE
  assert(event_count == 1 && events[0] == BCMF_TRACE_BUS_RX);
  assert(observed_data[0] == payload + sizeof(*rxheader));
  assert(observed_len[0] == 40 - sizeof(*rxheader));
  assert(observed_result[0] == 0 && gbus.rx_queue == 1);
  assert(rx_queue_at_hook == 0);
#else
  assert(event_count == 0 && gbus.rx_queue == 1);
#endif
  rxheader->checksum = 0;
  event_count = 0;
  assert(bcmf_gspi_read_f2_frame(&priv, 40) == -EINVAL);
  assert(event_count == 0);

  /* Exercise every early receive outcome in the actual driver definition. */
  assert(bcmf_gspi_read_f2_frame(&priv, 1) == -EINVAL);
  allocated_iframe = NULL;
  assert(bcmf_gspi_read_f2_frame(&priv, 40) == -EAGAIN);
  allocated_iframe = &incoming_iframe;
  incoming_iframe.header.len = 30;
  assert(bcmf_gspi_read_f2_frame(&priv, 40) == -ENOMEM);
  incoming_iframe.header.len = sizeof(payload);
  gspi_read_result = -EIO;
  assert(bcmf_gspi_read_f2_frame(&priv, 40) == -EIO);
  gspi_read_result = OK;
  rxheader->size = 0;
  assert(bcmf_gspi_read_f2_frame(&priv, 40) == OK);
  rxheader->size = 40;
  rxheader->checksum = (uint16_t)~rxheader->size;
  rxheader->data_offset = 0;
  assert(bcmf_gspi_read_f2_frame(&priv, 40) == -EINVAL);
  rxheader->data_offset = sizeof(*rxheader);
  for (unsigned int channel = 0; channel < 4; channel++)
    {
      if (channel == F2_FRAME_DATA_CHANNEL) continue; /* Already tested. */
      incoming_iframe.header.data = payload;
      incoming_iframe.header.len = sizeof(payload);
      rxheader->channel = channel;
      assert(bcmf_gspi_read_f2_frame(&priv, 40) == (channel == 3 ? -EINVAL : OK));
    }

  /* Coalescing is distinct from a failed queue operation. No new retries. */
  int calls_before = queue_calls;
  work_ready = false;
  bcmf_netdev_notify_rx(&priv);
  assert(queue_calls == calls_before);
  work_ready = true;
  queue_result = -EBUSY;
  bcmf_netdev_notify_rx(&priv);
  assert(queue_calls == calls_before + 1);
  queue_result = OK;
  rx_frame = NULL;
  rx_taken = 0;
  uint8_t *old_buffer = priv.bc_dev.d_buf;
  bcmf_rxpoll_work(&priv);
  assert(priv.bc_dev.d_buf == old_buffer);
#ifdef CONFIG_IEEE80211_BROADCOM_FRAME_TRACE
  assert(rx_events[BCMF_TRACE_RX_ATTEMPT] == 11);
  for (unsigned int i = BCMF_TRACE_RX_SHORT; i <= BCMF_TRACE_RX_UNKNOWN; i++)
    assert(rx_events[i] == 1);
  assert(rx_events[BCMF_TRACE_RX_NOTIFY] == 3);
  assert(rx_events[BCMF_TRACE_RX_WORKER] == 1);
  assert(rx_events[BCMF_TRACE_RX_COALESCED] == 1);
  assert(rx_events[BCMF_TRACE_RX_QUEUEERROR] == 1);
#else
  for (unsigned int i = 0; i < BCMF_TRACE_RX_COUNT; i++) assert(rx_events[i] == 0);
#endif

  /* Execute the actual daemon's Bluetooth and timed-to-lowpower waits.
   * Existing status reads and wait policy must remain unchanged in both
   * diagnostic-enabled and diagnostic-disabled builds.
   */
  worker_bus = &gbus;
  gbus.tx_queue = 0;
  gbus.irq_pending = false;
  gspi_read_result = OK;
  read_status = 0;
  char pointer[32];
  snprintf(pointer, sizeof(pointer), "%lx", (unsigned long)(uintptr_t)&priv);
  char *argv[] = {"bcmf-gspi-thread", pointer, NULL};
  for (int active = 0; active < 2; active++)
    {
      bt_running = active;
      timed_waits = untimed_waits = lowpower_calls = status_reads = 0;
#ifdef CONFIG_IEEE80211_BROADCOM_FRAME_TRACE
      uint32_t old_waits = daemon_events[BCMF_TRACE_DAEMON_WAIT];
      uint32_t old_wakes = daemon_events[BCMF_TRACE_DAEMON_WAKE];
#endif
      assert(bcmf_gspi_thread(2, argv) == 0 && !gbus.thread_run);
      assert(timed_waits == 1 && untimed_waits == (active ? 0 : 1));
      /* No redundant post-send read when no frame was transmitted. */
      assert(status_reads == (active ? 1 : 2));
      assert(lowpower_calls == (active ? 1 : 3));
#ifdef CONFIG_IEEE80211_BROADCOM_FRAME_TRACE
      assert(daemon_events[BCMF_TRACE_DAEMON_WAIT] - old_waits == (active ? 1u : 2u));
      assert(daemon_events[BCMF_TRACE_DAEMON_WAKE] - old_wakes == (active ? 1u : 2u));
#endif
    }

  /* Failed or partially written register reads must not authorize a transfer
   * or lose the pending interrupt. Bluetooth still gets its bounded turn.
   */
  bt_running = true;
  worker_fault = true;
  gbus.tx_queue = 1;
  read_status = CYW_REG_STATUS_F2_PKT_AVAIL | CYW_REG_STATUS_F2_RECEIVE_RDY |
                (40u << CYW_REG_STATUS_F2_PKT_LEN_SHIFT);
  status_on_read_failure = true;
  for (int result = -1; result <= 1; result += 2)
    {
      gspi_read_result = result < 0 ? -ETIMEDOUT : 7;
      gbus.irq_pending = false;
      timed_waits = bt_polls = 0;
      int prior_dequeues = dequeue_calls, prior_aborts = abort_calls;
      int prior_writes = write_calls;
      assert(bcmf_gspi_thread(2, argv) == OK && !gbus.thread_run);
      assert(timed_waits == 1 && bt_polls == 1);
      assert(dequeue_calls == prior_dequeues && write_calls == prior_writes);
      assert(abort_calls == prior_aborts);
    }
  status_on_read_failure = false;
  gspi_read_result = OK;

  /* A failed post-send read cannot authorize another queued write, even if
   * the backend left a READY bit in the partially written status word.
   */
  read_status = CYW_REG_STATUS_F2_RECEIVE_RDY;
  status_reads = 0;
  status_fail_at = 3; /* Initial poll, pre-send readiness, then post-send read. */
  status_on_read_failure = true;
  timed_waits = bt_polls = 0;
  gbus.tx_seq = 0;
  gbus.max_seq = 10;
  txheader->channel = F2_FRAME_DATA_CHANNEL;
  frame_freed = write_returned = 0;
  event_count = 0;
  int prior_writes = write_calls;
  assert(bcmf_gspi_thread(2, argv) == OK && !gbus.thread_run);
  assert(write_calls == prior_writes + 1 && gbus.tx_seq == 1);
  assert(status_reads == 3 && timed_waits == 1 && bt_polls == 1);
  status_fail_at = 0;
  status_on_read_failure = false;

  read_status = CYW_REG_STATUS_F2_RECEIVE_RDY;
  interrupt_flags = 32;
  for (int failure = 0; failure < 2; failure++)
    {
      gbus.irq_pending = true;
      interrupt_read_result = failure ? OK : -EIO;
      interrupt_write_result = failure ? -EIO : OK;
      interrupt_reads = interrupt_writes = 0;
      timed_waits = bt_polls = 0;
      int prior_dequeues = dequeue_calls;
      assert(bcmf_gspi_thread(2, argv) == OK && !gbus.thread_run);
      assert(gbus.irq_pending && interrupt_reads == 1);
      assert(interrupt_writes == failure);
      assert(dequeue_calls == prior_dequeues && timed_waits == 1 && bt_polls == 1);
    }

  /* A malformed zero-length F2 record is flushed, not a daemon exit. */
  worker_fault = false;
  stop_after_bt = true; /* Explicit external shutdown only after BT is reached. */
  gbus.tx_queue = 0;
  gbus.irq_pending = true;
  interrupt_read_result = interrupt_write_result = OK;
  interrupt_reads = interrupt_writes = 0;
  read_status = CYW_REG_STATUS_F2_PKT_AVAIL;
  int prior_aborts = abort_calls;
  timed_waits = bt_polls = 0;
  assert(bcmf_gspi_thread(2, argv) == OK && !gbus.thread_run);
  assert(abort_calls == prior_aborts + 1 && bt_polls == 1);
  assert(!gbus.irq_pending && interrupt_reads == 1 && interrupt_writes == 1);
  assert(timed_waits == 0);

  /* Error IRQs are required; DATA_NOT_AVAIL on an empty bus is not an error. */
  setup_mode = true;
  assert(bcmf_gspi_setup_interrupts(&gbus) == OK);
  setup_mode = false;
#ifdef EXPECT_BASELINE_ERROR_IRQS
  assert(setup_enable == CYW_REG_INTR_ENA_F2_PKT_AVAIL);
  assert(!(setup_clear & CYW_REG_INTERRUPT_FIFO_OVERFLOW));
#else
  assert(setup_enable == (CYW_REG_INTR_ENA_F2_PKT_AVAIL | BCMF_GSPI_ERROR_INTERRUPTS));
  assert(setup_clear == (CYW_REG_INTERRUPT_DATA_NOT_AVAIL | BCMF_GSPI_ERROR_INTERRUPTS));
  assert(!(setup_enable & CYW_REG_INTERRUPT_DATA_NOT_AVAIL));
#endif

  /* Polling sees a bus error without an RX interrupt. Preserve the queued
   * frame, retain failed acknowledgement and give Bluetooth its turn.
   */
  const uint32_t errors[] = {CYW_REG_STATUS_FIFO_OVERFLOW,
                            CYW_REG_STATUS_FIFO_UNDERFLOW,
                            CYW_REG_STATUS_CMD_DATA_ERROR, 0, 0};
  const uint16_t irqs[] = {CYW_REG_INTERRUPT_FIFO_OVERFLOW,
                          CYW_REG_INTERRUPT_FIFO_UNDERFLOW,
                          CYW_REG_INTERRUPT_COMMAND_ERROR,
                          CYW_REG_INTERRUPT_DATA_ERROR,
                          CYW_REG_INTERRUPT_F1_OVERFLOW};
  for (unsigned int error = 0; error < 5; error++)
    for (unsigned int failure = 0; failure < 3; failure++)
      {
        gbus.tx_queue = 1;
        gbus.tx_seq = 0;
        gbus.max_seq = 10;
        gbus.irq_pending = error >= 3;
#ifdef EXPECT_BASELINE_ERROR_IRQS
        /* Old code handles failed explicit IRQ acknowledgement correctly;
         * the regression is absent error IRQs and trusting pre-ack READY.
         */
        if (error >= 3 && failure != 0) continue;
#endif
        txheader->channel = F2_FRAME_CONTROL_CHANNEL;
        read_status = errors[error] | CYW_REG_STATUS_F2_RECEIVE_RDY;
        interrupt_flags = irqs[error];
        interrupt_read_result = failure == 1 ? -ETIMEDOUT : OK;
        interrupt_write_result = failure == 2 ? -EIO : OK;
        timed_waits = bt_polls = 0;
        stop_after_bt = true;
#ifdef EXPECT_BASELINE_ERROR_IRQS
        worker_fault = false;
#else
        worker_fault = true;
#endif
        int prior_reads = interrupt_reads, prior_acks = interrupt_writes;
        int prior_writes = write_calls, prior_frees = free_calls;
        int prior_dequeues = dequeue_calls;
        frame_freed = write_returned = 0;
        assert(bcmf_gspi_thread(2, argv) == OK && !gbus.thread_run);
#ifdef EXPECT_BASELINE_ERROR_IRQS
        assert(interrupt_reads == prior_reads + (error >= 3));
        assert(interrupt_writes == prior_acks + (error >= 3));
        assert(write_calls > prior_writes && gbus.tx_seq > 0);
        assert(free_calls - prior_frees == write_calls - prior_writes);
        assert(dequeue_calls - prior_dequeues == write_calls - prior_writes);
#else
        assert(interrupt_reads == prior_reads + 1);
        assert(interrupt_writes == prior_acks + (failure != 1));
        assert(gbus.irq_pending == (failure != 0));
        assert(write_calls == prior_writes && free_calls == prior_frees);
        assert(dequeue_calls == prior_dequeues && gbus.tx_queue == 1 && gbus.tx_seq == 0);
#endif
        assert(timed_waits == 1 && bt_polls == 1);
      }

  /* Exercise fault burst suppression and valid-status rearming in the actual
   * worker loop. A failed status read must leave the previous fault latched.
   */
  gbus.tx_queue = 0;
  gbus.irq_pending = false;
  interrupt_flags = 0;
  worker_fault = false;
  bt_running = false;
  stop_after_bt = false;
  scripted_status[0] = CYW_REG_STATUS_FIFO_OVERFLOW;
  scripted_status[1] = CYW_REG_STATUS_FIFO_OVERFLOW;
  scripted_status[2] = 0;
  scripted_status[3] = CYW_REG_STATUS_FIFO_UNDERFLOW;
  scripted_status[4] = 0;
  scripted_status[5] = CYW_REG_STATUS_CMD_DATA_ERROR;
  scripted_status[6] = 0;
  scripted_status[7] = CYW_REG_STATUS_FIFO_OVERFLOW;
  for (unsigned int i = 0; i < 8; i++) scripted_status_valid[i] = true;
  scripted_status_valid[4] = false;
  scripted_iteration = 0;
  scripted_count = 8;
  diagnostics_calls = 0;
  scripted_worker = true;
  worker_bus = &gbus;
  gbus.thread_run = false;
  assert(bcmf_gspi_thread(2, argv) == OK && !gbus.thread_run);
  scripted_worker = false;
#if defined(CONFIG_DEBUG_WIRELESS_ERROR) && \
    defined(CONFIG_IEEE80211_INFINEON_CYW43439_BLUETOOTH) && \
    !defined(EXPECT_BASELINE_ERROR_IRQS)
  assert(diagnostics_calls == 3);
#else
  assert(diagnostics_calls == 0);
#endif
  assert(scripted_iteration == scripted_count && status_reads >= (int)scripted_count);
  return 0;
}

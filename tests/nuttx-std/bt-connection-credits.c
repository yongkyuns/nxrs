/* SPDX-License-Identifier: MIT */
/* Host witness for extracted NuttX Bluetooth ACL credit accounting. */
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define FAR
#define CODE
#define UNUSED(value) ((void)(value))
#define OK 0
#define BT_CONN_TX "bt-tx"
#define BT_DUMMY 0
#define BT_NORMAL_PRIO 0
#define CONFIG_BLUETOOTH_TXCONN_NMSGS 4
#define CONFIG_BLUETOOTH_TXCONN_PRIORITY 100
#define CONFIG_BLUETOOTH_TXCONN_STACKSIZE 1024
#define O_RDWR 0
#define O_CREAT 0
#define O_CLOEXEC 0
#define DEBUGASSERT(condition) assert(condition)
#define wlinfo(...) ((void)0)
#define wlerr(...) ((void)0)
#define wlwarn(...) ((void)0)
#define BT_LE162HOST(value) (value)

typedef unsigned int irqstate_t;
struct bt_buf_s { uint8_t *data; size_t len; };
struct bt_queue_s { unsigned int dummy_wakeups; };
struct bt_conn_s;
enum bt_conn_state_e
{
  BT_CONN_DISCONNECTED,
  BT_CONN_CONNECT_SCAN,
  BT_CONN_CONNECT,
  BT_CONN_CONNECTED,
  BT_CONN_DISCONNECT
};
struct bt_conn_s
{
  enum bt_conn_state_e state;
  uint16_t pending_pkts;
  uint16_t handle;
  unsigned int refs;
  struct bt_queue_s tx_queue;
};

#include "bt-connection-credit-types.h"

static struct
{
  unsigned int le_pkts_sem;
  void *btdev;
} g_btdev;

static struct
{
  struct bt_conn_s *conn;
  unsigned int sync_sem;
} g_conn_handoff = {.sync_sem = 1};

static struct bt_conn_s *connections[4];
static unsigned int connection_count;
static unsigned int lookup_refs;
static unsigned int critical_depth;
static unsigned int release_before_wakeup;
static unsigned int credits_at_wakeup;
static bool real_tx_thread;

const char *state2str(enum bt_conn_state_e state)
{
  return state == BT_CONN_CONNECTED ? "connected" :
         state == BT_CONN_DISCONNECTED ? "disconnected" : "other";
}

#ifndef EXPECT_BASELINE_CREDIT_LEAK
static irqstate_t enter_critical_section(void)
{
  irqstate_t previous = critical_depth;
  critical_depth++;
  return previous;
}

static void leave_critical_section(irqstate_t flags)
{
  assert(critical_depth == flags + 1);
  critical_depth = flags;
}
#endif

static int nxsem_post(unsigned int *sem)
{
  (*sem)++;
  return 0;
}

static int nxsem_wait_uninterruptible(unsigned int *sem)
{
  if (*sem == 0)
    {
      return -1;
    }

  (*sem)--;
  return 0;
}

static struct bt_conn_s *bt_conn_addref(struct bt_conn_s *conn)
{
  conn->refs++;
  return conn;
}

static void bt_conn_release(struct bt_conn_s *conn)
{
  assert(conn->refs > 0);
  conn->refs--;
}

#ifndef EXPECT_BASELINE_CREDIT_LEAK
static struct bt_conn_s *bt_conn_lookup_handle(uint16_t handle)
{
  for (unsigned int i = 0; i < connection_count; i++)
    {
      if (connections[i]->handle == handle)
        {
          lookup_refs++;
          return bt_conn_addref(connections[i]);
        }
    }

  return NULL;
}
#endif

static int bt_queue_open(const char *name, int flags, int max_messages,
                         struct bt_queue_s *queue)
{
  assert(strcmp(name, BT_CONN_TX) == 0 && flags == 0);
  assert(max_messages == CONFIG_BLUETOOTH_TXCONN_NMSGS);
  queue->dummy_wakeups = 0;
  return 0;
}

static int kthread_create(const char *name, int priority, int stack_size,
                          int (*entry)(int, char **), char *arg)
{
  assert(strcmp(name, "BT Conn Tx") == 0);
  assert(priority == CONFIG_BLUETOOTH_TXCONN_PRIORITY);
  assert(stack_size == CONFIG_BLUETOOTH_TXCONN_STACKSIZE);
  assert(entry != NULL && arg == NULL);
  nxsem_post(&g_conn_handoff.sync_sem); /* Simulate the thread startup handshake. */
  return 1; /* Record creation without running a thread. */
}

static struct bt_buf_s dummy_buf;
static int conn_tx_kthread(int argc, char **argv)
{
  (void)argc;
  (void)argv;
  return 0;
}

static struct bt_buf_s *bt_buf_alloc(int type, void *conn, size_t len)
{
  (void)conn;
  assert(type == BT_DUMMY && len == 0);
  return &dummy_buf;
}

static int bt_queue_send(struct bt_queue_s *queue, struct bt_buf_s *buf,
                         int priority)
{
  struct bt_conn_s *owner = (struct bt_conn_s *)
    ((char *)queue - offsetof(struct bt_conn_s, tx_queue));

  assert(buf == &dummy_buf && priority == BT_NORMAL_PRIO);
  assert(critical_depth == 0);
  assert(queue->dummy_wakeups == 0 || queue->dummy_wakeups == 1);
  assert(owner->pending_pkts == 0);
  queue->dummy_wakeups++;
  release_before_wakeup++;
  credits_at_wakeup = g_btdev.le_pkts_sem;
  if (!real_tx_thread)
    {
      bt_conn_release(owner); /* Simulate the worker's reference release. */
    }
  return 0;
}

#ifndef EXPECT_BASELINE_CREDIT_LEAK
/* Platform stand-ins for the actual TX body extracted below. */
struct mq_attr { unsigned int mq_curmsgs; };
static struct bt_conn_s *tx_connection;
static struct bt_buf_s tx_buffer;
static int tx_result;
static bool disconnect_in_send, complete_in_send;
static unsigned int tx_calls, tx_releases, tx_cleanup;
void bt_conn_tx_complete(struct bt_conn_s *, uint16_t);
void bt_conn_set_state(struct bt_conn_s *, enum bt_conn_state_e);

static int bt_queue_receive(struct bt_queue_s *queue, struct bt_buf_s **buf)
{
  assert(queue == &tx_connection->tx_queue);
  *buf = &tx_buffer;
  return OK;
}

static int file_mq_getattr(struct bt_queue_s *queue, struct mq_attr *attr)
{
  assert(queue == &tx_connection->tx_queue);
  attr->mq_curmsgs = 0;
  return OK;
}

static void bt_buf_release(struct bt_buf_s *buf)
{
  assert(buf == &tx_buffer);
  tx_releases++;
}

static void bt_conn_reset_rx_state(struct bt_conn_s *conn)
{
  assert(conn == tx_connection);
  tx_cleanup++;
}

static int bt_send(void *driver, struct bt_buf_s *buf)
{
  (void)driver;
  assert(buf == &tx_buffer && critical_depth == 0);
  assert(tx_connection->pending_pkts == 1);
  tx_calls++;
  if (complete_in_send) bt_conn_tx_complete(tx_connection, 1);
  if (disconnect_in_send)
    {
      bt_conn_set_state(tx_connection, BT_CONN_DISCONNECTED);
    }
  else
    {
      /* Stop after this one transfer; state-helper behavior is tested above. */
      tx_connection->state = BT_CONN_DISCONNECTED;
    }
  return tx_result;
}
#endif

#include "bt-connection-credit-functions.h"

static void register_conn(struct bt_conn_s *conn, uint16_t handle)
{
  memset(conn, 0, sizeof(*conn));
  conn->handle = handle;
  assert(connection_count < sizeof(connections) / sizeof(connections[0]));
  connections[connection_count++] = conn;
}

static void reset_fixture(void)
{
  memset(connections, 0, sizeof(connections));
  connection_count = 0;
  lookup_refs = 0;
  critical_depth = 0;
  release_before_wakeup = 0;
  credits_at_wakeup = 0;
  g_btdev.le_pkts_sem = 0;
  g_conn_handoff.conn = NULL;
  g_conn_handoff.sync_sem = 1;
}

static void begin_connection(struct bt_conn_s *conn)
{
  bt_conn_set_state(conn, BT_CONN_CONNECTED);
}

#ifndef EXPECT_BASELINE_CREDIT_LEAK
static void spend_and_reserve(struct bt_conn_s *conn)
{
  assert(g_btdev.le_pkts_sem > 0);
  g_btdev.le_pkts_sem--; /* The token is consumed before reserve/send. */
  assert(bt_conn_reserve_tx(conn));
}

static void test_disconnect_reclaims_each_cycle(void)
{
  struct bt_conn_s conn;
  reset_fixture();
  register_conn(&conn, 0x40);
  g_btdev.le_pkts_sem = 3;
  for (unsigned int cycle = 0; cycle < 32; cycle++)
    {
      begin_connection(&conn);
      spend_and_reserve(&conn);
      spend_and_reserve(&conn);
      spend_and_reserve(&conn);
      assert(g_btdev.le_pkts_sem == 0 && conn.pending_pkts == 3);
      bt_conn_set_state(&conn, BT_CONN_DISCONNECTED);
      assert(g_btdev.le_pkts_sem == 3 && conn.pending_pkts == 0);
      assert(credits_at_wakeup == 3);
      assert(conn.refs == 0 && conn.tx_queue.dummy_wakeups == 1);
      assert(release_before_wakeup == cycle + 1);
    }
}

static void test_completion_and_connection_isolation(void)
{
  struct bt_conn_s first, second;
  struct bt_buf_s event_buf;
  uint8_t event_storage[sizeof(struct bt_hci_evt_num_completed_packets_s) +
                        2 * sizeof(((struct bt_hci_evt_num_completed_packets_s *)0)->h[0])] = {0};
  struct bt_hci_evt_num_completed_packets_s *event =
    (struct bt_hci_evt_num_completed_packets_s *)event_storage;
  reset_fixture();
  register_conn(&first, 0x41);
  register_conn(&second, 0x42);
  g_btdev.le_pkts_sem = 3;
  begin_connection(&first);
  begin_connection(&second);
  spend_and_reserve(&first);
  spend_and_reserve(&first);
  spend_and_reserve(&second);
  assert(g_btdev.le_pkts_sem == 0);

  event->num_handles = 2;
  event->h[0].handle = 0x41;
  event->h[0].count = 1;
  event->h[1].handle = 0x42;
  event->h[1].count = 8; /* Clamp to this connection's one outstanding packet. */
  event_buf.data = event_storage;
  event_buf.len = sizeof(event_storage);
  hci_num_completed_packets(&event_buf);
  assert(first.pending_pkts == 1 && second.pending_pkts == 0);
  assert(g_btdev.le_pkts_sem == 2 && lookup_refs == 2);
  assert(first.refs == 2 && second.refs == 2); /* State plus no leaked lookup ref. */

  event->num_handles = 1;
  event->h[0].handle = 0x43; /* Unknown handles do not mint credits. */
  event->h[0].count = 100;
  event_buf.len = sizeof(struct bt_hci_evt_num_completed_packets_s) +
                  sizeof(event->h[0]);
  hci_num_completed_packets(&event_buf);
  assert(g_btdev.le_pkts_sem == 2 && lookup_refs == 2);

  bt_conn_set_state(&first, BT_CONN_DISCONNECTED);
  assert(credits_at_wakeup == 3);
  bt_conn_set_state(&second, BT_CONN_DISCONNECTED);
  assert(g_btdev.le_pkts_sem == 3);
  event->h[0].handle = 0x41;
  event->h[0].count = 1;
  hci_num_completed_packets(&event_buf); /* Late and duplicate completions. */
  assert(g_btdev.le_pkts_sem == 3);
  assert(!bt_conn_reserve_tx(&first)); /* Disconnect wins against a later reserve. */
}

static void test_send_failure_and_state_gate(void)
{
  struct bt_conn_s conn;
  reset_fixture();
  register_conn(&conn, 0x44);
  g_btdev.le_pkts_sem = 2;
  assert(!bt_conn_reserve_tx(&conn));
  assert(conn.pending_pkts == 0);
  begin_connection(&conn);
  g_btdev.le_pkts_sem--;
  assert(bt_conn_reserve_tx(&conn));
  bt_conn_tx_complete(&conn, 1); /* Driver send failure rolls back reservation. */
  assert(conn.pending_pkts == 0 && g_btdev.le_pkts_sem == 2);
  bt_conn_set_state(&conn, BT_CONN_DISCONNECTED);
}

static void test_actual_tx_driver_result(void)
{
  for (unsigned int scenario = 0; scenario < 3; scenario++)
    {
      struct bt_conn_s conn;
      reset_fixture();
      register_conn(&conn, 0x45);
      conn.state = BT_CONN_CONNECTED;
      conn.refs = scenario == 2 ? 2 : 1; /* State plus real worker when needed. */
      tx_connection = g_conn_handoff.conn = &conn;
      g_btdev.le_pkts_sem = 1;
      tx_result = scenario == 1 ? OK : -1;
      complete_in_send = scenario == 1;
      disconnect_in_send = scenario == 2;
      tx_calls = tx_releases = tx_cleanup = 0;
      real_tx_thread = true;
      assert(actual_conn_tx_kthread(0, NULL) == EXIT_SUCCESS);
      assert(tx_calls == 1 && tx_releases == 1 && tx_cleanup == 1);
      assert(conn.refs == 0 && conn.pending_pkts == 0);
      assert(g_btdev.le_pkts_sem == 1); /* Never lose or double-return a token. */
      real_tx_thread = false;
    }
}
#else
static void test_baseline_disconnect_leak(void)
{
  struct bt_conn_s conn;
  reset_fixture();
  register_conn(&conn, 0x40);
  g_btdev.le_pkts_sem = 3;
  begin_connection(&conn);
  for (unsigned int sent = 0; sent < 3; sent++)
    {
      g_btdev.le_pkts_sem--;
    }

  bt_conn_set_state(&conn, BT_CONN_DISCONNECTED);
  assert(g_btdev.le_pkts_sem == 0 && credits_at_wakeup == 0);
  assert(conn.refs == 0);

  begin_connection(&conn);
  assert(g_btdev.le_pkts_sem == 0); /* No token exists for a next reservation. */
  bt_conn_set_state(&conn, BT_CONN_DISCONNECTED);
  assert(g_btdev.le_pkts_sem == 0 && conn.refs == 0);
}
#endif

int main(void)
{
#ifndef EXPECT_BASELINE_CREDIT_LEAK
  test_disconnect_reclaims_each_cycle();
  test_completion_and_connection_isolation();
  test_send_failure_and_state_gate();
  test_actual_tx_driver_result();
#else
  test_baseline_disconnect_leak();
#endif
  assert(critical_depth == 0);
  assert(lookup_refs == 2 || lookup_refs == 0);
  assert(release_before_wakeup > 0);
  return 0;
}

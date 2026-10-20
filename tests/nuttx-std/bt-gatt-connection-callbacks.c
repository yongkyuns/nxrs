/* SPDX-License-Identifier: MIT */
/* Host witness for actual patched GATT callback functions. */
#include <assert.h>
#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdatomic.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include <time.h>

#define FAR
#define CODE
#define wlinfo(...) ((void)0)
#define wlerr(...) ((void)0)
#define NXMUTEX_INITIALIZER PTHREAD_MUTEX_INITIALIZER
typedef pthread_mutex_t mutex_t;
struct bt_conn_s { int token; uint8_t disconn_reason; };
struct bt_gatt_attr_s;

#include "bt-gatt-connection-type.h"

static struct bt_gatt_conn_cb_s *g_conn_cbs;
static mutex_t g_conn_cb_lock = NXMUTEX_INITIALIZER;
static atomic_int lock_error;
static atomic_bool observe_unregister;
static atomic_bool unregister_waiting;
static unsigned int attribute_updates;
static pthread_mutex_t g_lock;

static int nxmutex_lock(mutex_t *lock)
{
  /* Registration while holding the fixture lock would invert callback order. */
  assert(pthread_mutex_trylock(&g_lock) == 0);
  assert(pthread_mutex_unlock(&g_lock) == 0);
  int error = atomic_exchange(&lock_error, 0);
  if (error != 0) return error;
  if (atomic_load(&observe_unregister))
    {
      /* Observe contention inside the actual unregister call, not merely
       * its thread's start. The callback cannot yet have been released.
       */
      assert(lock == &g_conn_cb_lock && pthread_mutex_trylock(lock) == EBUSY);
      atomic_store(&unregister_waiting, true);
    }
  return -pthread_mutex_lock(lock);
}

static void nxmutex_unlock(mutex_t *lock)
{
  assert(pthread_mutex_unlock(lock) == 0);
}

static uint8_t connected_cb(const struct bt_gatt_attr_s *attr, void *arg)
{
  (void)attr;
  (void)arg;
  return 0;
}

static uint8_t disconnected_cb(const struct bt_gatt_attr_s *attr, void *arg)
{
  (void)attr;
  (void)arg;
  return 0;
}

static void bt_gatt_foreach_attr(uint16_t first, uint16_t last,
                               uint8_t (*callback)(const struct bt_gatt_attr_s *, void *),
                               void *arg)
{
  assert(first == 1 && last == 65535);
  assert(arg != NULL);
  assert(callback == connected_cb || callback == disconnected_cb);
  attribute_updates++;
}

#include "bt-gatt-connection-functions.h"

/* Test only the C fixture's lifecycle glue; target builds use real headers. */
#define NXRS_RADIO_NAME "bnep0"
#define NXRS_RADIO_ADV_NAME "nxrs-gatt"
#define NXRS_RADIO_VALUE_LEN 20
#define NXRS_RADIO_ROUNDS 100
#define NXRS_RADIO_TIMEOUT_MS 10000
#define AF_BLUETOOTH 31
#define SOCK_RAW 3
#define BTPROTO_L2CAP 0
#define BT_LE_ADV_IND 0
#define BT_LE_AD_GENERAL 2
#define BT_LE_AD_NO_BREDR 4
#define BT_EIR_FLAGS 1
#define BT_EIR_UUID128_SOME 6
#define BT_EIR_NAME_COMPLETE 9
#define SIOCBTADVSTART 1001
#define SIOCBTADVSTOP 1002
struct bt_eir_s { uint8_t len, type, data[32]; };
struct btreq_s { char btr_name[16]; int btr_advtype; struct bt_eir_s *btr_advad, *btr_advsd; };
static const struct { union { uint8_t u128[16]; } u; } g_service_uuid = {.u.u128 = {0}};
static const struct { union { uint8_t u128[16]; } u; } g_legacy_service_uuid = {.u.u128 = {0}};
static int g_attributes[3];
static int socket_error, start_error, stop_error, unregister_after_start_error;
static unsigned int socket_closes;
static struct bt_conn_s *native_connection;
static unsigned int native_references, critical_depth, sleeps;
static bool retire_native_on_sleep;
static int64_t elapsed_ms;

typedef unsigned int irqstate_t;
#define BT_ADDR_LE_ANY NULL
#define BT_CONN_CONNECTED 1

static irqstate_t enter_critical_section(void)
{
  assert(critical_depth++ == 0);
  return 0;
}

static void leave_critical_section(irqstate_t flags)
{
  assert(flags == 0 && critical_depth-- == 1);
}

static struct bt_conn_s *bt_conn_lookup_state(const void *address, int state)
{
  assert(address == BT_ADDR_LE_ANY && state == BT_CONN_CONNECTED);
  assert(critical_depth == 1);
  if (native_connection != NULL) native_references++;
  return native_connection;
}

static void bt_conn_release(struct bt_conn_s *conn)
{
  assert(conn == native_connection && critical_depth == 0 && native_references == 1);
  native_references--;
}

#include "bt-gatt-fixture-globals.h"

static void bt_gatt_register(const void *attrs, size_t count)
{
  assert(attrs == g_attributes && count == 3);
}

static int fixture_socket(int family, int type, int protocol)
{
  assert(family == AF_BLUETOOTH && type == SOCK_RAW && protocol == BTPROTO_L2CAP);
  if (socket_error) { errno = socket_error; return -1; }
  return 17;
}

static int fixture_close(int fd)
{
  assert(fd == 17);
  socket_closes++;
  return 0;
}

static int fixture_ioctl(int fd, int command, unsigned long argument)
{
  const struct btreq_s *request = (const void *)(uintptr_t)argument;
  int error = command == SIOCBTADVSTART ? start_error : stop_error;
  assert(fd == 17 && strcmp(request->btr_name, NXRS_RADIO_NAME) == 0);
  assert(command == SIOCBTADVSTART || command == SIOCBTADVSTOP);
  if (error) {
    if (command == SIOCBTADVSTART) atomic_store(&lock_error, unregister_after_start_error);
    errno = error;
    return -1;
  }
  return 0;
}

#define socket fixture_socket
#define close fixture_close
#define ioctl fixture_ioctl
static int fixture_clock(clockid_t clock, struct timespec *now)
{
  assert(clock == CLOCK_MONOTONIC);
  now->tv_sec = elapsed_ms / 1000;
  now->tv_nsec = (elapsed_ms % 1000) * 1000000;
  return 0;
}

static int fixture_sleep(const struct timespec *interval, struct timespec *remaining)
{
  assert(interval->tv_sec == 0 && interval->tv_nsec == 10000000 && remaining == NULL);
  assert(g_conn_cb_registered && g_fd == 17 && native_references == 0);
  assert(pthread_mutex_trylock(&g_lock) == 0);
  assert(pthread_mutex_unlock(&g_lock) == 0);
  elapsed_ms += 10;
  sleeps++;
  if (retire_native_on_sleep) native_connection = NULL;
  return 0;
}

#define clock_gettime fixture_clock
#define nanosleep fixture_sleep
#include "bt-gatt-fixture-functions.h"
#undef socket
#undef close
#undef ioctl
#undef clock_gettime
#undef nanosleep

static void test_fixture_lifecycle(struct bt_conn_s *conn)
{
  struct bt_conn_s foreign = {.token=99, .disconn_reason=8};
  uint32_t writes, reads, invalid, lost;
  uint8_t packet[20] = {0}, value[20];
  for (unsigned int index = 0; index < 16; index++) packet[4 + index] = index;
  unsigned int closed = socket_closes;
  /* Defensive unknown-reason handling is not a normal connection lifecycle.
   * A fresh start must reset this deliberately unmatched callback count.
   */
  nxrs_radio_gatt_disconnected(NULL, NULL);
  assert(g_disconnect_reason == -1 && g_disconnections == 1);
  assert(nxrs_radio_gatt_start() == 0 && g_conn_cb_registered);
  assert(g_disconnect_reason == -1 && g_disconnections == 0);
  assert(nxrs_radio_gatt_start() == EBUSY);
  bt_gatt_connected(conn);
  bt_gatt_connected(&foreign);
  assert(g_selected_conn == NULL);
  assert(nxrs_radio_gatt_value_write(conn, NULL, packet, 20, 0) == 20);
  assert(nxrs_radio_gatt_value_read(&foreign, NULL, value, 20, 0) == -EACCES);
  assert(nxrs_radio_gatt_value_read(conn, NULL, value, 20, 0) == 20);
  bt_gatt_disconnected(&foreign);
  assert(g_disconnect_reason == 8);
  assert(nxrs_radio_gatt_snapshot(&writes, &reads, &invalid, &lost) == 0 && !lost);
  bt_gatt_disconnected(conn);
  assert(g_disconnect_reason == conn->disconn_reason);
  assert(nxrs_radio_gatt_snapshot(&writes, &reads, &invalid, &lost) == 0 && lost);
  assert(nxrs_radio_gatt_stop() == 0 && !g_conn_cb_registered && g_conn_cbs == NULL);
  assert(socket_closes == closed + 1 && g_fd == -1);
  assert(nxrs_radio_gatt_stop() == 0 && socket_closes == closed + 1);
  assert(nxrs_radio_gatt_start() == 0);
  assert(nxrs_radio_gatt_snapshot(&writes, &reads, &invalid, &lost) == 0 && !lost);
  assert(nxrs_radio_gatt_stop() == 0);

  socket_error = EMFILE;
  assert(nxrs_radio_gatt_start() == EMFILE && g_conn_cbs == NULL);
  assert(g_state == NXRS_RADIO_GATT_STOPPED);
  socket_error = 0;
  atomic_store(&lock_error, -ENOMEM);
  assert(nxrs_radio_gatt_start() == ENOMEM && g_conn_cbs == NULL);
  assert(g_state == NXRS_RADIO_GATT_STOPPED);

  start_error = EIO;
  assert(nxrs_radio_gatt_start() == EIO && !g_conn_cb_registered);
  assert(g_state == NXRS_RADIO_GATT_STOPPED && g_conn_cbs == NULL);
  unregister_after_start_error = -EINTR;
  assert(nxrs_radio_gatt_start() == EIO && g_conn_cb_registered);
  assert(g_state == NXRS_RADIO_GATT_STOPPING && g_conn_cbs == &g_conn_cb);
  assert(nxrs_radio_gatt_start() == EBUSY);
  assert(nxrs_radio_gatt_stop() == 0 && g_conn_cbs == NULL);
  start_error = unregister_after_start_error = 0;

  assert(nxrs_radio_gatt_start() == 0);
  atomic_store(&lock_error, -EINTR);
  assert(nxrs_radio_gatt_stop() == EINTR && g_conn_cb_registered);
  assert(g_state == NXRS_RADIO_GATT_STOPPING && g_fd == -1);
  assert(nxrs_radio_gatt_stop() == 0 && !g_conn_cb_registered);
  assert(nxrs_radio_gatt_start() == 0);
  stop_error = EINVAL;
  assert(nxrs_radio_gatt_stop() == EINVAL && g_fd == 17);
  assert(g_state == NXRS_RADIO_GATT_STOPPING && g_conn_cb_registered);
  stop_error = 0;
  assert(nxrs_radio_gatt_stop() == 0 && g_fd == -1 && g_conn_cbs == NULL);
}

static void test_fixture_disconnect_barrier(struct bt_conn_s *conn)
{
  unsigned int closed = socket_closes;
  assert(nxrs_radio_gatt_start() == 0);
  bt_gatt_connected(conn);
  bt_gatt_disconnected(conn);
  assert(g_connections == g_disconnections);

  /* The callback can precede the stack's final connection-state transition.
   * Balanced callbacks alone must not allow unregister or socket closure.
   */
  native_connection = conn;
  sleeps = 0;
  assert(nxrs_radio_gatt_stop() == ETIMEDOUT);
  assert(sleeps == NXRS_RADIO_TIMEOUT_MS / 10);
  assert(g_conn_cb_registered && g_conn_cbs == &g_conn_cb && g_fd == 17);
  assert(socket_closes == closed && native_references == 0);
  retire_native_on_sleep = true;
  sleeps = 0;
  assert(nxrs_radio_gatt_stop() == 0 && sleeps == 1);
  assert(!g_conn_cb_registered && g_conn_cbs == NULL && g_fd == -1);
  assert(socket_closes == closed + 1 && native_references == 0);
  retire_native_on_sleep = false;

  /* Native idle is likewise insufficient while a disconnect callback is due. */
  assert(nxrs_radio_gatt_start() == 0);
  bt_gatt_connected(conn);
  assert(nxrs_radio_gatt_stop() == ETIMEDOUT && g_conn_cb_registered && g_fd == 17);
  bt_gatt_disconnected(conn);
  assert(nxrs_radio_gatt_stop() == 0 && g_conn_cbs == NULL);

  assert(nxrs_radio_gatt_start() == 0);
  nxrs_radio_gatt_disconnected(NULL, NULL);
  assert(nxrs_radio_gatt_stop() == EPROTO && g_conn_cb_registered && g_fd == 17);
  bt_gatt_connected(conn);
  assert(nxrs_radio_gatt_stop() == 0 && g_conn_cbs == NULL);
}

struct counts_s
{
  unsigned int connected;
  unsigned int disconnected;
  unsigned int updated_before;
};

static void connected(struct bt_conn_s *conn, void *arg)
{
  struct counts_s *counts = arg;
  assert(conn->token == 42);
  assert(attribute_updates > counts->updated_before);
  counts->updated_before = attribute_updates;
  counts->connected++;
}

static void disconnected(struct bt_conn_s *conn, void *arg)
{
  struct counts_s *counts = arg;
  assert(conn->token == 42);
  assert(attribute_updates > counts->updated_before);
  counts->updated_before = attribute_updates;
  counts->disconnected++;
}

static pthread_mutex_t gate = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t changed = PTHREAD_COND_INITIALIZER;
static bool entered;
static bool release_callback;
static atomic_bool removed;

static void held_callback(struct bt_conn_s *conn, void *arg)
{
  (void)conn;
  (void)arg;
  assert(pthread_mutex_lock(&gate) == 0);
  entered = true;
  assert(pthread_cond_broadcast(&changed) == 0);
  while (!release_callback)
    {
      assert(pthread_cond_wait(&changed, &gate) == 0);
    }

  assert(!atomic_load(&removed));
  assert(pthread_mutex_unlock(&gate) == 0);
}

static void *notify_thread(void *arg)
{
  bt_gatt_connected(arg);
  return NULL;
}

static void *remove_thread(void *arg)
{
  assert(bt_gatt_conn_cb_unregister(arg) == 0);
  atomic_store(&removed, true);
  return NULL;
}

int main(void)
{
  struct bt_conn_s conn = {.token=42, .disconn_reason=19};
  struct counts_s first = {0}, second = {0};
  struct bt_gatt_conn_cb_s a = {.connected = connected, .disconnected = disconnected, .arg = &first};
  struct bt_gatt_conn_cb_s b = {.disconnected = disconnected, .arg = &second};
  struct bt_gatt_conn_cb_s empty = {0};
  struct bt_gatt_conn_cb_s held = {.connected = held_callback};
  pthread_t dispatcher, remover;

  assert(bt_gatt_conn_cb_register(NULL) == -EINVAL);
  assert(bt_gatt_conn_cb_register(&empty) == -EINVAL);
  assert(bt_gatt_conn_cb_unregister(NULL) == -EINVAL);
  assert(bt_gatt_conn_cb_unregister(&a) == -ENOENT);
  atomic_store(&lock_error, -EIO);
  assert(bt_gatt_conn_cb_register(&a) == -EIO);
  assert(g_conn_cbs == NULL);

  assert(bt_gatt_conn_cb_register(&a) == 0);
  assert(bt_gatt_conn_cb_register(&b) == 0);
  assert(bt_gatt_conn_cb_register(&a) == -EALREADY);
  assert(g_conn_cbs == &b && b.flink == &a && a.flink == NULL);
  atomic_store(&lock_error, -EINTR);
  bt_gatt_connected(&conn);
  bt_gatt_disconnected(&conn);
  assert(first.connected == 1 && first.disconnected == 1);
  assert(second.connected == 0 && second.disconnected == 1);
  atomic_store(&lock_error, -EIO);
  assert(bt_gatt_conn_cb_unregister(&a) == -EIO);
  assert(b.flink == &a);
  assert(bt_gatt_conn_cb_unregister(&a) == 0);
  assert(a.flink == NULL && b.flink == NULL);
  bt_gatt_connected(&conn);
  bt_gatt_disconnected(&conn);
  assert(first.connected == 1 && first.disconnected == 1);
  assert(second.disconnected == 2);
  assert(bt_gatt_conn_cb_unregister(&b) == 0);
  assert(bt_gatt_conn_cb_register(&a) == 0);
  assert(bt_gatt_conn_cb_unregister(&a) == 0);
  assert(g_conn_cbs == NULL);

  assert(bt_gatt_conn_cb_register(&held) == 0);
  assert(pthread_create(&dispatcher, NULL, notify_thread, &conn) == 0);
  assert(pthread_mutex_lock(&gate) == 0);
  while (!entered)
    {
      assert(pthread_cond_wait(&changed, &gate) == 0);
    }

  /* The actual dispatcher holds the same mutex that unregister requires. */

  assert(pthread_mutex_trylock(&g_conn_cb_lock) == EBUSY);
  atomic_store(&observe_unregister, true);
  assert(pthread_create(&remover, NULL, remove_thread, &held) == 0);
  while (!atomic_load(&unregister_waiting) && !atomic_load(&removed))
    {
      sched_yield();
    }

  assert(atomic_load(&unregister_waiting) && !atomic_load(&removed));
  release_callback = true;
  assert(pthread_cond_broadcast(&changed) == 0);
  assert(pthread_mutex_unlock(&gate) == 0);
  assert(pthread_join(dispatcher, NULL) == 0);
  assert(pthread_join(remover, NULL) == 0);
  atomic_store(&observe_unregister, false);
  assert(atomic_load(&removed) && g_conn_cbs == NULL);
  bt_gatt_connected(&conn);
  assert(g_conn_cbs == NULL);
  test_fixture_lifecycle(&conn);
  test_fixture_disconnect_barrier(&conn);
  return 0;
}

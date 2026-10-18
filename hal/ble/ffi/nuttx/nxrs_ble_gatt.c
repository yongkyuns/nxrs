/* SPDX-License-Identifier: MIT */
#include "nxrs_ble_gatt.h"

#include <nuttx/config.h>
#include <nuttx/clock.h>
#include <nuttx/irq.h>
#include <nuttx/wqueue.h>
#include <nuttx/wireless/bluetooth/bt_core.h>
#include <nuttx/wireless/bluetooth/bt_gatt.h>
#include <nuttx/wireless/bluetooth/bt_hci.h>
#include <wireless/bluetooth/bt_conn.h>
#include <wireless/bluetooth/bt_att.h>

#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdatomic.h>
#include <string.h>

/* The legacy host dispatches ATT/link callbacks on LPWORK. A later work item
 * is a callback-lifetime barrier only with a single LPWORK consumer. Refuse
 * unsupported configurations instead of silently weakening that guarantee.
 */
_Static_assert(CONFIG_SCHED_LPNTHREADS == 1, "GATT requires serialized LPWORK");
#if !defined(CONFIG_BUILD_FLAT) || defined(CONFIG_LIBC_USRWORK)
#  error "GATT bridge requires flat-build kernel work queues"
#endif

extern int nxrs_ble_peer_lookup(uint64_t session, uint64_t id,
                                uint8_t address[6], uint8_t *type);

/* One command, one link-loss record and one recovery barrier can coexist.
 * The extra slot permits a queued completion before a timeout-driven drain.
 */
#define EVENT_SLOTS 4
enum discovery_phase { DISC_SERVICE, DISC_CHARACTERISTIC };

static struct
{
  struct work_s work;
  struct bt_gatt_conn_cb_s callbacks;
  struct bt_gatt_discover_params_s discovery;
  struct bt_uuid_s service_uuid;
  struct bt_uuid_s characteristic_uuid;
  struct bt_conn_s *conn;
  bt_addr_le_t pending_peer;
  struct nxrs_gatt_command command;
  struct nxrs_gatt_event result;
  struct nxrs_gatt_event events[EVENT_SLOTS];
  void (*wake)(void *);
  void (*release)(void *);
  void *arg;
  uint64_t session;
  uint64_t connection;
  uint64_t recovery;
  uint16_t capacity;
  uint16_t service_start;
  uint16_t service_end;
  uint16_t found_handle;
  uint16_t read_offset;
  uint16_t retire_delay;
  unsigned int found;
  unsigned int head;
  unsigned int count;
  enum discovery_phase phase;
  atomic_bool active;
  bool registered;
  atomic_bool busy;
  atomic_bool issued;
  atomic_bool result_ready;
  atomic_bool disconnected;
  bool loss_published;
  bool cancelled;
  bool disconnect_sent;
  atomic_bool closing;
  atomic_bool recovering;
  atomic_bool poisoned;
  uint8_t reason;
} g_gatt;

static void dispatch(void *arg);

static void uuid_from_canonical(struct bt_uuid_s *uuid, const uint8_t bytes[16])
{
  static const uint8_t base_tail[12] =
    {0, 0, 0x10, 0, 0x80, 0, 0, 0x80, 0x5f, 0x9b, 0x34, 0xfb};
  if (bytes[0] == 0 && bytes[1] == 0 && memcmp(bytes + 4, base_tail, 12) == 0)
    {
      uuid->type = BT_UUID_16;
      uuid->u.u16 = ((uint16_t)bytes[2] << 8) | bytes[3];
    }
  else
    {
      uuid->type = BT_UUID_128;
      for (unsigned int i = 0; i < 16; i++) { uuid->u.u128[i] = bytes[15 - i]; }
    }
}

/* Work storage is static. Each reason updates retained state before queueing
 * the common dispatcher; callbacks never run on the calling service thread.
 */
static int schedule(void)
{
  /* All reasons share one retained state machine. Requeueing also expedites
   * delayed connection-attempt cleanup when an actual link event arrives.
   */
  return work_queue(LPWORK, &g_gatt.work, dispatch, NULL, 0);
}

static bool owns(const struct bt_conn_s *conn)
{
  return g_gatt.active && conn != NULL && conn == g_gatt.conn;
}

/* LPWORK only. Never expose a terminal result directly from an ATT callback:
 * waking a higher-priority owner there could let it reuse storage too early.
 */
static void result_ready(int error, uint8_t att_error)
{
  if (!g_gatt.busy || g_gatt.result_ready || g_gatt.recovering || g_gatt.closing)
    {
      return;
    }

  g_gatt.result.error = error;
  g_gatt.result.att_error = att_error;
  g_gatt.result_ready = true;
  (void)schedule();
}

static void connected(struct bt_conn_s *conn, void *arg)
{
  (void)arg;
  if (owns(conn))
    {
      if (g_gatt.recovering || g_gatt.closing)
        {
          /* LE Create Connection Cancel can lose to a successful connection.
           * Retire the newly created ATT bearer and send a real disconnect.
           */
          g_gatt.cancelled = false;
          g_gatt.disconnect_sent = false;
        }
      if (g_gatt.command.kind == NXRS_GATT_CONNECT)
        {
          result_ready(0, 0);
        }
      (void)schedule();
    }
}

static void disconnected(struct bt_conn_s *conn, void *arg)
{
  (void)arg;
  if (owns(conn))
    {
      g_gatt.disconnected = true;
      g_gatt.reason = conn->disconn_reason;
      (void)schedule();
    }
}

static uint8_t discovered(const struct bt_gatt_attr_s *attr, void *arg)
{
  struct bt_gatt_discover_params_s *params = arg;
  if (g_gatt.recovering || g_gatt.closing)
    {
      return BT_GATT_ITER_STOP;
    }

  g_gatt.found++;
  if (g_gatt.phase == DISC_SERVICE)
    {
      g_gatt.service_start = attr->handle;
      g_gatt.service_end = params->group_end_handle;
    }
  else
    {
      const struct bt_gatt_chrc_s *chrc = attr->user_data;
      if (chrc == NULL || chrc->value_handle <= attr->handle ||
          chrc->value_handle > g_gatt.service_end)
        {
          g_gatt.result.error = EPROTO;
        }
      else
        {
          g_gatt.found_handle = chrc->value_handle;
        }
    }

  /* Count all matches so duplicate services/characteristics fail explicitly. */
  return BT_GATT_ITER_CONTINUE;
}

static void discovery_done(void *arg)
{
  const struct bt_gatt_discover_params_s *params = arg;
  if (g_gatt.recovering || g_gatt.closing)
    {
      return;
    }

  if (params->result != 0 && params->result != BT_ATT_ERR_ATTRIBUTE_NOT_FOUND)
    {
      result_ready(0, params->result);
      return;
    }

  if (g_gatt.result.error || g_gatt.found != 1)
    {
      result_ready(g_gatt.result.error ? g_gatt.result.error :
                    g_gatt.found > 1 ? EEXIST : ENOENT, 0);
    }
  else if (g_gatt.phase == DISC_SERVICE)
    {
      if (g_gatt.service_start >= g_gatt.service_end)
        { result_ready(ENOENT, 0); return; }
      g_gatt.phase = DISC_CHARACTERISTIC;
      g_gatt.issued = false;
      (void)schedule();
    }
  else
    {
      g_gatt.result.handle = g_gatt.found_handle;
      result_ready(0, 0);
    }
}

static void written(struct bt_conn_s *conn, uint8_t error)
{
  if (owns(conn))
    {
      result_ready(0, error);
    }
}

static void read_value(struct bt_conn_s *conn, int error,
                        const void *data, uint16_t length)
{
  uint16_t payload;
  if (!owns(conn) || g_gatt.recovering || g_gatt.closing)
    {
      return;
    }

  if (error)
    {
      result_ready(error < 0 ? -error : 0, error > 0 ? error : 0);
      return;
    }

  payload = bt_gatt_get_mtu(conn);
  if (payload < 23) { result_ready(ENOTCONN, 0); return; }
  payload--;
  if ((length && data == NULL) || length > payload ||
      length > g_gatt.capacity - g_gatt.read_offset)
    {
      result_ready(EINVAL, 0);
      return;
    }

  if (length)
    {
      memcpy(g_gatt.result.data + g_gatt.read_offset, data, length);
    }
  g_gatt.read_offset += length;
  g_gatt.result.length = g_gatt.read_offset;
  if (length == payload)
    {
      /* Even a full-capacity value needs a final short/empty blob response
       * to distinguish it from an oversized value. Never label a prefix full.
       */
      g_gatt.issued = false;
      (void)schedule();
    }
  else
    {
      result_ready(0, 0);
    }
}

static bool publish(struct nxrs_gatt_event event)
{
  irqstate_t flags = enter_critical_section();
  bool ok = g_gatt.count < EVENT_SLOTS;
  if (ok)
    {
      g_gatt.events[(g_gatt.head + g_gatt.count) % EVENT_SLOTS] = event;
      g_gatt.count++;
      if (event.kind >= NXRS_GATT_CONNECT && event.kind <= NXRS_GATT_DISCONNECT)
        {
          g_gatt.busy = false;
          g_gatt.result_ready = false;
          if (event.kind == NXRS_GATT_DISCONNECT && event.error == 0)
            {
              g_gatt.disconnected = false;
              g_gatt.loss_published = false;
            }
        }
      if (event.kind == NXRS_GATT_QUIESCENT)
        {
          g_gatt.busy = false;
          g_gatt.result_ready = false;
          g_gatt.recovery = 0;
          g_gatt.recovering = false;
          g_gatt.disconnected = false;
          g_gatt.loss_published = false;
        }
    }
  else
    {
      /* Impossible for a conforming single-flight owner. Retain ownership. */
      g_gatt.poisoned = true;
    }
  leave_critical_section(flags);
  g_gatt.wake(g_gatt.arg);
  return ok;
}

/* LPWORK only, after all earlier callbacks have returned. A scan-stage
 * connection cancellation has no GATT disconnect callback; native terminal
 * state plus this serialized turn is the corresponding barrier.
 */
static bool retire_link(void)
{
  if (g_gatt.conn == NULL)
    {
      return true;
    }

  if (!g_gatt.cancelled)
    {
      bt_gatt_cancel(g_gatt.conn);
      g_gatt.cancelled = true;
    }

  if (g_gatt.conn->state == BT_CONN_DISCONNECTED)
    {
      bt_conn_release(g_gatt.conn);
      g_gatt.conn = NULL;
      return true;
    }

  if (!g_gatt.disconnect_sent)
    {
      int ret = bt_conn_disconnect(g_gatt.conn, 0x13);
      if (ret == 0 || ret == -ENOTCONN)
        {
          g_gatt.disconnect_sent = true;
          if (g_gatt.conn->state == BT_CONN_DISCONNECTED)
            {
              /* Queue a separate turn; do not reclaim inside native dispatch. */
              (void)schedule();
            }
        }
    }

  /* A cancelled CONNECT attempt can reach DISCONNECTED without a GATT
   * callback. Observe that native terminal state here, not on the owner.
   * Back off if the controller never settles; storage remains quarantined.
   */
  if (g_gatt.retire_delay == 0) { g_gatt.retire_delay = 10; }
  (void)work_queue(LPWORK, &g_gatt.work, dispatch, NULL,
                   MSEC2TICK(g_gatt.retire_delay));
  if (g_gatt.retire_delay < 1000) { g_gatt.retire_delay *= 2; }

  return false;
}

static void dispatch(void *arg)
{
  int ret;
  (void)arg;

  if (g_gatt.closing || g_gatt.recovering)
    {
      if (!retire_link())
        {
          return;
        }
      if (g_gatt.closing)
        {
          void (*release)(void *) = g_gatt.release;
          void *context = g_gatt.arg;
          if (g_gatt.registered && bt_gatt_conn_cb_unregister(&g_gatt.callbacks) != 0)
            {
              (void)work_queue(LPWORK, &g_gatt.work, dispatch, NULL, MSEC2TICK(1000));
              return; /* Keep callback context/lease alive on failure. */
            }
          /* A callback may have queued a successor while this item ran.
           * Remove it before resetting its intrusive work-queue storage.
           */
          (void)work_cancel(LPWORK, &g_gatt.work);
          irqstate_t flags = enter_critical_section();
          memset(&g_gatt, 0, sizeof(g_gatt));
          leave_critical_section(flags);
          release(context);
        }
      else
        {
          struct nxrs_gatt_event event = {0};
          event.operation = g_gatt.recovery;
          event.kind = NXRS_GATT_QUIESCENT;
          (void)publish(event);
        }
      return;
    }

  if (!g_gatt.registered)
    {
      ret = bt_gatt_conn_cb_register(&g_gatt.callbacks);
      if (ret)
        {
          result_ready(ret < 0 ? -ret : EIO, 0);
        }
      else
        {
          g_gatt.registered = true;
        }
    }

  if (g_gatt.busy && g_gatt.result_ready)
    {
      struct nxrs_gatt_event event = g_gatt.result;
      event.operation = g_gatt.command.operation;
      event.kind = g_gatt.command.kind;
      if (g_gatt.disconnected) { (void)schedule(); }
      (void)publish(event);
      return; /* Owner may immediately submit a successor after the wake. */
    }

  if (g_gatt.disconnected)
    {
      if (g_gatt.busy && g_gatt.command.kind == NXRS_GATT_DISCONNECT)
        {
          if (retire_link())
            {
              result_ready(0, 0);
            }
          return;
        }
      if (!g_gatt.loss_published)
        {
          struct nxrs_gatt_event event = {0};
          event.operation = g_gatt.connection;
          event.kind = NXRS_GATT_LINK_LOST;
          event.reason = g_gatt.reason;
          g_gatt.loss_published = true;
          (void)publish(event);
        }
      return;
    }

  if (!g_gatt.busy || g_gatt.issued || !g_gatt.registered)
    {
      return;
    }

  g_gatt.issued = true;
  switch (g_gatt.command.kind)
    {
      case NXRS_GATT_CONNECT:
        {
          struct bt_conn_s *existing;
          existing = bt_conn_lookup_addr_le(&g_gatt.pending_peer);
          if (existing != NULL)
            {
              bt_conn_release(existing);
              result_ready(EBUSY, 0);
              return;
            }
          g_gatt.connection = g_gatt.command.operation;
          g_gatt.conn = bt_conn_create_le(&g_gatt.pending_peer);
          if (g_gatt.conn == NULL) { result_ready(ENOBUFS, 0); }
          return;
        }
      case NXRS_GATT_DISCOVER:
        memset(&g_gatt.discovery, 0, sizeof(g_gatt.discovery));
        g_gatt.found = 0;
        g_gatt.discovery.func = discovered;
        g_gatt.discovery.destroy = discovery_done;
        g_gatt.discovery.start_handle = g_gatt.phase == DISC_SERVICE ? 1 : g_gatt.service_start + 1;
        g_gatt.discovery.end_handle = g_gatt.phase == DISC_SERVICE ? UINT16_MAX : g_gatt.service_end;
        g_gatt.discovery.uuid = g_gatt.phase == DISC_SERVICE ? &g_gatt.service_uuid : &g_gatt.characteristic_uuid;
        ret = g_gatt.phase == DISC_SERVICE ? bt_gatt_discover(g_gatt.conn, &g_gatt.discovery) :
          bt_gatt_discover_characteristic(g_gatt.conn, &g_gatt.discovery);
        break;
      case NXRS_GATT_READ:
        ret = bt_gatt_read(g_gatt.conn, g_gatt.command.handle, g_gatt.read_offset, read_value);
        break;
      case NXRS_GATT_WRITE:
        if (g_gatt.command.length > bt_gatt_get_mtu(g_gatt.conn) - 3)
          { result_ready(ENOTSUP, 0); return; }
        ret = bt_gatt_write(g_gatt.conn, g_gatt.command.handle,
                            g_gatt.command.data, g_gatt.command.length, written);
        break;
      case NXRS_GATT_DISCONNECT:
        ret = bt_conn_disconnect(g_gatt.conn, 0x13);
        break;
      default:
        ret = -EINVAL;
        break;
    }
  if (ret) { result_ready(ret < 0 ? -ret : EIO, 0); }
}

int nxrs_ble_gatt_open(uint64_t session, uint16_t capacity,
                       void (*wake)(void *), void (*release)(void *), void *arg)
{
  irqstate_t flags = enter_critical_section();
  if (session == 0 || capacity > NXRS_GATT_CAPACITY || wake == NULL || release == NULL)
    { leave_critical_section(flags); return EINVAL; }
  if (g_gatt.active)
    { leave_critical_section(flags); return EBUSY; }
  memset(&g_gatt, 0, sizeof(g_gatt));
  g_gatt.active = true;
  g_gatt.session = session;
  g_gatt.capacity = capacity;
  g_gatt.wake = wake;
  g_gatt.release = release;
  g_gatt.arg = arg;
  g_gatt.callbacks.connected = connected;
  g_gatt.callbacks.disconnected = disconnected;
  leave_critical_section(flags);
  return 0;
}

int nxrs_ble_gatt_submit(uint64_t session, const struct nxrs_gatt_command *cmd)
{
  irqstate_t flags = enter_critical_section();
  int ret;
  if (!g_gatt.active || session != g_gatt.session || g_gatt.closing || g_gatt.poisoned)
    { leave_critical_section(flags); return ENODEV; }
  if (g_gatt.busy || g_gatt.recovering || g_gatt.count)
    { leave_critical_section(flags); return EBUSY; }
  if (cmd == NULL || cmd->operation == 0 || cmd->kind < NXRS_GATT_CONNECT ||
      cmd->kind > NXRS_GATT_DISCONNECT || cmd->length > g_gatt.capacity)
    { leave_critical_section(flags); return EINVAL; }
  if (cmd->operation <= g_gatt.command.operation)
    { leave_critical_section(flags); return EINVAL; }
  if ((cmd->kind == NXRS_GATT_CONNECT) != (g_gatt.conn == NULL))
    { leave_critical_section(flags); return ENOTCONN; }
  if (g_gatt.disconnected)
    { leave_critical_section(flags); return ENOTCONN; }
  if (cmd->kind == NXRS_GATT_CONNECT)
    {
      ret = nxrs_ble_peer_lookup(cmd->peer_session, cmd->peer_id,
                                 g_gatt.pending_peer.val, &g_gatt.pending_peer.type);
      if (ret)
        { leave_critical_section(flags); return ret == EBUSY ? EBUSY : EINVAL; }
    }
  g_gatt.command = *cmd;
  memset(&g_gatt.result, 0, sizeof(g_gatt.result));
  g_gatt.busy = true;
  g_gatt.issued = false;
  g_gatt.result_ready = false;
  g_gatt.cancelled = false;
  g_gatt.disconnect_sent = false;
  g_gatt.phase = DISC_SERVICE;
  g_gatt.read_offset = 0;
  g_gatt.retire_delay = 0;
  uuid_from_canonical(&g_gatt.service_uuid, cmd->service);
  uuid_from_canonical(&g_gatt.characteristic_uuid, cmd->characteristic);
  ret = schedule();
  if (ret) { g_gatt.busy = false; }
  leave_critical_section(flags);
  return ret < 0 ? -ret : ret;
}

void nxrs_ble_gatt_recover(uint64_t session, uint64_t operation)
{
  irqstate_t flags = enter_critical_section();
  if (g_gatt.active && session == g_gatt.session && operation && !g_gatt.closing)
    {
      if (!g_gatt.recovering)
        {
          g_gatt.recovery = operation;
          g_gatt.recovering = true;
        }
      (void)schedule();
    }
  leave_critical_section(flags);
}

int nxrs_ble_gatt_poll(uint64_t session, struct nxrs_gatt_event *event)
{
  irqstate_t flags = enter_critical_section();
  int available = 0;
  if (g_gatt.active && session == g_gatt.session && event && g_gatt.count)
    {
      *event = g_gatt.events[g_gatt.head];
      g_gatt.head = (g_gatt.head + 1) % EVENT_SLOTS;
      g_gatt.count--;
      available = 1;
    }
  leave_critical_section(flags);
  return available;
}

void nxrs_ble_gatt_close(uint64_t session)
{
  irqstate_t flags = enter_critical_section();
  if (g_gatt.active && session == g_gatt.session && !g_gatt.closing)
    {
      g_gatt.closing = true;
      (void)schedule();
    }
  leave_critical_section(flags);
}

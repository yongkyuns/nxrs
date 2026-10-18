/* SPDX-License-Identifier: MIT */
/* Deterministic scheduler/native-stack witness for the real HAL C adapter. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <errno.h>

#define CONFIG_BUILD_FLAT 1
#ifndef CONFIG_SCHED_LPNTHREADS
#define CONFIG_SCHED_LPNTHREADS 1
#endif
#define LPWORK 1
#define MSEC2TICK(ms) (ms)
#define BT_UUID_128 1
#define BT_UUID_16 0
#define BT_GATT_ITER_STOP 0
#define BT_GATT_ITER_CONTINUE 1
#define BT_ATT_ERR_ATTRIBUTE_NOT_FOUND 0x0a
#define BT_CONN_DISCONNECTED 0
#define BT_CONN_CONNECT_SCAN 1
#define BT_CONN_CONNECT 2
#define BT_CONN_CONNECTED 3
typedef unsigned int irqstate_t;
typedef struct { uint8_t val[6], type; } bt_addr_le_t;
struct work_s { void (*worker)(void *); void *arg; unsigned int delay; };
struct bt_uuid_s { int type; union { uint16_t u16; uint8_t u128[16]; } u; };
struct bt_conn_s { int state; uint8_t disconn_reason; };
struct bt_gatt_attr_s { uint16_t handle; const void *user_data; };
struct bt_gatt_chrc_s { uint16_t value_handle; };
struct bt_gatt_discover_params_s
{
  const struct bt_uuid_s *uuid;
  uint8_t (*func)(const struct bt_gatt_attr_s *, void *);
  void (*destroy)(void *);
  uint16_t start_handle, end_handle, group_end_handle;
  uint8_t result;
};
struct bt_gatt_conn_cb_s
{
  void (*connected)(struct bt_conn_s *, void *);
  void (*disconnected)(struct bt_conn_s *, void *);
  void *arg;
};
static struct bt_conn_s native;
static struct bt_gatt_conn_cb_s *callbacks;
static struct bt_gatt_discover_params_s *discovery;
static void (*read_callback)(struct bt_conn_s *, int, const void *, uint16_t);
static void (*write_callback)(struct bt_conn_s *, uint8_t);
static struct work_s *queued;
static unsigned int critical, wakes, releases, opens, refs, cancel_calls, disconnect_calls, mtu = 23;
static bool on_lpwork, in_callback, existing, registry_live = true;
static int queue_error, unregister_error;
static uint16_t read_offset;

static irqstate_t enter_critical_section(void) { return critical++; }
static void leave_critical_section(irqstate_t flags) { assert(critical == flags + 1); critical--; }
static int work_queue(int qid, struct work_s *work, void (*fn)(void *), void *arg, unsigned int delay)
{
  assert(qid == LPWORK);
  if (queue_error) { int e = queue_error; queue_error = 0; return -e; }
  assert(queued == NULL || queued == work);
  work->worker = fn; work->arg = arg; work->delay = delay; queued = work;
  return 0;
}
static int work_cancel(int qid, struct work_s *work)
{
  assert(qid == LPWORK);
  if (queued == work) queued = NULL;
  work->worker = NULL;
  return 0;
}
static int bt_gatt_conn_cb_register(struct bt_gatt_conn_cb_s *cb)
{ assert(on_lpwork && !callbacks); callbacks = cb; return 0; }
static int bt_gatt_conn_cb_unregister(struct bt_gatt_conn_cb_s *cb)
{
  assert(on_lpwork && !in_callback && callbacks == cb);
  if (unregister_error) return -unregister_error;
  callbacks = NULL; return 0;
}
static int nxrs_ble_peer_lookup(uint64_t session, uint64_t id, uint8_t address[6], uint8_t *type)
{ if (!registry_live || session != 1 || id != 1) return ENOENT; memset(address, 0, 6); *type = 1; return 0; }
static struct bt_conn_s *bt_conn_lookup_addr_le(const bt_addr_le_t *peer)
{ (void)peer; assert(on_lpwork); if (existing) { refs++; return &native; } return NULL; }
static struct bt_conn_s *bt_conn_create_le(const bt_addr_le_t *peer)
{ (void)peer; assert(on_lpwork); native.state = BT_CONN_CONNECT_SCAN; refs++; return &native; }
static void bt_conn_release(struct bt_conn_s *conn)
{ assert(on_lpwork && conn == &native && refs); refs--; }
static int bt_conn_disconnect(struct bt_conn_s *conn, uint8_t reason)
{
  assert(on_lpwork && conn == &native && reason == 0x13);
  disconnect_calls++;
  if (conn->state == BT_CONN_CONNECT_SCAN) conn->state = BT_CONN_DISCONNECTED;
  return 0;
}
static void bt_gatt_cancel(struct bt_conn_s *conn)
{
  assert(on_lpwork && !in_callback && conn == &native);
  cancel_calls++; discovery = NULL; read_callback = NULL; write_callback = NULL;
}
static uint16_t bt_gatt_get_mtu(struct bt_conn_s *conn)
{ assert(on_lpwork && conn == &native); return mtu; }
static int bt_gatt_discover(struct bt_conn_s *conn, struct bt_gatt_discover_params_s *params)
{ assert(on_lpwork && conn == &native && params->start_handle == 1 && params->end_handle == UINT16_MAX); discovery = params; return 0; }
static int bt_gatt_discover_characteristic(struct bt_conn_s *conn, struct bt_gatt_discover_params_s *params)
{ assert(on_lpwork && conn == &native && params->start_handle == 6 && params->end_handle == 12); discovery = params; return 0; }
static int bt_gatt_read(struct bt_conn_s *conn, uint16_t handle, uint16_t offset,
                        void (*cb)(struct bt_conn_s *, int, const void *, uint16_t))
{ assert(on_lpwork && conn == &native && handle == 7); read_callback = cb; read_offset = offset; return 0; }
static int bt_gatt_write(struct bt_conn_s *conn, uint16_t handle, const void *data, uint16_t length,
                         void (*cb)(struct bt_conn_s *, uint8_t))
{ assert(on_lpwork && conn == &native && handle == 7 && data != NULL && length <= 20); write_callback = cb; return 0; }

#include "nxrs_ble_gatt.c"

static bool submit_on_wake;
static void wake(void *arg)
{
  assert(arg == &wakes && on_lpwork && !in_callback && !critical); wakes++;
  if (submit_on_wake)
    {
      submit_on_wake = false;
      struct nxrs_gatt_event event;
      struct nxrs_gatt_command cmd = {.operation = 3, .kind = NXRS_GATT_READ, .handle = 7};
      assert(nxrs_ble_gatt_poll(8, &event) == 1 && event.kind == NXRS_GATT_WRITE);
      assert(nxrs_ble_gatt_submit(8, &cmd) == 0);
    }
}
static void release(void *arg)
{ assert(arg == &wakes && on_lpwork && !in_callback && callbacks == NULL && !critical); releases++; }
static void step(void)
{
  assert(queued != NULL && !on_lpwork);
  void (*fn)(void *) = queued->worker;
  void *arg = queued->arg;
  queued->worker = NULL; queued = NULL;
  on_lpwork = true; fn(arg); on_lpwork = false;
  assert(!critical);
}
static void drain(void)
{ for (unsigned int i = 0; queued && i < 20; i++) step(); assert(!queued); }
static struct nxrs_gatt_event pop(uint32_t kind)
{
  struct nxrs_gatt_event event;
  assert(nxrs_ble_gatt_poll(8, &event) == 1 && event.kind == kind);
  return event;
}
static void opened(void)
{ assert(nxrs_ble_gatt_open(8, 64, wake, release, &wakes) == 0); opens++; }
static void submit(unsigned int kind, uint64_t operation)
{
  struct nxrs_gatt_command cmd = { .operation = operation, .peer_session = 1, .peer_id = 1, .kind = kind, .handle = 7 };
  assert(nxrs_ble_gatt_submit(8, &cmd) == 0);
  step();
}
static void link_up(void)
{
  submit(NXRS_GATT_CONNECT, 1);
  assert(refs == 1 && callbacks != NULL);
  on_lpwork = in_callback = true;
  native.state = BT_CONN_CONNECTED;
  callbacks->connected(&native, callbacks->arg);
  in_callback = on_lpwork = false;
  struct nxrs_gatt_event event;
  assert(nxrs_ble_gatt_poll(8, &event) == 0); /* Still inside native callback boundary. */
  drain(); assert(pop(NXRS_GATT_CONNECT).error == 0);
}
static void link_down(void)
{
  on_lpwork = in_callback = true;
  native.state = BT_CONN_DISCONNECTED; native.disconn_reason = 19;
  callbacks->disconnected(&native, callbacks->arg);
  in_callback = on_lpwork = false;
}
static void close_link(void)
{
  unsigned int before = releases;
  nxrs_ble_gatt_close(8);
  step();
  if (refs)
    { assert(releases == before); link_down(); }
  drain(); assert(refs == 0 && releases == before + 1 && !g_gatt.active);
}

int main(void)
{
  struct bt_uuid_s uuid;
  uint8_t canonical[16] = {0, 0, 0x18, 0x0d, 0, 0, 0x10, 0, 0x80, 0, 0, 0x80, 0x5f, 0x9b, 0x34, 0xfb};
  uuid_from_canonical(&uuid, canonical);
  assert(uuid.type == BT_UUID_16 && uuid.u.u16 == 0x180d);
  canonical[0] = 1;
  uuid_from_canonical(&uuid, canonical);
  assert(uuid.type == BT_UUID_128 && uuid.u.u128[15] == 1 && uuid.u.u128[0] == 0xfb);
  _Static_assert(sizeof(struct nxrs_gatt_command) == 128, "command ABI");
  _Static_assert(sizeof(struct nxrs_gatt_event) == 88, "event ABI");
  opened();
  assert(nxrs_ble_gatt_open(9, 20, wake, release, &wakes) == EBUSY);
  link_up();
  submit(NXRS_GATT_DISCOVER, 2);
  struct bt_gatt_attr_s service = {.handle = 5};
  on_lpwork = in_callback = true;
  discovery->group_end_handle = 12;
  assert(discovery->func(&service, discovery) == BT_GATT_ITER_CONTINUE);
  discovery->destroy(discovery);
  in_callback = on_lpwork = false;
  step();
  struct bt_gatt_chrc_s chrc = {.value_handle = 7};
  struct bt_gatt_attr_s attr = {.handle = 6, .user_data = &chrc};
  on_lpwork = in_callback = true;
  assert(discovery->func(&attr, discovery) == BT_GATT_ITER_CONTINUE);
  discovery->result = BT_ATT_ERR_ATTRIBUTE_NOT_FOUND; /* Normal end of search. */
  discovery->destroy(discovery);
  in_callback = on_lpwork = false;
  drain(); assert(pop(NXRS_GATT_DISCOVER).handle == 7);

  submit(NXRS_GATT_READ, 3);
  uint8_t packet[22]; memset(packet, 42, sizeof(packet));
  on_lpwork = in_callback = true;
  read_callback(&native, 0, packet, sizeof(packet));
  in_callback = on_lpwork = false;
  step(); assert(read_offset == 22);
  on_lpwork = in_callback = true;
  read_callback(&native, 0, packet, 3);
  in_callback = on_lpwork = false;
  drain(); struct nxrs_gatt_event event = pop(NXRS_GATT_READ);
  assert(event.error == 0 && event.length == 25 && event.data[24] == 42);

  submit(NXRS_GATT_WRITE, 4);
  on_lpwork = in_callback = true; write_callback(&native, 0); in_callback = on_lpwork = false;
  drain(); assert(pop(NXRS_GATT_WRITE).error == 0);

  /* Timeout races an already-recorded response: recover wins before publish. */
  submit(NXRS_GATT_READ, 5);
  on_lpwork = in_callback = true; read_callback(&native, 0, packet, 1); in_callback = on_lpwork = false;
  nxrs_ble_gatt_recover(8, 5); step();
  assert(nxrs_ble_gatt_poll(8, &event) == 0 && refs == 1);
  link_down(); drain(); assert(pop(NXRS_GATT_QUIESCENT).operation == 5 && refs == 0);

  /* A clean disconnect permits another link without resetting the adapter. */
  submit(NXRS_GATT_CONNECT, 6);
  on_lpwork = in_callback = true; native.state = BT_CONN_CONNECTED; callbacks->connected(&native, NULL); in_callback = on_lpwork = false;
  drain(); (void)pop(NXRS_GATT_CONNECT);
  submit(NXRS_GATT_DISCONNECT, 7); link_down(); drain();
  assert(pop(NXRS_GATT_DISCONNECT).error == 0 && refs == 0);
  close_link();

  /* Scan-stage cancellation has no GATT disconnected callback. */
  opened(); submit(NXRS_GATT_CONNECT, 1);
  nxrs_ble_gatt_recover(8, 1);
  nxrs_ble_gatt_recover(8, 99); /* Cannot replace the pending drain's token. */
  drain();
  assert(pop(NXRS_GATT_QUIESCENT).operation == 1 && refs == 0);
  close_link();

  /* Cancelled controller connection attempt settles later, also without GATT. */
  opened(); submit(NXRS_GATT_CONNECT, 1); native.state = BT_CONN_CONNECT;
  nxrs_ble_gatt_recover(8, 1); step();
  assert(refs == 1 && queued && queued->delay > 0);
  native.state = BT_CONN_DISCONNECTED;
  drain(); assert(pop(NXRS_GATT_QUIESCENT).operation == 1 && refs == 0);
  close_link();

  /* A scheduling failure rejects the command without accepting native work. */
  opened(); queue_error = EIO;
  struct nxrs_gatt_command cmd = {.operation = 1, .kind = NXRS_GATT_CONNECT, .peer_session = 1, .peer_id = 1};
  assert(nxrs_ble_gatt_submit(8, &cmd) == EIO && !g_gatt.busy);
  close_link();

  /* LE cancel loses the race: a late successful connection must be closed. */
  opened(); submit(NXRS_GATT_CONNECT, 1); native.state = BT_CONN_CONNECT;
  nxrs_ble_gatt_recover(8, 1); step();
  unsigned int stopped = disconnect_calls;
  on_lpwork = in_callback = true;
  native.state = BT_CONN_CONNECTED; callbacks->connected(&native, NULL);
  in_callback = on_lpwork = false;
  step(); assert(disconnect_calls == stopped + 1 && refs == 1);
  link_down(); drain(); assert(pop(NXRS_GATT_QUIESCENT).operation == 1);
  close_link();

  /* A full ATT response is a prefix until a short/empty blob proves its end. */
  assert(nxrs_ble_gatt_open(8, 44, wake, release, &wakes) == 0); opens++;
  link_up(); submit(NXRS_GATT_READ, 2);
  for (unsigned int i = 0; i < 2; i++)
    {
      on_lpwork = in_callback = true;
      read_callback(&native, 0, packet, 22);
      in_callback = on_lpwork = false;
      step();
    }
  assert(read_offset == 44 && nxrs_ble_gatt_poll(8, &event) == 0);
  on_lpwork = in_callback = true; read_callback(&native, 0, NULL, 0); in_callback = on_lpwork = false;
  drain(); event = pop(NXRS_GATT_READ);
  assert(event.error == 0 && event.length == 44);
  close_link();

  /* Oversized reads fail instead of returning a successful 64-byte prefix. */
  opened(); link_up(); submit(NXRS_GATT_READ, 2);
  for (unsigned int i = 0; i < 3; i++)
    {
      on_lpwork = in_callback = true;
      read_callback(&native, 0, packet, 22);
      in_callback = on_lpwork = false;
      if (i < 2) step();
    }
  drain(); assert(pop(NXRS_GATT_READ).error == EINVAL);
  close_link();

  /* Unregister failure retains context and lease until a successful retry. */
  opened(); link_up(); nxrs_ble_gatt_close(8); step(); link_down();
  unregister_error = EIO;
  unsigned int released = releases;
  step(); assert(refs == 0 && releases == released && g_gatt.active);
  assert(nxrs_ble_gatt_open(9, 20, wake, release, &wakes) == EBUSY);
  unregister_error = 0;
  drain(); assert(releases == released + 1 && !g_gatt.active);

  /* Rejection cannot adopt/cancel another owner's existing connection. */
  opened(); existing = true; refs = 1;
  unsigned int cancelled = cancel_calls;
  submit(NXRS_GATT_CONNECT, 1); drain();
  assert(pop(NXRS_GATT_CONNECT).error == EBUSY && refs == 1 && cancel_calls == cancelled);
  existing = false; refs = 0;
  close_link();

  /* The admitted target survives expiry of its source scan registry. */
  opened(); cmd = (struct nxrs_gatt_command){.operation = 1, .kind = NXRS_GATT_CONNECT, .peer_session = 1, .peer_id = 1};
  assert(nxrs_ble_gatt_submit(8, &cmd) == 0);
  registry_live = false; step(); assert(refs == 1);
  registry_live = true;
  nxrs_ble_gatt_close(8); drain(); assert(refs == 0);

  /* A higher-priority owner can consume and submit right inside its wake. */
  opened(); link_up(); submit(NXRS_GATT_WRITE, 2);
  on_lpwork = in_callback = true; write_callback(&native, 0); in_callback = on_lpwork = false;
  submit_on_wake = true; drain();
  assert(!submit_on_wake && g_gatt.command.operation == 3 && g_gatt.busy);
  on_lpwork = in_callback = true; read_callback(&native, 0, packet, 1); in_callback = on_lpwork = false;
  drain(); /* Leave completion queued as the owner's deadline expires. */
  nxrs_ble_gatt_recover(8, 3); step(); link_down(); drain();
  assert(pop(NXRS_GATT_READ).operation == 3);
  assert(pop(NXRS_GATT_QUIESCENT).operation == 3);
  close_link();

  /* No implicit truncation or write-without-response for a long write. */
  opened(); link_up(); cmd = (struct nxrs_gatt_command){.operation = 2, .kind = NXRS_GATT_WRITE, .handle = 7, .length = 21};
  assert(nxrs_ble_gatt_submit(8, &cmd) == 0); drain();
  assert(pop(NXRS_GATT_WRITE).error == ENOTSUP);
  close_link();

  /* A valid service prefix followed by a parser error is not unique success. */
  opened(); link_up(); submit(NXRS_GATT_DISCOVER, 2);
  on_lpwork = in_callback = true;
  discovery->group_end_handle = 12;
  assert(discovery->func(&service, discovery) == BT_GATT_ITER_CONTINUE);
  discovery->result = 0x04; /* Parser's terminal Invalid PDU status. */
  discovery->destroy(discovery);
  in_callback = on_lpwork = false;
  drain(); assert(pop(NXRS_GATT_DISCOVER).att_error == 0x04);
  close_link();
  assert(cancel_calls > 0 && releases == opens);
  return 0;
}

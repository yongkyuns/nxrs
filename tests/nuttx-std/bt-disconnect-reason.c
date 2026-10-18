/* Exercise the actual HCI handler; mock only its kernel/observer boundaries. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#define FAR
#define begin_packed_struct
#define end_packed_struct __attribute__((packed))
#define BT_LE162HOST(value) (value)
#define BT_CONN_AUTO_CONNECT 0
#define BT_HCI_OP_LE_SET_ADV_ENABLE 1
#define wlinfo(...) ((void)0)
#define wlerr(...) ((void)0)
enum bt_conn_state_e { BT_CONN_CONNECTED, BT_CONN_DISCONNECTED, BT_CONN_CONNECT_SCAN };
struct bt_conn_s {
  uint16_t handle;
#include "bt-disconnect-field.h"
  unsigned flags[1];
  enum bt_conn_state_e state;
};
struct bt_buf_s { uint8_t *data; };
#include "bt-disconnect-event.h"
static struct { uint8_t adv_enable; } g_btdev;
static struct bt_conn_s live;
static unsigned lookups, l2cap_calls, observer_calls, releases;
static uint8_t expected_reason;

static struct bt_conn_s *bt_conn_lookup_handle(uint16_t handle) {
  lookups++;
  return handle == live.handle ? &live : NULL;
}
static void bt_l2cap_disconnected(struct bt_conn_s *conn) {
  assert(conn == &live && conn->state == BT_CONN_CONNECTED);
  assert(conn->disconn_reason == expected_reason);
  l2cap_calls++;
}
static void bt_disconnected(struct bt_conn_s *conn) {
  assert(conn == &live && conn->disconn_reason == expected_reason);
  observer_calls++;
}
static void bt_conn_set_state(struct bt_conn_s *conn, enum bt_conn_state_e state) {
  assert(conn == &live && conn->disconn_reason == expected_reason);
  conn->state = state;
}
static void bt_conn_release(struct bt_conn_s *conn) {
  assert(conn == &live && conn->handle == 0);
  releases++;
}
static bool bt_atomic_testbit(unsigned *flags, unsigned bit) {
  return (*flags & (1u << bit)) != 0;
}
static void bt_le_scan_update(void) { }
static struct bt_buf_s *bt_hci_cmd_create(unsigned op, unsigned len) {
  assert(op == BT_HCI_OP_LE_SET_ADV_ENABLE && len == 1);
  return NULL;
}
static void *bt_buf_extend(struct bt_buf_s *buf, unsigned len) {
  (void)len;
  return buf->data;
}
static void bt_hci_cmd_send(unsigned op, struct bt_buf_s *buf) {
  (void)op; (void)buf;
  assert(false);
}
#include "bt-disconnect-functions.h"

static void reset(void) {
  live = (struct bt_conn_s){.handle=0x40, .disconn_reason=43,
                           .state=BT_CONN_CONNECTED};
  lookups = l2cap_calls = observer_calls = releases = 0;
}

int main(void) {
  struct bt_hci_evt_disconn_complete_s event = {.handle=0x40};
  struct bt_buf_s buf = {.data=(uint8_t *)&event};
  _Static_assert(sizeof(live.disconn_reason) == 1, "native reason is one byte");
  for (unsigned reason = 0; reason <= UINT8_MAX; reason++) {
    reset();
    expected_reason = event.reason = reason;
    hci_disconn_complete(&buf);
    assert(live.disconn_reason == reason && live.state == BT_CONN_DISCONNECTED);
    assert(lookups == 1 && l2cap_calls == 1 && observer_calls == 1 && releases == 1);
  }
  reset();
  event.status = 1;
  hci_disconn_complete(&buf);
  assert(live.disconn_reason == 43 && lookups == 0 && releases == 0);
  event.status = 0;
  event.handle = 0x41;
  hci_disconn_complete(&buf);
  assert(live.disconn_reason == 43 && lookups == 1 && releases == 0);
  assert(l2cap_calls == 0 && observer_calls == 0);
  return 0;
}

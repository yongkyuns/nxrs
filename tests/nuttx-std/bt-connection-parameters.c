/* Execute the patched parameter validation, copy, defaults and HCI encoding. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <errno.h>

#define FAR
#define CONFIG_BLUETOOTH_MAX_CONN 3
#define BT_HCI_ROLE_MASTER 0
#define BT_HCI_OP_LE_CREATE_CONN 0x200d
#define BT_LE_ADV_IND 0
#define BT_LE_ADV_DIRECT_IND 1
#define BT_HOST2LE16(x) ((uint16_t)(x))
#define wlerr(...) ((void)0)
typedef struct { uint8_t type; uint8_t val[6]; } bt_addr_le_t;
static const bt_addr_le_t any;
#define BT_ADDR_LE_ANY (&any)
enum { BT_CONN_DISCONNECTED, BT_CONN_CONNECT_SCAN, BT_CONN_CONNECT,
       BT_CONN_CONNECTED };

/* PARAMETERS */

struct bt_conn_s
{
  bt_addr_le_t dst;
  unsigned int role;
  int ref;
  int state;
  struct bt_le_conn_param_s le_create_param;
};
static struct bt_conn_s g_conns[CONFIG_BLUETOOTH_MAX_CONN];
static int lookups, releases, scans, state_changes, commands;
static bool no_buffer;
static int command_result;
static int stop_result, stop_calls;
struct bt_keys_s { bt_addr_le_t addr; };
static int bt_addr_le_cmp(const bt_addr_le_t *a, const bt_addr_le_t *b)
{ return memcmp(a, b, sizeof(*a)); }
static void bt_addr_le_copy(bt_addr_le_t *a, const bt_addr_le_t *b) { *a = *b; }
static void bt_atomic_set(int *value, int input) { *value = input; }
static struct bt_conn_s *bt_conn_lookup_addr_le(const bt_addr_le_t *peer)
{
  lookups++;
  for (unsigned i = 0; i < CONFIG_BLUETOOTH_MAX_CONN; i++)
    if (g_conns[i].ref && !bt_addr_le_cmp(&g_conns[i].dst, peer))
      { g_conns[i].ref++; return &g_conns[i]; }
  return NULL;
}
static void bt_conn_release(struct bt_conn_s *conn) { releases++; conn->ref--; }
static void bt_conn_set_state(struct bt_conn_s *conn, int state)
{ state_changes++; conn->state = state; }
static void bt_le_scan_update(void) { scans++; }
static int bt_hci_stop_scanning(void) { stop_calls++; return stop_result; }
static struct bt_conn_s *bt_conn_lookup_state(const bt_addr_le_t *peer, int state)
{
  for (unsigned i = 0; i < CONFIG_BLUETOOTH_MAX_CONN; i++)
    if (g_conns[i].ref && g_conns[i].state == state &&
        !bt_addr_le_cmp(&g_conns[i].dst, peer))
      { g_conns[i].ref++; return &g_conns[i]; }
  return NULL;
}

struct bt_hci_cp_le_create_conn_s
{
  uint16_t scan_interval, scan_window;
  uint8_t filter_policy;
  bt_addr_le_t peer_addr;
  uint8_t own_addr_type;
  uint16_t conn_interval_min, conn_interval_max, conn_latency;
  uint16_t supervision_timeout, min_ce_len, max_ce_len;
};
struct bt_buf_s { struct bt_hci_cp_le_create_conn_s command; };
static struct bt_buf_s buffer;
static struct bt_buf_s *bt_hci_cmd_create(unsigned opcode, size_t length)
{
  assert(opcode == BT_HCI_OP_LE_CREATE_CONN && length == sizeof(buffer.command));
  memset(&buffer, 0xa5, sizeof(buffer));
  return no_buffer ? NULL : &buffer;
}
static void *bt_buf_extend(struct bt_buf_s *buf, size_t length)
{ assert(buf == &buffer && length == sizeof(buffer.command)); return &buffer.command; }
static int bt_hci_cmd_send_sync(unsigned opcode, struct bt_buf_s *buf, void *response)
{
  assert(opcode == BT_HCI_OP_LE_CREATE_CONN && buf == &buffer && response == NULL);
  commands++; return command_result;
}
static struct bt_conn_s *bt_conn_create_le_with_params(
  const bt_addr_le_t *peer, const struct bt_le_conn_param_s *param);

/* FUNCTIONS */

static void reset(void)
{
  memset(g_conns, 0, sizeof(g_conns));
  lookups = releases = scans = state_changes = commands = command_result = 0;
  no_buffer = false;
  stop_result = stop_calls = 0;
}

int main(void)
{
  bt_addr_le_t peer = {0, {1, 2, 3, 4, 5, 6}};
  struct bt_le_conn_param_s param = {12, 12, 0, 36};
  struct bt_conn_s *conn;
  reset();
  struct interval_storage observed = {.le_conn_interval = 3200};
  assert(observed.le_conn_interval == 3200);
  assert(!le_conn_param_valid(NULL));
  assert(bt_conn_create_le_with_params(NULL, &param) == NULL && lookups == 0);
  assert(bt_conn_create_le_with_params(&peer, NULL) == NULL && lookups == 0);
  for (unsigned field = 0; field < 7; field++)
    {
      struct bt_le_conn_param_s bad = param;
      if (field == 0) bad.interval_min = 5;
      if (field == 1) bad.interval_min = 13;
      if (field == 2) bad.interval_max = 3201;
      if (field == 3) bad.latency = 500;
      if (field == 4) bad.timeout = 9;
      if (field == 5) bad.timeout = 3201;
      if (field == 6) bad.interval_min = bad.interval_max = 65535;
      assert(!le_conn_param_valid(&bad));
      assert(bt_conn_create_le_with_params(&peer, &bad) == NULL);
      assert(lookups == 0 && scans == 0 && state_changes == 0);
    }
  struct bt_le_conn_param_s edge = {40, 40, 0, 10};
  assert(!le_conn_param_valid(&edge));
  edge.timeout = 11; assert(le_conn_param_valid(&edge));
  edge = (struct bt_le_conn_param_s){6, 6, 499, 750};
  assert(!le_conn_param_valid(&edge));
  edge.timeout = 751; assert(le_conn_param_valid(&edge));
  edge = (struct bt_le_conn_param_s){3200, 3200, 2, 3200};
  assert(le_conn_param_valid(&edge));
  edge.latency = 3; assert(!le_conn_param_valid(&edge));

  conn = bt_conn_create_le(&peer);
  assert(conn && conn->le_create_param.interval_min == 24);
  assert(conn->le_create_param.interval_max == 40);
  assert(conn->le_create_param.latency == 0 && conn->le_create_param.timeout == 2000);
  assert(scans == 1 && state_changes == 1 && conn->ref == 1);
  struct bt_conn_s *existing = bt_conn_create_le_with_params(&peer, &param);
  assert(existing == conn && conn->ref == 2 && scans == 1);
  assert(conn->le_create_param.interval_min == 24); /* Not a reconfiguration API. */
  conn->state = BT_CONN_DISCONNECTED;
  assert(bt_conn_create_le_with_params(&peer, &param) == NULL && releases == 1);

  reset();
  conn = bt_conn_create_le_with_params(&peer, &param);
  assert(conn && memcmp(&conn->le_create_param, &param, sizeof(param)) == 0);
  param.timeout = 100;
  assert(conn->le_create_param.timeout == 36); /* Caller storage is not retained. */
  assert(hci_le_create_conn(&peer, &conn->le_create_param) == 0 && commands == 1);
  assert(buffer.command.conn_interval_min == 12 && buffer.command.conn_interval_max == 12);
  assert(buffer.command.conn_latency == 0 && buffer.command.supervision_timeout == 36);
  assert(buffer.command.scan_interval == 96 && buffer.command.scan_window == 48);
  assert(buffer.command.filter_policy == 0 && buffer.command.own_addr_type == 0);
  assert(buffer.command.min_ce_len == 0 && buffer.command.max_ce_len == 0);
  assert(bt_addr_le_cmp(&buffer.command.peer_addr, &peer) == 0);
  no_buffer = true;
  assert(hci_le_create_conn(&peer, &conn->le_create_param) == -ENOBUFS && commands == 1);
  no_buffer = false; command_result = -EIO;
  assert(hci_le_create_conn(&peer, &conn->le_create_param) == -EIO && commands == 2);
  edge = (struct bt_le_conn_param_s){6, 6, 499, 751};
  command_result = 0;
  assert(hci_le_create_conn(&peer, &edge) == 0);
  assert(buffer.command.conn_latency == 499 && buffer.command.supervision_timeout == 751);

  reset();
  conn = bt_conn_add(&peer, 1);
  assert(conn && memcmp(&conn->le_create_param, &g_default_le_param,
                        sizeof(g_default_le_param)) == 0);

  /* The actual advertising callback must use the copied per-connection timing,
   * balance its reference, and leave failed starts in the existing state.
   */
  for (unsigned fault = 0; fault < 3; fault++)
    {
      reset(); param = (struct bt_le_conn_param_s){12, 12, 0, 36};
      conn = bt_conn_create_le_with_params(&peer, &param);
      stop_result = fault == 1 ? -EIO : 0;
      command_result = fault == 2 ? -EIO : 0;
      check_pending_conn(&peer, BT_LE_ADV_IND, NULL);
      assert(stop_calls == 1 && conn->ref == 1);
      assert(commands == (fault == 1 ? 0 : 1));
      assert(conn->state == (fault == 0 ? BT_CONN_CONNECT : BT_CONN_CONNECT_SCAN));
      if (commands)
        assert(buffer.command.conn_interval_min == 12 &&
               buffer.command.supervision_timeout == 36);
    }
  reset();
  conn = bt_conn_create_le_with_params(&peer, &param);
  check_pending_conn(&peer, 4, NULL); /* Non-connectable report. */
  assert(stop_calls == 0 && commands == 0 && conn->ref == 1);
  struct bt_keys_s keys = {peer};
  bt_addr_le_t rpa = {1, {6, 5, 4, 3, 2, 1}};
  check_pending_conn(&rpa, BT_LE_ADV_DIRECT_IND, &keys);
  assert(commands == 1 && conn->ref == 1 && conn->state == BT_CONN_CONNECT);
  assert(bt_addr_le_cmp(&buffer.command.peer_addr, &rpa) == 0);
  assert(buffer.command.conn_interval_min == 12 && buffer.command.supervision_timeout == 36);
  return 0;
}

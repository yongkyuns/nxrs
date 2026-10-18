/* SPDX-License-Identifier: MIT */
/* Host witness for extracted NuttX advertising and ioctl code. */
#include <assert.h>
#include <errno.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define FAR
#define OK 0
#define wlinfo(...) ((void)0)
#define wlerr(...) ((void)0)
#define wlwarn(...) ((void)0)
#define SIOCBTADVSTOP 0xB701
#define BT_HCI_OP_LE_SET_ADV_DATA 1
#define BT_HCI_OP_LE_SET_SCAN_RSP_DATA 2
#define BT_HCI_OP_LE_SET_ADV_PARAMETERS 3
#define BT_HCI_OP_LE_SET_ADV_ENABLE 4
#define BT_HOST2LE16(value) (value)

struct bt_eir_s
{
  uint8_t len;
  uint8_t data[30];
};

struct bt_hci_cp_le_set_adv_data_s
{
  uint8_t len;
  uint8_t data[31];
};

struct bt_hci_cp_le_set_adv_parameters_s
{
  uint16_t min_interval;
  uint16_t max_interval;
  uint8_t type;
  uint8_t own_addr_type;
  uint8_t channel_map;
};

struct bt_buf_s
{
  uint16_t opcode;
  size_t len;
  uint8_t payload[64];
  uint8_t data[128];
};

typedef struct
{
  uint8_t val[6];
} bt_addr_t;

typedef struct
{
  uint8_t type;
  uint8_t val[6];
} bt_addr_le_t;

struct bt_conn_s
{
  uint16_t handle;
  struct { uint8_t type; uint8_t val[6]; } src;
  uint16_t le_conn_interval;
};

struct bt_keys_s { bt_addr_le_t addr; };

#include "bt-advertising-roles.h"
#define BT_ADDR_LE_PUBLIC 0
#define BT_CONN_CONNECT 1
#define BT_CONN_DISCONNECTED 2
#define BT_CONN_CONNECTED 3
#define BT_LE162HOST(value) (value)

struct bt_hci_evt_le_conn_complete_s
{
  uint8_t status;
  uint16_t handle;
  uint8_t role;
  bt_addr_le_t peer_addr;
  uint16_t interval;
};

struct bt_dev_s
{
  uint8_t adv_enable;
  bt_addr_t bdaddr;
};

static struct bt_dev_s g_btdev;
static struct bt_buf_s command;
static unsigned command_count;
static unsigned fail_command;
static unsigned fail_allocation;
static unsigned allocation_count;
static unsigned fail_conn_add;
static unsigned conn_add_count;
static struct bt_conn_s connection;
static int connection_state;
static uint16_t observed[16];
static const struct bt_eir_s advertisement[] = {{2, {0x01, 0x02}}, {0, {0}}};
static const struct bt_eir_s scan_response[] = {{1, {0x03}}, {0, {0}}};

static int baseline_stop(void);
static int patched_stop(void);

static struct bt_buf_s *bt_hci_cmd_create(uint16_t opcode, size_t len)
{
  allocation_count++;
  if (allocation_count == fail_allocation)
    {
      return NULL;
    }

  assert(len <= sizeof(command.payload));
  memset(&command, 0, sizeof(command));
  command.opcode = opcode;
  return &command;
}

static void *bt_buf_extend(struct bt_buf_s *buf, size_t len)
{
  assert(buf->len + len <= sizeof(buf->payload));
  void *result = &buf->payload[buf->len];
  buf->len += len;
  return result;
}

static int bt_hci_cmd_send_sync(uint16_t opcode, struct bt_buf_s *buf,
                                struct bt_buf_s **response)
{
  assert(buf == &command);
  assert(opcode == buf->opcode);
  assert(response == NULL);
  observed[command_count] = opcode;
  command_count++;
  return command_count == fail_command ? -EIO : 0;
}

static void bt_hci_cmd_send(uint16_t opcode, struct bt_buf_s *buf)
{
  assert(buf == &command);
  assert(opcode == buf->opcode);
  observed[command_count] = opcode;
  command_count++;
}

static struct bt_keys_s *bt_keys_find_irk(const bt_addr_le_t *addr)
{
  (void)addr;
  return NULL;
}

static struct bt_conn_s *bt_conn_lookup_state(const bt_addr_le_t *addr,
                                              int state)
{
  (void)addr;
  (void)state;
  return NULL;
}

static struct bt_conn_s *bt_conn_add(const bt_addr_le_t *addr, uint8_t role)
{
  (void)addr;
  (void)role;
  conn_add_count++;
  return fail_conn_add ? NULL : &connection;
}

static void bt_conn_set_state(struct bt_conn_s *conn, int state)
{
  (void)conn;
  connection_state = state;
}

static void bt_conn_release(struct bt_conn_s *conn) { (void)conn; }
static void copy_id_addr(struct bt_conn_s *conn, const bt_addr_le_t *addr)
{ (void)conn; (void)addr; }
static void bt_l2cap_connected(struct bt_conn_s *conn) { (void)conn; }
static void bt_connected(struct bt_conn_s *conn) { (void)conn; }
static void bt_le_scan_update(void) { }

#include "bt-advertising-functions.h"

static void reset(unsigned enabled)
{
  memset(&command, 0, sizeof(command));
  memset(observed, 0, sizeof(observed));
  g_btdev.adv_enable = enabled;
  command_count = 0;
  fail_command = 0;
  allocation_count = 0;
  fail_allocation = 0;
  fail_conn_add = 0;
  conn_add_count = 0;
  connection_state = 0;
}

static void check_conn_complete(void (*complete)(struct bt_buf_s *),
                                uint8_t status, uint8_t role,
                                unsigned initial, unsigned allocation_fails,
                                unsigned expected)
{
  struct bt_hci_evt_le_conn_complete_s event = {0};
  struct bt_buf_s buf = {0};
  g_btdev.adv_enable = initial;
  event.status = status;
  event.role = role;
  event.handle = 7;
  memcpy(buf.data, &event, sizeof(event));
  fail_conn_add = allocation_fails;
  conn_add_count = 0;
  connection_state = 0;
  complete(&buf);
  assert(g_btdev.adv_enable == expected);
  assert(conn_add_count == (status ? 0u : 1u));
  if (!status && !allocation_fails)
    {
      assert(connection_state == BT_CONN_CONNECTED);
    }
  if (expected == 0)
    {
      unsigned before_commands = command_count;
      unsigned before_allocations = allocation_count;
      assert(patched_stop() == 0);
      assert(command_count == before_commands);
      assert(allocation_count == before_allocations);
    }
  fail_conn_add = 0;
}

static void check_advertising_sequence(void)
{
  assert(command_count == 4);
  assert(observed[0] == BT_HCI_OP_LE_SET_ADV_DATA);
  assert(observed[1] == BT_HCI_OP_LE_SET_SCAN_RSP_DATA);
  assert(observed[2] == BT_HCI_OP_LE_SET_ADV_PARAMETERS);
  assert(observed[3] == BT_HCI_OP_LE_SET_ADV_ENABLE);
}

int main(void)
{
  int ret;
  unsigned stage;

  /* Every synchronous setup stage reports its failure and stops the sequence. */
  for (stage = 1; stage <= 3; stage++)
    {
      reset(0);
      fail_command = stage;
      ret = patched_start(0, advertisement, scan_response);
      assert(ret == -EIO);
      assert(command_count == stage);
      assert(g_btdev.adv_enable == 0);
    }

  /* Allocation failures at each setup stage also propagate without enabling. */
  for (stage = 1; stage <= 3; stage++)
    {
      reset(0);
      fail_allocation = stage;
      ret = patched_start(0, advertisement, scan_response);
      assert(ret == -ENOBUFS);
      assert(command_count == stage - 1);
      assert(g_btdev.adv_enable == 0);
    }

  reset(0);
  fail_command = 4;
  ret = patched_start(0, advertisement, scan_response);
  assert(ret == -EIO);
  assert(command_count == 4);
  assert(g_btdev.adv_enable == 0);

  reset(1);
  fail_command = 1;
  ret = patched_stop();
  assert(ret == -EIO);
  assert(command_count == 1);
  assert(g_btdev.adv_enable == 1);

  /* A failed stop while restarting aborts before sending setup commands. */
  reset(1);
  fail_command = 1;
  ret = patched_start(0, advertisement, scan_response);
  assert(ret == -EIO);
  assert(command_count == 1);
  assert(observed[0] == BT_HCI_OP_LE_SET_ADV_ENABLE);
  assert(g_btdev.adv_enable == 1);

  reset(0);
  ret = patched_start(0, advertisement, scan_response);
  assert(ret == 0);
  assert(g_btdev.adv_enable == 1);
  check_advertising_sequence();

  reset(1);
  ret = patched_stop();
  assert(ret == 0);
  assert(command_count == 1);
  assert(g_btdev.adv_enable == 0);

  reset(0);
  ret = patched_stop();
  assert(ret == 0);
  assert(command_count == 0);
  assert(allocation_count == 0);

  reset(0);
  assert(baseline_stop() == -EALREADY);
  assert(command_count == 0);
  assert(allocation_count == 0);

  reset(1);
  fail_allocation = 1;
  assert(patched_stop() == -ENOBUFS);
  assert(command_count == 0);
  assert(g_btdev.adv_enable == 1);

  check_conn_complete(patched_conn_complete, 0, BT_HCI_ROLE_SLAVE, 1, 0, 0);
  check_conn_complete(patched_conn_complete, 0, BT_HCI_ROLE_MASTER, 1, 0, 1);
  check_conn_complete(patched_conn_complete, 1, BT_HCI_ROLE_SLAVE, 1, 0, 1);
  check_conn_complete(patched_conn_complete, 0, BT_HCI_ROLE_SLAVE, 1, 1, 0);
  check_conn_complete(baseline_conn_complete, 0, BT_HCI_ROLE_SLAVE, 1, 0, 1);

  reset(1);
  fail_command = 1;
  assert(patched_ioctl_stop() == -EIO);
  assert(g_btdev.adv_enable == 1);

  /* Baseline witnesses reproduce the lost error/state and ignored restart failure. */
  reset(0);
  fail_command = 4;
  assert(baseline_start(0, advertisement, scan_response) == -EIO);
  assert(g_btdev.adv_enable == 1);

  reset(1);
  fail_command = 1;
  assert(baseline_stop() == -EIO);
  assert(g_btdev.adv_enable == 0);

  reset(1);
  fail_command = 1;
  assert(baseline_start(0, advertisement, scan_response) == 0);
  assert(command_count > 1);

  reset(1);
  fail_command = 1;
  assert(baseline_ioctl_stop() == 0);
  return 0;
}

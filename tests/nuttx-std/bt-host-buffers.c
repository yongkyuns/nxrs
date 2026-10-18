/* SPDX-License-Identifier: MIT */
/* Host witness for extracted NuttX Bluetooth buffer and host-flow code. */
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define FAR
#define DEBUGASSERT(condition) assert(condition)
#define wlinfo(...) ((void)0)
#define wlerr(...) ((void)0)
#define CONFIG_BLUETOOTH_BUFFER_PREALLOC 20
#define BT_HCI_OP_HOST_BUFFER_SIZE 0x0c33
#define BT_HCI_OP_SET_CTL_TO_HOST_FLOW 0x0c31
#define BT_HOST2LE16(value) (value)
#define MIN(a, b) ((a) < (b) ? (a) : (b))
#define BLUETOOTH_MAX_MTU 70
#define BLUETOOTH_MAX_HDRLEN 9
#define BLUETOOTH_H4_HDRLEN 1

struct iob_s { uint16_t io_offset, io_len; uint8_t io_data[CONFIG_IOB_BUFSIZE]; };
struct bt_hci_cp_host_buffer_size_s
{ uint16_t acl_mtu; uint8_t sco_mtu; uint16_t acl_pkts, sco_pkts; }
  __attribute__((packed));
struct bt_hci_acl_hdr_s { uint16_t handle, len; };
struct bt_hci_cp_set_ctl_to_host_flow_s { uint8_t enable; };
struct bt_driver_s { uint8_t head_reserve; };
struct bt_dev_s { struct bt_driver_s *btdev; };
static struct bt_dev_s g_btdev;

#include "bt-host-buffer-types.h"
size_t bt_buf_headroom(struct bt_buf_s *buf);
size_t bt_buf_tailroom(struct bt_buf_s *buf);
#define bt_buf_tail(buf) ((buf)->data + (buf)->len)
#include "bt-host-buffer-functions.h"

struct command_slot { struct bt_buf_s buf; struct iob_s frame; };
static struct command_slot commands[2];
static unsigned create_count, fail_create_at, received_acl_credits;
static bool reject_host_buffer, reject_flow, flow_enabled;
static uint16_t advertised_acl_mtu;

static struct bt_buf_s *bt_hci_cmd_create(uint16_t opcode, uint8_t param_len)
{
  struct command_slot *slot;
  (void)opcode;
  (void)param_len;
  if (++create_count == fail_create_at || create_count > 2) return NULL;
  slot = &commands[create_count - 1];
  memset(slot, 0, sizeof(*slot));
  slot->buf.frame = &slot->frame;
  slot->buf.data = slot->frame.io_data;
  return &slot->buf;
}

static int controller_command(uint16_t opcode, struct bt_buf_s *buf)
{
  if (opcode == BT_HCI_OP_HOST_BUFFER_SIZE)
    {
      struct bt_hci_cp_host_buffer_size_s *hbs = (void *)buf->data;
      advertised_acl_mtu = hbs->acl_mtu;
      if (reject_host_buffer || hbs->acl_mtu < 251) return -EIO;
      received_acl_credits = hbs->acl_pkts;
    }
  else if (opcode == BT_HCI_OP_SET_CTL_TO_HOST_FLOW)
    {
      if (reject_flow) return -ETIMEDOUT;
      flow_enabled = *(uint8_t *)buf->data == 1;
    }
  return 0;
}

#ifdef EXPECT_BASELINE_BUGS
static int bt_hci_cmd_send(uint16_t opcode, struct bt_buf_s *buf)
{
  (void)controller_command(opcode, buf);
  return 0; /* Queueing succeeds even when the controller rejects it. */
}
#endif

static int bt_hci_cmd_send_sync(uint16_t opcode, struct bt_buf_s *buf,
                                struct bt_buf_s **response)
{
  (void)response;
  return controller_command(opcode, buf);
}

#include "bt-host-buffer-init.h"

static void reset_init(bool reject_host, bool reject_flow_command,
                       unsigned allocation_failure)
{
  memset(commands, 0, sizeof(commands));
  create_count = 0;
  fail_create_at = allocation_failure;
  reject_host_buffer = reject_host;
  reject_flow = reject_flow_command;
  flow_enabled = false;
  received_acl_credits = 0;
  advertised_acl_mtu = 0;
}

static void test_buffer_capacity_and_reuse(void)
{
  struct iob_s frame = {0};
  struct bt_buf_s buf = {0};
  const size_t capacity = BLUETOOTH_MAX_FRAMELEN;
  buf.frame = &frame;
  buf.data = frame.io_data + 1; /* Driver reserved an H4 byte. */
  assert(bt_buf_headroom(&buf) == 1 && bt_buf_tailroom(&buf) == capacity - 1);
  memset(bt_buf_extend(&buf, capacity - 1), 0x5a, capacity - 1);
  assert(bt_buf_tailroom(&buf) == 0);
  assert(bt_buf_provide(&buf, 1) == frame.io_data);
#ifdef EXPECT_BASELINE_BUGS
  assert(sizeof(buf.len) == 1);
#else
  assert(sizeof(buf.len) == 2);
#endif
  assert(buf.len == capacity);
  assert(bt_buf_headroom(&buf) == 0);
  for (unsigned i = 0; i < 100; i++)
    {
      memset(&buf, 0, sizeof(buf));
      memset(&frame, 0, sizeof(frame));
      buf.frame = &frame;
      buf.data = frame.io_data + 1;
      *(uint8_t *)bt_buf_extend(&buf, 1) = (uint8_t)i;
      assert(frame.io_data[1] == (uint8_t)i &&
             bt_buf_tailroom(&buf) == capacity - 2);
      bt_buf_consume(&buf, 1);
      assert(buf.len == 0 && bt_buf_headroom(&buf) == 2);
    }
}

static void test_host_buffer_initialization(void)
{
  struct bt_driver_s driver;
  g_btdev.btdev = &driver;
  for (unsigned reserve = 0; reserve <= 1; reserve++)
    {
      driver.head_reserve = reserve;
      reset_init(false, false, 0);
#ifdef EXPECT_BASELINE_BUGS
      assert(configure_host_buffers() == 0);
      assert(advertised_acl_mtu == BLUETOOTH_MAX_FRAMELEN - 4 - reserve);
      assert(received_acl_credits == 0 && flow_enabled);
#else
      const unsigned expected_mtu = BLUETOOTH_MAX_FRAMELEN - 5;
      assert(configure_host_buffers() == (expected_mtu < 251 ? -EIO : 0));
      assert(advertised_acl_mtu == expected_mtu);
      assert(received_acl_credits == (expected_mtu < 251 ? 0 : 20));
      assert(flow_enabled == (expected_mtu >= 251));
#endif
    }
}

static void test_initialization_errors(void)
{
  struct bt_driver_s driver = {.head_reserve = 1};
  g_btdev.btdev = &driver;
  reset_init(true, false, 0);
#ifdef EXPECT_BASELINE_BUGS
  assert(configure_host_buffers() == 0 && flow_enabled);
#else
  assert(configure_host_buffers() == -EIO && !flow_enabled);
#endif
  if (CONFIG_IOB_BUFSIZE >= 256)
    {
      reset_init(false, true, 0);
      assert(configure_host_buffers() == -ETIMEDOUT && !flow_enabled);
    }
  for (unsigned fail = 1; fail <= 2; fail++)
    {
      if (fail == 2 && CONFIG_IOB_BUFSIZE < 256) continue;
      reset_init(false, false, fail);
      assert(configure_host_buffers() == -ENOBUFS);
      assert(!flow_enabled);
    }
}

static void test_receive_boundary(void)
{
  assert(receive_bound(BLUETOOTH_MAX_FRAMELEN - BLUETOOTH_H4_HDRLEN) == 0);
#ifdef EXPECT_BASELINE_BUGS
  assert(receive_bound(BLUETOOTH_MAX_FRAMELEN) == 0);
  assert(receive_bound(SIZE_MAX) == 0); /* Addition wraps before comparison. */
#else
  assert(receive_bound(BLUETOOTH_MAX_FRAMELEN) == -EINVAL);
  assert(receive_bound(SIZE_MAX) == -EINVAL);
#endif
}

int main(void)
{
  test_buffer_capacity_and_reuse();
  test_host_buffer_initialization();
  test_initialization_errors();
  test_receive_boundary();
  return 0;
}

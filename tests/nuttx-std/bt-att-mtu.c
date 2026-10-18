/* SPDX-License-Identifier: MIT */
/* Host witness for the extracted NuttX ATT MTU and receive functions. */
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define FAR
#define CODE
#define DEBUGASSERT(condition) assert(condition)
#define wlinfo(...) ((void)0)
#define wlerr(...) ((void)0)
#define MIN(a, b) ((a) < (b) ? (a) : (b))
#define MAX(a, b) ((a) > (b) ? (a) : (b))
#define BT_HOST2LE16(value) (value)
#define BT_LE162HOST(value) (value)
#ifndef CONFIG_IOB_BUFSIZE
#  define CONFIG_IOB_BUFSIZE 400
#endif
#define BLUETOOTH_MAX_FRAMELEN \
  MIN(CONFIG_IOB_BUFSIZE, 256)
#define BLUETOOTH_H4_HDRLEN 1

#define BT_ATT_DEFAULT_LE_MTU 23
#define BT_ATT_MAX_LE_MTU 517
#define BT_ATT_ERR_INVALID_PDU 0x04
#define BT_ATT_ERR_NOT_SUPPORTED 0x06
#define BT_ATT_ERR_UNLIKELY 0x0e
#define BT_ATT_OP_ERROR_RSP 0x01
#define BT_ATT_OP_MTU_REQ 0x02
#define BT_ATT_OP_MTU_RSP 0x03
#define BT_ATT_OP_WRITE_CMD 0x52
#define BT_ATT_OP_SIGNED_WRITE_CMD 0xd2
#define BT_L2CAP_CID_ATT 0x0004

struct bt_att_hdr_s
{
  uint8_t code;
} __attribute__((packed));

struct bt_att_error_rsp_s
{
  uint8_t request;
  uint16_t handle;
  uint8_t error;
} __attribute__((packed));

struct bt_att_exchange_mtu_req_s
{
  uint16_t mtu;
} __attribute__((packed));

struct bt_att_exchange_mtu_rsp_s
{
  uint16_t mtu;
} __attribute__((packed));

struct bt_l2cap_hdr_s
{
  uint16_t len;
  uint16_t cid;
} __attribute__((packed));

struct bt_hci_acl_hdr_s
{
  uint16_t handle;
  uint16_t len;
} __attribute__((packed));

struct bt_buf_s
{
  uint8_t *data;
  size_t len;
  size_t headroom;
  uint8_t storage[CONFIG_IOB_BUFSIZE];
};

struct bt_att_s
{
  uint16_t mtu;
};

struct bt_conn_s
{
  struct bt_att_s *att;
};

struct bt_driver_s
{
  uint8_t head_reserve;
};

struct bt_dev_s
{
  struct bt_driver_s *btdev;
};

static struct bt_dev_s g_btdev;

static size_t bt_buf_tailroom(struct bt_buf_s *buf)
{
  return BLUETOOTH_MAX_FRAMELEN - buf->headroom - buf->len;
}

static void *bt_buf_extend(struct bt_buf_s *buf, size_t len)
{
  void *tail = buf->data + buf->len;
  assert(len <= bt_buf_tailroom(buf));
  buf->len += len;
  return tail;
}

static struct bt_buf_s *bt_att_create_pdu(struct bt_conn_s *conn,
                                          uint8_t opcode, size_t len);
static void bt_l2cap_send(struct bt_conn_s *conn, uint16_t cid,
                          struct bt_buf_s *buf);
static void bt_buf_release(struct bt_buf_s *buf);
static void bt_buf_consume(struct bt_buf_s *buf, size_t len);
static uint8_t att_mtu_req(struct bt_conn_s *conn, struct bt_buf_s *data);
static uint8_t att_write_cmd(struct bt_conn_s *conn, struct bt_buf_s *data);
static void send_err_rsp(struct bt_conn_s *, uint8_t, uint16_t, uint8_t);

struct handler_info_s
{
  uint8_t op;
  uint8_t (*func)(struct bt_conn_s *conn, struct bt_buf_s *buf);
  uint8_t expect_len;
};

static const struct handler_info_s g_handlers[] =
{
  {BT_ATT_OP_MTU_REQ, att_mtu_req,
   sizeof(struct bt_att_exchange_mtu_req_s)},
  {BT_ATT_OP_WRITE_CMD, att_write_cmd, 1},
  {BT_ATT_OP_SIGNED_WRITE_CMD, att_write_cmd, 1},
};

#define NHANDLERS (sizeof(g_handlers) / sizeof(g_handlers[0]))

/* These bodies are copied from external/nuttx/wireless/bluetooth/bt_att.c
 * by the harness driver.  The header is generated for baseline and patched
 * builds, so the witness never carries a second implementation.
 */
#include "bt-att-mtu-mask.h"
#include "bt-att-mtu-functions.h"

struct buffer_slot
{
  bool allocated;
  struct bt_buf_s buf;
};

static struct buffer_slot slots[16];
static unsigned allocation_count;
static unsigned fail_allocation_at;
static unsigned release_count;
static unsigned send_count;
static unsigned error_response_count;
static unsigned mtu_response_count;
static uint8_t last_response_opcode;
static uint8_t last_error_request;
static uint8_t last_error;
static uint16_t last_response_mtu;
static uint16_t conn_mtu_at_send;
static unsigned conn_mtu_send_count;

static struct bt_buf_s *bt_att_create_pdu(struct bt_conn_s *conn,
                                          uint8_t opcode, size_t len)
{
  struct buffer_slot *slot;
  size_t headroom;

  (void)conn;
  (void)len;
  allocation_count++;
  if (allocation_count == fail_allocation_at)
    {
      return NULL;
    }

  for (size_t i = 0; i < sizeof(slots) / sizeof(slots[0]); i++)
    {
      if (!slots[i].allocated)
        {
          slot = &slots[i];
          memset(&slot->buf, 0, sizeof(slot->buf));
          slot->allocated = true;
          headroom = sizeof(struct bt_l2cap_hdr_s) +
                     sizeof(struct bt_hci_acl_hdr_s) +
                     g_btdev.btdev->head_reserve;
          slot->buf.headroom = headroom;
          slot->buf.data = slot->buf.storage + headroom;
          *(uint8_t *)bt_buf_extend(&slot->buf, 1) = opcode;
          return &slot->buf;
        }
    }

  return NULL;
}

static void bt_l2cap_send(struct bt_conn_s *conn, uint16_t cid,
                          struct bt_buf_s *buf)
{
  (void)conn;
  assert(cid == BT_L2CAP_CID_ATT);
  send_count++;
  last_response_opcode = buf->data[0];
  if (last_response_opcode == BT_ATT_OP_ERROR_RSP)
    {
      const struct bt_att_error_rsp_s *rsp =
        (const struct bt_att_error_rsp_s *)(buf->data + 1);
      error_response_count++;
      last_error_request = rsp->request;
      last_error = rsp->error;
    }
  else if (last_response_opcode == BT_ATT_OP_MTU_RSP)
    {
      const struct bt_att_exchange_mtu_rsp_s *rsp =
        (const struct bt_att_exchange_mtu_rsp_s *)(buf->data + 1);
      mtu_response_count++;
      last_response_mtu = rsp->mtu;
    }

  if (conn != NULL && conn->att != NULL)
    {
      conn_mtu_at_send = conn->att->mtu;
      conn_mtu_send_count++;
    }

  bt_buf_release(buf);
}

static void bt_buf_release(struct bt_buf_s *buf)
{
  for (size_t i = 0; i < sizeof(slots) / sizeof(slots[0]); i++)
    {
      if (&slots[i].buf == buf)
        {
          assert(slots[i].allocated);
          slots[i].allocated = false;
          release_count++;
          return;
        }
    }

  assert(!"Releasing a buffer not owned by the host pool");
}

static void bt_buf_consume(struct bt_buf_s *buf, size_t len)
{
  assert(len <= buf->len);
  buf->data += len;
  buf->len -= len;
}

static uint8_t att_write_cmd(struct bt_conn_s *conn, struct bt_buf_s *data)
{
  (void)conn;
  (void)data;
  return BT_ATT_ERR_INVALID_PDU;
}

static size_t expected_local_mtu(void)
{
  size_t tx_reserve = MAX(g_btdev.btdev->head_reserve,
                          BLUETOOTH_H4_HDRLEN);
  size_t overhead = sizeof(struct bt_l2cap_hdr_s) +
                    sizeof(struct bt_hci_acl_hdr_s) + tx_reserve;
  return MIN(BT_ATT_MAX_LE_MTU, BLUETOOTH_MAX_FRAMELEN - overhead);
}

static void reset_case(struct bt_conn_s *conn, struct bt_att_s *att,
                       struct bt_driver_s *driver, uint8_t tx_reserve)
{
  memset(slots, 0, sizeof(slots));
  memset(&att[0], 0, sizeof(att[0]));
  memset(conn, 0, sizeof(*conn));
  driver->head_reserve = tx_reserve;
  g_btdev.btdev = driver;
  att->mtu = BT_ATT_DEFAULT_LE_MTU;
  conn->att = att;
  allocation_count = 0;
  fail_allocation_at = 0;
  release_count = 0;
  send_count = 0;
  error_response_count = 0;
  mtu_response_count = 0;
  last_response_opcode = 0;
  last_error_request = 0;
  last_error = 0;
  last_response_mtu = 0;
  conn_mtu_at_send = 0;
  conn_mtu_send_count = 0;
}

static struct bt_buf_s *incoming_pdu(uint8_t opcode, const void *payload,
                                      size_t payload_len)
{
  struct buffer_slot *slot = NULL;

  for (size_t i = 0; i < sizeof(slots) / sizeof(slots[0]); i++)
    {
      if (!slots[i].allocated)
        {
          slot = &slots[i];
          memset(&slot->buf, 0, sizeof(slot->buf));
          slot->allocated = true;
          break;
        }
    }

  assert(slot != NULL);
  slot->buf.data = slot->buf.storage;
  slot->buf.storage[0] = opcode;
  if (payload_len != 0)
    {
      memcpy(slot->buf.storage + 1, payload, payload_len);
    }

  slot->buf.len = payload_len + 1;
  return &slot->buf;
}

static void receive(struct bt_conn_s *conn, uint8_t opcode,
                    const void *payload, size_t payload_len)
{
  struct bt_buf_s *buf = incoming_pdu(opcode, payload, payload_len);
  bt_att_receive(conn, buf, NULL, BT_L2CAP_CID_ATT);
}

static void test_mtu_request_range_and_advertisement(void)
{
  struct bt_conn_s conn;
  struct bt_att_s att;
  struct bt_driver_s driver;
  const uint16_t requested = 527;

  reset_case(&conn, &att, &driver, 0);
  receive(&conn, BT_ATT_OP_MTU_REQ, &requested, sizeof(requested));
#ifdef EXPECT_BASELINE_BUGS
  assert(error_response_count == 0 && mtu_response_count == 0);
  assert(att.mtu == BT_ATT_DEFAULT_LE_MTU);
  assert(release_count == 1);
#else
  assert(error_response_count == 0 && mtu_response_count == 1);
  assert(last_response_mtu == expected_local_mtu());
  assert(att.mtu == MIN(requested, expected_local_mtu()));
  assert(release_count == 2);
#endif

  reset_case(&conn, &att, &driver, 0);
  {
    const uint16_t minimum = BT_ATT_DEFAULT_LE_MTU;
    receive(&conn, BT_ATT_OP_MTU_REQ, &minimum, sizeof(minimum));
    assert(mtu_response_count == 1 && error_response_count == 0);
#ifdef EXPECT_BASELINE_BUGS
    assert(last_response_mtu == BT_ATT_DEFAULT_LE_MTU);
#else
    assert(last_response_mtu == expected_local_mtu());
#endif
    assert(att.mtu == BT_ATT_DEFAULT_LE_MTU);
  }

  reset_case(&conn, &att, &driver, 0);
  {
    const uint16_t maximum = BT_ATT_MAX_LE_MTU;
    receive(&conn, BT_ATT_OP_MTU_REQ, &maximum, sizeof(maximum));
#ifdef EXPECT_BASELINE_BUGS
    assert(last_response_mtu == expected_local_mtu() + 1);
#else
    assert(last_response_mtu == expected_local_mtu());
#endif
  }
}

static void test_request_errors_and_command_suppression(void)
{
  struct bt_conn_s conn;
  struct bt_att_s att;
  struct bt_driver_s driver;
  const uint16_t too_small = BT_ATT_DEFAULT_LE_MTU - 1;

  reset_case(&conn, &att, &driver, 0);
  receive(&conn, BT_ATT_OP_MTU_REQ, &too_small, sizeof(too_small));
#ifdef EXPECT_BASELINE_BUGS
  assert(error_response_count == 0);
#else
  assert(error_response_count == 1 && last_error == BT_ATT_ERR_INVALID_PDU);
  assert(last_error_request == BT_ATT_OP_MTU_REQ);
#endif
  assert(release_count == 1 + error_response_count);

  reset_case(&conn, &att, &driver, 0);
  receive(&conn, BT_ATT_OP_MTU_REQ, NULL, 0);
#ifdef EXPECT_BASELINE_BUGS
  assert(error_response_count == 0);
#else
  assert(error_response_count == 1 && last_error == BT_ATT_ERR_INVALID_PDU);
  assert(last_error_request == BT_ATT_OP_MTU_REQ);
#endif

  reset_case(&conn, &att, &driver, 0);
  fail_allocation_at = 1;
  {
    const uint16_t valid_mtu = BT_ATT_DEFAULT_LE_MTU;
    receive(&conn, BT_ATT_OP_MTU_REQ, &valid_mtu, sizeof(valid_mtu));
  }
  assert(mtu_response_count == 0);
#ifdef EXPECT_BASELINE_BUGS
  assert(allocation_count == 1);
  assert(error_response_count == 0);
#else
  assert(allocation_count == 2);
  assert(error_response_count == 1 && last_error == BT_ATT_ERR_UNLIKELY);
  assert(last_error_request == BT_ATT_OP_MTU_REQ);
#endif

  for (unsigned i = 0; i < 2; i++)
    {
      const uint8_t command = i == 0 ? BT_ATT_OP_WRITE_CMD :
                                      BT_ATT_OP_SIGNED_WRITE_CMD;
      reset_case(&conn, &att, &driver, 0);
      receive(&conn, command, NULL, 0);
      assert(send_count == 0 && error_response_count == 0);
      assert(release_count == 1);
    }
}

static void test_unknown_opcodes_and_repeated_cleanup(void)
{
  struct bt_conn_s conn;
  struct bt_att_s att;
  struct bt_driver_s driver;
  const uint8_t unknown_ops[] = {0x10, 0x12};

  for (size_t i = 0; i < sizeof(unknown_ops); i++)
    {
      reset_case(&conn, &att, &driver, 0);
      receive(&conn, unknown_ops[i], NULL, 0);
#ifdef EXPECT_BASELINE_BUGS
      assert(error_response_count == 0);
#else
      assert(error_response_count == 1);
      assert(last_error == BT_ATT_ERR_NOT_SUPPORTED);
      assert(last_error_request == unknown_ops[i]);
#endif
    }

  reset_case(&conn, &att, &driver, 0);
  for (unsigned i = 0; i < 100; i++)
    {
      receive(&conn, BT_ATT_OP_MTU_REQ, NULL, 0);
    }

#ifdef EXPECT_BASELINE_BUGS
  assert(release_count == 100);
  assert(send_count == 0);
#else
  assert(release_count == 200);
  assert(send_count == 100);
  assert(error_response_count == 100);
#endif
}

int main(void)
{
  test_mtu_request_range_and_advertisement();
  test_request_errors_and_command_suppression();
  test_unknown_opcodes_and_repeated_cleanup();
  return 0;
}

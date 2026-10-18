/* SPDX-License-Identifier: MIT */
/* Exercise actual patched ATT terminal functions, not a second implementation. */
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>

#define FAR
#define wlinfo(...) ((void)0)
#define BT_ATT_ERR_UNLIKELY 0x0e
#define BT_L2CAP_CID_ATT 4

struct bt_conn_s;
typedef void (*bt_att_func_t)(struct bt_conn_s *, uint8_t, const void *,
                             uint16_t, void *);
typedef void (*bt_att_destroy_t)(void *);
/* TYPES */
struct bt_conn_s { struct bt_att_s *att; unsigned int handle; };
struct bt_att_hdr_s { uint8_t code; };
struct bt_buf_s { void *data; };

static unsigned int g_notifications;
static unsigned int g_sends;
static void bt_gatt_disconnected(struct bt_conn_s *conn)
{
  assert(conn->att == NULL);
  g_notifications++;
}

static void bt_l2cap_send(struct bt_conn_s *conn, uint16_t cid,
                          struct bt_buf_s *buf)
{
  assert(conn->att != NULL && cid == BT_L2CAP_CID_ATT && buf != NULL);
  g_sends++;
}

void bt_att_cancel(struct bt_conn_s *conn);
/* FUNCTIONS */

struct witness
{
  struct bt_conn_s *conn;
  unsigned int calls;
  unsigned int destroys;
  uint8_t error;
  bool disconnect;
  bool chain;
  bool reenter;
};

static void destroyed(void *arg)
{
  struct witness *w = arg;
  w->destroys++;
  assert(w->destroys == 1);
  if (w->reenter)
    {
      bt_att_cancel(w->conn);
    }
}

static void response(struct bt_conn_s *conn, uint8_t err, const void *pdu,
                     uint16_t length, void *arg)
{
  struct witness *w = arg;
  struct bt_att_hdr_s header = {.code = 0x0a};
  struct bt_buf_s buffer = {.data = &header};
  w->calls++;
  w->error = err;
  assert(w->calls == 1 && w->destroys == 0 && w->conn == conn);
  assert(pdu == NULL && length == 0);
  if (w->disconnect)
    {
      assert(conn->att == NULL);
      assert(g_notifications == 0); /* Request resolves before link observers. */
      assert(bt_att_send(conn, &buffer, response, w, destroyed) == -ENOTCONN);
    }
  else
    {
      /* A detached response must not leave an old destructor in the slot. */
      assert(conn->att->req.func == NULL);
      assert(conn->att->req.user_data == NULL);
      assert(conn->att->req.destroy == NULL);
      if (w->reenter)
        {
          bt_att_cancel(conn);
          assert(w->destroys == 0);
        }
      if (w->chain)
        {
          assert(bt_att_send(conn, &buffer, response, w, NULL) == 0);
        }
    }
}

static void pending(struct bt_conn_s *conn, struct witness *w, uint8_t opcode)
{
  struct bt_att_hdr_s header = {.code = opcode};
  struct bt_buf_s buffer = {.data = &header};
  assert(bt_att_send(conn, &buffer, response, w, destroyed) == 0);
  assert(conn->att->req.op == opcode);
  /* A competing request cannot replace callback/context storage. */
  assert(bt_att_send(conn, &buffer, response, NULL, NULL) == -EBUSY);
  assert(conn->att->req.user_data == w);
}

int main(int argc, char **argv)
{
  (void)argv;
  assert(argc <= 2);
  struct bt_att_s att = {0};
  struct bt_conn_s conn = {.att = &att, .handle = 1};
  struct witness w = {.conn = &conn, .reenter = true};
  att.conn = &conn;
  if (argc == 1)
    {
      pending(&conn, &w, 0x0a);
      assert(att_handle_rsp(&conn, NULL, 0, 0) == 0);
      assert(w.calls == 1 && w.destroys == 1 && w.error == 0);
      assert(att_handle_rsp(&conn, NULL, 0, 0) == 0);
      bt_att_cancel(&conn);
      assert(w.calls == 1 && w.destroys == 1);

      /* Destructor reentry must see an empty slot, not destroy it again. */
      w = (struct witness){.conn = &conn, .reenter = true};
      pending(&conn, &w, 0x12);
      bt_att_cancel(&conn);
      assert(w.calls == 0 && w.destroys == 1);

      /* Normal callbacks can still chain discovery/read requests. */
      w = (struct witness){.conn = &conn, .chain = true};
      pending(&conn, &w, 0x08);
      assert(att_handle_rsp(&conn, NULL, 0, 0) == 0);
      assert(w.calls == 1 && w.destroys == 1 && att.req.func == response);
      assert(att.req.user_data == &w && att.req.destroy == NULL);
      bt_att_cancel(&conn);
    }

  /* MTU, discovery, read and acknowledged-write request opcodes. */
  const uint8_t opcodes[] = {0x02, 0x06, 0x08, 0x04, 0x0a, 0x12};
  for (size_t i = 0; i < sizeof(opcodes); i++)
    {
      att = (struct bt_att_s){.conn = &conn};
      conn.att = &att;
      w = (struct witness){.conn = &conn, .disconnect = true, .reenter = true};
      g_notifications = 0;
      pending(&conn, &w, opcodes[i]);
      bt_att_disconnected(&conn, NULL, BT_L2CAP_CID_ATT);
      assert(conn.att == NULL && att.conn == NULL);
      assert(w.calls == 1 && w.destroys == 1 && w.error == BT_ATT_ERR_UNLIKELY);
      assert(g_notifications == 1);
      bt_att_disconnected(&conn, NULL, BT_L2CAP_CID_ATT);
      bt_att_cancel(&conn);
      assert(w.calls == 1 && w.destroys == 1 && g_notifications == 1);
    }

  /* Reusing the channel after disconnect must not resurrect old callbacks. */
  att = (struct bt_att_s){.conn = &conn};
  conn.att = &att;
  w = (struct witness){.conn = &conn};
  pending(&conn, &w, 0x0a);
  assert(att_handle_rsp(&conn, NULL, 0, 0) == 0);
  assert(w.calls == 1 && w.destroys == 1);
  bt_att_disconnected(&conn, NULL, BT_L2CAP_CID_ATT);
  assert(w.calls == 1 && w.destroys == 1);
  assert(g_sends > 0);
  return 0;
}

/* SPDX-License-Identifier: MIT */
/* Real patched discovery/MTU functions, with only transport side effects stubbed. */
#include <assert.h>
#include <errno.h>
#include <stdint.h>
#include <stddef.h>

#define FAR
#define CODE
#define wlinfo(...) ((void)0)
#define BT_LE162HOST(x) (x)
#define BT_GATT_ITER_STOP 0
#define BT_GATT_ITER_CONTINUE 1
#define BT_ATT_ERR_INVALID_PDU 0x04
#define BT_ATT_ERR_UNLIKELY 0x0e
#define BT_GATT_PRIMARY_SERVICE(h, u) { .handle = h, .user_data = u }
struct bt_uuid_s { int unused; };
struct bt_gatt_attr_s { uint16_t handle; const void *user_data; };
typedef uint8_t (*bt_gatt_attr_func_t)(const struct bt_gatt_attr_s *, void *);
struct bt_att_s { uint16_t mtu; };
struct bt_conn_s { struct bt_att_s *att; };
struct group { uint16_t start_handle, end_handle; };
struct bt_att_find_type_rsp_s { struct group list[2]; };
/* PARAMETERS */

static unsigned int found, destroyed, continued;
static uint16_t observed_start, observed_end, next_start;
static int bt_gatt_discover(struct bt_conn_s *conn, struct bt_gatt_discover_params_s *params)
{ assert(conn != NULL); continued++; next_start = params->start_handle; return -ECANCELED; }
static uint8_t discovered(const struct bt_gatt_attr_s *attr, void *arg)
{
  struct bt_gatt_discover_params_s *params = arg;
  found++; observed_start = attr->handle; observed_end = params->group_end_handle;
  return BT_GATT_ITER_CONTINUE;
}
static void done(void *arg) { assert(arg != NULL); destroyed++; }
/* FUNCTIONS */

int main(void)
{
  struct bt_att_s att = {.mtu = 23};
  struct bt_conn_s conn = {.att = &att};
  assert(bt_gatt_get_mtu(&conn) == 23);
  att.mtu = 185; assert(bt_gatt_get_mtu(&conn) == 185);
  conn.att = NULL; assert(bt_gatt_get_mtu(&conn) == 0);
  assert(bt_gatt_get_mtu(NULL) == 0);
  struct bt_uuid_s uuid;
  struct bt_gatt_discover_params_s params = {
    .uuid = &uuid, .func = discovered, .destroy = done,
    .start_handle = 1, .end_handle = 20,
  };
  struct bt_att_find_type_rsp_s rsp = {.list = {{5, 12}}};
  att_find_type_rsp(&conn, 0, &rsp, sizeof(struct group), &params);
  assert(found == 1 && destroyed == 1 && continued == 1);
  assert(observed_start == 5 && observed_end == 12 && next_start == 13);

  const struct group invalid[] = {{0, 12}, {5, 4}, {5, 21}};
  for (size_t i = 0; i < sizeof(invalid) / sizeof(invalid[0]); i++)
    {
      params.start_handle = 1; rsp.list[0] = invalid[i];
      unsigned int before = destroyed;
      att_find_type_rsp(&conn, 0, &rsp, sizeof(struct group), &params);
      assert(found == 1 && continued == 1 && destroyed == before + 1);
    }
  params.start_handle = 1;
  att_find_type_rsp(&conn, 0, &rsp, 0, &params);
  assert(found == 1 && continued == 1); /* No repeat-query loop for empty response. */

  params.start_handle = 1; params.end_handle = UINT16_MAX;
  rsp.list[0] = (struct group){5, UINT16_MAX};
  att_find_type_rsp(&conn, 0, &rsp, sizeof(struct group), &params);
  assert(found == 2 && observed_end == UINT16_MAX && continued == 1);
  return 0;
}

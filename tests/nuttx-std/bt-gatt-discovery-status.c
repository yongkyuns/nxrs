/* SPDX-License-Identifier: MIT */
/* Real patched GATT discovery parsers; only transport effects are stubbed. */
#include <assert.h>
#include <errno.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>

#define FAR
#define CODE
#define wlinfo(...) ((void)0)
#define wlerr(...) ((void)0)
#define BT_LE162HOST(v) (v)
#define BT_UUID_16 0
#define BT_UUID_128 1
#define BT_ATT_ERR_INVALID_PDU 0x04
#define BT_ATT_ERR_ATTRIBUTE_NOT_FOUND 0x0a
#define BT_ATT_ERR_UNLIKELY 0x0e
#define BT_ATT_INFO_16 1
#define BT_ATT_INFO_128 2
#define BT_GATT_ITER_STOP 0
#define BT_GATT_ITER_CONTINUE 1

struct bt_uuid_s { uint8_t type; union { uint16_t u16; uint8_t u128[16]; } u; };
struct bt_gatt_chrc_s { uint8_t properties; uint16_t value_handle; struct bt_uuid_s *uuid; };
struct bt_gatt_attr_s { uint16_t handle; const void *user_data; };
typedef uint8_t (*bt_gatt_attr_func_t)(const struct bt_gatt_attr_s *, void *);
struct bt_conn_s { int unused; };
/* PARAMETERS */
/* WIRE TYPES */
/* PRIVATE TYPES */

#define BT_GATT_PRIMARY_SERVICE(h, u) { .handle = (h), .user_data = (u) }
#define BT_GATT_CHARACTERISTIC(h, v) { .handle = (h), .user_data = (v) }
#define BT_GATT_DESCRIPTOR(h, u, p, r, w, v) { .handle = (h), .user_data = (v) }

static unsigned int callbacks, destroys, continues;
static uint8_t callback_result = BT_GATT_ITER_CONTINUE;
static int continuation_result;
static int bt_uuid_cmp(const struct bt_uuid_s *a, const struct bt_uuid_s *b)
{ return a->type != b->type || memcmp(&a->u, &b->u, sizeof(a->u)); }
static int bt_gatt_discover(struct bt_conn_s *c, struct bt_gatt_discover_params_s *p)
{ (void)c; (void)p; continues++; return continuation_result; }
static int bt_gatt_discover_characteristic(struct bt_conn_s *c,
                                           struct bt_gatt_discover_params_s *p)
{ return bt_gatt_discover(c, p); }
static int bt_gatt_discover_descriptor(struct bt_conn_s *c,
                                       struct bt_gatt_discover_params_s *p)
{ return bt_gatt_discover(c, p); }
static uint8_t found(const struct bt_gatt_attr_s *attr, void *arg)
{ (void)attr; (void)arg; callbacks++; return callback_result; }
static void destroyed(void *arg)
{ struct bt_gatt_discover_params_s *p = arg; assert(p != NULL); destroys++; }
/* FUNCTIONS */

static struct bt_gatt_discover_params_s params(void)
{
  struct bt_gatt_discover_params_s p = {
    .func = found, .destroy = destroyed, .start_handle = 1, .end_handle = 20,
  };
  return p;
}

int main(void)
{
  struct bt_conn_s conn = {0};
  struct bt_gatt_discover_params_s p = params();
  struct { uint16_t start, end; } groups[2] = {{3, 5}, {0, 0}};
  att_find_type_rsp(&conn, 0, groups, sizeof(groups[0]) + 1, &p);
  assert(callbacks == 1 && destroys == 1 && p.result == BT_ATT_ERR_INVALID_PDU);

  callbacks = destroys = continues = 0; p = params();
  att_read_type_rsp(&conn, BT_ATT_ERR_ATTRIBUTE_NOT_FOUND, NULL, 0, &p);
  assert(callbacks == 0 && destroys == 1 && p.result == BT_ATT_ERR_ATTRIBUTE_NOT_FOUND);

  callbacks = destroys = continues = 0; p = params(); continuation_result = -ENOMEM;
  struct { uint16_t start, end; } one_group = {3, 5};
  att_find_type_rsp(&conn, 0, &one_group, sizeof(one_group), &p);
  assert(callbacks == 1 && continues == 1 && destroys == 1 &&
         p.result == BT_ATT_ERR_UNLIKELY);

  callbacks = destroys = continues = 0; p = params();
  att_find_info_rsp(&conn, BT_ATT_ERR_ATTRIBUTE_NOT_FOUND, NULL, 0, &p);
  assert(callbacks == 0 && destroys == 1 &&
         p.result == BT_ATT_ERR_ATTRIBUTE_NOT_FOUND);

  callbacks = destroys = continues = 0; p = params();
  uint8_t empty[1] = {BT_ATT_INFO_16};
  att_find_info_rsp(&conn, 0, empty, sizeof(empty), &p);
  assert(callbacks == 0 && destroys == 1 && p.result == BT_ATT_ERR_INVALID_PDU);

  /* A complete characteristic record can continue, but bad lengths/ranges
   * cannot be reported as a complete search even after a valid prefix.
   */
  uint8_t characteristic[] = {7, 3, 0, 2, 4, 0, 0x19, 0x2a};
  callbacks = destroys = continues = 0; p = params(); continuation_result = 0;
  att_read_type_rsp(&conn, 0, characteristic, sizeof(characteristic), &p);
  assert(callbacks == 1 && continues == 1 && destroys == 0 && p.result == 0);
  att_read_type_rsp(&conn, BT_ATT_ERR_ATTRIBUTE_NOT_FOUND, NULL, 0, &p);
  assert(destroys == 1 && p.result == BT_ATT_ERR_ATTRIBUTE_NOT_FOUND);

  callbacks = destroys = continues = 0; p = params();
  att_read_type_rsp(&conn, 0, characteristic, sizeof(characteristic) - 1, &p);
  assert(callbacks == 0 && continues == 0 && destroys == 1 &&
         p.result == BT_ATT_ERR_INVALID_PDU);
  callbacks = destroys = continues = 0; p = params();
  characteristic[4] = 21; /* Value handle exceeds the requested range. */
  att_read_type_rsp(&conn, 0, characteristic, sizeof(characteristic), &p);
  assert(callbacks == 0 && continues == 0 && destroys == 1 &&
         p.result == BT_ATT_ERR_INVALID_PDU);

  uint8_t descriptors[] = {BT_ATT_INFO_16, 3, 0, 0x02, 0x29, 2, 0, 0x02, 0x29};
  callbacks = destroys = continues = 0; p = params();
  att_find_info_rsp(&conn, 0, descriptors, sizeof(descriptors), &p);
  assert(callbacks == 1 && continues == 0 && destroys == 1 &&
         p.result == BT_ATT_ERR_INVALID_PDU); /* Decreasing handle order. */
  return 0;
}

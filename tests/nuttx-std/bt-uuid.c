/* SPDX-License-Identifier: MIT */
/* Execute the real conversion/comparator for all 16-bit UUID values. */
#include <assert.h>
#include <errno.h>
#include <stdint.h>
#include <string.h>

#define FAR
/* TYPES */
/* BASE */
/* FUNCTIONS */

int main(void)
{
  for (uint32_t value = 0; value <= UINT16_MAX; value++)
    {
      struct bt_uuid_s short_uuid = {.type = BT_UUID_16, .u.u16 = value};
      struct bt_uuid_s full_uuid = {
        .type = BT_UUID_128,
        .u.u128 = {0xfb, 0x34, 0x9b, 0x5f, 0x80, 0, 0, 0x80,
                   0, 0x10, 0, 0, value & 0xff, value >> 8, 0, 0},
      };
      struct bt_uuid_s expanded;
      uuid_to_uuid128(&short_uuid, &expanded);
      assert(memcmp(expanded.u.u128, full_uuid.u.u128, 16) == 0);
      assert(bt_uuid_cmp(&short_uuid, &full_uuid) == 0);
      assert(bt_uuid_cmp(&full_uuid, &short_uuid) == 0);
      assert(bt_uuid_cmp(&short_uuid, &short_uuid) == 0);
      full_uuid.u.u128[0] ^= 1;
      assert(bt_uuid_cmp(&short_uuid, &full_uuid) != 0);
      assert(bt_uuid_cmp(&full_uuid, &short_uuid) != 0);
    }
  return 0;
}

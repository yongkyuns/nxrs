/* SPDX-License-Identifier: MIT */
/* Exercise the actual extracted parser and public dhcpc_state declaration. */
#include <arpa/inet.h>
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "netutils/dhcpc.h"
#define nerr(...) ((void)0)

/* DEFINITIONS */
/* PARSER */

static const uint8_t options[] = {
  53, 1, 2,
  1, 4, 255, 255, 255, 0,
  3, 4, 192, 0, 2, 1,
  51, 4, 0, 0, 14, 16,
  54, 4, 192, 0, 2, 2,
  58, 4, 0, 0, 7, 8,
  59, 4, 0, 0, 12, 78,
  6, 8, 192, 0, 2, 3, 192, 0, 2, 4,
  42, 8, 192, 0, 2, 5, 192, 0, 2, 6,
  254, 2, 0xaa, 0xbb, 255
};

static void address(struct in_addr actual, const uint8_t expected[4])
{
  assert(memcmp(&actual.s_addr, expected, 4) == 0);
}

static void verify(uint8_t *data, size_t length, uint8_t type)
{
  struct dhcpc_state state = {0};
  assert(dhcpc_parseoptions(&state, data, length) == type);
  address(state.netmask, (uint8_t[]){255, 255, 255, 0});
  address(state.default_router, (uint8_t[]){192, 0, 2, 1});
  address(state.serverid, (uint8_t[]){192, 0, 2, 2});
  assert(state.lease_time == 3600 && state.renewal_time == 1800 && state.rebinding_time == 3150);
  assert(state.num_dnsaddr == 2 && state.num_ntpaddr == 2);
  address(state.dnsaddr[0], (uint8_t[]){192, 0, 2, 3});
  address(state.dnsaddr[1], (uint8_t[]){192, 0, 2, 4});
  address(state.ntpaddr[0], (uint8_t[]){192, 0, 2, 5});
  address(state.ntpaddr[1], (uint8_t[]){192, 0, 2, 6});
}

static size_t padded(uint8_t *out, size_t boundary, size_t pads, int every)
{
  size_t input = 0, output = 0, index = 0;
  while (input < sizeof(options))
    {
      if (index == boundary || every)
        for (size_t n = 0; n < pads; n++) out[output++] = 0;
      size_t length = options[input] == 255 ? 1 : options[input + 1] + 2;
      memcpy(out + output, options + input, length);
      output += length;
      input += length;
      index++;
    }
  return output;
}

int main(int argc, char **argv)
{
  (void)argv;
  uint8_t buffer[512];
  unsigned int checks = 0;
  /* The unpatched parser must pass non-PAD controls before being rejected. */
  for (uint8_t type = 2; type <= 5; type += 3)
    {
      memcpy(buffer, options, sizeof(options));
      buffer[2] = type;
      verify(buffer, sizeof(options), type);
      checks++;
    }
  if (argc > 1) return 0;
  for (uint8_t type = 2; type <= 5; type += 3)
    for (size_t pads = 1; pads <= 4; pads++)
      {
        for (size_t boundary = 0; boundary < 11; boundary++)
          {
            memset(buffer, 0, sizeof(buffer));
            size_t length = padded(buffer, boundary, pads, 0);
            buffer[pads * (boundary == 0) + 2] = type;
            verify(buffer, length, type);
            checks++;
          }
        size_t length = padded(buffer, 0, pads, 1);
        buffer[pads + 2] = type;
        verify(buffer, length, type);
        checks++;
      }
  /* PAD and END are one byte, including at the end of captured storage. */
  struct dhcpc_state state = {0};
  uint8_t pad = 0, end = 255;
  assert(dhcpc_parseoptions(&state, &pad, 1) == 0);
  assert(dhcpc_parseoptions(&state, &end, 1) == 0);
  assert(dhcpc_parseoptions(&state, &pad, 0) == 0);
  printf("DHCP_PAD_WITNESS checks=%u single_byte_boundaries=3\n", checks);
  return 0;
}

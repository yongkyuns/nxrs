/* SPDX-License-Identifier: MIT */
#include "payload_processing.h"

#if defined(NXRS_PAYLOAD_C_SPEED) && defined(__GNUC__) && !defined(__clang__)
/* Match the speed-oriented Rust probe and make the identical documented
 * disjoint-buffer contract visible to GCC. The kernel remains size-built. */
#define NXRS_NOINLINE __attribute__((noinline, optimize("O2")))
#define NXRS_RESTRICT restrict
#elif defined(__GNUC__) || defined(__clang__)
#define NXRS_NOINLINE __attribute__((noinline))
#define NXRS_RESTRICT
#else
#define NXRS_NOINLINE
#define NXRS_RESTRICT
#endif

NXRS_NOINLINE int nxrs_c_encode(uint8_t *NXRS_RESTRICT packet, size_t length,
                                uint32_t sequence, const int16_t *NXRS_RESTRICT samples)
{
  size_t i;

  if (length != NXRS_PAYLOAD_PACKET_BYTES || packet == NULL || samples == NULL)
    return -1;

  packet[0] = (uint8_t)(sequence & UINT32_C(0xff));
  packet[1] = (uint8_t)((sequence >> 8) & UINT32_C(0xff));
  packet[2] = (uint8_t)((sequence >> 16) & UINT32_C(0xff));
  packet[3] = (uint8_t)((sequence >> 24) & UINT32_C(0xff));
  packet[4] = 32u;
  packet[5] = 0u;
  packet[6] = 1u;
  packet[7] = 0u;

  for (i = 0; i < NXRS_PAYLOAD_SAMPLES; ++i)
    {
      uint16_t value = (uint16_t)samples[i];
      packet[8u + 2u * i] = (uint8_t)(value & UINT16_C(0xff));
      packet[9u + 2u * i] = (uint8_t)(value >> 8);
    }

  return 0;
}

NXRS_NOINLINE int nxrs_c_parse(const uint8_t *NXRS_RESTRICT packet, size_t length,
                               int16_t *NXRS_RESTRICT samples, uint32_t *NXRS_RESTRICT sequence)
{
  size_t i;

  if (length != NXRS_PAYLOAD_PACKET_BYTES || packet == NULL ||
      samples == NULL || sequence == NULL)
    return -1;

  if (packet[4] != 32u || packet[5] != 0u || packet[6] != 1u ||
      packet[7] != 0u)
    return -1;

  for (i = 0; i < NXRS_PAYLOAD_SAMPLES; ++i)
    {
      uint16_t bits = (uint16_t)packet[8u + 2u * i] |
                      (uint16_t)((uint16_t)packet[9u + 2u * i] << 8);
      int32_t value = bits >= UINT16_C(32768)
                          ? (int32_t)bits - INT32_C(65536)
                          : (int32_t)bits;
      samples[i] = (int16_t)value;
    }

  *sequence = (uint32_t)packet[0] |
              ((uint32_t)packet[1] << 8) |
              ((uint32_t)packet[2] << 16) |
              ((uint32_t)packet[3] << 24);
  return 0;
}

NXRS_NOINLINE void nxrs_c_filter(const int16_t *NXRS_RESTRICT samples, int32_t *NXRS_RESTRICT state,
                                int32_t *NXRS_RESTRICT output)
{
  size_t i;

  for (i = 0; i < NXRS_PAYLOAD_SAMPLES; ++i)
    {
      size_t axis = i % 3u;
      state[axis] += ((int32_t)samples[i] - state[axis]) / 8;
      output[i] = state[axis];
    }
}

NXRS_NOINLINE void nxrs_c_filter_xyz(const int16_t *NXRS_RESTRICT samples, int32_t *NXRS_RESTRICT state,
                                    int32_t *NXRS_RESTRICT output)
{
  for (size_t frame = 0; frame < NXRS_PAYLOAD_SAMPLES; frame += 3)
    for (size_t axis = 0; axis < 3; ++axis)
      {
        state[axis] += ((int32_t)samples[frame + axis] - state[axis]) / 8;
        output[frame + axis] = state[axis];
      }
}

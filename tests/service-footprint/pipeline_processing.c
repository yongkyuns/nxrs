/* SPDX-License-Identifier: MIT */
#include "pipeline_processing.h"

#include "payload_processing.h"

#include <stddef.h>
#include <string.h>

#if defined(NXRS_CQ_SPEED) && defined(__GNUC__) && !defined(__clang__)
#pragma GCC optimize ("O2")
#endif

int nxrs_c_pipeline_build(uint8_t payload[236], unsigned lane,
                          unsigned producer, unsigned sequence)
{
  int16_t samples[NXRS_PAYLOAD_SAMPLES];
  uint32_t token = ((uint32_t)lane << 24) |
                   ((uint32_t)producer << 16) | (uint32_t)sequence;

  if (payload == NULL || lane > UINT8_MAX || producer > UINT8_MAX ||
      sequence > UINT16_MAX)
    {
      return -1;
    }

  for (size_t i = 0; i < NXRS_PAYLOAD_SAMPLES; ++i)
    {
      uint32_t bits = (token + (uint32_t)i * UINT32_C(7919)) & UINT32_C(0xffff);
      samples[i] = (int16_t)((int32_t)bits - INT32_C(32768));
    }

  if (nxrs_c_encode(payload, NXRS_PAYLOAD_PACKET_BYTES, token, samples) != 0)
    {
      return -1;
    }
  memset(payload + NXRS_PAYLOAD_PACKET_BYTES, 0,
         236u - NXRS_PAYLOAD_PACKET_BYTES);
  return 0;
}

int nxrs_c_pipeline_process(const uint8_t payload[236], uint32_t token,
                            int32_t state[3], int32_t result[3])
{
  int16_t samples[NXRS_PAYLOAD_SAMPLES] = {0};
  int32_t filtered[NXRS_PAYLOAD_SAMPLES] = {0};
  uint32_t packet_token = 0;

  if (payload == NULL || state == NULL || result == NULL ||
      nxrs_c_parse(payload, NXRS_PAYLOAD_PACKET_BYTES, samples,
                   &packet_token) != 0 || packet_token != token)
    {
      return -1;
    }

  /* Commit state and result only after every packet check has succeeded. */
  nxrs_c_filter_xyz(samples, state, filtered);
  memcpy(result, state, 3u * sizeof(int32_t));
  return 0;
}

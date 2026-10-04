/* SPDX-License-Identifier: MIT */
#ifndef NXRS_PAYLOAD_PROCESSING_H
#define NXRS_PAYLOAD_PROCESSING_H

#include <stddef.h>
#include <stdint.h>

#define NXRS_PAYLOAD_SAMPLES 96u
#define NXRS_PAYLOAD_PACKET_BYTES 200u

/* Encode a version-1 payload packet. Returns -1 without writing on bad input. */
int nxrs_c_encode(uint8_t *packet, size_t length, uint32_t sequence,
                  const int16_t *samples);

/* Validate and decode a version-1 payload packet. Returns -1 without writing
 * outputs if any argument, length, or packet header field is invalid.
 */
int nxrs_c_parse(const uint8_t *packet, size_t length, int16_t *samples,
                 uint32_t *sequence);

/* Filter 96 samples in place-order using three caller-owned axis states.
 * Caller provides aligned, valid storage for all arrays; state and output must
 * be disjoint from the input and from each other. Each state must remain in
 * the signed 16-bit range. This function does not validate pointers.
 */
void nxrs_c_filter(const int16_t *samples, int32_t *state, int32_t *output);

/* Same arithmetic, grouped XYZ frames to avoid computing an axis modulo. */
void nxrs_c_filter_xyz(const int16_t *samples, int32_t *state, int32_t *output);

#endif /* NXRS_PAYLOAD_PROCESSING_H */

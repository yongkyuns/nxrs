/* SPDX-License-Identifier: MIT */
#ifndef NXRS_PIPELINE_PROCESSING_H
#define NXRS_PIPELINE_PROCESSING_H

#include <stdint.h>

/* Build the 200-byte encoded packet and clear the remaining 36 payload bytes. */
int nxrs_c_pipeline_build(uint8_t payload[236], unsigned lane,
                          unsigned producer, unsigned sequence);

/* Validate token and packet, then update axis state and return its final XYZ. */
int nxrs_c_pipeline_process(const uint8_t payload[236], uint32_t token,
                            int32_t state[3], int32_t result[3]);

#endif /* NXRS_PIPELINE_PROCESSING_H */

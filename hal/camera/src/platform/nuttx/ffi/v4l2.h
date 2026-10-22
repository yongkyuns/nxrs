/* SPDX-License-Identifier: MIT */
#ifndef NXRS_CAMERA_V4L2_H
#define NXRS_CAMERA_V4L2_H
#include <stddef.h>
#include <stdint.h>

int camera_open_flags(void);
int camera_format(int, uint16_t, uint16_t, uint8_t,
                       uint32_t *, uint32_t *, uint8_t *);
int camera_quality(int, uint8_t);
int camera_buffers(int, uint32_t);
int camera_queue(int, uint32_t, uint8_t *, size_t);
int camera_dequeue(int, uint32_t *, uintptr_t *, uint32_t *, uint32_t *, uint32_t *);
int camera_stream(int, int);
#endif

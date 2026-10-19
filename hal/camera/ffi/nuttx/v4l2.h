/* SPDX-License-Identifier: MIT */
#ifndef NXRS_CAMERA_V4L2_H
#define NXRS_CAMERA_V4L2_H
#include <stddef.h>
#include <stdint.h>

int nxrs_camera_open_flags(void);
int nxrs_camera_format(int, uint16_t, uint16_t, uint8_t,
                       uint32_t *, uint32_t *, uint8_t *);
int nxrs_camera_buffers(int, uint32_t);
int nxrs_camera_queue(int, uint8_t *, size_t);
int nxrs_camera_dequeue(int, uintptr_t *, uint32_t *, uint32_t *, uint32_t *);
int nxrs_camera_stream(int, int);
#endif

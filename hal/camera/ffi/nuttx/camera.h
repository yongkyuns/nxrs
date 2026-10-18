/* SPDX-License-Identifier: MIT */
#ifndef NXRS_NUTTX_CAMERA_H
#define NXRS_NUTTX_CAMERA_H
#include <stddef.h>
#include <stdint.h>

/* Experimental per-device read contract, not a V4L2 ioctl. Read returns one
 * complete packed frame, EAGAIN when not ready, zero at end of a replay.
 * This C-only layout is shared by driver/bridge; Rust receives scalar fields.
 */
#define RCIOC_FORMAT 0x524301
struct rc_device_format { uint16_t width; uint16_t height; uint8_t pixels; };

/* Errors are -1 unsupported, -2 busy, -3 timeout, -4 full, -5 invalid,
 * -6 I/O, -7 poisoned rollback. These are NOT negated errno values.
 */
int rc_nx_camera_open(const char *, uint16_t *, uint16_t *, uint8_t *);
int rc_nx_read(int, uint8_t *, size_t);
#endif

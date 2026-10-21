/* SPDX-License-Identifier: MIT */
#ifndef NXRS_NUTTX_STORAGE_H
#define NXRS_NUTTX_STORAGE_H
#include <stddef.h>
#include <stdint.h>
/* Stable bridge statuses, not native errno. -7 means append rollback failed. */
int rc_nx_file_create(const char *);
int rc_nx_append(int, const uint8_t *, size_t, const uint8_t *, size_t);
int rc_nx_flush(int);
#endif

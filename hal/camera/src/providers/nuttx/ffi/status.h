/* SPDX-License-Identifier: MIT */
#ifndef NXRS_CAMERA_STATUS_H
#define NXRS_CAMERA_STATUS_H
#include <errno.h>
/* Stable bridge status codes, not negated native errno values. */
static inline int rc_errno(int value)
{
  switch (value)
    {
      case ENOTSUP: case ENOTTY: return -1;
      case EAGAIN: case EBUSY: case EEXIST: return -2;
      case ETIMEDOUT: return -3;
      case ENOSPC: case ENOMEM: return -4;
      case EINVAL: return -5;
      default: return -6;
    }
}

/* Consumes the descriptor even on a driver close error. Never retry its number. */
int rc_nx_close(int);
#endif

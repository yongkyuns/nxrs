/* SPDX-License-Identifier: MIT
 * Compile with NuttX headers inside the target image, never as HOSTSRC.
 */
#include <nuttx/config.h>
#include <unistd.h>
#include "status.h"

int rc_nx_close(int fd)
{
  return close(fd) == 0 ? 0 : rc_errno(errno);
}

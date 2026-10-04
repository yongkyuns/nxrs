/* Target-compiled layout ledger only; not linked into measured firmware.
 * Kernel internal headers belong to this pinned NuttX qualification build.
 * The assembly's constant words reveal sizes without executing new code.
 */
#include <nuttx/config.h>
#include <nuttx/fs/fs.h>
#include <nuttx/mqueue.h>
#include <poll.h>
#include <stdint.h>
#include "mqueue/mqueue.h"
#include "mqueue/msg.h"
#include "mm_heap/mm.h"

_Static_assert(sizeof(void *) == 4, "layout ledger requires a 32-bit target");

const uint32_t nxrs_transport_resource_layout[] = {
  sizeof(struct mqueue_msg_s), MQ_MSG_SIZE(248), MQ_MSG_SIZE(16), MQ_MSG_SIZE(28),
  MM_SIZEOF_ALLOCNODE, MM_ALLOCNODE_OVERHEAD, MM_ALIGN, MM_MIN_CHUNK,
  sizeof(struct mqueue_inode_s), sizeof(struct inode), sizeof(struct file),
  sizeof(struct pollfd), CONFIG_PREALLOC_MQ_MSGS, CONFIG_PREALLOC_MQ_IRQ_MSGS,
#ifndef CONFIG_DISABLE_MQUEUE_SYSV
  sizeof(struct msgbuf_s), 1
#else
  0, 0
#endif
};

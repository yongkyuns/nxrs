/* Test-fixture translation of the public NuttX POSIX message-queue API.
 * Rust owns workload scheduling and joins every user before destruction.
 * Internal scheduler/file interfaces are used only for the blocked-wait witness.
 */
#ifndef MPSC_NUTTX_QUEUE_H
#define MPSC_NUTTX_QUEUE_H

#include <nuttx/config.h>
#include <nuttx/fs/fs.h>
#include <nuttx/irq.h>
#include <nuttx/sched.h>
#include <errno.h>
#include <fcntl.h>
#include <mqueue.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#if defined(CONFIG_DISABLE_MQUEUE) || CONFIG_MQ_MAXMSGSIZE < 64
#error "The common NuttX MPSC profile must support 64-byte POSIX messages"
#endif

static const char mp_queue_name[] = "/mpsc_perf";
static mqd_t sender = (mqd_t)-1;
static mqd_t receiver = (mqd_t)-1;
static mqd_t nonblocking = (mqd_t)-1;
static size_t message_bytes;
static bool queue_name_owned;

int mp_queue_destroy(void)
{
  mqd_t *descriptors[] = {&nonblocking, &receiver, &sender};
  int result = 0;

  /* Continue closing independent descriptors after an error. Retain any
   * failed handle so neither a retry nor the test can claim it was released.
   */
  for (size_t i = 0; i < sizeof(descriptors) / sizeof(descriptors[0]); ++i)
    {
      if (*descriptors[i] != (mqd_t)-1)
        {
          if (mq_close(*descriptors[i]) < 0)
            {
              int error = -errno;
              if (result == 0) result = error;
            }
          else
            {
              *descriptors[i] = (mqd_t)-1;
            }
        }
    }

  /* Only unlink a name created by this fixture's successful O_EXCL open. */
  if (queue_name_owned)
    {
      if (mq_unlink(mp_queue_name) < 0)
        {
          int error = -errno;
          if (result == 0) result = error;
        }
      else
        {
          queue_name_owned = false;
        }
    }

  if (sender == (mqd_t)-1 && receiver == (mqd_t)-1 &&
      nonblocking == (mqd_t)-1)
    {
      message_bytes = 0;
    }

  return result;
}

int mp_queue_create(size_t capacity, size_t bytes)
{
  if (queue_name_owned || sender != (mqd_t)-1 || receiver != (mqd_t)-1 ||
      nonblocking != (mqd_t)-1)
    {
      return -EBUSY;
    }
  if ((capacity != 8 && capacity != 64) || (bytes != 16 && bytes != 64))
    {
      return -EINVAL;
    }

  struct mq_attr attr = {.mq_maxmsg = capacity, .mq_msgsize = bytes};
  sender = mq_open(mp_queue_name, O_CREAT | O_EXCL | O_WRONLY, 0600, &attr);
  if (sender == (mqd_t)-1) return -errno;
  queue_name_owned = true;
  receiver = mq_open(mp_queue_name, O_RDONLY);
  if (receiver != (mqd_t)-1)
    {
      nonblocking = mq_open(mp_queue_name, O_WRONLY | O_NONBLOCK);
    }
  if (receiver == (mqd_t)-1 || nonblocking == (mqd_t)-1)
    {
      int error = -errno;
      int cleanup = mp_queue_destroy();
      return cleanup != 0 ? cleanup : error;
    }

  message_bytes = bytes;
  return 0;
}

int mp_queue_send(const uint8_t *data, int blocking)
{
  if (message_bytes == 0 || (blocking != 0 && blocking != 1)) return -EINVAL;
  /* Immutable open descriptions avoid shared mq_setattr flag mutation. Equal
   * message priority preserves FIFO order within this fixture's contract.
   */
  if (mq_send(blocking ? sender : nonblocking, (const char *)data,
              message_bytes, 0) == 0) return 0;
  int error = errno;
  return !blocking && error == EAGAIN ? 1 : -error;
}

int mp_queue_receive(uint8_t *data)
{
  if (message_bytes == 0) return -EINVAL;
  ssize_t n = mq_receive(receiver, (char *)data, message_bytes, NULL);
  return n < 0 ? -errno : n == (ssize_t)message_bytes ? 0 : -EIO;
}

int mp_queue_waiting(uintptr_t tid, int sending)
{
  struct file *filep;
  if (message_bytes == 0 || (sending != 0 && sending != 1) ||
      file_get(sending ? sender : receiver, &filep) < 0) return 0;

  /* The joined-lifetime protocol keeps the descriptors and queue alive. Read
   * the TCB state/object atomically on this single-core qualification target.
   * Waiting on a gate, another queue or the wrong queue direction is not proof.
   */
  irqstate_t flags = enter_critical_section();
  struct tcb_s *tcb = nxsched_get_tcb(tid);
  int waiting = tcb != NULL && tcb->sched_priority == 180 &&
    tcb->task_state == (sending ? TSTATE_WAIT_MQNOTFULL : TSTATE_WAIT_MQNOTEMPTY) &&
    filep->f_inode != NULL && filep->f_inode->i_private != NULL &&
    tcb->waitobj == filep->f_inode->i_private;
  leave_critical_section(flags);
  return file_put(filep) < 0 ? 0 : waiting;
}

#endif

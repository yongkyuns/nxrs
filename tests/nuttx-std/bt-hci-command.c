/* SPDX-License-Identifier: MIT */
/* Host harness for the actual hci_cmd_done() and
 * bt_hci_cmd_send_sync() bodies extracted from bt_hcicore.c into
 * bt-hci-command-functions.h.
 */

#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <pthread.h>
#include <stdio.h>
#include <string.h>

#define FAR
#define MSEC2TICK(value) (value)
#define TIMEOUT_MSEC 2500
#define BT_NORMAL_PRIO 0
#define wlerr(...) ((void)0)
#define wlwarn(...) ((void)0)
#define wlinfo(...) ((void)0)
#define DEBUGASSERT(condition) assert(condition)
#define CONFIG_BLUETOOTH_BUFFER_PREALLOC 4
#define CONFIG_BLUETOOTH_BUFFER_IRQRESERVE 0
#define POOL_BUFFER_GENERAL 1
#define POOL_BUFFER_DYNAMIC 0

typedef unsigned irqstate_t;

typedef struct
{
  unsigned count;
  bool initialized;
} sem_t;

struct bt_buf_s
{
  struct bt_buf_s *flink;
  void *frame;
  unsigned pool;
  unsigned type;
  unsigned ref;
  union
  {
    struct
    {
      uint16_t opcode;
      void *sync; /* Semaphore while pending; response buffer on success. */
    } hci;
  } u;
  unsigned len;
};

struct bt_dev_s
{
  struct bt_buf_s *sent_cmd;
  int tx_queue;
};

static struct bt_dev_s g_btdev;
static struct bt_buf_s pool[4];
static struct bt_buf_s *queued_cmd;
static struct bt_buf_s *command_under_test;
static struct bt_buf_s *completion_event;
static struct bt_buf_s *published_response;
static bool fail_queue;
static bool timeout_wait;
static bool timeout_before_tx;
static bool complete_at_timeout;
static uint8_t completion_status;
static unsigned wait_calls;
static unsigned destroy_calls;
static unsigned critical_depth;
static unsigned critical_entries;
static pthread_mutex_t g_buf_lock = PTHREAD_MUTEX_INITIALIZER;
static struct bt_buf_s *g_buf_free;
static unsigned freed_frames;

static void hci_cmd_done(uint16_t opcode, uint8_t status,
                         struct bt_buf_s *buf);

irqstate_t enter_critical_section(void)
{
  irqstate_t previous = critical_depth;
  critical_depth++;
  critical_entries++;
  return previous;
}

void leave_critical_section(irqstate_t previous)
{
  assert(critical_depth == previous + 1);
  critical_depth = previous;
}

static irqstate_t spin_lock_irqsave(pthread_mutex_t *lock)
{
  assert(pthread_mutex_lock(lock) == 0);
  return 0;
}

static void spin_unlock_irqrestore(pthread_mutex_t *lock, irqstate_t flags)
{
  (void)flags;
  assert(pthread_mutex_unlock(lock) == 0);
}

static void iob_free(void *frame)
{
  assert(frame != NULL);
  freed_frames++;
}

static void kmm_free(void *buffer)
{
  (void)buffer;
  assert(!"Unexpected dynamic free in the fixed host pool");
}

/* Actual reference-count operations, with kernel spinlock/free boundaries
 * mocked above. Allocation below deliberately forces immediate pool reuse.
 */
#include "bt-buffer-functions.h"

static struct bt_buf_s *bt_buf_alloc(uint16_t opcode)
{
  for (size_t i = 0; i < sizeof(pool) / sizeof(pool[0]); i++)
    {
      if (pool[i].ref == 0)
        {
          memset(&pool[i], 0, sizeof(pool[i]));
          pool[i].ref = 1;
          pool[i].pool = POOL_BUFFER_GENERAL;
          pool[i].u.hci.opcode = opcode;
          return &pool[i];
        }
    }

  return NULL;
}

static struct bt_buf_s *bt_hci_cmd_create(uint16_t opcode, uint8_t param_len)
{
  (void)param_len;
  return bt_buf_alloc(opcode);
}

static int bt_queue_send(int *queue, struct bt_buf_s *buf, int priority)
{
  assert(queue == &g_btdev.tx_queue);
  assert(priority == BT_NORMAL_PRIO);
  if (fail_queue)
    {
      return -ENOSPC;
    }

  assert(queued_cmd == NULL);
  queued_cmd = buf; /* Queue takes the caller's existing reference. */
  return 0;
}

static int nxsem_init(sem_t *sem, int pshared, unsigned value)
{
  (void)pshared;
  sem->count = value;
  sem->initialized = true;
  return 0;
}

static int nxsem_post(sem_t *sem)
{
  assert(sem != NULL && sem->initialized);
  sem->count++;
  return 0;
}

static int nxsem_destroy(sem_t *sem)
{
  assert(sem != NULL && sem->initialized);
  sem->initialized = false;
  destroy_calls++;
  return 0;
}

static void run_tx_and_completion(void)
{
  struct bt_buf_s *transmitted = queued_cmd;
  assert(transmitted != NULL);
  command_under_test = transmitted;
  queued_cmd = NULL;

  /* Match hci_tx_kthread(): establish sent_cmd ownership, then drop the
   * queue's reference after handing the frame to the driver.
   */
  g_btdev.sent_cmd = bt_buf_addref(transmitted);
  bt_buf_release(transmitted);

  completion_event = bt_buf_alloc(0xffff);
  assert(completion_event != NULL);
  published_response = completion_event;

  /* hci_cmd_done() is the extracted production function. */
  hci_cmd_done(transmitted->u.hci.opcode, completion_status, completion_event);
  bt_buf_release(completion_event); /* RX releases the event after handling. */
  completion_event = NULL;
}

static void dequeue_for_tx(void)
{
  struct bt_buf_s *transmitted = queued_cmd;
  assert(transmitted != NULL);
  command_under_test = transmitted;
  queued_cmd = NULL;
  g_btdev.sent_cmd = bt_buf_addref(transmitted);
  bt_buf_release(transmitted);
}

static int nxsem_tickwait_uninterruptible(sem_t *sem, unsigned ticks)
{
  struct bt_buf_s *reused;

  assert(sem != NULL && sem->initialized);
  assert(ticks == TIMEOUT_MSEC);
  wait_calls++;
  if (timeout_wait)
    {
      if (complete_at_timeout)
        {
          run_tx_and_completion();
        }
      else if (!timeout_before_tx)
        {
          dequeue_for_tx();
        }
      return -ETIMEDOUT;
    }

  run_tx_and_completion();
  assert(sem->count == 1);

  /* Run after hci_cmd_done() has returned and before the waiting caller
   * resumes. A freed command slot is immediately reused and its sync field
   * overwritten, deterministically modeling allocator reuse at the race.
   */
  reused = bt_buf_alloc(0x7ffe);
  assert(reused != NULL);
#ifdef EXPECT_BASELINE_BUGS
  assert(reused == command_under_test);
#else
  assert(reused != command_under_test);
#endif
  bt_buf_release(reused);
  sem->count--;
  return 0;
}

/* Generated by extracting the complete definitions of hci_cmd_done() and
 * bt_hci_cmd_send_sync() from external/nuttx/wireless/bluetooth/bt_hcicore.c.
 */
#include "bt-hci-command-functions.h"

static void reset(void)
{
  memset(&g_btdev, 0, sizeof(g_btdev));
  memset(pool, 0, sizeof(pool));
  queued_cmd = NULL;
  command_under_test = NULL;
  completion_event = NULL;
  published_response = NULL;
  fail_queue = false;
  timeout_wait = false;
  timeout_before_tx = false;
  complete_at_timeout = false;
  completion_status = 0;
  wait_calls = 0;
  destroy_calls = 0;
  critical_depth = 0;
  critical_entries = 0;
  g_buf_free = NULL;
  freed_frames = 0;
}

static void completion_lifetime(void)
{
  struct bt_buf_s *cmd;
  struct bt_buf_s *response = NULL;
  int ret;

  reset();
  cmd = bt_buf_alloc(0x1234);
  assert(cmd != NULL);
  ret = bt_hci_cmd_send_sync(0x1234, cmd, &response);

#ifdef EXPECT_BASELINE_BUGS
  /* The command was reclaimed/reused before the caller read u.hci.sync. */
  assert(ret == -EIO);
  assert(response == NULL);
  assert(published_response != NULL && published_response->ref == 1);
  bt_buf_release(published_response); /* Reclaim the baseline's orphaned ref. */
#else
  assert(ret == 0);
  assert(response == published_response);
  assert(response->ref == 1);
  assert(critical_entries >= 2);
  bt_buf_release(response);
#endif

  assert(wait_calls == 1);
  assert(destroy_calls == 1);
  assert(queued_cmd == NULL && g_btdev.sent_cmd == NULL);
  assert(critical_depth == 0);
}

static void queue_failure_ownership(void)
{
  struct bt_buf_s *cmd;
  int ret;

  reset();
  cmd = bt_buf_alloc(0x2345);
  assert(cmd != NULL);
  command_under_test = cmd;
  fail_queue = true;
  ret = bt_hci_cmd_send_sync(0x2345, cmd, NULL);
  assert(ret < 0);
  assert(wait_calls == 0 && destroy_calls == 1);
  assert(queued_cmd == NULL && g_btdev.sent_cmd == NULL);

#ifdef EXPECT_BASELINE_BUGS
  /* The original function leaves the caller's command reference behind
   * after enqueue failure.
   */
  assert(cmd->ref == 1);
  bt_buf_release(cmd);
#else
  assert(cmd->ref == 0);
  assert(critical_entries >= 1);
#endif
  assert(critical_depth == 0);
}

static void timeout_late_completion(void)
{
  struct bt_buf_s *cmd;
#ifndef EXPECT_BASELINE_BUGS
  struct bt_buf_s *late_event;
#endif
  int ret;

  reset();
  cmd = bt_buf_alloc(0x3456);
  assert(cmd != NULL);
  timeout_wait = true;
  ret = bt_hci_cmd_send_sync(0x3456, cmd, NULL);
  assert(ret < 0);
  assert(wait_calls == 1 && destroy_calls == 1);
  assert(queued_cmd == NULL && g_btdev.sent_cmd == cmd);

#ifdef EXPECT_BASELINE_BUGS
  /* Observe but never dereference the stack semaphore pointer after return. */
  assert(cmd->u.hci.sync != NULL);
  cmd->u.hci.sync = NULL;
  bt_buf_release(cmd); /* Simulate expiry of the pending sent-command ref. */
#else
  assert(cmd->u.hci.sync == NULL);
  assert(cmd->ref == 1); /* Only the sent-command reference remains. */
  assert(critical_entries >= 1);
  late_event = bt_buf_alloc(0x7ffd);
  assert(late_event != NULL);
  hci_cmd_done(0x3456, 0, late_event);
  bt_buf_release(late_event);
  assert(cmd->ref == 0);
  assert(critical_depth == 0);
#endif
}

#ifndef EXPECT_BASELINE_BUGS
static void assert_no_references(void)
{
  for (unsigned i = 0; i < 4; i++)
    {
      assert(pool[i].ref == 0);
    }
}

static void response_boundaries(void)
{
  struct bt_buf_s *response;
  int ret;

  /* A successful caller that does not request a response must release it. */
  reset();
  assert(bt_hci_cmd_send_sync(0x4567, NULL, NULL) == 0);
  assert_no_references();

  /* A controller error posts the waiter without retaining the response. */
  reset();
  completion_status = 0x0c;
  response = &pool[3];
  assert(bt_hci_cmd_send_sync(0x4567, NULL, &response) == -EIO);
  assert(response == NULL);
  assert_no_references();

  /* Completion racing just after the timeout still owns an event reference. */
  reset();
  timeout_wait = true;
  complete_at_timeout = true;
  assert(bt_hci_cmd_send_sync(0x4567, NULL, NULL) == -EIO);
  assert_no_references();

  /* An unsent command can outlive the waiter without using its semaphore. */
  reset();
  timeout_wait = true;
  timeout_before_tx = true;
  ret = bt_hci_cmd_send_sync(0x4567, NULL, NULL);
  assert(ret == -EIO && queued_cmd != NULL && queued_cmd->ref == 1);
  assert(queued_cmd->u.hci.sync == NULL);
  run_tx_and_completion();
  assert_no_references();

  /* Allocation failure must not expose the caller's old response pointer. */
  reset();
  for (unsigned i = 0; i < 4; i++)
    {
      assert(bt_buf_alloc(0x4567) != NULL);
    }
  response = &pool[3];
  assert(bt_hci_cmd_send_sync(0x4567, NULL, &response) == -ENOBUFS);
  assert(response == NULL && wait_calls == 0 && destroy_calls == 0);
  for (unsigned i = 0; i < 4; i++)
    {
      bt_buf_release(&pool[i]);
    }
}

static void *reference_worker(void *arg)
{
  struct bt_buf_s *buf = arg;
  for (unsigned i = 0; i < 10000; i++)
    {
      bt_buf_addref(buf);
      bt_buf_release(buf);
    }
  return NULL;
}

static void concurrent_references(void)
{
  pthread_t workers[4];
  struct bt_buf_s *buf;
  reset();
  buf = bt_buf_alloc(0x4567);
  buf->frame = buf; /* Opaque host IOB token. */
  for (unsigned i = 0; i < 4; i++)
    {
      assert(pthread_create(&workers[i], NULL, reference_worker, buf) == 0);
    }
  for (unsigned i = 0; i < 4; i++)
    {
      assert(pthread_join(workers[i], NULL) == 0);
    }
  assert(buf->ref == 1 && freed_frames == 0);
  bt_buf_release(buf);
  assert(buf->ref == 0 && freed_frames == 1 && g_buf_free == buf);
}
#endif

int main(void)
{
  completion_lifetime();
  queue_failure_ownership();
  timeout_late_completion();
#ifndef EXPECT_BASELINE_BUGS
  response_boundaries();
  concurrent_references();
#endif
  puts("bt HCI command lifetime harness passed");
  return 0;
}

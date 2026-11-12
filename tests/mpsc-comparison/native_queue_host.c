/* Execute the real fixture adapter against controlled native API outcomes.
 * These are adapter/lifetime tests, not a host substitute for the NuttX queue.
 */
#define _POSIX_C_SOURCE 200809L
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <mqueue.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <nuttx/fs/fs.h>
#include <nuttx/irq.h>
#include <nuttx/sched.h>

/* Include real declarations before redirecting calls: glibc's fortified
 * mq_open aliases otherwise bypass a command-line macro rename.
 */
mqd_t test_mq_open(const char *, int, ...);
int test_mq_close(mqd_t);
int test_mq_unlink(const char *);
int test_mq_send(mqd_t, const char *, size_t, unsigned);
ssize_t test_mq_receive(mqd_t, char *, size_t, unsigned *);
#define mq_open test_mq_open
#define mq_close test_mq_close
#define mq_unlink test_mq_unlink
#define mq_send test_mq_send
#define mq_receive test_mq_receive
#include "sdk_nuttx_queue.h"

static unsigned opens, closes, unlinks, file_gets, file_puts, sends, receives;
static unsigned fail_open_at;
static int fail_close_fd = -1, fail_unlink, send_error, receive_error;
static int fail_get, fail_put, named, alive[3], open_flags[3], refs, critical;
static size_t expected_capacity = 8, expected_bytes = 16;
static ssize_t receive_length = 16;
static int queue_object, other_object;
static struct inode queue_inode = {&queue_object};
static struct file queue_file = {&queue_inode};
static struct tcb_s task = {180, TSTATE_WAIT_MQNOTFULL, &queue_object};

static unsigned slot(mqd_t fd)
{
  assert(fd >= 100 && fd < 103);
  return (unsigned)fd - 100;
}

mqd_t test_mq_open(const char *name, int flags, ...)
{
  assert(strcmp(name, "/mpsc_perf") == 0);
  ++opens;
  if (opens == fail_open_at)
    {
      errno = (flags & O_CREAT) ? EEXIST : EMFILE;
      return (mqd_t)-1;
    }
  if (flags & O_CREAT)
    {
      assert(flags == (O_CREAT | O_EXCL | O_WRONLY) && !named);
      va_list args;
      va_start(args, flags);
      int mode = va_arg(args, int);
      const struct mq_attr *attr = va_arg(args, const struct mq_attr *);
      va_end(args);
      assert(mode == 0600);
      assert(attr->mq_maxmsg == (long)expected_capacity);
      assert(attr->mq_msgsize == (long)expected_bytes);
      assert(attr->mq_flags == 0 && attr->mq_curmsgs == 0);
      named = 1;
    }
  else
    {
      assert(named);
      assert(flags == O_RDONLY || flags == (O_WRONLY | O_NONBLOCK));
    }
  for (unsigned i = 0; i < 3; ++i)
    if (!alive[i])
      {
        alive[i] = 1;
        open_flags[i] = flags;
        return (mqd_t)(100 + i);
      }
  assert(!"too many descriptors");
  return (mqd_t)-1;
}

int test_mq_close(mqd_t fd)
{
  unsigned i = slot(fd);
  assert(alive[i] && refs == 0);
  ++closes;
  if (fd == fail_close_fd) {errno = EIO; return -1;}
  alive[i] = 0;
  /* Deliberately alter errno on success to expose lost original errors. */
  errno = ENOTTY;
  return 0;
}

int test_mq_unlink(const char *name)
{
  assert(named && strcmp(name, "/mpsc_perf") == 0);
  ++unlinks;
  if (fail_unlink) {errno = EBUSY; return -1;}
  named = 0;
  errno = ENOTTY;
  return 0;
}

int test_mq_send(mqd_t fd, const char *data, size_t bytes, unsigned priority)
{
  unsigned i = slot(fd);
  assert(alive[i] && (open_flags[i] & O_ACCMODE) == O_WRONLY);
  assert(bytes == expected_bytes && priority == 0);
  for (size_t j = 0; j < bytes; ++j) assert((unsigned char)data[j] == j);
  ++sends;
  if (send_error) {errno = send_error; return -1;}
  return 0;
}

ssize_t test_mq_receive(mqd_t fd, char *data, size_t bytes, unsigned *priority)
{
  unsigned i = slot(fd);
  assert(alive[i] && (open_flags[i] & O_ACCMODE) == O_RDONLY);
  assert(bytes == expected_bytes && priority == NULL);
  ++receives;
  if (receive_error) {errno = receive_error; return -1;}
  memset(data, 0x5a, bytes);
  return receive_length;
}

int file_get(int fd, struct file **filep)
{
  assert(!critical);
  ++file_gets;
  if (fail_get) return -EBADF;
  assert(alive[slot(fd)]);
  ++refs;
  *filep = &queue_file;
  return 0;
}

int file_put(struct file *filep)
{
  assert(!critical && filep == &queue_file && refs == 1);
  --refs;
  ++file_puts;
  return fail_put ? -EIO : 0;
}

irqstate_t enter_critical_section(void)
{
  assert(!critical);
  critical = 1;
  return 7;
}

void leave_critical_section(irqstate_t flags)
{
  assert(critical && flags == 7);
  critical = 0;
}

struct tcb_s *nxsched_get_tcb(int tid)
{
  assert(critical);
  return tid == 42 ? &task : NULL;
}

static void clean(void)
{
  assert(!named && !alive[0] && !alive[1] && !alive[2] && !refs && !critical);
}

static void success(size_t capacity, size_t bytes)
{
  expected_capacity = capacity;
  expected_bytes = bytes;
  receive_length = bytes;
  assert(mp_queue_create(capacity, bytes) == 0);
  assert(opens == 3);
  assert(open_flags[0] == (O_CREAT | O_EXCL | O_WRONLY));
  assert(open_flags[1] == O_RDONLY);
  assert(open_flags[2] == (O_WRONLY | O_NONBLOCK));
  unsigned char payload[64];
  for (size_t j = 0; j < sizeof(payload); ++j) payload[j] = j;
  assert(mp_queue_send(payload, 0) == 0);
  assert(mp_queue_send(payload, 1) == 0);
  assert(mp_queue_receive(payload) == 0);
  assert(payload[0] == 0x5a && payload[bytes - 1] == 0x5a);
  assert(sends == 2 && receives == 1);
  assert(mp_queue_create(capacity, bytes) == -EBUSY && opens == 3);
  assert(mp_queue_destroy() == 0);
  assert(closes == 3 && unlinks == 1);
  assert(mp_queue_destroy() == 0 && closes == 3 && unlinks == 1);
  clean();
}

static void partial(unsigned fail_at)
{
  fail_open_at = fail_at;
  assert(mp_queue_create(8, 16) == -(fail_at == 1 ? EEXIST : EMFILE));
  assert(opens == fail_at && closes == fail_at - 1);
  assert(unlinks == (fail_at > 1));
  clean();
  fail_open_at = 0;
  assert(mp_queue_create(8, 16) == 0);
  assert(mp_queue_destroy() == 0);
  clean();
}

static void partial_cleanup_error(void)
{
  fail_open_at = 3;
  fail_close_fd = 100;
  assert(mp_queue_create(8, 16) == -EIO);
  assert(alive[0] && !alive[1] && !alive[2] && !named);
  assert(closes == 2 && unlinks == 1);
  assert(mp_queue_create(8, 16) == -EBUSY && opens == 3);
  fail_close_fd = -1;
  assert(mp_queue_destroy() == 0 && closes == 3 && unlinks == 1);
  clean();
}

static void destruction_error(void)
{
  assert(mp_queue_create(8, 16) == 0);
  fail_close_fd = 101;
  fail_unlink = 1;
  assert(mp_queue_destroy() == -EIO);
  assert(!alive[0] && alive[1] && !alive[2] && named);
  assert(closes == 3 && unlinks == 1);
  assert(mp_queue_create(8, 16) == -EBUSY && opens == 3);
  fail_close_fd = -1;
  fail_unlink = 0;
  assert(mp_queue_destroy() == 0 && closes == 4 && unlinks == 2);
  clean();
}

static void errors(void)
{
  unsigned char payload[64];
  for (size_t j = 0; j < sizeof(payload); ++j) payload[j] = j;
  assert(mp_queue_send(payload, 0) == -EINVAL);
  assert(mp_queue_receive(payload) == -EINVAL);
  assert(mp_queue_create(8, 16) == 0);
  assert(mp_queue_send(payload, 2) == -EINVAL && sends == 0);
  send_error = EAGAIN;
  assert(mp_queue_send(payload, 0) == 1);
  assert(mp_queue_send(payload, 1) == -EAGAIN);
  send_error = EINTR;
  assert(mp_queue_send(payload, 0) == -EINTR);
  assert(mp_queue_send(payload, 1) == -EINTR);
  assert(sends == 4);
  receive_error = EINTR;
  assert(mp_queue_receive(payload) == -EINTR && receives == 1);
  receive_error = 0;
  receive_length = 8;
  assert(mp_queue_receive(payload) == -EIO && receives == 2);
  assert(mp_queue_destroy() == 0);
  clean();
}

static void waiting(void)
{
  assert(mp_queue_waiting(42, 1) == 0 && file_gets == 0);
  assert(mp_queue_create(8, 16) == 0);
  assert(mp_queue_waiting(42, 1) == 1);
  assert(mp_queue_waiting(42, 0) == 0);
  task.task_state = TSTATE_WAIT_MQNOTEMPTY;
  assert(mp_queue_waiting(42, 0) == 1);
  assert(mp_queue_waiting(42, 1) == 0);
  task.waitobj = &other_object;
  assert(mp_queue_waiting(42, 0) == 0);
  task.waitobj = &queue_object;
  task.task_state = TSTATE_WAIT_SEM;
  assert(mp_queue_waiting(42, 0) == 0 && mp_queue_waiting(42, 1) == 0);
  task.task_state = TSTATE_WAIT_MQNOTEMPTY;
  task.sched_priority = 179;
  assert(mp_queue_waiting(42, 0) == 0);
  task.sched_priority = 180;
  assert(mp_queue_waiting(43, 0) == 0);
  assert(mp_queue_waiting(42, 2) == 0);
  queue_inode.i_private = NULL;
  task.waitobj = NULL;
  assert(mp_queue_waiting(42, 0) == 0);
  queue_inode.i_private = &queue_object;
  task.waitobj = &queue_object;
  fail_get = 1;
  unsigned previous_puts = file_puts;
  assert(mp_queue_waiting(42, 0) == 0 && file_puts == previous_puts);
  fail_get = 0;
  fail_put = 1;
  assert(mp_queue_waiting(42, 0) == 0);
  fail_put = 0;
  assert(mp_queue_waiting(42, 0) == 1);
  assert(file_gets == file_puts + 1 && !refs);
  assert(mp_queue_destroy() == 0);
  assert(mp_queue_waiting(42, 0) == 0);
  clean();
}

int main(int argc, char **argv)
{
  assert(argc == 2);
  if (!strcmp(argv[1], "small")) success(8, 16);
  else if (!strcmp(argv[1], "large")) success(64, 64);
  else if (!strcmp(argv[1], "first-open")) partial(1);
  else if (!strcmp(argv[1], "second-open")) partial(2);
  else if (!strcmp(argv[1], "third-open")) partial(3);
  else if (!strcmp(argv[1], "partial-cleanup")) partial_cleanup_error();
  else if (!strcmp(argv[1], "destruction")) destruction_error();
  else if (!strcmp(argv[1], "errors")) errors();
  else if (!strcmp(argv[1], "waiting")) waiting();
  else if (!strcmp(argv[1], "invalid"))
    {
      assert(mp_queue_create(0, 16) == -EINVAL);
      assert(mp_queue_create(65, 16) == -EINVAL);
      assert(mp_queue_create(8, 0) == -EINVAL);
      assert(mp_queue_create(8, 65) == -EINVAL);
      assert(opens == 0 && closes == 0 && unlinks == 0);
    }
  else assert(!"unknown case");
  puts("native queue adapter host control passed");
  return 0;
}

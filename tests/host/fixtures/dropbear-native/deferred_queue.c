/* SPDX-License-Identifier: MIT
 * Compile the complete prepared session initializer and deferred-reply queue
 * functions. Buffer and encryption dependencies observe ownership only; this
 * witness does not qualify key exchange or a cryptographic implementation.
 */

#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <setjmp.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define DROPBEAR_NUTTX 1
#define DROPBEAR_SVR_MULTIUSER 0
#define DROPBEAR_FUZZ 0
#define DROPBEAR_PLUGIN 0
#define DEBUG_TRACE 0
#define DISABLE_ZLIB 1
#define DROPBEAR_PRIO_NORMAL 0
#define DROPBEAR_COMP_NONE 0
#define SSH_MSG_KEXINIT 20
#define TRACE(x)
#define MAX(a, b) ((a) > (b) ? (a) : (b))
#include "backport-limits.inc"

#ifndef WITNESS_QUEUE_CASE
#  define WITNESS_QUEUE_CASE 0
#endif

typedef struct
{
  unsigned int size;
  unsigned int len;
  unsigned int pos;
  unsigned char *data;
} buffer;

struct packetlist
{
  struct packetlist *next;
  buffer *payload;
};

struct key_direction
{
  const void *algo_crypt;
  const void *crypt_mode;
  const void *algo_mac;
  int algo_comp;
};

struct key_context
{
  struct key_direction recv;
  struct key_direction trans;
  const void *algo_kex;
  int algo_hostkey;
};

static struct
{
  int sock_in;
  int sock_out;
  int maxfd;
  int socket_prio;
  int signal_pipe[2];
  time_t connect_time;
  time_t last_packet_time_keepalive_recv;
  time_t last_packet_time_idle;
  time_t last_packet_time_any_sent;
  time_t last_packet_time_keepalive_sent;
  buffer *writepayload;
  buffer *readbuf;
  buffer *payload;
  unsigned int transseq;
  unsigned int recvseq;
  unsigned int writequeue;
  int requirenext;
  int dataallowed;
  int ignorenext;
  int lastpacket;
  struct packetlist *reply_queue_head;
  struct packetlist *reply_queue_tail;
  size_t reply_queue_len;
  struct key_context *keys;
  struct key_context *newkeys;
  void *session_id;
  void *kexhashbuf;
  void *transkexinit;
  void *dh_K;
  void *remoteident;
  void *chantypes;
  int allowprivport;
} ses;

static const int dropbear_nocipher;
static const int dropbear_mode_none;
static const int dropbear_nohash;
static unsigned int allocation_count;
static unsigned int free_count;
static unsigned int payload_copies;
static unsigned int buffer_count;
static unsigned int buffer_frees;
static unsigned int encrypted_replies;
static int reject_expected;
static int allocation_forbidden;
static jmp_buf rejected;

static void *m_malloc(size_t size)
{
  void *result;

  assert(!allocation_forbidden && "deferred reply allocated beyond bound");
  result = calloc(1, size);
  assert(result != NULL);
  allocation_count++;
  return result;
}

static void m_free(void *pointer)
{
  assert(pointer != NULL);
  free_count++;
  free(pointer);
}

static buffer *buf_new(unsigned int size)
{
  buffer *result;

  assert(!allocation_forbidden && "deferred payload allocated beyond bound");
  result = calloc(1, sizeof(*result));
  assert(result != NULL);
  result->data = malloc(size ? size : 1);
  assert(result->data != NULL);
  result->size = size;
  buffer_count++;
  return result;
}

static buffer *buf_newcopy(const buffer *source)
{
  buffer *result = buf_new(source->len);

  memcpy(result->data, source->data, source->len);
  result->len = source->len;
  payload_copies++;
  return result;
}

static void buf_setpos(buffer *buf, unsigned int position)
{
  assert(position <= buf->len);
  buf->pos = position;
}

static void buf_setlen(buffer *buf, unsigned int length)
{
  assert(length <= buf->size);
  buf->len = length;
  if (buf->pos > length)
    {
      buf->pos = length;
    }
}

static void buf_putbytes(buffer *buf, const unsigned char *data,
                         unsigned int length)
{
  assert(buf->pos + length <= buf->size);
  memcpy(buf->data + buf->pos, data, length);
  buf->pos += length;
  buf->len = MAX(buf->len, buf->pos);
}

static void buf_free(buffer *buf)
{
  assert(buf != NULL);
  buffer_frees++;
  free(buf->data);
  free(buf);
}

static void dropbear_exit(const char *message, ...) __attribute__((noreturn));
static void dropbear_exit(const char *message, ...)
{
  assert(reject_expected);
  assert(strcmp(message, "Full reply queue") == 0);
  longjmp(rejected, 1);
}

static void setnonblocking(int fd)
{
  assert(fd >= 0);
}

static void update_channel_prio(void)
{
}

static int witness_getgroups(int size, void *groups)
{
  assert(size == 0 && groups == NULL);
  errno = ENOSYS;
  return -1;
}

#define getgroups witness_getgroups

static time_t monotonic_now(void)
{
  return 123;
}

static void initqueue(unsigned int *queue)
{
  *queue = 0;
}

static void encrypt_packet(void)
{
  assert(ses.dataallowed);
  assert(ses.writepayload->len == 1);
  assert(ses.writepayload->data[0] == (unsigned char)(encrypted_replies + 1));
  encrypted_replies++;
  buf_setpos(ses.writepayload, 0);
  buf_setlen(ses.writepayload, 0);
}

#define CHECKCLEARTOWRITE() assert(ses.writepayload->len == 0)
#include "deferred-queue-functions.inc"

static void initialize(int poison_length)
{
  ses.reply_queue_len = poison_length ? 99 : 0;
  common_session_init(-1, -1);
  if (poison_length)
    {
      assert(ses.reply_queue_len == 0 && "new session retained deferred count");
    }

  assert(ses.reply_queue_head == NULL && ses.reply_queue_tail == NULL);
  assert(ses.writepayload != NULL && ses.keys != NULL);
}

static void release_session(void)
{
  assert(ses.reply_queue_head == NULL && ses.reply_queue_tail == NULL);
  assert(close(ses.signal_pipe[0]) == 0);
  assert(close(ses.signal_pipe[1]) == 0);
  buf_free(ses.writepayload);
  m_free(ses.keys);
  assert(allocation_count == free_count);
  assert(buffer_count == buffer_frees);
}

static void add_reply(unsigned char value)
{
  assert(ses.writepayload->len == 0);
  buf_putbytes(ses.writepayload, &value, 1);
  enqueue_reply_packet();
}

int main(void)
{
  unsigned int allocations_at_bound;
  unsigned int copies_at_bound;
  struct packetlist *first;
  struct packetlist *last;

  /* Preserve the upstream '>' comparison exactly. With the native threshold
   * one, two replies fit; the third must exit before allocating either item.
   */

  assert(MAX_DEFER_REPLY_QUEUE == 1);
  initialize(WITNESS_QUEUE_CASE == 0 || WITNESS_QUEUE_CASE == 3);
  if (WITNESS_QUEUE_CASE == 3)
    {
      release_session();
      puts("PASS fresh session clears deferred reply count");
      return 0;
    }

  ses.dataallowed = 0;
  add_reply(1);
  add_reply(2);
  first = ses.reply_queue_head;
  last = ses.reply_queue_tail;
  assert(first != NULL && first->next == last && last->next == NULL);
  assert(payload_copies == 2);

  if (WITNESS_QUEUE_CASE == 0)
    {
      assert(ses.reply_queue_len == MAX_DEFER_REPLY_QUEUE + 1);
    }

  if (WITNESS_QUEUE_CASE != 2)
    {
      allocations_at_bound = allocation_count;
      copies_at_bound = payload_copies;
      buf_putbytes(ses.writepayload, (const unsigned char *)"x", 1);
      reject_expected = 1;
      allocation_forbidden = 1;
      if (setjmp(rejected) == 0)
        {
          enqueue_reply_packet();
          assert(!"deferred reply guard did not reject the third item");
        }

      reject_expected = 0;
      allocation_forbidden = 0;
      assert(allocation_count == allocations_at_bound);
      assert(payload_copies == copies_at_bound);
      assert(ses.reply_queue_head == first && ses.reply_queue_tail == last);
      assert(ses.reply_queue_len == MAX_DEFER_REPLY_QUEUE + 1);
      assert(first->next == last && last->next == NULL);
      assert(ses.writepayload->len == 1 && ses.writepayload->data[0] == 'x');
      buf_setpos(ses.writepayload, 0);
      buf_setlen(ses.writepayload, 0);
      puts("PASS third deferred reply rejected before allocation");
    }

  /* The isolated drain negative control poisons only the scalar count; the
   * actual historical enqueue/flush definitions still own both queue items.
   */

  if (WITNESS_QUEUE_CASE == 2)
    {
      ses.reply_queue_len = 2;
    }

  maybe_flush_reply_queue();
  assert(ses.reply_queue_head == first && ses.reply_queue_tail == last);
  assert(ses.reply_queue_len == MAX_DEFER_REPLY_QUEUE + 1);
  assert(encrypted_replies == 0);
  ses.dataallowed = 1;
  maybe_flush_reply_queue();
  assert(ses.reply_queue_head == NULL && ses.reply_queue_tail == NULL);
  assert(ses.reply_queue_len == 0 && "drain retained deferred reply count");
  assert(encrypted_replies == 2);
  assert(buffer_frees == 2);
  assert(free_count == 2);
  maybe_flush_reply_queue();
  assert(encrypted_replies == 2);
  release_session();
  puts("PASS deferred queue drains once and resets its count");

  if (WITNESS_QUEUE_CASE == 0)
    {
      initialize(1);
      ses.dataallowed = 0;
      add_reply(3);
      assert(ses.reply_queue_len == 1);
      ses.dataallowed = 1;
      maybe_flush_reply_queue();
      assert(ses.reply_queue_len == 0 && encrypted_replies == 3);
      release_session();
      puts("PASS next session starts with reusable empty queue");
    }

  return 0;
}

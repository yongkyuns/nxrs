/* SPDX-License-Identifier: MIT
 * The runner extracts these two complete functions from the actual prepared
 * upstream source. Stubs observe allocation and dispatch to the existing
 * session owner; the separate lifecycle/NSH witnesses exercise its cleanup.
 */

#include "includes.h"
#include <setjmp.h>

struct sshsession ses;
struct runopts opts;

static unsigned int allocations;
static unsigned int buffers;
static unsigned int close_requests;
static struct dropbear_channel *active_channel;
static jmp_buf owner_exit;

void *m_malloc(size_t size)
{
  void *allocation = calloc(1, size);

  assert(allocation != NULL);
  allocations++;
  return allocation;
}

void *m_realloc(void *pointer, size_t size)
{
  (void)pointer;
  (void)size;
  /* With one existing slot, a second channel must be refused before growth. */
  assert(!"channel limit admitted an allocation");
  abort();
}

static void *cbuf_new(unsigned int size)
{
  void *buffer = calloc(1, size);

  assert(size == opts.recv_window);
  assert(buffer != NULL);
  buffers++;
  return buffer;
}

static struct dropbear_channel *getchannel_msg(const char *kind)
{
  assert(strcmp(kind, "Close") == 0);
  assert(active_channel != NULL);
  return active_channel;
}

void dropbear_close(const char *message, ...)
{
  assert(strcmp(message, "Channel closed") == 0);
  assert(active_channel->recv_close == 1);
  assert(active_channel->recv_eof == 1);
  close_requests++;
  longjmp(owner_exit, 1);
}

#include "channel-functions.inc"

int main(void)
{
  struct dropbear_channel *slots[1] = {NULL};
  const struct dropbear_chantype type = {.name = "session"};
  struct dropbear_channel *channel;

  ses.channels = slots;
  ses.chansize = 1;
  opts.recv_window = 512;

  channel = newchannel(9, &type, 1024, 256);
  assert(channel != NULL);
  assert(allocations == 1 && buffers == 1);
  assert(ses.chancount == 1 && slots[0] == channel);
  assert(channel->remotechan == 9 && channel->index == 0);
  assert(channel->readfd == FD_UNINIT && channel->writefd == FD_UNINIT);
  assert(channel->errfd == FD_CLOSED);
  assert(channel->recvwindow == opts.recv_window);

  assert(newchannel(10, &type, 1024, 256) == NULL);
  assert(allocations == 1 && buffers == 1);
  assert(ses.chancount == 1 && slots[0] == channel);
  puts("PASS channel second request refused before allocation");

  active_channel = channel;
  if (setjmp(owner_exit) == 0)
    {
      recv_msg_channel_close();
      assert(!"active client close did not reach the session owner");
    }

  assert(close_requests == 1 && allocations == 1 && buffers == 1);
  puts("PASS channel active client close enters owned teardown");

  /* The client's acknowledgment of a server-driven close follows the same
   * retirement path; it cannot start another channel or invent an exit code.
   */

  channel->sent_close = 1;
  channel->recv_close = channel->recv_eof = 0;
  if (setjmp(owner_exit) == 0)
    {
      recv_msg_channel_close();
      assert(!"close acknowledgment did not reach the session owner");
    }

  assert(close_requests == 2 && channel->sent_close == 1);
  assert(allocations == 1 && buffers == 1);
  puts("PASS channel close acknowledgment enters owned teardown");

  free(channel->writebuf);
  free(channel);
  slots[0] = NULL;
  ses.chancount = 0;
  active_channel = NULL;

  channel = newchannel(11, &type, 1024, 256);
  assert(channel != NULL && channel->remotechan == 11);
  assert(allocations == 2 && buffers == 2 && ses.chancount == 1);
  free(channel->writebuf);
  free(channel);
  puts("PASS channel capacity reusable only after retirement");
  return 0;
}

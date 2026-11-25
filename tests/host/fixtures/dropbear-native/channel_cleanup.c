/* SPDX-License-Identifier: MIT
 * Compile the complete real remove_channel function. Its dependencies observe
 * that owner cancellation precedes PTY closure and all resource retirement.
 */

#include "includes.h"

#define IS_DROPBEAR_SERVER 1

struct sshsession ses;

static unsigned int cancellation_count;
static unsigned int buffer_count;
static unsigned int descriptor_count;
static unsigned int cleanup_count;
static unsigned int connect_count;
static unsigned int free_count;
static unsigned int priority_count;
static struct dropbear_channel *owned_channel;

void dropbear_nuttx_channel_cancel(const struct dropbear_channel *channel)
{
  assert(channel == owned_channel);
  assert(cancellation_count == 0);
  assert(buffer_count == 0 && descriptor_count == 0 && cleanup_count == 0);
  cancellation_count++;
}

static void cbuf_free(void *buffer)
{
  assert(cancellation_count == 1);
  assert(buffer != NULL);
  assert(descriptor_count == 0);
  buffer_count++;
  free(buffer);
}

static void m_close(int descriptor)
{
  assert(cancellation_count == 1 && buffer_count == 2);
  assert(descriptor == 70 + (int)descriptor_count);
  assert(cleanup_count == 0);
  descriptor_count++;
}

static void channel_cleanup(const struct dropbear_channel *channel)
{
  assert(channel == owned_channel);
  assert(cancellation_count == 1 && descriptor_count == 3);
  assert(free_count == 0);
  /* The real NSH witness separately proves this callback joins its waiter. */
  cleanup_count++;
}

static void cancel_connect(void *connection)
{
  assert(connection == owned_channel->conn_pending);
  assert(cleanup_count == 1 && free_count == 0);
  connect_count++;
}

void m_free_direct(void *pointer)
{
  assert(pointer == owned_channel);
  assert(cleanup_count == 1 && connect_count == 1);
  assert(ses.channels[0] == NULL && ses.chancount == 1);
  free_count++;
  free(pointer);
  owned_channel = NULL;
}

static void update_channel_prio(void)
{
  assert(free_count == 1 && ses.chancount == 0);
  priority_count++;
}

#include "channel-removal-function.inc"

int main(void)
{
  const struct dropbear_chantype type = {
    .name = "session",
    .cleanup = channel_cleanup,
  };
  struct dropbear_channel *slots[1];
  int pending;

  owned_channel = calloc(1, sizeof(*owned_channel));
  assert(owned_channel != NULL);
  slots[0] = owned_channel;
  ses.channels = slots;
  ses.chansize = ses.chancount = 1;
  owned_channel->type = &type;
  owned_channel->writebuf = malloc(1);
  owned_channel->extrabuf = malloc(1);
  assert(owned_channel->writebuf && owned_channel->extrabuf);
  owned_channel->writefd = 70;
  owned_channel->readfd = 71;
  owned_channel->errfd = 72;
  owned_channel->conn_pending = &pending;

  remove_channel(owned_channel);
  assert(cancellation_count == 1 && buffer_count == 2 && descriptor_count == 3);
  assert(cleanup_count == 1 && connect_count == 1 && free_count == 1);
  assert(priority_count == 1 && owned_channel == NULL && slots[0] == NULL);
  puts("PASS channel cancellation precedes PTY close, waiter cleanup and free");
  return 0;
}

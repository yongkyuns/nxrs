/* SPDX-License-Identifier: MIT
 * Compile both complete prepared channel-open handlers. Dependency stubs pass
 * peer-provided scalar fields and observe what the handlers give allocation
 * and initialization; they do not normalize the packet size themselves.
 */

#include <assert.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define DROPBEAR_NUTTX 1
#define TRACE(x)
#define MIN(a, b) ((a) < (b) ? (a) : (b))
#define MAX(a, b) ((a) > (b) ? (a) : (b))
#define SSH_OPEN_UNKNOWN_CHANNEL_TYPE 3
#define SSH_OPEN_RESOURCE_SHORTAGE 4
#define SSH_OPEN_IN_PROGRESS -1
#include "backport-limits.inc"

#ifndef WITNESS_CHANNEL_CASE
#  define WITNESS_CHANNEL_CASE 0
#endif

struct Channel;
struct ChanType
{
  const char *name;
  int (*inithandler)(struct Channel *);
};

struct Channel
{
  const struct ChanType *type;
  unsigned int remotechan;
  unsigned int transwindow;
  unsigned int transmaxpacket;
  unsigned int recvwindow;
  unsigned int recvmaxpacket;
  unsigned int index;
  int await_open;
};

struct payload
{
  unsigned int values[3];
  unsigned int next;
};

static struct
{
  struct payload *payload;
  const struct ChanType **chantypes;
} ses;

static struct Channel channel;
static unsigned int expected_packet;
static unsigned int initializations;
static unsigned int confirmations;
static unsigned int priority_updates;

static char *buf_getstring(struct payload *payload, unsigned int *length)
{
  char *result = malloc(sizeof("session"));

  assert(payload == ses.payload && result != NULL);
  memcpy(result, "session", sizeof("session"));
  *length = sizeof("session") - 1;
  return result;
}

static unsigned int buf_getint(struct payload *payload)
{
  assert(payload == ses.payload && payload->next < 3);
  return payload->values[payload->next++];
}

static void m_free(void *pointer)
{
  assert(pointer != NULL);
  free(pointer);
}

static int initialized(struct Channel *selected)
{
  assert(selected == &channel);
  assert(selected->transmaxpacket == expected_packet);
  initializations++;
  return 0;
}

static const struct ChanType session = {"session", initialized};
static const struct ChanType *types[] = {&session, NULL};

static struct Channel *newchannel(unsigned int remotechan,
                                  const struct ChanType *type,
                                  unsigned int window, unsigned int packet)
{
  assert(type == &session);
  channel.type = type;
  channel.remotechan = remotechan;
  channel.transwindow = window;
  channel.transmaxpacket = packet;
  channel.recvwindow = 512;
  channel.recvmaxpacket = 256;
  return &channel;
}

static struct Channel *getchannel(void)
{
  return &channel;
}

static void remove_channel(struct Channel *selected)
{
  (void)selected;
  assert(!"unexpected channel removal");
}

static void update_channel_prio(void)
{
  priority_updates++;
}

static void send_msg_channel_open_confirmation(struct Channel *selected,
                                               unsigned int window,
                                               unsigned int packet)
{
  assert(selected == &channel);
  assert(window == channel.recvwindow && packet == channel.recvmaxpacket);
  confirmations++;
}

static void send_msg_channel_open_failure(unsigned int remote,
                                         unsigned int error,
                                         const char *reason,
                                         const char *language)
{
  (void)remote;
  (void)error;
  (void)reason;
  (void)language;
  assert(!"unexpected channel-open failure");
}

static void dropbear_exit(const char *message, ...) __attribute__((noreturn));
static void dropbear_exit(const char *message, ...)
{
  (void)message;
  assert(!"unexpected channel exit");
  abort();
}

#include "channel-packet-functions.inc"

static void receive(unsigned int advertised, unsigned int expected,
                    int confirmation)
{
  struct payload payload = {{9, 1024, advertised}, 0};
  unsigned int previous_initializations = initializations;
  unsigned int previous_priority = priority_updates;
  unsigned int previous_confirmations = confirmations;

  memset(&channel, 0, sizeof(channel));
  ses.payload = &payload;
  ses.chantypes = types;
  expected_packet = expected;
  if (confirmation)
    {
      channel.type = &session;
      channel.await_open = 1;
      recv_msg_channel_open_confirmation();
      assert(channel.await_open == 0);
    }
  else
    {
      recv_msg_channel_open();
    }

  assert(payload.next == 3);
  assert(channel.remotechan == 9 && channel.transwindow == 1024);
  assert(channel.transmaxpacket == expected);
  assert(channel.transmaxpacket >= 1);
  assert(channel.transmaxpacket <= TRANS_MAX_PAYLOAD_LEN);
  assert(initializations == previous_initializations + 1);
  assert(priority_updates == previous_priority + 1);
  assert(confirmations == previous_confirmations + (unsigned int)!confirmation);
}

int main(void)
{
  static const unsigned int advertised[] =
    {0, 1, 256, TRANS_MAX_PAYLOAD_LEN, TRANS_MAX_PAYLOAD_LEN + 1, UINT_MAX};
  static const unsigned int expected[] =
    {1, 1, 256, TRANS_MAX_PAYLOAD_LEN, TRANS_MAX_PAYLOAD_LEN, TRANS_MAX_PAYLOAD_LEN};
  unsigned int index;

  if (WITNESS_CHANNEL_CASE == 1)
    {
      receive(0, 1, 0);
    }
  else if (WITNESS_CHANNEL_CASE == 2)
    {
      receive(0, 1, 1);
    }
  else if (WITNESS_CHANNEL_CASE == 3)
    {
      receive(UINT_MAX, TRANS_MAX_PAYLOAD_LEN, 0);
    }
  else if (WITNESS_CHANNEL_CASE == 4)
    {
      receive(UINT_MAX, TRANS_MAX_PAYLOAD_LEN, 1);
    }
  else
    {
      for (index = 0; index < sizeof(advertised) / sizeof(advertised[0]); index++)
        {
          receive(advertised[index], expected[index], 0);
          receive(advertised[index], expected[index], 1);
        }
    }

  puts("PASS channel-open and confirmation bounds precede initialization");
  return 0;
}

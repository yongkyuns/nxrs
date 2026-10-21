/* Exercise the real output dispatcher after link-layer packet consumption. */
#include <assert.h>
#include <stddef.h>

#define FAR
#define OK 0

struct net_driver_s
{
  unsigned d_len;
  void *d_iob;
  void *d_fragout;
};

typedef int (*devif_poll_callback_t)(struct net_driver_s *);

static unsigned link_length;
static int retain_iob;
static int loopback_result;
static unsigned output_calls;
static unsigned loopback_calls;
static unsigned transmit_calls;
#ifdef CONFIG_NET_IPFRAG
static int fragment_result;
static unsigned fragment_calls;
static unsigned releases;

static int ip_fragout(struct net_driver_s *dev)
{
  assert(dev->d_len > 0 && dev->d_iob != NULL);
  fragment_calls++;
  return fragment_result;
}

static void netdev_iob_release(struct net_driver_s *dev)
{
  releases++;
  dev->d_iob = NULL;
}

static void *iob_peek_queue(void **queue)
{
  return *queue;
}

static int devif_poll_ipfrag(struct net_driver_s *dev,
                           devif_poll_callback_t callback)
{
  return callback(dev);
}
#endif

static void devif_out(struct net_driver_s *dev)
{
  output_calls++;
  dev->d_len = link_length;
  if (link_length == 0)
    {
      /* ARP/neighbor handling may drop a packet or transfer its ownership. */
      if (!retain_iob)
        {
          dev->d_iob = NULL;
        }
    }
}

static int devif_loopback(struct net_driver_s *dev)
{
  assert(dev->d_len > 0 && dev->d_iob != NULL);
  loopback_calls++;
  return loopback_result;
}

static int transmit(struct net_driver_s *dev)
{
  assert(dev->d_len > 0 && dev->d_iob != NULL);
  transmit_calls++;
  return 7;
}

/* FUNCTIONS */

int main(void)
{
  int packet;
  struct net_driver_s dev = {60, &packet, NULL};

  /* A consumed L2 packet must never reach loopback, fragmentation or NIC. */
  link_length = 0;
  assert(devif_poll_out(&dev, transmit) == 0);
  assert(output_calls == 1 && loopback_calls == 0 && transmit_calls == 0);
  assert(dev.d_iob == NULL);
#ifdef CONFIG_NET_IPFRAG
  assert(fragment_calls == 0 && releases == 0);
#endif

  /* Pending ARP resolution may clear d_len while retaining the IOB. */
  dev.d_len = 60;
  dev.d_iob = &packet;
  retain_iob = 1;
  assert(devif_poll_out(&dev, transmit) == 0);
  retain_iob = 0;
  assert(output_calls == 2 && loopback_calls == 0 && transmit_calls == 0);
  assert(dev.d_len == 0 && dev.d_iob == &packet);
#ifdef CONFIG_NET_IPFRAG
  assert(fragment_calls == 0 && releases == 0);
#endif

  /* An initially empty packet also does no work. */
  assert(devif_poll_out(&dev, transmit) == 0);
  assert(output_calls == 2 && loopback_calls == 0 && transmit_calls == 0);

  /* Normal and looped-back packets preserve their existing result. */
  dev.d_len = link_length = 74;
  dev.d_iob = &packet;
  assert(devif_poll_out(&dev, transmit) == 7);
  assert(transmit_calls == 1 && loopback_calls == 1);
  loopback_result = 1;
  assert(devif_poll_out(&dev, transmit) == 1);
  assert(transmit_calls == 1 && loopback_calls == 2);
  loopback_result = 0;
  assert(devif_poll_out(&dev, NULL) == 0);
  assert(transmit_calls == 1 && loopback_calls == 3);

#ifdef CONFIG_NET_IPFRAG
  fragment_result = -1;
  assert(devif_poll_out(&dev, transmit) == 1);
  assert(transmit_calls == 1 && releases == 1);
  fragment_result = 0;
  dev.d_iob = &packet;
  dev.d_fragout = &packet;
  assert(devif_poll_out(&dev, transmit) == 7);
  assert(transmit_calls == 2 && fragment_calls == 3);
#endif
  return 0;
}

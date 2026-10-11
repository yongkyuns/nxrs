/* Host regression coverage for the upstream RP23xx USB completion fixes.
 * The three driver functions under test are extracted from rp23xx_usbdev.c
 * into rp23xx-usb-functions.h by test-patches.py.  Queue, register, and
 * endpoint submission operations below are intentionally boundary mocks.
 */

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define OK 0
#define USBDEV_REQFLAGS_NULLPKT 1
#define RP23XX_USBCTRL_DPSRAM_EP_BUFF_CTRL_AVAIL (1u << 10)
#define RP23XX_USBCTRL_DPSRAM_EP_BUFF_CTRL_LEN_MASK 0x3ffu
#define RP23XX_TRACEERR_TXREQLOST 1
#define RP23XX_TRACEERR_RXREQLOST 2
#define TRACE_DEVERROR(x) (x)
#define TRACE_COMPLETE(x) (x)
#define usbtrace(event, value) ((void)(event), (void)(value))

#define CHECK(condition)                                                   \
  do                                                                       \
    {                                                                      \
      if (!(condition))                                                    \
        {                                                                  \
          fprintf(stderr, "%s:%d: check failed: %s\n", __FILE__,         \
                  __LINE__, #condition);                                   \
          return 1;                                                        \
        }                                                                  \
    }                                                                      \
  while (0)

struct usbdev_ep_s
{
  uint16_t maxpacket;
};

struct usbdev_req_s
{
  uint8_t *buf;
  uint32_t len;
  uint32_t xfrd;
  uint16_t flags;
  int result;
  void (*callback)(struct usbdev_ep_s *, struct usbdev_req_s *);
};

struct rp23xx_req_s
{
  struct usbdev_req_s req;
  struct rp23xx_req_s *flink;
  bool armed;
};

struct rp23xx_ep_s
{
  struct usbdev_ep_s ep;
  struct rp23xx_req_s *head;
  struct rp23xx_req_s *tail;
  uint8_t *data_buf;
  uint32_t buf_ctrl;
  uint8_t epphy;
  bool in;
  bool txnullpkt;
};

static uint32_t register_value;
static uint32_t writes[8];
static unsigned int write_count;
static unsigned int event_log[8];
static unsigned int event_count;
static unsigned int write_arms;
static unsigned int read_arms;

enum
{
  EVENT_BARRIER = 1,
  EVENT_WRITE
};

static uint32_t getreg32(uint32_t address)
{
  (void)address;
  return register_value;
}

static void putreg32(uint32_t value, uint32_t address)
{
  (void)address;
  register_value = value;
  writes[write_count++] = value;
  event_log[event_count++] = EVENT_WRITE;
}

static void test_barrier(void)
{
  event_log[event_count++] = EVENT_BARRIER;
}

#define UP_DMB() test_barrier()

static struct rp23xx_req_s *rp23xx_rqpeek(struct rp23xx_ep_s *ep)
{
  return ep->head;
}

static struct rp23xx_req_s *rp23xx_rqdequeue(struct rp23xx_ep_s *ep)
{
  struct rp23xx_req_s *req = ep->head;

  if (req != NULL)
    {
      ep->head = req->flink;
      if (ep->head == NULL)
        {
          ep->tail = NULL;
        }

      req->flink = NULL;
    }

  return req;
}

static void queue_request(struct rp23xx_ep_s *ep, struct rp23xx_req_s *req)
{
  req->flink = NULL;
  if (ep->tail == NULL)
    {
      ep->head = req;
    }
  else
    {
      ep->tail->flink = req;
    }

  ep->tail = req;
}

static void rp23xx_reqcomplete(struct rp23xx_ep_s *ep, int16_t result)
{
  struct rp23xx_req_s *req = rp23xx_rqdequeue(ep);

  if (req != NULL)
    {
      req->req.result = result;
      req->req.callback(&ep->ep, &req->req);
    }
}

static int rp23xx_wrrequest(struct rp23xx_ep_s *ep)
{
  struct rp23xx_req_s *req = rp23xx_rqpeek(ep);

  write_arms++;
  if (req != NULL)
    {
      req->armed = true;
    }

  /* Model the ZLP submission boundary needed to drive txcomplete through
   * its second completion.  The decision to submit it is made by the real
   * rp23xx_txcomplete implementation under test.
   */
  ep->txnullpkt = false;
  return OK;
}

static int rp23xx_rdrequest(struct rp23xx_ep_s *ep)
{
  struct rp23xx_req_s *req = rp23xx_rqpeek(ep);

  read_arms++;
  if (req != NULL)
    {
      req->armed = true;
    }

  return OK;
}

#include "rp23xx-usb-functions.h"

static struct rp23xx_req_s callback_request;
static uint8_t callback_buffer[32];
static struct rp23xx_ep_s *callback_endpoint;
static bool resubmit_in_callback;

static void completion_callback(struct usbdev_ep_s *ep,
                                struct usbdev_req_s *req)
{
  (void)ep;
  (void)req;

  if (resubmit_in_callback)
    {
      callback_request.req.buf = callback_buffer;
      callback_request.req.len = sizeof(callback_buffer);
      callback_request.req.xfrd = 0;
      callback_request.armed = true;
      queue_request(callback_endpoint, &callback_request);

      if (!callback_endpoint->in)
        {
          read_arms++;
        }
      else
        {
          write_arms++;
        }
    }
}

static void init_req(struct rp23xx_req_s *req, uint8_t *buffer,
                     uint32_t len)
{
  memset(req, 0, sizeof(*req));
  req->req.buf = buffer;
  req->req.len = len;
  req->req.callback = completion_callback;
}

static void reset_observation(void)
{
  register_value = 0;
  write_count = 0;
  event_count = 0;
  write_arms = 0;
  read_arms = 0;
  resubmit_in_callback = false;
}

static int test_callback_rearm_and_queued_request(void)
{
  struct rp23xx_ep_s ep = {.ep.maxpacket = 64, .epphy = 2, .in = true};
  struct rp23xx_req_s completed;
  struct rp23xx_req_s queued;
  uint8_t completed_buffer[64];
  uint8_t queued_buffer[64];

  reset_observation();
  init_req(&completed, completed_buffer, sizeof(completed_buffer));
  init_req(&queued, queued_buffer, sizeof(queued_buffer));
  completed.armed = true;
  queue_request(&ep, &completed);
  callback_endpoint = &ep;
  resubmit_in_callback = true;
  register_value = 64;
  rp23xx_txcomplete(&ep);
  CHECK(ep.head == &callback_request);
  CHECK(callback_request.armed);
  CHECK(write_arms == 1);

  reset_observation();
  memset(&ep, 0, sizeof(ep));
  ep.ep.maxpacket = 64;
  ep.epphy = 2;
  ep.in = true;
  init_req(&completed, completed_buffer, sizeof(completed_buffer));
  init_req(&queued, queued_buffer, sizeof(queued_buffer));
  queue_request(&ep, &completed);
  queue_request(&ep, &queued);
  register_value = 64;
  rp23xx_txcomplete(&ep);
  CHECK(ep.head == &queued);
  CHECK(queued.armed);
  CHECK(write_arms == 1);
  return 0;
}

static int test_tx_continuation_and_zlp(void)
{
  struct rp23xx_ep_s ep = {.ep.maxpacket = 64, .epphy = 2, .in = true};
  struct rp23xx_req_s req;
  uint8_t buffer[128];

  reset_observation();
  init_req(&req, buffer, sizeof(buffer));
  req.armed = true;
  queue_request(&ep, &req);
  register_value = 64;
  rp23xx_txcomplete(&ep);
  CHECK(ep.head == &req);
  CHECK(req.req.xfrd == 64);
  CHECK(write_arms == 1);

  reset_observation();
  memset(&ep, 0, sizeof(ep));
  ep.ep.maxpacket = 64;
  ep.epphy = 2;
  ep.in = true;
  init_req(&req, buffer, 64);
  req.armed = true;
  queue_request(&ep, &req);
  ep.txnullpkt = true;
  register_value = 64;
  rp23xx_txcomplete(&ep);
  CHECK(ep.head == &req);
  CHECK(req.req.xfrd == 64);
  CHECK(write_arms == 1);

  register_value = 0;
  rp23xx_txcomplete(&ep);
  CHECK(ep.head == NULL);
  CHECK(req.req.result == OK);
  CHECK(write_arms == 1);
  return 0;
}

static int test_rx_callback_rearm_and_continuation(void)
{
  struct rp23xx_ep_s ep = {.ep.maxpacket = 4, .epphy = 3};
  struct rp23xx_req_s completed;
  uint8_t completed_buffer[8];
  uint8_t packet[4] = {1, 2, 3, 4};

  reset_observation();
  init_req(&completed, completed_buffer, sizeof(packet));
  ep.data_buf = packet;
  queue_request(&ep, &completed);
  callback_endpoint = &ep;
  resubmit_in_callback = true;
  register_value = 4;
  rp23xx_rxcomplete(&ep);
  CHECK(memcmp(completed_buffer, packet, sizeof(packet)) == 0);
  CHECK(ep.head == &callback_request);
  CHECK(callback_request.armed);
  CHECK(read_arms == 1);

  reset_observation();
  memset(&ep, 0, sizeof(ep));
  ep.ep.maxpacket = 4;
  ep.epphy = 3;
  init_req(&completed, completed_buffer, sizeof(completed_buffer));
  ep.data_buf = packet;
  queue_request(&ep, &completed);
  register_value = 4;
  rp23xx_rxcomplete(&ep);
  CHECK(ep.head == &completed);
  CHECK(completed.req.xfrd == 4);
  CHECK(completed.armed);
  CHECK(read_arms == 1);
  return 0;
}

static int test_available_write_order(void)
{
  struct rp23xx_ep_s ep = {.buf_ctrl = 9};

  reset_observation();
  rp23xx_update_buffer_control(&ep, 0, 0x55u |
                               RP23XX_USBCTRL_DPSRAM_EP_BUFF_CTRL_AVAIL);
  CHECK(write_count == 2);
  CHECK(event_count == 3);
  CHECK(event_log[0] == EVENT_BARRIER);
  CHECK(event_log[1] == EVENT_WRITE);
  CHECK((writes[0] & RP23XX_USBCTRL_DPSRAM_EP_BUFF_CTRL_AVAIL) == 0);
  CHECK(event_log[2] == EVENT_WRITE);
  CHECK(writes[1] == (0x55u |
                      RP23XX_USBCTRL_DPSRAM_EP_BUFF_CTRL_AVAIL));
  return 0;
}

int main(void)
{
  if (test_callback_rearm_and_queued_request() != 0 ||
      test_tx_continuation_and_zlp() != 0 ||
      test_rx_callback_rearm_and_continuation() != 0 ||
      test_available_write_order() != 0)
    {
      return EXIT_FAILURE;
    }

  return EXIT_SUCCESS;
}

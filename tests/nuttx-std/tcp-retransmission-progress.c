/* SPDX-License-Identifier: MIT */
/* Actual TCP policy bodies; stand-ins model scheduling, not native ABI. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define FAR
#include "tcp-retransmission-constants.h"
#define DEBUGASSERT(x) assert(x)
#define ninfo(...) ((void)0)

struct net_driver_s { unsigned d_sndlen; void *d_iob; unsigned d_len; };
struct tcp_hdr_s {
  uint8_t ackno[4], seqno[4];
  uint16_t srcport, destport;
  uint8_t wnd[2], urgp[2], flags;
};
struct tcp_conn_s {
  uint8_t rcvseq[4], sndseq[4];
  uint16_t lport, rport;
  uint32_t rcv_adv, tx_unacked;
  uint8_t tcpstateflags, timeout, timer, rto, nrtx;
  uint16_t mss;
  uint32_t rexmit_seq;
  bool sendfile;
  uint32_t sndseq_max;
};

static unsigned timer_updates;
static unsigned sends;
static unsigned now_ms, deadline_ms;
static void tcp_update_retrantimer_patched(struct tcp_conn_s *, int);
static void tcp_update_retrantimer_baseline(struct tcp_conn_s *, int);
static void tcp_update_timer(struct tcp_conn_s *conn)
{
  timer_updates++;
  deadline_ms = conn->timer ? now_ms + 500 * conn->timer : 0;
}
static uint32_t tcp_getsequence(const uint8_t s[4])
{ return ((uint32_t)s[0] << 24) | ((uint32_t)s[1] << 16) | ((uint32_t)s[2] << 8) | s[3]; }
static uint32_t tcp_get_recvwindow(struct net_driver_s *dev, struct tcp_conn_s *conn)
{ (void)dev; (void)conn; return 4096; }
static void iob_update_pktlen(void *iob, unsigned len, bool flag)
{ (void)iob; (void)len; (void)flag; }
static void net_incr32(uint8_t s[4], unsigned n)
{ uint32_t v = tcp_getsequence(s) + n; s[0]=v>>24; s[1]=v>>16; s[2]=v>>8; s[3]=v; }
static uint8_t tcpip_hdrsize(struct tcp_conn_s *conn) { (void)conn; return 40; }
static bool tcp_should_send_recvwindow(struct tcp_conn_s *conn)
{ (void)conn; return false; }
static void tcp_send(struct net_driver_s *dev, struct tcp_conn_s *conn,
                     uint16_t flags, uint16_t len)
{ (void)dev; (void)conn; (void)flags; (void)len; sends++; }
static void tcp_rexmit(struct net_driver_s *dev, struct tcp_conn_s *conn, uint16_t result)
{ (void)dev; (void)conn; (void)result; }

#include "tcp-retransmission-functions.h"

#define CHECK(x) do { if (!(x)) { fprintf(stderr, "%s:%d: %s\n", __FILE__, __LINE__, #x); return 1; } } while (0)

static void init(struct tcp_conn_s *c, struct net_driver_s *d, struct tcp_hdr_s *h)
{
  memset(c, 0, sizeof(*c)); memset(d, 0, sizeof(*d)); memset(h, 0, sizeof(*h));
  c->rto = 3;
  c->mss = 1460;
  timer_updates = sends = 0;
  now_ms = deadline_ms = 0;
}

int main(void)
{
  struct tcp_conn_s patched, baseline;
  struct net_driver_s dev;
  struct tcp_hdr_s hdr;

  /* Pure ACKs cannot move the initial deadline, even across many polls. */
  init(&patched, &dev, &hdr);
  patched.tx_unacked = 64;
  patched.timer = 3;
  deadline_ms = 1500;
  for (unsigned elapsed = 0; elapsed < 15; elapsed++)
    {
      now_ms = elapsed * 100;
      tcp_sendcommon_patched(&dev, &patched, &hdr);
    }
  CHECK(patched.timer == 3 && timer_updates == 0 && deadline_ms == 1500);
  now_ms = 1500;
  patched.timeout = true; /* timer expiry queued for TX polling */
  tcp_sendcommon_patched(&dev, &patched, &hdr);
  CHECK(patched.timeout && patched.timer == 3);
  CHECK(timer_updates == 0 && deadline_ms == 1500);
  patched.timer = 0;
  tcp_sendcommon_patched(&dev, &patched, &hdr);
  CHECK(patched.timeout && patched.timer == 0 && timer_updates == 0);

  /* Reverse-patch behavior documents the regression: ACKs restart and clear. */
  init(&baseline, &dev, &hdr);
  baseline.tx_unacked = 64;
  baseline.timer = 3;
  baseline.timeout = true;
  for (unsigned elapsed = 0; elapsed < 15; elapsed++)
    {
      now_ms = elapsed * 100;
      tcp_sendcommon_baseline(&dev, &baseline, &hdr);
    }
  CHECK(baseline.timer == baseline.rto && timer_updates == 15 && !baseline.timeout);
  CHECK(deadline_ms == 2900);

  /* Explicit reset/cancel owns clearing a stale pending expiry. */
  patched.timeout = true;
  tcp_update_retrantimer_patched(&patched, 7);
  CHECK(!patched.timeout && patched.timer == 7);
  patched.timeout = true;
  tcp_update_retrantimer_patched(&patched, 0);
  CHECK(!patched.timeout && patched.timer == 0);
  baseline.timeout = true;
  tcp_update_retrantimer_baseline(&baseline, 7);
  CHECK(baseline.timeout && baseline.timer == 7);

  /* A stopped receive window still arms an idle timer for outstanding bytes. */
  init(&patched, &dev, &hdr);
  patched.tcpstateflags = TCP_STOPPED;
  patched.tx_unacked = 64;
  tcp_sendcommon_patched(&dev, &patched, &hdr);
  CHECK(patched.timer == patched.rto && hdr.wnd[0] == 0 && hdr.wnd[1] == 0);
  CHECK(timer_updates == 1);
  patched.timer = 2;
  tcp_sendcommon_patched(&dev, &patched, &hdr);
  CHECK(patched.timer == 2 && timer_updates == 1);

  /* The same outstanding predicate covers SYN/FIN sequence space and data. */
  const unsigned lengths[] = { 1, 1, 64 };
  for (unsigned i = 0; i < 3; i++)
    {
      init(&patched, &dev, &hdr);
      patched.tx_unacked = lengths[i];
      hdr.flags = i == 0 ? TCP_SYN : (i == 1 ? TCP_FIN : 0);
      tcp_sendcommon_patched(&dev, &patched, &hdr);
      CHECK(patched.timer == patched.rto && timer_updates == 1);
    }
  init(&patched, &dev, &hdr);
  tcp_sendcommon_patched(&dev, &patched, &hdr);
  CHECK(patched.timer == 0 && timer_updates == 0);

  /* Idle unbuffered polls preserve retry count; actual new data resets it. */
  init(&patched, &dev, &hdr);
  patched.nrtx = 4;
  patched.sendfile = true;
  tcp_appsend_patched(&dev, &patched, TCP_POLL);
  CHECK(patched.nrtx == 4);
  dev.d_sndlen = 64;
  tcp_appsend_patched(&dev, &patched, 0);
#if !defined(CONFIG_NET_TCP_WRITE_BUFFERS) || defined(CONFIG_NET_SENDFILE)
  CHECK(patched.nrtx == 0 && patched.tx_unacked == 64);
#else
  CHECK(patched.nrtx == 4 && patched.tx_unacked == 0);
#endif
  init(&baseline, &dev, &hdr);
  baseline.nrtx = 4;
  baseline.sendfile = true;
  tcp_appsend_baseline(&dev, &baseline, TCP_POLL);
#if !defined(CONFIG_NET_TCP_WRITE_BUFFERS) || defined(CONFIG_NET_SENDFILE)
  CHECK(baseline.nrtx == 0);
#else
  CHECK(baseline.nrtx == 4);
#endif

  puts("actual NuttX retransmission functions preserve timer and retry progress");
  return 0;
}

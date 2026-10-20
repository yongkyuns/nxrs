/* Actual input/close-state excerpts, with native sequence trimming. */
#include <assert.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#define FAR
#define DEBUGASSERT(value) assert(value)
#define ninfo(...) ((void)0)
#define TCP_STATE_MASK 0x0f
#define TCP_CLOSED 0
#define TCP_SYN_RCVD 2
#define TCP_SYN_SENT 3
#define TCP_ESTABLISHED 4
#define TCP_FIN_WAIT_1 5
#define TCP_FIN_WAIT_2 6
#define TCP_CLOSE_WAIT 7
#define TCP_CLOSING 8
#define TCP_TIME_WAIT 9
#define TCP_LAST_ACK 10
#define TCP_FIN 0x01
#define TCP_SYN 0x02
#define TCP_ACK 0x10
#define TCP_URG 0x20
#define TCP_CTL 0x3f
#define TCP_ACKDATA 0x02
#define TCP_NEWDATA 0x01
#define TCP_RXCLOSE 0x100
#define TCP_TXCLOSE 0x200
#define TCP_TIME_WAIT_TIMEOUT 120
#define HSEC_PER_SEC 2
#define TCP_SEQ_ADD(a, b) ((uint32_t)((a) + (b)))
#define TCP_SEQ_SUB(a, b) ((uint32_t)((a) - (b)))
#define TCP_SEQ_LT(a, b) ((int32_t)TCP_SEQ_SUB(a, b) < 0)
#define TCP_SEQ_LTE(a, b) ((int32_t)TCP_SEQ_SUB(a, b) <= 0)

struct net_driver_s
{
  unsigned d_len;
  unsigned d_sndlen;
  uint8_t *d_appdata;
};

struct tcp_hdr_s
{
  uint8_t seqno[4];
  uint8_t ackno[4];
  uint8_t urgp[2];
  uint8_t flags;
};

struct tcp_conn_s
{
  uint8_t tcpstateflags;
  uint8_t sndseq[4];
  uint8_t rcvseq[4];
  uint32_t sndseq_max;
  uint32_t tx_unacked;
  unsigned nrtx;
  int rto;
  int timer;
  int sa;
  int sv;
  bool sendfile;
};

static unsigned processed;
static unsigned acknowledgements;
static unsigned resets;
static unsigned close_notifications;

static uint32_t tcp_getsequence(const uint8_t value[4])
{
  return (uint32_t)value[0] << 24 | (uint32_t)value[1] << 16 |
         (uint32_t)value[2] << 8 | value[3];
}

static void tcp_setsequence(uint8_t value[4], uint32_t sequence)
{
  for (unsigned index = 0; index < 4; index++)
    {
      value[3 - index] = sequence >> (8 * index);
    }
}

static void net_incr32(uint8_t value[4], unsigned amount)
{
  tcp_setsequence(value, TCP_SEQ_ADD(tcp_getsequence(value), amount));
}

static void tcp_send(struct net_driver_s *dev, struct tcp_conn_s *conn,
                     unsigned flags, unsigned length)
{
  (void)dev;
  (void)conn;
  (void)length;
  assert(flags == TCP_ACK);
  acknowledgements++;
}

static void tcp_update_retrantimer(struct tcp_conn_s *conn, int timeout)
{
  conn->timer = timeout;
}

static void tcp_reset(struct net_driver_s *dev, struct tcp_conn_s *conn)
{
  (void)dev;
  (void)conn;
  resets++;
}

static unsigned tcp_callback(struct net_driver_s *dev, struct tcp_conn_s *conn,
                             unsigned flags)
{
  (void)dev;
  (void)conn;
  if (flags == TCP_TXCLOSE)
    {
      close_notifications++;
    }
  return flags;
}

static void tcp_appsend(struct net_driver_s *dev, struct tcp_conn_s *conn,
                        unsigned flags)
{
  (void)dev;
  (void)conn;
  (void)flags;
}

/* TRIM_FUNCTION */

static void input(struct net_driver_s *dev, struct tcp_conn_s *conn,
                  struct tcp_hdr_s *tcp)
{
  unsigned flags = 0;
  unsigned result;
  const unsigned tcpiplen = 40;
  /* INPUT_SECTION */
  processed++;
  switch (conn->tcpstateflags & TCP_STATE_MASK)
    {
      /* CLOSE_STATES */
      default:
        break;
    }
drop:
  dev->d_len = 0;
  return;
reset:
  resets++;
}

static void prepare(struct tcp_conn_s *conn, struct tcp_hdr_s *tcp,
                    unsigned state, uint32_t receive_next)
{
  memset(conn, 0, sizeof(*conn));
  memset(tcp, 0, sizeof(*tcp));
  conn->tcpstateflags = state;
  conn->sndseq_max = 300;
  conn->tx_unacked = 1;
  conn->rto = conn->timer = 3;
  conn->sa = conn->sv = 1;
  tcp_setsequence(conn->sndseq, 300);
  tcp_setsequence(conn->rcvseq, receive_next);
  tcp_setsequence(tcp->seqno, receive_next);
  tcp_setsequence(tcp->ackno, 300);
  tcp->flags = TCP_ACK;
  processed = acknowledgements = resets = close_notifications = 0;
}

int main(void)
{
  struct tcp_conn_s conn;
  struct tcp_hdr_s tcp;
  uint8_t payload[8] = {0};
  struct net_driver_s dev = {0, 0, payload};

  for (unsigned sendfile = 0; sendfile < 2; sendfile++)
    {
      /* The observed duplicate FIN is left of RCV.NXT, including wrap. */
      const uint32_t positions[] = {1001, 0};
      for (unsigned index = 0; index < 2; index++)
        {
          prepare(&conn, &tcp, TCP_CLOSING, positions[index]);
          conn.sendfile = sendfile;
          tcp_setsequence(tcp.seqno, TCP_SEQ_SUB(positions[index], 1));
          tcp.flags |= TCP_FIN;
          input(&dev, &conn, &tcp);
          assert(processed == 0 && acknowledgements == 1);
          assert(conn.tx_unacked == 1 && conn.timer == 3);
          assert(conn.sa == 1 && conn.sv == 1);
          assert(conn.tcpstateflags == TCP_CLOSING);
          tcp.flags = TCP_ACK;
          tcp_setsequence(tcp.seqno, positions[index]);
          input(&dev, &conn, &tcp);
          assert(processed == 1 && conn.tx_unacked == 0);
          assert(conn.tcpstateflags == TCP_TIME_WAIT);
          assert(conn.timer == TCP_TIME_WAIT_TIMEOUT * HSEC_PER_SEC);
        }

      /* A subsequent acceptable full ACK completes each FIN handshake. */
      const unsigned states[] = {TCP_CLOSING, TCP_LAST_ACK, TCP_FIN_WAIT_1};
      for (unsigned index = 0; index < 3; index++)
        {
          prepare(&conn, &tcp, states[index], 1001);
          conn.sendfile = sendfile;
          input(&dev, &conn, &tcp);
          unsigned expected = states[index] == TCP_CLOSING ? TCP_TIME_WAIT :
                              states[index] == TCP_LAST_ACK ? TCP_CLOSED :
                              TCP_FIN_WAIT_2;
          assert(processed == 1 && conn.tx_unacked == 0);
          assert(conn.tcpstateflags == expected);
          assert(close_notifications == (states[index] == TCP_LAST_ACK));

          /* An ACK that does not cover FIN must not complete the close. */
          prepare(&conn, &tcp, states[index], 1001);
          conn.sendfile = sendfile;
          tcp_setsequence(tcp.ackno, 299);
          input(&dev, &conn, &tcp);
          assert(processed == 1 && conn.tx_unacked == 1);
          assert(conn.tcpstateflags == states[index]);
          assert(close_notifications == 0);
        }

      /* Discarded keepalives/future data must not retire outstanding bytes. */
      for (int offset = -1; offset <= 1; offset += 2)
        {
          prepare(&conn, &tcp, TCP_ESTABLISHED, 1001);
          conn.sendfile = sendfile;
          tcp_setsequence(tcp.seqno, (uint32_t)(1001 + offset));
          input(&dev, &conn, &tcp);
          assert(processed == 0 && conn.tx_unacked == 1 && conn.timer == 3);
        }

      /* Trimming old data must still admit a segment with new payload. */
      prepare(&conn, &tcp, TCP_ESTABLISHED, 1001);
      conn.sendfile = sendfile;
      tcp_setsequence(tcp.seqno, 1000);
      dev.d_len = 2;
      dev.d_appdata = payload;
      input(&dev, &conn, &tcp);
      assert(processed == 1 && conn.tx_unacked == 0);
      assert(dev.d_appdata == payload + 1);
    }
  return 0;
}

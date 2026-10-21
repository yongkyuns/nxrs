/* SPDX-License-Identifier: MIT */
/* Actual congestion-control and RTO policy bodies; no socket ABI emulation. */
#include <assert.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define FAR
#define ninfo(...) ((void)0)
#define MIN(a, b) ((a) < (b) ? (a) : (b))
#define MAX(a, b) ((a) > (b) ? (a) : (b))
#define TCP_SEQ_SUB(a, b) ((uint32_t)((a) - (b)))
#define TCP_SEQ_LT(a, b) ((int32_t)TCP_SEQ_SUB(a, b) < 0)
#define TCP_SEQ_LTE(a, b) ((int32_t)TCP_SEQ_SUB(a, b) <= 0)
#define TCP_SEQ_GT(a, b) ((int32_t)TCP_SEQ_SUB(a, b) > 0)
#define TCP_IPV4_DEFAULT_MSS 1460
/* NATIVE_CONSTANTS */

struct tcp_hdr_s { uint8_t ackno[4]; };
struct tcp_conn_s
{
  uint8_t sndseq[4], flags, tcpstateflags, timer;
  uint32_t last_ackno, dupacks, fr_recover, cwnd, ssthresh;
  uint32_t snd_wnd, tx_unacked;
  /* Only the reverse-patch counterfactual uses this retired private field. */
  uint32_t max_cwnd;
  uint16_t mss;
};

static uint32_t tcp_getsequence(const uint8_t value[4])
{
  return (uint32_t)value[0] << 24 | (uint32_t)value[1] << 16 |
         (uint32_t)value[2] << 8 | value[3];
}

static void setsequence(uint8_t value[4], uint32_t sequence)
{
  for (unsigned index = 0; index < 4; index++)
    {
      value[3 - index] = sequence >> (8 * index);
    }
}

/* NATIVE_FUNCTIONS */

static void rto_policy(struct tcp_conn_s *conn)
{
  /* NATIVE_RTO_POLICY */
}

int main(void)
{
  const uint32_t bases[] = {100000, UINT32_MAX - 100000};
  for (unsigned index = 0; index < 2; index++)
    {
      struct tcp_conn_s conn = {0};
      struct tcp_hdr_s tcp = {0};
      conn.mss = TCP_IPV4_DEFAULT_MSS;
      conn.snd_wnd = 65535;
      conn.tcpstateflags = TCP_ESTABLISHED;
      conn.timer = 3;
      setsequence(tcp.ackno, bases[index]);
      tcp_cc_init(&conn);
      tcp_cc_update(&conn, &tcp);
      assert(conn.cwnd == 3 * conn.mss);

      /* Repeated congestion still resets to one SMSS. Successful future
       * traffic must be able to recover, not retain a historical ceiling.
       */
      for (unsigned retry = 0; retry < 3; retry++)
        {
          conn.cwnd = 4 * conn.mss;
          conn.tx_unacked = 8 * conn.mss;
          rto_policy(&conn);
          assert(conn.cwnd == conn.mss);
          assert(conn.ssthresh == 4 * conn.mss);
        }

      for (unsigned ack = 1; ack <= 400; ack++)
        {
          uint32_t edge = bases[index] + ack * conn.mss;
          setsequence(tcp.ackno, edge);
          setsequence(conn.sndseq, edge + 8 * conn.mss);
          tcp_cc_recv_ack(&conn, &tcp);
        }

      if (conn.cwnd <= 16384)
        {
          fprintf(stderr, "congestion window cannot recover: %" PRIu32 "\n", conn.cwnd);
          return 1;
        }
    }
  return 0;
}

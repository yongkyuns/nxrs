/* SPDX-License-Identifier: MIT */
/* Host stand-ins exercise actual collector/parser and patched BDC extraction;
 * this is bounds/lifecycle evidence, not a native-ABI or radio witness.
 */
#include <assert.h>
#include <string.h>
#include "radio-stats.c"

struct bcmf_dev_s { struct net_driver_s bc_dev; };
struct bcmf_bdc_header { uint8_t flags, priority, flags2, data_offset; };
#include "bcmf-trace-function.h"

static struct bcmf_dev_s device;
static const uint8_t peer[] = {192, 0, 2, 1};
static const uint8_t local[] = {192, 0, 2, 2};
static int locked;
static int critical;
static uint32_t ticks;
uint32_t clock_systime_ticks(void) { return ticks; }

int net_lock(void) { assert(!locked); locked = 1; return 0; }
void net_unlock(void) { assert(locked); locked = 0; }
struct net_driver_s *netdev_findbyname(const char *name)
{
  assert(locked);
  return strcmp(name, "wlan0") == 0 ? &device.bc_dev : NULL;
}
irqstate_t enter_critical_section(void) { return critical++; }
void leave_critical_section(irqstate_t flags) { assert(--critical == flags); }

int bcmf_trace_counters(struct net_driver_s *dev, struct bcmf_trace_counts_s *out)
{
  assert(locked && dev == &device.bc_dev);
  *out = (struct bcmf_trace_counts_s){.packet_result = -5, .retry_result = -22,
                                     .version = 8, .length = 1000,
                                     .tx_good_pkt = 3, .txretry = 4,
                                     .rxframe = 11, .rxerror = 12, .rxnobuf = 13,
                                     .rxoflo = 14, .rxfilter = 15, .rxbadproto = 16,
                                     .rxfragerr = 17, .reset = 18, .phy_valid = 1,
                                     .rxbadfcs = 31, .rxbadplcp = 32,
                                     .rxcrsglitch = 33, .rxfrmtoolong = 34,
                                     .rxfrmtooshrt = 35, .rxinvmachdr = 36,
                                     .rxstrt = 37};
  return 0;
}

static void be16(uint8_t *at, unsigned int value)
{
  at[0] = value >> 8;
  at[1] = value;
}

static void checksums(uint8_t *data)
{
  uint8_t *ip = data + 14;
  size_t ihl = (ip[0] & 15) * 4;
  size_t udp_len = trace_be16(ip + 2) - ihl;
  uint8_t *udp = ip + ihl;
  uint32_t sum = 17 + udp_len;
  /* Fixture writer, separate from the collector's trace_sum implementation. */
  be16(ip + 10, 0);
  be16(udp + 6, 0);
  for (size_t i = 12; i < 20; i += 2) sum += trace_be16(ip + i);
  for (size_t i = 0; i < udp_len; i += 2) sum += trace_be16(udp + i);
  sum = (sum & 0xffff) + (sum >> 16);
  sum = (sum & 0xffff) + (sum >> 16);
  be16(udp + 6, sum == 0xffff ? 0xffff : (uint16_t)~sum);
  sum = 0;
  for (size_t i = 0; i < ihl; i += 2) sum += trace_be16(ip + i);
  sum = (sum & 0xffff) + (sum >> 16);
  sum = (sum & 0xffff) + (sum >> 16);
  be16(ip + 10, (uint16_t)~sum);
}

static size_t packet(uint8_t *data, uint32_t sequence, int transmit)
{
  const size_t sizes[] = {16, 64, 256, 512};
  size_t size = sizes[sequence % 4];
  uint8_t *ip = data + 14;
  uint8_t *udp = ip + 20;
  uint8_t *body = udp + 8;
  memset(data, 0, 14 + 20 + 8 + size);
  be16(data + 12, 0x0800);
  ip[0] = 0x45;
  ip[9] = 17;
  be16(ip + 2, 20 + 8 + size);
  be16(ip + 6, 0x4000); /* DF is allowed, fragments are not. */
  memcpy(ip + (transmit ? 16 : 12), peer, 4);
  memcpy(ip + (transmit ? 12 : 16), local, 4);
  be16(udp + (transmit ? 2 : 0), 4444);
  be16(udp + (transmit ? 0 : 2), 5555);
  be16(udp + 4, 8 + size);
  body[0] = sequence >> 24;
  body[1] = sequence >> 16;
  body[2] = sequence >> 8;
  body[3] = sequence;
  for (size_t i = 4; i < size; i++) body[i] = (uint8_t)(sequence + i - 4);
  checksums(data);
  return 14 + 20 + 8 + size;
}

int main(void)
{
  uint32_t values[32];
  uint8_t data[600];
  size_t len;
  struct bcmf_trace_bus_s bus = {.sequence = 9, .credit = 12, .cached_ready = 1};
  memcpy(&device.bc_dev.d_ipaddr, local, sizeof(local));
  /* Independent checksum vector; odd length and carry folding too. */
  const uint8_t vector[] = {0x00, 0x01, 0xf2, 0x03, 0xf4, 0xf5, 0xf6, 0xf7};
  assert(trace_sum(vector, sizeof(vector), 0) == 0xddf2);
  assert(trace_sum(vector, 1, 0) == 0);
  assert(trace_sum(vector + 2, 1, 0) == 0xf200);
  assert(nxrs_radio_trace_start("absent", peer, 4444) == -ENODEV);
  assert(!locked && !critical);
  assert(nxrs_radio_trace_start("wlan0", peer, 4444) == 0);
  assert(nxrs_radio_daemon_snapshot(NULL, 19) == -EINVAL);
  assert(nxrs_radio_daemon_snapshot(values, 18) == -EINVAL);
  bcmf_trace_daemon(NULL, BCMF_TRACE_DAEMON_IRQ, 0, 0, 0);
  bcmf_trace_daemon(&device.bc_dev, (enum bcmf_trace_daemon_e)-1, 0, 0, 0);
  assert(nxrs_radio_daemon_snapshot(values, 19) == 0);
  for (int i = 2; i < 19; i++) assert(values[i] == 0);
  ticks = UINT32_MAX;
  bcmf_trace_daemon(&device.bc_dev, BCMF_TRACE_DAEMON_IRQ, 0, 0, 0);
  bcmf_trace_daemon(&device.bc_dev, BCMF_TRACE_DAEMON_WAIT, 0, 0, 0);
  ticks = 2;
  bcmf_trace_daemon(&device.bc_dev, BCMF_TRACE_DAEMON_POLL, 0,
                     BCMF_TRACE_STATUS_PACKET_AVAILABLE, 64);
  assert(nxrs_radio_daemon_snapshot(values, 19) == 0);
  assert(values[0] == 2 && values[1] == 10000000);
  assert(values[2] == 1 && values[3] == 1 && values[4] == UINT32_MAX);
  assert(values[5] == 1 && values[6] == 0 && values[7] == 1 && values[8] == 2);
  assert(values[9] == 0 && values[10] == 32 && values[11] == 64);
  assert(values[12] == 1 && values[13] == 1 && values[14] == 1 && values[15] == 0);
  assert(values[18] == 1);
  bcmf_trace_daemon(&device.bc_dev, BCMF_TRACE_DAEMON_POLL, -EIO, 32, 64);
  bcmf_trace_daemon(&device.bc_dev, BCMF_TRACE_DAEMON_WAKE, 0, 0, 0);
  assert(nxrs_radio_daemon_snapshot(values, 19) == 0);
  assert(values[5] == 2 && values[6] == 1 && (int32_t)values[9] == -EIO);
  assert(values[10] == 0 && values[11] == 0 && values[13] == 0);
  assert(values[15] == 1 && values[16] == 1 && values[17] == 2 && values[18] == 1);
  assert(nxrs_radio_rx_snapshot(NULL, 16) == -EINVAL);
  assert(nxrs_radio_rx_snapshot(values, 15) == -EINVAL);
  bcmf_trace_rx(NULL, BCMF_TRACE_RX_ATTEMPT);
  struct net_driver_s unrelated = {0};
  bcmf_trace_rx(&unrelated, BCMF_TRACE_RX_ATTEMPT);
  bcmf_trace_rx(&device.bc_dev, (enum bcmf_trace_rx_e)-1);
  bcmf_trace_rx(&device.bc_dev, BCMF_TRACE_RX_COUNT);
  assert(nxrs_radio_rx_snapshot(values, 16) == 0);
  for (int i = 0; i < 16; i++) assert(values[i] == 0);
  for (int i = 0; i < BCMF_TRACE_RX_COUNT; i++)
    bcmf_trace_rx(&device.bc_dev, (enum bcmf_trace_rx_e)i);
  assert(nxrs_radio_rx_snapshot(values, 16) == 0);
  for (int i = 0; i < 16; i++) assert(values[i] == 1);
  g_trace.rx_counts[BCMF_TRACE_RX_ATTEMPT] = UINT32_MAX;
  bcmf_trace_rx(&device.bc_dev, BCMF_TRACE_RX_ATTEMPT);
  assert(nxrs_radio_rx_snapshot(values, 16) == 0 && values[0] == 0);
  for (uint32_t seq = 0; seq < 10; seq++)
    {
      bus.ticket = 100 + seq;
      for (int stage = 0; stage < BCMF_TRACE_STAGE_COUNT; stage++)
        {
          len = packet(data, seq, stage <= BCMF_TRACE_BUS_TX);
          bcmf_trace_frame(&device.bc_dev, stage, data, len,
                           stage == BCMF_TRACE_BUS_TX ? -5 : 0,
                           stage == BCMF_TRACE_BUS_TX ? &bus : NULL);
          if (stage == BCMF_TRACE_BUS_TX)
            bcmf_trace_status(&device.bc_dev, bus.ticket, 0,
                               BCMF_TRACE_STATUS_READY | BCMF_TRACE_STATUS_PACKET_AVAILABLE);
        }
      assert(nxrs_radio_trace_snapshot(seq, values, 25) == 0);
      assert(values[0] == 1 && values[1] == 15 && (int32_t)values[2] == -5);
      for (int i = 8; i < 13; i++) assert(values[i] == 0);
      assert(values[13] == 1 && values[14] == 9 && values[15] == 12 && values[16] == 1);
      assert(values[17] == 1 && values[18] == 0 && values[19] == 1 && values[24] == 1);
      for (int i = 20; i < 24; i++) assert(values[i] == 0);
    }
  assert(values[7] == 2);
  for (int i = 3; i < 7; i++) assert(values[i] == 10);
  assert(nxrs_radio_trace_snapshot(0, values, 25) == 0 && values[0] == 0);
  for (int i = 8; i < 25; i++) assert(values[i] == 0);

  /* Delayed duplicate must not evict a newer record in the same slot. */
  len = packet(data, 0, 0);
  bcmf_trace_frame(&device.bc_dev, BCMF_TRACE_NET_RX, data, len, 0, NULL);
  assert(nxrs_radio_trace_snapshot(8, values, 25) == 0 && values[1] == 15);

  /* Every truncation and representative malformed/private packet is ignored. */
  len = packet(data, 11, 0);
  for (size_t short_len = 0; short_len < len; short_len++)
    bcmf_trace_frame(&device.bc_dev, BCMF_TRACE_NET_RX, data, short_len, 0, NULL);
  bcmf_trace_frame(NULL, BCMF_TRACE_NET_RX, data, len, 0, NULL);
  bcmf_trace_frame(&device.bc_dev, BCMF_TRACE_STAGE_COUNT, data, len, 0, NULL);
  bcmf_trace_frame(&device.bc_dev, (enum bcmf_trace_stage_e)-1, data, len, 0, NULL);
  const size_t offsets[] = {12, 14, 20, 23, 26, 34, 38, 46};
  for (size_t i = 0; i < sizeof(offsets) / sizeof(offsets[0]); i++)
    {
      uint8_t saved = data[offsets[i]];
      data[offsets[i]] ^= 0xff;
      bcmf_trace_frame(&device.bc_dev, BCMF_TRACE_NET_RX, data, len, 0, NULL);
      data[offsets[i]] = saved;
    }
  assert(nxrs_radio_trace_snapshot(11, values, 25) == 0 && values[0] == 0);

  /* The actual patched helper strips BDC extension words, not packet bytes. */
  memmove(data + 8, data, len);
  memset(data, 0, 8);
  data[3] = 1;
  bcmf_bdc_trace_frame(&device, BCMF_TRACE_BUS_RX, data, len + 8, 0, NULL);
  assert(nxrs_radio_trace_snapshot(11, values, 25) == 0 && values[1] == 4);
  for (size_t short_len = 0; short_len < 8; short_len++)
    bcmf_bdc_trace_frame(&device, BCMF_TRACE_BUS_RX, data, short_len, 0, NULL);

  /* Broken headers remain visible, and duplicates cannot erase bad evidence. */
  len = packet(data, 12, 1);
  data[14 + 10] ^= 1;
  data[14 + 20 + 6] ^= 1;
  bcmf_trace_frame(&device.bc_dev, BCMF_TRACE_NET_TX, data, len, 0, NULL);
  assert(nxrs_radio_trace_snapshot(12, values, 25) == 0);
  assert(values[8] == 1 && values[9] == 1 && values[10] == 0);
  len = packet(data, 12, 1);
  bcmf_trace_frame(&device.bc_dev, BCMF_TRACE_NET_TX, data, len, 0, NULL);
  assert(nxrs_radio_trace_snapshot(12, values, 25) == 0 && values[8] == 1 && values[9] == 1);
  be16(data + 14 + 20 + 6, 0);
  bcmf_trace_frame(&device.bc_dev, BCMF_TRACE_BUS_TX, data, len, 0, NULL);
  assert(nxrs_radio_trace_snapshot(12, values, 25) == 0 && values[10] == 2);

  len = packet(data, 13, 0);
  data[14 + 16] ^= 1; /* Wrong local destination, but valid checksums. */
  be16(data + 14 + 20 + 2, 5556);
  checksums(data);
  bcmf_trace_frame(&device.bc_dev, BCMF_TRACE_NET_RX, data, len, 0, NULL);
  assert(nxrs_radio_trace_snapshot(13, values, 25) == 0);
  assert(values[8] == 0 && values[9] == 0 && values[11] == 8 && values[12] == 8);

  len = packet(data, 14, 1);
  bus = (struct bcmf_trace_bus_s){.sequence = 255, .credit = 255, .ticket = 999};
  bcmf_trace_frame(&device.bc_dev, BCMF_TRACE_BUS_TX, data, len, 0, &bus);
  assert(nxrs_radio_trace_snapshot(14, values, 25) == 0);
  assert(values[13] == 1 && values[14] == 255 && values[15] == 255 && values[16] == 0);
  bcmf_trace_status(NULL, 999, 0, UINT32_MAX);
  bcmf_trace_status(&device.bc_dev, 1000, 0, UINT32_MAX);
  assert(nxrs_radio_trace_snapshot(14, values, 25) == 0 && values[17] == 0);
  bcmf_trace_status(&device.bc_dev, 999, -5, UINT32_MAX);
  bcmf_trace_status(&device.bc_dev, 999, 0, BCMF_TRACE_STATUS_READY);
  assert(nxrs_radio_trace_snapshot(14, values, 25) == 0 && values[17] == 1 && (int32_t)values[18] == -5);
  for (int i = 19; i < 25; i++) assert(values[i] == 0);

  assert(nxrs_radio_controller_snapshot("absent", values, 30) == -ENODEV);
  assert(nxrs_radio_controller_snapshot(NULL, values, 30) == -EINVAL);
  assert(nxrs_radio_controller_snapshot("wlan0", values, 29) == -EINVAL);
  assert(nxrs_radio_controller_snapshot("wlan0", values, 30) == 0);
  for (int i = 14; i < 22; i++) assert(values[i] == (uint32_t)i - 3);
  assert(values[22] == 1);
  for (int i = 23; i < 30; i++) assert(values[i] == (uint32_t)i + 8);
  assert((int32_t)values[0] == -5 && (int32_t)values[1] == -22);
  assert(values[2] == 8 && values[3] == 1000 && values[6] == 3 && values[11] == 4);

  assert(nxrs_radio_trace_start("wlan0", peer, 4444) == 0);
  assert(nxrs_radio_trace_snapshot(11, values, 25) == 0 && values[0] == 0);
  nxrs_radio_trace_stop();
  values[0] = 123;
  bcmf_trace_daemon(&device.bc_dev, BCMF_TRACE_DAEMON_IRQ, 0, 0, 0);
  assert(nxrs_radio_daemon_snapshot(values, 19) == -ENOSYS && values[0] == 123);
  values[0] = 123;
  bcmf_trace_rx(&device.bc_dev, BCMF_TRACE_RX_ATTEMPT);
  assert(nxrs_radio_rx_snapshot(values, 16) == -ENOSYS && values[0] == 123);
  bcmf_trace_status(&device.bc_dev, 999, 0, UINT32_MAX);
  values[0] = 123;
  assert(nxrs_radio_trace_snapshot(11, values, 25) == -ENOSYS && values[0] == 123);
  assert(!locked && !critical);
  return 0;
}

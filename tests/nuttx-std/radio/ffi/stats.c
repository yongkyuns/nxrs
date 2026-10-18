/* SPDX-License-Identifier: MIT */
/* Numeric diagnostic only: no addresses, credentials or packet contents. */
#include <nuttx/config.h>
#include <errno.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#if (defined(CONFIG_NET_STATISTICS) && defined(CONFIG_NETDEV_STATISTICS)) || \
    (defined(CONFIG_NET) && defined(CONFIG_NET_IPv4))
#include <nuttx/net/net.h>
#include <nuttx/net/netdev.h>
#endif

#if defined(CONFIG_NET) && defined(CONFIG_NET_IPv4)
#include <net/if.h>
#endif

#if defined(CONFIG_NET_STATISTICS) && defined(CONFIG_NETDEV_STATISTICS)
#include <nuttx/net/netstats.h>

_Static_assert(sizeof(net_stats_t) <= sizeof(uint32_t), "counter width exceeds bridge");
#endif

int nxrs_radio_net_snapshot(const char *interface, uint32_t *values, size_t count)
{
  if (interface == NULL || values == NULL || count != 12)
    {
      return -EINVAL;
    }

#if defined(CONFIG_NET_STATISTICS) && defined(CONFIG_NETDEV_STATISTICS)
  struct net_driver_s *device;
  int result = net_lock();
  if (result < 0)
    {
      return result;
    }

  device = netdev_findbyname(interface);
  if (device == NULL)
    {
      net_unlock();
      return -ENODEV;
    }

  values[0] = 8 * sizeof(net_stats_t);
  values[1] = g_netstats.udp.sent;
  values[2] = g_netstats.udp.recv;
  values[3] = g_netstats.udp.drop;
  values[4] = g_netstats.udp.chkerr;
  values[5] = g_netstats.tcp.rexmit;
  values[6] = g_netstats.tcp.drop;
  values[7] = g_netstats.tcp.chkerr;
  values[8] = g_netstats.ipv4.drop;
  values[9] = device->d_statistics.tx_packets;
  values[10] = device->d_statistics.rx_ipv4;
  values[11] = device->d_statistics.rx_dropped;
  net_unlock();
  return 0;
#else
  return -ENOSYS;
#endif
}

int nxrs_radio_interface_snapshot(const char *interface, uint32_t *values,
                                  size_t count)
{
  if (interface == NULL || values == NULL || count != 3)
    {
      return -EINVAL;
    }

#if defined(CONFIG_NET) && defined(CONFIG_NET_IPv4)
  struct net_driver_s *device;
  int result = net_lock();
  if (result < 0)
    {
      return result;
    }

  device = netdev_findbyname(interface);
  if (device == NULL)
    {
      net_unlock();
      return -ENODEV;
    }

  values[0] = IFF_IS_UP(device->d_flags);
  values[1] = IFF_IS_RUNNING(device->d_flags);
  values[2] = device->d_ipaddr;
  net_unlock();
  return 0;
#else
  return -ENOSYS;
#endif
}

#ifdef CONFIG_IEEE80211_BROADCOM_FRAME_TRACE
#include <nuttx/clock.h>
#include <nuttx/irq.h>
#include <nuttx/wireless/ieee80211/bcmf_trace.h>
_Static_assert(BCMF_TRACE_STAGE_COUNT == 4, "trace bridge stage count changed");
_Static_assert(BCMF_TRACE_RX_COUNT == 16, "receive bridge event count changed");
_Static_assert(sizeof(int) <= sizeof(uint32_t), "trace result exceeds bridge");

/* One request is in flight. Eight slots retain nearby completed sequences so
 * a failure can also check the preceding packet as a trace-path control.
 */
struct trace_record
{
  uint32_t sequence;
  uint32_t stages;
  int bus_result;
  /* Stage masks remember any invalid capture, including duplicates. */
  uint32_t bad_ip_checksum;
  uint32_t bad_udp_checksum;
  uint32_t omitted_udp_checksum;
  uint32_t bad_local_ip;
  uint32_t changed_local_port;
  uint32_t bus_known;
  uint32_t bus_sequence;
  uint32_t bus_credit;
  uint32_t bus_cached_ready;
  uint32_t bus_ticket;
  uint32_t post_known;
  int post_result;
  uint32_t post_flags;
};

struct daemon_record
{
  uint32_t irqs, irq_known, irq_tick;
  uint32_t polls, poll_errors, poll_known, poll_tick, poll_result;
  uint32_t poll_flags, poll_length;
  uint32_t wait_known, waiting, waits, wakes, wake_known, wake_tick;
  uint32_t available_reads;
};
_Static_assert(sizeof(struct daemon_record) == 17 * sizeof(uint32_t),
               "daemon bridge layout changed");

static struct
{
  struct net_driver_s *device;
  uint8_t peer[4];
  uint8_t local[4];
  uint16_t port;
  uint16_t local_port;
  uint32_t epoch;
  uint32_t counts[BCMF_TRACE_STAGE_COUNT];
  uint32_t rx_counts[BCMF_TRACE_RX_COUNT];
  struct daemon_record daemon;
  uint32_t evictions;
  struct trace_record records[8];
} g_trace;

static uint16_t trace_be16(const uint8_t *data)
{
  return (uint16_t)data[0] << 8 | data[1];
}

/* Network-order one's-complement sum; no aligned loads or packet mutation. */
static uint32_t trace_sum(const uint8_t *data, size_t length, uint32_t sum)
{
  while (length >= 2)
    {
      sum += trace_be16(data);
      data += 2;
      length -= 2;
    }

  if (length != 0)
    {
      sum += (uint32_t)data[0] << 8;
    }

  while (sum >> 16)
    {
      sum = (sum & 0xffff) + (sum >> 16);
    }

  return sum;
}

static int trace_sequence(const uint8_t *data, size_t length, int transmit,
                          const uint8_t peer[4], const uint8_t local[4],
                          uint16_t port, uint32_t *sequence,
                          uint32_t *checks, uint16_t *local_port)
{
  static const size_t sizes[] = {16, 64, 256, 512};
  const uint8_t *ip;
  const uint8_t *udp;
  const uint8_t *payload;
  size_t header;
  size_t total;
  size_t size;
  size_t i;

  if (data == NULL || length < 14 + 20 + 8 + 16 ||
      trace_be16(data + 12) != 0x0800)
    {
      return 0;
    }

  ip = data + 14;
  header = (ip[0] & 15) * 4;
  total = trace_be16(ip + 2);
  if ((ip[0] >> 4) != 4 || header < 20 || total > length - 14 ||
      total < header + 8 + 16 || ip[9] != 17 ||
      (trace_be16(ip + 6) & 0x3fff) != 0 ||
      memcmp(ip + (transmit ? 16 : 12), peer, 4) != 0)
    {
      return 0;
    }

  udp = ip + header;
  if (trace_be16(udp + (transmit ? 2 : 0)) != port ||
      trace_be16(udp + 4) != total - header)
    {
      return 0;
    }

  payload = udp + 8;
  size = total - header - 8;
  *sequence = (uint32_t)payload[0] << 24 | (uint32_t)payload[1] << 16 |
              (uint32_t)payload[2] << 8 | payload[3];
  if (size != sizes[*sequence % 4])
    {
      return 0;
    }

  for (i = 4; i < size; i++)
    {
      if (payload[i] != (uint8_t)(i - 4 + *sequence))
        {
          return 0;
        }
    }

  /* Invalid checksums are recorded, not filtered out of the loss trace.
   * IPv4 permits an omitted UDP checksum; that is not a verified checksum.
   */
  *checks = trace_sum(ip, header, 0) != 0xffff ? 1 : 0;
  if (trace_be16(udp + 6) == 0)
    {
      *checks |= 4;
    }
  else if (trace_sum(udp, total - header,
                     trace_sum(ip + 12, 8, 17 + total - header)) != 0xffff)
    {
      *checks |= 2;
    }

  if (memcmp(ip + (transmit ? 12 : 16), local, 4) != 0)
    {
      *checks |= 8;
    }

  *local_port = trace_be16(udp + (transmit ? 0 : 2));
  return 1;
}

void bcmf_trace_frame(struct net_driver_s *device, enum bcmf_trace_stage_e stage,
                      const uint8_t *data, size_t length, int result,
                      const struct bcmf_trace_bus_s *bus)
{
  uint8_t peer[4];
  uint8_t local[4];
  uint16_t port;
  uint16_t local_port;
  uint32_t epoch;
  uint32_t sequence;
  uint32_t checks;
  struct trace_record *record;
  irqstate_t flags;

  if ((unsigned int)stage >= BCMF_TRACE_STAGE_COUNT)
    {
      return;
    }

  flags = enter_critical_section();
  port = device == g_trace.device ? g_trace.port : 0;
  epoch = g_trace.epoch;
  memcpy(peer, g_trace.peer, sizeof(peer));
  memcpy(local, g_trace.local, sizeof(local));
  leave_critical_section(flags);
  if (port == 0 || !trace_sequence(data, length, stage <= BCMF_TRACE_BUS_TX,
                                  peer, local, port, &sequence, &checks,
                                  &local_port))
    {
      return;
    }

  flags = enter_critical_section();
  if (epoch == g_trace.epoch && g_trace.device == device)
    {
      g_trace.counts[stage]++;
      /* Compare to the first observed TX port, not an asserted socket binding. */
      if (stage == BCMF_TRACE_NET_TX && g_trace.local_port == 0)
        {
          g_trace.local_port = local_port;
        }

      record = &g_trace.records[sequence % 8];
      if (record->stages == 0 || sequence >= record->sequence)
        {
          if (record->stages != 0 && sequence != record->sequence)
            {
              g_trace.evictions++;
            }

          if (record->stages == 0 || sequence != record->sequence)
            {
              *record = (struct trace_record){.sequence = sequence};
            }

          record->stages |= 1u << stage;
          record->bad_ip_checksum |= (checks & 1) ? 1u << stage : 0;
          record->bad_udp_checksum |= (checks & 2) ? 1u << stage : 0;
          record->omitted_udp_checksum |= (checks & 4) ? 1u << stage : 0;
          record->bad_local_ip |= (checks & 8) ? 1u << stage : 0;
          if (local_port == 0 ||
              (g_trace.local_port != 0 && local_port != g_trace.local_port))
            {
              record->changed_local_port |= 1u << stage;
            }

          if (stage == BCMF_TRACE_BUS_TX)
            {
              record->bus_result = result;
              if (bus != NULL)
                {
                  record->bus_known = 1;
                  record->bus_sequence = bus->sequence;
                  record->bus_credit = bus->credit;
                  record->bus_cached_ready = !!bus->cached_ready;
                  record->bus_ticket = bus->ticket;
                }
            }
        }
    }

  leave_critical_section(flags);
}

void bcmf_trace_status(struct net_driver_s *device, uint32_t ticket,
                       int result, uint32_t status)
{
  irqstate_t flags = enter_critical_section();
  if (device != NULL && device == g_trace.device)
    {
      for (size_t i = 0; i < sizeof(g_trace.records) / sizeof(g_trace.records[0]); i++)
        {
          struct trace_record *record = &g_trace.records[i];
          if (record->bus_known && record->bus_ticket == ticket &&
              !record->post_known)
            {
              record->post_known = 1;
              record->post_result = result;
              record->post_flags = result == 0 ? status : 0;
              break;
            }
        }
    }

  leave_critical_section(flags);
}

void bcmf_trace_rx(struct net_driver_s *device, enum bcmf_trace_rx_e event)
{
  irqstate_t flags = enter_critical_section();
  if (device != NULL && device == g_trace.device &&
      (unsigned int)event < BCMF_TRACE_RX_COUNT)
    {
      g_trace.rx_counts[event]++;
    }

  leave_critical_section(flags);
}

void bcmf_trace_daemon(struct net_driver_s *device,
                       enum bcmf_trace_daemon_e event, int result,
                       uint32_t status, uint32_t length)
{
  irqstate_t flags = enter_critical_section();
  if (device != NULL && device == g_trace.device)
    {
      struct daemon_record *record = &g_trace.daemon;
      uint32_t now = (uint32_t)clock_systime_ticks();
      switch (event)
        {
          case BCMF_TRACE_DAEMON_IRQ:
            record->irqs++;
            record->irq_known = 1;
            record->irq_tick = now;
            break;
          case BCMF_TRACE_DAEMON_POLL:
            record->polls++;
            record->poll_errors += result != 0;
            record->poll_known = 1;
            record->poll_tick = now;
            record->poll_result = (uint32_t)result;
            record->poll_flags = result == 0 ? status : 0;
            record->poll_length = result == 0 ? length : 0;
            record->available_reads += result == 0 &&
              (status & BCMF_TRACE_STATUS_PACKET_AVAILABLE) != 0;
            break;
          case BCMF_TRACE_DAEMON_WAIT:
            record->wait_known = 1;
            record->waiting = 1;
            record->waits++;
            break;
          case BCMF_TRACE_DAEMON_WAKE:
            record->wait_known = 1;
            record->waiting = 0;
            record->wakes++;
            record->wake_known = 1;
            record->wake_tick = now;
            break;
          default:
            break;
        }
    }

  leave_critical_section(flags);
}
#endif

int nxrs_radio_daemon_snapshot(uint32_t *values, size_t count)
{
  if (values == NULL || count != 19)
    {
      return -EINVAL;
    }

#ifdef CONFIG_IEEE80211_BROADCOM_FRAME_TRACE
  irqstate_t flags = enter_critical_section();
  if (g_trace.device == NULL)
    {
      leave_critical_section(flags);
      return -ENOSYS;
    }

  values[0] = (uint32_t)clock_systime_ticks();
  values[1] = NSEC_PER_TICK;
  memcpy(values + 2, &g_trace.daemon, sizeof(g_trace.daemon));
  leave_critical_section(flags);
  return 0;
#else
  return -ENOSYS;
#endif
}

int nxrs_radio_rx_snapshot(uint32_t *values, size_t count)
{
  if (values == NULL || count != 16)
    {
      return -EINVAL;
    }

#ifdef CONFIG_IEEE80211_BROADCOM_FRAME_TRACE
  irqstate_t flags = enter_critical_section();
  if (g_trace.device == NULL)
    {
      leave_critical_section(flags);
      return -ENOSYS;
    }

  memcpy(values, g_trace.rx_counts, sizeof(g_trace.rx_counts));
  leave_critical_section(flags);
  return 0;
#else
  return -ENOSYS;
#endif
}

int nxrs_radio_trace_start(const char *interface, const uint8_t *peer,
                            uint16_t port)
{
  if (interface == NULL || peer == NULL || port == 0)
    {
      return -EINVAL;
    }

#ifdef CONFIG_IEEE80211_BROADCOM_FRAME_TRACE
  struct net_driver_s *device;
  uint32_t epoch;
  irqstate_t flags;
  int result = net_lock();
  if (result < 0)
    {
      return result;
    }

  device = netdev_findbyname(interface);
  if (device == NULL)
    {
      net_unlock();
      return -ENODEV;
    }

  flags = enter_critical_section();
  epoch = g_trace.epoch + 1;
  memset(&g_trace, 0, sizeof(g_trace));
  g_trace.device = device;
  memcpy(g_trace.peer, peer, sizeof(g_trace.peer));
  _Static_assert(sizeof(device->d_ipaddr) == 4, "IPv4 address width changed");
  /* NuttX stores in_addr_t in network byte order, just like its IP header. */
  memcpy(g_trace.local, &device->d_ipaddr, sizeof(g_trace.local));
  g_trace.port = port;
  g_trace.epoch = epoch;
  leave_critical_section(flags);
  net_unlock();
  return 0;
#else
  return -ENOSYS;
#endif
}

void nxrs_radio_trace_stop(void)
{
#ifdef CONFIG_IEEE80211_BROADCOM_FRAME_TRACE
  irqstate_t flags = enter_critical_section();
  g_trace.device = NULL;
  g_trace.port = 0;
  g_trace.epoch++;
  leave_critical_section(flags);
#endif
}

int nxrs_radio_trace_snapshot(uint32_t sequence, uint32_t *values, size_t count)
{
  if (values == NULL || count != 25)
    {
      return -EINVAL;
    }

#ifdef CONFIG_IEEE80211_BROADCOM_FRAME_TRACE
  const struct trace_record *record;
  irqstate_t flags = enter_critical_section();
  if (g_trace.device == NULL)
    {
      leave_critical_section(flags);
      return -ENOSYS;
    }

  record = &g_trace.records[sequence % 8];
  values[0] = record->stages != 0 && record->sequence == sequence;
  values[1] = values[0] ? record->stages : 0;
  values[2] = values[0] ? (uint32_t)record->bus_result : 0;
  memcpy(values + 3, g_trace.counts, sizeof(g_trace.counts));
  values[7] = g_trace.evictions;
  values[8] = values[0] ? record->bad_ip_checksum : 0;
  values[9] = values[0] ? record->bad_udp_checksum : 0;
  values[10] = values[0] ? record->omitted_udp_checksum : 0;
  values[11] = values[0] ? record->bad_local_ip : 0;
  values[12] = values[0] ? record->changed_local_port : 0;
  values[13] = values[0] ? record->bus_known : 0;
  values[14] = values[0] ? record->bus_sequence : 0;
  values[15] = values[0] ? record->bus_credit : 0;
  values[16] = values[0] ? record->bus_cached_ready : 0;
  values[17] = values[0] ? record->post_known : 0;
  values[18] = values[0] ? (uint32_t)record->post_result : 0;
  uint32_t status = values[0] ? record->post_flags : 0;
  values[19] = !!(status & BCMF_TRACE_STATUS_READY);
  values[20] = !!(status & BCMF_TRACE_STATUS_DATA_MISSING);
  values[21] = !!(status & BCMF_TRACE_STATUS_UNDERFLOW);
  values[22] = !!(status & BCMF_TRACE_STATUS_OVERFLOW);
  values[23] = !!(status & BCMF_TRACE_STATUS_COMMAND_ERROR);
  values[24] = !!(status & BCMF_TRACE_STATUS_PACKET_AVAILABLE);
  leave_critical_section(flags);
  return 0;
#else
  (void)sequence;
  return -ENOSYS;
#endif
}

int nxrs_radio_controller_snapshot(const char *interface, uint32_t *values,
                                   size_t count)
{
  if (interface == NULL || values == NULL || count != 30)
    {
      return -EINVAL;
    }

#ifdef CONFIG_IEEE80211_BROADCOM_FRAME_TRACE
  struct bcmf_trace_counts_s counters;
  struct net_driver_s *device;
  int result = net_lock();
  if (result < 0)
    {
      return result;
    }

  device = netdev_findbyname(interface);
  result = device == NULL ? -ENODEV : bcmf_trace_counters(device, &counters);
  net_unlock();
  if (result < 0)
    {
      return result;
    }

  values[0] = (uint32_t)counters.packet_result;
  values[1] = (uint32_t)counters.retry_result;
  values[2] = counters.version;
  values[3] = counters.length;
  values[4] = counters.rx_good_pkt;
  values[5] = counters.rx_bad_pkt;
  values[6] = counters.tx_good_pkt;
  values[7] = counters.tx_bad_pkt;
  values[8] = counters.rx_ocast_good_pkt;
  values[9] = counters.txframe;
  values[10] = counters.txretrans;
  values[11] = counters.txretry;
  values[12] = counters.txretrie;
  values[13] = counters.txfail;
  values[14] = counters.rxframe;
  values[15] = counters.rxerror;
  values[16] = counters.rxnobuf;
  values[17] = counters.rxoflo;
  values[18] = counters.rxfilter;
  values[19] = counters.rxbadproto;
  values[20] = counters.rxfragerr;
  values[21] = counters.reset;
  values[22] = counters.phy_valid;
  values[23] = counters.rxbadfcs;
  values[24] = counters.rxbadplcp;
  values[25] = counters.rxcrsglitch;
  values[26] = counters.rxfrmtoolong;
  values[27] = counters.rxfrmtooshrt;
  values[28] = counters.rxinvmachdr;
  values[29] = counters.rxstrt;
  return 0;
#else
  return -ENOSYS;
#endif
}

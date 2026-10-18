/* SPDX-License-Identifier: MIT */
/* Actual native vendor types + driver functions; mock transport, not ABI proof. */
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#define FAR
#define OK 0
#define begin_packed_struct
#define end_packed_struct __attribute__((packed))
/* These tests run on a little-endian host; target builds use native endian.h. */
#define le16toh(x) ((uint16_t)(x))
#define le32toh(x) ((uint32_t)(x))
#include "bcmf_ioctl.h"
#include "bcmf_trace_counters.h"
#include <nuttx/wireless/ieee80211/bcmf_trace.h>

struct net_driver_s
{
  void *d_private;
  int (*d_ioctl)(struct net_driver_s *, int, unsigned long);
};
struct bcmf_dev_s { bool bc_bifup; int ioctl_mutex; };
static int bcmf_ioctl(struct net_driver_s *dev, int cmd, unsigned long arg)
{ (void)dev; (void)cmd; (void)arg; return -ENOTTY; }
static int locked, lock_error, queries, packet_error, retry_error;
static uint32_t packet_size = sizeof(get_pktcnt_t), retry_size;
static uint16_t version = 6, declared_size;
static int nxmutex_lock(int *mutex)
{ (void)mutex; assert(!locked); if (lock_error) return lock_error; locked = 1; return 0; }
static void nxmutex_unlock(int *mutex) { (void)mutex; assert(locked); locked = 0; }

static void put16(uint8_t *data, uint16_t value)
{ memcpy(data, &value, sizeof(value)); }

static uint32_t fill30(uint8_t *data)
{
  const struct bcmf_trace_cnt30_prefix_s prefix =
    {.txframe = 11, .txretrans = 12, .txretry = 13, .txretrie = 14, .txfail = 15,
     .rxframe = 21, .rxerror = 22, .rxnobuf = 23, .rxoflo = 24,
     .rxfilter = 25, .rxbadproto = 26, .rxfragerr = 27, .reset = 28};
  const uint16_t tuple_size = sizeof(prefix) + 1;
  const uint16_t length = 8 + ((4 + tuple_size + 3) & ~3);
  put16(data, 30);
  put16(data + 2, length);
  /* Unknown tuple with a one-byte body: the parser must skip its padding. */
  put16(data + 4, 0x55);
  put16(data + 6, 1);
  data[8] = 0xa5;
  put16(data + 12, BCMF_TRACE_COUNTER_XTLV);
  put16(data + 14, tuple_size);
  memcpy(data + 16, &prefix, sizeof(prefix));
  data[16 + sizeof(prefix)] = 0x7e;
  return 4 + length;
}

static int bcmf_cdc_ioctl(struct bcmf_dev_s *priv, uint32_t ifidx, bool set,
                          uint32_t cmd, uint8_t *data, uint32_t *size)
{
  assert(priv && locked && !set && ifidx == 0 && cmd == WLC_GET_PKTCNTS);
  assert(*size == sizeof(get_pktcnt_t));
  const get_pktcnt_t packets = {1, 2, 3, 4, 5};
  memcpy(data, &packets, sizeof(packets));
  *size = packet_size;
  queries++;
  return packet_error;
}

static int bcmf_cdc_iovar_request(struct bcmf_dev_s *priv, uint32_t ifidx,
                                  bool set, char *name, uint8_t *data, uint32_t *size)
{
  assert(priv && locked && !set && ifidx == 0 && strcmp(name, "counters") == 0);
  assert(*size == BCMF_TRACE_COUNTER_BUFSIZE);
#define FILL(t) do { \
  t values = {.version = version, .length = declared_size ? declared_size : sizeof(t), \
              .txframe = 11, .txretrans = 12, .txretry = 13, .txretrie = 14, .txfail = 15, \
              .rxframe = 21, .rxerror = 22, .rxnobuf = 23, .rxoflo = 24, \
              .rxfilter = 25, .rxbadproto = 26, .rxfragerr = 27, .reset = 28, \
              .rxbadfcs = 31, .rxbadplcp = 32, .rxcrsglitch = 33, \
              .rxfrmtoolong = 34, .rxfrmtooshrt = 35, .rxinvmachdr = 36, \
              .rxstrt = 37}; \
  memcpy(data, &values, sizeof(values)); \
  *size = retry_size ? retry_size : sizeof(values); \
} while (0)
  switch (version)
    {
      case 6: FILL(wl_cnt_ver_six_t); break;
      case 7: FILL(wl_cnt_ver_seven_t); break;
      case 10: FILL(struct bcmf_trace_cnt10_prefix_s); break;
      case 30: *size = fill30(data); break;
      default: FILL(wl_cnt_ver_eight_t); break;
    }
#undef FILL
  queries++;
  return retry_error;
}

#include "bcmf-counters-functions.h"

static void check_receive(const struct bcmf_trace_counts_s *out)
{
  assert(out->rxframe == 21 && out->rxerror == 22 && out->rxnobuf == 23);
  assert(out->rxoflo == 24 && out->rxfilter == 25 && out->rxbadproto == 26);
  assert(out->rxfragerr == 27 && out->reset == 28);
}

static void check_phy(const struct bcmf_trace_counts_s *out)
{
  assert(out->phy_valid == 1);
  assert(out->rxbadfcs == 31 && out->rxbadplcp == 32);
  assert(out->rxcrsglitch == 33 && out->rxfrmtoolong == 34);
  assert(out->rxfrmtooshrt == 35 && out->rxinvmachdr == 36);
  assert(out->rxstrt == 37);
}

static void check_no_phy(const struct bcmf_trace_counts_s *out)
{
  assert(out->phy_valid == 0);
  assert(out->rxbadfcs == 0 && out->rxbadplcp == 0);
  assert(out->rxcrsglitch == 0 && out->rxfrmtoolong == 0);
  assert(out->rxfrmtooshrt == 0 && out->rxinvmachdr == 0);
  assert(out->rxstrt == 0);
}

int main(void)
{
  struct bcmf_dev_s priv = {.bc_bifup = true};
  struct net_driver_s dev = {.d_private = &priv, .d_ioctl = bcmf_ioctl};
  struct bcmf_trace_counts_s out;
  const struct bcmf_trace_counts_s sentinel = {.packet_result = 77};
  out = sentinel;
  assert(bcmf_trace_counters(NULL, &out) == -EINVAL);
  assert(bcmf_trace_counters(&dev, NULL) == -EINVAL);
  dev.d_ioctl = NULL;
  assert(bcmf_trace_counters(&dev, &out) == -ENODEV);
  dev.d_ioctl = bcmf_ioctl;
  lock_error = -EBUSY;
  assert(bcmf_trace_counters(&dev, &out) == -EBUSY);
  lock_error = 0;
  priv.bc_bifup = false;
  assert(bcmf_trace_counters(&dev, &out) == -EPERM);
  assert(!locked && queries == 0 && memcmp(&out, &sentinel, sizeof(out)) == 0);
  priv.bc_bifup = true;
  for (version = 6; version <= 8; version++)
    {
      int before = queries;
      assert(bcmf_trace_counters(&dev, &out) == 0);
      assert(!locked && queries == before + 2);
      assert(out.packet_result == 0 && out.retry_result == 0 && out.version == version);
      assert(out.rx_good_pkt == 1 && out.rx_bad_pkt == 2 && out.tx_good_pkt == 3
             && out.tx_bad_pkt == 4 && out.rx_ocast_good_pkt == 5);
      assert(out.txframe == 11 && out.txretrans == 12 && out.txretry == 13
             && out.txretrie == 14 && out.txfail == 15);
      check_receive(&out);
      check_phy(&out);
    }
  assert(out.version == 8);
  version = 6;
  retry_error = -EINVAL;
  assert(bcmf_trace_counters(&dev, &out) == 0 && out.retry_result == -EINVAL);
  assert(out.packet_result == 0 && out.tx_good_pkt == 3 && out.txretry == 0);
  assert(out.rxframe == 0 && out.rxnobuf == 0 && out.reset == 0);
  check_no_phy(&out);
  retry_error = 0;
  packet_size = sizeof(get_pktcnt_t) - 1;
  assert(bcmf_trace_counters(&dev, &out) == 0 && out.packet_result == -EPROTO);
  assert(out.tx_good_pkt == 0 && out.retry_result == 0 && out.txretry == 13);
  packet_size = sizeof(get_pktcnt_t);
  packet_error = -ETIMEDOUT;
  assert(bcmf_trace_counters(&dev, &out) == 0 && out.packet_result == -ETIMEDOUT);
  packet_error = 0;
  version = 10;
  assert(bcmf_trace_counters(&dev, &out) == 0 && out.retry_result == 0);
  assert(out.version == 10 && out.txframe == 11 && out.txretrans == 12
         && out.txretry == 13 && out.txretrie == 14 && out.txfail == 15);
  check_receive(&out);
  check_phy(&out);
  version = 30;
  assert(bcmf_trace_counters(&dev, &out) == 0 && out.retry_result == 0);
  assert(out.version == 30 && out.txframe == 11 && out.txretrans == 12
         && out.txretry == 13 && out.txretrie == 14 && out.txfail == 15);
  check_receive(&out);
  check_no_phy(&out);
  version = 99;
  assert(bcmf_trace_counters(&dev, &out) == 0 && out.retry_result == -EPROTONOSUPPORT);
  assert(out.version == 99 && out.txretry == 0 && out.tx_good_pkt == 3);
  check_no_phy(&out);
  version = 6;
  declared_size = 4;
  assert(bcmf_trace_counters(&dev, &out) == 0 && out.retry_result == -EPROTO);
  declared_size = 0;
  retry_size = sizeof(wl_cnt_ver_six_t) - 1;
  assert(bcmf_trace_counters(&dev, &out) == 0 && out.retry_result == -EPROTO);
  retry_size = 3;
  assert(bcmf_trace_counters(&dev, &out) == 0 && out.retry_result == -EPROTO);
  retry_size = UINT32_MAX;
  assert(bcmf_trace_counters(&dev, &out) == 0 && out.retry_result == -EPROTO);
  assert(!locked);

  /* Decode actual wire prefixes at unaligned addresses; never cast them. */
  {
    uint8_t storage[BCMF_TRACE_COUNTER_BUFSIZE + 1] = {0};
    uint8_t *wire = storage + 1;
    uint32_t size = fill30(wire);
    assert(bcmf_trace_decode_counts(wire, size, &out) == 0 && out.txretry == 13);
    check_receive(&out);
    check_no_phy(&out);
    assert(bcmf_trace_decode_counts(wire, size - 1, &out) == -EPROTO);
    check_no_phy(&out);
    put16(wire + 14, sizeof(struct bcmf_trace_cnt30_prefix_s) - 1);
    assert(bcmf_trace_decode_counts(wire, size, &out) == -EPROTO);
    fill30(wire);
    put16(wire + 14, UINT16_MAX);
    assert(bcmf_trace_decode_counts(wire, size, &out) == -EPROTO);
    fill30(wire);
    put16(wire + 2, UINT16_MAX);
    assert(bcmf_trace_decode_counts(wire, size, &out) == -EPROTO);
    fill30(wire);
    put16(wire + 12, 0x56);
    assert(bcmf_trace_decode_counts(wire, size, &out) == -EPROTONOSUPPORT);
    fill30(wire);
    put16(wire + 2, size - 4 - 1); /* Missing final alignment byte. */
    assert(bcmf_trace_decode_counts(wire, size, &out) == -EPROTO);
    fill30(wire);
    put16(wire + 2, 1); /* Partial tuple header. */
    assert(bcmf_trace_decode_counts(wire, size, &out) == -EPROTO);
    fill30(wire);
    put16(wire + 2, 0);
    assert(bcmf_trace_decode_counts(wire, size, &out) == -EPROTONOSUPPORT);
    check_no_phy(&out);
    put16(wire, 99);
    assert(bcmf_trace_decode_counts(wire, size, &out) == -EPROTONOSUPPORT);
    check_no_phy(&out);
    fill30(wire);
    /* Duplicate target tuple is ambiguous, even when both bodies are valid. */
    memcpy(wire + size, wire + 12, size - 12);
    put16(wire + 2, (size - 4) + (size - 12));
    assert(bcmf_trace_decode_counts(wire, size + size - 12, &out) == -EPROTO);

    const struct bcmf_trace_cnt10_prefix_s ten =
      {.version = 10, .length = sizeof(ten) + 8, .txframe = 11, .txretry = 13,
       .rxbadfcs = 31, .rxbadplcp = 32, .rxcrsglitch = 33,
       .rxfrmtoolong = 34, .rxfrmtooshrt = 35, .rxinvmachdr = 36,
       .rxstrt = 37};
    memcpy(wire, &ten, sizeof(ten));
    assert(bcmf_trace_decode_counts(wire, sizeof(ten) + 8, &out) == 0);
    check_phy(&out);
    assert(bcmf_trace_decode_counts(wire, sizeof(ten), &out) == -EPROTO);
    check_no_phy(&out);
    put16(wire + 2, sizeof(ten) - 1);
    assert(bcmf_trace_decode_counts(wire, sizeof(ten), &out) == -EPROTO);
  }
  return 0;
}

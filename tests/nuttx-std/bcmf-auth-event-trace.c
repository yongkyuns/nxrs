/* SPDX-License-Identifier: MIT */
/* Exercise the extracted native auth handler with boundary stand-ins. */
#include <assert.h>
#include <inttypes.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define FAR
#define OK 0

#include "bcmf-auth-native.h"

struct net_driver_s
{
  uint32_t d_flags;
};

struct bcmf_dev_s
{
  struct net_driver_s bc_dev;
  bool bc_bifup;
  bool auth_pending;
  uint32_t auth_status;
  void *auth_signal;
};

static void netdev_carrier_on(struct net_driver_s *dev);
static void netdev_carrier_off(struct net_driver_s *dev);
static void nxsem_post(void *signal);
static void bcmf_hexdump(uint8_t *data, unsigned int len,
                         unsigned long offset);
static void capture_warning(const char *format, ...);

#define wlwarn(...) capture_warning(__VA_ARGS__)
#define wlinfo(...) ((void)0)

#include "bcmf-auth-functions.h"

static char log_buffer[256];
static unsigned int log_count;
static unsigned int carrier_on_count;
static unsigned int carrier_off_count;
static unsigned int sem_post_count;
static unsigned int hexdump_count;

static void capture_warning(const char *format, ...)
{
  va_list args;
  int length;

  va_start(args, format);
  length = vsnprintf(log_buffer, sizeof(log_buffer), format, args);
  va_end(args);
  assert(length >= 0 && (size_t)length < sizeof(log_buffer));
  log_count++;
}

static void netdev_carrier_on(struct net_driver_s *dev)
{
  IFF_SET_RUNNING(dev->d_flags);
  carrier_on_count++;
}

static void netdev_carrier_off(struct net_driver_s *dev)
{
  IFF_CLR_RUNNING(dev->d_flags);
  carrier_off_count++;
}

static void nxsem_post(void *signal)
{
  assert(signal != NULL);
  sem_post_count++;
}

static void bcmf_hexdump(uint8_t *data, unsigned int len, unsigned long offset)
{
  (void)data;
  (void)len;
  (void)offset;
  hexdump_count++;
}

static void put_wire32(uint32_t value, uint8_t bytes[4])
{
  bytes[0] = (uint8_t)(value >> 24);
  bytes[1] = (uint8_t)(value >> 16);
  bytes[2] = (uint8_t)(value >> 8);
  bytes[3] = (uint8_t)value;
}

static void reset_observations(void)
{
  log_buffer[0] = '\0';
  log_count = 0;
  carrier_on_count = 0;
  carrier_off_count = 0;
  sem_post_count = 0;
  hexdump_count = 0;
}

static void expect_trace(const char *expected)
{
#ifdef CONFIG_IEEE80211_BROADCOM_FRAME_TRACE
  assert(log_count == 1);
  assert(strcmp(log_buffer, expected) == 0);
#else
  (void)expected;
  assert(log_count == 0);
#endif
}

int main(void)
{
  static const char fake_name[] = "private-name";
  static const char fake_credential[] = "credential-sentinel";
  static const uint8_t fake_payload[] = "payload-sentinel";
  struct
  {
    struct bcmf_event_s event;
    uint8_t payload[sizeof(fake_credential) + sizeof(fake_payload)];
  } frame;
  struct bcmf_dev_s priv;
  void *const signal = &priv;

  (void)capture_warning;
  memset(&frame, 0, sizeof(frame));
  memset(frame.event.addr.octet, 0xa5, sizeof(frame.event.addr.octet));
  memcpy(frame.event.src_name, fake_name, sizeof(fake_name));
  memcpy(frame.payload, fake_credential, sizeof(fake_credential));
  memcpy(frame.payload + sizeof(fake_credential), fake_payload,
         sizeof(fake_payload));
  put_wire32((uint32_t)WLC_E_PSK_SUP, (uint8_t *)&frame.event.type);
  put_wire32((uint32_t)WLC_E_STATUS_SUCCESS, (uint8_t *)&frame.event.status);
  put_wire32((uint32_t)WLC_E_SUP_OTHER, (uint8_t *)&frame.event.reason);
  ((uint8_t *)&frame.event.flags)[0] = 0xab;
  ((uint8_t *)&frame.event.flags)[1] = 0xcd;
  priv.bc_dev.d_flags = IFF_RUNNING;
  priv.bc_bifup = false;
  priv.auth_pending = true;
  priv.auth_status = 0xfeed;
  priv.auth_signal = signal;

  reset_observations();
  bcmf_wl_auth_event_handler(&priv, &frame.event, sizeof(frame));
  expect_trace("BCMF_AUTH_EVENT type=46 status=0 reason=512 flags=43981 "
               "pending=1 waiter=1 up=0 running=1\n");
  assert(carrier_on_count == 0 && carrier_off_count == 0);
  assert(sem_post_count == 0 && hexdump_count == 0);
  assert(priv.auth_status == 0xfeed && IFF_IS_RUNNING(priv.bc_dev.d_flags));

  priv.bc_bifup = true;
  reset_observations();
  bcmf_wl_auth_event_handler(&priv, &frame.event, sizeof(frame));
  expect_trace("BCMF_AUTH_EVENT type=46 status=0 reason=512 flags=43981 "
               "pending=1 waiter=1 up=1 running=1\n");
  assert(carrier_on_count == 1 && carrier_off_count == 0);
  assert(sem_post_count == 1 && hexdump_count == 1);
  assert(priv.auth_status == WLC_E_SUP_OTHER);
  assert(IFF_IS_RUNNING(priv.bc_dev.d_flags));
  assert(strstr(log_buffer, fake_name) == NULL);
  assert(strstr(log_buffer, fake_credential) == NULL);
  assert(strstr(log_buffer, (const char *)fake_payload) == NULL);

  put_wire32((uint32_t)WLC_E_LINK, (uint8_t *)&frame.event.type);
  put_wire32(1, (uint8_t *)&frame.event.reason);
  reset_observations();
  bcmf_wl_auth_event_handler(&priv, &frame.event, sizeof(frame));
  expect_trace("BCMF_AUTH_EVENT type=16 status=0 reason=1 flags=43981 "
               "pending=1 waiter=1 up=1 running=1\n");
  assert(carrier_on_count == 0 && carrier_off_count == 1);
  assert(sem_post_count == 0 && hexdump_count == 1);
  assert(!IFF_IS_RUNNING(priv.bc_dev.d_flags));

  return 0;
}

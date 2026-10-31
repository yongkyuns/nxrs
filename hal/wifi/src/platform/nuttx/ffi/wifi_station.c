/* SPDX-License-Identifier: MIT */
#include <nuttx/config.h>
#include <errno.h>
#include <stdint.h>
#include <string.h>
#include <sys/ioctl.h>
#include <net/if.h>
#include <netinet/in.h>
#include <nuttx/wireless/wireless.h>

/* Only target headers describe ioctl/DHCP layouts. No SDK or native structures
 * cross this private ABI. Builds without DHCP advertise scan capability only. */
#ifdef CONFIG_NETUTILS_DHCPC
#include "netutils/dhcpc.h"
#include "netutils/netlib.h"
#endif

static int named(struct iwreq *request, const char *name)
{
  size_t length = strlen(name);
  if (length == 0 || length >= IFNAMSIZ)
    {
      return EINVAL;
    }
  memset(request, 0, sizeof(*request));
  memcpy(request->ifr_name, name, length);
  return 0;
}

static int control(int fd, int command, void *request)
{
  return ioctl(fd, command, (unsigned long)(uintptr_t)request) < 0 ?
         (errno != 0 ? errno : EIO) : 0;
}

int wifi_station_available(void)
{
#ifdef CONFIG_NETUTILS_DHCPC
  return 1;
#else
  return 0;
#endif
}

/* This driver uses the native poll-thread shutdown barrier. Other drivers and
 * execution configurations need their own qualification before admission.
 */
int wifi_quiescence_available(void)
{
#if defined(CONFIG_ARCH_CHIP_ESP32S3) && \
    defined(CONFIG_ESPRESSIF_WIFI_STATION) && !defined(CONFIG_SMP)
  return 1;
#else
  return 0;
#endif
}

int wifi_interface_flags_get(int fd, const char *name,
                             uint32_t *flags, uint32_t *up_mask)
{
  struct ifreq request = {0};
  struct iwreq wireless;
  int result = named(&wireless, name);
  if (result != 0) return result;
  memcpy(request.ifr_name, wireless.ifr_name, IFNAMSIZ);
  result = control(fd, SIOCGIFFLAGS, &request);
  if (result == 0)
    {
      *flags = request.ifr_flags;
      *up_mask = IFF_UP;
    }
  return result;
}

int wifi_interface_flags_set(int fd, const char *name, uint32_t flags)
{
  struct ifreq request = {0};
  struct iwreq wireless;
  int result = named(&wireless, name);
  if (result != 0) return result;
  memcpy(request.ifr_name, wireless.ifr_name, IFNAMSIZ);
  request.ifr_flags = flags;
  return control(fd, SIOCSIFFLAGS, &request);
}

int wifi_station_link(int fd, const char *name, uint32_t *state)
{
  struct ifreq request = {0};
  struct iwreq wireless;
  int result = named(&wireless, name);
  if (result != 0) return result;
  memcpy(request.ifr_name, wireless.ifr_name, IFNAMSIZ);
  result = control(fd, SIOCGIFFLAGS, &request);
  if (result == 0)
    {
      /* Configured ESSID/BSSID are NOT evidence of association. */
      *state = (request.ifr_flags & IFF_RUNNING) != 0 ? 1 : 0;
    }
  return result;
}

int wifi_station_disconnect(int fd, const char *name)
{
  struct iwreq request;
  int result = named(&request, name);
  if (result != 0) return result;
  /* ESSID_OFF invokes the generic lower-half disconnect operation. It is not
   * the legacy random-SSID/zero-BSSID workaround. */
  request.u.essid.flags = IW_ESSID_OFF;
  return control(fd, SIOCSIWESSID, &request);
}

/* One native operation per binding. Rust owns ordering and rollback. */
int wifi_station_infra(int fd, const char *name)
{
  struct iwreq request;
  int result = named(&request, name);
  if (result) return result;
  request.u.mode = IW_MODE_INFRA;
  return control(fd, SIOCSIWMODE, &request);
}

int wifi_station_wpa(int fd, const char *name, int secured)
{
  struct iwreq request;
  int result = named(&request, name);
  if (result) return result;
  request.u.param.flags = IW_AUTH_WPA_VERSION;
  request.u.param.value = secured ? IW_AUTH_WPA_VERSION_WPA2 : IW_AUTH_WPA_VERSION_DISABLED;
  return control(fd, SIOCSIWAUTH, &request);
}

int wifi_station_cipher(int fd, const char *name, int secured)
{
  struct iwreq request;
  int result = named(&request, name);
  if (result) return result;
  request.u.param.flags = IW_AUTH_CIPHER_PAIRWISE;
  request.u.param.value = secured ? IW_AUTH_CIPHER_CCMP : IW_AUTH_CIPHER_NONE;
  return control(fd, SIOCSIWAUTH, &request);
}

int wifi_station_key(int fd, const char *name, const uint8_t *password, size_t length)
{
  struct iwreq request;
  union { struct iw_encode_ext alignment;
    uint8_t bytes[sizeof(struct iw_encode_ext) + 63]; } storage = {0};
  struct iw_encode_ext *key = (struct iw_encode_ext *)storage.bytes;
  int result = named(&request, name);
  if (result) return result;
  if (length > 63 || (length && (!password || length < 8))) return EINVAL;
  key->alg = length ? IW_ENCODE_ALG_CCMP : IW_ENCODE_ALG_NONE;
  key->key_len = length;
  if (length) memcpy(key->key, password, length);
  request.u.encoding.pointer = key;
  request.u.encoding.length = sizeof(*key) + length;
  request.u.encoding.flags = length ? 0 : IW_ENCODE_DISABLED;
  result = control(fd, SIOCSIWENCODEEXT, &request);
  explicit_bzero(&storage, sizeof(storage));
  return result;
}

int wifi_station_ssid(int fd, const char *name, const uint8_t *ssid, size_t length)
{
  struct iwreq request;
  int result = named(&request, name);
  if (result) return result;
  if (!ssid || !length || length > 32) return EINVAL;
  request.u.essid.pointer = (void *)ssid;
  request.u.essid.length = length;
  request.u.essid.flags = IW_ESSID_ON;
  return control(fd, SIOCSIWESSID, &request);
}

int wifi_station_address(const char *name, uint32_t field, uint32_t value)
{
#ifdef CONFIG_NETUTILS_DHCPC
  struct in_addr address = {.s_addr = htonl(value)};
  int result;
  switch (field) {
    case 0: result = netlib_set_ipv4addr(name, &address); break;
    case 1: result = netlib_set_ipv4netmask(name, &address); break;
    case 2: result = netlib_set_dripv4addr(name, &address); break;
    default: return EINVAL;
  }
  return result < 0 ? (errno ? errno : EIO) : 0;
#else
  (void)name; (void)field; (void)value;
  return ENOSYS;
#endif
}

int wifi_station_mac(const char *name, uint8_t mac[6])
{
#ifdef CONFIG_NETUTILS_DHCPC
  return netlib_getmacaddr(name, mac) < 0 ? (errno ? errno : EIO) : 0;
#else
  (void)name; (void)mac; return ENOSYS;
#endif
}

int wifi_dhcp_open(const char *name, const uint8_t mac[6], void **client)
{
#ifdef CONFIG_NETUTILS_DHCPC
  *client = dhcpc_open(name, mac, 6);
  return *client ? 0 : (errno ? errno : EIO);
#else
  (void)name; (void)mac; (void)client; return ENOSYS;
#endif
}

struct wifi_lease { uint32_t address, netmask, router, seconds; };
int wifi_dhcp_request(void *client, struct wifi_lease *output)
{
#ifdef CONFIG_NETUTILS_DHCPC
  struct dhcpc_state lease = {0};
  if (dhcpc_request(client, &lease) < 0) return ETIMEDOUT;
  output->address = ntohl(lease.ipaddr.s_addr);
  output->netmask = ntohl(lease.netmask.s_addr);
  output->router = ntohl(lease.default_router.s_addr);
  output->seconds = lease.lease_time;
  return 0;
#else
  (void)client; (void)output; return ENOSYS;
#endif
}

void wifi_dhcp_close(void *client)
{
#ifdef CONFIG_NETUTILS_DHCPC
  dhcpc_close(client);
#else
  (void)client;
#endif
}

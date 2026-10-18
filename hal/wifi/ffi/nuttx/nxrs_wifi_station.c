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

int nxrs_wifi_station_available(void)
{
#ifdef CONFIG_NETUTILS_DHCPC
  return 1;
#else
  return 0;
#endif
}

int nxrs_wifi_station_link(int fd, const char *name, uint32_t *state)
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

int nxrs_wifi_station_disconnect(int fd, const char *name)
{
  struct iwreq request;
  int result = named(&request, name);
  if (result != 0) return result;
  /* ESSID_OFF invokes the generic lower-half disconnect operation. It is not
   * the legacy random-SSID/zero-BSSID workaround. */
  request.u.essid.flags = IW_ESSID_OFF;
  return control(fd, SIOCSIWESSID, &request);
}

int nxrs_wifi_station_connect(int fd, const char *name,
                              const uint8_t *ssid, size_t ssid_length,
                              const uint8_t *password, size_t password_length)
{
  struct iwreq request;
  /* A union keeps the flexible-array key properly aligned. */
  union
  {
    struct iw_encode_ext alignment;
    uint8_t bytes[sizeof(struct iw_encode_ext) + 63];
  } storage = {0};
  struct iw_encode_ext *key = (struct iw_encode_ext *)storage.bytes;
  int result = named(&request, name);
  if (result != 0) return result;
  if (ssid_length == 0 || ssid_length > 32 || password_length > 63 ||
      (password_length != 0 && password_length < 8)) return EINVAL;

  /* Do not rely on a driver's boot default (BCMF starts in ad-hoc mode). */
  request.u.mode = IW_MODE_INFRA;
  result = control(fd, SIOCSIWMODE, &request);
  if (result != 0) return result;
  named(&request, name);

  request.u.param.flags = IW_AUTH_WPA_VERSION;
  request.u.param.value = password_length == 0 ?
                         IW_AUTH_WPA_VERSION_DISABLED : IW_AUTH_WPA_VERSION_WPA2;
  result = control(fd, SIOCSIWAUTH, &request);
  if (result != 0) return result;

  /* WPA version alone does not select the cipher on full-MAC drivers (BCMF).
   * Use the same generic wireless ioctl sequence for every NuttX board. */
  named(&request, name);
  request.u.param.flags = IW_AUTH_CIPHER_PAIRWISE;
  request.u.param.value = password_length == 0 ?
                         IW_AUTH_CIPHER_NONE : IW_AUTH_CIPHER_CCMP;
  result = control(fd, SIOCSIWAUTH, &request);
  if (result != 0) return result;

  named(&request, name);
  key->alg = password_length == 0 ? IW_ENCODE_ALG_NONE : IW_ENCODE_ALG_CCMP;
  key->key_len = password_length;
  memcpy(key->key, password, password_length);
  request.u.encoding.pointer = key;
  request.u.encoding.length = sizeof(*key) + password_length;
  request.u.encoding.flags = password_length == 0 ? IW_ENCODE_DISABLED : 0;
  result = control(fd, SIOCSIWENCODEEXT, &request);
  /* No password logging, including when native configuration fails. */
  explicit_bzero(&storage, sizeof(storage));
  if (result != 0) return result;

  named(&request, name);
  request.u.essid.pointer = (void *)ssid;
  request.u.essid.length = ssid_length; /* Raw bytes; not a C string. */
  request.u.essid.flags = IW_ESSID_ON;
  return control(fd, SIOCSIWESSID, &request);
}

int nxrs_wifi_station_clear_address(const char *name)
{
#ifdef CONFIG_NETUTILS_DHCPC
  struct in_addr zero = {0};
  int failed = 0;
  /* Try all three even if one operation fails. */
  failed |= netlib_set_ipv4addr(name, &zero) < 0;
  failed |= netlib_set_ipv4netmask(name, &zero) < 0;
  failed |= netlib_set_dripv4addr(name, &zero) < 0;
  return failed ? EIO : 0;
#else
  return ENOSYS;
#endif
}

int nxrs_wifi_station_dhcp(const char *name, uint32_t *lease_seconds)
{
#ifdef CONFIG_NETUTILS_DHCPC
  uint8_t mac[6];
  struct dhcpc_state lease = {0};
  void *client;
  int result;
  if (netlib_getmacaddr(name, mac) < 0) return errno != 0 ? errno : EIO;
  client = dhcpc_open(name, mac, sizeof(mac));
  if (client == NULL) return errno != 0 ? errno : EIO;
  /* Receive timeout/retries are configured explicitly by the platform profile.
   * The client can install a tentative OFFER address before ACK; callers must
   * clear configuration on EVERY failure, not just consult SIOCGIFADDR. */
  result = dhcpc_request(client, &lease);
  dhcpc_close(client);
  if (result < 0) return ETIMEDOUT;
  if (lease.ipaddr.s_addr == INADDR_ANY ||
      lease.ipaddr.s_addr == INADDR_BROADCAST ||
      IN_MULTICAST(ntohl(lease.ipaddr.s_addr)) ||
      lease.netmask.s_addr == INADDR_ANY || lease.lease_time == 0) return EPROTO;
  if (netlib_set_ipv4netmask(name, &lease.netmask) < 0 ||
      netlib_set_dripv4addr(name, &lease.default_router) < 0 ||
      netlib_set_ipv4addr(name, &lease.ipaddr) < 0) return EIO;
  *lease_seconds = lease.lease_time;
  return 0;
#else
  return ENOSYS;
#endif
}

/* CoreWLAN calls are synchronous and belong on the existing Wi-Fi worker.
 * No callbacks retain Rust pointers. Only an explicit Connect/Disconnect may
 * change an association; opening/scanning/stopping never powers the radio.
 */
#import <CoreWLAN/CoreWLAN.h>
#import <CoreLocation/CoreLocation.h>
#include <ifaddrs.h>
#include <net/if.h>
#include <netinet/in.h>
#include <stdbool.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include "../native/wifi.h"

@interface NXRSWifiContext : NSObject
@property(nonatomic, strong) CWInterface *interface;
@property(nonatomic, copy) NSString *ownedBSSID;
@property(nonatomic) uint32_t storage;
@end
@implementation NXRSWifiContext
@end

static int location_permission(void)
{
  /* Query only. The application, not a HAL worker, owns permission UI. */
  if (@available(macOS 11.0, *))
    {
      CLLocationManager *manager = [CLLocationManager new];
      return [CLLocationManager locationServicesEnabled] &&
        manager.authorizationStatus == kCLAuthorizationStatusAuthorizedAlways
        ? NXRS_WIFI_OK : NXRS_WIFI_DENIED;
    }
  return NXRS_WIFI_UNSUPPORTED;
}

static int wlan_error(NSError *error)
{
  if (![error.domain isEqualToString:CWErrorDomain]) return NXRS_WIFI_IO;
  switch (error.code)
    {
      case kCWOperationNotPermittedErr: return NXRS_WIFI_DENIED;
      case kCWTimeoutErr: case kCWSupplicantTimeoutErr: return NXRS_WIFI_TIMEOUT;
      case kCWChallengeFailureErr: case kCWInvalidPMKErr:
      case kCWEAPOLErr: return NXRS_WIFI_AUTH;
      case kCWNotSupportedErr: case kCWUnsupportedCapabilitiesErr: return NXRS_WIFI_UNSUPPORTED;
      case kCWInvalidParameterErr: case kCWInvalidFormatErr: return NXRS_WIFI_INVALID;
      case kCWReferenceNotBoundErr: return NXRS_WIFI_UNAVAILABLE;
      default: return NXRS_WIFI_IO;
    }
}

static int ip_addressed(CWInterface *interface, bool *addressed)
{
  const char *name = interface.interfaceName.UTF8String;
  struct ifaddrs *addresses = NULL;
  *addressed = false;
  if (name == NULL || getifaddrs(&addresses) != 0) return NXRS_WIFI_IO;
  for (struct ifaddrs *item = addresses; item != NULL; item = item->ifa_next)
    {
      if (item->ifa_addr == NULL ||
          !(item->ifa_flags & IFF_UP) || strcmp(item->ifa_name, name) != 0) continue;
      if (item->ifa_addr->sa_family == AF_INET) {
        const struct sockaddr_in *address = (const struct sockaddr_in *)item->ifa_addr;
        uint32_t host = ntohl(address->sin_addr.s_addr);
        if (host != 0 && (host >> 24) != 127 && (host & 0xffff0000) != 0xa9fe0000 &&
            (host >> 28) < 14) *addressed = true;
      } else if (item->ifa_addr->sa_family == AF_INET6) {
        const struct in6_addr *address = &((const struct sockaddr_in6 *)item->ifa_addr)->sin6_addr;
        if (!IN6_IS_ADDR_UNSPECIFIED(address) && !IN6_IS_ADDR_LOOPBACK(address) &&
            !IN6_IS_ADDR_LINKLOCAL(address) && !IN6_IS_ADDR_MULTICAST(address)) *addressed = true;
      }
    }
  freeifaddrs(addresses);
  return NXRS_WIFI_OK;
}

static double monotonic_seconds(void)
{
  struct timespec now;
  if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) return -1;
  return (double)now.tv_sec + (double)now.tv_nsec / 1000000000.0;
}

int nxrs_host_wifi_open(void **context, uint32_t storage)
{
  if (context == NULL) return NXRS_WIFI_INVALID;
  *context = NULL;
  if (storage != NXRS_WIFI_VOLATILE && storage != NXRS_WIFI_SYSTEM_MANAGED)
    return NXRS_WIFI_INVALID;
  @autoreleasepool {
    CWInterface *interface = [CWWiFiClient sharedWiFiClient].interface;
    if (interface == nil || !interface.powerOn) return NXRS_WIFI_UNAVAILABLE;
    NXRSWifiContext *state = [NXRSWifiContext new];
    state.interface = interface;
    state.storage = storage;
    *context = (__bridge_retained void *)state;
    return NXRS_WIFI_OK;
  }
}

int nxrs_host_wifi_scan(void *context, struct nxrs_host_wifi_ap *rows,
                        uint32_t capacity, uint32_t *count, uint32_t *truncated)
{
  if (context == NULL || rows == NULL || capacity == 0 || count == NULL || truncated == NULL)
    return NXRS_WIFI_INVALID;
  *count = *truncated = 0;
  @autoreleasepool {
    int permission = location_permission();
    if (permission != NXRS_WIFI_OK) return permission;
    NXRSWifiContext *state = (__bridge NXRSWifiContext *)context;
    NSError *error = nil;
    NSSet<CWNetwork *> *networks = [state.interface scanForNetworksWithSSID:nil error:&error];
    if (networks == nil) return wlan_error(error);
    for (CWNetwork *network in networks)
      {
        if (*count == capacity) { *truncated = 1; break; }
        NSData *ssid = network.ssidData;
        if (ssid.length > 32 || network.rssiValue < INT16_MIN || network.rssiValue > INT16_MAX)
          return NXRS_WIFI_PROTOCOL;
        struct nxrs_host_wifi_ap *row = &rows[(*count)++];
        memset(row, 0, sizeof(*row));
        row->ssid_length = (uint32_t)ssid.length;
        if (ssid.length != 0) memcpy(row->ssid, ssid.bytes, ssid.length);
        row->rssi_dbm = (int32_t)network.rssiValue;
        row->security = [network supportsSecurity:kCWSecurityNone] ? 0 :
          ([network supportsSecurity:kCWSecurityWPA2Personal] ? 1 : 2);
      }
    return NXRS_WIFI_OK;
  }
}

int nxrs_host_wifi_link(void *context, uint32_t *link)
{
  if (context == NULL || link == NULL) return NXRS_WIFI_INVALID;
  @autoreleasepool {
    NXRSWifiContext *state = (__bridge NXRSWifiContext *)context;
    if (!state.interface.powerOn || state.interface.interfaceMode == kCWInterfaceModeNone)
      { *link = 1; return NXRS_WIFI_OK; }
    if (state.interface.interfaceMode != kCWInterfaceModeStation) return NXRS_WIFI_BUSY;
    bool addressed;
    int result = ip_addressed(state.interface, &addressed);
    if (result == NXRS_WIFI_OK) *link = addressed ? 3 : 2;
    return result;
  }
}

int nxrs_host_wifi_disconnect(void *context)
{
  if (context == NULL) return NXRS_WIFI_INVALID;
  @autoreleasepool {
    NXRSWifiContext *state = (__bridge NXRSWifiContext *)context;
    if (state.interface.interfaceMode == kCWInterfaceModeNone)
      { state.ownedBSSID = nil; return NXRS_WIFI_OK; }
    if (state.ownedBSSID == nil) return NXRS_WIFI_BUSY;
    int permission = location_permission();
    if (permission != NXRS_WIFI_OK) return permission;
    /* Refuse to disconnect a replacement association made by another client. */
    if (![state.interface.bssid isEqualToString:state.ownedBSSID]) return NXRS_WIFI_BUSY;
    double start = monotonic_seconds();
    if (start < 0) return NXRS_WIFI_IO;
    [state.interface disassociate];
    while (state.interface.interfaceMode != kCWInterfaceModeNone)
      {
        double now = monotonic_seconds();
        if (now < start || now - start >= 2.0) return NXRS_WIFI_IO;
        usleep(20000);
      }
    state.ownedBSSID = nil;
    return NXRS_WIFI_OK;
  }
}

int nxrs_host_wifi_connect(void *context, const uint8_t *ssid, size_t ssid_length,
                          const uint8_t *password, size_t password_length)
{
  if (context == NULL || ssid == NULL || ssid_length == 0 || ssid_length > 32 ||
      password_length > 63 || (password_length != 0 &&
      (password == NULL || password_length < 8))) return NXRS_WIFI_INVALID;
  @autoreleasepool {
    NXRSWifiContext *state = (__bridge NXRSWifiContext *)context;
    /* CoreWLAN saves credentials; reject before scanning or associating unless
     * the caller explicitly allowed OS-managed storage for this session. */
    if (state.storage != NXRS_WIFI_SYSTEM_MANAGED) return NXRS_WIFI_UNSUPPORTED;
    if (!state.interface.powerOn) return NXRS_WIFI_UNAVAILABLE;
    int result = location_permission();
    if (result != NXRS_WIFI_OK) return result;
    bool addressed;
    result = ip_addressed(state.interface, &addressed);
    if (result != NXRS_WIFI_OK) return result;
    if (addressed || state.interface.interfaceMode != kCWInterfaceModeNone) return NXRS_WIFI_BUSY;
    NSData *wanted = [NSData dataWithBytes:ssid length:ssid_length];
    NSError *error = nil;
    NSSet<CWNetwork *> *networks = [state.interface scanForNetworksWithSSID:wanted error:&error];
    if (networks == nil) return wlan_error(error);
    CWNetwork *selected = nil;
    for (CWNetwork *network in networks)
      {
        CWSecurity security = password_length == 0 ? kCWSecurityNone : kCWSecurityWPA2Personal;
        if (![network.ssidData isEqualToData:wanted] || ![network supportsSecurity:security]) continue;
        if (selected == nil || network.rssiValue > selected.rssiValue) selected = network;
      }
    if (selected == nil) return NXRS_WIFI_UNAVAILABLE;
    if (selected.bssid == nil) return NXRS_WIFI_DENIED;
    /* Scanning may take time; never knowingly replace an intervening owner. */
    result = ip_addressed(state.interface, &addressed);
    if (result != NXRS_WIFI_OK) return result;
    if (addressed || state.interface.interfaceMode != kCWInterfaceModeNone) return NXRS_WIFI_BUSY;
    NSString *passphrase = password_length == 0 ? nil :
      [[NSString alloc] initWithBytes:password length:password_length encoding:NSASCIIStringEncoding];
    if (password_length != 0 && passphrase == nil) return NXRS_WIFI_INVALID;
    state.ownedBSSID = selected.bssid;
    BOOL connected = [state.interface associateToNetwork:selected password:passphrase error:&error];
    result = connected ? NXRS_WIFI_TIMEOUT : wlan_error(error);
    double start = monotonic_seconds();
    if (start < 0) result = NXRS_WIFI_IO;
    while (connected && start >= 0)
      {
        uint32_t link = 0;
        result = nxrs_host_wifi_link(context, &link);
        if (result != NXRS_WIFI_OK) break;
        if (![state.interface.bssid isEqualToString:state.ownedBSSID])
          { result = NXRS_WIFI_UNAVAILABLE; break; }
        if (link == 3) return NXRS_WIFI_OK;
        double now = monotonic_seconds();
        if (now < start) { result = NXRS_WIFI_IO; break; }
        if (now - start >= 20.0) { result = NXRS_WIFI_TIMEOUT; break; }
        usleep(20000);
      }
    /* Recoverable completion is permitted only after rollback proves idle. */
    return nxrs_host_wifi_disconnect(context) == NXRS_WIFI_OK ? result : NXRS_WIFI_IO;
  }
}

int nxrs_host_wifi_release(void *context)
{
  @autoreleasepool {
    if (context != NULL) {
      NXRSWifiContext *state = (__bridge NXRSWifiContext *)context;
      if (state.ownedBSSID != nil) {
        /* A different, visible BSSID proves our association was replaced;
         * leave that host connection alone. Hidden identity is not proof. */
        if (state.interface.bssid != nil &&
            ![state.interface.bssid isEqualToString:state.ownedBSSID]) {
          state.ownedBSSID = nil;
        } else {
          int status = nxrs_host_wifi_disconnect(context);
          if (status != NXRS_WIFI_OK) return status;
        }
      }
      (void)CFBridgingRelease(context);
    }
  }
  return NXRS_WIFI_OK;
}

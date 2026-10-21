/* CoreWLAN object/field translation only. All session policy lives in Rust. */
#import <CoreWLAN/CoreWLAN.h>
#import <CoreLocation/CoreLocation.h>
#include <ifaddrs.h>
#include <net/if.h>
#include <netinet/in.h>
#include <stdbool.h>
#include <string.h>
#include "../native/wifi.h"
int nxrs_macos_wifi_permission(void)
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


struct nxrs_macos_wifi_row {
  uint64_t length;
  int64_t rssi;
  uint32_t security;
  uint32_t bssid_length;
  uint8_t ssid[32];
  uint8_t bssid[64];
};
int nxrs_macos_wifi_open(void **output)
{
  @autoreleasepool {
    CWInterface *interface = [CWWiFiClient sharedWiFiClient].interface;
    if (interface == nil) return NXRS_WIFI_UNAVAILABLE;
    *output = (__bridge_retained void *)interface;
    return 0;
  }
}
void nxrs_macos_wifi_release(void *object) { if (object) CFRelease(object); }
int nxrs_macos_wifi_state(void *context, uint32_t *powered, uint32_t *mode,
                          uint8_t bssid[64], uint32_t *length)
{
  @autoreleasepool {
    CWInterface *interface = (__bridge CWInterface *)context;
    *powered = interface.powerOn;
    *mode = interface.interfaceMode == kCWInterfaceModeNone ? 0 :
            interface.interfaceMode == kCWInterfaceModeStation ? 1 : 2;
    NSData *identity = [interface.bssid dataUsingEncoding:NSUTF8StringEncoding];
    if (identity.length > 64) return NXRS_WIFI_PROTOCOL;
    *length = (uint32_t)identity.length;
    if (identity.length) memcpy(bssid, identity.bytes, identity.length);
    return 0;
  }
}
int nxrs_macos_wifi_addresses(void *context, void *arg,
                              void (*emit)(void *, uint32_t, uint32_t, const uint8_t *))
{
  @autoreleasepool {
    const char *name = ((__bridge CWInterface *)context).interfaceName.UTF8String;
    struct ifaddrs *addresses = NULL;
    if (!name || getifaddrs(&addresses)) return NXRS_WIFI_IO;
    for (struct ifaddrs *item=addresses; item; item=item->ifa_next) {
      if (!item->ifa_addr || strcmp(item->ifa_name,name)) continue;
      uint32_t up = (item->ifa_flags & IFF_UP) != 0;
      if (item->ifa_addr->sa_family == AF_INET)
        emit(arg,4,up,(const uint8_t *)&((struct sockaddr_in *)item->ifa_addr)->sin_addr);
      else if (item->ifa_addr->sa_family == AF_INET6)
        emit(arg,6,up,(const uint8_t *)&((struct sockaddr_in6 *)item->ifa_addr)->sin6_addr);
    }
    freeifaddrs(addresses);
    return 0;
  }
}
int nxrs_macos_wifi_scan(void *context, const uint8_t *ssid, size_t length, void **snapshot)
{
  @autoreleasepool {
    NSData *filter = ssid ? [NSData dataWithBytes:ssid length:length] : nil;
    NSError *error = nil;
    NSSet<CWNetwork *> *networks = [(__bridge CWInterface *)context scanForNetworksWithSSID:filter error:&error];
    if (!networks) return wlan_error(error);
    *snapshot = (__bridge_retained void *)networks.allObjects;
    return 0;
  }
}
size_t nxrs_macos_wifi_count(void *snapshot) { return ((__bridge NSArray *)snapshot).count; }
int nxrs_macos_wifi_row(void *snapshot, size_t index, struct nxrs_macos_wifi_row *row)
{
  @autoreleasepool {
    NSArray *items = (__bridge NSArray *)snapshot;
    if (index >= items.count) return NXRS_WIFI_INVALID;
    CWNetwork *network = items[index];
    memset(row,0,sizeof(*row));
    row->length = network.ssidData.length;
    row->rssi = network.rssiValue;
    row->security = ([network supportsSecurity:kCWSecurityNone] ? 1u : 0u) |
                    ([network supportsSecurity:kCWSecurityWPA2Personal] ? 2u : 0u);
    if (row->length <= 32 && row->length) memcpy(row->ssid,network.ssidData.bytes,row->length);
    NSData *identity = [network.bssid dataUsingEncoding:NSUTF8StringEncoding];
    if (identity.length > 64) return NXRS_WIFI_PROTOCOL;
    row->bssid_length = (uint32_t)identity.length;
    if (identity.length) memcpy(row->bssid,identity.bytes,identity.length);
    return 0;
  }
}
int nxrs_macos_wifi_associate(void *context, void *snapshot, size_t index,
                              const uint8_t *password, size_t length)
{
  @autoreleasepool {
    NSArray *items = (__bridge NSArray *)snapshot;
    if (index >= items.count) return NXRS_WIFI_INVALID;
    NSString *passphrase = length ? [[NSString alloc] initWithBytes:password length:length encoding:NSASCIIStringEncoding] : nil;
    if (length && !passphrase) return NXRS_WIFI_INVALID;
    NSError *error = nil;
    BOOL result = [(__bridge CWInterface *)context associateToNetwork:items[index] password:passphrase error:&error];
    return result ? 0 : wlan_error(error);
  }
}
void nxrs_macos_wifi_disassociate(void *context)
{ @autoreleasepool { [(__bridge CWInterface *)context disassociate]; } }

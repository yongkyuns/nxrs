#include "native_radio_abi/wifi.h"
/* Exercise the actual CoreWLAN bridge against typed fake SDK objects. No radio,
 * permission UI, keychain, association or host interface is touched. */
#import <CoreWLAN/CoreWLAN.h>
#import <CoreLocation/CoreLocation.h>
#include <assert.h>
#include <arpa/inet.h>
#include <ifaddrs.h>
#include <netinet/in.h>
#include <net/if.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static bool addressed, associate_ok = true, assign_address = true;
static CLAuthorizationStatus permission = kCLAuthorizationStatusAuthorizedAlways;
static unsigned scans, associations, disassociations;
static uint64_t elapsed_us;

@interface FakeNetwork : NSObject
@property(nonatomic, strong) NSData *ssidData;
@property(nonatomic, copy) NSString *bssid;
@property(nonatomic) NSInteger rssiValue;
@property(nonatomic) CWSecurity security;
- (BOOL)supportsSecurity:(CWSecurity)value;
@end
@implementation FakeNetwork
- (BOOL)supportsSecurity:(CWSecurity)value { return value == self.security; }
@end

@interface FakeInterface : NSObject
@property(nonatomic, copy) NSString *interfaceName;
@property(nonatomic, copy) NSString *bssid;
@property(nonatomic) BOOL powerOn;
@property(nonatomic) CWInterfaceMode interfaceMode;
@property(nonatomic, strong) NSSet *networks;
- (NSSet *)scanForNetworksWithSSID:(NSData *)ssid error:(NSError **)error;
- (BOOL)associateToNetwork:(CWNetwork *)network password:(NSString *)password error:(NSError **)error;
- (void)disassociate;
@end
@implementation FakeInterface
- (NSSet *)scanForNetworksWithSSID:(NSData *)ssid error:(NSError **)error
{ (void)ssid; (void)error; scans++; return self.networks; }
- (BOOL)associateToNetwork:(CWNetwork *)network password:(NSString *)password error:(NSError **)error
{
  (void)password; associations++;
  if (!associate_ok) { *error = [NSError errorWithDomain:CWErrorDomain code:kCWChallengeFailureErr userInfo:nil]; return NO; }
  self.interfaceMode = kCWInterfaceModeStation;
  self.bssid = network.bssid; addressed = assign_address; return YES;
}
- (void)disassociate
{ disassociations++; self.interfaceMode = kCWInterfaceModeNone; self.bssid = nil; addressed = false; }
@end

static FakeInterface *interface;
@interface FakeClient : NSObject
@property(readonly) CWInterface *interface;
+ (instancetype)sharedWiFiClient;
@end
@implementation FakeClient
+ (instancetype)sharedWiFiClient { return [self new]; }
- (CWInterface *)interface { return (CWInterface *)interface; }
@end
@interface FakeLocation : NSObject
@property(readonly) CLAuthorizationStatus authorizationStatus;
+ (BOOL)locationServicesEnabled;
@end
@implementation FakeLocation
+ (BOOL)locationServicesEnabled { return YES; }
- (CLAuthorizationStatus)authorizationStatus { return permission; }
@end

static struct ifaddrs address_record;
static struct sockaddr_in address_value;
static struct sockaddr_in6 address6_value;
static const char *ipv6_address;
static int fake_addresses(struct ifaddrs **output)
{
  memset(&address_record, 0, sizeof(address_record));
  address_record.ifa_name = "fixture0"; address_record.ifa_flags = IFF_UP;
  address_value.sin_family = AF_INET; address_value.sin_addr.s_addr = htonl(0x0a2a0002);
  address_record.ifa_addr = (struct sockaddr *)&address_value;
  if (ipv6_address != NULL) {
    address6_value.sin6_family = AF_INET6;
    assert(inet_pton(AF_INET6, ipv6_address, &address6_value.sin6_addr) == 1);
    address_record.ifa_addr = (struct sockaddr *)&address6_value;
  }
  *output = addressed ? &address_record : NULL; return 0;
}
static void fake_free(struct ifaddrs *value) { assert(value == NULL || value == &address_record); }
#define CWWiFiClient FakeClient
#define CLLocationManager FakeLocation
#define getifaddrs fake_addresses
#define freeifaddrs fake_free
#include "../../hal/wifi/src/platform/macos/ffi/wifi.m"

static void *open_fixture_with_storage(uint32_t storage)
{
  interface = [FakeInterface new]; interface.powerOn = YES; interface.interfaceName = @"fixture0";
  FakeNetwork *network = [FakeNetwork new];
  network.ssidData = [NSData dataWithBytes:"fixture" length:7];
  network.bssid = @"fixture-peer"; network.rssiValue = -47;
  network.security = kCWSecurityWPA2Personal;
  interface.networks = [NSSet setWithObject:network];
  addressed = false; ipv6_address = NULL; assign_address = associate_ok = true;
  permission = kCLAuthorizationStatusAuthorizedAlways;
  scans = associations = disassociations = 0; elapsed_us = 0;
  void *context = NULL; assert(nxrs_host_wifi_open(&context, storage) == NXRS_WIFI_OK && context != NULL);
  return context;
}

static void *open_fixture(void)
{ return open_fixture_with_storage(NXRS_WIFI_SYSTEM_MANAGED); }

int main(void)
{
  @autoreleasepool {
    void *invalid = (void *)1;
    assert(nxrs_host_wifi_open(&invalid, 2) == NXRS_WIFI_INVALID && invalid == NULL);
    assert(nxrs_host_wifi_open(NULL, NXRS_WIFI_VOLATILE) == NXRS_WIFI_INVALID);
    void *volatile_context = open_fixture_with_storage(NXRS_WIFI_VOLATILE);
    assert(nxrs_host_wifi_connect(volatile_context, (const uint8_t *)"fixture", 7,
      (const uint8_t *)"passphrase", 10) == NXRS_WIFI_UNSUPPORTED);
    assert(nxrs_host_wifi_connect(volatile_context, (const uint8_t *)"fixture", 7,
      NULL, 0) == NXRS_WIFI_UNSUPPORTED);
    assert(scans == 0 && associations == 0 && disassociations == 0);
    struct nxrs_host_wifi_ap observed[1]; uint32_t observed_count, observed_truncated;
    assert(nxrs_host_wifi_scan(volatile_context, observed, 1, &observed_count,
      &observed_truncated) == NXRS_WIFI_OK && observed_count == 1);
    assert(nxrs_host_wifi_release(volatile_context) == NXRS_WIFI_OK);
    assert(associations == 0 && disassociations == 0);
    void *context = open_fixture();
    struct nxrs_host_wifi_ap rows[1]; uint32_t count, truncated, link;
    assert(nxrs_host_wifi_scan(context, rows, 1, &count, &truncated) == NXRS_WIFI_OK);
    assert(count == 1 && truncated == 0 && rows[0].ssid_length == 7 && rows[0].security == 1 && rows[0].rssi_dbm == -47);
    assert(memcmp(rows[0].ssid, "fixture", 7) == 0);
    FakeNetwork *network = interface.networks.anyObject;
    network.ssidData = [NSMutableData dataWithLength:33];
    assert(nxrs_host_wifi_scan(context, rows, 1, &count, &truncated) == NXRS_WIFI_PROTOCOL);
    network.ssidData = nil;
    assert(nxrs_host_wifi_scan(context, rows, 1, &count, &truncated) == NXRS_WIFI_OK && rows[0].ssid_length == 0);
    FakeNetwork *second = [FakeNetwork new];
    interface.networks = [NSSet setWithObjects:network, second, nil];
    assert(nxrs_host_wifi_scan(context, rows, 1, &count, &truncated) == NXRS_WIFI_OK && count == 1 && truncated == 1);
    NSMutableSet *many = [NSMutableSet new];
    for (unsigned i = 0; i < 64; i++) [many addObject:[FakeNetwork new]];
    interface.networks = many;
    struct nxrs_host_wifi_ap bounded[64];
    assert(nxrs_host_wifi_scan(context, bounded, 64, &count, &truncated) == NXRS_WIFI_OK && count == 64 && truncated == 0);
    [many addObject:[FakeNetwork new]]; interface.networks = many;
    assert(nxrs_host_wifi_scan(context, bounded, 64, &count, &truncated) == NXRS_WIFI_OK && count == 64 && truncated == 1);
    permission = kCLAuthorizationStatusDenied;
    unsigned previous_scans = scans;
    assert(nxrs_host_wifi_scan(context, rows, 1, &count, &truncated) == NXRS_WIFI_DENIED && scans == previous_scans);
    assert(nxrs_host_wifi_connect(context, (const uint8_t *)"fixture", 7, (const uint8_t *)"passphrase", 10) == NXRS_WIFI_DENIED);
    assert(associations == 0);
    nxrs_host_wifi_release(context);

    context = open_fixture();
    interface.interfaceMode = kCWInterfaceModeStation; addressed = true;
    assert(nxrs_host_wifi_connect(context, (const uint8_t *)"fixture", 7, (const uint8_t *)"passphrase", 10) == NXRS_WIFI_BUSY);
    assert(nxrs_host_wifi_disconnect(context) == NXRS_WIFI_BUSY);
    nxrs_host_wifi_release(context);
    assert(addressed && associations == 0 && disassociations == 0);

    context = open_fixture();
    interface.interfaceMode = kCWInterfaceModeStation; addressed = true;
    for (NSString *value in @[@"::", @"::1", @"fe80::1", @"ff02::1"]) {
      ipv6_address = value.UTF8String;
      assert(nxrs_host_wifi_link(context, &link) == NXRS_WIFI_OK && link == 2);
    }
    ipv6_address = "fd00::2";
    assert(nxrs_host_wifi_link(context, &link) == NXRS_WIFI_OK && link == 3);
    nxrs_host_wifi_release(context);

    context = open_fixture();
    assert(nxrs_host_wifi_connect(context, (const uint8_t *)"fixture", 7, (const uint8_t *)"passphrase", 10) == NXRS_WIFI_OK);
    assert(nxrs_host_wifi_link(context, &link) == NXRS_WIFI_OK && link == 3);
    interface.bssid = @"replacement-peer";
    assert(nxrs_host_wifi_disconnect(context) == NXRS_WIFI_BUSY && disassociations == 0);
    nxrs_host_wifi_release(context); assert(addressed && disassociations == 0);

    context = open_fixture();
    assert(nxrs_host_wifi_connect(context, (const uint8_t *)"fixture", 7, (const uint8_t *)"passphrase", 10) == NXRS_WIFI_OK);
    permission = kCLAuthorizationStatusDenied;
    assert(nxrs_host_wifi_release(context) == NXRS_WIFI_DENIED);
    assert(addressed && disassociations == 0); /* context retained for retry */
    permission = kCLAuthorizationStatusAuthorizedAlways;
    assert(nxrs_host_wifi_release(context) == NXRS_WIFI_OK);
    assert(!addressed && disassociations == 1);

    context = open_fixture();
    network = interface.networks.anyObject; network.security = kCWSecurityNone;
    assert(nxrs_host_wifi_connect(context, (const uint8_t *)"fixture", 7, NULL, 0) == NXRS_WIFI_OK);
    assert(nxrs_host_wifi_disconnect(context) == NXRS_WIFI_OK && disassociations == 1);
    assert(nxrs_host_wifi_link(context, &link) == NXRS_WIFI_OK && link == 1);
    assert(interface.powerOn && !addressed);
    nxrs_host_wifi_release(context);

    context = open_fixture(); associate_ok = false;
    assert(nxrs_host_wifi_connect(context, (const uint8_t *)"fixture", 7, (const uint8_t *)"passphrase", 10) == NXRS_WIFI_AUTH);
    assert(interface.interfaceMode == kCWInterfaceModeNone && disassociations == 0);
    nxrs_host_wifi_release(context);

    context = open_fixture(); assign_address = false;
    assert(nxrs_host_wifi_connect(context, (const uint8_t *)"fixture", 7, (const uint8_t *)"passphrase", 10) == NXRS_WIFI_TIMEOUT);
    assert(interface.interfaceMode == kCWInterfaceModeNone && disassociations == 1);
    nxrs_host_wifi_release(context);
  }
  return 0;
}

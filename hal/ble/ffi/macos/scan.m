/* CoreBluetooth delegates stay on one SDK queue. The shared Rust worker owns
 * lifecycle and event delivery; no SDK callback enters Rust. Each scan gets a
 * fresh manager so a late callback cannot become a report in a newer scan.
 */
#import <CoreBluetooth/CoreBluetooth.h>
#include <stdbool.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include "../native/scan.h"
#include "bluetooth.h"

#define NXRS_SCAN_ROWS 32
@class NXRSBleDelegate;
@interface NXRSBleContext : NSObject {
@public
  struct nxrs_host_ble_report rows[NXRS_SCAN_ROWS];
  uint32_t count;
}
@property(nonatomic, strong) dispatch_queue_t queue;
@property(nonatomic, strong) CBCentralManager *manager;
@property(nonatomic, strong) NXRSBleDelegate *delegate;
@property(nonatomic, copy) NSData *service;
@property(nonatomic) bool scanning;
@end

@interface NXRSBleDelegate : NSObject <CBCentralManagerDelegate>
@property(nonatomic, weak) NXRSBleContext *owner;
@end
@implementation NXRSBleContext
@end

static bool advertised_service(NSDictionary *advertisement, NSData *service)
{
  if (service == nil) return true;
  for (NSString *key in @[CBAdvertisementDataServiceUUIDsKey, CBAdvertisementDataOverflowServiceUUIDsKey])
    for (CBUUID *uuid in advertisement[key])
      if (uuid_matches(uuid, service)) return true;
  return false;
}

@implementation NXRSBleDelegate
- (void)centralManagerDidUpdateState:(CBCentralManager *)central { (void)central; }
- (void)centralManager:(CBCentralManager *)central didDiscoverPeripheral:(CBPeripheral *)peripheral
    advertisementData:(NSDictionary<NSString *, id> *)advertisement RSSI:(NSNumber *)RSSI
{
  NXRSBleContext *state = self.owner;
  if (state == nil || !state.scanning || central != state.manager) return;
  bool matched = advertised_service(advertisement, state.service);
  /* An unrelated advertisement must not consume the bounded filtered queue. */
  if (!matched) return;
  uuid_t identity;
  [peripheral.identifier getUUIDBytes:identity];
  uint32_t index = 0;
  while (index < state->count && memcmp(state->rows[index].peer, identity, 16) != 0) index++;
  /* Raw RF reports are best effort. Never overwrite another peer's identity. */
  if (index == state->count) {
    if (state->count == NXRS_SCAN_ROWS) return;
    memset(&state->rows[index], 0, sizeof(state->rows[index]));
    memcpy(state->rows[index].peer, identity, 16);
    state->count++;
  }
  NSInteger rssi = RSSI.integerValue;
  state->rows[index].rssi_dbm = RSSI == nil || rssi == 127 || rssi < INT16_MIN || rssi > INT16_MAX
    ? INT32_MIN : (int32_t)rssi;
  state->rows[index].matches_filter |= matched ? 1u : 0u;
}
@end

static int authorization(void)
{
  /* Query first. Application permission UI and radio power are not HAL policy. */
  if (@available(macOS 10.15, *))
    return CBManager.authorization == CBManagerAuthorizationAllowedAlways ? NXRS_BLE_OK : NXRS_BLE_DENIED;
  return NXRS_BLE_UNSUPPORTED;
}

static int manager_status(CBManagerState state)
{
  switch (state) {
    case CBManagerStatePoweredOn: return NXRS_BLE_OK;
    case CBManagerStateUnsupported: return NXRS_BLE_UNSUPPORTED;
    case CBManagerStateUnauthorized: return NXRS_BLE_DENIED;
    case CBManagerStatePoweredOff: return NXRS_BLE_UNAVAILABLE;
    case CBManagerStateUnknown: case CBManagerStateResetting: return NXRS_BLE_BUSY;
  }
  return NXRS_BLE_IO;
}

static double monotonic_seconds(void)
{
  struct timespec now;
  if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) return -1;
  return (double)now.tv_sec + (double)now.tv_nsec / 1000000000.0;
}

static int ready_manager(NXRSBleContext *state)
{
  int permission = authorization();
  if (permission != NXRS_BLE_OK) return permission;
  double start = monotonic_seconds();
  if (start < 0) return NXRS_BLE_IO;
  dispatch_sync(state.queue, ^{
    if (state.manager == nil) {
      state.delegate = [NXRSBleDelegate new];
      state.delegate.owner = state;
      state.manager = [[CBCentralManager alloc] initWithDelegate:state.delegate queue:state.queue
        options:@{CBCentralManagerOptionShowPowerAlertKey: @NO}];
    }
  });
  while (true) {
    __block int result;
    dispatch_sync(state.queue, ^{ result = state.manager == nil ? NXRS_BLE_UNAVAILABLE : manager_status(state.manager.state); });
    if (result != NXRS_BLE_BUSY) return result;
    double now = monotonic_seconds();
    if (now < start || now - start >= 5.0) return NXRS_BLE_TIMEOUT;
    usleep(20000);
  }
}

/* All callers are the private worker, never the SDK queue or consumer loop. */
static void detach(NXRSBleContext *state)
{
  state.scanning = false;
  state.delegate.owner = nil;
  state.manager.delegate = nil;
  state.delegate = nil;
  state.manager = nil;
  state.service = nil;
  state->count = 0;
}

int nxrs_host_ble_scan_open(void **context)
{
  if (context == NULL) return NXRS_BLE_INVALID;
  *context = NULL;
  @autoreleasepool {
    int result = authorization();
    if (result != NXRS_BLE_OK) return result;
    NXRSBleContext *state = [NXRSBleContext new];
    state.queue = dispatch_queue_create("nxrs.ble.scan", DISPATCH_QUEUE_SERIAL);
    result = ready_manager(state);
    if (result != NXRS_BLE_OK) {
      dispatch_sync(state.queue, ^{ detach(state); });
      return result;
    }
    *context = (__bridge_retained void *)state;
    return NXRS_BLE_OK;
  }
}

int nxrs_host_ble_scan_start(void *context, const uint8_t *service)
{
  if (context == NULL) return NXRS_BLE_INVALID;
  @autoreleasepool {
    NXRSBleContext *state = (__bridge NXRSBleContext *)context;
    int result = ready_manager(state);
    if (result != NXRS_BLE_OK) return result;
    NSData *filter = service == NULL ? nil : [NSData dataWithBytes:service length:16];
    __block int started = NXRS_BLE_OK;
    dispatch_sync(state.queue, ^{
      if (state.scanning) { started = NXRS_BLE_BUSY; return; }
      started = manager_status(state.manager.state);
      if (started != NXRS_BLE_OK) return;
      state.service = filter;
      state->count = 0;
      state.scanning = true;
      [state.manager scanForPeripheralsWithServices:nil options:@{CBCentralManagerScanOptionAllowDuplicatesKey:@YES}];
      if (!state.manager.isScanning) { state.scanning = false; started = NXRS_BLE_IO; }
    });
    return started;
  }
}

int nxrs_host_ble_scan_get(void *context, struct nxrs_host_ble_report *rows,
                           uint32_t capacity, uint32_t *count)
{
  if (context == NULL || rows == NULL || capacity == 0 || count == NULL) return NXRS_BLE_INVALID;
  *count = 0;
  @autoreleasepool {
    NXRSBleContext *state = (__bridge NXRSBleContext *)context;
    __block int result = NXRS_BLE_OK;
    dispatch_sync(state.queue, ^{
      if (!state.scanning) { result = NXRS_BLE_UNAVAILABLE; return; }
      result = manager_status(state.manager.state);
      if (result != NXRS_BLE_OK) return;
      *count = state->count < capacity ? state->count : capacity;
      memcpy(rows, state->rows, *count * sizeof(*rows));
      state->count -= *count;
      memmove(state->rows, state->rows + *count, state->count * sizeof(*rows));
    });
    return result;
  }
}

int nxrs_host_ble_scan_stop(void *context)
{
  if (context == NULL) return NXRS_BLE_INVALID;
  @autoreleasepool {
    NXRSBleContext *state = (__bridge NXRSBleContext *)context;
    __block int result = NXRS_BLE_OK;
    dispatch_sync(state.queue, ^{
      if (state.manager.isScanning) [state.manager stopScan];
      if (state.manager.isScanning) { result = NXRS_BLE_IO; return; }
      /* This serial barrier retires all callbacks that could publish a row.
       * Later SDK messages see a nil owner, even across a subsequent restart.
       */
      detach(state);
    });
    return result;
  }
}

void nxrs_host_ble_scan_release(void *context)
{
  if (context == NULL) return;
  @autoreleasepool {
    NXRSBleContext *state = (__bridge NXRSBleContext *)context;
    dispatch_sync(state.queue, ^{ detach(state); });
    CFRelease(context);
  }
}

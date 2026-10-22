/* CoreBluetooth field translation and serial delegate lifetime barriers.
 * Rust owns scan state, UUID filtering and bounded report delivery. */
#import <CoreBluetooth/CoreBluetooth.h>
#include <stdbool.h>
#include "status.h"
@class NXRSBleDelegate;
@interface NXRSBleContext : NSObject
@property(nonatomic, strong) dispatch_queue_t queue;
@property(nonatomic, strong) CBCentralManager *manager;
@property(nonatomic, strong) NXRSBleDelegate *delegate;
@property(nonatomic) void *callback;
@end
@interface NXRSBleDelegate : NSObject <CBCentralManagerDelegate>
@property(nonatomic, weak) NXRSBleContext *owner;
@end
@implementation NXRSBleContext
@end
extern void macos_scan_begin_rust(void *, void *, const uint8_t *, int64_t, bool);
extern void macos_scan_uuid_rust(void *, const uint8_t *, size_t);
extern void macos_scan_end_rust(void *);
@implementation NXRSBleDelegate
- (void)centralManagerDidUpdateState:(CBCentralManager *)central { (void)central; }
- (void)centralManager:(CBCentralManager *)central didDiscoverPeripheral:(CBPeripheral *)peripheral
    advertisementData:(NSDictionary<NSString *, id> *)advertisement RSSI:(NSNumber *)RSSI
{
  NXRSBleContext *state = self.owner;
  if (state == nil) return;
  uuid_t identity; [peripheral.identifier getUUIDBytes:identity];
  macos_scan_begin_rust(state.callback, (__bridge void *)central, identity, RSSI.integerValue, RSSI != nil);
  for (NSString *key in @[CBAdvertisementDataServiceUUIDsKey, CBAdvertisementDataOverflowServiceUUIDsKey])
    for (CBUUID *uuid in advertisement[key])
      macos_scan_uuid_rust(state.callback, uuid.data.bytes, uuid.data.length);
  macos_scan_end_rust(state.callback);
}
@end
int macos_scan_authorization(void)
{
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
void *macos_scan_context(void *callback)
{
  NXRSBleContext *state = [NXRSBleContext new]; state.callback = callback;
  state.queue = dispatch_queue_create("nxrs.ble.scan", DISPATCH_QUEUE_SERIAL);
  return (__bridge_retained void *)state;
}
void *macos_scan_manager(void *context)
{
  NXRSBleContext *state = (__bridge NXRSBleContext *)context;
  dispatch_sync(state.queue, ^{
    state.delegate = [NXRSBleDelegate new]; state.delegate.owner = state;
    state.manager = [[CBCentralManager alloc] initWithDelegate:state.delegate queue:state.queue
      options:@{CBCentralManagerOptionShowPowerAlertKey:@NO}];
  });
  return (__bridge void *)state.manager;
}
int macos_scan_status(void *context)
{
  NXRSBleContext *state = (__bridge NXRSBleContext *)context; __block int status;
  dispatch_sync(state.queue, ^{ status = state.manager == nil ? NXRS_BLE_UNAVAILABLE : manager_status(state.manager.state); });
  return status;
}
bool macos_scan_start(void *context)
{
  NXRSBleContext *state = (__bridge NXRSBleContext *)context; __block bool active;
  dispatch_sync(state.queue, ^{
    [state.manager scanForPeripheralsWithServices:nil options:@{CBCentralManagerScanOptionAllowDuplicatesKey:@YES}];
    active = state.manager.isScanning;
  }); return active;
}
bool macos_scan_stop(void *context)
{
  NXRSBleContext *state = (__bridge NXRSBleContext *)context; __block bool active;
  dispatch_sync(state.queue, ^{
    if (state.manager.isScanning) [state.manager stopScan];
    active = state.manager.isScanning;
  }); return !active;
}
void macos_scan_detach(void *context)
{
  NXRSBleContext *state = (__bridge NXRSBleContext *)context;
  dispatch_sync(state.queue, ^{
    state.delegate.owner = nil; state.manager.delegate = nil;
    state.delegate = nil; state.manager = nil;
  });
}
void macos_scan_release(void *context) { CFRelease(context); }

/* Exercise the production CoreBluetooth bridge with typed fake central and
 * peripheral objects. Real CBUUID normalization is tested; no radio is opened.
 */
#import <CoreBluetooth/CoreBluetooth.h>
#include <assert.h>
#include <stdbool.h>
#include <time.h>
#include <unistd.h>

static CBManagerAuthorization permission = CBManagerAuthorizationAllowedAlways;
static CBManagerState initial_state = CBManagerStatePoweredOn;
static unsigned managers, starts, stops;
static bool stuck_stop, failed_start;
static uint64_t elapsed_us;

@interface FakeManager : NSObject
@property(nonatomic, weak) id<CBCentralManagerDelegate> delegate;
@property(nonatomic) CBManagerState state;
@property(nonatomic) BOOL isScanning;
@property(nonatomic, strong) dispatch_queue_t queue;
- (instancetype)initWithDelegate:(id<CBCentralManagerDelegate>)delegate queue:(dispatch_queue_t)queue options:(NSDictionary *)options;
- (void)scanForPeripheralsWithServices:(NSArray *)services options:(NSDictionary *)options;
- (void)stopScan;
@end
@implementation FakeManager
- (instancetype)initWithDelegate:(id<CBCentralManagerDelegate>)delegate queue:(dispatch_queue_t)queue options:(NSDictionary *)options
{
  self = [super init];
  if (self) {
    managers++; self.delegate = delegate; self.queue = queue; self.state = initial_state;
    assert([options[CBCentralManagerOptionShowPowerAlertKey] isEqual:@NO]);
  }
  return self;
}
- (void)scanForPeripheralsWithServices:(NSArray *)services options:(NSDictionary *)options
{
  assert(services == nil && [options[CBCentralManagerScanOptionAllowDuplicatesKey] isEqual:@YES]);
  starts++; self.isScanning = !failed_start;
}
- (void)stopScan { stops++; if (!stuck_stop) self.isScanning = NO; }
@end

@interface FakeAuthorization : NSObject
+ (CBManagerAuthorization)authorization;
@end
@implementation FakeAuthorization
+ (CBManagerAuthorization)authorization { return permission; }
@end

@interface FakePeripheral : NSObject
@property(nonatomic, strong) NSUUID *identifier;
@end
@implementation FakePeripheral
@end

static int fake_clock(clockid_t kind, struct timespec *now)
{ assert(kind == CLOCK_MONOTONIC); now->tv_sec = elapsed_us / 1000000; now->tv_nsec = (elapsed_us % 1000000) * 1000; return 0; }
static int fake_sleep(useconds_t value) { elapsed_us += value; return 0; }

#define CBManager FakeAuthorization
#define CBCentralManager FakeManager
#define clock_gettime fake_clock
#define usleep fake_sleep
#include "../../hal/ble/ffi/macos/scan.m"

static NSData *canonical(NSString *value)
{
  uuid_t bytes;
  [[[NSUUID alloc] initWithUUIDString:value] getUUIDBytes:bytes];
  return [NSData dataWithBytes:bytes length:16];
}

static void advertise(NXRSBleContext *state, FakeManager *manager, NXRSBleDelegate *delegate,
                       uint8_t identity, NSDictionary *data, NSNumber *rssi)
{
  FakePeripheral *peripheral = [FakePeripheral new];
  uuid_t bytes = {0}; bytes[15] = identity;
  peripheral.identifier = [[NSUUID alloc] initWithUUIDBytes:bytes];
  dispatch_sync(state.queue, ^{
    /* The imported SDK protocol still names the real central class. Only our
     * fake's matching selectors are invoked by the included production code. */
    [delegate centralManager:(id)manager didDiscoverPeripheral:(CBPeripheral *)peripheral advertisementData:data RSSI:rssi];
  });
}

static void *open_fixture(void)
{
  permission = CBManagerAuthorizationAllowedAlways; initial_state = CBManagerStatePoweredOn;
  elapsed_us = 0; stuck_stop = failed_start = false;
  void *context = NULL;
  assert(nxrs_host_ble_scan_open(&context) == NXRS_BLE_OK && context != NULL);
  return context;
}

int main(void)
{
  @autoreleasepool {
    NSData *heart = canonical(@"0000180D-0000-1000-8000-00805F9B34FB");
    NSData *long_uuid = canonical(@"12345678-0000-1000-8000-00805F9B34FB");
    NSData *custom = canonical(@"4E585253-0001-4000-8000-000000000001");
    assert(uuid_matches([CBUUID UUIDWithString:@"180D"], heart));
    assert(uuid_matches([CBUUID UUIDWithString:@"12345678"], long_uuid));
    assert(uuid_matches([CBUUID UUIDWithString:@"4E585253-0001-4000-8000-000000000001"], custom));
    assert(!uuid_matches([CBUUID UUIDWithString:@"180E"], heart));
    assert(!uuid_matches([CBUUID UUIDWithString:@"12345678"], custom));

    for (NSNumber *value in @[@(CBManagerAuthorizationDenied), @(CBManagerAuthorizationNotDetermined), @(CBManagerAuthorizationRestricted)]) {
      permission = value.integerValue;
      void *context = (void *)1; unsigned before = managers;
      assert(nxrs_host_ble_scan_open(&context) == NXRS_BLE_DENIED && context == NULL && managers == before);
    }
    permission = CBManagerAuthorizationAllowedAlways;
    for (NSNumber *value in @[@(CBManagerStatePoweredOff), @(CBManagerStateUnsupported), @(CBManagerStateUnauthorized), @(CBManagerStateUnknown)]) {
      initial_state = value.integerValue; elapsed_us = 0;
      int expected = initial_state == CBManagerStateUnknown ? NXRS_BLE_TIMEOUT : manager_status(initial_state);
      void *context = (void *)1;
      assert(nxrs_host_ble_scan_open(&context) == expected && context == NULL);
    }

    void *context = open_fixture();
    NXRSBleContext *state = (__bridge NXRSBleContext *)context;
    assert(nxrs_host_ble_scan_start(context, heart.bytes) == NXRS_BLE_OK);
    FakeManager *old_manager = state.manager;
    NXRSBleDelegate *old_delegate = state.delegate;
    assert(nxrs_host_ble_scan_start(context, NULL) == NXRS_BLE_BUSY);
    NSDictionary *matched = @{CBAdvertisementDataServiceUUIDsKey:@[[CBUUID UUIDWithString:@"180D"]]};
    NSDictionary *overflow = @{CBAdvertisementDataOverflowServiceUUIDsKey:@[[CBUUID UUIDWithString:@"180D"]]};
    for (uint8_t i = 1; i < 40; i++) advertise(state, old_manager, old_delegate, i, @{}, @-70);
    advertise(state, old_manager, old_delegate, 1, matched, @-50);
    advertise(state, old_manager, old_delegate, 1, overflow, @-40);
    advertise(state, old_manager, old_delegate, 2, matched, @127);
    struct nxrs_host_ble_report rows[8]; uint32_t count;
    assert(nxrs_host_ble_scan_get(context, rows, 1, &count) == NXRS_BLE_OK && count == 1);
    assert(rows[0].peer[15] == 1 && rows[0].rssi_dbm == -40 && rows[0].matches_filter == 1);
    assert(nxrs_host_ble_scan_get(context, rows, 8, &count) == NXRS_BLE_OK && count == 1);
    assert(rows[0].peer[15] == 2 && rows[0].rssi_dbm == INT32_MIN);

    /* Failed stop keeps callback storage and ownership alive for an explicit retry. */
    stuck_stop = true;
    assert(nxrs_host_ble_scan_stop(context) == NXRS_BLE_IO && state.manager == old_manager && old_delegate.owner == state);
    stuck_stop = false;
    assert(nxrs_host_ble_scan_stop(context) == NXRS_BLE_OK && old_delegate.owner == nil && old_manager.delegate == nil);
    assert(nxrs_host_ble_scan_get(context, rows, 8, &count) == NXRS_BLE_UNAVAILABLE && count == 0);
    assert(nxrs_host_ble_scan_start(context, NULL) == NXRS_BLE_OK && state.manager != old_manager);
    advertise(state, old_manager, old_delegate, 3, matched, @-20);
    /* Even a stale manager passed to the new delegate is rejected. */
    advertise(state, old_manager, state.delegate, 4, matched, @-20);
    assert(nxrs_host_ble_scan_get(context, rows, 8, &count) == NXRS_BLE_OK && count == 0);

    for (uint8_t i = 1; i <= 40; i++) advertise(state, state.manager, state.delegate, i, @{}, @-65);
    for (uint8_t expected = 1; expected <= 32; expected += 8) {
      assert(nxrs_host_ble_scan_get(context, rows, 8, &count) == NXRS_BLE_OK && count == 8);
      for (uint32_t i = 0; i < count; i++) assert(rows[i].peer[15] == expected + i);
    }
    assert(nxrs_host_ble_scan_get(context, rows, 8, &count) == NXRS_BLE_OK && count == 0);
    assert(nxrs_host_ble_scan_stop(context) == NXRS_BLE_OK);
    nxrs_host_ble_scan_release(context);
    assert(old_delegate.owner == nil);

    context = open_fixture(); state = (__bridge NXRSBleContext *)context;
    failed_start = true;
    assert(nxrs_host_ble_scan_start(context, NULL) == NXRS_BLE_IO);
    nxrs_host_ble_scan_release(context);

    context = open_fixture(); state = (__bridge NXRSBleContext *)context;
    assert(nxrs_host_ble_scan_start(context, NULL) == NXRS_BLE_OK);
    dispatch_sync(state.queue, ^{ state.manager.state = CBManagerStatePoweredOff; });
    assert(nxrs_host_ble_scan_get(context, rows, 8, &count) == NXRS_BLE_UNAVAILABLE && count == 0);
    assert(nxrs_host_ble_scan_stop(context) == NXRS_BLE_OK);
    nxrs_host_ble_scan_release(context);
    assert(managers > 0 && starts > 0 && stops > 0);
  }
  return 0;
}

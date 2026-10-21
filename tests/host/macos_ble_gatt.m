#include "native_radio_abi/nxrs_ble_gatt.h"
/* Production bridge with typed SDK fakes and real serial dispatch. No radio,
 * permissions, connection or host Bluetooth state is touched. */
#import <CoreBluetooth/CoreBluetooth.h>
#include <assert.h>
#include <errno.h>
#include <stdatomic.h>
#include <unistd.h>

static CBManagerAuthorization permission = CBManagerAuthorizationAllowedAlways;
static unsigned connects, cancels, scans, reads, writes, discoveries;
static _Atomic unsigned wakes, releases;
static NSData *written;
static CBPeripheral *known_peer;
static CBService *requested_service;
static void (*wake_observer)(void *);

@interface FakeAuthorization : NSObject
+ (CBManagerAuthorization)authorization;
@end
@implementation FakeAuthorization
+ (CBManagerAuthorization)authorization { return permission; }
@end

@interface FakeManager : NSObject
@property(nonatomic, weak) id<CBCentralManagerDelegate> delegate;
@property(nonatomic) CBManagerState state;
@property(nonatomic) BOOL isScanning;
- (instancetype)initWithDelegate:(id<CBCentralManagerDelegate>)delegate queue:(dispatch_queue_t)queue options:(NSDictionary *)options;
- (NSArray *)retrievePeripheralsWithIdentifiers:(NSArray *)identifiers;
- (void)scanForPeripheralsWithServices:(NSArray *)services options:(NSDictionary *)options;
- (void)stopScan;
- (void)connectPeripheral:(CBPeripheral *)peripheral options:(NSDictionary *)options;
- (void)cancelPeripheralConnection:(CBPeripheral *)peripheral;
@end
@implementation FakeManager
- (instancetype)initWithDelegate:(id<CBCentralManagerDelegate>)delegate queue:(dispatch_queue_t)queue options:(NSDictionary *)options
{
  (void)queue; self = [super init];
  if (self) {
    self.delegate = delegate; self.state = CBManagerStatePoweredOn;
    assert([options[CBCentralManagerOptionShowPowerAlertKey] isEqual:@NO]);
  }
  return self;
}
- (NSArray *)retrievePeripheralsWithIdentifiers:(NSArray *)identifiers
{ (void)identifiers; return known_peer == nil ? @[] : @[known_peer]; }
- (void)scanForPeripheralsWithServices:(NSArray *)services options:(NSDictionary *)options
{ (void)options; assert(services == nil); self.isScanning = YES; scans++; }
- (void)stopScan { self.isScanning = NO; }
- (void)connectPeripheral:(CBPeripheral *)peripheral options:(NSDictionary *)options
{ (void)peripheral; assert(options == nil); connects++; }
- (void)cancelPeripheralConnection:(CBPeripheral *)peripheral
{ assert(peripheral != nil); cancels++; /* Not a completion or property update. */ }
@end

@interface FakeCharacteristic : NSObject
@property(nonatomic, strong) CBUUID *UUID;
@property(nonatomic) CBCharacteristicProperties properties;
@property(nonatomic) BOOL isNotifying;
@property(nonatomic, strong) NSData *value;
@end
@implementation FakeCharacteristic
@end
@interface FakeService : NSObject
@property(nonatomic, strong) CBUUID *UUID;
@property(nonatomic, strong) NSArray *characteristics;
@end
@implementation FakeService
@end
@interface FakePeripheral : NSObject
@property(nonatomic, strong) NSUUID *identifier;
@property(nonatomic, weak) id<CBPeripheralDelegate> delegate;
@property(nonatomic) CBPeripheralState state;
@property(nonatomic, strong) NSArray *services;
- (void)discoverServices:(NSArray *)services;
- (void)discoverCharacteristics:(NSArray *)characteristics forService:(CBService *)service;
- (void)readValueForCharacteristic:(CBCharacteristic *)characteristic;
- (NSUInteger)maximumWriteValueLengthForType:(CBCharacteristicWriteType)type;
- (void)writeValue:(NSData *)value forCharacteristic:(CBCharacteristic *)characteristic type:(CBCharacteristicWriteType)type;
@end
@implementation FakePeripheral
- (void)discoverServices:(NSArray *)services { assert(services.count == 1); discoveries++; }
- (void)discoverCharacteristics:(NSArray *)characteristics forService:(CBService *)service
{ assert(characteristics.count == 1 && service != nil); requested_service = service; discoveries++; }
- (void)readValueForCharacteristic:(CBCharacteristic *)characteristic { assert(characteristic != nil); reads++; }
- (NSUInteger)maximumWriteValueLengthForType:(CBCharacteristicWriteType)type
{ assert(type == CBCharacteristicWriteWithResponse); return 64; }
- (void)writeValue:(NSData *)value forCharacteristic:(CBCharacteristic *)characteristic type:(CBCharacteristicWriteType)type
{ assert(characteristic != nil && type == CBCharacteristicWriteWithResponse); written = value; writes++; }
@end

#define CBManager FakeAuthorization
#define CBCentralManager FakeManager
#include "../../hal/ble/src/platform/macos/ffi/gatt.m"

static void wake(void *arg)
{
  assert(arg == (void *)42); atomic_fetch_add(&wakes, 1);
  if (wake_observer != NULL) wake_observer(arg);
}
static void release(void *arg) { assert(arg == (void *)42); atomic_fetch_add(&releases, 1); }
static uint64_t next_session = 100;

extern uint64_t nxrs_mac_gatt_test_session(void);
extern void nxrs_mac_gatt_test_contend(bool);
extern void nxrs_mac_gatt_test_saturate(void);
static NXRSBackend *backend(void) { __block NXRSBackend *value; dispatch_sync(g_queue, ^{value=g_backend;});return value; }
static void pump(void)
{
  dispatch_sync(g_queue, ^{ process_control(); });
  dispatch_sync(g_queue, ^{}); /* execute deferred post-callback barriers */
}
static NXRSBackend *open_fixture(void)
{
  permission = CBManagerAuthorizationAllowedAlways;
  connects = cancels = scans = reads = writes = discoveries = 0;
  known_peer = nil; written = nil;
  uint64_t session = ++next_session;
  assert(nxrs_ble_gatt_open(session, 64, wake, release, (void *)42) == 0);
  NXRSBackend *state = backend(); assert(state != nil);
  assert(nxrs_ble_gatt_open(session + 1, 64, wake, release, (void *)42) == EBUSY);
  return state;
}
static struct nxrs_gatt_command command(unsigned kind, uint64_t operation)
{
  struct nxrs_gatt_command result = {0};
  result.kind = kind; result.operation = operation;
  return result;
}
static FakePeripheral *peer(uint8_t identity)
{
  FakePeripheral *value = [FakePeripheral new];
  uuid_t bytes = {0}; bytes[15] = identity;
  value.identifier = [[NSUUID alloc] initWithUUIDBytes:bytes];
  return value;
}
static struct nxrs_gatt_event next(NXRSBackend *state)
{
  (void)state;
  struct nxrs_gatt_event result;
  for (unsigned i = 0; i < 1000; i++) {
    pump();
    if (nxrs_ble_gatt_poll(nxrs_mac_gatt_test_session(), &result) != 0) return result;
    usleep(1000);
  }
  assert(!"missing terminal event"); return result;
}
static void empty(NXRSBackend *state)
{
  (void)state;
  pump(); struct nxrs_gatt_event result;
  assert(nxrs_ble_gatt_poll(nxrs_mac_gatt_test_session(), &result) == 0);
}
static void close_fixture(NXRSBackend *state)
{
  (void)state;
  unsigned before = atomic_load(&releases);
  nxrs_ble_gatt_close(nxrs_mac_gatt_test_session()); pump();
  assert(backend() == nil && atomic_load(&releases) == before + 1);
  pump(); assert(atomic_load(&releases) == before + 1);
}
static FakePeripheral *begin_connect(NXRSBackend *state, uint64_t operation, bool cached)
{
  (void)state;
  FakePeripheral *target = peer(1);
  known_peer = cached ? (CBPeripheral *)target : nil;
  struct nxrs_gatt_command request = command(NXRS_GATT_CONNECT, operation);
  [target.identifier getUUIDBytes:request.service];
  assert(nxrs_ble_gatt_submit(nxrs_mac_gatt_test_session(), &request) == 0); pump();
  if (permission == CBManagerAuthorizationAllowedAlways)
    assert(nxrs_ble_gatt_submit(nxrs_mac_gatt_test_session(), &request) == EBUSY);
  return target;
}
static void connected(NXRSBackend *state, FakePeripheral *target)
{
  dispatch_sync(g_queue, ^{
    target.state = CBPeripheralStateConnected;
    [state.delegate centralManager:(id)state.manager didConnectPeripheral:(CBPeripheral *)target];
  });
}
static void disconnected(NXRSBackend *state, FakePeripheral *target)
{
  dispatch_sync(g_queue, ^{
    target.state = CBPeripheralStateDisconnected;
    [state.delegate centralManager:(id)state.manager didDisconnectPeripheral:(CBPeripheral *)target error:nil];
  });
}

struct disconnect_observation {
  NXRSBackend *state;
  unsigned count;
  struct nxrs_gatt_event events[2];
};
static struct disconnect_observation *active_disconnect_observation;
static void observe_disconnect_wake(void *arg)
{
  assert(arg == (void *)42 && active_disconnect_observation != NULL);
  struct disconnect_observation *observation = active_disconnect_observation;
  assert(observation->count < 2);
  assert(nxrs_ble_gatt_poll(nxrs_mac_gatt_test_session(), &observation->events[observation->count]) == 1);
  if (observation->count == 0) {
    assert(observation->events[0].kind == NXRS_GATT_LINK_LOST_UNKNOWN_REASON);
    assert([observation->state peripheral] != nil);
  } else {
    assert(observation->events[1].kind == NXRS_GATT_DISCONNECT);
    assert([observation->state peripheral] == nil);
    assert([[observation->state delegate] owner] == nil);
  }
  observation->count++;
}

int main(void)
{
  @autoreleasepool {
    NXRSBackend *state = open_fixture();
    struct nxrs_gatt_command request = command(NXRS_GATT_CONNECT, 1);
    struct nxrs_gatt_event event;
    unsigned wake_before = atomic_load(&wakes);
    nxrs_mac_gatt_test_contend(true);
    pump(); assert(atomic_load(&wakes) > wake_before);
    event = next(state); assert(event.kind == NXRS_GATT_CONNECT && event.operation == 1);
    close_fixture(state);

    /* No completion will arrive after a lock-only Busy rejection. Admission
     * readiness must itself re-arm the owner, even with an empty event FIFO. */
    state = open_fixture(); wake_before = atomic_load(&wakes);
    nxrs_mac_gatt_test_contend(false);
    pump(); assert(atomic_load(&wakes) > wake_before);
    empty(state); close_fixture(state);

    /* Cancellation of a pending connect requires a real SDK terminal callback,
     * even while the peripheral property still says Disconnected. */
    state = open_fixture(); FakePeripheral *target = begin_connect(state, 1, true);
    unsigned before = atomic_load(&releases);
    nxrs_ble_gatt_recover(nxrs_mac_gatt_test_session(), 1); pump();
    assert(cancels > 0 && atomic_load(&releases) == before); empty(state);
    connected(state, target); pump(); /* late success must be cancelled again */
    assert(cancels >= 2); empty(state);
    disconnected(state, target);
    event = next(state); assert(event.kind == NXRS_GATT_QUIESCENT && event.operation == 1);
    empty(state); close_fixture(state);

    /* Explicit disconnect publishes loss before SDK objects detach, then its
     * terminal completion only after didDisconnect provides the callback barrier. */
    state = open_fixture(); target = begin_connect(state, 1, true); connected(state, target);
    event = next(state); assert(event.kind == NXRS_GATT_CONNECT && event.error == 0);
    request = command(NXRS_GATT_DISCONNECT, 2);
    assert(nxrs_ble_gatt_submit(nxrs_mac_gatt_test_session(), &request) == 0); pump();
    assert(cancels > 0); empty(state);
    struct disconnect_observation observation = { .state = state };
    active_disconnect_observation = &observation;
    wake_observer = observe_disconnect_wake;
    disconnected(state, target); pump();
    wake_observer = NULL; active_disconnect_observation = NULL;
    assert(observation.count == 2);
    assert(observation.events[0].operation == 1 && observation.events[1].operation == 2);
    empty(state); close_fixture(state);

    /* A missing OS cache entry falls back to the exact opaque identifier. */
    state = open_fixture(); target = begin_connect(state, 1, false);
    assert(scans == 1 && connects == 0);
    FakePeripheral *other = peer(2);
    dispatch_sync(g_queue, ^{
      [state.delegate centralManager:(id)state.manager didDiscoverPeripheral:(CBPeripheral *)other advertisementData:@{} RSSI:@-40];
    });
    assert(connects == 0);
    dispatch_sync(g_queue, ^{
      [state.delegate centralManager:(id)state.manager didDiscoverPeripheral:(CBPeripheral *)target advertisementData:@{} RSSI:@-40];
    });
    assert(connects == 1);
    connected(state, target);
    event = next(state); assert(event.kind == NXRS_GATT_CONNECT && event.error == 0);

    FakeService *service = [FakeService new];
    service.UUID = [CBUUID UUIDWithString:@"4E585253-0002-4000-8000-000000000001"];
    FakeService *legacy = [FakeService new];
    legacy.UUID = [CBUUID UUIDWithString:@"4E585253-0001-4000-8000-000000000001"];
    FakeCharacteristic *characteristic = [FakeCharacteristic new];
    characteristic.UUID = [CBUUID UUIDWithString:@"4E585253-0001-4000-8000-000000000002"];
    characteristic.properties = CBCharacteristicPropertyRead | CBCharacteristicPropertyWrite;
    FakeCharacteristic *legacy_characteristic = [FakeCharacteristic new];
    legacy_characteristic.UUID = characteristic.UUID;
    legacy_characteristic.properties = characteristic.properties;
    legacy.characteristics = @[legacy_characteristic];
    service.characteristics = @[characteristic]; target.services = @[legacy, service];
    request = command(NXRS_GATT_DISCOVER, 2);
    uuid_t uuid;
    [[[NSUUID alloc] initWithUUIDString:@"4E585253-0002-4000-8000-000000000001"] getUUIDBytes:uuid];
    memcpy(request.service, uuid, 16);
    [[[NSUUID alloc] initWithUUIDString:@"4E585253-0001-4000-8000-000000000002"] getUUIDBytes:uuid];
    memcpy(request.characteristic, uuid, 16);
    assert(nxrs_ble_gatt_submit(nxrs_mac_gatt_test_session(), &request) == 0); pump();
    dispatch_sync(g_queue, ^{
      [state.delegate peripheral:(CBPeripheral *)target didDiscoverServices:nil];
      assert(requested_service == (CBService *)service);
      /* An identical echo UUID in V1 must not satisfy the V2 request. */
      [state.delegate peripheral:(CBPeripheral *)target didDiscoverCharacteristicsForService:(CBService *)legacy error:nil];
    });
    empty(state);
    dispatch_sync(g_queue, ^{
      [state.delegate peripheral:(CBPeripheral *)target didDiscoverCharacteristicsForService:(CBService *)service error:nil];
    });
    event = next(state); assert(event.kind == NXRS_GATT_DISCOVER && event.handle == 1 && event.error == 0);
    assert(state.attributes.count == 1 && state.attributes[0] == (CBCharacteristic *)characteristic);

    request = command(NXRS_GATT_WRITE, 3); request.handle = 1; request.length = 20;
    memset(request.data, 0x5a, request.length);
    assert(nxrs_ble_gatt_submit(nxrs_mac_gatt_test_session(), &request) == 0); pump();
    assert(writes == 1 && written.length == 20 && memcmp(written.bytes, request.data, 20) == 0);
    dispatch_sync(g_queue, ^{ [state.delegate peripheral:(CBPeripheral *)target didWriteValueForCharacteristic:(CBCharacteristic *)characteristic error:nil]; });
    event = next(state); assert(event.kind == NXRS_GATT_WRITE && event.error == 0);

    request = command(NXRS_GATT_READ, 4); request.handle = 1;
    characteristic.value = [NSMutableData dataWithLength:64];
    assert(nxrs_ble_gatt_submit(nxrs_mac_gatt_test_session(), &request) == 0); pump();
    dispatch_sync(g_queue, ^{ [state.delegate peripheral:(CBPeripheral *)target didUpdateValueForCharacteristic:(CBCharacteristic *)characteristic error:nil]; });
    event = next(state); assert(event.kind == NXRS_GATT_READ && event.error == 0 && event.length == 64);

    request.operation = 5;
    assert(nxrs_ble_gatt_submit(nxrs_mac_gatt_test_session(), &request) == 0); pump();
    dispatch_sync(g_queue, ^{
      [state.delegate peripheral:(CBPeripheral *)target didUpdateValueForCharacteristic:(CBCharacteristic *)characteristic
        error:[NSError errorWithDomain:CBATTErrorDomain code:CBATTErrorUnlikelyError userInfo:nil]];
    });
    event = next(state); assert(event.att_error == 0x0e && event.error == 0 && state.peripheral != nil);

    request.operation = 6; characteristic.value = [NSMutableData dataWithLength:65];
    assert(nxrs_ble_gatt_submit(nxrs_mac_gatt_test_session(), &request) == 0); pump();
    dispatch_sync(g_queue, ^{ [state.delegate peripheral:(CBPeripheral *)target didUpdateValueForCharacteristic:(CBCharacteristic *)characteristic error:nil]; });
    event = next(state); assert(event.kind == NXRS_GATT_READ && event.error == EINVAL && event.length == 0);
    nxrs_ble_gatt_recover(nxrs_mac_gatt_test_session(), 6); pump(); empty(state);
    disconnected(state, target);
    event = next(state); assert(event.kind == NXRS_GATT_LINK_LOST_UNKNOWN_REASON);
    event = next(state); assert(event.kind == NXRS_GATT_QUIESCENT && event.operation == 6);
    close_fixture(state);

    /* Connect completion must precede unsolicited loss, even when both SDK
     * callbacks arrive before the deferred publication block can run. */
    state = open_fixture(); target = begin_connect(state, 1, true);
    dispatch_sync(g_queue, ^{
      target.state = CBPeripheralStateConnected;
      [state.delegate centralManager:(id)state.manager didConnectPeripheral:(CBPeripheral *)target];
      target.state = CBPeripheralStateDisconnected;
      [state.delegate centralManager:(id)state.manager didDisconnectPeripheral:(CBPeripheral *)target error:nil];
    });
    event = next(state); assert(event.kind == NXRS_GATT_CONNECT);
    event = next(state); assert(event.kind == NXRS_GATT_LINK_LOST_UNKNOWN_REASON && event.operation == 1);
    nxrs_ble_gatt_recover(nxrs_mac_gatt_test_session(), 1);
    event = next(state); assert(event.kind == NXRS_GATT_QUIESCENT);
    close_fixture(state);

    /* A dropped owner will never poll a saturated queue. Keep the native
     * lifetime until disconnect, then release without needing event capacity. */
    state = open_fixture(); target = begin_connect(state, 1, true); connected(state, target);
    event = next(state); assert(event.kind == NXRS_GATT_CONNECT);
    nxrs_mac_gatt_test_saturate();
    before = atomic_load(&releases);
    nxrs_ble_gatt_close(nxrs_mac_gatt_test_session()); pump();
    assert(backend() == state && atomic_load(&releases) == before);
    disconnected(state, target); pump();
    assert(backend() == nil && atomic_load(&releases) == before + 1);

    /* Cancel while scanning: queued old central messages become inert and
     * cannot connect or publish into a subsequent connection generation. */
    state = open_fixture(); target = begin_connect(state, 1, false);
    NXRSBackendDelegate *scanning_delegate = state.delegate;
    FakeManager *scanning_manager = state.manager;
    nxrs_ble_gatt_recover(nxrs_mac_gatt_test_session(), 1);
    event = next(state); assert(event.kind == NXRS_GATT_QUIESCENT && connects == 0);
    dispatch_sync(g_queue, ^{
      [scanning_delegate centralManager:(id)scanning_manager didDiscoverPeripheral:(CBPeripheral *)target advertisementData:@{} RSSI:@-40];
    });
    empty(state); assert(connects == 0 && scanning_delegate.owner == nil);
    close_fixture(state);

    /* Drop may retain the wake capture until disconnect, but never releases it
     * merely because cancel was queued or a native property changed. */
    state = open_fixture(); target = begin_connect(state, 1, true); connected(state, target);
    event = next(state); assert(event.kind == NXRS_GATT_CONNECT);
    NXRSBackendDelegate *old_delegate = state.delegate;
    FakeManager *old_manager = state.manager;
    before = atomic_load(&releases);
    nxrs_ble_gatt_close(nxrs_mac_gatt_test_session()); pump();
    assert(backend() == state && atomic_load(&releases) == before);
    disconnected(state, target); pump();
    assert(backend() == nil && atomic_load(&releases) == before + 1);
    dispatch_sync(g_queue, ^{ [old_delegate centralManager:(id)old_manager didConnectPeripheral:(CBPeripheral *)target]; });
    assert(old_delegate.owner == nil && target.delegate == nil && atomic_load(&releases) == before + 1);

    /* didFailToConnect is also a quiet barrier; no imaginary disconnect is
     * required when a cancelled pending attempt never established a link. */
    state = open_fixture(); target = begin_connect(state, 1, true);
    nxrs_ble_gatt_recover(nxrs_mac_gatt_test_session(), 1); pump(); empty(state);
    dispatch_sync(g_queue, ^{
      [state.delegate centralManager:(id)state.manager didFailToConnectPeripheral:(CBPeripheral *)target
        error:[NSError errorWithDomain:CBErrorDomain code:CBErrorOperationCancelled userInfo:nil]];
    });
    event = next(state); assert(event.kind == NXRS_GATT_QUIESCENT);
    close_fixture(state);

    state = open_fixture(); permission = CBManagerAuthorizationDenied;
    target = begin_connect(state, 1, true);
    event = next(state); assert(event.kind == NXRS_GATT_CONNECT && event.error == EACCES && connects == 0);
    nxrs_ble_gatt_recover(nxrs_mac_gatt_test_session(), 1);
    event = next(state); assert(event.kind == NXRS_GATT_QUIESCENT);
    close_fixture(state);
    assert(atomic_load(&wakes) > 0);
  }
  return 0;
}

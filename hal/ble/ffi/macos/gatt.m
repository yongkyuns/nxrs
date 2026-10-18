/* Bounded CoreBluetooth GATT adapter. Only SDK-queue code touches SDK objects.
 * Owner submit/poll use try-lock; recover/drop use reliable atomic priority
 * signals. Completion publication is deferred until the SDK callback returns.
 */
#include <errno.h>
#include <pthread.h>
#include <stdatomic.h>
#include "../native/nxrs_ble_gatt.h"
#include "bluetooth.h"

#define NXRS_GATT_EVENTS 4
#define NXRS_GATT_ATTRIBUTES 32
enum phase { IDLE, CONNECTING, SERVICES, CHARACTERISTICS, READING, WRITING, DISCONNECTING, RETIRING };
@class NXRSBackendDelegate;
@interface NXRSBackend : NSObject {
@public
  /* Protected by g_lock. SDK code may lock; consumer code only tries. */
  uint64_t session;
  uint16_t capacity;
  void (*wake)(void *);
  void (*release)(void *);
  void *arg;
  struct nxrs_gatt_command admitted;
  struct nxrs_gatt_event events[NXRS_GATT_EVENTS];
  unsigned head, count;
  bool busy, requested, closing, poisoned, fault_pending;
  /* SDK queue only. One ordinary completion + link loss + barrier can be
   * outstanding per admission; four FIFO slots cover that maximum. */
  struct nxrs_gatt_command command;
  struct nxrs_gatt_event result;
  enum phase phase;
  uint64_t connection, handled_recovery, recovery;
  bool connect_issued, quiet, result_ready, lost, flush_queued, released;
}
@property(nonatomic, strong) CBCentralManager *manager;
@property(nonatomic, strong) NXRSBackendDelegate *delegate;
@property(nonatomic, strong) CBPeripheral *peripheral;
@property(nonatomic, strong) CBService *service;
@property(nonatomic, strong) CBCharacteristic *active;
@property(nonatomic, strong) NSMutableArray<CBCharacteristic *> *attributes;
@property(nonatomic, strong) NSData *target;
@end
@interface NXRSBackendDelegate : NSObject <CBCentralManagerDelegate, CBPeripheralDelegate>
@property(nonatomic, weak) NXRSBackend *owner;
@end
@implementation NXRSBackend
@end

static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;
static NXRSBackend *g_backend;
static dispatch_queue_t g_queue;
/* One permanent control source, like NuttX's static work item. It retains no
 * per-session objects. Atomic recover/drop never race a freed source handle. */
static dispatch_source_t g_control;
static _Atomic uint64_t g_close_session, g_recover_session, g_recover_operation;
static _Atomic uint64_t g_retry_session;
static void process_control(void);
static void schedule_flush(NXRSBackend *state);
static void begin_retire(NXRSBackend *state);
static void start_connect(NXRSBackend *state);

static void initialize_control(void)
{
  static dispatch_once_t once;
  dispatch_once(&once, ^{
    g_queue = dispatch_queue_create("nxrs.ble.gatt", DISPATCH_QUEUE_SERIAL);
    g_control = dispatch_source_create(DISPATCH_SOURCE_TYPE_DATA_ADD, 0, 0, g_queue);
    dispatch_source_set_event_handler(g_control, ^{ @autoreleasepool { process_control(); } });
    dispatch_resume(g_control);
  });
}

static int sdk_error(NSError *error)
{
  if (error == nil) return 0;
  if (![error.domain isEqualToString:CBErrorDomain]) return EIO;
  switch (error.code) {
    case CBErrorInvalidParameters: case CBErrorInvalidHandle: return EINVAL;
    case CBErrorNotConnected: case CBErrorPeripheralDisconnected: return ENOTCONN;
    case CBErrorOutOfSpace: return ENOBUFS;
    case CBErrorOperationCancelled: return ECANCELED;
    case CBErrorConnectionTimeout: case CBErrorEncryptionTimedOut: return ETIMEDOUT;
    case CBErrorOperationNotSupported: return ENOTSUP;
    case CBErrorUnknownDevice: return ENODEV;
    case CBErrorConnectionLimitReached: return EBUSY;
    default: return EIO;
  }
}

static void complete(NXRSBackend *state, int error, NSError *sdk)
{
  if (state->result_ready || state->phase == IDLE || state->phase == RETIRING || state->released) return;
  memset(&state->result, 0, sizeof(state->result));
  state->result.operation = state->command.operation;
  state->result.kind = state->command.kind;
  if (sdk != nil && [sdk.domain isEqualToString:CBATTErrorDomain] && sdk.code > 0 && sdk.code <= UINT8_MAX)
    state->result.att_error = (uint8_t)sdk.code;
  else state->result.error = error != 0 ? error : sdk_error(sdk);
  state->result_ready = true;
  state->phase = IDLE;
  schedule_flush(state);
}

static bool enqueue(NXRSBackend *state, struct nxrs_gatt_event event, bool release_busy)
{
  pthread_mutex_lock(&g_lock);
  bool room = state->count < NXRS_GATT_EVENTS;
  if (room) {
    state->events[(state->head + state->count++) % NXRS_GATT_EVENTS] = event;
    if (release_busy) state->busy = false;
  }
  else { state->poisoned = true; state->fault_pending = true; }
  void (*wake)(void *) = state->wake;
  void *arg = state->arg;
  pthread_mutex_unlock(&g_lock);
  if (wake != NULL) wake(arg);
  return room;
}

static void detach(NXRSBackend *state)
{
  state.peripheral.delegate = nil;
  state.manager.delegate = nil;
  state.delegate.owner = nil;
  state.peripheral = nil; state.manager = nil; state.delegate = nil;
  state.service = nil; state.active = nil; state.target = nil;
  state.attributes = nil;
  state->connect_issued = false;
}

static void flush(NXRSBackend *state)
{
  state->flush_queued = false;
  if (state->released) return;
  /* A dropped owner cannot drain the FIFO. Retirement must never depend on
   * delivery capacity; discard reports, but still wait for SDK quiescence. */
  if (state->closing) { state->result_ready = false; state->lost = false; }
  if (state->result_ready) {
    if (!enqueue(state, state->result, state->recovery == 0 && !state->closing)) return;
    state->result_ready = false;
  }
  if (state->lost) {
    struct nxrs_gatt_event event = {0};
    event.operation = state->connection;
    event.kind = NXRS_GATT_LINK_LOST_UNKNOWN_REASON;
    if (!enqueue(state, event, false)) return;
    state->lost = false;
  }
  if (!state->quiet || (state->phase != RETIRING && state->phase != DISCONNECTING)) return;
  /* This block runs after didDisconnect/didFailToConnect returned. Apple
   * guarantees no further peripheral-delegate calls after didDisconnect.
   * Detaching both delegates additionally invalidates queued central messages.
   */
  detach(state);
  if (state->closing) {
    pthread_mutex_lock(&g_lock);
    state->released = true;
    void (*release)(void *) = state->release;
    void *arg = state->arg;
    state->wake = NULL; state->release = NULL; state->arg = NULL;
    if (g_backend == state) g_backend = nil;
    pthread_mutex_unlock(&g_lock);
    if (release != NULL) release(arg);
    return;
  }
  struct nxrs_gatt_event event = {0};
  if (state->phase == DISCONNECTING) {
    event.operation = state->command.operation; event.kind = NXRS_GATT_DISCONNECT;
  } else {
    event.operation = state->recovery; event.kind = NXRS_GATT_QUIESCENT;
  }
  if (!enqueue(state, event, true)) return;
  state->recovery = state->connection = 0;
  state->phase = IDLE;
}

static void schedule_flush(NXRSBackend *state)
{
  if (state->flush_queued || state->released) return;
  state->flush_queued = true;
  dispatch_async(g_queue, ^{ @autoreleasepool { flush(state); } });
}

static void begin_retire(NXRSBackend *state)
{
  if (state.manager.isScanning) [state.manager stopScan];
  if (state->connect_issued && !state->quiet) {
    /* Even a Disconnected property is not a callback barrier for a pending
     * connect. Wait for didFailToConnect/didDisconnect (or controller loss). */
    [state.manager cancelPeripheralConnection:state.peripheral];
  } else {
    state->quiet = true;
    schedule_flush(state);
  }
}

static void start_connect(NXRSBackend *state)
{
  if (state->phase != CONNECTING || state->connect_issued) return;
  NSUUID *identity = [[NSUUID alloc] initWithUUIDBytes:state->command.service];
  NSArray<CBPeripheral *> *known = [state.manager retrievePeripheralsWithIdentifiers:@[identity]];
  CBPeripheral *candidate = nil;
  for (CBPeripheral *item in known) if ([item.identifier isEqual:identity]) {
    if (candidate != nil) { complete(state, EEXIST, nil); return; }
    candidate = item;
  }
  if (candidate == nil) {
    /* OS caches may forget a peer after the scan manager is retired. Scan for
     * the copied identity, not a name. The shared client owns the deadline. */
    [state.manager scanForPeripheralsWithServices:nil options:@{CBCentralManagerScanOptionAllowDuplicatesKey:@YES}];
    return;
  }
  state.peripheral = candidate;
  candidate.delegate = state.delegate;
  state->connect_issued = true; state->quiet = false;
  [state.manager connectPeripheral:candidate options:nil];
}

static void start_command(NXRSBackend *state)
{
  if (state->command.kind == NXRS_GATT_CONNECT) {
    state->phase = CONNECTING; state->quiet = true;
    if (@available(macOS 10.15, *)) {
      if (CBManager.authorization != CBManagerAuthorizationAllowedAlways) { complete(state, EACCES, nil); return; }
    } else { complete(state, ENOTSUP, nil); return; }
    state.delegate = [NXRSBackendDelegate new]; state.delegate.owner = state;
    state.attributes = [NSMutableArray arrayWithCapacity:NXRS_GATT_ATTRIBUTES];
    state.manager = [[CBCentralManager alloc] initWithDelegate:state.delegate queue:g_queue
      options:@{CBCentralManagerOptionShowPowerAlertKey:@NO}];
    if (state.manager.state == CBManagerStatePoweredOn) start_connect(state);
    return;
  }
  if (state.peripheral == nil || state.peripheral.state != CBPeripheralStateConnected) {
    state->phase = READING; complete(state, ENOTCONN, nil); return;
  }
  if (state->command.kind == NXRS_GATT_DISCONNECT) {
    state->phase = DISCONNECTING;
    begin_retire(state);
    return;
  }
  if (state->command.kind == NXRS_GATT_DISCOVER) {
    state->phase = SERVICES;
    state.target = [NSData dataWithBytes:state->command.service length:16];
    state.service = nil;
    [state.peripheral discoverServices:@[[CBUUID UUIDWithData:state.target]]];
    return;
  }
  state->phase = state->command.kind == NXRS_GATT_READ ? READING : WRITING;
  unsigned index = state->command.handle;
  if (index == 0 || index > state.attributes.count) { complete(state, EINVAL, nil); return; }
  state.active = state.attributes[index - 1];
  if (state.active.isNotifying) { complete(state, ENOTSUP, nil); return; }
  if (state->phase == READING) {
    if (!(state.active.properties & CBCharacteristicPropertyRead)) { complete(state, ENOTSUP, nil); return; }
    [state.peripheral readValueForCharacteristic:state.active];
  } else {
    if (!(state.active.properties & CBCharacteristicPropertyWrite) ||
        state->command.length > [state.peripheral maximumWriteValueLengthForType:CBCharacteristicWriteWithResponse]) {
      complete(state, ENOTSUP, nil); return;
    }
    NSData *data = [NSData dataWithBytes:state->command.data length:state->command.length];
    [state.peripheral writeValue:data forCharacteristic:state.active type:CBCharacteristicWriteWithResponse];
  }
}

static void process_control(void)
{
  pthread_mutex_lock(&g_lock);
  NXRSBackend *state = g_backend;
  if (state == nil) { pthread_mutex_unlock(&g_lock); return; }
  if (atomic_load_explicit(&g_close_session, memory_order_acquire) == state->session) state->closing = true;
  uint64_t token = atomic_load_explicit(&g_recover_session, memory_order_acquire) == state->session
    ? atomic_load_explicit(&g_recover_operation, memory_order_acquire) : 0;
  bool recover = token != 0 && token != state->handled_recovery &&
    (token == state->command.operation || token == state->connection ||
     (state->requested && token == state->admitted.operation));
  if (state->closing || recover) {
    state->requested = false; state->busy = true;
    if (recover) { state->recovery = state->handled_recovery = token; }
    state->phase = RETIRING;
    /* Unpublished old results cannot become a successor's completion. */
    state->result_ready = false;
    pthread_mutex_unlock(&g_lock);
    begin_retire(state);
    return;
  }
  bool requested = state->requested;
  uint64_t retry = atomic_exchange_explicit(&g_retry_session, 0, memory_order_acq_rel);
  bool waiting = state->count != 0 || state->fault_pending || retry == state->session;
  void (*wake)(void *) = state->wake;
  void *arg = state->arg;
  if (requested) { state->command = state->admitted; state->requested = false; }
  pthread_mutex_unlock(&g_lock);
  if (requested) start_command(state);
  else if (state->result_ready || state->lost || state->phase == RETIRING || state->phase == DISCONNECTING) schedule_flush(state);
  /* A consumer's try-lock poll may have raced a control-only critical section.
   * Its poke must re-arm the wait even if no new SDK callback is expected. */
  if (waiting && wake != NULL) wake(arg);
}

@implementation NXRSBackendDelegate
- (void)centralManagerDidUpdateState:(CBCentralManager *)central
{
  NXRSBackend *state = self.owner;
  if (state == nil || central != state.manager) return;
  if (central.state == CBManagerStatePoweredOn) { start_connect(state); return; }
  if (central.state == CBManagerStateUnknown || central.state == CBManagerStateResetting) {
    if (state->phase == CONNECTING && !state->connect_issued) return;
  }
  state->quiet = true;
  if (state->connection != 0) state->lost = true;
  int error = central.state == CBManagerStateUnauthorized ? EACCES :
    central.state == CBManagerStateUnsupported ? ENOTSUP : ENETDOWN;
  complete(state, error, nil);
  schedule_flush(state);
}
- (void)centralManager:(CBCentralManager *)central didDiscoverPeripheral:(CBPeripheral *)peripheral
    advertisementData:(NSDictionary<NSString *, id> *)data RSSI:(NSNumber *)rssi
{
  (void)data; (void)rssi;
  NXRSBackend *state = self.owner;
  if (state == nil || central != state.manager || state->phase != CONNECTING || state->connect_issued) return;
  NSUUID *identity = [[NSUUID alloc] initWithUUIDBytes:state->command.service];
  if (![peripheral.identifier isEqual:identity]) return;
  [central stopScan];
  state.peripheral = peripheral; peripheral.delegate = self;
  state->connect_issued = true; state->quiet = false;
  [central connectPeripheral:peripheral options:nil];
}
- (void)centralManager:(CBCentralManager *)central didConnectPeripheral:(CBPeripheral *)peripheral
{
  NXRSBackend *state = self.owner;
  if (state == nil || central != state.manager || peripheral != state.peripheral) return;
  if (state->phase == RETIRING || state->phase == DISCONNECTING) { [central cancelPeripheralConnection:peripheral]; return; }
  if (state->phase != CONNECTING) return;
  state->connection = state->command.operation;
  complete(state, 0, nil);
}
- (void)centralManager:(CBCentralManager *)central didFailToConnectPeripheral:(CBPeripheral *)peripheral error:(NSError *)error
{
  NXRSBackend *state = self.owner;
  if (state == nil || central != state.manager || peripheral != state.peripheral || !state->connect_issued) return;
  if (state->phase != CONNECTING && state->phase != RETIRING && state->phase != DISCONNECTING) return;
  state->quiet = true;
  complete(state, error == nil ? ENOTCONN : 0, error);
  schedule_flush(state);
}
- (void)centralManager:(CBCentralManager *)central didDisconnectPeripheral:(CBPeripheral *)peripheral error:(NSError *)error
{
  (void)error;
  NXRSBackend *state = self.owner;
  if (state == nil || central != state.manager || peripheral != state.peripheral || state->quiet) return;
  state->quiet = true;
  if (state->connection != 0) state->lost = true;
  if (state->phase != DISCONNECTING) complete(state, ENOTCONN, nil);
  schedule_flush(state);
}
- (void)peripheral:(CBPeripheral *)peripheral didDiscoverServices:(NSError *)error
{
  NXRSBackend *state = self.owner;
  if (state == nil || peripheral != state.peripheral || state->phase != SERVICES) return;
  if (error != nil) { complete(state, 0, error); return; }
  CBService *found = nil;
  for (CBService *service in peripheral.services) if (uuid_matches(service.UUID, state.target)) {
    if (found != nil) { complete(state, EEXIST, nil); return; }
    found = service;
  }
  if (found == nil) { complete(state, ENOENT, nil); return; }
  state.service = found; state->phase = CHARACTERISTICS;
  state.target = [NSData dataWithBytes:state->command.characteristic length:16];
  [peripheral discoverCharacteristics:@[[CBUUID UUIDWithData:state.target]] forService:found];
}
- (void)peripheral:(CBPeripheral *)peripheral didDiscoverCharacteristicsForService:(CBService *)service error:(NSError *)error
{
  NXRSBackend *state = self.owner;
  if (state == nil || peripheral != state.peripheral || service != state.service || state->phase != CHARACTERISTICS) return;
  if (error != nil) { complete(state, 0, error); return; }
  CBCharacteristic *found = nil;
  for (CBCharacteristic *characteristic in service.characteristics) if (uuid_matches(characteristic.UUID, state.target)) {
    if (found != nil) { complete(state, EEXIST, nil); return; }
    found = characteristic;
  }
  if (found == nil) { complete(state, ENOENT, nil); return; }
  NSUInteger index = [state.attributes indexOfObjectIdenticalTo:found];
  if (index == NSNotFound) {
    if (state.attributes.count == NXRS_GATT_ATTRIBUTES) { complete(state, ENOBUFS, nil); return; }
    index = state.attributes.count; [state.attributes addObject:found];
  }
  complete(state, 0, nil);
  state->result.handle = (uint16_t)(index + 1);
}
- (void)peripheral:(CBPeripheral *)peripheral didUpdateValueForCharacteristic:(CBCharacteristic *)characteristic error:(NSError *)error
{
  NXRSBackend *state = self.owner;
  if (state == nil || peripheral != state.peripheral || characteristic != state.active || state->phase != READING) return;
  if (error != nil) { complete(state, 0, error); return; }
  NSData *value = characteristic.value;
  if (value == nil || value.length > state->capacity) { complete(state, value == nil ? EIO : EINVAL, nil); return; }
  complete(state, 0, nil);
  state->result.length = (uint16_t)value.length;
  if (value.length != 0) memcpy(state->result.data, value.bytes, value.length);
}
- (void)peripheral:(CBPeripheral *)peripheral didWriteValueForCharacteristic:(CBCharacteristic *)characteristic error:(NSError *)error
{
  NXRSBackend *state = self.owner;
  if (state == nil || peripheral != state.peripheral || characteristic != state.active || state->phase != WRITING) return;
  complete(state, 0, error);
}
@end

int nxrs_ble_gatt_open(uint64_t session, uint16_t capacity, void (*wake)(void *), void (*release)(void *), void *arg)
{
  if (session == 0 || capacity > NXRS_GATT_CAPACITY || wake == NULL || release == NULL) return EINVAL;
  initialize_control();
  if (pthread_mutex_trylock(&g_lock) != 0) return EBUSY;
  if (g_backend != nil) { pthread_mutex_unlock(&g_lock); return EBUSY; }
  NXRSBackend *state = [NXRSBackend new];
  state->session = session; state->capacity = capacity;
  state->wake = wake; state->release = release; state->arg = arg;
  state->quiet = true;
  g_backend = state;
  pthread_mutex_unlock(&g_lock);
  return 0;
}

int nxrs_ble_gatt_submit(uint64_t session, const struct nxrs_gatt_command *command)
{
  if (command == NULL || command->operation == 0 || command->kind < NXRS_GATT_CONNECT ||
      command->kind > NXRS_GATT_DISCONNECT || command->length > NXRS_GATT_CAPACITY) return EINVAL;
  if (pthread_mutex_trylock(&g_lock) != 0) {
    /* Admission took nothing. Wake the retained-command owner after the SDK
     * critical section even when there is no event or active operation. */
    atomic_store_explicit(&g_retry_session, session, memory_order_release);
    dispatch_source_merge_data(g_control, 1);
    return EBUSY;
  }
  NXRSBackend *state = g_backend;
  int result = state == nil || state->session != session || state->closing ? ENOTCONN :
    state->poisoned ? EIO : state->busy || state->requested ? EBUSY : 0;
  if (result == 0) {
    if (command->length > state->capacity) result = EMSGSIZE;
    else { state->admitted = *command; state->requested = state->busy = true; }
  }
  pthread_mutex_unlock(&g_lock);
  if (result == 0) dispatch_source_merge_data(g_control, 1);
  return result;
}

void nxrs_ble_gatt_recover(uint64_t session, uint64_t operation)
{
  atomic_store_explicit(&g_recover_operation, operation, memory_order_release);
  atomic_store_explicit(&g_recover_session, session, memory_order_release);
  dispatch_source_merge_data(g_control, 1);
}

int nxrs_ble_gatt_poll(uint64_t session, struct nxrs_gatt_event *event)
{
  if (event == NULL) return 0;
  if (pthread_mutex_trylock(&g_lock) != 0) {
    dispatch_source_merge_data(g_control, 1);
    return 0;
  }
  NXRSBackend *state = g_backend;
  int found = state != nil && state->session == session && (state->count != 0 || state->fault_pending);
  bool retry = found && state->poisoned;
  if (found) {
    if (state->fault_pending) {
      memset(event, 0, sizeof(*event)); state->fault_pending = false; /* shared decoder reports Protocol */
    } else {
      *event = state->events[state->head]; state->head = (state->head + 1) % NXRS_GATT_EVENTS; state->count--;
    }
  }
  pthread_mutex_unlock(&g_lock);
  /* Only backpressured publication needs SDK work after a successful drain.
   * A normal poll must not create a new lock contender for the next submit. */
  if (retry) dispatch_source_merge_data(g_control, 1);
  return found;
}

void nxrs_ble_gatt_close(uint64_t session)
{
  atomic_store_explicit(&g_close_session, session, memory_order_release);
  dispatch_source_merge_data(g_control, 1);
}

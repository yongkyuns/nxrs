/* CoreBluetooth SDK entry points and delegate/dispatch lifetime glue only. */
#import <CoreBluetooth/CoreBluetooth.h>
#include <errno.h>
#include <stdbool.h>
#include "../native/nxrs_ble_gatt.h"
@class NXRSBackendDelegate;
@interface NXRSBackend : NSObject
@property(nonatomic,strong) CBCentralManager *manager;
@property(nonatomic,strong) NXRSBackendDelegate *delegate;
@property(nonatomic,strong) CBPeripheral *peripheral;
@property(nonatomic,strong) CBService *service;
@property(nonatomic,strong) CBCharacteristic *active;
@property(nonatomic,strong) NSMutableArray<CBCharacteristic *> *attributes;
@end
@interface NXRSBackendDelegate : NSObject <CBCentralManagerDelegate, CBPeripheralDelegate>
@property(nonatomic,weak) NXRSBackend *owner;
@end
@implementation NXRSBackend
@end
static NXRSBackend *g_backend;
static dispatch_queue_t g_queue;
static dispatch_source_t g_control;
extern void nxrs_mac_gatt_control_rust(void);
extern void nxrs_mac_gatt_flush_rust(void);
extern void nxrs_mac_gatt_event_rust(unsigned, void *, void *, void *, int, uint8_t, const uint8_t *, size_t);
static void process_control(void) { nxrs_mac_gatt_control_rust(); }
void nxrs_mac_gatt_initialize(void)
{
  static dispatch_once_t once;
  dispatch_once(&once, ^{
    g_queue=dispatch_queue_create("nxrs.ble.gatt",DISPATCH_QUEUE_SERIAL);
    g_control=dispatch_source_create(DISPATCH_SOURCE_TYPE_DATA_ADD,0,0,g_queue);
    dispatch_source_set_event_handler(g_control, ^{ @autoreleasepool {process_control();} });
    dispatch_resume(g_control);
  });
}
void nxrs_mac_gatt_poke(void) { dispatch_source_merge_data(g_control,1); }
void nxrs_mac_gatt_defer(void) { dispatch_async(g_queue, ^{ @autoreleasepool {nxrs_mac_gatt_flush_rust();} }); }
void nxrs_mac_gatt_allocate(void) { g_backend=[NXRSBackend new]; }
int nxrs_mac_gatt_errno(unsigned kind)
{
  const int errors[]={0,EINVAL,EBUSY,EIO,ENOTCONN,EMSGSIZE,EEXIST,ENOENT,ENOBUFS,ENOTSUP,EACCES,ENETDOWN};
  return kind < sizeof(errors)/sizeof(errors[0]) ? errors[kind] : EIO;
}
static int sdk_error(NSError *error)
{
  if (error==nil) return 0;
  if (![error.domain isEqualToString:CBErrorDomain]) return EIO;
  switch(error.code) {
    case CBErrorInvalidParameters:case CBErrorInvalidHandle:return EINVAL;
    case CBErrorNotConnected:case CBErrorPeripheralDisconnected:return ENOTCONN;
    case CBErrorOutOfSpace:return ENOBUFS;
    case CBErrorOperationCancelled:return ECANCELED;
    case CBErrorConnectionTimeout:case CBErrorEncryptionTimedOut:return ETIMEDOUT;
    case CBErrorOperationNotSupported:return ENOTSUP;
    case CBErrorUnknownDevice:return ENODEV;
    case CBErrorConnectionLimitReached:return EBUSY;
    default:return EIO;
  }
}
static void event(unsigned kind,CBCentralManager *manager,CBPeripheral *peripheral,id object,NSError *error,const uint8_t *data,size_t length)
{
  uint8_t att=error!=nil && [error.domain isEqualToString:CBATTErrorDomain] && error.code>0 && error.code<=UINT8_MAX ? (uint8_t)error.code : 0;
  nxrs_mac_gatt_event_rust(kind,(__bridge void *)manager,(__bridge void *)peripheral,(__bridge void *)object,att?0:sdk_error(error),att,data,length);
}
@implementation NXRSBackendDelegate
- (void)centralManagerDidUpdateState:(CBCentralManager *)central { if(self.owner!=nil) event(1,central,nil,nil,nil,NULL,0); }
- (void)centralManager:(CBCentralManager *)central didDiscoverPeripheral:(CBPeripheral *)peripheral advertisementData:(NSDictionary<NSString *,id> *)data RSSI:(NSNumber *)rssi
{ (void)data;(void)rssi;if(self.owner!=nil)event(2,central,peripheral,nil,nil,NULL,0); }
- (void)centralManager:(CBCentralManager *)central didConnectPeripheral:(CBPeripheral *)peripheral
{if(self.owner!=nil)event(3,central,peripheral,nil,nil,NULL,0);}
- (void)centralManager:(CBCentralManager *)central didFailToConnectPeripheral:(CBPeripheral *)peripheral error:(NSError *)error
{if(self.owner!=nil)event(4,central,peripheral,nil,error,NULL,0);}
- (void)centralManager:(CBCentralManager *)central didDisconnectPeripheral:(CBPeripheral *)peripheral error:(NSError *)error
{if(self.owner!=nil)event(5,central,peripheral,nil,error,NULL,0);}
- (void)peripheral:(CBPeripheral *)peripheral didDiscoverServices:(NSError *)error
{if(self.owner!=nil)event(6,nil,peripheral,nil,error,NULL,0);}
- (void)peripheral:(CBPeripheral *)peripheral didDiscoverCharacteristicsForService:(CBService *)service error:(NSError *)error
{if(self.owner!=nil)event(7,nil,peripheral,service,error,NULL,0);}
- (void)peripheral:(CBPeripheral *)peripheral didUpdateValueForCharacteristic:(CBCharacteristic *)characteristic error:(NSError *)error
{if(self.owner!=nil)event(8,nil,peripheral,characteristic,error,characteristic.value.bytes,characteristic.value==nil?SIZE_MAX:characteristic.value.length);}
- (void)peripheral:(CBPeripheral *)peripheral didWriteValueForCharacteristic:(CBCharacteristic *)characteristic error:(NSError *)error
{if(self.owner!=nil)event(9,nil,peripheral,characteristic,error,NULL,0);}
@end
int nxrs_mac_gatt_authorization(void)
{
  if(@available(macOS 10.15,*)) return CBManager.authorization==CBManagerAuthorizationAllowedAlways?0:EACCES;
  return ENOTSUP;
}
void nxrs_mac_gatt_manager(void)
{
  g_backend.delegate=[NXRSBackendDelegate new];g_backend.delegate.owner=g_backend;
  g_backend.attributes=[NSMutableArray array];
  g_backend.manager=[[CBCentralManager alloc]initWithDelegate:g_backend.delegate queue:g_queue options:@{CBCentralManagerOptionShowPowerAlertKey:@NO}];
}
unsigned nxrs_mac_gatt_manager_state(void)
{
  switch(g_backend.manager.state) {case CBManagerStatePoweredOn:return 0;case CBManagerStateUnknown:case CBManagerStateResetting:return 1;
    case CBManagerStateUnauthorized:return 2;case CBManagerStateUnsupported:return 3;default:return 4;}
}
void *nxrs_mac_gatt_object(unsigned field,size_t index)
{
  switch(field){case 1:return (__bridge void *)g_backend.manager;case 2:return (__bridge void *)g_backend.peripheral;
    case 3:return (__bridge void *)g_backend.service;case 4:return (__bridge void *)g_backend.active;
    case 5:return (__bridge void *)g_backend.peripheral.services[index];case 6:return (__bridge void *)g_backend.service.characteristics[index];default:return NULL;}
}
size_t nxrs_mac_gatt_count(unsigned field) {return field==5?g_backend.peripheral.services.count:g_backend.service.characteristics.count;}
void nxrs_mac_gatt_identity(void *object,uint8_t *bytes) { [((__bridge CBPeripheral *)object).identifier getUUIDBytes:bytes]; }
void nxrs_mac_gatt_uuid(void *object,bool characteristic,const uint8_t **bytes,size_t *length)
{NSData *data=characteristic?((__bridge CBCharacteristic *)object).UUID.data:((__bridge CBService *)object).UUID.data;*bytes=data.bytes;*length=data.length;}
void *nxrs_mac_gatt_retrieve(const uint8_t *identity,size_t index,size_t *count)
{
  NSUUID *uuid=[[NSUUID alloc]initWithUUIDBytes:identity];NSArray *known=[g_backend.manager retrievePeripheralsWithIdentifiers:@[uuid]];*count=known.count;
  return index<known.count?(__bridge void *)known[index]:NULL;
}
void nxrs_mac_gatt_scan(void) {[g_backend.manager scanForPeripheralsWithServices:nil options:@{CBCentralManagerScanOptionAllowDuplicatesKey:@YES}];}
void nxrs_mac_gatt_stop_scan(void) {if(g_backend.manager.isScanning)[g_backend.manager stopScan];}
void nxrs_mac_gatt_connect(void *peripheral)
{g_backend.peripheral=(__bridge CBPeripheral *)peripheral;g_backend.peripheral.delegate=g_backend.delegate;[g_backend.manager connectPeripheral:g_backend.peripheral options:nil];}
bool nxrs_mac_gatt_connected(void) {return g_backend.peripheral!=nil&&g_backend.peripheral.state==CBPeripheralStateConnected;}
void nxrs_mac_gatt_cancel(void) {[g_backend.manager cancelPeripheralConnection:g_backend.peripheral];}
void nxrs_mac_gatt_services(const uint8_t *uuid) {[g_backend.peripheral discoverServices:@[[CBUUID UUIDWithData:[NSData dataWithBytes:uuid length:16]]]];}
void nxrs_mac_gatt_characteristics(void *service,const uint8_t *uuid)
{g_backend.service=(__bridge CBService *)service;[g_backend.peripheral discoverCharacteristics:@[[CBUUID UUIDWithData:[NSData dataWithBytes:uuid length:16]]] forService:g_backend.service];}
void nxrs_mac_gatt_pin(void *attribute) {[g_backend.attributes addObject:(__bridge CBCharacteristic *)attribute];}
unsigned nxrs_mac_gatt_properties(void *attribute)
{CBCharacteristic *c=(__bridge CBCharacteristic *)attribute;return (c.isNotifying?1u:0u)|((c.properties&CBCharacteristicPropertyRead)?2u:0u)|((c.properties&CBCharacteristicPropertyWrite)?4u:0u);}
size_t nxrs_mac_gatt_max_write(void) {return [g_backend.peripheral maximumWriteValueLengthForType:CBCharacteristicWriteWithResponse];}
void nxrs_mac_gatt_read(void *attribute) {g_backend.active=(__bridge CBCharacteristic *)attribute;[g_backend.peripheral readValueForCharacteristic:g_backend.active];}
void nxrs_mac_gatt_write(void *attribute,const uint8_t *data,size_t length)
{g_backend.active=(__bridge CBCharacteristic *)attribute;[g_backend.peripheral writeValue:[NSData dataWithBytes:data length:length] forCharacteristic:g_backend.active type:CBCharacteristicWriteWithResponse];}
void nxrs_mac_gatt_detach(bool release)
{
  g_backend.peripheral.delegate=nil;g_backend.manager.delegate=nil;g_backend.delegate.owner=nil;
  g_backend.peripheral=nil;g_backend.manager=nil;g_backend.delegate=nil;g_backend.service=nil;g_backend.active=nil;g_backend.attributes=nil;
  if(release)g_backend=nil;
}

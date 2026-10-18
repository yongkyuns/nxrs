/* Private SDK normalization shared by scanning and GATT. */
#ifndef NXRS_MACOS_BLUETOOTH_H
#define NXRS_MACOS_BLUETOOTH_H
#import <CoreBluetooth/CoreBluetooth.h>
#include <stdbool.h>
#include <string.h>

static bool uuid_matches(CBUUID *uuid, NSData *service)
{
  NSData *data = uuid.data;
  uint8_t canonical[16] = {0, 0, 0, 0, 0, 0, 0x10, 0, 0x80, 0, 0, 0x80, 0x5f, 0x9b, 0x34, 0xfb};
  /* SDK data is in UUID/network order, not advertising packet byte order. */
  if (data.length == 2) memcpy(canonical + 2, data.bytes, 2);
  else if (data.length == 4) memcpy(canonical, data.bytes, 4);
  else if (data.length == 16) memcpy(canonical, data.bytes, 16);
  else return false;
  return service.length == 16 && memcmp(canonical, service.bytes, 16) == 0;
}
#endif

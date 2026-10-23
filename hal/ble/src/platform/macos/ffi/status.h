#pragma once
/* Private CoreWLAN/CoreBluetooth status translation consumed by Rust. */
enum host_ble_status {
  NXRS_BLE_OK = 0, NXRS_BLE_UNSUPPORTED = 1, NXRS_BLE_BUSY = 2,
  NXRS_BLE_UNAVAILABLE = 3, NXRS_BLE_DENIED = 4, NXRS_BLE_INVALID = 5,
  NXRS_BLE_TIMEOUT = 6, NXRS_BLE_IO = 7
};

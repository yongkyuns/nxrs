/* Private, fixed-width scan bridge. Peer bytes are OS identities, not MACs.
 * Calls run on the shared scan worker; no Rust callback or buffer is retained.
 */
#ifndef NXRS_HOST_BLE_SCAN_H
#define NXRS_HOST_BLE_SCAN_H
#include <stddef.h>
#include <stdint.h>

enum nxrs_host_ble_status {
  NXRS_BLE_OK = 0, NXRS_BLE_UNSUPPORTED = 1, NXRS_BLE_BUSY = 2,
  NXRS_BLE_UNAVAILABLE = 3, NXRS_BLE_DENIED = 4, NXRS_BLE_INVALID = 5,
  NXRS_BLE_TIMEOUT = 6, NXRS_BLE_IO = 7
};

struct nxrs_host_ble_report {
  uint8_t peer[16];
  int32_t rssi_dbm; /* INT32_MIN when unavailable */
  uint32_t matches_filter; /* exactly 0 or 1 */
};
_Static_assert(sizeof(struct nxrs_host_ble_report) == 24, "BLE report size");
_Static_assert(_Alignof(struct nxrs_host_ble_report) == 4, "BLE report alignment");
_Static_assert(offsetof(struct nxrs_host_ble_report, rssi_dbm) == 16, "BLE RSSI offset");
_Static_assert(offsetof(struct nxrs_host_ble_report, matches_filter) == 20, "BLE filter offset");

int nxrs_host_ble_scan_open(void **context);
int nxrs_host_ble_scan_start(void *context, const uint8_t *service); /* NULL or 16 bytes */
int nxrs_host_ble_scan_get(void *context, struct nxrs_host_ble_report *rows,
                           uint32_t capacity, uint32_t *count);
int nxrs_host_ble_scan_stop(void *context);
/* Only after successful stop, or failed start before any scan was admitted. */
void nxrs_host_ble_scan_release(void *context);
#endif

/* Private synchronous desktop bridge. No OS handles escape into the API. */
#ifndef NXRS_HOST_WIFI_H
#define NXRS_HOST_WIFI_H
#include <stddef.h>
#include <stdint.h>

#include "../../../hal/wifi/src/platform/macos/ffi/status.h"

struct nxrs_host_wifi_ap {
  uint32_t ssid_length;
  uint32_t security; /* 0 open, 1 WPA2 Personal, 2 other */
  int32_t rssi_dbm;
  uint8_t ssid[32];
};

_Static_assert(sizeof(struct nxrs_host_wifi_ap) == 44 &&
  _Alignof(struct nxrs_host_wifi_ap) == 4 &&
  offsetof(struct nxrs_host_wifi_ap, security) == 4 &&
  offsetof(struct nxrs_host_wifi_ap, rssi_dbm) == 8 &&
  offsetof(struct nxrs_host_wifi_ap, ssid) == 12, "desktop Wi-Fi row ABI");
_Static_assert(NXRS_WIFI_OK == 0 && NXRS_WIFI_UNSUPPORTED == 1 && NXRS_WIFI_BUSY == 2 &&
  NXRS_WIFI_UNAVAILABLE == 3 && NXRS_WIFI_DENIED == 4 && NXRS_WIFI_AUTH == 5 &&
  NXRS_WIFI_INVALID == 6 && NXRS_WIFI_TIMEOUT == 7 && NXRS_WIFI_PROTOCOL == 8 &&
  NXRS_WIFI_IO == 9, "desktop Wi-Fi error ABI");

/* Permission, not a request to force persistence on a volatile provider. */
enum nxrs_host_wifi_storage { NXRS_WIFI_VOLATILE = 0, NXRS_WIFI_SYSTEM_MANAGED = 1 };
int nxrs_host_wifi_open(void **context, uint32_t storage);
int nxrs_host_wifi_scan(void *context, struct nxrs_host_wifi_ap *rows,
                        uint32_t capacity, uint32_t *count, uint32_t *truncated);
int nxrs_host_wifi_connect(void *context, const uint8_t *ssid, size_t ssid_length,
                          const uint8_t *password, size_t password_length);
int nxrs_host_wifi_disconnect(void *context);
int nxrs_host_wifi_link(void *context, uint32_t *state); /* 1 disconnected, 2 associated, 3 addressed */
/* Stop only an association owned by this context, never an observed host link.
 * A failure retains context/callback ownership; retry before releasing Rust's
 * worker lease. No radio power change. Volatile sessions never save credentials;
 * system-managed sessions may leave saved credentials even after failure/stop. */
int nxrs_host_wifi_release(void *context);
#endif

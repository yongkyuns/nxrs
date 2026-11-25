# NuttX ESP32S3 WiFi STA Connection Issue

**Date:** 2026-01-25
**Status:** RESOLVED - Wrong password
**Affected:** NuttX ESP32S3 WiFi Station Mode

## Resolution

The issue was an incorrect passphrase. The working passphrase is intentionally not stored in this repository.
Network-specific values in the excerpts below are redacted.

```
esp_evt_work_cb: Wi-Fi station connected
IP: <device-ip> (via DHCP)
Gateway: <gateway-ip>
Ping 8.8.8.8: 10-20ms, 0% packet loss
```

---

## Summary

WiFi scanning works correctly on NuttX ESP32S3, but STA (station) connection to an access point times out with `ETIMEDOUT` (errno 110). The official NuttX `esp32s3-devkit:wifi` demo exhibits the same behavior, confirming this is a NuttX driver issue.

## Symptoms

- `wapi scan wlan0` works - finds nearby networks including the target AP
- `wapi essid wlan0 <configured-network> 1` triggers connection but times out after 10 seconds
- `wapi show wlan0` shows `ESSID_OFF` flag after connection attempt
- No IP address obtained via DHCP

## Technical Analysis

### Connection Flow (traced through driver code)

1. `SIOCSIWESSID` with `IW_ESSID_ON` flag received
2. `netdev_upperhalf.c:924-944` calls `ops->essid()` then `ops->connect()`
3. `esp_wlan_netdev.c:938` - `esp_wifi_sta_essid()` sets SSID config
4. `esp_wlan_netdev.c:430` - `esp_wlan_connect()` calls `esp_wifi_sta_connect()`
5. `esp_wifi_api.c:682` - `esp_wifi_connect()` (ESP HAL) is invoked
6. `esp_wlan_netdev.c:443-453` - Waits for `IFF_RUNNING` flag (set by `WIFI_EVENT_STA_CONNECTED`)
7. **Timeout after 10 seconds** - `WIFI_EVENT_STA_CONNECTED` never arrives

### Key Files

| File | Function | Role |
|------|----------|------|
| `drivers/net/netdev_upperhalf.c:924` | SIOCSIWESSID handler | Triggers essid + connect |
| `arch/xtensa/src/common/espressif/esp_wlan_netdev.c:417` | `esp_wlan_connect()` | Calls ESP HAL connect |
| `arch/xtensa/src/common/espressif/esp_wifi_api.c:674` | `esp_wifi_sta_connect()` | Wraps `esp_wifi_connect()` |
| `arch/xtensa/src/common/espressif/esp_wifi_event_handler.c:202` | Event handler | Should receive `WIFI_EVENT_STA_CONNECTED` |

### Configuration

```
CONFIG_ESPRESSIF_WIFI=y
CONFIG_ESPRESSIF_WIFI_STATION=y
CONFIG_ESPRESSIF_WIFI_ENABLE_WPA3_SAE=y
CONFIG_ESPRESSIF_WIFI_CONNECT_TIMEOUT=10
CONFIG_WIRELESS_WAPI=y
```

## What Works

- WiFi hardware initialization
- WiFi PHY calibration
- Network scanning (finds all nearby APs with SSID, BSSID, channel, RSSI, auth mode)
- Setting WiFi mode, passphrase, cipher

## What Fails

- Actual connection to AP
- `WIFI_EVENT_STA_CONNECTED` event never received
- `IFF_RUNNING` flag never set on wlan0 interface

## Ruled Out

- **Our HAL code** - Official NuttX demo has same issue
- **WPA3-SAE fix** - Already applied (`sae_h2e_identifier` memset present in code)
- **Configuration** - Matches official `esp32s3-devkit:wifi` defconfig

## Possible Causes

1. **Event loop issue** - Events not being processed correctly
2. **ESP HAL initialization** - Missing step that ESP-IDF performs (NVS init, event loop create)
3. **WPA3 compatibility** - Target network may use WPA3-only
4. **Driver refactor regression** - Recent Sep 2025 refactor may have introduced issue

## Debug Output (2026-01-25)

With `CONFIG_DEBUG_WIRELESS_INFO` enabled, the connection attempt shows:

```
esp_wifi_sta_auth: set authmode to WIFI_AUTH_WPA2_PSK
esp_wifi_sta_password: <redacted>
esp_wifi_sta_essid: <redacted>
esp_wifi_sta_connect: Wi-Fi station connecting
esp_evt_work_cb: Wi-Fi station disconnected, reason: 3
esp_wlan_connect: Connection timeout after 10 seconds
esp_evt_work_cb: Wi-Fi station disconnected, reason: 15
```

### WiFi Disconnect Reason Codes

| Code | Constant | Meaning |
|------|----------|---------|
| 3 | `WIFI_REASON_AUTH_LEAVE` | Authentication left |
| 8 | `WIFI_REASON_ASSOC_LEAVE` | Association left |
| **15** | **`WIFI_REASON_4WAY_HANDSHAKE_TIMEOUT`** | **4-way handshake timeout** |

### Root Cause Identified

**The 4-way WPA handshake is timing out (reason 15).** This indicates:
1. The ESP32S3 connects to the AP and starts authentication
2. The WPA/WPA2 4-way handshake begins but never completes
3. After handshake timeout, driver reports disconnect with reason 15

### Possible Causes

1. **Password issue** - Verify the passphrase privately before testing
2. **WPA3-SAE mode** - Router may require WPA3-only which has known issues
3. **PMF (Protected Management Frames)** - Router may require PMF which ESP32S3 might not support correctly
4. **Driver timing issue** - Handshake packets not processed in time

## Next Steps

- [x] Enable `CONFIG_DEBUG_WIRELESS_INFO` and trace events during connection
- [ ] **Verify the passphrase privately** for the configured network
- [ ] Test with WPA2-only network (mobile hotspot with WPA2-PSK only)
- [ ] Check router settings for WPA3-only mode or PMF requirements
- [ ] Compare with ESP-IDF station example initialization sequence
- [ ] File GitHub issue on apache/nuttx if confirmed as driver bug

## Related Links

- [NuttX ESP32S3-DevKit Docs](https://nuttx.apache.org/docs/latest/platforms/xtensa/esp32s3/boards/esp32s3-devkit/index.html)
- [WPA3-SAE Fix Commit](https://www.mail-archive.com/commits@nuttx.apache.org/msg101592.html)
- [WiFi Driver Refactor PR](https://www.mail-archive.com/commits@nuttx.apache.org/msg144080.html)

## Test Environment

- Board: ESP32-S3-DevKitC-1
- NuttX: master branch (commit 0c68623b0c)
- Target Network: configured WPA2/WPA3-PSK network

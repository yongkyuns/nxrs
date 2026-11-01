# Camera firmware

This product entry composes the shared camera, connectivity, TLS enrollment,
detection, trial confirmation and OTA behavior. Build the signed default with:

```sh
bash platform/nuttx/camera/build.sh --out /private/tmp/camera-firmware
```

Supply the operator signing inputs described in
[signed updates](../../../docs/firmware-updates.md). The profile boots `camera` directly; it loads saved records before network
initialization and opens USB only for missing first-time commissioning records.
TLS enrollment, trusted time, reconnect and OTA then use Wi-Fi.

The single-core product has four application tasks: main 32 KiB, Wi-Fi 8 KiB,
TLS 64 KiB and PICO detection 128 KiB (232 KiB of configured stacks).
Priorities are main 100, detector 90 and TLS 80; the Wi-Fi provider owns its
worker configuration. Kernel/SDK tasks are additional. Size and scheduling
qualification must use the actual NuttX image and device.
Timed runs, reconnect injection, metrics and synthetic model witnesses remain
in the [stream fixture](../../../tests/nuttx-std/stream/README.md).

# NuttX home camera platform

This library supplies native startup, commissioning console access, worker
scheduling and execution budgets to [`app/home-cam`](../../../app/home-cam).
It contains no application lifecycle. Profiles and board initialization own
hardware configuration and driver registration; HAL implementations remain in
their capability crates.

```sh
bash platform/nuttx/home-cam/build.sh --out /private/tmp/home-cam
```

Supply operator signing inputs from [signed updates](../../../docs/firmware-updates.md).
The signed profile calls `home_cam_main`. Its four application tasks retain
32 KiB main, 8 KiB Wi-Fi, 64 KiB TLS and 128 KiB detection stacks (232 KiB total).
Main priority is 100, detector 90 and TLS 80. Kernel/SDK tasks are additional.
Qualification requires the actual NuttX build and device.

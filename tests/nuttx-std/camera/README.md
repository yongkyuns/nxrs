# Physical camera qualification

Targets the Freenove ESP32-S3-WROOM board with OV3660. Board wiring is selected
by `esp32s3-camera`; the application uses only `camera::open()` and `Camera`.
No Wi-Fi credentials, desktop camera framework or second RTOS is required.

The acquisition-only results below describe the earlier single-DMA-buffer
image. The current live adapter uses two FIFO DMA buffers; its separate JPEG
performance qualification is recorded in [`../stream`](../stream). Do not
treat an older image's measurements as qualification of a newer image.

Verified on the connected Freenove ESP32-S3/OV3660: 90 real 320×240 RGB565
frames across 30 start/stop cycles in one boot. The checksum-matched image was
visually inspected; kernel/user heap use grew by **0/0 bytes** after warm-up.
This is a short functional qualification, not a streaming or long-duration soak
test. A successful build alone is not a passed hardware test.

## Build and run

Use the existing ESP Rust/NuttX toolchain environment, Python 3.11+ and the
host's GNU make/sed/Kconfig tools. The standalone builder limits compilation to
two workers. The minimal ABI profile removes only the expectation that this
single-threaded fixture imports pthread create/join; native ABI checks still run.

```sh
bash tests/nuttx-std/camera/build.sh
```

Back up the connected board before replacing its firmware. The `nuttx.bin`
image uses ESP simple boot at offset 0; flash only the verified ESP32-S3 device,
not the Pico. With the new firmware at NSH, use the Python environment containing
pyserial (the esptool environment is suitable):

```sh
python tests/nuttx-std/camera/run.py --port /dev/cu.usbmodem14201 \
  --repeat 10 --output /private/tmp/camera-frame.ppm
```

The runner requires exactly nine complete 320×240 RGB565 frames across three
start/stop cycles, bounded polling, successful shell status, and an exact
checksum-matched frame dump. It saves a mode-600 PPM image for inspection without
extra imaging dependencies. Images may contain private scenes: keep them outside
Git. A recognizable image is required in addition to the console PASS marker.
With `--repeat 10`, it also requires 90 frames across 30 start/stop cycles in
one boot and no growth in either heap between the first and final invocation.

JPEG capture uses the same image and runner, adding `--jpeg` and a new `.jpg`
output path. With the JPEG patches, both raw and JPEG modes passed 18 frames
across six start/stop cycles with zero kernel/user heap growth after warm-up.
The saved sensor JPEG fully decoded as 320×240 and was visually inspected. The
runner checks exact framing and checksum; it does not replace a real JPEG codec.
Wi-Fi streaming qualification is separate: see [`../stream`](../stream).

## Scope

This fixture qualifies raw/JPEG acquisition, not MJPEG streaming. Each raw frame is
153,600 bytes; the current provider owns two DMA buffers and the fixture one destination.
The sensor stays clocked between individual captures. Stream-off stops DMA;
closing the device puts the sensor in standby. Timestamps describe observation,
not exposure. The 50 ms polling bound is a fixture failure threshold, not a
hard-real-time guarantee or a measured latency claim.

The profile places user allocations in PSRAM and kernel allocations in internal
RAM. The current adapter plus fixture's three raw payload capacities total
460,800 bytes; this depends on resolution
and pixel format, not a fixed HAL cost. The interrupt ownership fix adds one
fixed pointer per IRQ/CPU, not a growing allocation per camera reopen.

Dependency changes are patches 0067–0072 under `upstream/nuttx/patches/` and
0002 under `upstream/esp-hal-3rdparty/patches/`, applied to isolated build copies.
They cover the sensor, board registration, clock/DMA/cache correctness and
capture/interrupt cleanup. Native sensor and lifecycle tests mock hardware and
run with address/UB sanitizers; Rust ownership tests mock the ioctl boundary.
Neither replaces the physical test.

JPEG adds patches 0073–0075: propagate start failures, configure the sensor's
JPEG mode, and retain only the actual compressed frame extent. CAM must remain
running with DMA off while waiting for VSYNC; at the first VSYNC, reset the
FIFO and arm DMA. Leaving CAM stopped would also disable the interrupt needed
to start it. Native tests cover this ordering and reject truncated JPEG extents.
Patch 0079 rolls back a failed QBUF without retaining the caller's new buffer;
native fault-injection tests cover FIFO/ring queues and safe retry.

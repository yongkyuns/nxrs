# Physical camera qualification

Targets the Freenove ESP32-S3-WROOM board with OV3660. Board wiring is selected
by `esp32s3-camera`; the application uses only `camera::open()` and `Camera`.
No Wi-Fi credentials, desktop camera framework or second RTOS is required.

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

## Scope

This is raw-frame acquisition, not JPEG/MJPEG streaming. Each raw frame is
153,600 bytes; the provider owns one DMA buffer and the fixture one destination.
The sensor stays clocked between individual captures. Stream-off stops DMA;
closing the device puts the sensor in standby. Timestamps describe observation,
not exposure. The 50 ms polling bound is a fixture failure threshold, not a
hard-real-time guarantee or a measured latency claim.

The profile places user allocations in PSRAM and kernel allocations in internal
RAM. Its two raw payload buffers total 307,200 bytes; this depends on resolution
and pixel format, not a fixed HAL cost. The interrupt ownership fix adds one
fixed pointer per IRQ/CPU, not a growing allocation per camera reopen.

Dependency changes are patches 0067–0072 under `upstream/nuttx/patches/` and
0002 under `upstream/esp-hal-3rdparty/patches/`, applied to isolated build copies.
They cover the sensor, board registration, clock/DMA/cache correctness and
capture/interrupt cleanup. Native sensor and lifecycle tests mock hardware and
run with address/UB sanitizers; Rust ownership tests mock the ioctl boundary.
Neither replaces the physical test.

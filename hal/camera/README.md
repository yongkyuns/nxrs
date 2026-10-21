# Camera HAL

One `nxrs-camera` crate contains the contract and private providers. A checked-in
profile selects one provider through `NXRS_PLATFORM`; unset builds select `none`.

| Provider | Support and behavior |
| --- | --- |
| `native` | Linux/macOS/Windows packed Gray8/RGB565 file replay. Blocking bounded-size input loading at setup, bounded memory-copy polling, and exact owner-clock retry deadlines between frames. Not a physical camera driver. |
| `nuttx` | Standard NuttX video-capture adapter selected by `camera::open()`. The physical fixture targets the Freenove ESP32-S3/OV3660. The older read-device adapter remains for synthetic sim/QEMU tests only. |
| `mock` | Explicit std-based scripted camera, including WASM test builds. Preserves deliberate staging corruption, failures and stop retries. Not a web hardware implementation. |

The root manifest records supported providers in Cargo metadata. Native and
NuttX providers reject unsupported targets. `target_os="none"` is admitted by
the NuttX provider only for existing core-only compilation/link fixtures, not
as a claim of bare-metal operation without NuttX.

The native nxrs profile loads file input through
`camera::replay(...)`. Live acquisition uses `camera::open()` with no device path.
Desktop physical capture currently returns `Unsupported`; replay is not a hardware substitute.
The selected profile owns provider choice; app/nxrs has no
direct provider dependency. Arguments, service wiring, timestamps and shutdown remain
unchanged. Replay now advertises `Camera::next_poll_at_ms()`, allowing the
app-owned execution loop to block until the next frame deadline instead of
polling every millisecond. Ordinary execution timekeeping continues to use
`std::time`.

## Dependency boundaries

The camera API owns `camera::DeviceError`; storage and transport own separate
error types. Camera does not depend on `hal/common`, which holds the shared radio
delivery protocol. `hal/support/nuttx` owns only the existing consumptive
descriptor-close policy reused by camera, file storage and the legacy UDP fixture.
Providers translate scalar bridge status codes into their capability's errors.
Adoption of a raw descriptor
is unsafe and requires unique ownership; close consumes the number even when
the driver reports an error. There is one implementation, not a copy per device.

`src/api.rs` contains the contract and `src/providers/` contains private
provider modules. NuttX helper C sources are in `ffi/nuttx/`.

## Target integration

The firmware builder selects and compiles `ffi/nuttx/` helpers with configured
target headers. `v4l2.c` translates standard format, USERPTR queue and stream
ioctls; Rust uses ordinary `File` ownership. The legacy `camera.c` read-device
contract is used only by synthetic qualification. Shared descriptor support
owns consumptive close. No native ioctl struct or errno crosses the scalar ABI.

The live adapter owns two 64-byte-aligned DMA buffers in a FIFO queue. It
prequeues both so capture can continue while the owner consumes a frame. It
copies only after nonblocking dequeue, validates the returned buffer identity,
and requeues only after the copy. It retains a ready frame if the caller's
buffer is too small, and normalizes big-endian sensor RGB565 to the contract's
little-endian RGB565. Stop must quiesce DMA before storage is released. Failed
stream-off retains storage for retry; Drop fails closed if quiescence is unknown.

A driver-flagged corrupt frame is discarded and requeued without copying its
payload. `Camera::discarded_frames()` counts these events since the last successful
start (`None` means unmeasured). Invalid ownership/length metadata and failed
requeues still fault closed. This follows the recoverable
[V4L2 error-buffer contract](https://cdn.kernel.org/doc/html/latest/userspace-api/media/v4l/vidioc-qbuf.html).

The sensor supports 320×240 RGB565, and 320×240 or 640×480 JPEG. The adapter
reserves two raw-size DMA capacities (width × height × 2), even for compressed
JPEG: 307,200 B at QVGA or 1,228,800 B at VGA. The caller's destination is
additional. This is selected image storage, not fixed framework overhead; JPEG
reports its actual compressed length. See the physical fixtures below for what
has actually been qualified.

`Camera::set_jpeg_quality(JpegQuality::new(81)?)` requests quality before start;
the portable range is 1 (lowest) through 100 (highest), not a promise of equal
image quality across sensors. Unsupported providers return `Unsupported`, and
the live adapter rejects changes while open. An explicit request survives stop
and is reapplied on the next start. Leaving it unset preserves the sensor's
default. The OV3660 maps this range onto its 64 quantization settings; 81 keeps
its original scale-12 default. Higher quality usually produces larger frames.

Timestamps are owner-clock observation times,
not exposure times. Pending capture advertises a 5 ms retry; this is nonblocking
deadline-driven polling, not qualified interrupt wakeup.

## Verification

Provider selection is profile-owned. For example, run the standalone replay
fixture with `NXRS_PLATFORM=native cargo run -p nxrs-camera --example native_replay`.
The shared isolation tooling is being updated for the consolidated crate layout.

See [physical qualification](../../tests/nuttx-std/camera/README.md) for build/run
instructions and the current hardware result. Host ownership tests use a
synthetic driver boundary; they do not establish physical capture. The hardware
fixture is not a Wi-Fi stream. See [streaming qualification](../../tests/nuttx-std/stream/README.md)
for the separate portable MJPEG composition and measured device results.
Native fault-injection tests cover capture-start failure ownership, queue
rollback and safe retry; ordinary streaming does not exercise every error path.

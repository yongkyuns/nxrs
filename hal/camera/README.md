# Camera HAL

One `nxrs-camera` crate owns the camera contract, configured acquisition and
optional explicit replay. Read `src/lib.rs` for the public surface; the private
`src/platform/mod.rs` selects acquisition only, with no implementation bodies.

| API | Selection and behavior |
| --- | --- |
| `Camera`, `Format`, `Capture`, errors | Always available; contract-only builds remain `no_std`. |
| `camera::open()` | Uses the camera binding in `NXRS_PLATFORM`. NuttX selects live capture; an explicit mock selects a scripted source. Unbound or unsupported live acquisition returns `Unsupported`. |
| `camera::replay::open(...)`, `camera::replay::ReplayCamera` | Enabled by the ordinary Cargo `replay` feature. Loads packed Gray8/RGB565 files through Rust `std`, independently of camera admission. Not a physical camera driver. |

Replay has no default feature. Hardware-only and contract-only consumers do not
compile the file replay module. A consumer that needs replay declares it in its
ordinary Cargo dependency:

```toml
[dependencies]
nxrs-camera = { path = "../../hal/camera", features = ["replay"] }
```

This is a software-functionality choice, not an OS/provider feature. It requires
a target with Rust `std`; requesting it on a core-only target is not supported.
No OS whitelist or profile-derived cfg chooses the replay implementation.

The established root names `camera::replay(...)`, `camera::camera(...)` and
`camera::ReplayCamera` remain explicit compatibility aliases behind that same
feature. They are not backend-dependent exports. Prefer the `camera::replay`
namespace for new code. Scripted fixture exports remain explicitly gated by
`hal_mock`; they are never selected by a missing hardware binding.

The native `nxrs` CLI enables replay through its default `replay` feature and
keeps its existing invocation. Consumers of its contract-only application
library use `default-features = false`; the CLI target requires replay. Services
and the physical `home-cam` product do not need this feature. The old core-only
read-device adapter remains test-local in `tests/nuttx/support`.

Desktop physical capture currently returns `Unsupported`; `camera::open()`
never silently becomes replay. Explicit replay also works without
`NXRS_PLATFORM`, or alongside an explicitly selected mock. Loading is bounded
setup work; polling is an in-memory copy with owner-clock retry deadlines.
Ordinary execution timekeeping continues to use `std::time`.

## Source and dependency boundaries

`src/api.rs` owns the portable contract and `camera::DeviceError`. Storage and
transport own separate error types. Camera does not depend on `hal/common`,
which holds radio request identities and bounded payload storage.

`src/platform/mod.rs` maps the configured acquisition to one source module and
exports only the factory and deliberate mock fixture items. Factories and the
private unavailable type belong in those modules. `src/replay/` is shared Rust
behavior, not an acquisition backend or an unavailable stub. Host-injected
provider tests are wired separately in `src/test_support.rs`.

The NuttX provider owns its `File` and DMA buffers in Rust. Its private `close.c`
binding reports close errors that `File` destruction cannot return; Rust
consumes the descriptor before calling it. This organization change does not
alter capture, cancellation, buffer ownership or cleanup semantics.

## Target integration

The firmware builder selects and compiles `src/platform/nuttx/ffi/` helpers with configured
target headers. `v4l2.c` translates standard format, USERPTR queue and stream
ioctls; Rust uses ordinary `File` ownership. The legacy `tests/nuttx/c/camera.c` read-device
contract is used only by synthetic qualification. Camera-local `close.c` reports
consumptive close status. No native ioctl struct or errno crosses the scalar ABI.

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

The camera workflow keeps the core/WASM isolation builds and provider ownership
tests, and additionally runs replay with no profile, a mock profile and the Linux
profile. The same replay tests cover size/format rejection, clock monotonicity,
capacity, deadlines, restart and the explicitly exported constructors.

Run without a hardware profile:

```sh
cargo test --locked -p nxrs-camera --features replay
# The fixture file must contain two 2x2 Gray8 frames: [7; 4] followed by [9; 4].
cargo run --locked -p nxrs-camera --features replay --example native_replay -- source.raw
```

See [physical qualification](../../tests/nuttx-std/camera/README.md) for build/run
instructions and the current hardware result. Host ownership tests use a
synthetic driver boundary; they do not establish physical capture. The hardware
fixture is not a Wi-Fi stream. See [streaming qualification](../../tests/nuttx-std/stream/README.md)
for the separate portable MJPEG composition and measured device results.
Native fault-injection tests cover capture-start failure ownership, queue
rollback and safe retry; ordinary streaming does not exercise every error path.

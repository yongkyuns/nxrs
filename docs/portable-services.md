# Portable service composition

## Scope and status

This guide describes the camera/recording/telemetry service qualification.
`tests/camera-support` supplies the small shared Recorder, Monitor and
`CameraProduct` composition used by native, NuttX and WASM tests. It is test
support, not a production application or runtime framework. The old `app/nxrs`
replay executable is retired; `app/home-cam` owns the home-monitoring product
and its integration tests.

The shared qualification composition and service contracts are `no_std` and
allocation-free. Deterministic scenarios execute natively and as WASM in
Node/Chromium. Native tests additionally exercise real disk and UDP I/O.
These do not establish physical capture, RTOS scheduling or radio behavior.

## Dependency direction

The qualification composes the same capability-first service APIs:

```text
tests/camera-support -> nxrs-camera-service
                        nxrs-recording-service
                        nxrs-telemetry-service
                        -> nxrs-camera / nxrs-storage / nxrs-transport

service/navigation   -> nxrs-imu / nxrs-gnss
                          -> provider-independent api contract
                          -> one build-selected provider
```

Production apps and services depend on capability contracts, never on this test
helper. Native qualification explicitly constructs replay, recording and UDP
resources and transfers them into the generic services. Hardware acquisition
remains inside the owning capability HAL; test support does not select a
platform or construct native devices.

Selected images exclude unselected HAL provider implementations and their
exclusive Rust/C driver code; linker dead-code elimination is not the provider
selection mechanism.

`tests/portable` is the shared scenario runner, using explicit capability-local mocks.
Mock storage and transport use host vectors with configured maximum record/packet
counts. Device scripts and test oracle allocations belong only to the test
harness, not production services.

## Qualification workflows request capabilities, not platforms

Recorder composes `Frames + Recordings`. Monitor composes `Frames + Telemetry`.
These workflows operate through the traits and can be tested separately or
wired together inside the test composition. They cannot start/stop the camera
through `Frames`.

`CameraProduct` is one ordinary explicit constructor and tick function, not a
registry or mandatory framework. One camera service is shared by both workflows,
while the storage and transport services own their respective HAL adapters.
Scenarios can omit Monitor or substitute a frame provider. Product apps
compose their own services; this helper is not part of their dependency graph.

The current service-level composition API looks like:

```rust,ignore
// A qualification fixture supplies resources through the capability contracts.
let mut camera = CameraService::<_, 65536, 2, _>::with_buffers(
    platform_camera, history_buffers, staging_buffer,
)?;
let mut recordings = RecordingService::new(platform_storage);
let mut telemetry = TelemetryService::new(platform_transport);
let mut recorder = Recorder::default();
let mut monitor = Monitor::default();

let actual = camera.start(requested)?;
recorder.start(&camera)?;
monitor.start(&camera)?;

// Tests supply explicit times and can retry consumers independently.
let capture_result = camera.poll(clock.now_ms());
let recording_result = recorder.step(&camera, &mut recordings);
let monitor_result = monitor.step(&camera, &mut telemetry);
// Handle all three results; one error must not skip an independent workflow.
```

This construction belongs to qualification. Product service wiring stays in
handwritten app code; it is not a platform launcher or generated service graph.

The actual negotiated camera format is exposed; unsupported/invalid formats
are rejected rather than reported as successful configurations. This is a
single-source stream per Frames port. Cursors are sequence positions, not
handles: reconnecting a consumer workflow to a different logical stream requires
stopping and starting that consumer. A stopped/restarted camera retains its
sequence counter.

## Ownership, buffers and backpressure

CameraService owns one device, a fixed history, and one private staging buffer.
Payload capacity is `(history + 1) * frame_capacity`, plus metadata. No frame
allocation or deep cloning occurs in the portable path. Failed/pending reads
may overwrite staging but cannot corrupt published data. Actual byte lengths,
negotiated format and monotonic timestamps are checked before publication.

A returned frame borrows its service, so Rust prevents capture/teardown while
that view is still in use. Frames do not escape into unbounded application
queues. This is NOT a general DMA zero-copy lease pool. Large pools need static
or otherwise explicitly budgeted placement, not an oversized thread stack.

Each consumer workflow has its own cursor. A full history overwrites the oldest
frame; slow readers report exact sequence gaps, while a fast peer keeps progressing.
Each consumer processes at most one frame per step. Failed sink acceptance leaves
its cursor unchanged. A retry that outlives retained history sees a gap.

Storage and transport adapters must honor atomic local acceptance: Err accepts
nothing. Do not wrap partial writes with this interface without buffering or
commit semantics. Transport success is local acceptance, not a remote delivery
or exactly-once networking guarantee.

The native recording adapter now implements its cross-thread boundary with a
fixed startup-allocated pool. Queue capacity is reserved before the borrowed
frame is copied; a full queue therefore returns Busy without allocating/copying
a payload. Each accepted record owns one preallocated buffer until the writer
completes or discards it after a sticky error, then that buffer returns to the
pool. This is specific to the recording path and does not introduce a generic
lease pool into portable services.

Slow blocking HAL calls must not run unbounded on the shared owner; use a
bounded worker/timeout adapter.

## Lifecycle

Stopping a workflow does not imply device stop. Recorder first becomes Stopping,
accepts no new frames, and flushes previously accepted records. It does not drain
unread history. Failed flush remains retryable. Monitor stops independently.
The app's coordinated shutdown stops its workflows and services; no other app
remains running on the MCU.

Only the app's composition owner stops the camera. Failed device cleanup remains
StopPending: publication is hidden, new capture/start is rejected, and the
owner retains the device for retry. Invalid format negotiation also rolls back
through that cleanup path. App shutdown attempts all cleanup operations,
not just those before the first error. Callers must complete explicit shutdown;
this slice does not claim reliable hardware cleanup from an infallible Drop.
For native recording, successful flush is the app-visible commit guarantee;
`FileRecorder::finish()` is the explicit blocking worker-join operation, while
Drop only closes submission and detaches.

## Execution adapters

The portable code does not impose threads or async. The example owner can run
on a native/RTOS thread, and a browser can drive the same step functions from
callbacks. The native harness exercises bounded command/reply channels,
shutdown acknowledgement and worker joining. It does not implement isolated
processes or independently scheduled per-workflow/service workers. Future
message passing connects execution owners within the selected app, not apps to
one another. One app does not require all its services to share one thread.

The target main explicitly starts the loops/threads it needs. Prefer ordinary
std threads and bounded channels on NuttX/native and the selected pthread-enabled
Emscripten browser profile. The separate
[browser thread probe](https://github.com/yongkyuns/nxrs/actions/runs/36274987242)
passed in actual Chrome and Safari at `7511b1c`; the roadmap records its exact
scope. This does not migrate the current production services or qualify browser I/O.
No custom thread API, mandatory cooperative executor, generic actor framework,
universal service trait or prescribed runtime package split is needed.

Browser HAL callbacks must run on a responsive context and deliver bounded
completions to the waiting Rust owner. Do not expect a callback queued on a
worker to execute while that worker is stuck inside a synchronous Rust loop.
Never block the browser UI thread waiting for a worker that needs UI proxying.
Keep such platform-specific I/O handling inside HAL/target support. The existing
threadless WASM mock tests remain useful; they are not the chosen threaded profile.

HAL poll functions must return promptly (Pending when no data is ready).
`Camera::next_poll_at_ms()` lets a timer-driven provider advertise the earliest
owner-clock time at which another poll may make useful progress. The native
replay provider uses this to eliminate blanket 1 ms polling. None means no
time-based retry is advertised; a future interrupt/callback-driven provider must
supply an actual readiness wakeup rather than forcing a fake periodic timer.

Existing blocking drivers need a platform-specific worker or bounded timeout
adapter; this is not solved by putting `async` on a blocking function. Device
completion and wakeup logic remain behind the HAL, and clocks are injected in
a single monotonic domain. Browser mocks exercise no actual browser camera.

The simpler design still requires explicit queue limits, overload behavior,
readiness, shutdown and ownership across threads. Small messages can be copied;
large cross-thread payloads need owned buffers. Native recording now demonstrates
the intended narrow pattern: preallocate the selected path's buffers, reserve
capacity before copying, transfer one buffer to the worker, and recycle it on
completion. A generic lease pool, request-ID protocol or subscription system is
not required unless another concrete use needs it.

## Running and CI

The root toolchain is pinned to Rust 1.90.0. Current portable commands are:

```sh
NXRS_PLATFORM=mock cargo +1.90.0 test --locked -p nxrs-simulator
NXRS_PLATFORM=mock cargo +1.90.0 run --locked -p nxrs-simulator
rustup target add --toolchain 1.90.0 thumbv6m-none-eabi wasm32-unknown-unknown
cargo +1.90.0 check --locked -p nxrs-camera-test-support --target thumbv6m-none-eabi
NXRS_PLATFORM=mock cargo +1.90.0 build --locked --release --lib -p nxrs-simulator --target wasm32-unknown-unknown
node tests/browser/run-wasm-scenarios.mjs target/wasm32-unknown-unknown/release/nxrs_simulator.wasm
```

For real browser execution install `playwright@1.56.1`, run
`npx playwright install chromium`, and add `--browser` to the Node command.

`.github/workflows/portable.yml` runs native tests on Linux/macOS/Windows,
compiles the production crates for Cortex-M0, and executes the shared Rust
assertions inside Node and a real headless Chromium page. These are application
and HAL-contract qualifications, not MCU emulation or hardware validation.
The browser runner reads scenario names/count from WASM itself and fails on
any trap, failed assertion, invalid registry or missing scenario execution.

The suite covers fan-out, independent/standalone compositions, slow consumers,
atomic retries, full storage, start/stop/flush failures, actual format
negotiation, malformed metadata, staging isolation, clock regression, repeated
lifecycles and deterministic interleavings checked against a separate history
model. Existing Linux/NuttX build checks remain intact. The new normal-main
NuttX/std and app-entry gates remain implementation work.

## Hardware integration

Native and NuttX adapters now live under `hal/native` and `hal/nuttx`. Physical
camera/radio integration and a possible Zephyr adapter remain separate work;
do not expose unsafe global state as safe shared services.
Qualify real negotiated formats, bounded latency, stop/restart, buffers and
error/partial-write behavior with the same contract tests plus hardware tests.
Route firmware entry points through the selected app's construction. OS selection
is a backend decision; it must not fork application policy or scenario behavior.

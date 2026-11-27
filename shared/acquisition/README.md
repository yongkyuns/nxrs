# Shared acquisition support

IMU, GNSS and audio use this package to start, wake and join workers and to
wait for NuttX descriptor readiness. Applications use each HAL's session or
stream. Qualification fixtures can exercise the worker and native wait directly.

Each explicit `Worker::start` starts one Rust thread. The caller supplies the
task body and can share its own state in the same allocation as the stop flag.
The task body owns device I/O, event delivery, overload policy, terminal
notification and cleanup. It can retain a device lease through sink destruction.
The worker preserves its result for repeated joins.

`stop_and_join` joins finished work even if the wake endpoint has failed.
Interrupted wake writes retry. If a running worker cannot be woken, the
operation returns an error and retains its handle so the caller can retry.
Resources held by the task remain with it until cleanup finishes.

The NuttX poll/pipe ABI lives under `src/platform/nuttx/ffi/`; Rust owns the
descriptors. Cancellation wins when a device and the cancel pipe are both
ready. `readiness::wait` retries interrupted waits. Timed callers recompute
their remaining deadline after interruption. Read device state to confirm
that data is available after a timer wakes the task.

Explicit mock providers use the cancellable timer in `acquisition::mock`.
The optional `probe` feature provides wake/drain timing and I²C/SPI
transaction observers. The owning HAL selects the observation points and
must keep callbacks in task context and nonblocking.

On NuttX, `Trace::for_source` also records bounded native B/E/instant notes using
the canonical capability and device ID. IMU names its wake, drain, and bus
transactions; GNSS names wake and read/parse/deliver work. The native enabled
filter controls retention, so these markers follow `trace start` / `trace stop`.
They use fixed stack storage and add no observer allocation or worker thread.
Long or sanitized source names retain a visible stable hash suffix, and scopes
preserve their stage name. Host observer callbacks remain available through
`Trace::new`. Without `probe`, tracing is a zero-sized no-op implementation.

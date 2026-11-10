# Shared acquisition support

`nxrs-acquisition` is shared HAL implementation support, not a device driver or
a profile-selectable capability. IMU and GNSS reuse its worker, bounded delivery,
cancellation and readiness mechanisms. Sensor protocols, cadence, resource
budgets, error policy and device cleanup remain in its callers.

Firmware qualification witnesses may depend on this helper directly when both
the application and profile explicitly select qualification. Ordinary applications
use the owning HAL APIs; qualification does not expose private provider modules.

Each explicit `Worker::start` creates one thread. This package shares code, not
an executor; it adds no queue of its own. Drivers do not acquire a worker merely
by implementing a device contract. A future shared executor must qualify bounded
device work, fairness, cancellation and deadlines before replacing these workers.
The worker and session handle share one allocation for the cancellation flag and
loss counter; thread and wake-endpoint allocations are separate.

`Worker::start_guarded` additionally retains caller-owned storage through terminal
delivery and destruction of the sink, including unwinding or spawn failure. IMU
uses this to keep its canonical device leased until callbacks have retired.

On NuttX, `acquisition::nuttx` provides one private target-header poll/pipe binding
for both HALs. Rust owns the resulting descriptors. `wait` reports device
readiness or cancellation; `tick` reports timeout or cancellation. Cancellation
wins simultaneous readiness, and errors propagate to the caller. The shared
`readiness::wait` retries interrupted device waits; timed callers recompute their
remaining deadline after interruption. A timeout never proves sensor data exists.

The native ABI source lives under `src/platform/nuttx/ffi/`. Host tests inject
wait operations or compile the C bridge against host headers; they do not select
a second production backend. This extraction adds no worker or queue.

`stop_and_join` joins completed work even if its wake endpoint has failed, so a
terminal device failure remains observable. Interrupted wake writes retry. If a
still-running worker cannot be woken, joining returns an error and retains the
handle for retry; that error does not establish quiescence or release resources.

`acquisition::probe` owns optional wake/drain timing and embedded-hal I²C/SPI
transaction observers. Enable `probe` and attach an observer with `Trace::new`;
callbacks run in task context and must not block. Without that feature, `Trace`
is zero-sized, buses remain unwrapped and observer creation returns `None`.
Callers choose observation points and map unavailable diagnostics to their own
errors; sensor-specific diagnostic configuration stays in the device catalogue.

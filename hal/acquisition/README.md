# Shared acquisition support

`nxrs-acquisition` is shared HAL implementation support, not a device driver or
a profile-selectable capability. IMU and GNSS reuse its worker, bounded delivery,
cancellation and readiness mechanisms. Sensor protocols, cadence, resource
budgets, error policy and device cleanup remain in its callers.

Each explicit `Worker::start` creates one thread. This package shares code, not
an executor; it adds no queue of its own. Drivers do not acquire a worker merely
by implementing a device contract. A future shared executor must qualify bounded
device work, fairness, cancellation and deadlines before replacing these workers.

On NuttX, `acquisition::nuttx` provides one private target-header poll/pipe binding
for both HALs. Rust owns the resulting descriptors. `wait` reports device
readiness or cancellation; `tick` reports timeout or cancellation. Cancellation
wins simultaneous readiness, and errors propagate to the caller. The shared
`readiness::wait` retries interrupted device waits; timed callers recompute their
remaining deadline after interruption. A timeout never proves sensor data exists.

The native ABI source lives under `src/platform/nuttx/ffi/`. Host tests inject
wait operations or compile the C bridge against host headers; they do not select
a second production backend. This extraction adds no worker or queue.

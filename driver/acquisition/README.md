# Shared acquisition support

`nxrs-acquisition` provides worker, bounded delivery, cancellation and readiness
mechanisms shared by IMU and GNSS. Despite its historical directory under
`driver/`, it is runtime support: sensor protocols, cadence, resource budgets,
error policy and device cleanup remain in its callers.

On NuttX, `acquisition::nuttx` provides one private target-header poll/pipe binding
for both HALs. Rust owns the resulting descriptors. `wait` reports device
readiness or cancellation; `tick` reports timeout or cancellation. Cancellation
wins simultaneous readiness, and errors propagate to the caller. The shared
`readiness::wait` retries interrupted device waits; timed callers recompute their
remaining deadline after interruption. A timeout never proves sensor data exists.

The native ABI source lives under `src/platform/nuttx/ffi/`. Host tests inject
wait operations or compile the C bridge against host headers; they do not select
a second production backend. This extraction adds no worker or queue.

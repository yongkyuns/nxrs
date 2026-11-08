# NuttX GPIO interrupt resource

`Interrupt::open_rising(path, on_cleanup_failure)` owns a NuttX rising-edge GPIO
registration. It implements `embedded_hal::digital::InputPin` and lends its file
to task-context readiness waits. Only this interrupt mode is currently supported;
there are no host backends or additional profile/provider switches.

The Rust resource owns registration, rollback and exactly-once unregister.
NuttX owns ISR notification storage; it never calls Rust. If rollback or teardown
fails, the supplied hook runs in task context, including during Drop. The caller
must retain that uncertainty and prevent unsafe reacquisition. The hook must not
panic. Repeated unregister calls do not retry an uncertain native operation.

IMU supplies its quarantine hook and keeps sensor shutdown ordering, IRQ/timer
selection and sample policy. Native GPIO ioctl glue stays beside this resource
under `src/platform/nuttx/ffi/`. Host witnesses inject target operations into the
real resource implementation; those checks do not qualify physical interrupts.

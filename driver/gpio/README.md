# NuttX GPIO interrupt resource

`Interrupt::open_rising(path, on_cleanup_failure)` owns a NuttX rising-edge GPIO
registration. It implements `embedded_hal::digital::InputPin` and lends its file
to task-context readiness waits. Only this interrupt mode is currently supported;
there are no host backends or additional profile/provider switches.

The package also compiles when NuttX disables `CONFIG_DEV_GPIO`, as in the
emulator mock profiles. Native operations then report unsupported without issuing
ioctls; a configured hardware IRQ cannot silently fall back to a timer.

The Rust resource owns registration, rollback and exactly-once unregister.
NuttX owns ISR notification storage; it never calls Rust. If rollback or teardown
fails, the supplied hook runs in task context, including during Drop. The caller
must retain that uncertainty and prevent unsafe reacquisition. The hook must not
panic. Repeated unregister calls do not retry an uncertain native operation.

`open_rising_with_context(path, context, on_cleanup_failure)` retains typed context
and a task-context function pointer without boxing. IMU passes its canonical
device owner so a failed IRQ cleanup quarantines only the affected instance.

IMU supplies its quarantine hook and keeps sensor shutdown ordering, IRQ/timer
selection and sample policy. Native GPIO ioctl glue stays beside this resource
under `src/platform/nuttx/ffi/`. Host witnesses inject target operations into the
real resource implementation; those checks do not qualify physical interrupts.

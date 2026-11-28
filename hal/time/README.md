# System wall-clock control

`nxrs-time` supplies the wall-clock controls that Rust `std` does not expose.
Import its library as `time`. Continue to read wall time with
`std::time::SystemTime`, measure elapsed time with `std::time::Instant`, and
wait through ordinary std facilities.

Acquire one `time::Control` through `time::open()`. The handle is exclusive
among users of this HAL in one linked Rust image. Native clock writers, other
processes and hardware RTC setters require coordination by the same owner.
Dropping the handle releases admission and leaves an ongoing correction running;
cancellation is explicit. The provider allocates no worker, queue or heap state.

## Operations and failures

| Operation | Contract |
| --- | --- |
| `set` | Step to a `SystemTime`, including representable pre-epoch values. Success clears a correction managed by this capability. |
| `replace_adjustment` | Replace the remaining correction with signed whole microseconds and return the previous residual. Applied progress is preserved. |
| `pending_adjustment` | Query without replacing the correction. Fractional microseconds round away from zero, so zero means completed. |
| `cancel_adjustment` | Cancel the residual after accounting for applied progress and return the cancelled amount. |
| `capabilities` | Report the available software slew rate. This is a correction limit, not oscillator accuracy or a retention guarantee. |

A positive correction speeds up wall time; a negative correction slows it
without reversing it. Supported operations preserve monotonic time. Invalid
correction requests are rejected before mutation. `Unsupported`, `Busy` and
`OutOfRange` distinguish unavailable operations, an existing owner and
unrepresentable values. `Io` can follow a native operation that already took
effect and does not guarantee rollback. The [Rust contract](src/lib.rs) defines
the API.

## NuttX binding

A consuming NuttX profile selects the capability with this profile excerpt:

```toml
[hal]
time = "nuttx"
```

Use the normal `cargo nxrs build` frontend, which prepares the owning native
helper against configured NuttX headers and the registered patch series.
No device endpoint or physical-device catalogue entry is needed for the system
clock. An unbound build returns `Unsupported`; it does not open a host clock.

The repaired software backend reports `slew_rate_ppm: Some(500)` only when
both `CONFIG_CLOCK_TIMEKEEPING` and `CONFIG_CLOCK_ADJTIME` are enabled.
Other admitted configurations support stepping and return `Unsupported` for
adjustment, query and cancellation. Software corrections must fit signed
64-bit nanoseconds: whole-microsecond inputs range from
−9,223,372,036,854,775 through +9,223,372,036,854,775.

`CONFIG_RTC_HIRES` is rejected at acquisition, including for stepping. In that
configuration NuttX obtains monotonic time from an RTC that wall-clock setting
can change. The provider therefore cannot offer its monotonic-time contract.

## RTC and synchronization ownership

`hal/rtc` retains direct hardware-clock access, including the device's indication
that time was set. It exchanges whole-second `SystemTime` values; that indication
alone does not establish trustworthy UTC. Its NuttX setter also changes system
time, so it must be coordinated with the wall-clock owner. A successful
`time::Control::set` may cause NuttX to update hardware RTC state, but does not
confirm that retention write.

The [time synchronization controller](../../service/time-sync/README.md) owns
source selection, uncertainty, correction policy and lifecycle invalidation.
It accepts correlated UTC/`Instant` observations and is driven by its caller;
the HAL owns no source protocol or polling cadence. The OS/BSP remains
responsible for RTC retention and restoring clock accounting after sleep.

See the [time synchronization fixture](../../tests/nuttx-std/time-sync/README.md)
for the current host and target evidence. This slice does not activate a
product, supply GNSS timestamp correlation or an NTP client, or qualify physical
RTC retention and deep sleep. Earlier clock-control and time-adjustment evidence
remains scoped to its recorded configurations.

# Time synchronization controller

`nxrs-time-sync-service` supplies a synchronous UTC discipline owner. Import the
library as `time_sync`, give it one [`time::Control`](../../hal/time/README.md),
and deliver observations from already admitted sources. It selects a usable
reference, seeds an unknown clock, requests bounded smooth corrections, and
reports measured quality. The caller drives it from an existing execution owner.
There is no worker, queue, socket, timer executor or heap allocation in the service.

Read clocks and wait with Rust std. The distinction between system-clock control,
source acquisition and retained hardware time is defined in the
[architecture](../../docs/architecture.md#time-synchronization-ownership) and
[HAL architecture](../../docs/hal-platform-architecture.md).

## Compose one owner

This composition excerpt has two admitted sources. Its numbers illustrate a
policy; qualify oscillator and read-resolution bounds for the actual board.

```rust
use std::time::Duration;
use time_sync::{Controller, Error, InitialTime, Policy, Source};

fn compose<C: time::Control>(clock: C) -> Result<Controller<C, 2>, Error> {
    Controller::new(
        clock,
        Policy {
            accuracy: Duration::from_millis(5),
            refresh_interval: Duration::from_secs(60),
            max_holdover: Duration::from_secs(3_600),
            max_slew_time: Duration::from_secs(60),
            clock_resolution: Duration::from_micros(100),
            drift_ppm: 20,
        },
        [Source { priority: 0 }, Source { priority: 1 }],
        InitialTime::Unknown,
    )
}
```

Use `InitialTime::Unknown` when the product explicitly authorizes one seed step.
Use `Preserve` when an existing system clock must not be stepped implicitly,
including after an explicitly validated RTC restoration. `Preserve` does not
certify UTC quality. Construction validates the policy and cancels any inherited
adjustment before source admission; a failed cancellation is returned to the
caller. Providers without slew support skip adjustment operations.

The controller retains exactly one observation for each of its N source slots.
The caller assigns those slots to real sources and establishes their trust.
An observation with a small claimed uncertainty does not make an untrusted
network server trustworthy. Fresh sources precede holdover sources, then lower
priority numbers win. Within the preferred tier, smaller aged uncertainty wins.
Equally preferred sources must share a common possible UTC interval; conflicting
evidence cancels a pending correction and returns `Conflict`.

## Admit UTC at its measurement epoch

Capture `controller.epoch()` before starting a measurement. Deliver an
`Observation` containing that token, its source slot, the local `Instant` at
which its UTC value applies, and an assumed absolute error bound. UTC uses the
POSIX timescale; adapters must agree on leap-second handling. The uncertainty
must include receiver quality, epoch association, capture quantization and
transport delay. A statistical standard deviation is not an absolute bound.

Delivery time is not automatically the measurement epoch. Do not reinterpret
numeric GNSS ticks as an opaque std `Instant`, or label an old exchange with a
new post-wake epoch. Current GNSS UTC reports do not yet supply this qualified
association; adding that report must reuse the existing receiver session.

`observe` rejects a wrong source/epoch, pre-construction or pre-resume data,
future timestamps, observations older than `max_holdover`, non-increasing source
timestamps, and unacceptable uncertainty before replacing retained evidence.
Representability checks fail explicitly. Accepted evidence may remain retained
after a backend operation fails, but the operation is not reported as applied.

## Correct and verify

`observe` admits evidence and reconciles immediately. `poll` checks current
native progress and then brackets a std `SystemTime` read with two `Instant`
reads. It projects the source UTC to that measurement epoch and includes the
full read bracket, configured read resolution and one microsecond of correction
granularity in the error bound.

For an observation age A measured by `Instant`, oscillator bound P ppm adds
`ceil(A × P / (1,000,000 − P))` to source uncertainty. This includes the slow-clock
worst case when P is specified relative to actual elapsed time. Policy rejects
P at or above 1,000,000. These are assumed bounds: the controller cannot discover
oscillator accuracy or prove that a source's uncertainty is truthful.

The measured signed offset is the new remaining correction. For example, after
3 ms of an 8 ms correction has been applied, new evidence may measure the clock
as 6 ms slow. Replacement requests +6 ms. It does not combine that measurement
with the old +5 ms residual. When the residual still agrees with the measurement
within uncertainty, the controller preserves it to avoid resetting progress.

Initial seeding consumes its one step only when the projected reference still
fits the accuracy budget. A successful set reports `Checking` until a later
paired read verifies alignment. An ambiguous set failure consumes step permission
without claiming initialization; retrying evidence must not cause another
unannounced discontinuity. Initialization and seed permission remain separate
from freshness, adjustment completion and current accuracy.

For an initialized clock, the permitted slew offset is the provider's reported
rate multiplied by `max_slew_time`. The qualified 500 ppm provider can correct
30 ms within a 60-second budget. A larger discrepancy, or a needed correction on
a provider without slew support, cancels the residual and returns
`StepRequired { offset_us }`. The product must decide whether to authorize a
discontinuity, corroborate sources, or continue with degraded quality. The
controller never regains seed permission merely because time became stale.

If the measured clock is already within the accuracy budget, or the error cannot
be distinguished from measurement uncertainty, a remaining correction is
cancelled. Cancellation returns `Checking`; another read establishes quality.
A zero pending correction alone is insufficient for `Synchronized`.

## Drive deadlines and interpret status

`Status` is a historical measurement at `checked_at`. `last_status` retrieves
that record; `poll` obtains a current one. Do not cache a synchronized result as
an indefinitely valid readiness flag.

| State | Meaning and caller action |
| --- | --- |
| `Unknown` | UTC has not been verified or deliberately seeded; obtain usable evidence. |
| `Checking` | A step/cancellation was accepted; poll for readback. |
| `Adjusting` | A correction remains active; poll at `check_at` or on new evidence. |
| `Synchronized` | A fresh reference, no remaining slew, and the measured absolute error bound satisfy policy. |
| `Holdover` | The clock was initialized, but its reference needs renewal or no longer establishes the required accuracy. |
| `StepRequired` | Smooth recovery cannot satisfy the configured correction budget; product recovery is needed. |
| `Conflict` | Equally preferred sources disagree; obtain new credible evidence. |
| `Fault` | A backend/read/arithmetic failure prevents a current quality claim. |
| `Suspended` | Source evidence is invalidated and new observations are refused. |

`refresh_at` requests another source observation by the earlier of the periodic
interval and the point where oscillator drift consumes the accuracy budget.
Connectivity or GNSS availability can prompt an earlier observation. A deadline
does not start acquisition; the caller owns network timeout, retry and power
policy. If a source is unavailable, use its existing backoff and lifecycle events
instead of repeatedly polling an expired deadline.

`check_at` requests adjustment completion or immediate readback. With no
correction and no fresh evidence to check, it is `None`. At `refresh_interval`
the reference enters holdover. Beyond `max_holdover`, extrapolation stops, any
residual is cancelled and `error_bound` becomes `None`. An absent source does not
stop unrelated acquisition. Backend failures also clear quality/pending claims:
an I/O failure can occur after a native change and is not a rollback guarantee.

## Suspend and resume

Call `suspend` before an unqualified monotonic continuity boundary. It invalidates
all source evidence and refuses observations even if native cancellation fails.
Handle that error before allowing sleep. Quiesce or drain acquisition as required
by each producer; epoch tokens also reject delayed pre-sleep deliveries.

The board/OS must restore clock accounting before calling `resume`. Resume
cancels any inherited correction, starts a new source epoch and sets the minimum
accepted observation time. It preserves initialization and does not authorize a
step. Fresh evidence can then drive smooth recovery or `StepRequired`.

This lifecycle API does not measure elapsed sleep time, restore an RTC or make
`Instant` portable across suspend/reset. A reset-on-wake path constructs a new
controller; never persist `Instant`. Existing `hal/rtc::Clock::set` also changes
the OS clock and must be coordinated with this single writer. Physical RTC
validity, retention completion and board sleep behavior remain separate gates.

## Evidence and current integration

From the repository root, `cargo test -p nxrs-time -p nxrs-time-sync-service`
runs the host contracts using fake controls without changing the host clock.
The [NuttX integration fixture](../../tests/nuttx-std/time-sync/README.md)
uses the production APIs, controlled MPS2-AN521/QEMU timing and a same-profile
image comparison. Its synthetic observations test discipline; they do not
qualify an external UTC source.

No product selects this service yet. Time-only GNSS reporting and epoch
correlation, bounded NTP sampling, physical RTC retention and application
sleep integration remain tracked in
[case #490](https://github.com/yongkyuns/nxrs/issues/490). Motion Tracker's existing
historical capture-to-UTC mapping is unchanged.

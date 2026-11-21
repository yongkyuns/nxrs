# Pulse output and edge observation

The pulse capability owns a configurable PWM output and one rising-edge input.
Applications use the same API with an explicit timer-driven mock or a NuttX
profile that binds real endpoints. It adds one acquisition worker because GPIO
readiness can block. It does not add an application event bus or controller.

The existing [controller application](../../website/examples/controller/) is
the runnable example. Its application policy, commands and shutdown remain in
that one package.

## Contract

- `pulse::open(sink)` acquires the selected resources exclusively and establishes
  stopped output. A second open returns `Busy` until both the output and the
  acquisition worker, including sink destruction, have retired.
- `Waveform::new(hz, percent)` admits 10–100 Hz and 1–99 percent duty.
  Invalid values have no hardware effects. These are requested settings; the
  hardware timer can quantize them. `configure` applies and starts a waveform.
- `disable` explicitly stops output. Previously queued or in-flight edge
  notifications can still arrive; joined `shutdown` is the completion barrier.
- The sink runs in task context and must return promptly. Return `false` when
  admission fails. Each event gets one attempt, with no retry or waiting for
  consumer capacity.
- `Stats.observed` counts observed edge notifications; `rejected` counts those
  whose sink returned false. Both saturate at `u64::MAX`. A source failure is
  retained independently in `Stats.fault` and in the shutdown result, even when
  its one `Event::Fault` notification is rejected.
- Edge sequence numbers start at one for each session. Their `Instant` values
  are worker receipt timestamps. They are not ISR timestamps, physical edge
  times or a measurement of the PWM timer's accuracy.

GPIO notification coalescing and device behavior can lose physical transitions
before the worker observes them. An observation count must not be presented as
an exact hardware pulse count. Software queue rejection and missing hardware
notifications are different phenomena.

The lower frequency limit also avoids the pinned RP23XX driver's divider clamp.
Its 16-bit TOP and maximum 255.9375 clock divider at the Pico 2 profile's 150 MHz
clock cannot represent rates below about 8.94 Hz. The shared 10 Hz minimum keeps
requests inside that timer range; it does not replace electrical measurement.

## Lifetime and recovery

Explicit shutdown attempts output-off, wakes the source, joins it, unregisters
GPIO notification, retires the sink, and releases the descriptors. It attempts
source cleanup even if output-off fails. Repeating a completed shutdown returns
the same result.

A failed cancellation retains its worker for retry without joining an unwoken
blocked thread. A failed output stop retains the output for another cleanup
attempt. Successfully retiring resources does not erase an earlier session
fault. A source I/O failure followed by successful cleanup allows a new session.

Drop attempts output-off and requests cancellation without joining. The worker
keeps ownership until it really finishes. No hard shutdown deadline is claimed:
driver operations and the user-supplied sink must return.

Native GPIO unregister failure quarantines admission until reboot. A native PWM
configuration or stop failure also quarantines this capability. In the pinned
NuttX PWM upper half, a failed lower-half stop clears the upper-half started
flag, and a failed start may have effects without setting it. A later successful
STOP alone therefore cannot establish hardware recovery. The HAL keeps that
uncertainty visible instead of silently reusing the resource.

The exclusive lease coordinates this NXRS capability; arbitrary native code can
still open the same endpoints. Profiles must assign the endpoints to one owner.

## Provider boundary

The NuttX provider consumes the profile's PWM endpoint, GPIO input endpoint and
worker stack budget. It reuses `driver/pwm`, `driver/gpio` and the cancellable
readiness path in `hal/acquisition`; it adds no native helper. Applications do
not receive paths, descriptors, native requests or board names.

The explicit mock synthesizes one notification per requested period while
enabled. It starts disabled and supports the same cancellation and exclusive
ownership contract. It does not model duty-cycle electrical behavior, IRQ
coalescing or hardware failure. Unbound profiles return `Unsupported`.

## Verification

~~~sh
cargo test --locked -p nxrs-pulse -p nxrs-pwm
NXRS_PLATFORM=mock cargo test --locked -p nxrs-pulse
~~~

Controlled source fixtures exercise rejected events, retained source failures,
failed cancellation, cleanup retry, quarantine, sink retirement and blocked
shutdown without scheduler sleeps. The PWM tests execute its actual native Rust
owner with a real host `File` and injected native controls. Those tests check
ownership and error handling, not NuttX's ioctl ABI or electrical output.
The explicit mock test checks the factory and source integration. Target builds
and physical edge/timing observations remain separately identified evidence.

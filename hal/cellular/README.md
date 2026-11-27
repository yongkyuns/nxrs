# Cellular lifecycle

`connect` acquires one SIM7080/PPP lifetime. `connect_managed` retains the same
exclusive owner while retrying recoverable network failures with a 1–30 second
backoff. `Status` is retained separately from its notification. Notifications
run in the worker and must return without waiting. `RecoveryRequired` is
terminal: an uncertain modem remains quarantined until platform recovery.

Resource admission and network recovery are different decisions. Invalid APNs,
an already owned device, a quarantined device, and worker/pipe creation can fail
before a session exists. Applications decide whether and how often to retry
that admission, independently of other optional transports.

Use `request_stop()` to signal cancellation and wake the UART/PPP wait without
joining. Signal all independently owned operations before waiting for any of
them. `is_finished()` permits an asynchronous supervisor to join only a retired
worker; `join()` reports its retained terminal result. `stop()` is the synchronous
request-and-join convenience. The native session's destructor also completes
resource retirement; its task and callback state must never be freed early.

After PPP stops, the provider must establish command mode before releasing the
modem. Both 1.1 second escape guards, the escape write/reply, and ATH/AT fallback
consume one `SHUTDOWN_TIMEOUT` of 10.2 seconds. Each write/response additionally
has its existing 2 second command allowance. Child operations cannot refresh
the parent deadline. Missing escape acknowledgement is tolerated only when
ATH or AT subsequently proves command mode; no acknowledgement, a failed
escape write, or an expired cleanup deadline returns `RecoveryRequired` and
retains quarantine.

This is a software I/O budget, not a physical scheduling bound. A supervising
product must also budget scheduler/native-call retirement and use its watchdog
when that outer deadline expires. The tracker cancels its TCP dial and modem
together, allowing the 3 second connect attempt to retire concurrently with
UART cleanup. It continues supervision within an explicit product shutdown
deadline, independently of its normal watchdog feed interval.

The host tests execute the same cleanup policy with a deterministic clock and
test missing replies, fallback acknowledgement, delayed scheduling, and failed
writes. They establish budget consumption and quarantine decisions. NuttX poll,
UART timing, modem guard behavior and callback/descriptor retirement still
require the existing target and physical qualification paths.

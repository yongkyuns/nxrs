# Event-driven navigation demonstration

The application composes IMU, GNSS, fusion and health processing through portable
service APIs. Product profiles select HAL implementations; app and service code
contain no OS, board, device-path or native-driver knowledge.

```sh
NXRS_PLATFORM=host-demo cargo run --locked -p nxrs-event-demo
NXRS_PLATFORM=host-imu cargo run --locked -p nxrs-dual-imu-demo
```

## Ownership and delivery

```text
IMU interrupt → HAL FIFO drain → typed Sample ─→ ImuService ──┐
GNSS UART ready → HAL read/UBX → typed Fix ────→ GnssService ─┼→ FusionService
commands ──────────────────────────────────→ service inboxes ┘
```

Each HAL owns acquisition, parsing, normalization and device cleanup. Services
own local processing and session lifecycle. Acquisition events use bounded,
nonblocking task-context admission; handlers do not execute inline on producers.
ISRs notify through a qualified native path and never invoke Rust channels.

Each sensor service blocks on its event inbox and handles samples, commands and
faults. It has no sampling timer and performs no bus/UART reads. Pause/resume
controls processing; HAL acquisition continues draining and suppressed samples
count as drops. Hardware ODR is independent of service commands. The former
`set_period`/service polling configuration is removed.

There are two sensor acquisition workers, two sensor service owners and one
fusion owner in the full demonstration. The dual-IMU fixture creates two
independent pipelines. This counts application workers, not OS/system tasks.
Synthetic sources use timers solely as explicit mocks; production providers use
UART readiness or IMU interrupt/FIFO notification without a polling fallback.

## Lifetime and overload

Create inboxes and sinks before starting acquisition. Ordinary queue-full
rejections are counted and sequence gaps remain visible. A terminal producer
fault has one retained retry event and a sticky join result. The acquisition
worker never waits for a consumer to free measurement capacity.

Explicit teardown wakes blocked acquisition, joins it, and verifies cleanup
before reporting resource release. Explicit fusion stop drains previously
admitted inputs; abandoned handles cancel immediately. Drop requests cancellation without joining;
the detached worker retains resources until its cleanup completes. Terminal
cleanup failures quarantine the corresponding native capability until reboot.

IMU samples carry SI units, host receive time and nominal hardware sample
interval. Fusion integrates consecutive samples using that interval, skips
missing input intervals, and exposes `imu_discontinuities`; it does not extrapolate
unobserved motion. FIFO arrival time does not supply the integration interval.
Full FIFO is an explicit source fault.

## Qualification

`service/navigation/tests/active.rs` exercises event delivery, pause/resume and
explicit teardown using mocks. Sensor HAL suites exercise UBX fragmentation,
FIFO framing, units, overload and failures. `tests/host/test-event-demo-nuttx.py`
checks the actual demo transcript in existing NuttX simulator/QEMU runs.

Host/mocked results do not establish physical interrupt latency, sample-clock
accuracy, power use or FIFO throughput. See [CoreS3 constraints](m5stack-cores3.md)
for the unwired M135 interrupt. See [communication requirements](concurrency-event-communication.md)
for the wider event-driven architecture and qualification requirements.

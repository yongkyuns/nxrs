# GNSS capability

`nxrs-gnss` owns receiver communication, configuration and the hardware-independent
fix API. Services receive WGS84 latitude/longitude, speed in m/s and optional
movement course in radians. `timestamp_ms` is the owner’s monotonic reception
time, not UTC or measured satellite-to-host latency. Navigation owns projection
into local north/east coordinates.

The NuttX provider owns a nonblocking UART. The independent
[`kinavis-ubx` crate](https://github.com/KINAVIS/kinavis) supplies UBX framing,
checksums, typed messages and modern CFG-VALGET/VALSET encoding into caller-owned
buffers. We pin 0.1.1 with default features disabled and `libm` enabled; its
kernel types stay private. Neither dependency uses allocation, an OS or a runtime
in this configuration. No Nxrs protocol wrapper crate is needed. The CoreS3 profile uses the existing
size-optimized build mode to discard unused codec code.

The adapter admits only M9’s 92-byte NAV-PVT, configuration acknowledgements and
the requested CFG-VALGET response. It validates wire domains before the codec
wraps longitude/course, rejects unusable fixes and suppresses repeated epochs.
A 92-byte fixed payload buffer and 24-byte command buffer are sufficient; the
HAL omits unreliable course and never infers UTC validity from fix validity.

Acquisition reads the existing UART1 NAV-PVT output rate, sets it to one message
per navigation epoch **in RAM only**, waits for ACK and verifies by readback.
Release/failed acquisition restores that rate with readback before serial mode,
descriptor and lease release. Configuration and restoration each have a one-second
deadline; uncertain restoration quarantines acquisition until reboot. Baud,
protocol enables, navigation rate, BBR and flash settings are preserved. UART1
must already accept/output UBX (the receiver’s default configuration).

Each poll drains at most 32 × 128 bytes and reports `Busy` when unavailable or
catching up. No worker thread or heap buffer is added. The explicit mock remains
available. Host fixtures and native builds qualify software behavior/linking;
physical receiver firmware, power, UART timing, RF, accuracy and peak task-stack
usage remain unqualified.

Reference: [M9 SPG4.04 interface description, UBX-21022436 R02](https://content.u-blox.com/sites/default/files/u-blox-M9-SPG-4.04_InterfaceDescription_UBX-21022436.pdf).

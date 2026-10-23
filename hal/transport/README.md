# Packet output, not a general transport HAL

`PacketSink::send(&[u8])` is an optional output boundary for packet-producing
logic such as telemetry. It submits one complete packet with explicit local
acceptance or rejection. It is not interthread communication, a socket API,
connection management, or a reliable-delivery protocol. `Transport` is only the
compatibility name for the identical trait, not a second abstraction.

A plain `FnMut(&[u8]) -> Result<(), DeviceError>` implements PacketSink. A caller
can inject a bounded collector or callback without a provider crate, heap
allocation, registration, or thread. Services therefore depend only on the
backend-free packet contract when their output must remain configurable.

## Standard-library UDP

All supported OS implementations share one Rust `std::net::UdpSocket`
implementation in `src/udp.rs`; there is no OS-specific socket code:

| Provider | Tested purpose |
| --- | --- |
| linux / macos / windows | Linux/macOS/Windows nonblocking std UDP with numeric addresses. |
| nuttx | NuttX nonblocking std UDP after the socket ABI/runtime qualification. |
| mock | Scripted tests; owned packets and errors, not network I/O. |

Platform selection exposes the existing constructor appropriate to each profile.
The NuttX-facing constructor contains no socket FFI or descriptor wrapper. Its
`UdpSender` binds an ephemeral IPv4 socket, connects it with `std::net`, sets
nonblocking mode, and adapts send results to `DeviceError`. The underlying
standard-library path is qualified by `tests/nuttx-std/udp`: the target build
checks the exact Rust/C socket ABI and then executes the provider in fresh NuttX
kernels.

The old scalar UDP C bridge is no longer a HAL implementation. A copy remains
only under `tests/nuttx/c` for the older core-only integration fixture, whose
Rust target has no standard library. That compatibility code is test-local and
must not be selected by a std-based application.

Ordinary browser execution must choose an actual web output or an explicit
network gateway. Pthread support does not provide direct browser UDP, and a
WebSocket is not an interchangeable UDP implementation.

## Acceptance and ownership

Success means local socket acceptance only, not remote delivery. Errors accept
no part of a packet. Borrowed bytes are not retained. The native provider rejects
empty packets by contract; the NuttX provider preserves the prior NuttX behavior
and accepts valid zero-length UDP datagrams. Both reject payloads larger than
65,507 bytes and map transient nonblocking/interrupted sends to `Busy`.

Run the native fixture with `NXRS_PLATFORM=linux cargo run -p nxrs-transport --example native_send -- 127.0.0.1:9000`.
The shared isolation tooling checks the compiled platform selection.
The NuttX std
provider is compiled and executed by the separate NuttX UDP qualification rather
than by a fake core-only target.

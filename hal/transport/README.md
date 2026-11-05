# Packet output

`PacketSink::send(&[u8])` is an optional output boundary for packet-producing
logic such as telemetry. It accepts or rejects one complete packet locally.
A plain `FnMut(&[u8]) -> Result<(), DeviceError>` implements the contract, so a
caller can inject a collector or callback without a provider registry or thread.
Services whose operation is specifically a UDP socket may use `std::net`
directly. Internal thread communication uses channels.

`PacketSink` is the sole trait name. The former `Transport` alias and
profile-dependent `open()`/`transport()` factories have been removed; applications
choose the concrete operation they intend to perform.

## Explicit UDP functionality

The additive `udp` Cargo feature exposes one shared implementation using
`std::net::UdpSocket`. It has no default, OS whitelist or hardware-profile
requirement. Contract-only builds remain `no_std`.

```toml
[dependencies]
nxrs-transport = { path = "../../hal/transport", features = ["udp"] }
```

| Operation | Public constructor | Packet contract |
| --- | --- | --- |
| Numeric IPv4 or IPv6 packet output | `udp::UdpTransport::connect(SocketAddr)` or `::bind(local, peer)` | Nonempty packets up to 65,507 bytes. |
| IPv4 datagram output | `udp::UdpSender::connect([u8; 4], port)` | Includes zero-length datagrams, preserving the qualified NuttX fixture behavior. |
| Scripted acceptance and failures | `MockTransport::default()` with a mock profile | Owned in-memory packets and explicit errors; no network I/O. |

Both UDP types share the same socket submission implementation. Inputs are typed
numeric addresses, so no name resolution occurs during submission. Sockets are
nonblocking; `Busy` requests a later owner retry, not spinning. Success means
local acceptance, not remote receipt. Validation rejects oversized packets
before submitting bytes.

Explicit UDP alongside a mock profile still performs real socket I/O. This is a
software operation selected by its Cargo feature, never a fallback from a
failed hardware factory. There is no per-OS UDP provider code or descriptor
wrapper. The NuttX socket path still requires its std ABI/runtime qualification.

## Verification

```sh
cargo test --locked -p nxrs-transport --features udp
cargo run --locked -p nxrs-transport --features udp --example native_send -- 127.0.0.1:9000
```

The loopback tests retain packet-byte and socket-owner checks, independent
sockets, invalid-packet rejection without submission, local-address inspection
and the distinct empty-datagram contracts. Run software tests with no profile
and with mock/native profiles. Physical NuttX UDP qualification remains under
`tests/nuttx-std/udp`.

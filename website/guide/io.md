# Networking and storage

Many firmware applications combine device control with communication and persistent data. Use standard Rust APIs for ordinary file and IP operations, then add an explicit ownership or delivery boundary where the workload needs one.

This chapter assumes you can run the [first application](first-application.md). The exercises below run on the host from the NXRS checkout. Target use additionally needs the selected profile's network and filesystem resources.

## Networking

Use `std::net` for supported TCP and UDP data paths. Configure the network interface, addressing, and any radio connection before starting application traffic. Wi-Fi or Bluetooth control belongs to the relevant capability implementation rather than the socket code.

A connection-handling component can own its socket, receive commands through a bounded inbox, and report state changes to the application. Define timeouts, retry limits, and shutdown behavior alongside the protocol. Bound pending messages so a disconnected peer does not accumulate unlimited work.

Protocol choice remains with the application. A destination can receive dedicated commands or records. Broadcast and remote acknowledgement are separate protocol choices, not consequences of using Rust channels or sockets.

### Try the packet-output example

The [transport crate](../../hal/transport/README.md) provides a small `PacketSink` contract and optional UDP implementation. Run its host tests:

```sh
cargo test --locked -p nxrs-transport --features udp
```

The tests check actual loopback packet bytes as well as invalid inputs and independent socket ownership. No profile is required: `udp` explicitly enables shared Rust socket functionality.

To send the example’s two datagrams to a receiver you are running on your own host:

```sh
cargo run --locked -p nxrs-transport --features udp --example native_send -- 127.0.0.1:9000
```

A successful send only means the local socket accepted the datagram. The example does not prove a receiver exists or that it processed the packet. Use the loopback tests for a self-contained receive check, or inspect the bytes at your receiver.

### Decide what success means

| Boundary | What success means | What you add if the product needs more |
| --- | --- | --- |
| Command queued to a network worker | Local admission into the bounded inbox | Completion reply and request deadline |
| UDP packet submitted | Local socket acceptance | Sequence IDs, acknowledgements and retry/deduplication policy |
| Bytes written to a TCP stream | Acceptance by the stream's write operation | Message framing and application-level response |
| Peer reports an operation complete | The meaning defined by your protocol | Durable-state or exactly-once guarantees, if required and implemented |

The [UDP API](../../hal/transport/src/udp.rs) accepts numeric addresses and reports backpressure. Its explicit constructor does not silently substitute a mock transport when a network is unavailable.

### Optional remote shell

Use the ESP32-S3 Wi-Fi lab fixture to open an interactive NuttX Shell (NSH)
over SSH and run a Rust command through it. SSH is off by default. The fixture
builds a flat, single-core NuttX image. Dropbear binds to an explicit IPv4
address on port 2222 and serves one password-authenticated terminal at a time.

#### Build and run the example

Follow the [NuttX tool setup](firmware.md), then build the fixture from the
repository root. Replace the example IP with a DHCP reservation for your board
on the same lab network as the host.

```sh
bash tests/nuttx-std/ssh/build.sh --out target/dropbear-ssh --bind 192.168.50.42
```

Use the [board flashing and credential setup](../../tests/nuttx-std/ssh/README.md)
to flash the image and prepare a native Dropbear host key, a verified
`known_hosts` entry, and disposable SSH and WPA2 password files. The host needs
OpenSSH, `dropbearkey`, and Python with `pyserial`. Close other serial clients
before the harness opens the board's local maintenance console.

Run the terminal case from the host. Set `SSH_TEST_SECRETS` to the private
directory from credential setup and substitute your serial port, SSID, IP
address, and verified fingerprint.

```sh
python3 tests/nuttx-std/ssh/run.py \
  --serial-port /dev/ttyACM0 --ssid nxrs-lab \
  --wifi-password-file "$SSH_TEST_SECRETS/wifi-password" \
  --host 192.168.50.42 --user nxrs_test \
  --password-file "$SSH_TEST_SECRETS/ssh-password" \
  --provision-key "$SSH_TEST_SECRETS/dropbear_ecdsa_host_key" \
  --known-hosts "$SSH_TEST_SECRETS/known_hosts" \
  --expected-fingerprint 'SHA256:REPLACE_WITH_VERIFIED_FINGERPRINT' \
  --build-evidence target/dropbear-ssh/ssh-build-evidence.json \
  --tmpfs --case pty
```

The harness connects Wi-Fi, provisions the empty lab filesystem over the local
console, starts Dropbear, checks a real OpenSSH terminal, and shuts down the
server and Wi-Fi. Look for `pty_rust_stdio_threads_resize_interrupt_rekey_channel_limits: passed`
and `complete_local_shutdown: passed`. The `--tmpfs` run uses disposable
credentials and does not test persistence across reboot.

#### Open the terminal yourself

With an already provisioned server reporting `state=listening` and the local
owner still running, connect from another terminal. The automated case above
stops the server when it finishes.

```sh
ssh -tt -p 2222 -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile="$SSH_TEST_SECRETS/known_hosts" nxrs_test@192.168.50.42
```

At the remote `nsh>` prompt, enter `ssh_probe stdio 0123456789abcdef01234567`.
When it prints `NXRS_SSH_READY`, type `input:0123456789abcdef01234567`.
The program prints markers for the joined Rust worker, stdout, and stderr,
ending with `NXRS_SSH_SUCCESS` and the same token. Enter `exit` to close the session. Use the local
console's `stop` and `quit` commands if you are running the lab owner
interactively. The current port supports interactive NSH only. Remote exec,
file transfer, and forwarding are disabled.

#### How it fits the capability model

SSH follows the same ownership and explicit-selection rules. The profile
selects the board and Wi-Fi HAL. NuttX Kconfig enables the native maintenance
service. The Wi-Fi HAL owns radio
access, and Dropbear owns the listener, authentication, and NSH terminal. The
Rust command uses ordinary `std::io` and `std::thread`. It has no SSH dependency
or portable SSH HAL API. The lab fixture starts and joins the native server and
keeps Wi-Fi available until shutdown completes.

See the [Dropbear integration reference](../../docs/dropbear-ssh.md) for the
supported profile, lifecycle, and remaining qualification.

## Files and storage

Use `std::fs` and `std::io` when the selected target supplies the required filesystem and device. Mount and configure the storage through NuttX and the platform integration before opening application files.

Keep record formats separate from the filesystem implementation. Decide when data is buffered, flushed, rotated, or discarded, and report write failures to the owner. A recorder worker can isolate slower storage operations from other processing while keeping a fixed buffer budget.

The existing [storage HAL](../../hal/storage/README.md) has a frame-record contract; it is not a general replacement for Rust file APIs. It exposes two distinct operations:

| Operation | Selection | Completion and cleanup |
| --- | --- | --- |
| Queued directory recording | `recording` Cargo feature and `recording::FileRecorder::create_new` | Append admits work; successful `flush()` is a commit barrier; `finish()` drains and joins |
| Synchronous append/rollback record file | `create_record_file` with a configured NuttX storage binding | Appends and rollback use the owned file directly; unavailable acquisition reports `Unsupported` |

The queued `FileRecorder` requires an existing parent directory on a trusted local filesystem with hard-link support. Its commit barrier covers publication of accepted records; remote filesystem failure semantics and directory durability after power loss are outside this adapter's contract. Check those requirements before choosing it for a board's storage medium. The [constructor contract](../../hal/storage/src/recording/writer.rs) states these limits.

Choose the contract intentionally. Do not treat queue admission as a committed file or dropping a recorder as a successful flush. The [storage documentation](../../hal/storage/README.md) records the exact rollback, sticky-error and buffer ownership behavior.

### Try the storage tests

```sh
cargo test --locked -p nxrs-storage --features recording
```

These host tests check actual committed records, queue limits, flush barriers, writer failures and explicit joining. They exercise the software path without requiring a hardware profile. Physical flash behavior and power-loss recovery need separate device tests.

## Design example: a connected data logger

A logger can have an input component, a processing component, a recorder, and a network connection manager. Typed records pass between owners; configuration describes the input device, storage, and network resources. This is a design example, not an additional checked-in application.

When connectivity is lost, decide whether to retain records locally, reject new uploads, or publish only the latest state after reconnecting. When storage fills, apply an explicit retention policy. Define how the application learns that policy was exercised: a counter, typed fault or rejected command is more useful than silently discarding required work.

Keep storage and network budgets separate. A peer that stops reading must not hold every buffer needed to keep recording. A worker can move an owned buffer or reserve a buffer from a fixed pool; avoid copying a large payload into an unbounded retry list.

[Designing an application](designing-applications.md) develops these ownership and failure decisions. The [examples index](examples.md) links runnable programs separately from design illustrations.

## Security and testing

Authentication, encryption, credential storage, and update policy are part of the product design. See the [security reference](../../docs/security.md) for the existing integration and deployment requirements.

Test short reads and writes, disconnects, timeouts, full storage, and shutdown while I/O is active. During shutdown, close admission, decide what to drain or cancel, wake blocked operations and join the owners. A timeout should leave an observable failure and a valid owner, not an apparently reusable resource with work still running.

Continue with [Testing and debugging](testing.md) for the workflow or [Performance and memory](performance.md) for retained buffers, queue storage and throughput.

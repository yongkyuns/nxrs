# NuttX simulator qualification

The x86-64 NuttX simulator is now retained as a **core-only compatibility
oracle**, not as evidence for the production Rust `std` execution model.

It boots a real NuttX sim kernel, exercises the same camera/recording/monitoring
behavior as the ESP32-S3 std integration, and independently checks target VFS,
UDP, descriptor lifetime and fault recovery. It is not `cargo run` on Linux
and its application calls resolve inside NuttX rather than escaping to host
libc.

## Architecture

`tests/nuttx/src/app.rs` contains the behavior shared with the ESP32-S3 std
integration. The simulator enters that behavior through the temporary
`tests/nuttx/src/lib.rs` no_std/staticlib compatibility entry because the
current `ARCH_SIM` target is built as `x86_64-unknown-none`, not as a
qualified Rust/NuttX std target. Its clock/sleep compatibility helper therefore
lives under `tests/nuttx`; no production Clock trait or aggregate NuttX HAL is
kept for this fixture.

Camera and storage depend directly on their NuttX capability crates, the same
providers used by the ESP32 integration.
The old scalar UDP bridge is now test-local under `tests/nuttx/c` and
`tests/nuttx/src/transport.rs`; it is **not** a production HAL implementation.
Production NuttX UDP uses `std::net::UdpSocket`.

The synthetic camera in `tests/nuttx/c/runner.c` is a real registered NuttX
read device. Each open owns a NuttX pthread producer, mutex and semaphore, and
the producer is joined on close. Recording uses NuttX tmpfs. UDP traverses
NuttX IPv4 loopback; TAP, host usrsock and host sockets are disabled.

Linux simulator random devices use host
[`getrandom(GRND_NONBLOCK)`](https://man7.org/linux/man-pages/man2/getrandom.2.html) through the
named native `host_random` boundary. Each request admits at most 256 bytes;
unready/error results propagate without a seed or software fallback. This lets
NuttX select UDP ports while retaining fail-closed `arc4random_buf` behavior.
Simulator qualification does not establish physical entropy or device keys.

## Validation

The compatibility fixture checks:

- unsupported-format cleanup;
- camera EOF and producer open/join counts;
- recording stop/restart while telemetry continues;
- exact record and packet bytes decoded independently in C/Python;
- descriptor-consumed close failures and exact FD reuse;
- storage write/truncate/seek/fsync fault handling and rollback poisoning;
- two complete invocations in one live NuttX kernel;
- deliberate target failure and corrupted-transcript rejection.

The simulator link gate inspects NuttX's renamed host-collision symbols and
relocations. Calls such as open/read/write/socket/pthread/clock must resolve
inside `nuttx.rel`; an unresolved host POSIX call fails qualification.

Recent trusted self-hosted runs, including **36373959027**, pass this
compatibility path after the ESP32 behavior was factored into shared Rust code.

## Build and run

```sh
sudo apt-get install bison flex gperf kconfig-frontends genromfs libncurses-dev zlib1g-dev
python3 -m pip install kconfiglib==14.1.0
rustup toolchain install 1.90.0 --profile minimal --component rust-src
rustup target add --toolchain 1.90.0 x86_64-unknown-none
git submodule update --init

bash tools/build-nuttx-sim.sh
python3 tests/host/test-nuttx-sim.py target/nuttx-sim/nuttx/nuttx
```

Rust core is rebuilt with the simulator's small/PIC code model via the scoped
`-Zbuild-std=core` path. No Rust std or host POSIX runtime is linked into this
fixture.

## Qualification boundary

Do not use this simulator as proof that Rust std works on NuttX. Ordinary
main/std is independently qualified on real NuttX targets, and the full
camera/storage/std-UDP integration is qualified under ESP32-S3 QEMU.

The simulator remains useful because it cheaply exercises same-kernel reentry,
fault injection and target/host-link isolation. It can be removed later if those
properties are covered more directly by std-based target tests without relying
on host-backed simulator ABIs.

It does not emulate ESP32-S3 instructions, DMA, interrupts, radios or physical
sensor registers and establishes no hardware real-time or persistence guarantee.

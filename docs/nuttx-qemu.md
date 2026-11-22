# ESP32-S3 QEMU qualification

ESP32-S3 QEMU now qualifies the full camera/storage/telemetry integration through
an ordinary Rust `main()` and the patched NuttX `std` runtime. QEMU remains a
board/execution profile, not a separate application backend.

## Current result

Trusted self-hosted run **36375036817** passed at
`7e0c26a8a19c2ace54de2c172a5c5691965af754`.

The build passed:

- the normal Cargo binary and rustc-generated `main`/std startup path;
- the 47-observation baseline C/Rust ABI gate;
- the 42-observation IPv4 UDP/socket ABI gate for the exact Xtensa libc;
- final 32-bit Xtensa/NuttX link inspection;
- camera and storage NuttX providers;
- the production `nxrs-transport-nuttx` `std::net::UdpSocket` provider;
- target preemption, descriptor-lifetime and storage-fault controls.

The QEMU execution reached `RC_RUST_STD_MAIN BEGIN`, completed the product
scenario, printed `RC_RUST_STD_MAIN PASS`, returned cleanly to NSH, and passed
the independent record/UDP/ABI oracle. A separate injected-failure command was
rejected. Artifact **10950034289** has archive SHA-256
`886f1abf27994de1c7fe1968de9fe4ecbc07f6aad0b31458b3022738a39a1db6`.

## Architecture

`tests/nuttx/src/main.rs` is the ordinary Rust std entry for this qualification.
It uses `std::time::Instant`, `std::thread::sleep`, and the production NuttX
std UDP provider. Shared product behavior lives in `tests/nuttx/src/app.rs` so
the temporary core-only simulator and the std image exercise the same
camera/recording/monitoring semantics.

The NuttX application Makefile consumes the partially linked Cargo binary and
renames only rustc's generated C-ABI `main` to `rc_rust_std_main`; the call
through `std::rt` is preserved. The C `runner.c` is now an external
qualification harness: it registers the synthetic camera, owns the independent
UDP receiver and file verifier, and invokes that generated std entry with normal
argc/argv. It is not a Rust application launcher or alternate service
implementation.

Camera and storage still use their narrow target C bridges because those
providers depend on configured NuttX driver/VFS structures and the existing
fault-injection fixtures. UDP does **not**: the std integration image excludes
the old test-local `transport.c`, and final-link inspection rejects
`rc_nx_udp_open` and `rc_nx_send`.

The socket gate is compiled from the exact C headers and exact rebuilt Rust
libc used by this image. Xtensa currently uses pinned libc 0.2.174; RV32 uses
0.2.175. Both source bindings are blob-pinned and receive the same private
`sockaddr_storage` alignment correction before qualification.

## Coverage

The scenario checks unsupported camera-format cleanup, descriptor ownership,
target timing, four camera frames, recording stop/restart with uninterrupted
monitoring, three exact tmpfs records, four exact UDP summaries, storage rollback
faults, cleanup, and independent C decoding.

`EXAMPLES_NXRS_PREEMPTION` also requires a higher-priority FIFO task to
wake four times while a lower-priority CPU-bound task is runnable on single-core
NuttX. A scheduler-locked negative control must fail that progress condition.

The std runtime is invoked once per QEMU boot in this full integration test.
Repeated process-runtime entry in one flat NuttX address space is not claimed.
The older core-only x86 simulator deliberately retains its two-invocation cleanup
test; the separate ordinary-std lifecycle probe covers fresh-kernel std startup
and cleanup repeatedly.

## Reproduce on Linux x86-64

Install the dependencies from the QEMU workflow, then:

```sh
git submodule update --init
bash tools/install-qemu-tools.sh
bash tools/build-nuttx-qemu.sh
source target/qemu-tools/environment.sh
python3 tests/host/test-nuttx-sim.py "$(command -v qemu-system-xtensa)" \
  --qemu-image target/nuttx-qemu/nuttx/nuttx.merged.bin \
  --log target/nuttx-qemu/console.log \
  --cycles 1 --require-std-main
```

The compiler, rust-src, GCC and QEMU archives are pinned and hash-verified. The
build uses the same private NuttX std fixes as the ordinary runtime
qualification; installed toolchains are not modified.

## Tracker device simulation

The [tracker simulator](../tests/nuttx-std/tracker-sim/README.md) uses the same
NuttX/Rust build path with local device models and accelerated guest time.
It exercises the serial, I²C, SPI, touch, settings and IP driver paths.
The full motion tracker scenario also drives the app dashboard and pairs an
independent TLS client through NuttX GATT and an emulated HCI UART controller.

## Limits

Camera bytes are synthetic and storage is tmpfs. This does not qualify a physical
ESP32-S3 camera, DMA/cache coherency, Wi-Fi/BLE radio hardware, PSRAM, persistent storage,
hardware deadlines or electrical behavior. Loopback proves the target
network/std path, not a physical radio. The private std/libc corrections remain
scoped compatibility patches rather than a claim of stock upstream NuttX std
support.
